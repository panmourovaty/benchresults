'use strict';

(() => {
  const collator = new Intl.Collator('en', { numeric: true, sensitivity: 'base' });
  let mermaidReady;
  let diagramSequence = 0;

  function initializeDirectory() {
    const browser = document.getElementById('directory-browser');
    if (!browser || browser.dataset.initialized) return;
    browser.dataset.initialized = 'true';
    browser.dataset.sortKey = 'name';
    browser.dataset.direction = 'ascending';
    browser.querySelectorAll('[data-enhanced]').forEach(element => {
      element.removeAttribute('inert');
      element.removeAttribute('aria-hidden');
    });
    browser.querySelectorAll('button[data-sort]').forEach(button => { button.disabled = false; });
    filterDirectory(browser);
    initializeDocumentPreloading(browser);
    renderReadmeDiagrams(browser);
  }

  function initializeDocumentPreloading(browser) {
    const connection = navigator.connection;
    if (!window.htmx || connection?.saveData || /(^|-)2g$/.test(connection?.effectiveType || '')) return;
    const directoryPath = browser.querySelector('.location-label span')?.textContent;
    for (const link of browser.querySelectorAll('[data-prefetch-document], .readme-panel a[href]')) {
      const href = link.getAttribute('href');
      if (href.startsWith('#') || link.hasAttribute('download')) continue;
      let url;
      try { url = new URL(href, location.href); } catch { continue; }
      if (url.origin !== location.origin || !/^https?:$/.test(url.protocol)) continue;
      if (url.pathname === directoryPath && !url.search) continue;
      // Let HTMX's preload extension warm the HTTP cache without intercepting
      // clicks. The custom request trigger is never fired by browser navigation.
      link.setAttribute('hx-get', url.pathname + url.search);
      link.setAttribute('hx-trigger', 'document-prefetch-only');
      link.setAttribute('hx-preload', 'mouseenter, focus, touchstart');
      link.setAttribute('hx-config', '{"cache":"force-cache"}');
      htmx.process(link);
    }
  }

  async function renderReadmeDiagrams(browser) {
    const diagrams = [...browser.querySelectorAll('.readme-mermaid')];
    if (!diagrams.length) return;
    const showError = diagram => {
      const message = diagram.querySelector('.mermaid-error');
      message.textContent = 'Unable to display this diagram. The source is shown above.';
      message.hidden = false;
    };
    try {
      // Dynamic ES module imports are deferred and strict. Ordinary listings
      // never download Mermaid, and navigation reuses this one import promise.
      mermaidReady ||= import('https://cdn.jsdelivr.net/npm/mermaid@12.1.0/dist/mermaid.esm.min.mjs')
        .then(({ default: mermaid }) => {
          mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'default', suppressErrorRendering: true });
          return mermaid;
        }).catch(error => { mermaidReady = null; throw error; });
      const mermaid = await mermaidReady;
      for (const diagram of diagrams) {
        if (browser !== document.getElementById('directory-browser')) return;
        try {
          const source = diagram.querySelector('.mermaid-source');
          const canvas = diagram.querySelector('.mermaid-canvas');
          const { svg } = await mermaid.render(`readme-diagram-${++diagramSequence}`, source.textContent);
          if (browser !== document.getElementById('directory-browser')) return;
          canvas.innerHTML = svg;
          canvas.hidden = false;
          source.hidden = true;
        } catch {
          showError(diagram);
        }
      }
    } catch {
      if (browser === document.getElementById('directory-browser')) diagrams.forEach(showError);
    }
  }

  function filterDirectory(browser) {
    const search = browser.querySelector('#file-search');
    if (!search) return;
    const query = search.value.trim().toLocaleLowerCase();
    const rows = [...browser.querySelectorAll('[data-entry]')];
    let count = 0;
    rows.forEach(row => {
      row.hidden = !row.dataset.name.toLocaleLowerCase().includes(query);
      if (!row.hidden) count++;
    });
    const empty = browser.querySelector('[data-empty]');
    empty.hidden = count > 0;
    empty.querySelector('p').textContent = rows.length ? 'No files match your filter.' : 'This folder is empty.';
    empty.querySelector('[data-clear-filter]').hidden = !query;
    browser.querySelector('[data-result-count]').textContent = query
      ? `${count} of ${rows.length} items`
      : `${count} ${count === 1 ? 'item' : 'items'}`;
  }

  document.addEventListener('input', event => {
    if (event.target.id === 'file-search') filterDirectory(event.target.closest('#directory-browser'));
  });

  document.addEventListener('click', event => {
    const clear = event.target.closest('[data-clear-filter]');
    if (clear) {
      const browser = clear.closest('#directory-browser');
      const search = browser.querySelector('#file-search');
      search.value = '';
      filterDirectory(browser);
      search.focus();
      return;
    }
    const button = event.target.closest('button[data-sort]');
    if (!button || button.disabled) return;
    const browser = button.closest('#directory-browser');
    if (!browser) return;
    const key = button.dataset.sort;
    const direction = browser.dataset.sortKey === key && browser.dataset.direction === 'ascending' ? 'descending' : 'ascending';
    browser.dataset.sortKey = key;
    browser.dataset.direction = direction;
    const rows = [...browser.querySelectorAll('[data-entry]')];
    rows.sort((left, right) => {
      const folderOrder = Number(right.dataset.kind === 'folder') - Number(left.dataset.kind === 'folder');
      if (folderOrder) return folderOrder;
      const comparison = key === 'name' ? collator.compare(left.dataset.name, right.dataset.name)
        : Number(left.dataset[key]) - Number(right.dataset[key]);
      return (comparison || collator.compare(left.dataset.name, right.dataset.name)) * (direction === 'ascending' ? 1 : -1);
    });
    const tbody = browser.querySelector('#file-list');
    const empty = browser.querySelector('[data-empty]');
    rows.forEach(row => tbody.insertBefore(row, empty));
    browser.querySelectorAll('button[data-sort]').forEach(sortButton => {
      const active = sortButton === button;
      sortButton.closest('th').setAttribute('aria-sort', active ? direction : 'none');
      sortButton.querySelector('i').className = `fa-solid ${active ? (direction === 'ascending' ? 'fa-arrow-up' : 'fa-arrow-down') : 'fa-sort'}`;
    });
  });

  // Once HTMX consumes a preload, keep pointer events from immediately warming
  // the same link again while the outgoing directory is being replaced.
  document.addEventListener('htmx:before:request', event => {
    const { ctx } = event.detail;
    const link = ctx.sourceElement;
    if (!link.matches('a[hx-boost="true"]')) return;
    link.setAttribute('data-navigation-pending', '');
    // The preload extension may supply a rejected promise or an HTTP error.
    // On a click, retry once with a fresh GET instead of reusing that response.
    const fetchRequest = ctx.fetch;
    ctx.fetch = async (url, options) => {
      try {
        const response = await fetchRequest(url, options);
        if (response.ok) return response;
      } catch (error) {
        if (options.signal?.aborted) throw error;
      }
      return fetch(url, { ...options, cache: 'reload' });
    };
  });
  document.addEventListener('htmx:finally:request', event => {
    event.detail.ctx.sourceElement.removeAttribute('data-navigation-pending');
  });
  document.addEventListener('mouseenter', event => {
    if (event.target.matches?.('a[data-navigation-pending]')) event.stopImmediatePropagation();
  }, true);

  document.addEventListener('DOMContentLoaded', initializeDirectory);
  document.addEventListener('htmx:after:process', initializeDirectory);
})();
