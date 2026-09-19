// Exercise actual image rendering with direct external requests unavailable.
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const { chromium } = require('./.tmp/node_modules/playwright-core');
const root = path.resolve(__dirname, '../src/remnawave_manager/data/disguises');
const origin = 'https://images.test';
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl3M2kAAAAASUVORK5CYII=', 'base64');
(async () => {
  const browser = await chromium.launch({ executablePath: process.env.IMAGE_BROWSER || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true });
  try {
    const page = await browser.newPage();
    const proxied = [], external = [], errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', async route => {
      const u = new URL(route.request().url());
      if (u.origin !== origin) { external.push(u.href); return route.abort(); }
      if (/^\/(?:_images|_loop\/media|_fokus\/media)\//.test(u.pathname)) {
        proxied.push(u.pathname + u.search);
        return route.fulfill({ contentType: 'image/png', body: png });
      }
      if (u.pathname === '/') return route.fulfill({ contentType: 'text/html', body: '<!doctype html><meta http-equiv="Content-Security-Policy" content="default-src \'self\'; script-src \'self\'; img-src \'self\' data: blob:"><body></body>' });
      try {
        return route.fulfill({ contentType: 'text/javascript', body: await fs.readFile(path.join(root, u.pathname)) });
      } catch { return route.fulfill({ status: 404, body: '' }); }
    });
    await page.goto(origin);
    const result = await page.evaluate(async () => {
      const { imageURL } = await import('/shared/image-proxy.js');
      const northline = await import('/01-northline/northline-ui.js');
      const aster = await import('/02-aster-observatory/aster-data.js');
      const asterUI = await import('/02-aster-observatory/aster-ui.js');
      const morrow = await import('/03-morrow-coffee/morrow-yappy.js');
      const morrowUI = await import('/03-morrow-coffee/morrow-ui.js');
      const answers = await import('/04-signal-works/answers-data.js');
      const svod = await import('/05-field-notes/svod-article.js');
      const loop = await import('/06-loop-archive/loop-data.js');
      const photo = document.createElement('img');
      const filename = 'a'.repeat(96) + '.jpg?size=origin';
      photo.src = answers.images({ type: 'imageGallery', attrs: { gallery: [{ src: filename }] } })[0];
      const loopImage = document.createElement('img');
      loopImage.src = loop.mediaURL('https://media.gifs.ru/' + 'a'.repeat(32) + '.gif');
      const article = svod.renderArticle({ title: 'Фото', html: '<p>Фото</p><img src="//upload.wikimedia.org/wikipedia/commons/4/44/%D0%A4%D0%BE%D1%82%D0%BE.svg.png" srcset="https://evil.test/x.png 2x">' });
      document.body.append(
        northline.picture('https://mastodon.ml/system/accounts/avatars/a.png'),
        northline.picture('https://files.mastodon.social/media_attachments/a.jpg'),
        // Render older stored absolute URLs as well as freshly normalized URLs.
        asterUI.el('img', { src: 'https://pic.rtbcdn.ru/a.jpg?width=400&v=2' }),
        morrowUI.el('img', { src: 'https://cdn-st.yappy.media/a.jpg' }),
        photo, article.container, loopImage,
      );
      const checks = {
        aster: aster.safeImage('https://pic.rtbcdn.ru/a.jpg'),
        morrow: morrow.safeURL('https://cdn-st.rutubelist.ru/a.jpg'),
        video: morrow.safeURL('https://vb-rtb.uma.media/video.mp4', true),
        idempotent: imageURL(imageURL('https://pic.rtbcdn.ru/a.jpg?width=400')),
        bad: ['https://evil.test/a.png', 'https://pic.rtbcdn.ru:8443/a.png', 'https://u:p@pic.rtbcdn.ru/a.png', '/_images/evil.test/a.png', 'javascript:alert(1)'].map(imageURL),
        mail: {
          bare: answers.safeImage(filename),
          relative: answers.safeImage('/api/pictures/images/' + filename),
          saved: answers.safeImage('/' + filename),
          proxy: answers.safeImage('/_images/otvet.cdn-vk.net/api/pictures/images/' + filename),
          legacyAvatar: answers.safeImage('https://filin.mail.ru/pic?d=abc_123~'),
          invalid: ['missing.jpg', '/api/pictures/images/../../admin', '/unknown/a.png', 'https://evil.test/a.jpg'].map(answers.safeImage),
        },
        srcset: article.container.querySelector('img').hasAttribute('srcset'),
      };
      await Promise.all([...document.images].map(img => new Promise((resolve, reject) => {
        img.loading = 'eager';
        if (img.complete && img.naturalWidth) return resolve();
        img.onload = resolve;
        img.onerror = () => reject(new Error('Failed image: ' + img.src));
      })));
      checks.loaded = [...document.images].every(img => img.naturalWidth > 0);
      return checks;
    });
    assert.equal(result.aster, '/_images/pic.rtbcdn.ru/a.jpg');
    assert.equal(result.morrow, '/_images/cdn-st.rutubelist.ru/a.jpg');
    assert.equal(result.video, 'https://vb-rtb.uma.media/video.mp4');
    assert.equal(result.idempotent, '/_images/pic.rtbcdn.ru/a.jpg?width=400');
    assert.deepEqual(result.bad, ['', '', '', '', '']);
    const mailImage = '/_images/otvet.cdn-vk.net/api/pictures/images/' + 'a'.repeat(96) + '.jpg?size=origin';
    for (const key of ['bare', 'relative', 'saved', 'proxy']) assert.equal(result.mail[key], mailImage);
    assert.equal(result.mail.legacyAvatar, '/_images/filin.mail.ru/pic?d=abc_123~');
    assert.deepEqual(result.mail.invalid, ['', '', '', '']);
    assert.equal(result.srcset, false);
    assert.equal(result.loaded, true);
    assert.equal(proxied.length, 7);
    assert.deepEqual(external, []);
    assert.deepEqual(errors, []);
    console.log('PASS: images render through same-origin routes; stored URLs, Unicode, query strings, srcset removal, local proxy idempotency, hostile origins and direct video preserved.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
