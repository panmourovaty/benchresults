import { mkdtemp, readdir, readFile, rename, rm, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { minify } from 'html-minifier-terser';
import CleanCSS from 'clean-css';
import { minify as minifyJavaScript } from 'terser';
import { dataRoot, generateDirectoryIndexes, projectRoot } from './generate-index.mjs';

const staging = await mkdtemp(path.join(projectRoot, '.build-'));
let count = 0;

async function minifyDirectory(directory) {
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const file = path.join(directory, entry.name);
    if (entry.isDirectory()) await minifyDirectory(file);
    else if (entry.name.toLowerCase().endsWith('.html')) {
      await writeFile(file, await minify(await readFile(file, 'utf8'), {
        collapseWhitespace: true,
        removeComments: true,
        minifyJS: true,
        minifyCSS: true,
      }));
      count++;
    }
  }
}

async function minifyBrowserAssets(directory) {
  const javascript = path.join(directory, '_assets', 'browser.js');
  const stylesheet = path.join(directory, '_assets', 'browser.css');
  const js = await minifyJavaScript(await readFile(javascript, 'utf8'));
  const css = new CleanCSS({ level: 1 }).minify(await readFile(stylesheet, 'utf8'));
  if (css.errors.length) throw new Error(css.errors.join('\n'));
  await writeFile(javascript, js.code);
  await writeFile(stylesheet, css.styles);
}

try {
  const { directories } = await generateDirectoryIndexes({ sourceDir: dataRoot, outputDir: staging });
  await minifyDirectory(staging);
  await minifyBrowserAssets(staging);
  await rm(path.join(projectRoot, 'dist'), { recursive: true, force: true });
  await rename(staging, path.join(projectRoot, 'dist'));
  console.log(`Built dist/: ${directories.size} directory listings, ${count} HTML files and browser JS/CSS minified.`);
} finally {
  await rm(staging, { recursive: true, force: true });
}
