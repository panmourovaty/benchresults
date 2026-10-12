import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import vm from 'node:vm';
import test from 'node:test';

const source = await readFile(new URL('../DATA/_assets/browser.js', import.meta.url), 'utf8');

function requestHarness(cachedFetch, freshFetch, signal) {
  const listeners = new Map();
  vm.runInNewContext(source, {
    document: { addEventListener(name, listener) { listeners.set(name, listener); } },
    fetch: freshFetch,
    Intl,
  });
  const ctx = {
    sourceElement: { matches: () => true, setAttribute() {} },
    fetch: cachedFetch,
  };
  listeners.get('htmx:before:request')({ detail: { ctx } });
  return ctx.fetch('/folder/', { method: 'GET', headers: { 'HX-Request': 'true' }, signal });
}

test('navigation reuses a successful hover response without another request', async () => {
  const response = new Response('<main>Directory</main>');
  assert.equal(await requestHarness(() => Promise.resolve(response), () => assert.fail('unexpected request')), response);
});

test('a cached 503 is replaced by one fresh GET on navigation', async () => {
  let requests = 0;
  const response = await requestHarness(
    () => Promise.resolve(new Response('Unavailable', { status: 503 })),
    async (url, options) => {
      requests++;
      assert.equal(url, '/folder/');
      assert.equal(options.method, 'GET');
      assert.equal(options.headers['HX-Request'], 'true');
      assert.equal(options.cache, 'reload');
      return new Response('<main>Directory</main>');
    },
  );
  assert.equal(response.status, 200);
  assert.equal(requests, 1);
});

test('a rejected preload retries once and does not loop if the server still fails', async () => {
  let requests = 0;
  const response = await requestHarness(
    () => Promise.reject(new TypeError('Failed to fetch')),
    async () => { requests++; return new Response('Unavailable', { status: 503 }); },
  );
  assert.equal(response.status, 503);
  assert.equal(requests, 1);
});

test('cancelled navigation does not issue a retry', async () => {
  const controller = new AbortController();
  controller.abort();
  const error = new DOMException('Cancelled', 'AbortError');
  await assert.rejects(requestHarness(
    () => Promise.reject(error),
    () => assert.fail('cancelled request retried'),
    controller.signal,
  ), error);
});
