import { cp, mkdir, readFile, readdir, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { renderReadme } from './render-readme.mjs';

export const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
export const dataRoot = path.join(projectRoot, 'DATA');
const generatedMarker = '<meta name="generator" content="benchresults-directory-index">';
const repository = 'https://github.com/panmourovaty/benchresults';
const collator = new Intl.Collator('en', { numeric: true, sensitivity: 'base' });

function isPublic(entry, relativeDir) {
  if (entry.name.startsWith('.') || entry.isSymbolicLink()) return false;
  if (['node_modules', 'dist'].includes(entry.name)) return false;
  if (entry.name === 'AGENTS.md') return false;
  if (!relativeDir && entry.name.startsWith('_')) return false;
  return entry.isFile() || entry.isDirectory();
}

export function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]);
}

export function publicUrl(relativePath, directory = false) {
  const encoded = relativePath.split('/').map(segment => encodeURIComponent(segment)
    .replace(/[!'()*]/g, char => `%${char.charCodeAt(0).toString(16).toUpperCase()}`)).join('/');
  return `/${encoded}${directory && encoded ? '/' : ''}`;
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
  return `${new Intl.NumberFormat('en', { maximumFractionDigits: 1 }).format(value)} ${units[unit]}`;
}

function fileType(name) {
  const extension = path.extname(name).slice(1).toLowerCase();
  if (['html', 'htm'].includes(extension)) return ['fa-solid fa-file-code', 'html', 'HTML report'];
  if (extension === 'json') return ['fa-solid fa-code', 'data', 'JSON data'];
  if (extension === 'py') return ['fa-brands fa-python', 'script', 'Python script'];
  if (['js', 'mjs', 'ts', 'css', 'sh', 'c', 'cpp', 'rs', 'go'].includes(extension)) return ['fa-solid fa-file-code', 'script', `${extension.toUpperCase()} source`];
  if (['csv', 'xls', 'xlsx', 'ods'].includes(extension)) return ['fa-solid fa-file-csv', 'data', 'Spreadsheet'];
  if (extension === 'pdf') return ['fa-solid fa-file-pdf', 'document', 'PDF document'];
  if (['png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'avif', 'bmp', 'ico'].includes(extension)) return ['fa-solid fa-file-image', 'image', 'Image'];
  if (['zip', 'gz', 'xz', 'bz2', '7z', 'rar', 'tar', 'zst'].includes(extension)) return ['fa-solid fa-file-zipper', 'archive', 'Archive'];
  if (['mp4', 'webm', 'mkv', 'mov'].includes(extension)) return ['fa-solid fa-file-video', 'image', 'Video'];
  if (['mp3', 'wav', 'flac', 'ogg', 'm4a'].includes(extension)) return ['fa-solid fa-file-audio', 'image', 'Audio'];
  if (['txt', 'md', 'log', 'doc', 'docx', 'odt'].includes(extension)) return ['fa-solid fa-file-lines', 'document', 'Document'];
  return ['fa-solid fa-file', 'file', extension ? `${extension.toUpperCase()} file` : 'File'];
}

async function isGenerated(file) {
  try {
    const html = await readFile(file, 'utf8');
    // Also recognize the marker after HTML minification removes attribute quotes.
    return /<meta\s+name=["']?generator["']?\s+content=["']?benchresults-directory-index["']?\s*\/?\s*>/i.test(html);
  } catch (error) {
    if (error.code === 'ENOENT') return false;
    throw error;
  }
}

// mouseenter fires once for the whole link, avoiding repeated mouseover events
// when crossing its icon and label. Focus/touch give other inputs the same head start.
const folderNavigation = 'hx-boost="true" hx-target="#directory-browser" hx-select="#directory-browser" hx-swap="outerHTML show:window:top" hx-push-url="true" hx-preload="mouseenter, focus, touchstart" hx-indicator="#navigation-progress"';

function directoryLink(relativePath, label, boosted, classes = '') {
  return `<a class="${classes}" href="${escapeHtml(publicUrl(relativePath, true))}" ${boosted ? folderNavigation : ''}>${label}</a>`;
}

function breadcrumbs(relativeDir, directories) {
  const parts = relativeDir ? relativeDir.split('/') : [];
  const items = [];
  if (!parts.length) {
    items.push('<li class="breadcrumb-item active" aria-current="page"><i class="fa-solid fa-house" aria-hidden="true"></i> Archive</li>');
  } else {
    items.push(`<li class="breadcrumb-item">${directoryLink('', '<i class="fa-solid fa-house" aria-hidden="true"></i> Archive', directories.get('').generated)}</li>`);
    parts.forEach((part, index) => {
      const relativePath = parts.slice(0, index + 1).join('/');
      items.push(index === parts.length - 1
        ? `<li class="breadcrumb-item active" aria-current="page">${escapeHtml(part)}</li>`
        : `<li class="breadcrumb-item">${directoryLink(relativePath, escapeHtml(part), directories.get(relativePath).generated)}</li>`);
    });
  }
  return `<nav aria-label="Breadcrumb"><ol class="breadcrumb">${items.join('')}</ol></nav>`;
}

function renderEntry(entry, directories) {
  const directory = entry.directory;
  const [icon, kind, type] = directory ? ['fa-solid fa-folder', 'folder', 'Directory'] : fileType(entry.name);
  const child = directory ? directories.get(entry.relativePath) : null;
  const childCount = child ? child.entries.length : 0;
  const detail = directory ? `${childCount} ${childCount === 1 ? 'item' : 'items'}` : type;
  const label = `<span class="file-icon icon-${kind}"><i class="${icon}" aria-hidden="true"></i></span><span class="entry-text"><span class="entry-name">${escapeHtml(entry.name)}${directory ? '/' : ''}</span><span class="entry-detail">${detail}</span></span><i class="fa-solid ${directory ? 'fa-chevron-right' : 'fa-arrow-up-right-from-square'} entry-arrow" aria-hidden="true"></i>`;
  const link = directory
    ? directoryLink(entry.relativePath, label, child.generated, 'entry-link')
    : `<a class="entry-link" href="${escapeHtml(publicUrl(entry.relativePath))}"${kind === 'html' ? ' data-prefetch-document' : ''}>${label}</a>`;
  const modified = entry.modified.toISOString();
  return `<tr data-entry data-name="${escapeHtml(entry.name)}" data-kind="${kind}" data-size="${entry.size}" data-modified="${entry.modified.getTime()}">
    <td>${link}</td>
    <td class="type-column"><span class="type-label type-${kind}">${type}</span></td>
    <td class="size-column">${directory ? '&mdash;' : formatBytes(entry.size)}</td>
    <td class="modified-column"><time datetime="${modified}" title="${modified}">${modified.slice(0, 10)}</time></td>
  </tr>`;
}

function documentStart(title, description) {
  return `<!DOCTYPE html>
<html lang="en" data-bs-theme="light">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  ${generatedMarker}
  <meta name="description" content="${escapeHtml(description)}">
  <meta name="color-scheme" content="light">
  <meta name="htmx-config" content='{"noSwap":[204,304,"4xx","5xx"],"preload":{"autoBoost":false}}'>
  <title>${escapeHtml(title)} · Pan Mourovaty's benchmarks</title>
  <link rel="preconnect" href="https://cdn.jsdelivr.net" crossorigin>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@6.0.0-alpha.1/dist/css/bootstrap.min.css">
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@fortawesome/fontawesome-free@7.3.1/css/all.min.css">
  <link rel="stylesheet" href="/_assets/browser.css">
  <script defer src="https://cdn.jsdelivr.net/npm/htmx.org@4.0.0/dist/htmx.min.js"></script>
  <script defer src="https://cdn.jsdelivr.net/npm/htmx.org@4.0.0/dist/ext/hx-preload.js"></script>
  <script defer src="/_assets/browser.js"></script>
</head>
<body>
  <a class="skip-link" href="#directory-browser">Skip to files</a>
  <div id="navigation-progress" class="navigation-progress" aria-hidden="true"></div>
  <header class="site-header">
    <div class="container archive-container header-inner">
      <a class="brand" href="/"><span class="brand-mark"><i class="fa-solid fa-chart-simple" aria-hidden="true"></i></span><span>Pan Mourovaty<span class="brand-subtitle">BENCHMARK ARCHIVE</span></span></a>
      <a class="source-link" href="${repository}"><i class="fa-brands fa-github" aria-hidden="true"></i><span>Source on GitHub</span><i class="fa-solid fa-arrow-up-right-from-square" aria-hidden="true"></i></a>
    </div>
  </header>`;
}

function documentEnd() {
  return `<footer class="site-footer container archive-container"><span>Pan Mourovaty's benchmarks</span><span>Hardware, games &amp; performance.</span></footer>
</body>
</html>\n`;
}

function renderDirectory(relativeDir, directories) {
  const { entries, readme } = directories.get(relativeDir);
  const folders = entries.filter(entry => entry.directory).length;
  const files = entries.length - folders;
  const bytes = entries.reduce((total, entry) => total + (entry.directory ? 0 : entry.size), 0);
  const title = relativeDir ? path.posix.basename(relativeDir) : 'Benchmark results';
  const parent = relativeDir.includes('/') ? relativeDir.slice(0, relativeDir.lastIndexOf('/')) : '';
  const parentRow = relativeDir ? `<tr class="parent-row"><td colspan="4">${directoryLink(parent, '<span class="parent-icon"><i class="fa-solid fa-arrow-turn-up" aria-hidden="true"></i></span><span>Parent directory</span><span class="parent-path">../</span>', directories.get(parent).generated, 'parent-link')}</td></tr>` : '';
  const stats = `<span><i class="fa-solid fa-folder" aria-hidden="true"></i> ${folders} ${folders === 1 ? 'folder' : 'folders'}</span><span><i class="fa-solid fa-file-lines" aria-hidden="true"></i> ${files} ${files === 1 ? 'file' : 'files'}</span><span>${formatBytes(bytes)} <span class="stat-label">in files</span></span>`;
  return `${documentStart(title, `Browse ${relativeDir ? `/${relativeDir}/` : 'the archive'} in Pan Mourovaty's benchmark collection.`)}
  <main id="directory-browser" class="container archive-container" tabindex="-1">
    ${breadcrumbs(relativeDir, directories)}
    <section class="directory-heading" aria-labelledby="directory-title">
      <p class="eyebrow">${relativeDir ? 'Inside the archive' : 'Measurements & reports'}</p>
      <h1 id="directory-title">${escapeHtml(title)}</h1>
      <p class="directory-description">${relativeDir ? 'Reports, data and source files in this folder.' : 'A collection of real-world hardware and gaming benchmarks. Open a report, or explore a folder.'}</p>
      <div class="directory-stats">${stats}</div>
    </section>
    <section class="listing-panel" aria-label="Directory contents">
      <div class="listing-toolbar">
        <div class="location-label"><i class="fa-solid fa-folder-open" aria-hidden="true"></i><span>${escapeHtml(publicUrl(relativeDir, true))}</span></div>
        <div class="search-field" data-enhanced inert aria-hidden="true"><i class="fa-solid fa-magnifying-glass" aria-hidden="true"></i><label class="visually-hidden" for="file-search">Filter files in this folder</label><input class="form-control" type="search" id="file-search" placeholder="Filter files…" autocomplete="off" spellcheck="false" aria-controls="file-list"></div>
      </div>
      <div class="table-responsive">
        <table class="table align-middle file-table">
          <caption class="visually-hidden">Files and folders in ${escapeHtml(publicUrl(relativeDir, true))}. Modification dates are in UTC.</caption>
          <thead><tr>
            <th scope="col" aria-sort="ascending"><button type="button" class="sort-button" data-sort="name" disabled>Name <i class="fa-solid fa-arrow-up" aria-hidden="true"></i></button></th>
            <th scope="col" class="type-column">Type</th>
            <th scope="col" class="size-column" aria-sort="none"><button type="button" class="sort-button" data-sort="size" disabled>Size <i class="fa-solid fa-sort" aria-hidden="true"></i></button></th>
            <th scope="col" class="modified-column" aria-sort="none"><button type="button" class="sort-button" data-sort="modified" disabled>Modified <i class="fa-solid fa-sort" aria-hidden="true"></i></button></th>
          </tr></thead>
          <tbody id="file-list">${parentRow}${entries.map(entry => renderEntry(entry, directories)).join('\n')}
            <tr data-empty${entries.length ? ' hidden' : ''}><td colspan="4" class="empty-state"><i class="fa-solid fa-folder-open" aria-hidden="true"></i><p>${entries.length ? 'No files match your filter.' : 'This folder is empty.'}</p><button type="button" class="btn btn-sm btn-outline-secondary" data-clear-filter hidden>Clear filter</button></td></tr>
          </tbody>
        </table>
      </div>
      <div class="listing-footer"><span data-result-count role="status" aria-live="polite">${entries.length} ${entries.length === 1 ? 'item' : 'items'}</span><span>Modified dates in UTC</span></div>
    </section>
    ${readme ? `<section class="readme-panel" aria-label="Folder README"><header class="readme-header"><span><i class="fa-brands fa-markdown" aria-hidden="true"></i> ${escapeHtml(readme.name)}</span><a href="${escapeHtml(publicUrl(readme.relativePath))}">View source <i class="fa-solid fa-arrow-up-right-from-square" aria-hidden="true"></i></a></header><article class="readme-body">${readme.html}</article></section>` : ''}
  </main>
  ${documentEnd()}`;
}

export async function generateDirectoryIndexes({ sourceDir = dataRoot, outputDir = sourceDir } = {}) {
  sourceDir = path.resolve(sourceDir);
  outputDir = path.resolve(outputDir);
  const directories = new Map();
  await mkdir(outputDir, { recursive: true });

  async function collect(relativeDir) {
    const sourcePath = path.join(sourceDir, relativeDir);
    const outputPath = path.join(outputDir, relativeDir);
    await mkdir(outputPath, { recursive: true });
    const children = await readdir(sourcePath, { withFileTypes: true });
    const entries = [];
    let authoredIndex = false;
    let readme = null;
    for (const child of children) {
      if (!isPublic(child, relativeDir)) continue;
      const relativePath = relativeDir ? `${relativeDir}/${child.name}` : child.name;
      const sourceFile = path.join(sourceDir, relativePath);
      const outputFile = path.join(outputDir, relativePath);
      if (child.isDirectory() && sourceDir !== outputDir &&
          (sourceFile === outputDir || outputDir.startsWith(`${sourceFile}${path.sep}`))) continue;
      if (child.name === 'index.html') {
        authoredIndex = !await isGenerated(sourceFile);
        if (authoredIndex && sourceDir !== outputDir) await cp(sourceFile, outputFile, { preserveTimestamps: true });
        continue;
      }
      if (!relativeDir && child.name === '404.html') continue;
      const info = await stat(sourceFile);
      if (child.isFile() && child.name.toLowerCase() === 'readme.md' && (!readme || child.name === 'README.md')) {
        readme = { name: child.name, relativePath, html: renderReadme(await readFile(sourceFile, 'utf8'), publicUrl(relativeDir, true)) };
      }
      entries.push({ name: child.name, relativePath, directory: child.isDirectory(), size: info.size, modified: info.mtime });
      if (child.isDirectory()) await collect(relativePath);
      else if (sourceDir !== outputDir) await cp(sourceFile, outputFile, { preserveTimestamps: true });
    }
    entries.sort((left, right) => Number(right.directory) - Number(left.directory) || collator.compare(left.name, right.name));
    directories.set(relativeDir, { entries, readme, generated: !authoredIndex });
  }

  await collect('');
  if (sourceDir !== outputDir) {
    await cp(path.join(sourceDir, '_assets'), path.join(outputDir, '_assets'), { recursive: true });
    for (const name of ['_headers', '_redirects']) {
      try { await cp(path.join(sourceDir, name), path.join(outputDir, name)); }
      catch (error) { if (error.code !== 'ENOENT') throw error; }
    }
  }
  for (const [relativeDir, directory] of directories) {
    if (directory.generated) await writeFile(path.join(outputDir, relativeDir, 'index.html'), renderDirectory(relativeDir, directories));
  }
  const source404 = path.join(sourceDir, '404.html');
  let authored404;
  try { await stat(source404); authored404 = !await isGenerated(source404); }
  catch (error) { if (error.code !== 'ENOENT') throw error; authored404 = false; }
  if (authored404) {
    if (sourceDir !== outputDir) await cp(source404, path.join(outputDir, '404.html'), { preserveTimestamps: true });
  } else {
    await writeFile(path.join(outputDir, '404.html'), `${documentStart('Page not found', 'This file or folder could not be found in the benchmark archive.')}<main id="directory-browser" class="container archive-container not-found"><p class="eyebrow">404 · Page not found</p><h1>This path is missing.</h1><p>The file or folder may have moved. Browse the archive to find the report you need.</p><a class="btn btn-primary" href="/"><i class="fa-solid fa-arrow-left" aria-hidden="true"></i> Back to the archive</a></main>${documentEnd()}`);
  }
  return { directories, outputDir };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (process.argv.length > 3) throw new Error('Usage: bash generate_index.sh [output-directory]');
  const outputDir = process.argv[2] ? path.resolve(process.argv[2]) : path.join(projectRoot, 'dist');
  const result = await generateDirectoryIndexes({ outputDir });
  console.log(`Generated listings for ${result.directories.size} directories in ${outputDir}`);
}
