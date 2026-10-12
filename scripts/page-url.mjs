// Cloudflare Pages serves these paths directly and redirects the .html URLs.
// Keep filesystem names intact; only navigation links use the canonical path.
export function canonicalHtmlPath(pathname) {
  if (pathname.endsWith('/index.html')) return pathname.slice(0, -10);
  return pathname.endsWith('.html') ? pathname.slice(0, -5) : pathname;
}
