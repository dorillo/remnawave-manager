/* No HTTP server: serve the template and deterministic provider responses in memory.
 * node tests/aster.browser.cjs
 * ASTER_LIVE=1 probes real RUTUBE CORS, covers and the embedded player instead.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { chromium } = require('./.tmp/node_modules/playwright-core');
const root = path.resolve(__dirname, '../src/remnawave_manager/data/disguises');
const origin = 'https://aster.test';
const base = `${origin}/02-aster-observatory/`;
const live = process.env.ASTER_LIVE === '1';
const policy = execFileSync(
  process.env.PYTHON || 'python3',
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
      process.env.ASTER_BROWSER ||
      'C:/Program Files/Google/Chrome/Application/chrome.exe',
    headless: true,
  });
  const context = await browser.newContext({
    locale: 'ru-RU',
    viewport: { width: 1440, height: 1000 },
  });
  const errors = [];
  const csp = [];
  let denyCatalog = false;
  let denySearch = false;
  let searchCalls = 0;
  const searchPages = [];
  await context.route(`${origin}/**`, async (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith('/_aster/rutube-video/')) {
      const id = url.pathname.split('/').at(-1);
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ id, title: 'Current title', description: 'Полное описание из API' }) });
    }
    if (url.pathname.startsWith('/_aster/rutube-profile/')) {
      const id = url.pathname.split('/').at(-1);
      const fixture = JSON.parse(await fs.readFile(path.join(root, '02-aster-observatory/data/catalog.json'), 'utf8'));
      const channel = fixture.channels.find(channel => String(channel.id) === id);
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ id, name: channel?.name || 'Автор свежего комментария', avatar_url: channel?.avatar, subscribers_count: 12345 }) });
    }
    if (url.pathname.startsWith('/_aster/rutube-comments/')) {
      const fixture = JSON.parse(await fs.readFile(path.join(root, '02-aster-observatory/data/catalog.json'), 'utf8'));
      const video = fixture.videos.find(video => video.id === url.pathname.split('/').at(-1));
      return route.fulfill({ contentType: 'application/json', body: JSON.stringify({ results: video?.publicComments || [], comments_count: video?.commentsCount || 0, has_next: false }) });
    }
    if (url.pathname === '/_aster/rutube-search') {
      searchCalls += 1;
      const page = Math.max(1, Number(url.searchParams.get('page')) || 1);
      searchPages.push(page);
      if (denySearch) return route.abort();
      const fixture = JSON.parse(
        await fs.readFile(
          path.join(root, '02-aster-observatory/data/catalog.json'),
          'utf8',
        ),
      );
      const query = (url.searchParams.get('query') || '').toLowerCase();
      const rows = [
        ...fixture.videos.map((video) => ({ ...video, content_type: 'video' })),
        ...fixture.channels.flatMap((channel) =>
          channel.videos.map((video) => ({
            ...video,
            channelId: channel.id,
            channel: channel.name,
            avatar: channel.avatar,
            content_type: 'video',
          })),
        ),
      ];
      const matches = rows.filter((video) =>
        `${video.title} ${video.channel}`.toLowerCase().includes(query),
      );
      const selected = query === 'minecraft' ? rows : matches.length ? matches : rows;
      const offset = (page - 1) * 20;
      return route.fulfill({
        contentType: 'application/json',
        body: JSON.stringify({
          results: selected.slice(offset, offset + 20),
          has_next: offset + 20 < selected.length,
        }),
      });
    }
    let filename = path.resolve(root, `.${decodeURIComponent(url.pathname)}`);
    if (!filename.startsWith(root + path.sep)) return route.abort();
    if (url.pathname.endsWith('/'))
      filename = path.join(filename, 'index.html');
    if (denyCatalog && filename.endsWith('catalog.json')) return route.abort();
    try {
      const body = await fs.readFile(filename);
      const types = {
        '.html': 'text/html',
        '.js': 'text/javascript',
        '.css': 'text/css',
        '.json': 'application/json',
        '.svg': 'image/svg+xml',
        '.jpg': 'image/jpeg',
      };
      await route.fulfill({
        body,
        headers:
          path.extname(filename) === '.html'
            ? { 'Content-Security-Policy': policy }
            : {},
        contentType:
          types[path.extname(filename)] || 'application/octet-stream',
      });
    } catch {
      await route.fulfill({ status: 404, body: 'Not found' });
    }
  });
  if (!live) {
    await context.route('https://pic.rtbcdn.ru/**', (route) =>
      route.fulfill({
        contentType: 'image/svg+xml',
        body: '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="360"><rect width="640" height="360" fill="#8b9a8d"/></svg>',
      }),
    );
    await context.route('https://rutube.ru/**', async (route) => {
      if (route.request().url().includes('/play/embed/'))
        return route.fulfill({
          contentType: 'text/html',
          body: '<!doctype html><title>Fixture player</title><button id="play">Play</button><script>document.querySelector("button").onclick=()=>{parent.postMessage(JSON.stringify({type:"player:playStart",data:{}}),"https://aster.test");parent.postMessage(JSON.stringify({type:"player:currentTime",data:{time:123}}),"https://aster.test");parent.postMessage(JSON.stringify({type:"player:changeState",data:{state:"paused"}}),"https://aster.test");};</script>',
        });
      // A readable HTTP response without CORS must still fail in the browser.
      return route.fulfill({
        contentType: 'application/json',
        body: '{"results":[]}',
      });
    });
  }
  const page = await context.newPage();
  page.on('pageerror', (error) => errors.push(error.message));
  await page.addInitScript(() => {
    window.__csp = [];
    document.addEventListener('securitypolicyviolation', (e) =>
      window.__csp.push(e.violatedDirective),
    );
  });
  await page.goto(base);
  await page.locator('.video-card').first().waitFor();
  assert.equal(await page.locator('.video-card').count(), 24);
  assert.ok(await page.locator('.channel-avatar img').first().isVisible());
  const searchLeft = await page
    .locator('.search')
    .evaluate((node) => node.getBoundingClientRect().left);
  await page.locator('.menu').click();
  assert.equal(
    await page.locator('main').evaluate((node) => getComputedStyle(node).marginLeft),
    '88px',
  );
  await page.locator('.menu').click();
  if (live) {
    const cors = await page.evaluate(async () => {
      try {
        const response = await fetch(
          'https://rutube.ru/api/video/person/24233749/?page=1',
        );
        return { readable: true, status: response.status };
      } catch (error) {
        return { readable: false, error: error.message };
      }
    });
    await page.evaluate(() => {
      window.__playerEvents = [];
      window.addEventListener('message', (event) => {
        if (event.origin === 'https://rutube.ru') {
          try {
            const value =
              typeof event.data === 'string'
                ? JSON.parse(event.data)
                : event.data;
            window.__playerEvents.push(value.type);
          } catch {}
        }
      });
    });
    await page.locator('.thumb').first().click();
    await page.locator('iframe').waitFor();
    await page.waitForTimeout(15000);
    const frame = page
      .frames()
      .find((f) => f.url().includes('rutube.ru/play/embed'));
    console.log(
      'LIVE',
      JSON.stringify({
        cors,
        iframe: frame?.url(),
        frameTitle: await frame?.title().catch(() => ''),
        images: await page.locator('.video-card img').evaluateAll((imgs) =>
          imgs.map((img) => ({
            loaded: img.complete && img.naturalWidth > 0,
            src: img.src,
          })),
        ),
        status: await page.locator('.player-status').textContent(),
      }),
    );
    if (frame) {
      console.log(
        'FRAME',
        (
          await frame
            .locator('body')
            .innerText()
            .catch(() => '')
        ).slice(0, 700),
      );
      console.log(
        'PLAY',
        await frame
          .evaluate(async () => {
            const video = document.querySelector('video');
            if (!video) return 'No video element';
            video.muted = true;
            try {
              await video.play();
              return { state: video.readyState, time: video.currentTime };
            } catch (e) {
              return e.message;
            }
          })
          .catch((e) => e.message),
      );
      await page.waitForTimeout(5000);
      console.log(
        'PLAYBACK',
        await frame
          .evaluate(() => {
            const v = document.querySelector('video');
            return v
              ? {
                  time: v.currentTime,
                  paused: v.paused,
                  error: v.error?.message,
                  state: v.readyState,
                }
              : null;
          })
          .catch(() => null),
      );
      console.log('EVENTS', [
        ...new Set(await page.evaluate(() => window.__playerEvents)),
      ]);
    }
    await page.screenshot({
      path: path.join(__dirname, '.tmp/aster-live.png'),
      fullPage: true,
    });
    return;
  }
  const catalogFixture = JSON.parse(
    await fs.readFile(
      path.join(root, '02-aster-observatory/data/catalog.json'),
      'utf8',
    ),
  );
  await page.getByRole('button', { name: 'Показать ещё', exact: true }).click();
  assert.equal(
    await page.locator('.video-card').count(),
    48,
  );
  await page.getByRole('button', { name: 'Показать ещё', exact: true }).waitFor();
  await page.evaluate(() => {
    location.hash = '/catalog';
  });
  await page
    .getByRole('button', { name: 'Загрузить ещё видео', exact: true })
    .waitFor();
  assert.equal(await page.locator('.video-card').count(), 24);
  await page
    .getByRole('button', { name: 'Загрузить ещё видео', exact: true })
    .click();
  assert.equal(await page.locator('.video-card').count(), 48);
  await page.locator('.search input').fill('minecraft');
  await page.locator('.search').evaluate((form) => form.requestSubmit());
  await page.waitForURL('**/#/catalog?q=minecraft');
  await page.locator('.video-card').first().waitFor();
  assert.equal(await page.locator('.video-card').count(), 20);
  assert.ok(searchCalls > 0);
  assert.deepEqual(searchPages, [1]);
  await page
    .getByRole('button', { name: 'Загрузить ещё видео', exact: true })
    .click();
  await page.locator('.video-card').nth(39).waitFor();
  assert.equal(await page.locator('.video-card').count(), 40);
  assert.deepEqual(searchPages, [1, 2]);
  const firstTitle = catalogFixture.videos.find(
    (video) =>
      video.publicComments?.filter((comment) => !comment.parent_id).length > 10,
  ).title;
  denySearch = true;
  await page.locator('.search input').fill(firstTitle);
  await page.locator('.search').evaluate((form) => form.requestSubmit());
  await page.waitForURL('**/#/catalog?q=*');
  await page.locator('.video-card').first().waitFor();
  assert.ok((await page.locator('main').innerText()).includes(firstTitle));
  await page.locator('.top-actions .account').click();
  await page
    .getByRole('button', { name: 'Создать профиль', exact: true })
    .click();
  await page.locator('dialog:not([open])').waitFor({ state: 'detached' });
  await page.locator('dialog input[name=name]').fill('Александр');
  await page.locator('dialog input[name=email]').fill('aster@example.test');
  await page.locator('dialog input[name=password]').fill('a-strong-passphrase');
  await page.locator('dialog button[type=submit]').click();
  await page.locator('dialog').waitFor({ state: 'detached' });
  await page.locator('.thumb').first().click();
  await page.locator('.public-comment').first().waitFor();
  await page.getByText('Полное описание из API', { exact: true }).waitFor();
  await page.locator('.watch-channel .subscriber-count').filter({ hasText: '12' }).waitFor();
  assert.equal(await page.getByRole('button', { name: 'Повторить' }).count(), 0);
  await page.frameLocator('iframe').locator('#play').evaluate(() => {
    parent.postMessage(
      JSON.stringify({ type: 'player:error', data: {} }),
      'https://aster.test',
    );
  });
  const retryPlayer = page.getByRole('button', { name: 'Повторить' });
  await retryPlayer.waitFor();
  await retryPlayer.click();
  assert.equal(await retryPlayer.count(), 0);
  const publicAuthor = page
    .locator('.public-comment .comment-author a')
    .first();
  const publicAuthorHref = await publicAuthor.getAttribute('href');
  assert.match(publicAuthorHref, /^#\/user\/\d+$/);
  assert.equal(await publicAuthor.getAttribute('target'), null);
  assert.ok(
    (await page.locator('.public-comment .comment-author .meta').first().innerText())
      .trim().length > 0,
  );
  assert.equal(await page.locator('.comment-source').count(), 0);
  assert.ok(
    await page.locator('.public-comment .comment-avatar img').first().isVisible(),
  );
  assert.equal(await page.locator('.public-comment:not(.comment-reply)').count(), 10);
  await page
    .getByRole('button', { name: 'Показать ещё комментарии', exact: true })
    .click();
  assert.ok(await page.locator('.public-comment:not(.comment-reply)').count() > 10);
  assert.equal(await page.locator('.watch-rail .video-card').count(), 18);
  await page
    .locator('.watch-rail')
    .getByRole('button', { name: 'Показать ещё', exact: true })
    .click();
  assert.ok(await page.locator('.watch-rail .video-card').count() > 18);
  assert.ok(
    Math.abs(
      (await page
        .locator('.search')
        .evaluate((node) => node.getBoundingClientRect().left)) - searchLeft,
    ) <= 1,
  );
  const publicLikes = catalogFixture.videos.find(
    (video) => video.title === firstTitle,
  ).publicLikes;
  const videoLike = page.getByRole('button', {
    name: 'Нравится',
    exact: true,
  });
  const heartSize = await videoLike.locator('svg').boundingBox();
  assert.equal(await videoLike.getAttribute('aria-pressed'), 'false');
  assert.ok(
    (await videoLike.innerText()).includes(
      new Intl.NumberFormat('ru-RU').format(publicLikes),
    ),
  );
  const fixturePlay = page.frameLocator('iframe').locator('#play');
  await fixturePlay.evaluate(
    (node) =>
      new Promise((resolve) => {
        const ready = () =>
          typeof node.onclick === 'function' ? resolve() : setTimeout(ready, 10);
        ready();
      }),
  );
  await fixturePlay.click();
  const id = await page.evaluate(() => location.hash.split('/').at(-1));
  await page.waitForFunction(
    (videoId) => {
      const state = JSON.parse(
        localStorage.getItem('disguise:aster:state:v1') || '{}',
      );
      return state.accounts?.[0]?.library?.history?.[videoId]?.time === 123;
    },
    id,
  );
  await videoLike.click();
  assert.equal(await videoLike.getAttribute('aria-pressed'), 'true');
  const likedSize = await videoLike.locator('svg').boundingBox();
  assert.equal(likedSize.width, heartSize.width);
  assert.equal(likedSize.height, heartSize.height);
  await page
    .getByRole('button', { name: 'Смотреть позже', exact: true })
    .first()
    .click();
  await page.getByRole('button', { name: 'Подписаться', exact: true }).click();
  assert.equal(await page.getByText('Плейлисты', { exact: true }).count(), 0);
  assert.equal(await page.locator('.watch-layout > .watch-primary').count(), 1);
  assert.equal(await page.locator('.watch-layout > .watch-rail').count(), 1);
  await page.screenshot({
    path: path.join(__dirname, '.tmp/aster-watch.png'),
  });
  await page
    .getByLabel('Напишите комментарий…')
    .fill('<script>alert(1)</script>');
  await page.locator('.comment-form button[type=submit]').click();
  await page
    .locator('.public-comment')
    .first()
    .locator(':scope > .button-row')
    .getByRole('button', { name: 'Ответить', exact: true })
    .click();
  assert.equal(await page.locator('.comments > .comment-form').count(), 1);
  assert.equal(
    await page.locator('.comment-form-inline').evaluate((form) =>
      form.parentElement?.getAttribute('data-comment-id')),
    await page.locator('.public-comment').first().getAttribute('data-comment-id'),
  );
  assert.equal(
    await page.locator('.comment-form-inline').getByRole('button').count(),
    2,
  );
  await page.getByLabel('Напишите ответ…').fill('Ответ на комментарий');
  await page.locator('.comment-form-inline button[type=submit]').click();
  const publicCommentLike = page
    .locator('.public-comment')
    .first()
    .getByRole('button', { name: 'Нравится комментарий', exact: true })
    .first();
  assert.equal(await publicCommentLike.locator('svg').evaluate(node => node.getBoundingClientRect().width), 18);
  await publicCommentLike.click();
  assert.equal(await publicCommentLike.getAttribute('aria-pressed'), 'true');
  assert.equal(await publicCommentLike.locator('svg').evaluate(node => node.getBoundingClientRect().width), 18);
  const nestedPublicReply = page.locator('.public-comment.comment-reply').first();
  if (await nestedPublicReply.count()) {
    await nestedPublicReply
      .locator(':scope > .button-row')
      .getByRole('button', { name: 'Ответить', exact: true })
      .click();
    assert.equal(await page.locator('.comment-form-inline').count(), 1);
    await page.getByLabel('Напишите комментарий…').focus();
    assert.equal(await page.locator('.comment-form-inline').count(), 0);
  }
  assert.equal(await page.locator('.comment:not(.public-comment)').count(), 2);
  assert.ok(
    await page
      .locator('.comment-text')
      .filter({ hasText: '<script>alert(1)</script>' })
      .count(),
  );
  await page
    .locator('.comment:not(.public-comment)')
    .last()
    .getByRole('button', { name: 'Редактировать', exact: true })
    .click();
  await page.getByLabel('Измените комментарий…').fill('Изменённый ответ');
  await page.locator('.comment-form-inline button[type=submit]').click();
  assert.equal(
    await page
      .locator('.comment:not(.public-comment) .comment-text')
      .last()
      .innerText(),
    'Изменённый ответ',
  );
  await page.evaluate(() => {
    location.hash = '/history';
  });
  await page.locator('.video-card').first().waitFor();
  const state = await page.evaluate(() =>
    JSON.parse(localStorage.getItem('disguise:aster:state:v1')),
  );
  const lib = state.accounts[0].library;
  assert.equal(lib.history[id].time, 123);
  assert.equal(lib.comments.length, 2);
  assert.equal(lib.commentLikes.length, 1);
  assert.ok(lib.likes.includes(id));
  assert.ok(lib.later.includes(id));
  assert.ok(lib.follows.length);
  const followedSnapshot = catalogFixture.channels.find(
    (channel) => channel.id === lib.follows[0],
  );
  await page.evaluate(() => {
    location.hash = '/subscriptions';
  });
  const allChannels = page.getByRole('button', {
    name: 'Все каналы',
    exact: true,
  });
  await allChannels.waitFor();
  assert.equal(await allChannels.getAttribute('aria-pressed'), 'true');
  await page
    .getByRole('button', { name: followedSnapshot.name, exact: true })
    .click();
  await page.waitForURL(`**/#/subscriptions?channel=${lib.follows[0]}`);
  assert.ok(await page.locator('.video-card').count());
  assert.ok(
    await page.locator('.video-card .channel-link').evaluateAll(
      (nodes, channel) => nodes.every((node) => node.textContent === channel),
      followedSnapshot.name,
    ),
  );
  if (followedSnapshot.videos.length > 24)
    await page
      .getByRole('button', { name: 'Загрузить ещё видео', exact: true })
      .waitFor();
  await page.getByRole('button', { name: 'Все каналы', exact: true }).click();
  await page.waitForURL('**/#/subscriptions');
  await page.evaluate((channelId) => {
    location.hash = `/channel/${channelId}`;
  }, lib.follows[0]);
  await page.locator('.channel-banner img').waitFor();
  assert.equal(
    await page.locator('.video-card').count(),
    Math.min(24, followedSnapshot.videos.length),
  );
  if (followedSnapshot.videos.length > 24)
    await page.getByRole('button', { name: 'Показать ещё', exact: true }).waitFor();
  await page.evaluate(() => {
    location.hash = '/history';
  });
  await page.locator('.video-card').first().waitFor();
  await page.locator('.thumb').first().click();
  assert.ok(
    (await page.locator('iframe').getAttribute('src')).includes('t=123'),
  );
  // Untrusted parent messages cannot forge watch history.
  await page.reload();
  await page.locator('.public-comment').first().waitFor();
  assert.equal(await page.locator('.comment:not(.public-comment)').count(), 2);
  await page.evaluate((href) => {
    location.hash = href.slice(1);
  }, publicAuthorHref);
  await page.locator('.user-banner').waitFor();
  assert.equal(new URL(page.url()).hash, publicAuthorHref);
  assert.ok(await page.locator('.user-comment').count());
  await page.evaluate((videoId) => {
    location.hash = `/watch/${videoId}`;
  }, id);
  await page.locator('.public-comment').first().waitFor();
  await page
    .locator('.comment:not(.public-comment)')
    .first()
    .getByRole('button', { name: 'Удалить', exact: true })
    .first()
    .click();
  await page.getByRole('button', { name: 'Подтвердить', exact: true }).click();
  await page.reload();
  await page.locator('.comment-form').waitFor();
  assert.equal(await page.locator('.comment:not(.public-comment)').count(), 1);
  await page.evaluate(() =>
    window.postMessage(
      { type: 'player:currentTime', data: { time: 999 } },
      '*',
    ),
  );
  await page.evaluate(() => {
    location.hash = '/home';
  });
  await page.locator('.video-card').first().waitFor();
  assert.equal(
    await page.getByRole('heading', { name: 'Продолжить просмотр' }).count(),
    0,
  );
  assert.equal(
    await page.getByRole('button', { name: 'Добавить по ссылке' }).count(),
    0,
  );
  for (const lang of ['ru', 'en']) {
    if (await page.locator('html').getAttribute('lang') !== lang)
      await page.getByRole('button', { name: /Язык интерфейса|Interface language/ }).click();
    for (const width of [320, 360, 390, 720, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 1000 });
      for (const route of [
        'home',
        'catalog',
        'profile',
        'subscriptions',
        'history',
        'likes',
        'later',
        `watch/${id}`,
        `channel/${lib.follows[0]}`,
      ]) {
        await page.evaluate((route) => {
          location.hash = `/${route}`;
        }, route);
        await page.waitForTimeout(35);
        assert.ok(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
          `Overflow ${lang} ${width} ${route}`,
        );
        assert.deepEqual(
          await page.evaluate(() => {
            const ids = [...document.querySelectorAll('[id]')].map(
              (node) => node.id,
            );
            const named = (node) =>
              Boolean(
                node.getAttribute('aria-label')?.trim() ||
                  node.getAttribute('aria-labelledby')?.trim() ||
                  node.getAttribute('title')?.trim() ||
                  node.closest('label') ||
                  node.textContent?.trim(),
              );
            return {
              duplicateIds: ids.filter(
                (id, index) => ids.indexOf(id) !== index,
              ),
              unnamedControls: [
                ...document.querySelectorAll(
                  'button, input, select, textarea, iframe',
                ),
              ].filter((node) => !named(node)).length,
              imagesWithoutAlt: [
                ...document.querySelectorAll('img:not([alt])'),
              ].length,
            };
          }),
          {
            duplicateIds: [],
            unnamedControls: 0,
            imagesWithoutAlt: 0,
          },
          `Accessibility ${lang} ${width} ${route}`,
        );
      }
    }
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.evaluate(() => {
    location.hash = '/home';
  });
  await page.waitForTimeout(50);
  await page.screenshot({
    path: path.join(__dirname, '.tmp/aster-desktop.png'),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: path.join(__dirname, '.tmp/aster-mobile.png'),
    fullPage: true,
  });
  await page.evaluate(() => {
    location.hash = '/profile';
  });
  await page.getByRole('button', { name: 'Edit profile', exact: true }).click();
  await page.locator('.profile-form').waitFor();
  assert.equal(await page.locator('dialog').getByText('aster@example.test').count(), 0);
  await page.getByRole('button', { name: 'Close', exact: true }).click();
  assert.equal(await page.locator('.privacy').count(), 0);
  assert.equal(await page.getByRole('button', { name: 'Add by URL' }).count(), 0);
  assert.equal(await page.locator('a[href*="rutube.ru"]').count(), 0);
  assert.deepEqual(
    await page.evaluate(async () => {
      const { safeImage } = await import('./aster-data.js');
      return [safeImage('javascript:alert(1)'), safeImage('https://evil.test/x.jpg')];
    }),
    ['', ''],
  );
  const uploadedId = 'abcdef0123456789abcdef0123456789';
  await page.evaluate(async ({ uploadedId }) => {
    const ownerId = JSON.parse(
      localStorage.getItem('disguise:aster:state:v1'),
    ).session;
    const db = await new Promise((resolve, reject) => {
      const request = indexedDB.open('aster:media:v1', 1);
      request.onupgradeneeded = () => {
        const store = request.result.createObjectStore('videos', { keyPath: 'id' });
        store.createIndex('ownerId', 'ownerId');
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
    });
    await new Promise((resolve, reject) => {
      const transaction = db.transaction('videos', 'readwrite');
      transaction.objectStore('videos').put({
        id: uploadedId,
        ownerId,
        title: 'Local fixture video',
        description: 'Stored in IndexedDB',
        createdAt: Date.now(),
        duration: 12,
        poster: '',
        size: 7,
        type: 'video/mp4',
        file: new Blob(['fixture'], { type: 'video/mp4' }),
      });
      transaction.oncomplete = resolve;
      transaction.onerror = () => reject(transaction.error);
    });
    db.close();
    location.hash = '/home';
  }, { uploadedId });
  await page.locator('.video-card').first().waitFor();
  await page.evaluate(() => {
    location.hash = '/profile';
  });
  await page.getByRole('link', { name: 'Watch: Local fixture video' }).click();
  await page.locator('.local-screen video').waitFor();
  assert.ok(await page.locator('.watch-rail .video-card').count());
  await page.getByLabel('Write a comment…').fill('Local video comment');
  await page.locator('.comment-form button[type=submit]').click();
  assert.equal(
    await page.locator('.comment-text').filter({ hasText: 'Local video comment' }).count(),
    1,
  );
  await page.evaluate(() => {
    location.hash = '/profile';
  });
  await page
    .getByRole('button', { name: 'Delete video: Local fixture video' })
    .click();
  await page.getByRole('button', { name: 'Confirm', exact: true }).click();
  await page.getByText('No videos on this channel yet', { exact: true }).waitFor();
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await page.getByRole('button', { name: 'Confirm', exact: true }).click();
  await page
    .getByRole('button', { name: 'Sign in or create a profile', exact: true })
    .first()
    .click();
  await page.locator('dialog input[name=email]').fill('aster@example.test');
  await page.locator('dialog input[name=password]').fill('wrong-password');
  await page.locator('dialog button[type=submit]').click();
  await page
    .locator('dialog [role=alert]')
    .filter({ hasText: 'Check your email and password' })
    .waitFor();
  await page
    .getByRole('button', { name: 'Create profile', exact: true })
    .click();
  await page.locator('dialog:not([open])').waitFor({ state: 'detached' });
  await page.locator('dialog input[name=name]').fill('Second');
  await page.locator('dialog input[name=email]').fill('second@example.test');
  await page.locator('dialog input[name=password]').fill('another-passphrase');
  await page.locator('dialog button[type=submit]').click();
  await page.locator('dialog').waitFor({ state: 'detached' });
  await page.evaluate(() => {
    location.hash = '/likes';
  });
  await page.getByRole('heading', { name: 'No liked videos yet' }).waitFor();
  await require('./personal-empty.helpers.cjs')(page, [['#/likes', 'likedVideos'], ['#/later', 'later'], ['#/subscriptions', 'following'], ['#/history', 'historyVideo']]);
  denyCatalog = true;
  await page.reload();
  await page.getByRole('heading', { name: 'No liked videos yet' }).waitFor();
  await page.evaluate(() => {
    location.hash = '/home';
  });
  await page.locator('.video-card').first().waitFor();
  await page.evaluate(() => {
    Storage.prototype.setItem = () => {
      throw new DOMException('Quota', 'QuotaExceededError');
    };
  });
  await page.locator('.card-save').first().click();
  await page
    .locator('#toast')
    .filter({ hasText: 'Changes could not be saved' })
    .waitFor();
  csp.push(...(await page.evaluate(() => window.__csp)));
  assert.deepEqual(errors, []);
  assert.deepEqual(csp, []);
  console.log(
    'PASS: general feed, YouTube-style responsive interface, search, profiles, isolated libraries, subscriptions, comment/reply CRUD and reactions, resume, CORS fallback, storage failure and RU/EN route checks.',
  );
})()
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  })
  .finally(async () => {
    await browser?.close();
  });
