import { Marked } from 'marked';
import hljs from 'highlight.js';
import sanitizeHtml from 'sanitize-html';
import GithubSlugger from 'github-slugger';
import { canonicalHtmlPath } from './page-url.mjs';

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, character => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[character]);
}

function resolveUrl(value, directoryUrl, pageLink = false) {
  if (value.startsWith('#')) return value.startsWith('#readme-') ? value : `#readme-${value.slice(1)}`;
  const base = new URL(directoryUrl, 'https://readme.invalid');
  try {
    const url = new URL(value, base);
    if (url.origin !== base.origin) return value;
    const pathname = pageLink ? canonicalHtmlPath(url.pathname) : url.pathname;
    return `${pathname}${url.search}${url.hash}`;
  } catch {
    return value;
  }
}

function scopeHeadingId(tagName, attributes) {
  if (attributes.id && !attributes.id.startsWith('readme-')) attributes.id = `readme-${attributes.id}`;
  return { tagName, attribs: attributes };
}

export function renderReadme(markdown, directoryUrl = '/') {
  const slugger = new GithubSlugger();
  const marked = new Marked({
    gfm: true,
    renderer: {
      heading({ text, tokens, depth }) {
        const id = `readme-${slugger.slug(text)}`;
        return `<h${depth} id="${escapeHtml(id)}">${this.parser.parseInline(tokens)}</h${depth}>\n`;
      },
      code({ text, lang }) {
        const language = (lang || '').trim().split(/\s+/)[0].toLowerCase();
        if (language === 'mermaid') {
          // Keep the graph definition readable when JS/CDN access is unavailable.
          return `<div class="readme-mermaid"><div class="mermaid-canvas" hidden></div><pre class="mermaid-source"><code>${escapeHtml(text)}</code></pre><p class="mermaid-error" role="status" hidden></p></div>\n`;
        }
        const supported = language && hljs.getLanguage(language);
        const code = supported ? hljs.highlight(text, { language, ignoreIllegals: true }).value : escapeHtml(text);
        return `<pre><code class="hljs${supported ? ` language-${escapeHtml(language)}` : ''}">${code}\n</code></pre>\n`;
      },
    },
  });

  return sanitizeHtml(marked.parse(markdown), {
    allowedTags: [...sanitizeHtml.defaults.allowedTags, 'img', 'del', 'input'],
    allowedAttributes: {
      a: ['href', 'title', 'id'],
      img: ['src', 'alt', 'title', 'width', 'height', 'loading', 'decoding'],
      h1: ['id'], h2: ['id'], h3: ['id'], h4: ['id'], h5: ['id'], h6: ['id'],
      code: ['class'], span: ['class'], pre: ['class'],
      div: ['class', 'hidden'], p: ['class', 'hidden', 'role'],
      th: ['align'], td: ['align'],
      input: ['type', 'checked', 'disabled'],
    },
    allowedClasses: {
      code: ['hljs', 'language-*'], span: ['hljs-*', '*_'],
      div: ['readme-mermaid', 'mermaid-canvas'],
      pre: ['mermaid-source'], p: ['mermaid-error'],
    },
    allowedEmptyAttributes: ['alt', 'hidden'],
    transformTags: {
      h1: scopeHeadingId, h2: scopeHeadingId, h3: scopeHeadingId,
      h4: scopeHeadingId, h5: scopeHeadingId, h6: scopeHeadingId,
      a(tagName, attributes) {
        if (attributes.href) attributes.href = resolveUrl(attributes.href, directoryUrl, true);
        if (attributes.id) attributes.id = `readme-${attributes.id}`;
        return { tagName, attribs: attributes };
      },
      img(tagName, attributes) {
        if (attributes.src) attributes.src = resolveUrl(attributes.src, directoryUrl);
        return { tagName, attribs: { ...attributes, loading: 'lazy', decoding: 'async' } };
      },
      input(tagName, attributes) {
        return { tagName, attribs: { type: 'checkbox', disabled: '', ...(attributes.checked !== undefined ? { checked: '' } : {}) } };
      },
    },
  });
}
