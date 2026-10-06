// Run scripts/preview_morrow.py first. Requires Chrome and Playwright (NODE_PATH supported).
// Set MORROW_MOBILE=1 to exercise the touch layout and swipe gesture.
import { createRequire } from 'node:module';
const { chromium } = createRequire(import.meta.url)('playwright');
import assert from 'node:assert/strict';
const browser = await chromium.launch({ channel: 'chrome', headless: true });
const mobile = process.env.MORROW_MOBILE === '1';
const context = await browser.newContext({ viewport: mobile ? { width: 390, height: 844 } : { width: 1280, height: 900 }, isMobile: mobile, hasTouch: mobile, reducedMotion: 'reduce' });
await context.addInitScript(() => {
  HTMLMediaElement.prototype.play = async function () {};
  HTMLMediaElement.prototype.pause = function () {};
  HTMLMediaElement.prototype.load = function () {};
  window.blobCreated = [];
  window.blobRevoked = [];
  const create = URL.createObjectURL.bind(URL), revoke = URL.revokeObjectURL.bind(URL);
  URL.createObjectURL = blob => { const url = create(blob); window.blobCreated.push(url); return url; };
  URL.revokeObjectURL = url => { window.blobRevoked.push(url); revoke(url); };
});
const id = n => n.toString(16).padStart(32, '0');
const author = { uuid: id(999), nickname: 'author', firstName: 'Author', subscribers: 12 };
const raw = n => ({ uuid: id(n), creator: author, description: `video-${n}`, link: 'https://vb-rtb.uma.media/test.mp4', publishedAt: `2026-09-${String(30 - Math.floor(n / 12)).padStart(2, '0')}T12:00:00Z`, likesCount: 1, viewsCount: 10, commentsCount: 0 });
let requests = [];
let failSearchPage = false;
await context.route('**/_morrow/yappy/**', async route => {
  const url = new URL(route.request().url());
  const path = url.pathname.split('/').slice(3).join('/');
  requests.push(path + url.search);
  if (failSearchPage && path === 'search' && url.searchParams.get('page') === '3') {
    await route.fulfill({ status: 503, json: {} });
    return;
  }
  let body;
  if (path.startsWith('author-videos/')) {
    const cursor = url.searchParams.get('created');
    const p = cursor ? 31 - Number(cursor.slice(8, 10)) : 1;
    body = { results: Array.from({ length: 12 }, (_, i) => raw((p - 1) * 12 + i + 1)), next: p < 4 ? 'next' : null };
  } else if (path.startsWith('author/')) body = author;
  else if (path.startsWith('video/')) body = raw(parseInt(path.split('/')[1], 16));
  else {
    const p = Number(url.searchParams.get('page') || 1);
    const start = path === 'search' ? 101 : 201;
    body = { results: Array.from({ length: 12 }, (_, i) => raw(start + (p - 1) * 12 + i)), next: p < 4 ? 'next' : null };
  }
  await route.fulfill({ json: body });
});
await context.route('https://vb-rtb.uma.media/**', route => route.abort());
const page = await context.newPage();
const errors = [];
page.on('pageerror', error => { errors.push(error.message); console.log('PAGEERROR', error.message); });

const base = 'http://127.0.0.1:5503/';
async function active(n) {
  await page.waitForFunction(expected => {
    const feed = document.querySelector('.feed');
    if (!feed) return false;
    const i = Math.round(feed.scrollTop / feed.clientHeight);
    return feed.querySelector(`[data-index="${i}"] .caption`)?.textContent === `video-${expected}`;
  }, n);
}
async function scroll(delta) {
  await page.locator('.feed').evaluate((feed, d) => { feed.scrollTop += d * feed.clientHeight; }, delta);
}
try {
  await page.goto(base + '#/explore');

  await page.locator('.video-tile').nth(19).waitFor();
  await page.evaluate(async raws => {
    const { authenticate } = await import('./morrow-auth.js');
    const { ProfileStore } = await import('./morrow-store.js');
    const { video } = await import('./morrow-yappy.js');
    const account = await authenticate('queue-test', 'password123', true);
    const store = await new ProfileStore(account).load();
    await store.change(d => {
      d.videos = raws.map(video);
      d.likes = d.videos.map(v => v.id);
      d.saved = [d.videos[2].id, d.videos[0].id, d.videos[3].id];
      d.history = [d.videos[1], d.videos[3], d.videos[0]].map(v => ({ id: v.id, at: Date.now() }));
      d.following = [raws[0].creator.uuid];
    });
    await new Promise((resolve, reject) => {
      const request = indexedDB.open('morrow:media:v1', 1);
      request.onupgradeneeded = () => request.result.createObjectStore('videos', { keyPath: 'id' }).createIndex('ownerId', 'ownerId');
      request.onsuccess = () => {
        const db = request.result;
        const tx = db.transaction('videos', 'readwrite');
        for (let i = 1; i <= 5; i++) tx.objectStore('videos').put({ id: (300 + i).toString(16).padStart(32, '0'), ownerId: account.id, description: `video-${300 + i}`, createdAt: i * 1000, file: new Blob(['test'], { type: 'video/mp4' }), poster: '', views: 0 });
        tx.oncomplete = () => { db.close(); resolve(); };
        tx.onerror = reject;
      };
    });
  }, [1, 2, 3, 4, 5].map(raw));
  for (const [tab, sequence] of [['likes', [5, 4, 3, 2, 1]], ['saved', [4, 1, 3]], ['history', [2, 4, 1]], ['myVideos', [305, 304, 303, 302, 301]]]) {
    await page.goto(base + '#/profile?tab=' + tab);
    await page.reload();
    await page.locator('.video-tile').nth(1).click();
    await active(sequence[1]);
    requests = [];
    await scroll(1); await active(sequence[2]);
    await scroll(-2); await active(sequence[0]);
    await page.reload(); await active(sequence[1]);
    assert.equal(requests.some(r => r.startsWith('feed?')), false, tab);
    await page.goBack();
    await page.locator('.video-tile').nth(1).click();
    await active(sequence[1]);
    console.log('PASS profile', tab, 'order, previous/next, reload, reselect');
    if (tab === 'myVideos') {
      await scroll(3); await active(301);
      await scroll(-4); await active(305);
      assert.equal(await page.evaluate(() => {
        const src = document.querySelector('[data-index="0"] video')?.src;
        return src?.startsWith('blob:') && !window.blobRevoked.includes(src);
      }), true);
      console.log('PASS uploads recreate blob URL on remount');
    }
  }
  for (const [source, first, forbidden] of [[`#/author/remote/${author.uuid}`, 1, 'feed?'], ['#/search?q=demo', 101, 'feed?'], ['#/explore', 201, 'search?']]) {
    await page.goto(base + source);
    await page.locator('.video-tile').nth(19).waitFor();
    requests = [];
    await page.locator('.video-tile').nth(18).click();
    await active(first + 18);
    await scroll(1); await active(first + 19);
    await scroll(1); await active(first + 20);
    await scroll(3); await active(first + 23);
    await scroll(1); await active(first + 24);
    assert.equal(requests.some(r => r.startsWith(forbidden)), false);
    const url = page.url();
    await page.goBack(); await page.locator('.video-tile').first().waitFor();
    await page.goForward(); await active(first + 24);
    await page.reload(); await active(first + 18);
    await page.goBack(); await page.locator('.video-tile').first().waitFor();
    await page.goForward(); await active(first + 18);
    await page.goto(url); await active(first + 18);
    console.log('PASS', source, 'pagination, reload, back/forward');
  }
  await page.goto(base + '#/search?q=retry');
  await page.locator('.video-tile').nth(19).waitFor();
  failSearchPage = true;
  await page.locator('.video-tile').nth(18).click(); await active(119);
  await scroll(5); await active(124);
  await page.locator('.feed-tail [role="alert"]').waitFor({ state: 'attached' });
  failSearchPage = false;
  await page.locator('.feed-tail button').evaluate(button => button.click());
  await page.waitForFunction(() => document.querySelectorAll('.video-slide').length > 24);
  await scroll(1); await active(125);
  console.log('PASS failed continuation retries the same search');
  await page.goto(base + '#/following'); await active(1);
  if (mobile) {
    const client = await context.newCDPSession(page);
    await client.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: 200, y: 650 }] });
    for (let y = 600; y >= 200; y -= 50)
      await client.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: 200, y }] });
    await client.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
  } else {
    await page.locator('.feed').hover();
    await page.mouse.wheel(0, 600);
  }
  await active(2);
  console.log('PASS following', mobile ? 'touch swipe' : 'mouse wheel');
  if (!mobile) {
    await page.keyboard.press('ArrowDown'); await active(3);
    await page.locator('.feed-arrows button').first().click(); await active(2);
    console.log('PASS keyboard and arrow controls');
  }
  await page.goto(base + `#/video/remote/${id(1)}`); await active(1);

  await page.waitForFunction(() => document.querySelectorAll('.video-slide').length > 1);
  console.log('PASS standalone link retains recommendations');
  assert.deepEqual(errors, []);
} catch (error) {
  console.log('DEBUG', await page.locator('body').innerText(), requests, errors);
  throw error;
} finally {
  await browser.close();
}
