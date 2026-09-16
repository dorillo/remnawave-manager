/* Headless integration checks. All site files are fulfilled in memory: no server is started.
 * npm install --prefix tests/.tmp --no-save --no-package-lock playwright-core@1.63.0
 * node tests/northline.browser.cjs
 * NORTHLINE_BROWSER can point to a Chromium executable on other platforms.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('./.tmp/node_modules/playwright-core');
const root = path.resolve(__dirname, '../src/remnawave_manager/data/disguises');
const origin = 'https://northline.test';
const base = `${origin}/01-northline/`;
const jpg = Buffer.from(
  '/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////2wBDAf//////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPyF//9oADAMBAAIAAwAAABD/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAEDAQE/EAf/xAAUEQEAAAAAAAAAAAAAAAAAAAAA/9oACAECAQE/EAf/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oACAEBAAE/EAf/2Q==',
  'base64',
);
const png = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl3M2kAAAAASUVORK5CYII=',
  'base64',
);
const account = (id = '1') => ({
  id,
  username: `person${id}`,
  acct: `person${id}@example.social`,
  display_name: ['Анна Светлова', 'Михаил Лесной', 'Ирина Котова'][Number(id) - 1] || `Автор ${id}`,
  avatar_static: 'https://cdn.example.org/avatar.jpg',
  header_static: 'https://cdn.example.org/cover.jpg',
  note: '<p>Город, фотография и маленькие открытия.</p>',
  url: `https://example.social/@person${id}`,
  followers_count: 421,
  following_count: 89,
  statuses_count: 170,
  created_at: '2022-03-14T08:00:00Z',
  fields: [{ name: 'Сайт', value: '<a href="https://example.org/">Портфолио</a>' }],
});
const attachment = (id, type = 'image') => ({
  id: String(id),
  type,
  url: `https://cdn.example.org/${id}.jpg`,
  preview_url: `https://cdn.example.org/${id}-preview.jpg`,
  description: 'Тихий вечер у воды',
  meta: { original: { width: 1200, height: 800 } },
});
const status = (id = '101', author = '1', overrides = {}) => ({
  id,
  uri: `https://example.social/users/${author}/statuses/${id}`,
  url: `https://example.social/@person${author}/${id}`,
  account: account(author),
  content:
    '<p>Сегодня гуляли по набережной. Свет был удивительный.</p><p>Хочется запомнить этот момент. <a class="mention hashtag" href="https://mastodon.social/tags/photography">#<span>photography</span></a></p>',
  created_at: new Date(Date.now() - Number(id) * 1000).toISOString(),
  language: 'ru',
  media_attachments: [attachment(id)],
  favourites_count: 28,
  replies_count: 2,
  reblogs_count: 4,
  tags: [{ name: 'photography', url: 'https://mastodon.social/tags/photography' }],
  ...overrides,
});
let offline = false;
let added = false;
const apiRequests = [];
const errors = [];
const cspErrors = [];
let browser;
(async () => {
  browser = await chromium.launch({
    executablePath:
      process.env.NORTHLINE_BROWSER || 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: true,
  });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    reducedMotion: 'reduce',
    locale: 'ru-RU',
  });
  const page = await context.newPage();
  const cdp = await context.newCDPSession(page);
  await cdp.send('Runtime.enable');
  cdp.on('Runtime.exceptionThrown', (event) =>
    console.error(JSON.stringify(event.exceptionDetails)),
  );
  page.on('pageerror', (error) => {
    errors.push(error.message);
    console.error('PAGE ERROR', error.message);
  });
  page.on('console', (message) => {
    if (/Content Security Policy|violates.*directive/i.test(message.text()))
      cspErrors.push(message.text());
  });
  await context.route('**/*', async (route) => {
    const url = new URL(route.request().url());
    if (url.origin === origin) {
      const resource = decodeURIComponent(
        url.pathname.endsWith('/') ? url.pathname + 'index.html' : url.pathname,
      );
      const file = path.resolve(root, '.' + resource);
      assert(file.startsWith(root + path.sep));
      try {
        const body = await fs.readFile(file);
        const extension = path.extname(file);
        await route.fulfill({
          body,
          contentType:
            {
              '.js': 'text/javascript',
              '.css': 'text/css',
              '.html': 'text/html; charset=utf-8',
              '.jpg': 'image/jpeg',
            }[extension] || 'application/octet-stream',
        });
      } catch {
        await route.fulfill({ status: 404, body: '' });
      }
      return;
    }
    if (url.hostname === 'mastodon.social' && url.pathname.startsWith('/api/')) {
      apiRequests.push(url.pathname + url.search);
      if (process.env.NORTHLINE_LIVE === '1') {
        await route.continue();
        return;
      }
      if (offline) {
        await route.abort();
        return;
      }
      let data;
      if (url.pathname.includes('/timelines/tag/')) {
        data = url.searchParams.has('max_id')
          ? [
              status('80', '3', {
                language: 'en',
                content: '<p>An older story, loaded with pagination.</p>',
              }),
            ]
          : [
              status(),
              status('102', '2', {
                media_attachments: [attachment(102), attachment(103)],
                content: '<p>Зарисовки нового дня.</p>',
                poll: {
                  expired: false,
                  expires_at: new Date(Date.now() + 86400000).toISOString(),
                  votes_count: 7,
                  options: [
                    { title: 'Утро', votes_count: 4 },
                    { title: 'Вечер', votes_count: 3 },
                  ],
                },
              }),
              status('103', '3', {
                language: 'en',
                media_attachments: [],
                content: '<p>A quiet afternoon with a good book.</p>',
                card: {
                  url: 'https://example.org/book',
                  title: 'Книга недели',
                  description: 'Обсуждение прочитанного',
                  image: 'https://cdn.example.org/book.jpg',
                },
              }),
            ];
        if (added && !url.searchParams.has('max_id'))
          data.unshift(status('110', '2', { content: '<p>Свежая запись после обновления.</p>' }));
      } else if (url.pathname.endsWith('/context'))
        data = {
          ancestors: [],
          descendants: [
            status('201', '2', {
              in_reply_to_id: '101',
              media_attachments: [],
              content: '<p>Какой красивый свет!</p>',
            }),
            status('202', '3', {
              in_reply_to_id: '201',
              media_attachments: [],
              content: '<p>Тоже люблю это место.</p>',
            }),
          ],
        };
      else if (/\/statuses\/\d+$/.test(url.pathname)) data = status(url.pathname.split('/').at(-1));
      else if (/\/accounts\/\d+\/statuses$/.test(url.pathname))
        data = url.searchParams.has('pinned')
          ? [
              status('301', url.pathname.split('/')[4], {
                content: '<p>Закреплённая история автора.</p>',
              }),
            ]
          : [
              status('302', url.pathname.split('/')[4], {
                content: '<p>Публикация из профиля, отсутствующая в ленте.</p>',
              }),
            ];
      else if (/\/accounts\/\d+$/.test(url.pathname))
        data = account(url.pathname.split('/').at(-1));
      else if (url.pathname.endsWith('/directory'))
        data = [account('1'), account('2'), account('3')];
      else if (url.pathname.endsWith('/trends/tags'))
        data = [
          { name: 'photography', history: [{ accounts: '124' }] },
          { name: 'art', history: [{ accounts: '89' }] },
        ];
      else if (url.pathname.endsWith('/search')) {
        await route.fulfill({ status: 401, json: { error: 'Unauthorized' } });
        return;
      } else throw new Error('Unexpected API request ' + url);
      await route.fulfill({ json: data });
      return;
    }
    if (url.hostname === 'cdn.example.org') {
      await route.fulfill({ body: jpg, contentType: 'image/jpeg' });
      return;
    }
    if (process.env.NORTHLINE_LIVE === '1') {
      await route.continue();
      return;
    }
    await route.abort();
  });
  await page.goto(base);
  assert.equal(await page.locator('html').getAttribute('lang'), 'ru');
  assert.equal(await page.getByLabel('Язык интерфейса').inputValue(), 'ru');
  assert.equal(await page.getByRole('link', { name: 'Line', exact: true }).locator('.brand-mark svg').count(), 1);
  await page
    .locator('.post')
    .first()
    .waitFor({ timeout: 30000 })
    .catch(async (error) => {
      console.error(await page.locator('body').innerText());
      console.error(apiRequests);
      await page.screenshot({ path: path.join(__dirname, '.tmp/failure.png') });
      throw error;
    });
  if (process.env.NORTHLINE_LIVE === '1') {
    await page.waitForTimeout(1800);
    const postCount = await page.locator('.post').count();
    const images = await page
      .locator('.post-image')
      .evaluateAll((elements) =>
        elements.map((e) => ({ loaded: e.complete && e.naturalWidth > 0, src: e.currentSrc })),
      );
    await page.screenshot({ path: path.join(__dirname, '.tmp/northline-live-desktop.png') });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: path.join(__dirname, '.tmp/northline-live-mobile.png') });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert(
      images.some((image) => image.loaded),
      'Live photos should load',
    );
    assert.deepEqual(errors, []);
    assert.deepEqual(cspErrors, []);
    console.log(
      JSON.stringify({
        mode: 'live',
        postCount,
        loadedImages: images.filter((i) => i.loaded).length,
        errors,
        cspErrors,
      }),
    );
    return;
  }
  assert.equal(await page.locator('.post').count(), 3, 'Duplicates across topics must be removed');
  assert.equal(await page.locator('.post[data-post-id="101"] .rich-text p').count(), 2);
  assert.equal(await page.locator('.post-image').count(), 3, 'Mastodon image enum must render');
  assert.equal(
    await page.locator('.post[data-post-id="101"] .rich-text a').getAttribute('href'),
    '#/tag/photography',
  );
  const first = page.locator('.post[data-post-id="101"]');
  await first.getByRole('button', { name: 'Сохранить запись', exact: true }).click();
  assert.equal(await page.getByRole('dialog').locator('.auth-reason').count(), 0);
  await page.getByRole('dialog').getByRole('button', { name: 'Регистрация' }).click();
  await page.getByRole('dialog').getByLabel('Логин', { exact: true }).fill('x');
  await page.getByRole('dialog').getByLabel('Имя в профиле', { exact: true }).fill('Тестовый читатель');
  await page.getByRole('dialog').getByLabel('Пароль', { exact: true }).fill('very-local-password');
  await page.getByRole('dialog').getByRole('button', { name: 'Зарегистрироваться' }).click();
  await page.getByRole('dialog').locator('.auth-error').waitFor();
  await page.getByRole('dialog').getByLabel('Логин', { exact: true }).fill('northline_test');
  await page.getByRole('dialog').getByLabel('Имя в профиле', { exact: true }).fill('Тестовый читатель');
  await page.getByRole('dialog').getByLabel('Пароль', { exact: true }).fill('very-local-password');
  await page.getByRole('dialog').getByRole('button', { name: 'Зарегистрироваться' }).click();
  await page.locator('dialog[open]').waitFor({ state: 'detached' });
  await first.getByRole('button', { name: 'Сохранить запись', exact: true }).click();
  const savedStyle = await first.getByRole('button', { name: 'Убрать из сохранённого' }).evaluate((control) => ({
    selected: control.classList.contains('selected'),
    background: getComputedStyle(control).backgroundColor,
    fill: getComputedStyle(control.querySelector('svg')).fill,
  }));
  assert(savedStyle.selected);
  assert.notEqual(savedStyle.background, 'rgba(0, 0, 0, 0)');
  assert.notEqual(savedStyle.fill, 'none');
  await page.locator('.post[data-post-id="102"]').getByRole('button', { name: 'Проголосовать' }).click();
  await page.getByRole('dialog').getByLabel('Утро').check();
  await page.getByRole('dialog').getByRole('button', { name: 'Проголосовать' }).click();
  await page.locator('.post[data-post-id="102"]').getByRole('button', { name: 'Изменить голос' }).waitFor();
  await page.locator('.primary-nav').getByText('Профиль', { exact: true }).click();
  await page.locator('.my-line-tabs a[href="#/me?tab=saved"]').click();
  await page.locator('.post[data-post-id="101"]').waitFor();
  assert.equal(await page.locator('.post').count(), 1);
  await page.reload();
  await page.locator('.post[data-post-id="101"]').waitFor();
  await page.locator('.post').getByRole('button', { name: 'Действия с записью' }).click();
  await page.getByRole('button', { name: 'Добавить в избранное' }).click();
  assert(await page.locator('.post[data-post-id="101"] .post-action[aria-label^="Нравится"]').evaluate((control) => control.classList.contains('selected')));
  await page.locator('.my-line-tabs a[href="#/me?tab=likes"]').click();
  assert.equal(await page.locator('.post').count(), 1);
  await page.locator('.post').getByRole('link', { name: 'Открыть обсуждение, 2 ответов' }).click();
  await page.getByText('Какой красивый свет!', { exact: true }).waitFor();
  assert.equal(await page.locator('.thread-reply.nested').count(), 1);
  const remoteReply = page.locator('.thread-reply').filter({ has: page.locator('.post[data-post-id="201"]') });
  await remoteReply.getByRole('button', { name: /^Ответить @/ }).click();
  await page.getByLabel('Текст комментария').fill('Ответ именно внешнему комментарию');
  await page.getByRole('dialog').getByRole('button', { name: 'Опубликовать' }).click();
  await page.reload();
  await page.locator('.local-comment').filter({ hasText: 'Ответ именно внешнему комментарию' }).waitFor();
  assert(await page.locator('.thread').evaluate((thread) => {
    const nodes = [...thread.children];
    const parent = nodes.findIndex((node) => node.querySelector('[data-post-id="201"]'));
    const child = nodes.findIndex((node) => node.textContent.includes('Ответ именно внешнему комментарию'));
    const other = nodes.findIndex((node) => node.querySelector('[data-post-id="202"]'));
    return parent >= 0 && child > parent && nodes[child].classList.contains('nested') && (other < 0 || child > other);
  }), 'Remote and local replies must share one thread after reload');
  await page.locator('.reply-prompt').getByRole('button', { name: 'Написать комментарий' }).click();
  await page.getByLabel('Текст комментария').fill('Локальный комментарий для проверки');
  await page.getByRole('dialog').getByRole('button', { name: 'Опубликовать' }).click();
  await page.getByText('Локальный комментарий для проверки', { exact: true }).waitFor();
  await page.locator('.full-post .author-name').click();
  await page.getByRole('heading', { name: 'Анна Светлова', exact: true }).waitFor();
  await page.getByText('Публикация из профиля, отсутствующая в ленте.', { exact: true }).waitFor();
  assert.equal(await page.getByText('Закреплённая запись', { exact: true }).count(), 1);
  assert.equal(await page.getByText('Профиль в Mastodon', { exact: true }).count(), 0);
  await page.locator('.profile-top').getByRole('button', { name: 'Добавить автора' }).click();
  await page.locator('.sidebar-link').filter({ hasText: 'Выбранные авторы' }).click();
  await page.getByText('Публикация из профиля, отсутствующая в ленте.', { exact: true }).waitFor();
  await page.goto(base + '#/feed');
  await page.locator('.post[data-post-id="101"]').waitFor();
  await page.waitForFunction(() => !document.querySelector('.page-heading button').disabled);
  added = true;
  await page.getByRole('button', { name: 'Проверить новые записи' }).click();
  await page.getByRole('button', { name: /Новые записи · 1/ }).waitFor();
  assert.equal(await page.locator('.post[data-post-id="110"]').count(), 0);
  await page.getByRole('button', { name: /Новые записи · 1/ }).click();
  await page.locator('.post[data-post-id="110"]').waitFor();
  await page.getByRole('button', { name: 'Показать ещё', exact: true }).click();
  await page.getByText('An older story, loaded with pagination.', { exact: true }).waitFor();
  assert(apiRequests.some((url) => url.includes('max_id=')));
  await page.getByLabel('Язык публикаций').selectOption('ru');
  assert.equal(
    await page.getByText('An older story, loaded with pagination.', { exact: true }).count(),
    0,
  );
  await page.getByRole('button', { name: 'Мои интересы', exact: true }).click();
  await page.getByRole('dialog').getByRole('checkbox').first().uncheck();
  await page.getByRole('button', { name: 'Сохранить интересы' }).click();
  await page.locator('.post').first().waitFor();
  await page.locator('.post[data-post-id="102"] .media-button').first().click();
  await page.getByRole('dialog').getByRole('button', { name: 'Вперёд' }).click();
  assert.equal(await page.getByText('2 / 2', { exact: true }).count(), 1);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('dialog[open]').count(), 0);
  await page.getByRole('button', { name: 'Написать', exact: true }).click();
  await page.getByLabel('Текст черновика').fill('Проверочный черновик о прогулке');
  await page.keyboard.press('Escape');
  await page.locator('.primary-nav').getByText('Профиль', { exact: true }).click();
  await page.locator('.my-line-tabs a[href="#/me?tab=drafts"]').click();
  await page.getByText('Проверочный черновик о прогулке', { exact: true }).waitFor();
  assert.equal(await page.locator('.post[data-post-id^="local:"]').count(), 0, 'A draft must not become a published story');
  await page.locator('#content').getByRole('button', { name: 'Создать публикацию', exact: true }).click();
  await page.getByLabel('Текст черновика').fill('Опубликованная локальная история');
  await page.getByLabel('Добавить фото или видео').setInputFiles([
    { name: 'evening.png', mimeType: 'image/png', buffer: png },
    { name: 'short-video.mp4', mimeType: 'video/mp4', buffer: Buffer.from('local-video-preview') },
  ]);
  await page.locator('.composer-attachment').nth(1).waitFor();
  await page.getByRole('dialog').screenshot({ path: path.join(__dirname, '.tmp/northline-story-editor.png') });
  await page.getByRole('dialog').getByRole('button', { name: 'Опубликовать' }).click();
  const ownPost = page.locator('.post[data-post-id^="local:"]');
  await ownPost.getByText('Опубликованная локальная история', { exact: true }).waitFor();
  const uploadedMedia = await page.evaluate(() => {
    const state = JSON.parse(localStorage.getItem('disguise:northline:state'));
    return state.profiles[state.session.userId].reader.localPosts[0].media.map((media) => media.type);
  });
  assert.deepEqual(uploadedMedia, ['image', 'video']);
  assert.equal(await ownPost.locator('.author-name').getAttribute('href'), '#/me');
  await ownPost.getByRole('button', { name: 'Сохранить запись' }).click();
  await page.locator('.local-stats').getByText('2 сохранённых', { exact: true }).waitFor();
  await ownPost.getByRole('link', { name: 'Открыть обсуждение, 0 ответов' }).click();
  await page.getByRole('heading', { name: 'Разговор ещё впереди' }).waitFor();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.locator('.reply-prompt').getByRole('button', { name: 'Написать комментарий' }).click();
  const commentField = page.getByLabel('Текст комментария');
  assert.equal(await commentField.getAttribute('maxlength'), '5000');
  await page.getByRole('dialog').getByText('До 4 вложений', { exact: true }).waitFor();
  await page.getByRole('dialog').getByText('0 / 5000', { exact: true }).waitFor();
  const commentLayout = await page.getByRole('dialog').evaluate((modal) => {
    const field = modal.querySelector('.compose-text').getBoundingClientRect();
    const action = modal.querySelector('.form-actions .button').getBoundingClientRect();
    return { fieldBottom: field.bottom, actionTop: action.top };
  });
  assert(commentLayout.actionTop > commentLayout.fieldBottom, 'Comment action must not overlap textarea');
  await page.getByRole('dialog').screenshot({ path: path.join(__dirname, '.tmp/northline-comment-editor.png') });
  await commentField.fill('Первый ответ на свою историю');
  await page.getByRole('dialog').getByText('28 / 5000', { exact: true }).waitFor();
  await page.getByLabel('Добавить фото или видео к комментарию').setInputFiles({
    name: 'comment.png', mimeType: 'image/png', buffer: png,
  });
  await page.locator('.comment-attachments .composer-attachment').waitFor();
  await page.getByRole('dialog').getByRole('button', { name: 'Опубликовать' }).click();
  assert.equal(await page.getByRole('heading', { name: 'Разговор ещё впереди' }).count(), 0);
  await page.getByText('Первый ответ на свою историю', { exact: true }).waitFor();
  let ownComment = page.locator('.local-comment').filter({ hasText: 'Первый ответ на свою историю' });
  assert.equal(await ownComment.locator('.local-comment-media img').count(), 1);
  await ownComment.getByRole('button', { name: 'Нравится, 0' }).click();
  ownComment = page.locator('.local-comment').filter({ hasText: 'Первый ответ на свою историю' });
  assert(await ownComment.getByRole('button', { name: 'Нравится, 1' }).getAttribute('aria-pressed'));
  await ownComment.getByRole('button', { name: 'Ответить @northline_test' }).click();
  await page.getByRole('dialog').getByText('@northline_test', { exact: true }).waitFor();
  await page.getByLabel('Текст комментария').fill('Вложенный ответ для проверки');
  await page.getByRole('dialog').getByRole('button', { name: 'Опубликовать' }).click();
  await page.locator('.local-comment').filter({ hasText: 'Вложенный ответ для проверки' }).waitFor();
  assert.equal(await page.locator('.thread-reply.nested .local-comment').count(), 1);
  await page.screenshot({ path: path.join(__dirname, '.tmp/northline-comments-mobile.png'), fullPage: true });
  await page.locator('.thread-reply.nested .local-comment').screenshot({ path: path.join(__dirname, '.tmp/northline-comment-reply.png') });
  ownComment = page.locator('.local-comment').filter({ hasText: 'Первый ответ на свою историю' });
  assert.equal(await ownComment.getByRole('button', { name: 'Сохранить комментарий' }).count(), 1);
  await ownComment.getByRole('button', { name: 'Сохранить комментарий' }).click();
  ownComment = page.locator('.local-comment').filter({ hasText: 'Первый ответ на свою историю' });
  await ownComment.getByRole('button', { name: 'Убрать комментарий из сохранённого' }).waitFor();
  await ownComment.getByRole('button', { name: 'Поделиться в Line, 0' }).click();
  const discussionUrl = page.url();
  await page.locator('.primary-nav').getByText('Профиль', { exact: true }).click();
  await page.locator('.my-line-tabs a[href="#/me?tab=reposts"]').click();
  await page.locator('.reposted-comment').getByText('Первый ответ на свою историю', { exact: true }).waitFor();
  await page.goto(discussionUrl);
  await page.getByText('Первый ответ на свою историю', { exact: true }).waitFor();
  ownComment = page.locator('.local-comment').filter({ hasText: 'Первый ответ на свою историю' });
  await ownComment.getByRole('button', { name: 'Удалить комментарий' }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Удалить', exact: true }).click();
  await page.getByRole('heading', { name: 'Разговор ещё впереди' }).waitFor();
  await page.evaluate(() => {
    const state = JSON.parse(localStorage.getItem('disguise:northline:state'));
    const localPost = state.profiles[state.session.userId].reader.localPosts[0];
    state.accounts.push({ id: 'visitor-user', username: 'visitor', displayName: 'Гость обсуждения' });
    state.comments.push({
      id: 'visitor-comment', postId: `local:${localPost.id}`, parentId: '', userId: 'visitor-user',
      text: 'Комментарий другого пользователя', media: [], createdAt: new Date().toISOString(),
    });
    localStorage.setItem('disguise:northline:state', JSON.stringify(state));
  });
  await page.reload();
  const foreignComment = page.locator('.local-comment').filter({ hasText: 'Комментарий другого пользователя' });
  await foreignComment.waitFor();
  await foreignComment.getByRole('button', { name: 'Ответить @visitor' }).click();
  await page.getByLabel('Текст комментария').fill('Ответ другому пользователю');
  await page.getByRole('dialog').getByRole('button', { name: 'Опубликовать' }).click();
  await page.locator('.local-comment').filter({ hasText: 'Ответ другому пользователю' }).waitFor();
  assert.equal(await page.locator('.thread-reply.nested .local-comment').count(), 1);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.locator('.primary-nav').getByText('Профиль', { exact: true }).click();
  await page.locator('.post[data-post-id^="local:"]').getByRole('button', { name: 'Действия с записью' }).click();
  await page.getByRole('button', { name: 'Редактировать историю' }).click();
  await page.getByLabel('Текст истории').fill('Отредактированная история');
  await page.getByRole('button', { name: 'Сохранить изменения' }).click();
  await page.getByText('Отредактированная история', { exact: true }).waitFor();
  await page.evaluate(() => {
    window.restoreStorage = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      if (key === 'disguise:northline:state') throw new DOMException('Full', 'QuotaExceededError');
      return window.restoreStorage.call(this, key, value);
    };
  });
  await page.locator('.post[data-post-id^="local:"]').getByRole('button', { name: 'Действия с записью' }).click();
  await page.getByRole('button', { name: 'Удалить историю' }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Удалить', exact: true }).click();
  await page.getByText('Не удалось сохранить изменения. Попробуйте ещё раз.', { exact: true }).waitFor();
  assert.equal(await page.locator('.post[data-post-id^="local:"]').count(), 1);
  await page.evaluate(() => { Storage.prototype.setItem = window.restoreStorage; delete window.restoreStorage; });
  await page.locator('.post[data-post-id^="local:"]').getByRole('button', { name: 'Действия с записью' }).click();
  await page.getByRole('button', { name: 'Удалить историю' }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Удалить', exact: true }).click();
  assert.equal(await page.locator('.post[data-post-id^="local:"]').count(), 0);
  await page.reload();
  await page.locator('.local-profile').waitFor();
  assert.equal(await page.locator('.post[data-post-id^="local:"]').count(), 0,
    'Deleted stories must not reappear after reload');
  assert.equal(await page.evaluate(() => {
    const state = JSON.parse(localStorage.getItem('disguise:northline:state'));
    return state.comments.filter((comment) => comment.postId.startsWith('local:')).length;
  }), 0, 'Deleting a story also persists deletion of its comments');
  await page.getByRole('button', { name: 'Редактировать профиль', exact: true }).click();
  await page.getByLabel('Имя', { exact: true }).fill('Дмитрий');
  await page.getByLabel('Загрузить фото профиля').setInputFiles({
    name: 'avatar.png', mimeType: 'image/png', buffer: png,
  });
  await page.getByRole('dialog').locator('.profile-avatar-preview img').waitFor();
  await page.getByRole('button', { name: 'Сохранить', exact: true }).click();
  assert.equal(await page.locator('.local-profile h2').textContent(), 'Дмитрий');
  assert.equal(await page.locator('.local-profile .avatar img').count(), 1);
  const profileLayout = await page.locator('.local-profile').evaluate((profile) => {
    const avatar = profile.querySelector('.avatar').getBoundingClientRect();
    const name = profile.querySelector('h2').getBoundingClientRect();
    return { avatarBottom: avatar.bottom, nameTop: name.top };
  });
  assert(profileLayout.nameTop >= profileLayout.avatarBottom, 'Profile name must not overlap the avatar');
  await page.setViewportSize({ width: 390, height: 844 });
  assert(await page.locator('.my-line-tabs').evaluate((tabs) => tabs.scrollWidth <= tabs.clientWidth));
  assert.equal(await page.locator('.my-line-tabs a > span').first().isVisible(), false);
  await page.screenshot({ path: path.join(__dirname, '.tmp/northline-profile-mobile.png') });
  // A tall tab used to shrink the search box and wrap the profile buttons.
  for (const width of [320, 360, 390, 455, 463, 700, 720, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 844 });
    let baseline;
    for (const tab of ['posts', 'reposts', 'likes', 'saved', 'drafts']) {
      await page.goto(base + '#/me?tab=' + tab);
      await page.locator('.local-profile').waitFor();
      const geometry = await page.evaluate(() => {
        const selectors = ['.global-search', '.local-profile', '.profile-controls .button'];
        return selectors.flatMap((selector) => [...document.querySelectorAll(selector)].map((node) => {
          const rect = node.getBoundingClientRect();
          return [rect.x, rect.width, rect.height];
        }));
      });
      if (!baseline) baseline = geometry;
      assert.deepEqual(geometry, baseline, `Stable profile geometry: ${width}px, ${tab}`);
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
        `No horizontal overflow: ${width}px, ${tab}`);
    }
    if (width <= 720) {
      const rows = await page.locator('.profile-controls .button').evaluateAll((buttons) =>
        buttons.map((button) => button.getBoundingClientRect().top));
      assert.equal(rows[0], rows[1], `Profile actions share a row at ${width}px`);
    }
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.reload();
  await page.locator('.my-line-tabs a[href="#/me?tab=drafts"]').click();
  await page.getByText('Проверочный черновик о прогулке', { exact: true }).waitFor();
  await page.locator('.draft').getByRole('button', { name: 'Удалить', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Удалить', exact: true }).click();
  assert.equal(await page.locator('.draft').count(), 0);
  await page.goto(base + '#/feed');
  const repostTarget = page.locator('.post[data-post-id="101"]');
  await repostTarget.waitFor();
  await repostTarget.getByRole('button', { name: /Поделиться в Line, / }).click();
  await page.locator('.primary-nav').getByText('Профиль', { exact: true }).click();
  await page.locator('.my-line-tabs a[href="#/me?tab=reposts"]').click();
  await page.locator('.post[data-post-id="101"]').waitFor();
  await page.getByRole('button', { name: 'Выйти', exact: true }).click();
  await page.getByRole('dialog').getByRole('heading', { name: 'Выйти из профиля?' }).waitFor();
  await page.getByRole('dialog').getByRole('button', { name: 'Выйти', exact: true }).click();
  await page.getByRole('heading', { name: 'Ваш профиль ждёт' }).waitFor();
  assert.equal(await page.locator('.mini-profile strong').textContent(), 'Гость Line');
  await page.getByRole('button', { name: 'Войти', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Регистрация' }).click();
  await page.getByRole('dialog').getByLabel('Логин', { exact: true }).fill('northline_test');
  await page.getByRole('dialog').getByLabel('Имя в профиле', { exact: true }).fill('Другой читатель');
  await page.getByRole('dialog').getByLabel('Пароль', { exact: true }).fill('another-password');
  await page.getByRole('dialog').getByRole('button', { name: 'Зарегистрироваться' }).click();
  await page.getByRole('dialog').getByText('Этот логин уже занят.', { exact: true }).waitFor();
  await page.getByRole('dialog').getByRole('button', { name: 'У меня уже есть профиль' }).click();
  await page.getByRole('dialog').getByLabel('Логин', { exact: true }).fill('northline_test');
  await page.getByRole('dialog').getByLabel('Пароль', { exact: true }).fill('very-local-password');
  await page.getByRole('dialog').getByRole('button', { name: 'Войти', exact: true }).click();
  await page.locator('.local-profile').getByRole('heading', { name: 'Дмитрий' }).waitFor();
  await page.getByLabel('Поиск в Line').fill('Анна');
  await page.getByLabel('Поиск в Line').press('Enter');
  await page.getByText(/Гостевой поиск ограничен/).waitFor();
  assert(await page.locator('.search-people').count());
  // Malicious HTML, whitespace and URL validation exercise the same renderer used by profiles and posts.
  const safety = await page.evaluate(async () => {
    const { richText } = await import('./northline-ui.js');
    const { normalizeMastodonStatus, safeUrl, plainText } = await import('./northline-data.js');
    const node = richText(
      '<p onclick="alert(1)">Hello</p><script>window.pwned=true</script><a href="javascript:alert(1)">bad</a><img src=x onerror="alert(2)"><iframe src="https://evil.test"></iframe>',
    );
    const media = normalizeMastodonStatus({
      id: '1',
      account: { id: '2' },
      content: '',
      media_attachments: [{ id: '3', type: 'image', url: 'https://cdn.example.org/a.jpg' }],
    });
    return {
      html: node.innerHTML,
      url: safeUrl('javascript:alert(1)'),
      credentials: safeUrl('https://user:pass@example.org'),
      text: plainText('<p>One</p><p>Two<br>Three</p>'),
      media: media.media.length,
    };
  });
  assert(!/onclick|script|iframe|onerror|javascript/.test(safety.html));
  assert.equal(safety.url, '');
  assert.equal(safety.credentials, '');
  assert.equal(safety.text, 'One\n\nTwo\nThree');
  assert.equal(safety.media, 1);
  const modalSafety = await page.evaluate(async () => {
    const { dialog } = await import('./northline-ui.js');
    const first = dialog('First');
    const second = dialog('Second');
    const distinctLabels = first.modal.getAttribute('aria-labelledby') !== second.modal.getAttribute('aria-labelledby');
    second.close();
    await new Promise((resolve) => second.modal.addEventListener('close', resolve, { once: true }));
    const locked = document.body.classList.contains('dialog-open');
    first.close();
    await new Promise((resolve) => first.modal.addEventListener('close', resolve, { once: true }));
    return { distinctLabels, locked, released: !document.body.classList.contains('dialog-open') };
  });
  assert.deepEqual(modalSafety, { distinctLabels: true, locked: true, released: true });
  const retries = await page.evaluate(async () => {
    const { request } = await import('./northline-data.js');
    const originalFetch = window.fetch;
    const originalNow = Date.now;
    let clock = originalNow();
    let calls = 0;
    try {
      Date.now = () => clock;
      window.fetch = async () => {
        calls++;
        return calls === 1
          ? new Response('{}', { status: 429, headers: { 'Retry-After': '60' } })
          : new Response('[{"id":"ok"}]', { status: 200 });
      };
      await request('/api/v1/test-rate-limit', { force: true }).catch(() => {});
      await request('/api/v1/test-retry', { force: true }).catch(() => {});
      const beforeCooldown = calls;
      clock += 61000;
      const recovered = await request('/api/v1/test-retry', { force: true });
      window.fetch = async () => { throw new Error('offline'); };
      const stale = await request('/api/v1/test-retry', { force: true });
      return { beforeCooldown, calls, recovered: recovered.data[0].id, stale: stale.stale };
    } finally {
      window.fetch = originalFetch;
      Date.now = originalNow;
    }
  });
  assert.deepEqual(retries, { beforeCooldown: 1, calls: 2, recovered: 'ok', stale: true });
  await page.goto(base + '#/feed');
  await page.locator('.post').first().waitFor();
  offline = true;
  await page.reload();
  await page
    .getByText(/Не удалось обновить ленту|Показана сохранённая лента/)
    .first()
    .waitFor();
  assert((await page.locator('.post').count()) > 0, 'Offline cache must retain content');
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: path.join(__dirname, '.tmp/northline-mobile.png') });
  assert(
    await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
    'No horizontal overflow on phone',
  );
  assert.equal(await page.locator('.primary-nav .nav-item').count(), 4);
  assert.equal(await page.locator('.primary-nav').getByText('Сохранённое', { exact: true }).count(), 0);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: path.join(__dirname, '.tmp/northline-desktop.png') });
  assert(
    !(await page
      .locator('body')
      .innerText()
      .then((text) => /\bnull\b|\bundefined\b|\bfalse\b/.test(text))),
    'Conditional rendering must not expose null/false',
  );
  await page.getByLabel('Язык интерфейса').selectOption('en');
  await page.getByRole('link', { name: 'Explore', exact: true }).waitFor();
  assert.equal(await page.locator('html').getAttribute('lang'), 'en');
  assert.equal(await page.getByLabel('Interface language').inputValue(), 'en');
  assert.equal(await page.title(), 'Explore — Line');
  for (const width of [320, 390, 720, 768, 1440]) {
    await page.setViewportSize({ width, height: 844 });
    for (const route of ['feed', 'topics', 'authors', 'profile/1', 'post/101', 'me']) {
      await page.goto(base + '#/' + route);
      await page.locator('main').waitFor();
      await page.waitForTimeout(80);
      if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) {
        console.error(await page.evaluate(() => [...document.querySelectorAll('main *')]
          .filter((node) => node.getBoundingClientRect().right > innerWidth)
          .map((node) => ({ tag: node.tagName, class: node.className, text: node.textContent.slice(0, 80), right: node.getBoundingClientRect().right }))));
        await page.screenshot({ path: path.join(__dirname, '.tmp/northline-overflow.png') });
      }
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
        `English route fits: ${route}, ${width}px`);
    }
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.locator('.primary-nav').getByRole('link', { name: 'Profile', exact: true }).click();
  await page.getByRole('heading', { name: 'Profile', exact: true }).waitFor();
  await page.getByRole('button', { name: 'Edit profile', exact: true }).click();
  await page.getByRole('dialog').getByRole('heading', { name: 'Your profile', exact: true }).waitFor();
  await page.getByRole('dialog').getByRole('button', { name: 'Close', exact: true }).click();
  await page.locator('.primary-nav').getByRole('link', { name: 'Explore', exact: true }).click();
  await page.screenshot({ path: path.join(__dirname, '.tmp/northline-interface-en.png') });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: path.join(__dirname, '.tmp/northline-interface-en-mobile.png') });
  await page.getByLabel('Interface language').selectOption('ru');
  await page.getByRole('link', { name: 'Обзор', exact: true }).waitFor();
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'languages', { configurable: true, get: () => ['en-US'] });
    Object.defineProperty(navigator, 'language', { configurable: true, get: () => 'en-US' });
  });
  await page.evaluate(() => localStorage.removeItem('northline:interface-language:v1'));
  await page.reload();
  await page.getByRole('link', { name: 'Explore', exact: true }).waitFor();
  assert.equal(await page.locator('html').getAttribute('lang'), 'en');
  assert.deepEqual(errors, [], 'No uncaught JavaScript errors');
  assert.deepEqual(cspErrors, [], 'No CSP violations');
  console.log(
    'PASS: feed, auth, reactions, favourites, polls, comments, story and draft CRUD, profiles, threads, selected authors, refresh, pagination, media, search, sanitization, offline cache, mobile layout.',
  );
})()
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  })
  .finally(async () => {
    await browser?.close();
  });
