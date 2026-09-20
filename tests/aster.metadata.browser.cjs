// Live metadata must enrich sparse cards and freshly loaded commenter profiles.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('./.tmp/node_modules/playwright-core');
const root = path.resolve(__dirname, '../src/remnawave_manager/data/disguises');
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.ASTER_BROWSER || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' });
  try {
    const page = await browser.newPage({ locale: 'ru-RU', viewport: { width: 390, height: 900 } });
    const id = 'a'.repeat(32), errors = [];
    let failProfile = false, failDescription = false;
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      const json = data => route.fulfill({ contentType: 'application/json', body: JSON.stringify(data) });
      if (url.origin !== 'https://aster.test') return route.fulfill({ body: '' });
      if (url.pathname.endsWith('/data/catalog.json')) return json({ videos: [{ id, title: 'Video', channelId: '7', channel: 'Channel' }] });
      if (url.pathname.startsWith('/_aster/rutube-video/')) {
        if (failDescription) return route.fulfill({ status: 502, body: '{}' });
        return json({ id, title: 'Video', description: 'Live description <script>untrusted()</script>' });
      }
      if (url.pathname.startsWith('/_aster/rutube-profile/')) {
        if (failProfile) return route.fulfill({ status: 502, body: '{}' });
        const profileId = url.pathname.split('/').at(-1);
        return json({ id: profileId, name: profileId === '7' ? 'Channel' : 'Fresh commenter', subscribers_count: 1234, video_count: 12 });
      }
      if (url.pathname.startsWith('/_aster/rutube-comments/')) return json({ results: [{ id: 1, text: 'Comment absent from the catalog', user: { id: 42, name: 'Fresh commenter' } }], has_next: false });
      const file = path.join(root, url.pathname.endsWith('/') ? url.pathname + 'index.html' : url.pathname);
      try {
        return route.fulfill({ body: await fs.readFile(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.svg') ? 'image/svg+xml' : 'text/html' });
      } catch { return route.fulfill({ status: 404, body: '' }); }
    });
    await page.goto(`https://aster.test/02-aster-observatory/#/watch/${id}`);
    await page.getByText('Live description <script>untrusted()</script>', { exact: true }).waitFor();
    await page.locator('.watch-channel .subscriber-count').filter({ hasText: '234' }).waitFor();
    assert.equal(await page.locator('.description script').count(), 0);
    await page.locator('.comment-author a').click();
    await page.locator('.user-banner h1').filter({ hasText: 'Fresh commenter' }).waitFor();
    await page.getByText('Comment absent from the catalog', { exact: true }).waitFor();
    await page.reload();
    await page.locator('.user-banner h1').filter({ hasText: 'Fresh commenter' }).waitFor();
    await page.locator('.user-banner .subscriber-count').filter({ hasText: '234' }).waitFor();
    // A network error must never be labelled as a deleted or missing profile.
    failProfile = true;
    await page.reload();
    await page.getByText('Профиль временно недоступен.', { exact: true }).waitFor();
    assert.equal(await page.getByText('Профиль пользователя не найден', { exact: true }).count(), 0);
    failProfile = false;
    await page.locator('.user-banner button').click();
    await page.locator('.user-banner h1').waitFor();
    failDescription = true;
    await page.evaluate(id => { location.hash = `/watch/${id}`; }, id);
    await page.getByText('Не удалось загрузить описание.', { exact: false }).waitFor();
    failDescription = false;
    await page.locator('.description button').click();
    await page.getByText('Live description <script>untrusted()</script>', { exact: true }).waitFor();

    await page.evaluate(async () => {
      await (await import('/02-aster-observatory/aster-auth.js')).register('Reader', 'reader@example.test', 'test-password');
    });
    await page.reload();
    const subscribers = page.locator('.watch-channel .subscriber-count');
    await page.waitForFunction(() => document.querySelector('.watch-channel .subscriber-count')?.textContent.replace(/\D/g, '') === '1234');
    await page.locator('.watch-channel button[aria-pressed]').click();
    assert.equal((await subscribers.innerText()).replace(/\D/g, ''), '1235');
    await page.evaluate(() => { location.hash = '/channel/7'; });
    await page.waitForFunction(() => document.querySelector('.channel-banner .subscriber-count')?.textContent.replace(/\D/g, '') === '1235');
    await page.reload();
    await page.waitForFunction(() => document.querySelector('.channel-banner .subscriber-count')?.textContent.replace(/\D/g, '') === '1235');
    await page.locator('.channel-banner button[aria-pressed]').click();
    assert.equal((await page.locator('.subscriber-count').innerText()).replace(/\D/g, ''), '1234');
    assert.deepEqual(errors, []);
    console.log('PASS: live descriptions, subscribers, fresh commenter profile, direct reload, truthful errors, retry and text sanitization.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
