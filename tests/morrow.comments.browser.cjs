const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('./.tmp/node_modules/playwright-core');
const root = path.resolve(__dirname, '../src/remnawave_manager/data/disguises');
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.MORROW_BROWSER || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' });
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let release, requestReady, fail = false;
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/_morrow/')) {
        await new Promise(resolve => { release = resolve; requestReady(); });
        return route.fulfill({ status: fail ? 502 : 200, contentType: 'application/json', body: JSON.stringify({ results: [], next: null }) });
      }
      if (url.pathname === '/') return route.fulfill({ contentType: 'text/html', body: '<!doctype html><html lang="ru"><link rel="stylesheet" href="/03-morrow-coffee/styles.css"><body><div id="notice"></div></body>' });
      return route.fulfill({ body: await fs.readFile(path.join(root, url.pathname)), contentType: url.pathname.endsWith('.css') ? 'text/css' : 'text/javascript' });
    });
    await page.goto('https://morrow.test/');
    async function open(id) {
      const requested = new Promise(resolve => { requestReady = resolve; });
      await page.evaluate(async id => {
        const { openComments } = await import('/03-morrow-coffee/morrow-comments.js');
        openComments({ id }, { getStore: () => null, gate: () => {}, mutate: () => {}, openAuthor: () => {} });
      }, id);
      await requested;
    }
    await open('1'.repeat(32));
    assert.equal(await page.locator('.empty-note').count(), 0);
    assert.equal(await page.locator('.comments-loading').isVisible(), true);
    release(); release = null;
    await page.getByText('Комментариев пока нет. Начните обсуждение', { exact: true }).waitFor();
    assert.equal(await page.locator('.comments-loading').isVisible(), false);
    await page.keyboard.press('Escape');
    fail = true;
    await open('2'.repeat(32));
    release();
    await page.locator('.comment-form .error').filter({ hasText: /.+/ }).waitFor();
    assert.equal(await page.locator('.empty-note').count(), 0, 'API errors do not claim that comments are empty');
    assert.deepEqual(errors, []);
    console.log('Morrow: empty comments appear after loading; failures remain distinct from empty results.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
