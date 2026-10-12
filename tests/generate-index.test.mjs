import assert from 'node:assert/strict';
import { mkdtemp, mkdir, readFile, readdir, rm, symlink, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { minify } from 'html-minifier-terser';
import { createHash } from 'node:crypto';
import { gzipSync } from 'node:zlib';
import { dataRoot, generateDirectoryIndexes } from '../scripts/generate-index.mjs';

async function fixture(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'benchresults-test-'));
  const sourceDir = path.join(root, 'DATA');
  const outputDir = path.join(root, 'output');
  await mkdir(path.join(sourceDir, '_assets'), { recursive: true });
  await writeFile(path.join(sourceDir, '_assets', 'browser.css'), 'body { color: blue; }');
  t.after(() => rm(root, { recursive: true, force: true }));
  const put = async (name, content = 'content') => {
    const file = path.join(sourceDir, name);
    await mkdir(path.dirname(file), { recursive: true });
    await writeFile(file, content);
  };
  return { sourceDir, outputDir, put };
}

test('creates independent nested listings with canonical links, breadcrumbs and folders first', async t => {
  const { sourceDir, outputDir, put } = await fixture(t);
  await put('report.html', '<!DOCTYPE html><title>Report</title><p>Original report</p>');
  await put('fsoverhead/report.json', '{"result":42}');
  await put('fsoverhead/nested/fsbench.py', 'print(42)');
  await mkdir(path.join(sourceDir, 'empty'));
  await generateDirectoryIndexes({ sourceDir, outputDir });
  const root = await readFile(path.join(outputDir, 'index.html'), 'utf8');
  const folder = await readFile(path.join(outputDir, 'fsoverhead', 'index.html'), 'utf8');
  const nested = await readFile(path.join(outputDir, 'fsoverhead', 'nested', 'index.html'), 'utf8');
  assert.ok(root.indexOf('data-name="fsoverhead"') < root.indexOf('data-name="report.html"'));
  assert.match(root, /href="\/fsoverhead\/" hx-boost="true"/);
  assert.match(root, /hx-preload="mouseenter, focus, touchstart"/);
  assert.doesNotMatch(root, /data-name="report.json"/);
  assert.match(folder, /href="\/fsoverhead\/report.json"/);
  assert.match(folder, /href="\/" hx-boost="true"/);
  assert.match(nested, /href="\/fsoverhead\/" hx-boost="true"/);
  assert.match(nested, /href="\/fsoverhead\/nested\/fsbench.py"/);
  assert.match(nested, /fa-brands fa-python/);
  assert.match(await readFile(path.join(outputDir, 'empty/index.html'), 'utf8'), /This folder is empty/);
  assert.equal(await readFile(path.join(outputDir, 'report.html'), 'utf8'), await readFile(path.join(sourceDir, 'report.html'), 'utf8'));
  assert.match(root, /href="\/report" data-prefetch-document/);
  assert.match(await readFile(path.join(outputDir, '404.html'), 'utf8'), /Page not found/);
});

test('encodes filenames as URL path segments and escapes names and attributes', async t => {
  const { sourceDir, outputDir, put } = await fixture(t);
  const directory = 'měření & tests';
  const filename = 'RTX3080+i9 #1% "quoted" <tag>.html';
  await put(`${directory}/${filename}`, '<p>Report</p>');
  await generateDirectoryIndexes({ sourceDir, outputDir });
  const folder = await readFile(path.join(outputDir, directory, 'index.html'), 'utf8');
  assert.match(folder, /href="\/m%C4%9B%C5%99en%C3%AD%20%26%20tests\/RTX3080%2Bi9%20%231%25%20%22quoted%22%20%3Ctag%3E"/);
  assert.match(folder, /RTX3080\+i9 #1% &quot;quoted&quot; &lt;tag&gt;.html/);
  assert.doesNotMatch(folder, /<tag>/);
});

test('publishes only DATA, excludes hidden entries, symlinks and output, and copies Pages config', async t => {
  const { sourceDir, put } = await fixture(t);
  for (const name of ['.git/config', '.env', 'node_modules/secret', 'AGENTS.md', 'dist/old.html']) await put(name);
  // Repository tools sit outside DATA and must never enter the deployed tree.
  for (const name of ['scripts/build.mjs', 'tests/private.html', 'README.md', 'package.json', 'generate_index.sh']) {
    const file = path.join(path.dirname(sourceDir), name);
    await mkdir(path.dirname(file), { recursive: true });
    await writeFile(file, 'repository tooling');
  }
  await put('_headers', '/*\n  X-Content-Type-Options: nosniff');
  await put('_redirects', '/old /new 301');
  await put('report.json', '{}');
  await symlink(path.join(sourceDir, '.env'), path.join(sourceDir, 'linked-secret'));
  const outputDir = path.join(sourceDir, 'preview/site');
  await generateDirectoryIndexes({ sourceDir, outputDir });
  assert.deepEqual((await readdir(outputDir)).sort(), ['404.html', '_assets', '_headers', '_redirects', 'index.html', 'report.json']);
  assert.equal(await readFile(path.join(outputDir, '_redirects'), 'utf8'), '/old /new 301');
  const root = await readFile(path.join(outputDir, 'index.html'), 'utf8');
  assert.doesNotMatch(root, /data-name="(?:_assets|_headers|_redirects|preview|linked-secret)"/);
});

test('preserves authored indexes and 404s, and keeps links to authored pages as native navigation', async t => {
  const { sourceDir, outputDir, put } = await fixture(t);
  const authored = '<!DOCTYPE html><title>Custom homepage</title><script>window.custom = true</script>';
  await put('custom/index.html', authored);
  await put('custom/report.html', '<p>Report</p>');
  await put('404.html', '<h1>Custom 404</h1>');
  await generateDirectoryIndexes({ sourceDir, outputDir });
  assert.equal(await readFile(path.join(outputDir, 'custom/index.html'), 'utf8'), authored);
  assert.equal(await readFile(path.join(outputDir, '404.html'), 'utf8'), '<h1>Custom 404</h1>');
  const root = await readFile(path.join(outputDir, 'index.html'), 'utf8');
  assert.match(root, /href="\/custom\/" >/, 'authored directory is not HTMX boosted');
  await generateDirectoryIndexes({ sourceDir });
  assert.equal(await readFile(path.join(sourceDir, 'custom/index.html'), 'utf8'), authored);
});

test('regenerates in-place indexes without listing generated files, even after minification', async t => {
  const { sourceDir, put } = await fixture(t);
  await put('folder/report.html', '<p>Report</p>');
  await generateDirectoryIndexes({ sourceDir });
  const first = await readFile(path.join(sourceDir, 'index.html'), 'utf8');
  await writeFile(path.join(sourceDir, 'index.html'), await minify(first, { removeAttributeQuotes: true, collapseWhitespace: true, removeComments: true }));
  await put('folder/new.json', '{}');
  await generateDirectoryIndexes({ sourceDir });
  const root = await readFile(path.join(sourceDir, 'index.html'), 'utf8');
  assert.match(root, /2 items/);
  assert.doesNotMatch(root, /data-name="(?:index.html|404.html)"/);
  assert.match(await readFile(path.join(sourceDir, 'folder/index.html'), 'utf8'), /new.json/);
});

test('the default DATA source keeps deployed report, folder and asset paths at the site root', async t => {
  const outputDir = await mkdtemp(path.join(os.tmpdir(), 'benchresults-data-'));
  t.after(() => rm(outputDir, { recursive: true, force: true }));
  await generateDirectoryIndexes({ outputDir });
  const rootEntries = await readdir(outputDir);
  assert.ok(!rootEntries.includes('DATA'));
  assert.ok(!rootEntries.includes('scripts'));
  assert.ok(!rootEntries.includes('tests'));
  for (const relativePath of ['hardinfo2_report_hunt.html', 'fsoverhead/report.html', 'fsoverhead/report.json', 'fsoverhead/fsbench.py', '_assets/browser.js']) {
    assert.deepEqual(await readFile(path.join(outputDir, relativePath)), await readFile(path.join(dataRoot, relativePath)));
  }
  const root = await readFile(path.join(outputDir, 'index.html'), 'utf8');
  const folder = await readFile(path.join(outputDir, 'fsoverhead/index.html'), 'utf8');
  assert.match(root, /href="\/hardinfo2_report_hunt"/);
  assert.match(root, /href="\/fsoverhead\/"/);
  assert.match(folder, /href="\/fsoverhead\/report"/);
  assert.match(root, /src="\/_assets\/browser.js"/);
  assert.doesNotMatch(root + folder, /(?:href|src)="\/DATA\//);
  const css = root.match(/<style id="archive-styles">([\s\S]*?)<\/style>/)?.[1];
  assert.ok(css, 'browser styles are available with the first HTML response');
  assert.doesNotMatch(root + folder, /<link[^>]+rel="stylesheet"/);
  assert.ok(gzipSync(css).length < 12_000, 'inlined CSS stays small enough for first paint');
  assert.match(css, /\.fa-arrow-down/);
  assert.match(css, /\.form-control/);
  assert.match(css, /\.table-responsive/);
  assert.match(css, /\.readme-body/);
  assert.doesNotMatch(css, /\.carousel|\.accordion|\.modal/);
  const fontUrls = [...new Set([...css.matchAll(/url\((\/_assets\/vendor-fonts\/[^)]+)\)/g)].map(match => match[1]))];
  assert.ok(fontUrls.length > 0, 'icon fonts remain available without a third-party CSS request');
  for (const url of fontUrls) {
    const bytes = await readFile(path.join(outputDir, url.slice(1)));
    const hash = createHash('sha256').update(bytes).digest('hex').slice(0, 12);
    assert.ok(url.endsWith(`.${hash}.woff2`), 'font URL reflects its actual contents');
  }
});

test('public sites inside DATA can have script folders and documentation files', async t => {
  const { sourceDir, outputDir, put } = await fixture(t);
  await put('scripts/chart.js', '"use strict";');
  await put('README.md', 'Benchmark notes');
  await generateDirectoryIndexes({ sourceDir, outputDir });
  assert.equal(await readFile(path.join(outputDir, 'scripts/chart.js'), 'utf8'), '"use strict";');
  const root = await readFile(path.join(outputDir, 'index.html'), 'utf8');
  assert.match(root, /href="\/scripts\/"/);
  assert.match(root, /href="\/README.md"/);
});

test('embeds each folder README below its listing and retains the raw file link', async t => {
  const { sourceDir, outputDir, put } = await fixture(t);
  await put('README.md', '# Root notes\n\n```js\nconst answer = 42;\n```');
  await put('fsoverhead/README.md', '# Folder notes\n\n[Report](report.html)\n\n```mermaid\ngraph TD\nA --> B\n```');
  await put('fsoverhead/report.html', '<p>Report</p>');
  await mkdir(path.join(sourceDir, 'no-readme'));
  await generateDirectoryIndexes({ sourceDir, outputDir });
  const root = await readFile(path.join(outputDir, 'index.html'), 'utf8');
  const folder = await readFile(path.join(outputDir, 'fsoverhead/index.html'), 'utf8');
  const plain = await readFile(path.join(outputDir, 'no-readme/index.html'), 'utf8');
  assert.ok(root.indexOf('class="readme-panel"') > root.indexOf('class="listing-footer"'));
  assert.match(root, /<h1 id="readme-root-notes">Root notes/);
  assert.match(root, /class="hljs-keyword">const/);
  assert.match(root, /href="\/README.md"/);
  assert.match(folder, /<h1 id="readme-folder-notes">Folder notes/);
  assert.match(folder, /href="\/fsoverhead\/report"/);
  assert.match(folder, /class="readme-mermaid"/);
  assert.doesNotMatch(folder, /Root notes/);
  assert.doesNotMatch(plain, /class="readme-panel"/);
  assert.equal(await readFile(path.join(outputDir, 'README.md'), 'utf8'), await readFile(path.join(sourceDir, 'README.md'), 'utf8'));
});

test('README detection supports lowercase names and preserves authored folder pages', async t => {
  const { sourceDir, outputDir, put } = await fixture(t);
  await put('lowercase/readme.md', '# Lowercase notes');
  await put('authored/index.html', '<h1>Existing site</h1>');
  await put('authored/README.md', '# Notes');
  await generateDirectoryIndexes({ sourceDir, outputDir });
  assert.match(await readFile(path.join(outputDir, 'lowercase/index.html'), 'utf8'), /Lowercase notes/);
  assert.equal(await readFile(path.join(outputDir, 'authored/index.html'), 'utf8'), '<h1>Existing site</h1>');
});
