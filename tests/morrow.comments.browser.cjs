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
    let release, requestReady, fail = false, paginated = false;
    let pageSize = 20, pageCount = 3, automaticResponse = false;
    const requests = [];
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.startsWith('/_morrow/')) {
        const current = Number(url.searchParams.get('page'));
        requests.push(current);
        if (!automaticResponse) await new Promise(resolve => { release = resolve; requestReady(); });
        const results = paginated ? Array.from({ length: pageSize }, (_, i) => ({ hex: (current * pageSize + i).toString(16), markdown: `Комментарий ${current}-${i}`, commenter: { nickname: 'author' } })) : [];
        return route.fulfill({ status: fail ? 502 : 200, contentType: 'application/json', body: JSON.stringify({ results, next: paginated && current < pageCount ? 'true' : null }) });
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
    await page.locator('dialog').waitFor({ state: 'detached' });
    fail = true;
    await open('2'.repeat(32));
    release();
    await page.locator('.comment-form .error').filter({ hasText: /.+/ }).waitFor();
    assert.equal(await page.locator('.empty-note').count(), 0, 'API errors do not claim that comments are empty');
    await page.keyboard.press('Escape');
    await page.locator('dialog').waitFor({ state: 'detached' });
    fail = false;
    paginated = true;
    requests.length = 0;
    await open('3'.repeat(32));
    const more = page.locator('.more-comments');
    assert.equal(await more.isDisabled(), true);
    release();
    await page.waitForFunction(() => !document.querySelector('.more-comments').disabled);
    assert.equal(await page.locator('.comment').count(), 20);
    await page.locator('.comment-list').evaluate(list => {
      list.scrollTop = list.scrollHeight;
      list.dispatchEvent(new Event('scroll'));
    });
    await page.waitForTimeout(150);
    assert.deepEqual(requests, [1], 'opening and scrolling load only the first page');
    async function nextPage() {
      const requested = new Promise(resolve => { requestReady = resolve; });
      await more.click();
      await requested;
      await more.evaluate(button => button.click());
      assert.equal(await more.isDisabled(), true);
      assert.equal(await more.isVisible(), false);
      assert.equal(await page.locator('.comment-list .comments-loading').count(), 1);
      assert.equal(await page.locator('.comments-loading').evaluate(node =>
        node.getBoundingClientRect().top >= document.querySelector('.comment:last-of-type').getBoundingClientRect().bottom), true,
        'loading indicator stays below the comments');
      await page.locator('.comment-list').evaluate(list => { list.scrollTop = 0; });
      await page.locator('.comment-author').first().click();
      release();
      await page.waitForFunction(() => !document.querySelector('.more-comments').disabled);
    }
    fail = true;
    await nextPage();
    assert.equal(await page.locator('.comment').count(), 20, 'failed continuation keeps loaded comments');
    assert.equal(await more.isVisible(), true);
    fail = false;
    await nextPage();
    assert.equal(await page.locator('.comment').count(), 40);
    assert.deepEqual(requests, [1, 2, 2], 'retry requests the same page without concurrent duplicates');
    await nextPage();
    assert.equal(await page.locator('.comment').count(), 60);
    assert.equal(await more.isVisible(), false, 'button disappears when comments end');
    assert.deepEqual(requests, [1, 2, 2, 3]);
    await page.keyboard.press('Escape');
    await page.locator('dialog').waitFor({ state: 'detached' });
    pageSize = 3;
    pageCount = 10;
    automaticResponse = true;
    requests.length = 0;
    for (const width of [360, 1280]) {
      await page.setViewportSize({ width, height: 800 });
      await page.evaluate(async width => {
        const { openComments } = await import('/03-morrow-coffee/morrow-comments.js');
        openComments({ id: String(width === 360 ? 4 : 5).repeat(32) }, { getStore: () => null, gate: () => {}, mutate: () => {}, openAuthor: () => {} });
      }, width);
      await page.waitForFunction(() => !document.querySelector('.more-comments').disabled);
      assert.equal(await page.locator('.comment').count(), 12, 'short API pages form a useful batch');
      const calls = requests.length;
      await page.locator('.comment-list').evaluate(list => {
        list.scrollTop = list.scrollHeight;
        list.dispatchEvent(new Event('scroll'));
      });
      await page.waitForTimeout(150);
      assert.equal(requests.length, calls, 'scrolling never starts the next batch');
      assert.equal(await more.evaluate(node => node.closest('.comment-list') !== null), true);
      const scrollTop = await page.locator('.comment-list').evaluate(list => list.scrollTop);
      await more.click();
      await page.waitForFunction(() => !document.querySelector('.more-comments').disabled);
      assert.equal(await page.locator('.comment').count(), 24);
      assert.equal(await page.locator('.comment-list').evaluate(list => list.scrollTop), scrollTop, 'continuation preserves scroll position');
      await more.click();
      await page.waitForFunction(() => !document.querySelector('.more-comments').disabled);
      assert.equal(await page.locator('.comment').count(), 30);
      assert.equal(await more.isVisible(), false);
      await page.keyboard.press('Escape');
      await page.locator('dialog').waitFor({ state: 'detached' });
    }
    assert.deepEqual(errors, []);
    console.log('Morrow comments passed: empty/error states, manual pagination, busy guard, retry and exhaustion.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
