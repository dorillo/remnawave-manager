/* Fresh lists must work without a packaged snapshot and preserve retry/cancellation. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const os = require('node:os');
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
    const held = new Map();
    const hold = key => {
      let release;
      held.set(key, new Promise(resolve => { release = resolve; }));
      return () => { held.delete(key); release(); };
    };
    const categories = { movies: 4, series: 5, music: 6, entertainment: 57,
      kids: 7, sport: 16, news: 8, science: 52, travel: 11, culture: 64 };
    let failedPage = 3, failFeed = false, failSearch = true, failEight = true, slowEight = false, releaseEight;
    let sparseFeed = false;
    const feedSizes = [24, 24, 24, 10, 12, 8, 7, 9, 10, 20, 9];
    page.on('pageerror', e => errors.push(e.message));
    await page.route('**/*', async route => {
      const u = new URL(route.request().url()), p = u.pathname;
      const json = data => route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
      if (p.endsWith('/data/catalog.json')) return route.fulfill({ status: 503, body: '{}' });
      if (p.startsWith('/_aster/')) {
        calls.push(p + u.search);
        if (held.has(p + u.search)) await held.get(p + u.search);
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
          if (sparseFeed === 'budget')
            return json({ results: [raw(2001 + offset / 24)], has_next: offset < 24 * 24 });
          if (sparseFeed === 'repeated')
            return json({ results: [raw(3001)], has_next: true });
          if (sparseFeed) {
            const index = offset / 24;
            const start = feedSizes.slice(0, index).reduce((sum, size) => sum + size, 0);
            // Overlapping and unavailable results leave short normalized pages.
            return json({ results: [raw(1001), { ...raw(9999), is_paid: true },
              ...Array.from({ length: feedSizes[index] }, (_, i) => raw(1001 + start + i))],
              has_next: index < feedSizes.length - 1 });
          }
          return json({ results: Array.from({ length: 24 }, (_, i) => raw(offset + i + 101)), has_next: offset < 24 });
        }
        if (p.startsWith('/_aster/rutube-category/')) {
          const category = Number(p.split('/').at(-1));
          if (!Object.values(categories).includes(category)) return route.fulfill({ status: 404, body: '{}' });
          return json({ results: [{ ...raw(401), category: { id: category } }], has_next: false });
        }
        if (p === '/_aster/rutube-search') {
          const number = Number(u.searchParams.get('page'));
          if (number === 2 && failSearch && u.searchParams.get('query') === 'fresh') return route.fulfill({ status: 502, body: '{}' });
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
    const settled = () => page.waitForFunction(() => document.querySelector('.live-results > .list-feedback > .more') && !document.querySelector('.live-results > .list-feedback > .more').disabled);
    const visit = async route => { await page.goto(base + '#/' + route); await settled(); };
    const releaseFirst = hold('/_aster/rutube-channel/7?page=1');
    await page.goto(base + '#/channel/7');
    await page.locator('.live-results .skeleton-grid').waitFor();
    assert.equal(await page.locator('.live-results .skeleton-card').count(), 24);
    assert.equal(await page.locator('.live-results .ui-spinner').count(), 0);
    for (const width of [375, 768, 1440, 1920, 2334]) {
      await page.setViewportSize({ width, height: 900 });
      for (const theme of ['light', 'dark']) {
        await page.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), 'skeletons fit the viewport');
        const columns = await page.locator('.skeleton-grid').evaluate(node => getComputedStyle(node).gridTemplateColumns.split(' ').length);
        assert.equal(24 % columns, 0, `complete skeleton rows at ${width}`);
        await page.screenshot({ path: path.join(os.tmpdir(), `aster-skeleton-${theme}-${width}.png`) });
      }
    }
    releaseFirst(); await settled();
    assert.equal(await page.locator('.skeleton-grid').count(), 0);
    assert.equal(await page.locator('.video-card').count(), 24);
    assert.match(await page.locator('.video-card').first().innerText(), /Fresh 7 video 1\n/);
    const normalized = await page.evaluate(async () => {
      const { normalize } = await import('./aster-data.js');
      return normalize({ id: 'a'.repeat(32), created_ts: '2020-01-01T00:00:00', publication_ts: '2026-10-01T20:26:08' }).published;
    });
    assert.equal(normalized, '2026-10-01T17:26:08.000Z');
    const releaseMore = hold('/_aster/rutube-channel/7?page=3');
    await page.locator('.live-results > .list-feedback > .more').click();
    await page.locator('.live-results .skeleton-grid').waitFor();
    assert.equal(await page.locator('.video-card').count(), 24, 'loading continuation keeps existing cards');
    assert.equal(await page.locator('.live-results .skeleton-card').count(), 24, 'continuation reserves a full batch');
    releaseMore(); await settled();
    assert.equal(await page.locator('.skeleton-grid').count(), 0, 'failed continuation clears skeletons');
    assert.equal(await page.locator('.video-card').count(), 40, 'failed continuation retains earlier pages');
    failedPage = 0;
    await page.locator('.live-results > .list-feedback > .more').click(); await settled();
    assert.equal(await page.locator('.video-card').count(), 60);
    assert.equal(await page.locator('.live-results > .list-feedback > .more').isVisible(), false);
    assert.equal(new Set(await page.locator('.thumb').evaluateAll(a => a.map(x => x.href))).size, 60);
    assert.equal(calls.filter(x => x === '/_aster/rutube-channel/7?page=3').length, 2);
    await visit('home');
    assert.match(await page.locator('.video-card').first().innerText(), /Fresh 7 video 101/);
    await page.locator('.live-results > .list-feedback > .more').click(); await settled();
    assert.equal(await page.locator('.video-card').count(), 48);
    sparseFeed = true;
    await page.reload(); await settled();
    for (let batch = 1; batch <= 7; batch++) {
      if (batch > 1) {
        await page.locator('.live-results > .list-feedback > .more').click(); await settled();
      }
      assert.equal(await page.locator('.video-card').count(), Math.min(batch * 24, 157),
        `batch ${batch} fills despite duplicates, unavailable videos and short pages`);
    }
    const links = await page.locator('.thumb').evaluateAll(nodes => nodes.map(node => node.hash));
    assert.deepEqual(links, Array.from({ length: 157 }, (_, i) => '#/watch/' + id(1001 + i)),
      'buffered results retain provider order without losing or repeating videos');
    assert.equal(await page.locator('.live-results > .list-feedback > .more').isVisible(), false);
    sparseFeed = 'budget';
    let before = calls.length;
    await page.reload(); await settled();
    const feedCallsSince = () => calls.slice(before).filter(call => call.startsWith('/_aster/rutube-feed')).length;
    assert.equal(feedCallsSince(), 10, 'sparse feeds respect the per-click request budget');
    assert.equal(await page.locator('.video-card').count(), 0, 'short batches stay buffered');
    await page.locator('.live-results > .list-feedback > .more').click(); await settled();
    assert.equal(feedCallsSince(), 20);
    await page.locator('.live-results > .list-feedback > .more').click(); await settled();
    assert.equal(feedCallsSince(), 24);
    assert.equal(await page.locator('.video-card').count(), 24, 'buffer survives multiple clicks');
    await page.locator('.live-results > .list-feedback > .more').click(); await settled();
    assert.equal(await page.locator('.video-card').count(), 25, 'the final partial row is never discarded');
    sparseFeed = 'repeated';
    before = calls.length;
    await page.reload(); await settled();
    assert.equal(feedCallsSince(), 4, 'repeated pages stop without a request loop');
    assert.equal(await page.locator('.video-card').count(), 1);
    assert.equal(await page.locator('.live-results > .list-feedback > .more').isVisible(), false);
    sparseFeed = false;
    await page.reload(); await settled();
    await visit('catalog?sort=newest');
    assert.match(await page.locator('.video-card').first().innerText(), /Fresh 7 video 101/);
    await visit('catalog?topic=news');
    assert.equal(await page.locator('.video-card').count(), 1);
    assert(calls.includes('/_aster/rutube-category/8?page=1'));
    for (const [topic, category] of Object.entries(categories)) {
      await visit('catalog?topic=' + topic);
      assert.equal(await page.locator('.video-card').count(), 1, topic + ' loads');
      assert(calls.includes(`/_aster/rutube-category/${category}?page=1`), topic + ' requests the matching category');
      assert.equal(await page.locator('.list-feedback.has-error').count(), 0);
    }
    for (const topic of ['education', 'city']) {
      await visit('catalog?topic=' + topic);
      assert.equal(await page.locator('.video-card').count(), 24, topic + ' loads through search');
    }
    const releaseSearch = hold('/_aster/rutube-search?query=fresh&client=wdp&page=1');
    await page.goto(base + '#/catalog?q=fresh');
    await page.locator('main > .skeleton-grid').waitFor();
    assert.equal(await page.locator('main .ui-spinner').count(), 0);
    releaseSearch();
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
    assert.match(await page.locator('.live-results .list-status').innerText(), /Не удалось/);
    failEight = false;
    await page.locator('.live-results > .list-feedback > .more').click(); await settled();
    await page.locator('.live-results > .list-feedback > .more').click(); await settled();
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
    assert.equal(await page.locator('.skeleton-grid').count(), 0, 'failed initial load clears skeletons');
    assert.equal(await page.locator('.live-results > .list-feedback > .more').innerText(), 'Повторить');
    failFeed = false;
    await page.locator('.live-results > .list-feedback > .more').click(); await settled();
    assert.equal(await page.locator('.video-card').count(), 24);
    assert.deepEqual(errors, []);
    console.log('ASTER freshness passed: live home/channel/topics/subscriptions, publication dates, pagination, retry, direct links, cancellation and no snapshot dependency.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
