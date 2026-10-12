import assert from 'node:assert/strict';
import test from 'node:test';
import { minify } from 'html-minifier-terser';
import { renderReadme } from '../scripts/render-readme.mjs';

test('renders GitHub-style Markdown with heading anchors, tables and task lists', () => {
  const html = renderReadme('# Results\n\n[Jump](#results)\n\n## Results\n\n**Bold** and *italic* with `code`.\n\n| OS | Score |\n| -- | -- |\n| Linux | 42 |\n\n- [x] Complete\n- [ ] Pending\n');
  assert.match(html, /<h1 id="readme-results">Results<\/h1>/);
  assert.match(html, /<h2 id="readme-results-1">Results<\/h2>/);
  assert.match(html, /href="#readme-results"/);
  assert.match(html, /<strong>Bold<\/strong>/);
  assert.match(html, /<table>/);
  assert.match(html, /type="checkbox" disabled checked/);
});

test('highlights fenced snippets during generation and preserves indentation through minification', async () => {
  const html = renderReadme('```python\ndef bench():\n    return "ok"\n```\n\n```javascript\nconst result = 42;\n```');
  assert.match(html, /class="hljs language-python"/);
  assert.match(html, /class="hljs-keyword">def/);
  assert.match(html, /class="hljs-string">"ok"/);
  assert.match(html, /class="hljs language-javascript"/);
  const minified = await minify(html, { collapseWhitespace: true, removeComments: true });
  assert.match(minified, /\n    <span class="hljs-keyword">return/);
});

test('unknown and unspecified languages render as escaped plain code', () => {
  const html = renderReadme('```not-a-language\n<script>alert("no")</script>\n```\n\n```\na < b && c > d\n```');
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /a &lt; b &amp;&amp; c &gt; d/);
  assert.doesNotMatch(html, /<script>/);
});

test('keeps Mermaid definitions intact and hides unused canvas and error elements', async () => {
  const html = renderReadme('```mermaid\nflowchart TD\n    A[Read] --> B[Render]\n```');
  assert.match(html, /class="readme-mermaid"/);
  assert.match(html, /class="mermaid-canvas" hidden/);
  assert.match(html, /class="mermaid-source"><code>flowchart TD\n    A\[Read\] --&gt; B\[Render\]/);
  assert.match(html, /class="mermaid-error" role="status" hidden/);
  const minified = await minify(html, { collapseWhitespace: true });
  assert.match(minified, /flowchart TD\n    A\[Read\]/);
});

test('resolves relative Markdown and HTML links and images from the folder URL', () => {
  const html = renderReadme('[Report](./report.html)\n\n[Parent](../summary.html)\n\n[External](https://example.com/)\n\n![Plot](images/plot.png)\n\n<a href="./report.json">Data</a><img src="../logo.svg">', '/fsoverhead/nested/');
  assert.match(html, /href="\/fsoverhead\/nested\/report.html"/);
  assert.match(html, /href="\/fsoverhead\/summary.html"/);
  assert.match(html, /href="https:\/\/example.com\/"/);
  assert.match(html, /src="\/fsoverhead\/nested\/images\/plot.png"/);
  assert.match(html, /src="\/fsoverhead\/logo.svg"/);
  assert.match(html, /href="\/fsoverhead\/nested\/report.json"/);
  assert.match(html, /loading="lazy" decoding="async"/);
});

test('README HTML cannot execute scripts, override navigation or collide with the browser IDs', () => {
  const html = renderReadme('<script>alert(1)</script><img src="x.png" onerror="alert(1)"><a href="javascript:alert(1)" hx-boost="true">bad</a><h1 id="directory-browser">Title</h1>\n\n```html\n<script>const example = true;</script>\n```');
  assert.doesNotMatch(html, /<script|onerror=|javascript:|hx-boost=/);
  assert.match(html, /id="readme-directory-browser"/);
  assert.match(html, /class="hljs language-html"/);
});
