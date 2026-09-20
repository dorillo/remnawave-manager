const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('./.tmp/node_modules/playwright-core');
const root = path.resolve(__dirname, '../src/remnawave_manager/data/disguises');
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.ASTER_BROWSER || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true });
  try {
    const page = await browser.newPage();
    const calls = [], errors = [];
    let failure = false;
    let holdRoots = false, releaseRoots;
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/*', async route => {
      const u = new URL(route.request().url());
      if (u.pathname.startsWith('/_aster/rutube-comments/')) {
        calls.push(u.search);
        if (failure) return route.fulfill({ status: 502, body: '{}' });
        const parent = u.searchParams.get('parent_id');
        if (holdRoots && !parent) await new Promise(resolve => { releaseRoots = resolve; });
        const cursor = u.searchParams.get('comment_id');
        const id = parent ? '200' : cursor ? '101' : '100';
        const comment = { id, text: 'Live comment ' + id, parent_id: parent, user: { id: 7, name: 'Reader', avatar_url: 'https://static.rutubelist.ru/avatar.png' }, replies_number: id === '100' ? 1 : 0, state: 1 };
        return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ results: [comment], comments_count: 42, has_next: !parent && !cursor }) });
      }
      if (u.pathname.startsWith('/_images/')) return route.fulfill({ contentType: 'image/svg+xml', body: '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1"/>' });
      if (u.pathname === '/') return route.fulfill({ contentType: 'text/html', body: '<!doctype html><body></body>' });
      return route.fulfill({ contentType: 'text/javascript', body: await fs.readFile(path.join(root, u.pathname)) });
    });
    await page.goto('https://aster.test/');
    async function mount(local = false, snapshot = []) {
      await page.evaluate(async ({ local, snapshot }) => {
        const { comments } = await import('/02-aster-observatory/aster-comments.js');
        window.controller?.abort();
        window.controller = new AbortController();
        document.body.replaceChildren(comments({ id: 'a'.repeat(32), local, publicComments: snapshot }, action => action(), window.controller.signal));
      }, { local, snapshot });
    }
    await mount();
    await page.getByText('Live comment 100', { exact: true }).waitFor();
    assert.equal(await page.locator('.comment-avatar img').getAttribute('src'), '/_images/static.rutubelist.ru/avatar.png');
    await page.getByRole('button', { name: 'Загрузить ответы', exact: true }).click();
    await page.getByText('Live comment 200', { exact: true }).waitFor();
    await page.getByRole('button', { name: 'Показать ещё комментарии', exact: true }).click();
    await page.getByText('Live comment 101', { exact: true }).waitFor();
    assert.deepEqual(calls, ['', '?parent_id=100', '?comment_id=100']);
    failure = true;
    await mount();
    await page.getByRole('button', { name: 'Повторить загрузку комментариев' }).waitFor();
    assert.equal(await page.getByText('Добавьте первый комментарий.').count(), 0);
    failure = false;
    await page.getByRole('button', { name: 'Повторить загрузку комментариев' }).click();
    await page.getByText('Live comment 100', { exact: true }).waitFor();
    // A slow first page must not discard replies fetched from a snapshot meanwhile.
    holdRoots = true;
    const snapshotRoot = { id: 'rt:100', text: 'Snapshot root', author: 'Reader', createdAt: Date.now(), likes: 0, repliesCount: 1 };
    await mount(false, [snapshotRoot]);
    await page.getByRole('button', { name: 'Загрузить ответы', exact: true }).click();
    await page.getByText('Live comment 200', { exact: true }).waitFor();
    holdRoots = false;
    releaseRoots();
    await page.getByText('Live comment 100', { exact: true }).waitFor();
    assert.equal(await page.getByText('Live comment 200', { exact: true }).count(), 1);
    failure = true;
    await mount(false, Array.from({ length: 12 }, (_, i) => ({ ...snapshotRoot, id: `rt:${i}`, repliesCount: 0, text: `Saved ${i}` })));
    await page.getByRole('button', { name: 'Повторить загрузку комментариев' }).waitFor();
    await page.getByRole('button', { name: 'Показать ещё комментарии', exact: true }).click();
    await page.getByText('Saved 11', { exact: true }).waitFor();
    const beforeLocal = calls.length;
    await mount(true);
    await page.getByText('Добавьте первый комментарий.').waitFor();
    assert.equal(calls.length, beforeLocal);
    assert.deepEqual(errors, []);
    console.log('PASS: comments absent from snapshot load live; pagination, replies, avatar_url, retry and local-only videos.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
