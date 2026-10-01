/* Fresh lists must work without a packaged snapshot and preserve retry/cancellation. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('./.tmp/node_modules/playwright-core');
const root = path.resolve(__dirname, '../src/remnawave_manager/data/disguises');
const base = 'https://aster.test/02-aster-observatory/';
const id = n => n.toString(16).padStart(32, '0');
const raw = (n, channel = '7') => ({ id: id(n), title: `Fresh ${channel} video ${n}`, duration: 120,
  created_ts: '2020-01-01T12:00:00', publication_ts: new Date(Date.now() - n * 60000).toISOString(),
  author: { id: Number(channel), name: `Channel ${channel}` }, category: { id: 8 } });
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.ASTER_BROWSER || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true });
  try {
    const page = await browser.newPage();
    const errors = [], calls = [];
    let failedPage = 3, failFeed = false, failSearch = true, failEight = true, slowEight = false, releaseEight;
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/*', async route => {
      const u = new URL(route.request().url()), p = u.pathname;
      const json = data => route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
      if (p.endsWith('/data/catalog.json')) return route.fulfill({ status: 503, body: '{}' });
      if (p.startsWith('/_aster/')) {
        calls.push(p + u.search);
        if (p.startsWith('/_aster/rutube-channel/')) {
          const channel = p.split('/').at(-1), number = Number(u.searchParams.get('page'));
          if (channel === '8' && slowEight) await new Promise(resolve => { releaseEight = resolve; });
          if ((channel === '7' && number === failedPage) || (channel === '8' && failEight))
            return route.fulfill({ status: 502, body: '{}' });
          const start = (number - 1) * 20 + (channel === '8' ? 201 : 1);
          return json({ results: Array.from({ length: 20 }, (_, i) => raw(start + i, channel)), has_next: number < 3 });
        }
        if (p === '/_aster/rutube-feed') {
          if (failFeed) return route.fulfill({ status: 502, body: '{}' });
          const offset = Number(u.searchParams.get('offset'));
          return json({ results: Array.from({ length: 24 }, (_, i) => raw(offset + i + 101)), has_next: offset < 24 });
        }
        if (p.startsWith('/_aster/rutube-category/')) return json({ results: [raw(401)], has_next: false });
        if (p === '/_aster/rutube-search') {
          const number = Number(u.searchParams.get('page'));
          if (number === 2 && failSearch) return route.fulfill({ status: 502, body: '{}' });
          return json({ results: Array.from({ length: 20 }, (_, i) => raw(501 + (number - 1) * 20 + i)), has_next: number < 2 });
        }
        if (p.startsWith('/_aster/rutube-video/')) return json({ ...raw(801), id: p.split('/').at(-1) });
        if (p.startsWith('/_aster/rutube-profile/')) return json({ id: Number(p.split('/').at(-1)), name: 'Live channel', video_count: 60 });
        if (p.startsWith('/_aster/rutube-comments/')) return json({ results: [], has_next: false });
      }
      if (u.hostname !== 'aster.test') return route.fulfill({ body: '' });
      const file = path.join(root, p.endsWith('/') ? p + 'index.html' : p);
      try { return route.fulfill({ body: await fs.readFile(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.svg') ? 'image/svg+xml' : 'text/html' }); }
      catch { return route.fulfill({ status: 404, body: '' }); }
    });
    const settled = () => page.waitForFunction(() => document.querySelector('.live-results > .more') && !document.querySelector('.live-results > .more').disabled);
    const visit = async route => { await page.goto(base + '#/' + route); await settled(); };
    await visit('channel/7');
    assert.equal(await page.locator('.video-card').count(), 24);
    assert.match(await page.locator('.video-card').first().innerText(), /Fresh 7 video 1\n/);
    const normalized = await page.evaluate(async () => {
      const { normalize } = await import('./aster-data.js');
      return normalize({ id: 'a'.repeat(32), created_ts: '2020-01-01T00:00:00', publication_ts: '2026-10-01T20:26:08' }).published;
    });
    assert.equal(normalized, '2026-10-01T17:26:08.000Z');
    await page.locator('.live-results > .more').click(); await settled();
    assert.equal(await page.locator('.video-card').count(), 40, 'failed continuation retains earlier pages');
    failedPage = 0;
    await page.locator('.live-results > .more').click(); await settled();
    assert.equal(await page.locator('.video-card').count(), 60);
    assert.equal(await page.locator('.live-results > .more').isVisible(), false);
    assert.equal(new Set(await page.locator('.thumb').evaluateAll(a => a.map(x => x.href))).size, 60);
    assert.equal(calls.filter(x => x === '/_aster/rutube-channel/7?page=3').length, 2);
    await visit('home');
    assert.match(await page.locator('.video-card').first().innerText(), /Fresh 7 video 101/);
    await page.locator('.live-results > .more').click(); await settled();
    assert.equal(await page.locator('.video-card').count(), 48);
    await visit('catalog?sort=newest');
    assert.match(await page.locator('.video-card').first().innerText(), /Fresh 7 video 101/);
    await visit('catalog?topic=news');
    assert.equal(await page.locator('.video-card').count(), 1);
    assert(calls.includes('/_aster/rutube-category/8?page=1'));
    await page.goto(base + '#/catalog?q=fresh');
    await page.waitForFunction(() => document.querySelectorAll('.video-card').length === 20);
    await page.locator('main > .more').click();
    await page.getByRole('button', { name: 'Повторить', exact: true }).waitFor();
    assert.equal(await page.locator('.video-card').count(), 20, 'search failure preserves live results');
    failSearch = false;
    await page.getByRole('button', { name: 'Повторить', exact: true }).click();
    await page.waitForFunction(() => document.querySelectorAll('.video-card').length === 40);
    // A direct URL absent from both snapshot and search must work after reload.
    await page.goto(base + '#/watch/' + id(801));
    await page.locator('.watch-title').filter({ hasText: 'Fresh 7 video 801' }).waitFor();
    await page.waitForFunction(() => document.querySelectorAll('.watch-rail .video-card').length > 0);
    await page.evaluate(async () => {
      await (await import('./aster-auth.js')).register('Reader', 'fresh@example.test', 'password-123');
      (await import('./aster-store.js')).updateUser(account => { account.library.follows = ['7', '8']; });
    });
    await visit('subscriptions');
    assert.equal(await page.locator('.video-card').count(), 20, 'one unavailable channel does not hide the others');
    assert.match(await page.locator('.live-results > [role=status]').innerText(), /Не удалось/);
    failEight = false;
    await page.locator('.live-results > .more').click(); await settled();
    await page.locator('.live-results > .more').click(); await settled();
    assert(await page.getByText('Fresh 8 video 201', { exact: true }).count());
    // A late response must not overwrite the next route or report an error there.
    slowEight = true;
    await page.goto(base + '#/channel/8');
    await page.waitForTimeout(100);
    await page.evaluate(() => { location.hash = '/home'; });
    releaseEight?.();
    await settled();
    assert.match(await page.locator('main h1').innerText(), /Для вас/);
    assert.match(await page.locator('.video-card').first().innerText(), /Fresh 7 video 101/);
    // Without a snapshot, network failure still offers retry; success recovers.
    slowEight = false; failFeed = true;
    await page.reload(); await settled();
    assert.equal(await page.locator('.video-card').count(), 0);
    assert.equal(await page.locator('.live-results > .more').innerText(), 'Повторить');
    failFeed = false;
    await page.locator('.live-results > .more').click(); await settled();
    assert.equal(await page.locator('.video-card').count(), 24);
    assert.deepEqual(errors, []);
    console.log('ASTER freshness passed: live home/channel/topics/subscriptions, publication dates, pagination, retry, direct links, cancellation and no snapshot dependency.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
