// The shell remains usable before either bundled data or external APIs respond.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const engines = require('./.tmp/node_modules/playwright-core');
const root = path.resolve(__dirname, '../src/remnawave_manager/data/disguises');
const sites = [
 ['01-northline', ['me','me?tab=likes','me?tab=saved','me?tab=drafts','me?tab=reposts','authors?tab=selected','feed?tab=following']],
 ['02-aster-observatory', ['profile','subscriptions','history','likes','later']],
 ['03-morrow-coffee', ['profile','following','profile?tab=likes','profile?tab=saved','profile?tab=history','profile?tab=myVideos']],
 ['04-signal-works', ['profile','local','saved','profile/answers','profile/notifications']],
 ['05-field-notes', ['library','history']],
 ['06-loop-archive', ['profile','likes','collections','uploads']],
 ['07-fokus-news', ['profile','saved','history','profile/ownComments','profile/ownReactions']],
];
(async () => {
  const engine = process.env.UI_ENGINE || 'chromium';
  const browser = await engines[engine].launch(engine === 'chromium' ? { executablePath: process.env.UI_BROWSER || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' } : {});
  try {
    for (const [site, routes] of sites) {
      const context = await browser.newContext({ locale: process.env.GUEST_LOCALE || 'ru-RU', viewport: { width: 1440, height: 1000 } });
      await context.addInitScript(english => {
        if (!english) return;
        localStorage.setItem('svod:preferences:v1', JSON.stringify({ lang: 'en' }));
        localStorage.setItem('loop:lang', 'en');
        localStorage.setItem('fokus:lang', 'en');
      }, (process.env.GUEST_LOCALE || '').startsWith('en'));
      const page = await context.newPage(), errors = [];
      page.on('pageerror', error => errors.push(error.message));
      let release;
      const pending = new Promise(resolve => { release = resolve; });
      await context.route('**/*', async route => {
        const url = new URL(route.request().url());
        if (url.pathname.startsWith('/_') || url.pathname.endsWith('.json') || url.origin !== 'https://ux.test') {
          await pending;
          return route.fulfill({ status: 503, body: '{}' }).catch(() => {});
        }
        const file = url.pathname.startsWith('/shared/') ? path.join(root, url.pathname)
          : path.join(root, site, url.pathname === '/' ? 'index.html' : url.pathname);
        try {
          await route.fulfill({ body: await fs.readFile(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.svg') ? 'image/svg+xml' : 'text/html' });
        } catch { await route.fulfill({ status: 404, body: '' }); }
      });
      try {
        await page.goto('https://ux.test/', { waitUntil: 'domcontentloaded' });
        await page.locator('header').first().waitFor();
        for (const route of routes) {
          await page.evaluate(route => { location.hash = '#/' + route; }, route);
          const prompt = page.locator(site === '05-field-notes' ? `.guest-prompt[data-guest-section="${route === 'history' ? 'historyReading' : 'library'}"]` : '.guest-prompt');
          await prompt.waitFor();
          await page.waitForTimeout(70);
          assert.equal(await prompt.count(), 1, site + '/' + route);
          const english = (process.env.GUEST_LOCALE || '').startsWith('en');
          assert.equal(await prompt.locator('button').textContent(), site === '05-field-notes' ? (route === 'library' ? (english ? 'Sign in to build your library' : 'Войти и собрать библиотеку') : (english ? 'Sign in to view your history' : 'Войти и открыть историю')) : english ? 'Sign in or create a profile' : 'Войти или создать профиль', site + '/' + route);
          for (const width of [320, 390, 1440]) {
            await page.setViewportSize({ width, height: 900 });
            assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, site + '/' + route + ' overflow ' + width);
          }
        }
        if (site === '07-fokus-news') {
          const brand = page.locator('a.brand').first();
          const original = await brand.evaluate(node => getComputedStyle(node).color);
          await brand.hover();
          await page.waitForTimeout(220);
          assert.notEqual(await brand.evaluate(node => getComputedStyle(node).color), original);
        }
        await page.locator('.guest-login').click();
        await page.locator('dialog[open], [role=dialog], .modal').first().waitFor();
        if (site === '07-fokus-news') {
          assert.ok(await page.locator('a.brand .brand-logo').first().evaluate(img => img.complete && img.naturalWidth > 0));
        }
        release();
        assert.deepEqual(errors, [], site + ': no script errors');
        console.log(`${site}: ${routes.length} guest routes, login, mobile layout OK`);
      } finally { release(); await context.close(); }
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
