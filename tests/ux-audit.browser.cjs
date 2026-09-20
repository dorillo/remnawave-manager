// The shell remains usable before either bundled data or external APIs respond.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const engines = require('./.tmp/node_modules/playwright-core');
const root = path.resolve(__dirname, '../src/remnawave_manager/data/disguises');
const sites = [
  ['01-northline', 'Line', '#/me', 'Профиль'],
  ['02-aster-observatory', 'Aster', '#/profile', 'Профиль'],
  ['03-morrow-coffee', 'Morrow', '#/explore', 'Обзор'],
  ['04-signal-works', 'Spros', '#/local', 'Мои вопросы'],
  ['05-field-notes', 'Svod', '#/about', 'О проекте'],
  ['06-loop-archive', 'Loop', '#/profile', 'Профиль'],
  ['07-fokus-news', 'Fokus', '#/profile', 'Профиль'],
];
(async () => {
  const engine = process.env.UI_ENGINE || 'chromium';
  const browser = await engines[engine].launch(engine === 'chromium' ? { executablePath: process.env.UI_BROWSER || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' } : {});
  try {
    for (const [site, brand, hash, section] of sites) {
      const context = await browser.newContext({ locale: 'ru-RU', viewport: { width: 1440, height: 1000 } });
      const page = await context.newPage(), errors = [];
      await page.clock.setFixedTime(new Date('2037-07-15T12:00:00Z'));
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
        await page.waitForFunction(brand => document.title.startsWith(brand + ' — '), brand);
        assert.ok(await page.locator('nav').count(), brand + ': navigation before data');
        if (['Line', 'Spros'].includes(brand)) await page.getByText(`© 2037 ${brand}`, { exact: true }).waitFor();
        await page.evaluate(hash => { location.hash = hash; }, hash);
        await page.waitForFunction(() => !!document.querySelector('main')?.textContent.trim());
        await page.waitForFunction(([brand, section]) => document.title === `${brand} — ${section}`, [brand, section]);
        for (const theme of ['light', 'dark']) {
          await page.evaluate(theme => { document.documentElement.dataset.theme = theme; }, theme);
          for (const width of [320, 390, 768, 1024, 1440, 1920]) {
            await page.setViewportSize({ width, height: 900 });
            if (width <= 768 && ['Line', 'Aster'].includes(brand)) {
              const search = await page.locator(brand === 'Line' ? '.global-search' : '.search').boundingBox();
              const logo = await page.locator(brand === 'Line' ? '.brand' : '.top-brand').boundingBox();
              if (width <= 720 || brand === 'Aster') {
                assert.ok(search.y >= logo.y + logo.height, brand + ': mobile search has its own row');
                assert.ok(search.width > width * .85, brand + ': mobile search uses full width');
              }
            }
            const overflow = await page.evaluate(() => document.documentElement.scrollWidth > innerWidth);
            assert.equal(overflow, false, `${brand}: overflow at ${width}/${theme}`);
          }
          const link = page.locator('nav a:visible').first();
          await link.hover();
          assert.equal(await link.evaluate(node => getComputedStyle(node).textDecorationLine), 'none');
          if (brand === 'Line') {
            const surface = await page.locator('.guest-prompt').evaluate(node => getComputedStyle(node).backgroundColor);
            const rgb = surface.match(/\d+/g).slice(0, 3).map(Number);
            assert.equal(rgb.every(value => value < 80), theme === 'dark', 'Line empty state uses the current palette');
          }
          await page.screenshot({ path: `/tmp/ux-${engine}-${brand}-${theme}.png` });
        }
        if (brand === 'Morrow') {
          await page.waitForFunction(() => !!document.querySelector('.load-button .loading-spinner'));
          const spinner = page.locator('.load-button .loading-spinner');
          for (const width of [320, 1440]) {
            await page.setViewportSize({ width, height: 900 });
            const size = await spinner.evaluate(node => ({ width: node.offsetWidth, height: node.offsetHeight }));
            assert.ok(size.width >= 16 && size.height >= 16, 'Morrow: the loading spinner keeps its size in a button');
          }
        }
        // Multiline titles must not vertically centre the close control.
        for (const width of [320, 1440]) {
          await page.setViewportSize({ width, height: 900 });
          const position = await page.evaluate(brand => {
            const classes = { Line: 'modal-header', Aster: 'dialog-head', Morrow: '', Spros: 'modal-head', Svod: 'dialog-head', Loop: 'dialog-heading', Fokus: 'dialog-top' };
            const dialog = document.createElement('dialog');
            if (['Line', 'Spros'].includes(brand)) dialog.className = 'modal';
            const header = document.createElement('header'); header.className = classes[brand];
            const title = document.createElement('h2'); title.textContent = 'Удалить аккаунт вместе с библиотекой и историей? Это действие нельзя отменить.';
            const close = document.createElement('button'); close.textContent = '×';
            if (brand === 'Fokus') { header.append(close); dialog.append(header, title); }
            else { header.append(title, close); dialog.append(header); }
            document.body.append(dialog); dialog.showModal();
            const result = { close: close.getBoundingClientRect().top, header: header.getBoundingClientRect().top, padding: parseFloat(getComputedStyle(header).paddingTop) };
            dialog.close(); dialog.remove(); return result;
          }, brand);
          assert.ok(Math.abs(position.close - position.header - position.padding) < 2, `${brand}: top-aligned close button at ${width}`);
        }
        const indicator = await page.evaluate(async () => {
          const { loadingNode } = await import('/shared/feedback.js');
          document.body.append(loadingNode('Загрузка…'));
          return getComputedStyle(document.querySelector('.ui-loading:last-child .ui-spinner')).animationName;
        });
        assert.equal(indicator, 'ui-spin');
        await page.emulateMedia({ reducedMotion: 'reduce' });
        assert.equal(await page.locator('.ui-loading').last().locator('.ui-spinner').evaluate(node => getComputedStyle(node).animationName), 'none');
        release();
        assert.deepEqual(errors, [], brand + ': no script errors');
        console.log(`${brand}: shell before data, themes, hover, 320–1920 px, reduced motion OK`);
      } finally { release(); await context.close(); }
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
