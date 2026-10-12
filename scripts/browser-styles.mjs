import { createHash } from 'node:crypto';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import CleanCSS from 'clean-css';
import { PurgeCSS } from 'purgecss';

const bootstrapStylesheet = fileURLToPath(import.meta.resolve('bootstrap/dist/css/bootstrap.min.css'));
const fontAwesomeRoot = path.dirname(fileURLToPath(import.meta.resolve('@fortawesome/fontawesome-free/package.json')));

export async function buildBrowserStyles({ sourceDir, outputDir, pages }) {
  const browserCss = await readFile(path.join(sourceDir, '_assets', 'browser.css'), 'utf8');
  const browserJs = await readFile(path.join(sourceDir, '_assets', 'browser.js'), 'utf8').catch(error => {
    if (error.code === 'ENOENT') return '';
    throw error;
  });
  const styles = await new PurgeCSS().purge({
    content: [
      ...pages.map(raw => ({ raw, extension: 'html' })),
      { raw: browserJs, extension: 'js' },
      // Include our own CSS so its selectors and Bootstrap variable references
      // survive, including README highlights and states applied after navigation.
      { raw: browserCss, extension: 'css' },
    ],
    css: [
      bootstrapStylesheet,
      path.join(fontAwesomeRoot, 'css', 'all.min.css'),
      { raw: browserCss },
    ],
    defaultExtractor: content => content.match(/[\w-/:]+(?<!:)/g) || [],
    safelist: ['fa-arrow-up', 'fa-arrow-down', 'fa-sort'],
    keyframes: true,
    variables: true,
    // Font Awesome selects its families through CSS variables. Keep font faces
    // rather than letting the purger incorrectly remove all of them.
    fontFace: false,
  });

  let fontAwesomeCss = styles[1].css;
  const fontDirectory = path.join(outputDir, '_assets', 'vendor-fonts');
  const fonts = [...fontAwesomeCss.matchAll(/url\(\.\.\/webfonts\/([^)]*\.woff2)\)/g)];
  if (fonts.length) {
    await mkdir(fontDirectory, { recursive: true });
    await writeFile(path.join(fontDirectory, 'LICENSE.txt'), await readFile(path.join(fontAwesomeRoot, 'LICENSE.txt')));
  }
  for (const name of new Set(fonts.map(match => match[1]))) {
    const bytes = await readFile(path.join(fontAwesomeRoot, 'webfonts', name));
    const hash = createHash('sha256').update(bytes).digest('hex').slice(0, 12);
    const filename = `${path.basename(name, '.woff2')}.${hash}.woff2`;
    await writeFile(path.join(fontDirectory, filename), bytes);
    fontAwesomeCss = fontAwesomeCss.replaceAll(`../webfonts/${name}`, `/_assets/vendor-fonts/${filename}`);
  }
  const css = new CleanCSS({ level: 1 }).minify([
    styles[0].css,
    fontAwesomeCss,
    // Preserve all archive styles; only the vendor libraries are pruned.
    browserCss,
  ].join('\n'));
  if (css.errors.length) throw new Error(css.errors.join('\n'));
  return css.styles;
}
