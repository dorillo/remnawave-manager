// Interface remains usable while external data is pending, in all three engines.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const engines = require('./.tmp/node_modules/playwright-core');
const root = path.resolve(__dirname, '../src/remnawave_manager/data/disguises');
const sites = [
  ['01-northline', 'Line', '#/authors', 'Авторы', 'Authors'],
  ['02-aster-observatory', 'Aster', '#/history', 'История', 'History'],
  ['03-morrow-coffee', 'Morrow', '#/explore', 'Обзор', 'Explore'],
  ['04-signal-works', 'Spros', '#/saved', 'Сохранённое', 'Saved'],
  ['05-field-notes', 'Svod', '#/history', 'История чтения', 'Reading history'],
];
(async () => {
  for (const engine of (process.env.UI_ENGINES || 'chromium,firefox,webkit').split(',')) {
    const browser = await engines[engine].launch(engine === 'chromium' ? {
      executablePath: process.env.UI_BROWSER || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    } : {});
    try {
      for (const [site, brand, hash, ru, en] of sites) {
        const context = await browser.newContext({ locale: 'ru-RU', colorScheme: 'light', viewport: { width: 1440, height: 1000 } });
        const page = await context.newPage();
        const errors = [];
        page.on('pageerror', error => {
          // WebKit reports canceled, deliberately pending API requests on reload as access-control errors.
          if (engine === 'webkit' && /ui\.test\/_[^ ]+.*due to access control checks/.test(error.message)) return;
          errors.push(error.message);
        });
        let release;
        const pending = new Promise(resolve => { release = resolve; });
        await context.route('**/*', async route => {
          const u = new URL(route.request().url());
          if (u.pathname.startsWith('/_') || u.origin !== 'https://ui.test') {
            await pending;
            return route.fulfill({ status: 503, body: '{}' }).catch(() => {});
          }
          const filename = path.resolve(root, site, '.' + (u.pathname === '/' ? '/index.html' : u.pathname));
          // Shared imports resolve one level above the site directory.
          const file = u.pathname.startsWith('/shared/') ? path.join(root, u.pathname) : filename;
          try {
            await route.fulfill({ body: await fs.readFile(file), contentType: file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.svg') ? 'image/svg+xml' : 'text/html' });
          } catch { await route.fulfill({ status: 404, body: '' }); }
        });
        try {
          await page.goto('https://ui.test/', { waitUntil: 'domcontentloaded' });
          await page.locator('header').first().waitFor();
          const themeValue = () => page.locator('html').getAttribute('data-theme');
          assert.equal(await themeValue(), brand === 'Svod' ? 'system' : 'light');
          const lightScheme = await page.locator('html').evaluate(node => getComputedStyle(node).colorScheme);
          await page.emulateMedia({ colorScheme: 'dark' });
          await page.waitForFunction(light => getComputedStyle(document.documentElement).colorScheme !== light, lightScheme);
          await page.emulateMedia({ colorScheme: 'light' });
          await page.waitForFunction(light => getComputedStyle(document.documentElement).colorScheme === light, lightScheme);
          if (['Aster', 'Spros'].includes(brand))
            await page.waitForFunction(brand => document.title === `${brand} — Главная`, brand);
          assert.equal(await page.getByRole('button', { name: /Язык интерфейса|^Язык$/ }).first().innerText(), 'EN');
          assert.ok(await page.locator('nav').count(), `${brand}: navigation before API response`);
          const initialTitle = { Line: 'Обзор', Aster: 'Главная', Morrow: 'Для вас', Spros: 'Главная', Svod: 'Главная' }[brand];
          await page.waitForFunction(([title, brand]) => document.title === (['Aster', 'Spros', 'Svod'].includes(brand) ? `${brand} — ${title}` : `${title} — ${brand}`), [initialTitle, brand]);
          const controls = { Line: '.interface-language button', Morrow: '.topbar-actions .language-button', Spros: '.head-actions .icon-btn', Svod: '.header-actions .quiet' }[brand];
          if (controls) {
            const buttons = page.locator(controls);
            assert.equal(await buttons.count(), 2);
            for (const button of await buttons.all()) {
              const rect = await button.boundingBox();
              assert.equal(rect.width, rect.height, `${brand}: square control`);
              const background = await button.evaluate(node => getComputedStyle(node).backgroundColor);
              await button.hover();
              await page.waitForFunction(([selector, index, old]) => getComputedStyle(document.querySelectorAll(selector)[index]).backgroundColor !== old, [controls, await button.evaluate(node => [...node.parentNode.children].filter(n => n.matches('button')).indexOf(node)), background]);
              await page.mouse.move(0, 0);
            }
            if (brand === 'Line') assert.equal(await page.locator('.network-label').count(), 0);
            if (['Line', 'Spros'].includes(brand)) {
              for (const button of await buttons.all())
                assert.equal(await button.evaluate(node => getComputedStyle(node).borderTopWidth), '0px');
            }
          }

          assert.doesNotMatch(await page.locator('body').innerText(), /Loading Line|Loading Aster/);
          await page.evaluate(hash => { location.hash = hash; }, hash);
          await page.waitForFunction(([title, brand]) => document.title === (['Aster', 'Spros', 'Svod'].includes(brand) ? `${brand} — ${title}` : `${title} — ${brand}`), [ru, brand]);
          await page.getByRole('button', { name: /Язык интерфейса|^Язык$/ }).first().click();
          await page.waitForFunction(([title, brand]) => document.title === (['Aster', 'Spros', 'Svod'].includes(brand) ? `${brand} — ${title}` : `${title} — ${brand}`), [en, brand]);
          assert.equal(await page.getByRole('button', { name: /Interface language|^Language$/ }).first().innerText(), 'RU');
          if (['Aster', 'Spros'].includes(brand)) {
            await page.evaluate(() => { location.hash = '#/home'; });
            await page.waitForFunction(brand => document.title === `${brand} — Home`, brand);
            await page.evaluate(hash => { location.hash = hash; }, hash);
            await page.waitForFunction(([title, brand]) => document.title === (['Aster', 'Spros', 'Svod'].includes(brand) ? `${brand} — ${title}` : `${title} — ${brand}`), [en, brand]);
          }
          assert.ok((await page.locator('body').innerText()).includes(brand));
          if (brand === 'Svod') {
            await page.getByRole('button', { name: 'Appearance', exact: true }).click();
            await page.getByLabel('Theme', { exact: true }).selectOption('dark');
            await page.getByRole('dialog').getByRole('button', { name: 'Close', exact: true }).click();
          } else {
            // Exercise the manual override before a fresh visit.
            const control = page.getByRole('button', { name: /Switch theme|^Appearance$/ }).first();
            if (await page.locator('html').getAttribute('data-theme') === 'dark') await control.click();
            await control.click();
          }
          assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
          await page.reload({ waitUntil: 'domcontentloaded' });
          await page.locator('header').first().waitFor();
          assert.equal(await page.locator('html').getAttribute('lang'), 'en');
          assert.equal(await themeValue(), brand === 'Svod' ? 'system' : 'light');
          await page.emulateMedia({ colorScheme: 'dark' });
          await page.waitForFunction(light => getComputedStyle(document.documentElement).colorScheme !== light, lightScheme);
          for (const width of [320, 390, 768, 1440]) {
            await page.setViewportSize({ width, height: 1000 });
            assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `${engine} ${brand} overflow at ${width}`);
          }
          await page.screenshot({ path: `/tmp/ui-${engine}-${brand}.png` });
          if (brand === 'Morrow') {
            // Isolate sizing from video codecs and availability of the CDN.
            await page.evaluate(() => {
              const main = document.querySelector('main');
              main.className = 'main watching';
              main.innerHTML = '<div class="feed"><div class="video-slide"><div class="video-card"><div class="video-canvas"><div class="video-surface"><video></video></div></div><div class="video-actions"><button>Like</button></div></div></div></div>';
            });
            for (const width of [320, 768, 1024, 1440, 1920]) {
              await page.setViewportSize({ width, height: 1000 });
              const rect = await page.locator('.video-canvas').boundingBox();
              assert.ok(rect.width >= (width === 768 ? 220 : 300), `${engine}: collapsed video at ${width}: ${rect.width}`);
              assert.ok(rect.x >= 0 && rect.x + rect.width <= width, `${engine}: video outside viewport`);
              const video = await page.locator('video').boundingBox();
              assert.ok(Math.abs(video.width - rect.width) < 1);
            }
          }
          assert.deepEqual(errors, [], `${engine} ${brand} script errors`);
          console.log(`${engine} ${brand}: deferred API, title, language, theme, persistence, layout OK`);
        } finally {
          release();
          await context.close();
        }
      }
    } finally { await browser.close(); }
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
