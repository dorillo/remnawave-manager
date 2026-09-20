const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('./.tmp/node_modules/playwright-core');
const root = path.resolve(__dirname, '../src/remnawave_manager/data/disguises');

(async () => {
  const browser = await chromium.launch({
    executablePath: process.env.FOKUS_BROWSER || undefined,
    headless: true,
  });
  try {
    for (const scenario of [
      { system: 'dark', expected: 'dark' },
      { system: 'light', expected: 'light' },
      { system: 'dark', saved: 'light', expected: 'light' },
      { system: 'light', saved: 'dark', expected: 'dark' },
      { system: 'dark', saved: 'invalid', expected: 'dark' },
      { system: 'dark', blocked: true, expected: 'dark' },
    ]) {
      const context = await browser.newContext({ colorScheme: scenario.system });
      try {
        await context.addInitScript(({ saved, blocked }) => {
          if (blocked) {
            Object.defineProperty(window, 'localStorage', {
              get() { throw new DOMException('Storage disabled', 'SecurityError'); },
            });
          } else if (saved && !sessionStorage.getItem('theme-test-initialized')) {
            localStorage.setItem('fokus:theme', saved);
            sessionStorage.setItem('theme-test-initialized', 'yes');
          }
        }, scenario);
        await context.route('**/*', async route => {
          const url = new URL(route.request().url());
          if (url.origin !== 'https://fokus.test' || url.pathname.startsWith('/_')) {
            return route.fulfill({ status: 503, body: '' });
          }
          const file = url.pathname.startsWith('/shared/')
            ? path.join(root, url.pathname)
            : path.join(root, '07-fokus-news', url.pathname === '/' ? 'index.html' : url.pathname);
          const contentType = file.endsWith('.js') ? 'text/javascript' : file.endsWith('.css') ? 'text/css' : file.endsWith('.svg') ? 'image/svg+xml' : 'text/html';
          await route.fulfill({ body: await fs.readFile(file), contentType });
        });
        const page = await context.newPage();
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        const expectTheme = async expected => {
          await page.waitForFunction(value => document.documentElement.dataset.theme === value, expected);
          assert.equal(await page.evaluate(() => getComputedStyle(document.documentElement).colorScheme), expected);
        };
        await page.goto('https://fokus.test/');
        await expectTheme(scenario.expected);
        const oppositeSystem = scenario.system === 'dark' ? 'light' : 'dark';
        const manual = scenario.saved === 'dark' || scenario.saved === 'light';
        await page.emulateMedia({ colorScheme: oppositeSystem });
        const current = manual ? scenario.saved : oppositeSystem;
        await expectTheme(current);
        await page.locator('[data-action="theme"]').click();
        const chosen = current === 'dark' ? 'light' : 'dark';
        await expectTheme(chosen);
        await page.emulateMedia({ colorScheme: current });
        await page.waitForTimeout(100);
        await expectTheme(chosen);
        if (!scenario.blocked) {
          assert.equal(await page.evaluate(() => localStorage.getItem('fokus:theme')), chosen);
          await page.reload();
          await expectTheme(chosen);
        }
        assert.deepEqual(errors, []);
      } finally {
        await context.close();
      }
    }
    console.log('Fokus theme: all 6 browser scenarios passed');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
