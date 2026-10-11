#!/usr/bin/env python3
"""Measure Linux filesystem space overhead using disposable loopback images.

Uses optional BLAKE3 verification; --no-verify needs no Python packages.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import fcntl
import hashlib
import html
import json
import math
import os
from pathlib import Path
import platform
import random
import re
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

try:
    from blake3 import blake3
except ImportError:
    blake3 = None

BASE = Path(__file__).resolve().parent
FILESYSTEMS = ("ext4", "xfs", "btrfs", "f2fs", "ntfs", "exfat")
CHUNK = 1024 * 1024
SPACE_ERRORS = (errno.ENOSPC, errno.EDQUOT)
F2FS_RELEASE = 0x8008F512  # _IOR(0xf5, 18, __u64), Linux generic ioctl ABI
F2FS_GET_COMPRESS_BLOCKS = 0x8008F511
MANIFEST = ".fsbench-manifest.json"


class BenchError(RuntimeError):
    pass


class CleanupError(BenchError):
    pass


class StopFill(Exception):
    pass


def log(message: str) -> None:
    print(message, flush=True)


def parse_size(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(B|[KMGT](?:i?B)?)?\s*", value, re.I)
    if not match:
        raise argparse.ArgumentTypeError("Use a size such as 100GB, 100GiB or 512MiB")
    number, suffix = match.groups()
    suffix = (suffix or "B").upper()
    power = "KMGT".index(suffix[0]) + 1 if suffix[0] in "KMGT" else 0
    result = int(float(number) * (1024 if "I" in suffix else 1000) ** power)
    if result <= 0:
        raise argparse.ArgumentTypeError("Size must be positive")
    return result


def human(value: int | float | None) -> str:
    if value is None:
        return "—"
    if abs(value) < 1024:
        return f"{value:,.0f} B"
    for unit in ("KiB", "MiB", "GiB", "TiB", "PiB"):
        value /= 1024
        if abs(value) < 1024:
            return f"{value:,.2f} {unit}"
    return f"{value:,.2f} PiB"


def new_hasher(data: bytes = b""):
    if blake3 is None:
        raise BenchError("Missing Python package 'blake3'. Install with: python3 -m venv .venv && "
                         ".venv/bin/python -m pip install -r requirements.txt. "
                         "Then run: sudo .venv/bin/python fsbench.py --image-dir /path/to/drive. "
                         "Or use --no-verify to disable hashing without this dependency.")
    return blake3(data)


class Progress:
    """Flushed, time-throttled byte progress, including while inside a large file."""
    def __init__(self, label: str, total: int | None, interval: float = 5, initial: int = 0):
        self.label, self.total, self.interval = label, total, interval
        self.completed = initial
        self.initial = initial
        self.started = self.last_update = time.monotonic()
        log(f"{label}: {human(initial)}" + (f" / {human(total)}" if total is not None else ""))

    def advance(self, count: int, current: str = "") -> None:
        self.completed += count
        now = time.monotonic()
        if now - self.last_update < self.interval:
            return
        elapsed = max(now - self.started, 1e-9)
        rate = (self.completed - self.initial) / elapsed
        status = human(self.completed)
        if self.total is not None:
            percent = 100 * self.completed / self.total if self.total else 100
            status += f" / {human(self.total)} ({percent:.1f}%)"
            if rate > 0:
                seconds = max(0, self.total - self.completed) / rate
                status += f"; ETA {seconds / 60:.1f} min"
        log(f"{self.label}: {status}; {human(rate)}/s" + (f"; {current}" if current else ""))
        self.last_update = now

    def finish(self) -> None:
        log(f"{self.label}: finished {human(self.completed)} in {time.monotonic() - self.started:.1f} s")


def atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o644)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def file_hash(path: Path, progress: Progress | None = None, legacy_digest=None) -> str:
    digest = new_hasher()
    with path.open("rb") as stream:
        while chunk := stream.read(CHUNK):
            digest.update(chunk)
            if legacy_digest is not None:
                legacy_digest.update(chunk)
            if progress is not None:
                progress.advance(len(chunk), path.name)
    return digest.hexdigest()


def hash_files(root: Path, files: list[Path], label: str, interval: float = 5) -> dict[str, str]:
    progress = Progress(label, sum(path.stat().st_size for path in files), interval)
    hashes = {path.relative_to(root).as_posix(): file_hash(path, progress) for path in files}
    progress.finish()
    return hashes


def discover_data(root: Path) -> list[Path]:
    if not root.is_dir():
        raise BenchError(f"Data directory does not exist: {root}")
    files = []
    for directory, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            path = Path(directory) / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise BenchError(f"Only regular files and directories are supported: {path}")
        files.extend(Path(directory) / name for name in names if name != MANIFEST)
    if not files or not sum(path.stat().st_size for path in files):
        raise BenchError("Dataset must contain at least one nonempty regular file")
    # Spread large file types evenly. Small files are spread between large files,
    # rather than putting an entire type at the start of each capacity pass.
    small = sorted((p for p in files if p.stat().st_size <= 16384), key=lambda p: p.relative_to(root).as_posix())
    large = sorted((p for p in files if p.stat().st_size > 16384), key=lambda p: p.relative_to(root).as_posix())
    if not large:
        return small
    ordered = []
    for index, path in enumerate(large):
        ordered.append(path)
        ordered.extend(small[index * len(small) // len(large):(index + 1) * len(small) // len(large)])
    return ordered


def generate_data(root: Path, size: int, small_files: int, seed: int, progress_interval: float = 5,
                  verify: bool = True) -> dict[str, str] | None:
    """Generate an exact-size, reproducible mixed dataset; never overwrite input."""
    settings = {"version": 1, "size": size, "small_files": small_files, "seed": seed}
    if root.exists():
        manifest = root / MANIFEST
        if not manifest.exists():
            raise BenchError(f"{root} exists without a benchmark manifest; use --data-dir for existing data")
        saved = json.loads(manifest.read_text())
        if saved.get("settings") != settings:
            raise BenchError(f"Demo settings changed; choose a new --demo-dir (existing data is retained): {root}")
        actual = {p.relative_to(root).as_posix(): p for p in discover_data(root)}
        if set(actual) != set(saved["files"]):
            raise BenchError("Demo file list changed; choose a new --demo-dir")
        for name, record in saved["files"].items():
            if actual[name].stat().st_size != record["size"]:
                raise BenchError(f"Demo data changed: {name}; choose a new --demo-dir")
        if not verify:
            log(f"Reusing demo data; checksum verification disabled: {root}")
            return None
        algorithm = saved.get("hash_algorithm", "sha256")
        if algorithm not in ("blake3", "sha256", None):
            raise BenchError(f"Unsupported manifest hash algorithm: {algorithm}")
        if algorithm == "sha256":
            log("Upgrading existing manifest to BLAKE3 (legacy checks verified in the same read pass)")
        elif algorithm is None:
            log("Creating BLAKE3 checksum baseline for previously unverified demo data")
        progress = Progress("Validating workload (BLAKE3)", sum(p.stat().st_size for p in actual.values()), progress_interval)
        hashes = {}
        for name, record in saved["files"].items():
            legacy = hashlib.sha256() if algorithm == "sha256" else None
            checksum = file_hash(actual[name], progress, legacy)
            expected = legacy.hexdigest() if legacy is not None else checksum
            if algorithm is not None and expected != record[algorithm]:
                raise BenchError(f"Demo data changed: {name}; choose a new --demo-dir")
            hashes[name] = checksum
        progress.finish()
        if algorithm != "blake3":
            saved["hash_algorithm"] = "blake3"
            saved["files"] = {name: {"size": actual[name].stat().st_size, "blake3": hashes[name]} for name in hashes}
            atomic_write(manifest, json.dumps(saved, indent=2))
            log("Manifest upgraded to BLAKE3")
        log(f"Reusing verified demo data: {root}")
        return hashes
    if size < small_files * 4096 + 65536:
        raise BenchError("Dataset is too small for --small-files; reduce that count or increase --dataset-size")
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".fsbench-data-", dir=root.parent))
    rng = random.Random(seed)
    records = {}
    progress = Progress("Generating demo workload", size, progress_interval)
    try:
        small_bytes = 0
        for index in range(small_files):
            length = rng.randint(64, 4096)
            relative = f"small/group-{index // 100:04d}/item-{index:06d}.json"
            path = staging / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            template = json.dumps({"id": index, "enabled": True, "description": "demo configuration data"}).encode() + b"\n"
            content = (template * (length // len(template) + 1))[:length]
            path.write_bytes(content)
            small_bytes += length
            records[relative] = {"size": length}
            if verify:
                records[relative]["blake3"] = new_hasher(content).hexdigest()
            progress.advance(length, relative)
        remaining = size - small_bytes
        index = 0
        while remaining:
            length = min(8 * CHUNK, remaining)
            kind = ("random", "text", "random", "log")[index % 4]
            extension = {"random": "bin", "text": "txt", "log": "log"}[kind]
            relative = f"large/{index:06d}-{kind}.{extension}"
            path = staging / relative
            path.parent.mkdir(exist_ok=True)
            digest = new_hasher() if verify else None
            with path.open("wb") as stream:
                left = length
                while left:
                    count = min(CHUNK, left)
                    if kind == "random":
                        content = rng.randbytes(count)
                    else:
                        line = (f"record={index} Linux filesystem space benchmark: "
                                "configuration, documents, application logs, repeatable sample data.\n").encode()
                        content = (line * (count // len(line) + 1))[:count]
                    stream.write(content)
                    if digest is not None:
                        digest.update(content)
                    progress.advance(count, relative)
                    left -= count
            records[relative] = {"size": length}
            if digest is not None:
                records[relative]["blake3"] = digest.hexdigest()
            remaining -= length
            index += 1
        (staging / MANIFEST).write_text(json.dumps({"settings": settings, "hash_algorithm": "blake3" if verify else None,
                                                   "files": records}, indent=2))
        staging.rename(root)
        progress.finish()
        log(f"Generated {human(size)} demo data: {root}")
        return {name: record["blake3"] for name, record in records.items()} if verify else None
    finally:
        if staging.exists():
            shutil.rmtree(staging)


@dataclass(frozen=True)
class Variant:
    filesystem: str
    compression: bool = False
    driver: str = ""

    @property
    def label(self) -> str:
        fs = {"ext4": "EXT4", "xfs": "XFS", "btrfs": "Btrfs", "f2fs": "F2FS", "ntfs": "NTFS", "exfat": "exFAT"}[self.filesystem]
        algorithm = {"btrfs": "zstd:3", "f2fs": "zstd, released blocks", "ntfs": "LZNT1"}
        return fs + (f" ({algorithm[self.filesystem]})" if self.compression else " (default)")

    def format_command(self, device: str, reserved: float) -> list[str]:
        commands = {
            "ext4": ["mkfs.ext4", "-F", "-m", str(reserved), "-E", "nodiscard,lazy_itable_init=0,lazy_journal_init=0"],
            "xfs": ["mkfs.xfs", "-f", "-K"],
            "btrfs": ["mkfs.btrfs", "-f", "-K"],
            "f2fs": ["mkfs.f2fs", "-f", "-t", "0"],
            "ntfs": ["mkfs.ntfs", "-F", "-Q"],
            "exfat": ["mkfs.exfat"],
        }
        command = commands[self.filesystem].copy()
        if self.compression and self.filesystem == "f2fs":
            command += ["-O", "extra_attr,compression"]
        return command + [device]

    def mount_command(self, device: str, mountpoint: Path) -> list[str]:
        options = ["rw", "noatime", "nodev", "nosuid", "noexec"]
        fs_type = self.filesystem
        if self.filesystem == "btrfs":
            options += ["nodiscard", "compress=zstd:3" if self.compression else "compress=no"]
        elif self.filesystem == "f2fs" and self.compression:
            options += ["compress_algorithm=zstd", "compress_extension=*"]
        elif self.filesystem == "ntfs":
            fs_type = self.driver
            if self.driver == "ntfs-3g":
                options += ["compression" if self.compression else "nocompression"]
        return ["mount", "-t", fs_type, "-o", ",".join(options), device, str(mountpoint)]


class Runner:
    def __init__(self, verbose: bool = False, progress_interval: float = 5) -> None:
        self.commands: list[dict] = []
        self.verbose, self.progress_interval = verbose, progress_interval

    def run(self, command: list[str], *, check: bool = True, timeout: int = 300) -> subprocess.CompletedProcess:
        start = time.monotonic()
        try:
            if self.verbose:
                log("  Running: " + shlex.join(command))
                with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                      errors="replace", env={**os.environ, "LC_ALL": "C"}) as process:
                    try:
                        while True:
                            remaining = timeout - (time.monotonic() - start)
                            if remaining <= 0:
                                raise subprocess.TimeoutExpired(command, timeout)
                            try:
                                stdout, stderr = process.communicate(timeout=min(self.progress_interval, remaining))
                                break
                            except subprocess.TimeoutExpired:
                                log(f"  Still running {command[0]} ({time.monotonic() - start:.1f} s)")
                        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                    except BaseException:
                        process.kill()
                        process.communicate()
                        raise
            else:
                result = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=timeout,
                                        env={**os.environ, "LC_ALL": "C"})
        except subprocess.TimeoutExpired as exc:
            self.commands.append({"command": command, "error": "timeout", "seconds": time.monotonic() - start})
            raise BenchError(f"Command timed out: {shlex.join(command)}") from exc
        self.commands.append({"command": command, "returncode": result.returncode, "stdout": result.stdout,
                              "stderr": result.stderr, "seconds": time.monotonic() - start})
        if check and result.returncode:
            raise BenchError(f"{shlex.join(command)} failed ({result.returncode}): {(result.stderr or result.stdout).strip()}")
        if self.verbose:
            log(f"  {command[0]} finished in {time.monotonic() - start:.1f} s (exit {result.returncode})")
        return result


def format_command(variant: Variant, device: str, reserved: float, runner: Runner) -> list[str]:
    command = variant.format_command(device, reserved)
    if variant.filesystem == "exfat" and shutil.which("mkfs.exfat"):
        # Recent exfatprogs may automatically partition a whole disk. Older
        # versions always create a bare filesystem and do not accept this flag.
        help_result = runner.run(["mkfs.exfat", "--help"], check=False, timeout=10)
        help_text = help_result.stdout + help_result.stderr
        if "--partition-table" in help_text:
            # Some exfatprogs releases advertise the long option but only
            # correctly parse its short form.
            command[-1:-1] = ["-P", "none"]
        if "--no-discard" in help_text:
            command.insert(-1, "-K")
    return command


def is_mounted(path: Path) -> bool:
    # /proc uses octal escapes for whitespace and backslashes in mount paths.
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        field = line.split()[4]
        unescaped = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), field)
        if unescaped == str(path):
            return True
    return False


class DiskImage:
    """Format only the loop device whose backing file we created and verified."""
    def __init__(self, image_dir: Path, size: int, runner: Runner):
        self.image_dir, self.size, self.runner = image_dir, size, runner
        self.work: Path | None = None
        self.image: Path | None = None
        self.mountpoint: Path | None = None
        self.loop: str | None = None

    def __enter__(self) -> DiskImage:
        self.image_dir.mkdir(parents=True, exist_ok=True)
        self.work = Path(tempfile.mkdtemp(prefix="fsbench-", dir=self.image_dir))
        self.image = self.work / "disk.raw"
        self.mountpoint = self.work / "mount"
        try:
            self.mountpoint.mkdir()
            with self.image.open("xb") as stream:
                stream.truncate(self.size)
            self.loop = self.runner.run(["losetup", "--find", "--show", str(self.image)]).stdout.strip()
            if not re.fullmatch(r"/dev/loop\d+", self.loop):
                raise BenchError(f"Unexpected loop device returned by losetup: {self.loop!r}")
            info = json.loads(self.runner.run(["losetup", "--json", "--list", "--output", "NAME,BACK-FILE", self.loop]).stdout)
            devices = info.get("loopdevices", [])
            if len(devices) != 1 or devices[0]["name"] != self.loop or Path(devices[0]["back-file"]).resolve() != self.image.resolve():
                raise BenchError("Loop backing-file verification failed; refusing to format")
            return self
        except BaseException:
            self.cleanup()
            raise

    def cleanup(self) -> None:
        # Defer additional interrupts until the resources have been released.
        # This runs on the main thread, including during KeyboardInterrupt unwind.
        previous = {sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM)}
        try:
            self._cleanup()
        except (BenchError, OSError) as exc:
            if isinstance(exc, CleanupError):
                raise
            raise CleanupError(f"Cleanup failed; inspect retained image {self.image}: {exc}") from exc
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    def _cleanup(self) -> None:
        # Never recursively remove this directory: if unmount failed that would
        # delete files inside a live filesystem. Keep image and print recovery.
        if self.mountpoint and is_mounted(self.mountpoint):
            result = self.runner.run(["umount", str(self.mountpoint)], check=False)
            if result.returncode or is_mounted(self.mountpoint):
                raise CleanupError(f"Could not unmount {self.mountpoint}. Image retained at {self.image}. "
                                   f"Recovery: sudo umount {shlex.quote(str(self.mountpoint))}; "
                                   f"sudo losetup -d {self.loop}")
        # The attach command may have completed just as it was interrupted, before
        # its output was assigned. Discover associations by our private backing
        # file rather than risk leaving that loop attached or unlinking its image.
        associated = []
        if self.image and self.image.exists():
            found = self.runner.run(["losetup", "--associated", str(self.image), "--noheadings", "--output", "NAME"], check=False)
            if found.returncode:
                raise CleanupError(f"Cannot check loop associations; retained image: {self.image}")
            associated = found.stdout.split()
        for device in associated:
            if not re.fullmatch(r"/dev/loop\d+", device):
                raise CleanupError(f"Unexpected loop association; retained image: {self.image}")
            result = self.runner.run(["losetup", "--detach", device], check=False)
            if result.returncode:
                raise CleanupError(f"Could not detach {device}; image retained at {self.image}")
        self.loop = None
        if self.mountpoint and self.mountpoint.exists():
            self.mountpoint.rmdir()
        if self.image:
            self.image.unlink(missing_ok=True)
        if self.work and self.work.exists():
            self.work.rmdir()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.cleanup()


def sync_filesystem(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.syncfs(fd) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), str(path))
    finally:
        os.close(fd)


def stat_space(path: Path) -> dict:
    s = os.statvfs(path)
    unit = s.f_frsize or s.f_bsize
    return {"total_bytes": s.f_blocks * unit, "free_bytes": s.f_bfree * unit,
            "available_bytes": s.f_bavail * unit, "used_bytes": (s.f_blocks - s.f_bfree) * unit,
            "reserved_bytes": max(0, s.f_bfree - s.f_bavail) * unit, "block_size": unit,
            "inodes_total": s.f_files, "inodes_free": s.f_ffree}


def scan_files(root: Path) -> dict:
    result = {"logical_bytes": 0, "allocated_bytes": 0, "files": 0, "directories": 0,
              "directory_allocated_bytes": 0}
    if not root.exists():
        return result
    for directory, dirs, names in os.walk(root):
        result["directories"] += 1
        result["directory_allocated_bytes"] += Path(directory).stat().st_blocks * 512
        for name in names:
            info = (Path(directory) / name).stat()
            result["logical_bytes"] += info.st_size
            result["allocated_bytes"] += info.st_blocks * 512
            result["files"] += 1
    return result


def parse_btrfs_df(output: str) -> dict:
    result = {"metadata_used_physical_bytes": 0, "metadata_allocated_physical_bytes": 0,
              "system_used_physical_bytes": 0, "data_used_physical_bytes": 0, "groups": []}
    for line in output.splitlines():
        match = re.match(r"(Data|Metadata|System),\s*(\w+): total=(\d+), used=(\d+)", line)
        if not match:
            continue
        kind, profile, total, used = match.groups()
        # These are single-device images; only single and DUP are expected.
        if profile not in ("single", "DUP"):
            raise BenchError(f"Unexpected Btrfs allocation profile: {profile}")
        ratio = 2 if profile == "DUP" else 1
        result["groups"].append({"type": kind, "profile": profile, "allocated": int(total), "used": int(used)})
        result[f"{kind.lower()}_used_physical_bytes"] += int(used) * ratio
        if kind == "Metadata":
            result["metadata_allocated_physical_bytes"] += int(total) * ratio
    if not result["groups"]:
        raise BenchError("Cannot parse btrfs filesystem df --raw output")
    return result


def parse_compsize(output: str) -> dict:
    match = re.search(r"^TOTAL\s+\d+%\s+(\d+)\s+(\d+)\s+(\d+)\s*$", output, re.M)
    if not match:
        raise BenchError("Cannot parse compsize -b output")
    disk, uncompressed, referenced = map(int, match.groups())
    return {"disk_bytes": disk, "uncompressed_extent_bytes": uncompressed, "referenced_bytes": referenced,
            "saved_bytes": uncompressed - disk,
            "saving_percent": 100 * (1 - disk / uncompressed) if uncompressed else 0}


def snapshot(mountpoint: Path, image: Path, variant: Variant, runner: Runner, disk_size: int) -> dict:
    log("  Syncing filesystem and collecting space statistics")
    sync_filesystem(mountpoint)
    space = stat_space(mountpoint)
    files = scan_files(mountpoint / "payload")
    result = {"space": space, "files": files, "image_allocated_bytes": image.stat().st_blocks * 512,
              "unaddressable_bytes": disk_size - space["total_bytes"], "native": {}, "warnings": []}
    if variant.filesystem == "btrfs" and shutil.which("btrfs"):
        for args, key in ((["filesystem", "df", "--raw"], "btrfs_df"),
                          (["filesystem", "usage", "--raw"], "btrfs_usage")):
            raw = runner.run(["btrfs", *args, str(mountpoint)], check=False)
            result["native"][key] = raw.stdout + raw.stderr
            if key == "btrfs_df" and raw.returncode == 0:
                try:
                    result["btrfs"] = parse_btrfs_df(raw.stdout)
                except BenchError as exc:
                    result["warnings"].append(str(exc))
    if variant.filesystem == "btrfs" and files["files"] and shutil.which("compsize"):
        raw = runner.run(["compsize", "-b", str(mountpoint / "payload")], check=False, timeout=1800)
        result["native"]["compsize"] = raw.stdout + raw.stderr
        if raw.returncode == 0:
            try:
                result["compression"] = parse_compsize(raw.stdout)
            except BenchError as exc:
                result["warnings"].append(str(exc))
        else:
            result["warnings"].append("compsize failed; exact Btrfs extent compression is unavailable")
    # st_blocks on compressed Btrfs may account for uncompressed extents.
    data_bytes = files["allocated_bytes"]
    if variant.filesystem == "btrfs" and variant.compression:
        data_bytes = result.get("compression", {}).get("disk_bytes")
    result["file_data_bytes"] = data_bytes
    # Includes filesystem regions excluded from statvfs, directories, journals,
    # inline data and allocator accounting. Deliberately NOT called exact metadata.
    residual = disk_size - space["free_bytes"] - data_bytes if data_bytes is not None else None
    result["non_file_overhead_estimate_bytes"] = residual if residual is not None and residual >= 0 else None
    if residual is not None and residual < 0:
        result["warnings"].append("File allocation and statvfs accounting disagree; overhead estimate omitted")
    return result


def enable_ntfs_compression(root: Path) -> None:
    name = "system.ntfs_attrib_be"
    flags = struct.unpack(">I", os.getxattr(root, name))[0]
    os.setxattr(root, name, struct.pack(">I", flags | 0x800))


def release_f2fs(path: Path) -> int:
    # Read-only descriptor avoids EBUSY from an open writer. No future writes to
    # this file are attempted after release (kernel makes the file read-only).
    with path.open("rb") as stream:
        compressed = bytearray(8)
        fcntl.ioctl(stream.fileno(), F2FS_GET_COMPRESS_BLOCKS, compressed, True)
        if not struct.unpack("=Q", compressed)[0]:
            return 0
        released = bytearray(8)
        fcntl.ioctl(stream.fileno(), F2FS_RELEASE, released, True)
        return struct.unpack("=Q", released)[0] * 4096


class Writer:
    def __init__(self, mountpoint: Path, image_dir: Path, variant: Variant, args: argparse.Namespace):
        self.mountpoint, self.image_dir, self.variant, self.args = mountpoint, image_dir, variant, args
        self.logical = 0
        self.files = 0
        self.released_bytes = 0
        self.last_log = time.monotonic()
        self.last_host_check = 0.0
        self.progress: Progress | None = None

    def check_host_space(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self.last_host_check >= 1:
            self.last_host_check = now
            if shutil.disk_usage(self.image_dir).free < self.args.host_reserve:
                raise BenchError(f"Host free space fell below --host-reserve ({human(self.args.host_reserve)}); "
                                 "stopping to avoid exhausting the image's backing filesystem")

    def copy(self, source: Path, target: Path, *, capacity: bool, limit: int | None = None) -> int:
        """Buffered byte copies, no reflinks/hardlinks/holes; fsync every file."""
        self.check_host_space()
        written = 0
        reason = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as src, target.open("xb", buffering=0) as dst:
                while chunk := src.read(min(CHUNK, limit - written) if limit is not None else CHUNK):
                    self.check_host_space()
                    available = stat_space(self.mountpoint)["available_bytes"]
                    if capacity:
                        allowance = min(len(chunk), self.args.max_logical_bytes - self.logical - written,
                                        max(0, available - self.args.leave_free))
                        if allowance <= 0:
                            reason = "logical limit" if self.logical + written >= self.args.max_logical_bytes else "available-space limit"
                            break
                        if allowance < len(chunk):
                            reason = "logical limit" if self.logical + written + allowance >= self.args.max_logical_bytes else "available-space limit"
                        chunk = chunk[:allowance]
                    elif available < len(chunk):
                        raise OSError(errno.ENOSPC, "Shared dataset exceeds ordinary-user available space")
                    view = memoryview(chunk)
                    while view:
                        count = dst.write(view)
                        if not count:
                            raise OSError(errno.EIO, "Write made no progress")
                        view = view[count:]
                        written += count
                        if self.progress is not None:
                            self.progress.advance(count, f"{source.name}; available {human(available)}")
                    if reason:
                        break
                os.fsync(dst.fileno())
        except OSError as exc:
            # Delayed allocation ENOSPC from fsync is also capacity exhaustion.
            # Discard this file rather than count bytes which may not be durable.
            target.unlink(missing_ok=True)
            if self.progress is not None:
                self.progress.completed -= written
            if capacity and exc.errno in SPACE_ERRORS:
                raise StopFill("ENOSPC" if exc.errno == errno.ENOSPC else "EDQUOT") from exc
            raise
        if not written and source.stat().st_size:
            target.unlink(missing_ok=True)
            raise StopFill(reason or "available-space limit")
        if self.variant.filesystem == "f2fs" and self.variant.compression:
            self.released_bytes += release_f2fs(target)
        self.logical += written
        self.files += 1
        if self.progress is None and time.monotonic() - self.last_log >= 10:
            log(f"  Written {human(self.logical)}, {self.files:,} files; free {human(stat_space(self.mountpoint)['available_bytes'])}")
            self.last_log = time.monotonic()
        if reason == "available-space limit" and stat_space(self.mountpoint)["available_bytes"] > self.args.leave_free:
            # Compression can make new space visible at fsync/close/release.
            # Continue the fill if that happened during this final partial file.
            reason = None
        if reason:
            raise StopFill(reason)
        return written


def fill_tail(writer: Writer, source: Path, payload: Path, reason: str) -> str:
    """After ENOSPC, probe smaller files so a failed large write doesn't hide free space."""
    size = min(CHUNK, source.stat().st_size)
    if not size:
        return reason
    number = 0
    minimum = stat_space(writer.mountpoint)["block_size"]
    while size >= minimum and writer.logical < writer.args.max_logical_bytes:
        try:
            writer.copy(source, payload / "tail" / f"fragment-{number:06d}.bin", capacity=True, limit=size)
            number += 1
        except StopFill as exc:
            reason = str(exc)
            if reason not in ("ENOSPC", "EDQUOT"):
                return reason
            size //= 4
    return "logical limit" if writer.logical >= writer.args.max_logical_bytes else reason


def verify_reference(data_root: Path, files: list[Path], target_root: Path, hashes: dict, progress_interval: float = 5) -> None:
    progress = Progress("  Verifying read-back (BLAKE3)", sum(path.stat().st_size for path in files), progress_interval)
    for source in files:
        relative = source.relative_to(data_root).as_posix()
        if file_hash(target_root / relative, progress) != hashes[relative]:
            raise BenchError(f"Read-back checksum mismatch: {relative}")
    progress.finish()


def run_variant(variant: Variant, args: argparse.Namespace, files: list[Path], hashes: dict) -> dict:
    result = {"variant": asdict(variant), "label": variant.label, "status": "error", "warnings": []}
    runner = Runner(verbose=True, progress_interval=args.progress_interval)
    start = time.monotonic()
    try:
        with DiskImage(args.image_dir, args.disk_size, runner) as disk:
            started = time.monotonic()
            runner.run(format_command(variant, disk.loop, args.ext4_reserved_percent, runner), timeout=1800)
            result["format_seconds"] = time.monotonic() - started
            runner.run(variant.mount_command(disk.loop, disk.mountpoint))
            if not is_mounted(disk.mountpoint):
                raise BenchError("Mount command succeeded without a mounted filesystem")
            result["empty"] = snapshot(disk.mountpoint, disk.image, variant, runner, args.disk_size)
            native_commands = {"ext4": ["tune2fs", "-l", disk.loop], "xfs": ["xfs_info", str(disk.mountpoint)],
                               "ntfs": ["ntfsinfo", "-m", "-f", disk.loop]}
            native = native_commands.get(variant.filesystem)
            if native and shutil.which(native[0]):
                raw = runner.run(native, check=False)
                result["format_info"] = raw.stdout + raw.stderr
            payload = disk.mountpoint / "payload"
            payload.mkdir()
            if variant.filesystem == "ntfs" and variant.compression:
                enable_ntfs_compression(payload)
            writer = Writer(disk.mountpoint, args.image_dir, variant, args)
            writer.progress = Progress("  Writing reference workload", sum(p.stat().st_size for p in files), args.progress_interval)
            reference_root = payload / "pass-000000"
            started = time.monotonic()
            for source in files:
                writer.copy(source, reference_root / source.relative_to(args.data_dir), capacity=False)
            log("  Syncing reference workload")
            sync_filesystem(disk.mountpoint)
            writer.progress.finish()
            result["reference_write_seconds"] = time.monotonic() - started
            result["reference"] = snapshot(disk.mountpoint, disk.image, variant, runner, args.disk_size)
            result["reference"]["f2fs_released_bytes"] = writer.released_bytes
            result["reference_verified"] = False
            if not args.no_verify:
                verify_reference(args.data_dir, files, reference_root, hashes, args.progress_interval)
                result["reference_verified"] = True
            if args.mode == "full":
                log("  Shared dataset measured; continuing capacity fill")
                writer.progress = Progress("  Capacity fill", None, args.progress_interval, initial=writer.logical)
                started = time.monotonic()
                pass_number = 1
                reason = "logical limit"
                try:
                    while writer.logical < args.max_logical_bytes:
                        destination = payload / f"pass-{pass_number:06d}"
                        for source in files:
                            writer.copy(source, destination / source.relative_to(args.data_dir), capacity=True)
                        # Include committed metadata in the next pass's free-space estimate.
                        sync_filesystem(disk.mountpoint)
                        pass_number += 1
                except StopFill as exc:
                    reason = str(exc)
                    if reason in ("ENOSPC", "EDQUOT"):
                        reason = fill_tail(writer, source, payload, reason)
                result["capacity_write_seconds"] = time.monotonic() - started
                writer.progress.finish()
                result["stop_reason"] = reason
                result["final"] = snapshot(disk.mountpoint, disk.image, variant, runner, args.disk_size)
                result["final"]["f2fs_released_bytes"] = writer.released_bytes
                result["completed_passes"] = pass_number
            else:
                result["final"] = result["reference"]
                result["stop_reason"] = "sample only"
            result["status"] = "ok"
            for stage in ("empty", "reference", "final"):
                result["warnings"].extend(result[stage]["warnings"])
            if variant.filesystem == "btrfs" and variant.compression and not shutil.which("compsize"):
                result["warnings"].append("Install compsize for exact Btrfs compression extent statistics")
            log(f"  {human(result['final']['files']['logical_bytes'])} logical data; stopped: {result['stop_reason']}")
    except CleanupError as exc:
        # Do not continue to another large image while the previous one is mounted.
        result["error"] = str(exc)
        result["status"] = "error"
        result["cleanup_failed"] = True
    except KeyboardInterrupt:
        result["status"] = "interrupted"
        result["error"] = "Interrupted; disposable disk cleanup attempted"
    except (BenchError, OSError, ValueError) as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        log(f"  FAILED: {exc}")
    result["seconds"] = time.monotonic() - start
    result["commands"] = runner.commands
    return result


def pair_results(results: list[dict]) -> None:
    for row in results:
        if row["status"] != "ok" or not row["variant"]["compression"]:
            continue
        baseline = next((r for r in results if r["status"] == "ok" and
                         r["variant"]["filesystem"] == row["variant"]["filesystem"] and
                         not r["variant"]["compression"]), None)
        if not baseline:
            continue
        def consumed(r):
            return r["empty"]["space"]["free_bytes"] - r["reference"]["space"]["free_bytes"]
        saved = consumed(baseline) - consumed(row)
        row["paired_net_saved_bytes"] = saved
        row["paired_net_saved_percent"] = 100 * saved / consumed(baseline) if consumed(baseline) else None
        # Fair end-to-end comparison of the EXACT SAME reference workload.
        # This includes compression's effect on metadata and allocation rounding.
        baseline_data = baseline["reference"]["file_data_bytes"]
        compressed_data = row["reference"]["file_data_bytes"]
        if baseline_data is not None and compressed_data is not None:
            row["paired_file_allocation_saved_bytes"] = baseline_data - compressed_data


def escape(value) -> str:
    return html.escape(str(value), quote=True)


def byte_cell(value) -> str:
    title = f"{value:,} bytes" if value is not None else "Unavailable"
    return f'<td title="{escape(title)}" data-value="{value if value is not None else -1}">{human(value)}</td>'


def render_report(report: dict) -> str:
    rows, references, details = [], [], []
    for index, row in enumerate(report["results"]):
        label = escape(row["label"])
        if row["status"] == "ok":
            initial, ref, final = (row[stage] for stage in ("empty", "reference", "final"))
            fs = final["files"]
            capacity = fs["logical_bytes"] if row["stop_reason"] != "sample only" else None
            metadata = final.get("btrfs", {}).get("metadata_used_physical_bytes")
            direct = ref.get("compression", {}).get("saved_bytes")
            if row["variant"]["filesystem"] == "f2fs" and row["variant"]["compression"]:
                direct = ref.get("f2fs_released_bytes")
            if not row["variant"]["compression"]:
                direct = None
            rows.append(f'<tr><td><a href="#run-{index}">{label}</a></td><td class="ok">OK</td>' +
                        byte_cell(initial["space"]["available_bytes"]) + byte_cell(initial["space"]["reserved_bytes"]) +
                        byte_cell(capacity) + byte_cell(final["space"]["available_bytes"]) +
                        byte_cell(final["non_file_overhead_estimate_bytes"]) + byte_cell(metadata) +
                        f'<td>{fs["files"]:,}</td><td>{escape(row["stop_reason"])}</td></tr>')
            references.append(f'<tr><td>{label}</td>' + byte_cell(ref["files"]["logical_bytes"]) +
                              byte_cell(ref["file_data_bytes"]) + byte_cell(ref["non_file_overhead_estimate_bytes"]) +
                              byte_cell(direct) + byte_cell(row.get("paired_net_saved_bytes")) +
                              f'<td>{"BLAKE3 OK" if row["reference_verified"] else "Skipped"}</td>' +
                              f'<td>{ref["files"]["logical_bytes"] / max(row["reference_write_seconds"], 1e-9) / 2**20:.1f}</td></tr>')
        else:
            rows.append(f'<tr><td>{label}</td><td class="error">{escape(row["status"])}</td>'
                        f'<td colspan="8">{escape(row.get("error", "Not run"))}</td></tr>')
        raw = escape(json.dumps(row, indent=2))
        warnings = "".join(f"<li>{escape(w)}</li>" for w in dict.fromkeys(row.get("warnings", [])))
        details.append(f'<details id="run-{index}"><summary>{label} — {escape(row["status"])} '
                       f'({row.get("seconds", 0):.1f} s)</summary><ul>{warnings}</ul><pre>{raw}</pre></details>')
    config = report["config"]
    empty = '<tr><td colspan="10">No filesystem runs yet.</td></tr>'
    environment = escape(json.dumps(report["environment"], indent=2))
    configuration = escape(json.dumps(config, indent=2))
    return f'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Linux filesystem space benchmark</title>
<style>
:root{{color-scheme:light dark;font-family:system-ui,sans-serif;line-height:1.55}}
body{{max-width:1500px;margin:40px auto;padding:0 24px}} h1{{margin-bottom:4px}} h2{{margin-top:36px}}
.muted{{opacity:.75}} .scroll{{overflow:auto;border:1px solid #8885;border-radius:8px}}
table{{border-collapse:collapse;width:100%;font-size:14px}}th,td{{text-align:right;padding:12px;border-bottom:1px solid #8884;white-space:nowrap}}
th{{cursor:pointer;background:#8881}}th:first-child,td:first-child{{text-align:left}}
tr:hover{{background:#8881}}.ok{{color:#25934e}}.error{{color:#d15d43}}
details{{border:1px solid #8885;padding:12px;margin:12px 0;border-radius:8px}}summary{{cursor:pointer}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}}code{{background:#8882;padding:2px 4px}}
.cards{{display:flex;flex-wrap:wrap;gap:16px;margin:24px 0}}.card{{border:1px solid #8885;border-radius:8px;padding:16px;min-width:180px}}
.card strong{{display:block;font-size:24px}} li{{margin:8px 0}} a{{color:inherit}}
</style>
<h1>Linux filesystem space benchmark</h1>
<p class="muted">Generated {escape(report["generated_at"])} · {escape(report["state"])} · click column headings to sort</p>
<div class="cards"><div class="card">Virtual disk<strong>{human(config["disk_size"])}</strong>{config["disk_size"]:,} bytes</div>
<div class="card">Shared dataset<strong>{human(report.get("dataset_bytes"))}</strong>{report.get("dataset_files", 0):,} source files</div>
<div class="card">Mode<strong>{escape(config["mode"])}</strong>{len(report["results"])} runs recorded</div></div>
<h2>Usable capacity and overhead</h2>
<div class="scroll"><table><thead><tr><th>Filesystem / configuration</th><th>Status</th><th>Empty usable space</th>
<th>Empty reserved space</th><th>Logical data stored</th><th>Final available</th><th>Non-file overhead (est.)</th>
<th>Btrfs metadata used</th><th>Files stored</th><th>Stop reason</th></tr></thead><tbody>{''.join(rows) or empty}</tbody></table></div>
<h2>Identical dataset: allocation and compression</h2>
<div class="scroll"><table><thead><tr><th>Filesystem / configuration</th><th>Logical data</th><th>File data allocation</th>
<th>Non-file overhead (est.)</th><th>Extent / released savings</th><th>Net saved vs baseline</th><th>Read-back</th>
<th>Write MiB/s</th></tr></thead><tbody>{''.join(references) or '<tr><td colspan="8">No successful runs yet.</td></tr>'}</tbody></table></div>
<h2>How to read the measurements</h2>
<ul>
<li><b>Empty usable space</b> is <code>f_bavail × f_frsize</code> after format and sync. Reserved space is
<code>(f_bfree − f_bavail) × f_frsize</code>; EXT4's default 5% reserve is reported separately from metadata.</li>
<li><b>Logical data stored</b> is the actual sum of file lengths after the capacity fill. The writer respects
ordinary-user available space even when run as root. ENOSPC, inode exhaustion, metadata constraints and final file
size can stop a fill with free blocks remaining. This is workload-dependent capacity, not a universal maximum.
A logical-limit stop is a lower bound; sample mode does not measure capacity.</li>
<li><b>Non-file overhead estimate</b> is image size minus <code>f_bfree × f_frsize</code> minus reported file data allocation.
It includes journals, filesystem structures excluded from statvfs, directories, inline file data, allocator accounting
and other metadata. It excludes ordinary-user reserved free blocks. Filesystems account differently; this is not an
exact cross-filesystem metadata counter. Negative or unavailable estimates are omitted.</li>
<li><b>Btrfs metadata used</b> comes from <code>btrfs filesystem df --raw</code>, with DUP multiplied by two.
Allocated-but-unused metadata chunks, system blocks, profiles and full usage output are in run details.
This counter can include inline file content.</li>
<li><b>File data allocation</b> normally sums <code>st_blocks × 512</code> for regular files; allocation slack and
inline data affect it. Compressed Btrfs uses <code>compsize -b</code>, since <code>st_blocks</code> can misrepresent
compressed extent size. No reflinks, hardlinks, sparse copies or deduplication are used.</li>
<li><b>Extent / released savings</b> uses Btrfs's uncompressed extent size minus physical extent size, or F2FS's
compression blocks released by ioctl. Other unavailable counters display a dash.
F2FS files become non-writable after releasing their reserved compression blocks.</li>
<li><b>Net saved vs baseline</b> compares the reduction in free bytes for the exact same dataset in the uncompressed
and compressed runs. It includes changes in metadata and rounding, so can be negative. NTFS compression uses
NTFS-3G with LZNT1 and a marked parent directory. Capacity-stage compression values are not compared across
different amounts of logical data.</li>
<li><b>Raw image host allocation</b> in details is the backing file's <code>st_blocks</code>, which depends on the host
filesystem (including its compression or CoW). It is not the guest filesystem's overhead.
Write rates include per-file fsync and F2FS release ioctls; this is a space benchmark, not a device speed benchmark.</li>
</ul>
<h2>Run details and command output</h2>{''.join(details)}
<details><summary>Configuration</summary><pre>{configuration}</pre></details>
<details><summary>Environment and tool versions</summary><pre>{environment}</pre></details>
<p class="muted">Sources: <a href="https://btrfs.readthedocs.io/en/latest/btrfs-filesystem.html">Btrfs accounting</a>,
<a href="https://github.com/kilobyte/compsize">compsize</a>,
<a href="https://www.kernel.org/doc/html/latest/filesystems/f2fs.html">F2FS compression</a>,
<a href="https://github.com/tuxera/ntfs-3g/wiki/Using-Transparently-Compressed-Files">NTFS-3G compression</a>.</p>
<script>
document.querySelectorAll('th').forEach(th => th.addEventListener('click', () => {{
 const table=th.closest('table'), body=table.tBodies[0], col=th.cellIndex;
 const asc=th.dataset.sort!=='asc'; th.dataset.sort=asc?'asc':'desc';
 const rows=Array.from(body.rows);
 rows.sort((a,b)=>{{const x=a.cells[col],y=b.cells[col];if(!x||!y)return 0;
  const xv=x.dataset.value,yv=y.dataset.value;
  const d=xv!==undefined&&yv!==undefined ? Number(xv)-Number(yv) : x.textContent.localeCompare(y.textContent,undefined,{{numeric:true}});
  return asc?d:-d;}}); rows.forEach(r=>body.appendChild(r));
}}));
</script></html>'''


def write_report(report: dict, output: Path) -> None:
    pair_results(report["results"])
    report["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    atomic_write(output.with_suffix(".json"), json.dumps(report, indent=2))
    atomic_write(output, render_report(report))
    # Make sudo-created artifacts editable by the invoking user.
    if os.geteuid() == 0 and os.environ.get("SUDO_UID"):
        uid, gid = int(os.environ["SUDO_UID"]), int(os.environ.get("SUDO_GID", "0"))
        for path in (output, output.with_suffix(".json")):
            os.chown(path, uid, gid)


def build_variants(args: argparse.Namespace) -> list[Variant]:
    variants = []
    for fs in args.filesystems:
        variants.append(Variant(fs, False, args.ntfs_driver if fs == "ntfs" else fs))
        if args.compression == "on" and fs in ("btrfs", "f2fs", "ntfs"):
            variants.append(Variant(fs, True, "ntfs-3g" if fs == "ntfs" else fs))
    return variants


def requirements(variant: Variant) -> list[str]:
    required = [f"mkfs.{variant.filesystem}"]
    if variant.filesystem == "ntfs" and variant.driver == "ntfs-3g":
        required.append("ntfs-3g")
    return required


def environment_info(variants: list[Variant], runner: Runner) -> dict:
    tools_info = {}
    commands = {"losetup": ["--version"], "mount": ["--version"], "mkfs.ext4": ["-V"],
                "mkfs.xfs": ["-V"], "mkfs.btrfs": ["--version"], "mkfs.f2fs": ["-V"],
                "mkfs.ntfs": ["--version"], "mkfs.exfat": ["-V"], "ntfs-3g": ["--version"],
                "btrfs": ["--version"]}
    for name, flags in commands.items():
        path = shutil.which(name)
        if path:
            result = runner.run([name, *flags], check=False, timeout=10)
            tools_info[name] = {"path": path, "version": (result.stdout + result.stderr).strip()[:4096]}
    return {"platform": platform.platform(), "kernel": platform.release(), "python": platform.python_version(),
            "tools": tools_info, "optional_compsize": shutil.which("compsize"),
            "filesystems": Path("/proc/filesystems").read_text() if Path("/proc/filesystems").exists() else "",
            "ext4_host_defaults": Path("/etc/mke2fs.conf").read_text() if Path("/etc/mke2fs.conf").exists() else ""}


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--image-dir", "--raw-dir", type=Path, default=BASE / ".images", help="Directory for temporary .raw disks")
    parser.add_argument("--disk-size", type=parse_size, default=parse_size("100GB"), help="Virtual disk size (GB is decimal; GiB is binary)")
    parser.add_argument("--filesystems", nargs="+", default=list(FILESYSTEMS), help="Filesystem names, separated by spaces or commas")
    parser.add_argument("--compression", choices=("on", "off"), default="on", help="Add compressed Btrfs, F2FS and NTFS variants")
    parser.add_argument("--ntfs-driver", choices=("ntfs-3g", "ntfs3"), default="ntfs-3g", help="Driver for baseline NTFS (compression always uses NTFS-3G)")
    parser.add_argument("--mode", choices=("full", "sample"), default="full", help="Fill to capacity or only copy the shared dataset once")
    parser.add_argument("--data-dir", type=Path, help="Existing regular-file workload; copied read-only, repeated in full mode")
    parser.add_argument("--demo-dir", type=Path, default=BASE / "demo_data", help="Location for generated and retained demo data")
    parser.add_argument("--dataset-size", type=parse_size, default=parse_size("256MiB"), help="Generated demo data size")
    parser.add_argument("--small-files", type=int, default=2000, help="Small demo files for metadata and allocation-slack comparisons")
    parser.add_argument("--seed", type=int, default=2026, help="Reproducible demo data seed")
    parser.add_argument("--output", type=Path, default=BASE / "report.html", help="HTML report path; also writes matching .json")
    parser.add_argument("--ext4-reserved-percent", type=float, default=5.0, help="EXT4 root reserve percentage")
    parser.add_argument("--leave-free", type=parse_size, default=0, help="Stop full fill at this available-space threshold")
    parser.add_argument("--max-logical-bytes", type=parse_size, help="Full-fill logical data limit; default is four times disk size")
    parser.add_argument("--host-reserve", type=parse_size, default=parse_size("2GiB"), help="Minimum free space to retain on the image host filesystem")
    parser.add_argument("--no-verify", action="store_true", help="Disable all hashing and checksum verification; BLAKE3 is not required")
    parser.add_argument("--progress-interval", type=float, default=5.0, help="Seconds between byte progress updates and command heartbeats")
    parser.add_argument("--dry-run", action="store_true", help="Print commands and dependencies, without creating files or requiring root")
    parser.add_argument("--generate-only", action="store_true", help="Generate/reuse demo data without creating disks; does not need root")
    args = parser.parse_args(argv)
    names = [name.lower() for value in args.filesystems for name in value.split(",")]
    if any(name not in FILESYSTEMS for name in names):
        parser.error(f"Valid filesystems: {', '.join(FILESYSTEMS)}")
    args.filesystems = list(dict.fromkeys(names))
    if args.disk_size < parse_size("512MiB") or args.disk_size % 512:
        parser.error("--disk-size must be at least 512MiB and a multiple of 512 bytes")
    if args.small_files < 0:
        parser.error("--small-files cannot be negative")
    if not math.isfinite(args.ext4_reserved_percent) or not 0 <= args.ext4_reserved_percent <= 50:
        parser.error("--ext4-reserved-percent must be between 0 and 50")
    if not math.isfinite(args.progress_interval) or args.progress_interval <= 0:
        parser.error("--progress-interval must be a positive finite number")
    if args.output.suffix.lower() != ".html":
        parser.error("--output must end in .html")
    for name in ("image_dir", "output", "demo_dir"):
        setattr(args, name, getattr(args, name).expanduser().resolve())
    args.custom_data = args.data_dir is not None
    args.data_dir = args.data_dir.expanduser().resolve() if args.data_dir else args.demo_dir
    args.max_logical_bytes = args.max_logical_bytes or args.disk_size * 4
    if args.leave_free >= args.disk_size:
        parser.error("--leave-free must be smaller than --disk-size")
    if args.output.is_relative_to(args.data_dir) or args.output.with_suffix(".json").is_relative_to(args.data_dir):
        parser.error("Report output must be outside the data directory")
    if args.image_dir == args.data_dir or args.image_dir.is_relative_to(args.data_dir):
        parser.error("Image directory must be outside the data directory")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    variants = build_variants(args)
    if args.dry_run:
        log(f"Disk: {args.disk_size:,} bytes ({human(args.disk_size)}); image directory: {args.image_dir}")
        log(f"Mode: {args.mode}; report: {args.output}; root is required for real disk runs")
        for variant in variants:
            missing = [name for name in requirements(variant) if not shutil.which(name)]
            log(f"\n{variant.label} [{variant.driver}]; missing tools: {', '.join(missing) or 'none'}")
            log("  " + shlex.join(format_command(variant, "/dev/loopN", args.ext4_reserved_percent, Runner())))
            log("  " + shlex.join(variant.mount_command("/dev/loopN", Path("/temporary/mount"))))
        log("\nLoop devices are verified against freshly created backing files before formatting.")
        return 0
    try:
        log(f"Filesystem space benchmark starting; workload: {args.data_dir}")
        if not args.no_verify:
            new_hasher()  # Fail before any disk creation if verification needs the dependency.
        else:
            log("Verification disabled; skipping all hashing (BLAKE3 is not required)")
        if args.generate_only:
            if args.custom_data:
                raise BenchError("--generate-only cannot be combined with --data-dir")
            generate_data(args.demo_dir, args.dataset_size, args.small_files, args.seed, args.progress_interval,
                          verify=not args.no_verify)
            return 0
        if platform.system() != "Linux":
            raise BenchError("Disk benchmarking requires Linux")
        if os.geteuid() != 0:
            raise BenchError("Loop setup, format and mount require root. Run with sudo; use --dry-run without root.")
        for name in ("losetup", "mount", "umount"):
            if not shutil.which(name):
                raise BenchError(f"Required tool missing: {name}")
        args.image_dir.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(args.image_dir).free < args.disk_size + args.host_reserve:
            raise BenchError(f"Image host needs at least {human(args.disk_size + args.host_reserve)} free "
                             "for a full-size image and host reserve")
        log("Preparing workload and checking file list")
        hashes = None
        if not args.custom_data:
            hashes = generate_data(args.demo_dir, args.dataset_size, args.small_files, args.seed, args.progress_interval,
                                   verify=not args.no_verify)
        files = discover_data(args.data_dir)
        dataset_bytes = sum(p.stat().st_size for p in files)
        if dataset_bytes >= args.disk_size - args.leave_free or dataset_bytes > args.max_logical_bytes:
            raise BenchError("Reference dataset must fit within the disk and logical-data limit; use smaller data")
        if args.no_verify:
            hashes = {}
        elif hashes is None:
            hashes = hash_files(args.data_dir, files, "Hashing workload (BLAKE3)", args.progress_interval)
        else:
            log("Reusing workload hashes from validation; no second hash pass")
        config = {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()}
        report = {"state": "running", "config": config, "dataset_bytes": dataset_bytes, "dataset_files": len(files),
                  "hash_algorithm": None if args.no_verify else "blake3", "dataset_blake3": hashes,
                  "environment": environment_info(variants, Runner()), "results": []}
        write_report(report, args.output)
        interrupted = False
        old_handler = signal.signal(signal.SIGTERM, lambda signum, frame: (_ for _ in ()).throw(KeyboardInterrupt()))
        try:
            for variant in variants:
                log(f"\nBenchmarking {variant.label} [{variant.driver}]")
                missing = [name for name in requirements(variant) if not shutil.which(name)]
                if missing:
                    row = {"label": variant.label, "variant": asdict(variant), "status": "skipped",
                           "error": f"Missing tools: {', '.join(missing)}"}
                    log(f"  SKIPPED: {row['error']}")
                else:
                    row = run_variant(variant, args, files, hashes)
                report["results"].append(row)
                write_report(report, args.output)
                if row.get("cleanup_failed") or row["status"] == "interrupted":
                    interrupted = True
                    break
        except KeyboardInterrupt:
            interrupted = True
        finally:
            signal.signal(signal.SIGTERM, old_handler)
            report["state"] = "interrupted" if interrupted else "complete"
            write_report(report, args.output)
        log(f"\nHTML report: {args.output}\nJSON data:   {args.output.with_suffix('.json')}")
        return 130 if interrupted else (1 if any(r["status"] != "ok" for r in report["results"]) else 0)
    except (BenchError, OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
