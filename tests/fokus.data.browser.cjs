/* Parser regressions under the production CSP, including untrusted source HTML. */
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("./.tmp/node_modules/playwright-core");
const root = path.resolve(
  __dirname,
  "../src/remnawave_manager/data/disguises/07-fokus-news",
);
(async () => {
  const browser = await chromium.launch({
    executablePath:
      process.env.FOKUS_BROWSER ||
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    headless: true,
  });
  try {
    const page = await browser.newPage(),
      violations = [],
      external = [];
    let response = "";
    page.on("console", (msg) => {
      if (/Content Security Policy|violates.*directive/i.test(msg.text()))
        violations.push(msg.text());
    });
    await page.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      if (url.origin !== "https://fokus.test") {
        external.push(url.href);
        return route.abort();
      }
      if (url.pathname.startsWith("/_fokus/"))
        return route.fulfill({ body: response, contentType: "text/plain" });
      if (url.pathname === "/")
        return route.fulfill({
          body: "<!doctype html><title>Parser tests</title>",
          contentType: "text/html",
          headers: {
            "Content-Security-Policy":
              "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self'; object-src 'none'",
          },
        });
      await route.fulfill({
        body: await fs.readFile(path.join(url.pathname.startsWith("/shared/") ? path.dirname(root) : root, url.pathname)),
        contentType: "text/javascript",
      });
    });
    await page.goto("https://fokus.test/");
    const text = await page.evaluate(async () => {
      const { inert, safeInline } = await import("./fokus-data.js");
      const raw = `<!-- <div style="color: red">ignored</div> -->
      <div class="article__text" title="1 > 0; style='do not change'" STYLE = "color:red" style='background:url(https://evil.test/style)'>Текст <strong style=color:red>новости</strong></div>
      <div/style=color:red>Косая черта</div><div style>Пустой атрибут</div>
      <svg style="fill:red"><g style='fill:blue'></g></svg>
      <style>body{display:none}</style><script>window.pwned=true</script>
      <img src="https://evil.test/img" onerror="window.pwned=true"><iframe src="https://evil.test/frame"></iframe>
      <textarea>Буквальный текст: &lt;span style="color:red"&gt;</textarea>`;
      const fragment = inert(raw);
      return {
        text: fragment.querySelector(".article__text").textContent,
        title: fragment.querySelector(".article__text").title,
        html: safeInline(fragment.querySelector(".article__text")),
        styles: fragment.querySelectorAll("[style],style,script,iframe").length,
        literal: fragment.querySelector("textarea").textContent,
        pwned: !!window.pwned,
      };
    });
    assert.equal(text.text, "Текст новости");
    assert.equal(text.title, "1 > 0; style='do not change'");
    assert.equal(text.html, "Текст <strong>новости</strong>");
    assert.equal(text.literal, 'Буквальный текст: <span style="color:red">');
    assert.equal(text.styles, 0);
    assert.equal(text.pwned, false);
    await page.waitForTimeout(100);
    assert.deepEqual(violations, []);
    assert.deepEqual(external, []);
    async function article(raw) {
      response = raw;
      return page.evaluate(async () => {
        const data = await import("./fokus-data.js");
        return data.load("article", {
          ref: data.articleRef("/20260919/test-123.html"),
          force: true,
        });
      });
    }
    const online = await article('<meta property="og:title" content="Онлайн-репортаж"><div class="article m-white_online"><div class="online__item-time">10:04</div><div class="online__item-text">Новая <strong>запись</strong><script>bad()</script></div><div class="article__block" data-type="list"><ul><li>Итог</li></ul></div></div>');
    assert.ok(online.blocks.some(block => block.html?.includes('Новая <strong>запись</strong>')));
    assert.ok(online.blocks.some(block => block.html === '<h3>10:04</h3>'));
    assert.ok(online.blocks.every(block => !block.html?.includes('<script')));
    if (process.env.FOKUS_ONLINE_FIXTURE) {
      const live = await article(await fs.readFile(process.env.FOKUS_ONLINE_FIXTURE, 'utf8'));
      assert.ok(live.blocks.length > 20, 'Real online report parses its timeline');
      console.log('Online report blocks:', live.blocks.length);
    }

    const hosts = ["cdnn21.img.ria.ru", "cdnn21.imgria.ru"];
    for (const host of hosts) {
      const url = `https://${host}/images/07ea/09/18/123_80.jpg`;
      const expected = "/_fokus/media/images/07ea/09/18/123_80.jpg";
      const feed = await loadPage("feed", `<rss><channel><item><link>https://ria.ru/20260919/test-123.html</link><title>Фото</title><enclosure url="${url}"/></item></channel></rss>`);
      assert.equal(feed.items[0].image, expected, 'RSS CDN alias');
      for (const markup of [
        `<img src="${url}">`,
        `<img src="data:image/gif;base64,AA" data-src="${url}">`,
        `<img src="/placeholder.svg" srcset="https://evil.test/a.jpg 1x, ${url} 2x">`,
        `<picture><source data-srcset="  ${url} 640w"><img src="/placeholder.svg"></picture>`,
        `<img src="/placeholder.svg"><img data-src="${url}">`,
      ]) {
        const result = await article(`<h1 class="article__title">Фото</h1><div class="article__body"><div class="article__block" data-type="image">${markup}</div></div>`);
        assert.ok(result.blocks.some(block => block.type === 'image' && block.src === expected), 'image candidates: '+markup);
      }
    }
    const rejected = await page.evaluate(async () => {
      const { media } = await import('./fokus-data.js');
      return ['https://cdnn21.imgria.ru.evil.test/images/a.jpg', 'https://evil.test/images/a.jpg',
        'https://user@cdnn21.imgria.ru/images/a.jpg', 'https://cdnn21.imgria.ru:8443/images/a.jpg',
        'javascript:alert(1)', 'https://cdnn21.imgria.ru/images/a.svg'].map(media);
    });
    assert.deepEqual(rejected, Array(6).fill(''));
    const img = "https://cdnn21.img.ria.ru/images/123_80.jpg";
    let parsed = await article(
      `<meta property="og:title" content="Тестовый лонгрид"><div class="article__body m-longread"><div class="white-longread__block white-longread__header" data-type="header"><div class="white-longread__header-author">Автор</div><div class="white-longread__header-media"><img src="data:image/svg+xml,placeholder" data-src="${img}"></div></div><div class="white-longread__block" data-type="text"><div class="white-longread__text-body" style="color:red">Первый абзац <strong>лонгрида</strong></div></div><div class="white-longread__block" data-type="h3"><div class="white-longread__width">Подзаголовок</div></div><div class="white-longread__block" data-type="media-image"><img src="data:image/svg+xml,placeholder" data-src="${img}"><div class="white-longread__media-description">Подпись</div></div><div class="white-longread__block" data-type="text"><div class="white-longread__text-body">Конец статьи</div></div></div>`,
    );
    assert.equal(parsed.title, "Тестовый лонгрид");
    assert.equal(parsed.author, "Автор");
    assert.equal(parsed.image, "/_fokus/media/images/123_80.jpg");
    assert.deepEqual(
      parsed.blocks.map((b) => b.type),
      ["text", "text", "image", "text"],
    );
    assert.equal(parsed.blocks[1].html, "<h3>Подзаголовок</h3>");
    assert.equal(parsed.blocks[2].caption, "Подпись");
    parsed = await article(
      `<h1 class="article__title">Тестовая фотолента</h1><meta property="og:image" content="${img}"><div class="article__body"><div class="article__block" data-type="photolenta">${[1, 2].map((i) => `<div class="article__photo-item"><img style="width:100%" src="data:image/svg+xml,placeholder" data-src="https://cdnn21.img.ria.ru/images/${i}_80.jpg"><div class="article__photo-item-text"><p>Снимок ${i}</p></div></div>`).join("")}</div></div>`,
    );
    assert.equal(
      parsed.image,
      "",
      "the gallery does not repeat its first photo as a cover",
    );
    assert.deepEqual(
      parsed.blocks.map((b) => [b.type, b.src, b.caption]),
      [
        ["image", "/_fokus/media/images/1_80.jpg", "Снимок 1"],
        ["image", "/_fokus/media/images/2_80.jpg", "Снимок 2"],
      ],
    );
    await page.evaluate(async () => {
      const store = await import("./fokus-store.js");
      await store.change((s) => {
        s.cache = [];
      }, false);
    });
    parsed = await article(`<div class="article__body"><div class="article__block" data-type="list"><ul><li>Первый пункт</li><li>Второй пункт</li></ul></div><div class="article__block" data-type="video"><video poster="${img}" data-title="Тестовое видео"></video></div></div>`);
    assert.equal(parsed.blocks[0].html, '<ul><li>Первый пункт</li><li>Второй пункт</li></ul>');
    assert.equal(parsed.blocks[1].type, 'videoPreview');
    assert.equal(parsed.blocks[1].src, '/_fokus/media/images/123_80.jpg');
    await page.evaluate(async () => (await import('./fokus-store.js')).change(s => { s.cache = []; }, false));
    await assert.rejects(article('<div class="article__body"><div class="article__visual-journalism-black"><script>window.pwned=true</script></div></div>'), /articleFormat/);
    // A real malformed response must still fail, not become a fake empty article.
    await assert.rejects(
      article("<html><h1>Access denied</h1></html>"),
      /schema/,
    );
    async function loadPage(kind, raw, options = {}) {
      response = raw;
      return page.evaluate(
        async ({ kind, options }) => {
          const { load } = await import("./fokus-data.js");
          return load(kind, { ...options, force: true });
        },
        { kind, options },
      );
    }
    parsed = await loadPage(
      "section",
      '<div class="list-items-loaded" data-next-url="/services/world/more.html?id=123&amp;date=20260919T120000"></div>',
      { section: "world" },
    );
    assert.equal(
      parsed.cursor,
      "",
      "empty article page ignores stale next URL",
    );
    parsed = await loadPage(
      "search",
      '<div class="list-items-loaded" data-count="1" data-next-url="/services/search/getmore/?query=test&amp;offset=20"><div class="list-item"><a class="list-item__title" href="https://ria.ru/20260919/test-123.html">Result</a></div></div>',
      { query: "test" },
    );
    assert.equal(parsed.items.length, 1);
    assert.equal(parsed.cursor, "", "search total reached");
    parsed = await loadPage(
      "comments",
      JSON.stringify({
        chat: {
          messages: [],
          last_date: { sec: 1789760000, usec: 1 },
          last_id: "a".repeat(24),
        },
      }),
      { ref: { id: "123" } },
    );
    assert.equal(parsed.cursor, "", "empty comments ignore stale cursor");
    assert.deepEqual(violations, []);
    assert.deepEqual(external, []);
    console.log("Fokus parser/CSP checks passed");
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
