/* In-memory static origin, no preview server. MORROW_LIVE=1 probes Prexzy. */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { chromium } = require('./.tmp/node_modules/playwright-core');
const root = path.resolve(
  __dirname,
  '../src/remnawave_manager/data/disguises/03-morrow-coffee',
);
const origin = 'https://morrow.test';
const policy = execFileSync(
  'python',
  ['-c', 'from remnawave_manager.site_policy import NODE_CSP; print(NODE_CSP)'],
  {
    env: { ...process.env, PYTHONPATH: path.resolve(__dirname, '../src') },
    encoding: 'utf8',
  },
).trim();
let browser;
(async () => {
  browser = await chromium.launch({
    executablePath:
      process.env.MORROW_BROWSER ||
      'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: true,
  });
  const context = await browser.newContext({
    locale: 'ru-RU',
    viewport: { width: 1440, height: 1000 },
    acceptDownloads: true,
  });
  await context.route(`${origin}/**`, async (route) => {
    const url = new URL(route.request().url());
    const name = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    const resolved = path.resolve(root, name);
    if (!resolved.startsWith(root + path.sep)) return route.abort();
    try {
      await route.fulfill({
        body: await fs.readFile(resolved),
        contentType: name.endsWith('.js')
          ? 'text/javascript'
          : name.endsWith('.css')
            ? 'text/css'
            : 'text/html',
        headers: { 'Content-Security-Policy': policy },
      });
    } catch {
      await route.fulfill({ status: 404, body: 'Not found' });
    }
  });
  let mode = 'ok';
  let calls = [];
  if (!process.env.MORROW_LIVE)
    await context.route('https://prexzyapis.com/**', async (route) => {
      if (route.request().method() === 'OPTIONS')
        return route.fulfill({
          headers: {
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST',
            'Access-Control-Allow-Headers': '*',
          },
        });
      calls.push(new URLSearchParams(route.request().postData()).get('prompt'));
      if (mode === 'delay')
        await new Promise((resolve) => setTimeout(resolve, 1200));
      if (mode === 'network') return route.abort();
      const status = mode === 'rate' ? 429 : 200;
      await route.fulfill({
        status,
        headers: {
          'Access-Control-Allow-Origin': '*',
          'Retry-After': '1',
          'Access-Control-Expose-Headers': 'Retry-After',
        },
        contentType: 'application/json',
        body: JSON.stringify({
          status: true,
          result: {
            text:
              mode === 'empty'
                ? []
                : [
                    '## Ответ\n\n**Полезный текст**\n\n<script>window.injected=1</script>\n\n[опасно](javascript:alert(1))\n\n```js\nconst n = 1;\n```\n\n| A | B |\n| --- | --- |\n| 1 | 2 |',
                  ],
          },
        }),
      });
    });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', (error) => {
    errors.push(error.message);
    console.error('PAGE:', error.message);
  });
  await page.addInitScript(() => {
    window.cspErrors = [];
    document.addEventListener('securitypolicyviolation', (e) =>
      window.cspErrors.push(e.violatedDirective),
    );
  });
  await page.goto(origin);
  if (process.env.MORROW_LIVE) {
    const result = await page.evaluate(async () => {
      const { generate } = await import('./morrow-ai.js');
      return generate({
        prompt:
          'User: My name is Vera. Assistant: Hello. User: What is my name? Reply with name only.',
        signal: new AbortController().signal,
      });
    });
    assert.match(result, /Vera/i);
    console.log('Live Prexzy POST, CORS and context: PASS');
    return;
  }
  // Guest appearance preferences survive reload without carrying profile data.
  await page
    .locator('.rail')
    .getByRole('button', { name: 'Настройки', exact: true })
    .click();
  await page.getByLabel('Язык интерфейса').selectOption('en');
  await page.getByLabel('Interface language').waitFor();
  await page.reload();
  await page.getByLabel('Interface language').waitFor();
  assert.equal(await page.getByLabel('Interface language').inputValue(), 'en');
  await page.getByLabel('Interface language').selectOption('ru');
  await page.getByLabel('Язык интерфейса').waitFor();
  async function signup(login) {
    await page
      .locator('.rail-bottom')
      .getByRole('button', { name: 'Войти', exact: true })
      .click();
    await page
      .getByRole('dialog')
      .getByRole('button', { name: 'Создать аккаунт' })
      .click();
    await page.locator('dialog input[name=login]').fill(login);
    await page.locator('dialog input[name=password]').fill('password123');
    await page
      .getByRole('dialog')
      .getByRole('button', { name: 'Создать аккаунт' })
      .click();
    await page.locator('dialog').waitFor({ state: 'detached' });
  }
  async function send(text) {
    await page.locator('#prompt').fill(text);
    await page.getByRole('button', { name: 'Отправить', exact: true }).click();
  }
  async function navigate(name) {
    await page
      .locator('.rail')
      .getByRole('button', { name, exact: true })
      .click();
  }
  await signup('alice');
  await send('Мой проект называется Atlas.');
  await page.locator('.message.assistant').waitFor();
  assert.equal(await page.evaluate(() => window.injected), undefined);
  assert.equal(
    await page
      .locator('.message script, .message a[href^="javascript:"]')
      .count(),
    0,
  );
  assert.equal(await page.locator('.message table').count(), 1);
  await send('Как называется мой проект?');
  await page.waitForFunction(
    () => document.querySelectorAll('.message.assistant').length === 2,
  );
  assert.ok(calls[1].includes('Atlas') && calls[1].includes('assistant'));
  await page.reload();
  await page.locator('.message.assistant').first().waitFor();
  assert.equal(await page.locator('.message.assistant').count(), 2);
  await page.getByRole('button', { name: 'Изменить запрос' }).last().click();
  await page.locator('dialog textarea').fill('Изменённый вопрос');
  await page
    .locator('dialog')
    .getByRole('button', { name: 'Сохранить', exact: true })
    .click();
  await page.locator('dialog').waitFor({ state: 'detached' });
  assert.ok(calls.at(-1).includes('Изменённый вопрос'));
  await navigate('Проекты');
  await page.getByRole('button', { name: 'Новый проект' }).click();
  await page
    .getByRole('dialog')
    .getByLabel('Название', { exact: true })
    .fill('Atlas');
  await page
    .getByRole('dialog')
    .getByLabel('Инструкция для AI')
    .fill('Answer in short paragraphs.');
  await page
    .getByRole('dialog')
    .getByRole('button', { name: 'Сохранить' })
    .click();
  await page.getByRole('button', { name: 'Открыть проект' }).click();
  await page.getByLabel('Загрузить файлы', { exact: true }).setInputFiles({
    name: 'notes.md',
    mimeType: 'text/markdown',
    buffer: Buffer.from('Unique file marker <img src=x onerror=alert(1)>'),
  });
  await page.getByRole('heading', { name: 'notes.md' }).waitFor();
  await page
    .locator('main')
    .getByRole('button', { name: 'Новый диалог', exact: true })
    .click();
  await page.getByRole('button', { name: 'Выбрать файлы' }).click();
  await page.getByRole('dialog').getByLabel('notes.md').check();
  await page
    .getByRole('dialog')
    .getByRole('button', { name: 'Сохранить' })
    .click();
  await send('Объясни файл');
  await page.locator('.message.assistant').waitFor();
  assert.ok(calls.at(-1).includes('Unique file marker'));
  assert.ok(calls.at(-1).includes('Answer in short paragraphs.'));
  mode = 'delay';
  await send('Запрос для отмены');
  await page.getByRole('button', { name: 'Остановить', exact: true }).click();
  await page.waitForTimeout(1400);
  assert.equal(await page.locator('.message.assistant').count(), 1);
  mode = 'empty';
  await page
    .getByRole('button', { name: 'Повторить ответ', exact: true })
    .last()
    .click();
  await page.getByRole('status').filter({ hasText: 'пустой' }).waitFor();
  mode = 'rate';
  await page
    .getByRole('button', { name: 'Повторить ответ', exact: true })
    .last()
    .click();
  await page
    .getByRole('status')
    .filter({ hasText: 'Лимит запросов' })
    .waitFor();
  await page.waitForTimeout(1100);
  mode = 'network';
  await page
    .getByRole('button', { name: 'Повторить ответ', exact: true })
    .last()
    .click();
  await page
    .getByRole('status')
    .filter({ hasText: 'Нет соединения' })
    .waitFor();
  mode = 'ok';
  await page
    .getByRole('button', { name: 'Повторить ответ', exact: true })
    .last()
    .click();
  await page.waitForFunction(
    () => document.querySelectorAll('.message.assistant').length === 2,
  );
  await navigate('Профиль');
  const backupPromise = page.waitForEvent('download');
  await page
    .getByRole('button', { name: 'Экспорт данных', exact: true })
    .click();
  const backup = JSON.parse(
    await fs.readFile(await (await backupPromise).path(), 'utf8'),
  );
  assert.equal(backup.workspace.projects[0].name, 'Atlas');
  assert.equal(JSON.stringify(backup).includes('password123'), false);
  await page.getByRole('button', { name: 'Выйти', exact: true }).click();
  await page
    .getByRole('dialog')
    .getByRole('button', { name: 'Выйти', exact: true })
    .click();
  await signup('bob');
  assert.equal(await page.locator('.history-item').count(), 0);
  await navigate('Файлы');
  assert.equal(await page.locator('.file-card').count(), 0);
  await navigate('Профиль');
  await page.getByLabel('Импорт данных', { exact: true }).setInputFiles({
    name: 'backup.json',
    mimeType: 'application/json',
    buffer: Buffer.from(JSON.stringify(backup)),
  });
  await page
    .getByRole('dialog')
    .getByRole('button', { name: 'Импорт данных' })
    .click();
  await page.locator('dialog').waitFor({ state: 'detached' });
  assert.equal(await page.locator('.history-item').count(), 2);
  await navigate('Диалоги');
  await page.locator('.history-item').first().click();
  // A late response belongs to the originating chat, even after navigation.
  mode = 'delay';
  const previousReplies = await page.locator('.message.assistant').count();
  await send('Navigation isolation test');
  await page.getByRole('button', { name: 'Остановить', exact: true }).waitFor();
  await page
    .locator('.sidebar')
    .getByRole('button', { name: 'Новый диалог', exact: true })
    .click();
  await page.waitForTimeout(1400);
  assert.equal(await page.locator('.message.assistant').count(), 0);
  await page.goBack();
  await page.waitForFunction(
    (n) => document.querySelectorAll('.message.assistant').length === n,
    previousReplies + 1,
  );
  mode = 'ok';
  // Fail only the assistant write, after the user message has committed.
  await page.evaluate(() => {
    window.originalPut = IDBObjectStore.prototype.put;
    IDBObjectStore.prototype.put = function (value, ...rest) {
      if (
        this.name === 'workspaces' &&
        value.data.chats.some(
          (c) =>
            c.messages.some((m) => m.text === 'Unsaved answer test') &&
            c.messages.at(-1)?.role === 'assistant',
        )
      )
        throw new DOMException('full', 'QuotaExceededError');
      return window.originalPut.call(this, value, ...rest);
    };
  });
  await send('Unsaved answer test');
  await page.locator('.message.assistant .error').waitFor();
  await page.evaluate(() => {
    IDBObjectStore.prototype.put = window.originalPut;
    delete window.originalPut;
  });
  await page
    .locator('.message.assistant')
    .last()
    .getByRole('button', { name: 'Сохранить', exact: true })
    .click();
  await page
    .locator('.message.assistant .error')
    .waitFor({ state: 'detached' });
  // Drafts persist before sending, including the first message of a new chat.
  await page
    .locator('.sidebar')
    .getByRole('button', { name: 'Новый диалог', exact: true })
    .click();
  await page.locator('#prompt').fill('Unsent draft');
  await page.waitForTimeout(600);
  await page.reload();
  await page.waitForFunction(
    () => document.querySelector('#prompt')?.value === 'Unsent draft',
  );
  const fileChecks = await page.evaluate(async () => {
    const { readFile } = await import('./morrow-files.js');
    const { buildContext } = await import('./morrow-ai.js');
    const { blankWorkspace, newChat, parseImport } = await import(
      './morrow-store.js'
    );
    const failures = [];
    for (const file of [
      new File(['x'], 'x.html'),
      new File(['x'.repeat(262145)], 'x.txt'),
      new File([new Uint8Array([255])], 'x.txt'),
    ]) {
      try {
        await readFile(file);
        failures.push(false);
      } catch {
        failures.push(true);
      }
    }
    const chat = newChat();
    chat.messages.push({ role: 'user', text: 'x'.repeat(25000), files: [] });
    try {
      buildContext(chat, null, blankWorkspace().settings);
      failures.push(false);
    } catch (e) {
      failures.push(e.message === 'contextLimit');
    }
    for (const value of ['{', 'null']) {
      try {
        parseImport(value);
        failures.push(false);
      } catch (e) {
        failures.push(e.message === 'invalidData');
      }
    }
    return failures;
  });
  assert.ok(fileChecks.every(Boolean));
  const validation = await page.evaluate(async () => {
    const { validateWorkspace, blankWorkspace, WorkspaceStore } = await import(
      './morrow-store.js'
    );
    const result = [];
    for (const mutation of [
      (d) => {
        d.profile.avatar = 'javascript:alert(1)';
      },
      (d) => {
        d.projects = [
          { id: '__proto__', name: 'x', description: '', instruction: '' },
        ];
      },
      (d) => {
        d.files = Array(101).fill({});
      },
    ]) {
      const data = blankWorkspace();
      mutation(data);
      try {
        validateWorkspace(data);
        result.push(false);
      } catch {
        result.push(true);
      }
    }
    const id = sessionStorage.getItem('morrow:session:v1');
    const a = new WorkspaceStore(),
      b = new WorkspaceStore();
    await a.load(id);
    await b.load(id);
    await a.mutate((d) => {
      d.profile.name = 'Bob';
    });
    try {
      await b.mutate((d) => {
        d.profile.name = 'Stale';
      });
      result.push(false);
    } catch (e) {
      result.push(e.message === 'conflict');
    }
    const old = IDBObjectStore.prototype.put;
    IDBObjectStore.prototype.put = function () {
      throw new DOMException('full', 'QuotaExceededError');
    };
    try {
      await a.mutate((d) => {
        d.profile.name = 'Lost';
      });
      result.push(false);
    } catch {
      result.push(a.data.profile.name === 'Bob');
    } finally {
      IDBObjectStore.prototype.put = old;
    }
    return result;
  });
  assert.ok(validation.every(Boolean), JSON.stringify(validation));
  await page.reload();
  await navigate('Настройки');
  await page.getByLabel('Язык интерфейса').selectOption('en');
  await page.getByLabel('Appearance').selectOption('dark');
  await page.waitForFunction(
    () => document.documentElement.dataset.theme === 'dark',
  );
  assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
  for (const width of [320, 360, 390, 720, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    for (const name of ['Chats', 'Projects', 'Files', 'Profile', 'Settings']) {
      await navigate(name);
      if (name === 'Chats') {
        if (width <= 760)
          await page.getByRole('button', { name: 'Menu', exact: true }).click();
        await page
          .locator('.history-item')
          .filter({ hasText: 'Объясни файл' })
          .click();
      }
      assert.ok(
        await page.evaluate(
          () =>
            document.documentElement.scrollWidth <= innerWidth + 1 &&
            document.querySelector('main').scrollWidth <=
              document.querySelector('main').clientWidth + 1,
        ),
        `${name} overflow at ${width}`,
      );
    }
  }
  await page.screenshot({
    path: path.join(__dirname, '.tmp/morrow-desktop.png'),
  });
  await navigate('Chats');
  await page
    .locator('.history-item')
    .filter({ hasText: 'Объясни файл' })
    .click();
  await page.screenshot({
    path: path.join(__dirname, '.tmp/morrow-chat-desktop.png'),
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: path.join(__dirname, '.tmp/morrow-chat-mobile.png'),
  });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await navigate('Settings');
  await page.getByLabel('Interface language').selectOption('ru');
  await page.getByLabel('Язык интерфейса').waitFor();
  await navigate('Диалоги');
  await page
    .locator('.history-item')
    .filter({ hasText: 'Объясни файл' })
    .click();
  mode = 'delay';
  await send('Signout race test');
  await page.getByRole('button', { name: 'Остановить', exact: true }).waitFor();
  await navigate('Профиль');
  await page.getByRole('button', { name: 'Выйти', exact: true }).click();
  await page
    .getByRole('dialog')
    .getByRole('button', { name: 'Выйти', exact: true })
    .click();
  await signup('charlie');
  await page.waitForTimeout(1400);
  assert.equal(await page.locator('.message.assistant').count(), 0);
  assert.equal(await page.locator('.history-item').count(), 0);
  await navigate('Профиль');
  await page
    .getByRole('button', { name: 'Удалить аккаунт', exact: true })
    .click();
  await page
    .getByRole('dialog')
    .getByLabel('Удалить аккаунт', { exact: true })
    .fill('charlie');
  await page
    .getByRole('dialog')
    .getByRole('button', { name: 'Сохранить', exact: true })
    .click();
  await page.locator('dialog').waitFor({ state: 'detached' });
  await page
    .locator('.rail-bottom')
    .getByRole('button', { name: 'Войти', exact: true })
    .click();
  await page.locator('dialog input[name=login]').fill('alice');
  await page.locator('dialog input[name=password]').fill('wrongpass123');
  await page
    .getByRole('dialog')
    .getByRole('button', { name: 'Войти', exact: true })
    .click();
  await page
    .getByRole('dialog')
    .getByText('Неверный логин или пароль.')
    .waitFor();
  await page.locator('dialog input[name=password]').fill('password123');
  await page
    .getByRole('dialog')
    .getByRole('button', { name: 'Войти', exact: true })
    .click();
  await page.locator('dialog').waitFor({ state: 'detached' });
  assert.equal(await page.locator('.history-item').count(), 2);
  const deleted = await page.evaluate(async () =>
    (await import('./morrow-db.js')).findAccount('charlie'),
  );
  assert.equal(deleted, undefined);
  // Accelerate only the provider deadline, leaving browser and test clocks intact.
  const timeoutResult = await page.evaluate(async () => {
    const { generate } = await import('./morrow-ai.js');
    const original = window.setTimeout;
    window.setTimeout = (fn, delay, ...args) =>
      original(fn, delay === 60000 ? 15 : delay, ...args);
    try {
      await generate({
        prompt: 'Timeout test',
        signal: new AbortController().signal,
      });
      return 'unexpected';
    } catch (e) {
      return e.message;
    } finally {
      window.setTimeout = original;
    }
  });
  assert.equal(timeoutResult, 'timeout');
  assert.deepEqual(errors, []);
  assert.deepEqual(await page.evaluate(() => window.cspErrors), []);
  console.log(
    'Morrow browser: account isolation, chats, context, files, projects, import/export, cancellation, failures, storage, XSS, i18n, themes, responsive: PASS',
  );
})()
  .catch(async (error) => {
    console.error(error);
    const page = browser?.contexts()[0]?.pages()[0];
    if (page) {
      console.error('Notice:', await page.locator('#notice').textContent());
      await page.screenshot({
        path: path.join(__dirname, '.tmp/morrow-failure.png'),
      });
    }
    process.exitCode = 1;
  })
  .finally(async () => {
    await browser?.close();
  });
