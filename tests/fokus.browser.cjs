/* Deterministic end-to-end checks; no requests or writes to the source. */
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("./.tmp/node_modules/playwright-core");
const root = path.resolve(
  __dirname,
  "../src/remnawave_manager/data/disguises/07-fokus-news",
);
const origin = "https://fokus.test",
  url = "https://ria.ru/20260919/test-2118660370.html";
const malicious =
  '<script>window.pwned=true</script><iframe src="https://evil.test/"></iframe><img src="https://evil.test/image" onerror="window.pwned=true">';
const rss = `<rss><channel>${Array.from({ length: 45 }, (_, i) => `<item><title>Новость ${i}: важные события дня</title><link>https://ria.ru/20260919/test-${2118660370 + i}.html</link><pubDate>2026-09-19T12:00:00+03:00</pubDate><category>Общество</category></item>`).join("")}</channel></rss>`;
const home = `<div class="cell"><a href="${url}"></a><span class="cell-video__title">Главная новость дня</span></div>${malicious}`;
const article = `<h1 class="article__title">Заголовок проверяемой статьи</h1><meta property="article:published_time" content="2026-09-19T12:00:00+03:00"><div class="article__body"><div class="article__block" data-type="text"><div class="article__text">РИА Новости. Полный текст новости <strong>с выделением</strong>${malicious}<a href="javascript:alert(1)">опасная ссылка</a></div></div><div class="article__block" data-type="quote"><div class="article__quote-text">Содержательная цитата.</div></div></div>`;
let count = 10,
  failComments = false,
  failCommentsFor = "",
  failArticle = false,
  missingLikes = false,
  failNext = false;
const dynamics = (id = "2118660370") =>
  `<div class="article__userbar-emoji"><div class="emoji" data-id="${id}">${["s1", "s6", "s2", "s3", "s4", "s5"].map((x) => `<a data-type="${x}"><span class="m-value">${count}</span></a>`).join("")}</div></div>`;
const comment = {
  id: "a".repeat(24),
  nickname: "Читатель РИА",
  text: "Внешний комментарий",
  created: { sec: 1789760000 },
  user_ip: "PRIVATE",
  parent_comment: {
    id: "b".repeat(24),
    nickname: "Автор ответа",
    text: "Контекст ответа",
  },
};
(async () => {
  const browser = await chromium.launch({
    executablePath:
      process.env.FOKUS_BROWSER ||
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    headless: true,
  });
  try {
    const context = await browser.newContext({
        viewport: { width: 1440, height: 1000 },
      }),
      bad = [],
      errors = [],
      requests = [];
    await context.route("**/*", async (r) => {
      const u = new URL(r.request().url());
      requests.push(r.request().url());
      if (u.origin !== origin) {
        bad.push(u.href);
        return r.abort();
      }
      if (r.request().method() !== "GET") {
        bad.push(r.request().method());
        return r.abort();
      }
      if (u.pathname.startsWith("/_fokus/")) {
        let body = "",
          status = 200;
        if (u.pathname.endsWith("/feed")) body = rss;
        else if (u.pathname.endsWith("/home")) body = home;
        else if (u.pathname.endsWith("/search")) {
          const offset = Number(u.searchParams.get("offset"));
          body = `<div class="list-items-loaded" data-count="21" ${offset ? "" : `data-next-url="/services/search/getmore/?query=${encodeURIComponent(u.searchParams.get("query"))}&amp;offset=20"`}><div class="list-item"><a class="list-item__title" href="https://ria.ru/20260919/archive-${3000 + offset}.html">Архивный результат ${offset}</a></div></div>`;
        } else if (u.pathname.includes("/article/")) {
          body = failNext && u.pathname.endsWith("test-2118660372.html")
            ? '<div class="article__body"><div class="article__visual-journalism-black"><script src="https://evil.test/embed.js"></script></div></div>' : article;
          if (
            failArticle ||
            (failNext && u.pathname.endsWith("test-2118660371.html"))
          )
            status = 503;
        } else if (u.pathname.includes("/dynamics/"))
          body = dynamics(u.pathname.split("/").at(-1));
        else if (u.pathname.endsWith("/comments")) {
          if (
            failComments ||
            u.searchParams.get("article_id") === failCommentsFor
          )
            status = 503;
          body = JSON.stringify({
            chat: {
              messages: u.searchParams.has("date") ? [] : [comment],
              last_date: u.searchParams.has("date")
                ? null
                : { sec: 1789760000, usec: 1 },
              last_id: u.searchParams.has("date") ? null : comment.id,
            },
            emoji_chat: missingLikes
              ? []
              : [{ object_id: comment.id, emotions: { s1: 5, s6: 1 } }],
          });
        } else if (u.pathname.endsWith("/rooms"))
          body = `<a class="r-list__item" data-url="${url}" data-title="Обсуждаем новость"><span class="r-list__item-stat-messages">23</span></a>`;
        else {
          status = 404;
          bad.push(u.href);
        }
        return r.fulfill({ status, body, contentType: "text/plain" });
      }
      try {
        const file = path.join(
          u.pathname.startsWith("/shared/") ? path.dirname(root) : root,
          u.pathname === "/" ? "index.html" : u.pathname,
        );
        await r.fulfill({
          body: await fs.readFile(file),
          contentType: file.endsWith(".js")
            ? "text/javascript"
            : file.endsWith(".css")
              ? "text/css"
              : file.endsWith(".svg")
                ? "image/svg+xml"
                : "text/html",
        });
      } catch {
        await r.fulfill({ status: 404, body: "" });
      }
    });
    const page = await context.newPage();
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto(origin);
    await page.locator(".lead-story").waitFor();
    assert.equal(
      await page.locator(".lead-story h2").innerText(),
      "Главная новость дня",
    );
    assert.equal(
      await page.locator('#navigation a[href="#/rooms"]').count(),
      0,
    );
    assert.equal(await page.locator("input[type=search]").count(), 1);
    assert.equal(await page.locator(".editorial .story-grid .card").count(), 6);
    const editorialLinks = await page
      .locator(".editorial .story-link")
      .evaluateAll((els) => els.map((el) => el.getAttribute("href")));
    assert.equal(new Set(editorialLinks).size, 7);
    const remainingLinks = await page
      .locator(".more-news .story-link")
      .evaluateAll((els) => els.map((el) => el.getAttribute("href")));
    assert(!remainingLinks.some((href) => editorialLinks.includes(href)));
    await page.locator(".lead-story a").click();
    await page.locator(".article-stream > .reader .comment").first().waitFor();
    assert.equal(
      await page.locator(".article-stream > .reader .reaction").count(),
      6,
    );
    assert.equal(await page.evaluate(() => window.pwned), undefined);
    assert.equal(
      await page.locator(".reader iframe,.reader script").count(),
      0,
    );
    assert.equal(
      await page
        .locator(".article-stream > .reader .reaction strong")
        .first()
        .innerText(),
      "10",
    );
    assert.equal(await page.title(), 'Заголовок проверяемой статьи');
    await page.locator('#comment-text').fill('Гостевой черновик');
    await page.locator('#comment-form [type=submit]').click();
    await page.locator('#auth-form').waitFor();
    await page.keyboard.press('Escape');
    assert.equal(await page.locator('#comment-text').inputValue(), 'Гостевой черновик');
    async function register(login, name) {
      await page.locator('[data-action="auth"]').first().click();
      const secondary = page.locator('[data-action="register"]');
      assert.equal(
        await secondary.evaluate((b) =>
          Math.round(b.getBoundingClientRect().width),
        ),
        await page
          .locator("#auth-form [type=submit]")
          .evaluate((b) => Math.round(b.getBoundingClientRect().width)),
      );
      await secondary.hover();
      assert.equal(
        await secondary.evaluate((b) => getComputedStyle(b).textDecorationLine),
        "none",
      );
      await secondary.click();
      await page.locator("#auth-form [name=name]").fill(name);
      await page.locator("#auth-form [name=login]").fill(login);
      await page.locator("#auth-form [name=password]").fill("password123");
      await page.locator("#auth-form [type=submit]").click();
      await page.locator("#modal").waitFor({ state: "hidden" });
    }
    await register("reader1", "Первый читатель");
    await require('./personal-empty.helpers.cjs')(page, [['#/profile/saved', 'savedNews'], ['#/profile/ownComments', 'comments'], ['#/profile/ownReactions', 'reactions']]);
    await page.locator('.article-stream > .reader .comment').first().waitFor();
    const like = page.locator(
      '.article-stream > .reader .comment[data-comment^="ria:"] [data-action="comment-like"]',
    );
    assert.equal(await like.innerText(), "5");
    await like.click();
    await page.waitForFunction(
      () =>
        document
          .querySelector(
            '.article-stream > .reader .comment [data-action="comment-like"]',
          )
          .getAttribute("aria-pressed") === "true",
    );
    assert.equal(await like.innerText(), "6");
    assert.notEqual(
      await like.locator("svg").evaluate((el) => getComputedStyle(el).fill),
      "none",
    );
    assert.equal(
      await like.evaluate((el) => getComputedStyle(el).backgroundColor),
      "rgba(0, 0, 0, 0)",
    );
    await like.click();
    await page.waitForFunction(
      () =>
        document
          .querySelector(
            '.article-stream > .reader .comment [data-action="comment-like"]',
          )
          .getAttribute("aria-pressed") === "false",
    );
    missingLikes = true;
    await page.reload();
    await page.waitForFunction(
      () =>
        document.querySelector(
          '.article-stream > .reader .comment [data-action="comment-like"] span',
        )?.textContent === "0",
    );
    await like.click();
    await page.waitForFunction(
      () =>
        document.querySelector(
          '.article-stream > .reader .comment [data-action="comment-like"] span',
        )?.textContent === "1",
    );
    assert.match(
      await like.getAttribute("title"),
      /Полный счётчик лайков недоступен/,
    );
    await like.click();
    await page.waitForFunction(
      () =>
        document
          .querySelector(
            '.article-stream > .reader .comment [data-action="comment-like"]',
          )
          ?.getAttribute("aria-pressed") === "false",
    );
    missingLikes = false;
    await page.reload();
    await page.waitForFunction(
      () =>
        document.querySelector(
          '.article-stream > .reader .comment [data-action="comment-like"] span',
        )?.textContent === "5",
    );

    await page
      .locator(".article-stream > .reader .reaction[data-code=s1]")
      .click();
    await page.waitForFunction(
      () =>
        document.querySelector(".article-stream > .reader .reaction strong")
          ?.textContent === "11",
    );
    await page
      .locator(".article-stream > .reader .reaction[data-code=s2]")
      .click();
    await page.waitForFunction(
      () =>
        document.querySelector(
          ".article-stream > .reader .reaction[data-code=s1] strong",
        )?.textContent === "10",
    );
    assert.equal(
      await page
        .locator(".article-stream > .reader .reaction[data-code=s2] strong")
        .innerText(),
      "11",
    );
    await page.locator("[data-action=save]").first().click();
    await page.waitForFunction(
      () =>
        document
          .querySelector("[data-action=save]")
          .getAttribute("aria-pressed") === "true",
    );
    await page.locator("#comment-text").fill("Мой первый комментарий");
    await page.locator("#comment-form [type=submit]").click();
    await page.getByText("Мой первый комментарий", { exact: true }).waitFor();
    await page
      .locator(
        '.article-stream > .reader .comment[data-comment^="ria:"] [data-action=reply]',
      )
      .click();
    assert.equal(
      await page
        .locator("#comment-form")
        .evaluate((f) => f.previousElementSibling.dataset.comment),
      "ria:2118660370:" + comment.id,
    );
    await page.locator("#comment-text").fill("Локальный ответ внешнему автору");
    await page.locator("#comment-form [type=submit]").click();
    await page
      .getByText("Локальный ответ внешнему автору", { exact: true })
      .waitFor();
    assert.equal(
      await page
        .getByText("Локальный ответ внешнему автору", { exact: true })
        .evaluate(
          (p) =>
            p
              .closest(".article-stream > .reader .comment-thread")
              .previousElementSibling.querySelector(
                ".article-stream > .reader .comment",
              ).dataset.comment,
        ),
      "ria:2118660370:" + comment.id,
    );
    await page
      .locator('.article-stream > .reader .comment[data-comment^="local:"]')
      .filter({ hasText: "Локальный ответ внешнему автору" })
      .locator("[data-action=edit-comment]")
      .click();
    await page.locator("#comment-text").fill("Исправленный ответ");
    await page.locator("#comment-form [type=submit]").click();
    await page
      .locator(".article-stream > .reader .comment-text")
      .filter({ hasText: /^Исправленный ответ$/ })
      .waitFor();
    const parentReply = page
      .locator(".article-stream > .reader .comment")
      .filter({ hasText: "Исправленный ответ" });
    const parentRef = await parentReply.getAttribute("data-comment");
    await parentReply.locator('[data-action="reply"]').click();
    await page.locator("#comment-text").fill("Ответ на ответ");
    await page.setViewportSize({ width: 320, height: 900 });
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      "inline reply overflow",
    );
    await page.locator("#comment-form [type=submit]").click();
    await page.getByText("Ответ на ответ", { exact: true }).waitFor();
    assert.equal(
      await page
        .getByText("Ответ на ответ", { exact: true })
        .evaluate(
          (p) =>
            p
              .closest(".article-stream > .reader .comment-thread")
              .previousElementSibling.querySelector(
                ".article-stream > .reader .comment",
              ).dataset.comment,
        ),
      parentRef,
    );
    await page.setViewportSize({ width: 1440, height: 1000 });
    count = 20;
    await page.reload();
    await page.waitForFunction(
      () =>
        document.querySelector(
          ".article-stream > .reader .reaction[data-code=s2] strong",
        )?.textContent === "21",
    );
    await page
      .locator(".article-stream > .reader [data-action=comments-more]")
      .waitFor({ state: "hidden" });
    assert.equal(
      await page
        .locator('.article-stream > .reader .comment[data-comment^="ria:"]')
        .count(),
      1,
    );
    await page.reload();
    await page
      .locator(".article-stream > .reader .comment-text")
      .filter({ hasText: /^Исправленный ответ$/ })
      .waitFor();
    assert.equal(
      await page
        .locator(".article-stream > .reader .reaction[data-code=s2]")
        .getAttribute("aria-pressed"),
      "true",
    );
    const raw = await page.evaluate(async () =>
      JSON.stringify(await (await import("./fokus-store.js")).read()),
    );
    assert(!raw.includes("PRIVATE"));
    assert(!raw.includes("password123"));
    // A theme change must not discard a comment draft; language may translate only the interface.
    await page.locator("#comment-text").fill("Незавершённый текст");
    await page.locator("[data-action=theme]").click();
    assert.equal(
      await page.locator("#comment-text").inputValue(),
      "Незавершённый текст",
    );
    await page.locator("[data-action=language]").click();
    assert.equal(
      await page.locator("#comment-text").inputValue(),
      "Незавершённый текст",
    );
    assert.equal(
      await page.locator(".reader>h1").innerText(),
      "Заголовок проверяемой статьи",
    );
    assert.equal(await page.locator(".brand").first().innerText(), "Fokus");
    for (const width of [320, 375, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      assert(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        `article overflow ${width}`,
      );
      assert.equal(await page.locator("input[type=search]").count(), 1);
    }
    await page.locator(".account-link").click();
    await page.locator('[data-action="edit-profile"]').waitFor();
    assert.equal(await page.locator("#profile-form").count(), 0);
    assert.equal(
      await page
        .locator('.profile-account-actions [data-action="logout"]')
        .count(),
      1,
    );
    assert.equal(
      await page
        .locator('.profile-account-actions [data-action="delete-account"]')
        .count(),
      1,
    );
    await page.locator('[data-action="edit-profile"]').click();
    await page.locator('#profile-form [name="name"]').fill("Не сохранять");
    const avatarImage = await page.evaluate(() => {
      const canvas = document.createElement("canvas");
      canvas.width = canvas.height = 2;
      return canvas.toDataURL("image/png").split(",")[1];
    });
    await page.locator("#avatar-file").setInputFiles({
      name: "avatar.png",
      mimeType: "image/png",
      buffer: Buffer.from(avatarImage, "base64"),
    });
    await page.locator("#profile-avatar-preview img").waitFor();
    await page.setViewportSize({ width: 320, height: 900 });
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      "profile editor overflow",
    );
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.locator('#profile-form [data-action="close"]').click();
    assert.equal(
      await page.locator(".profile-heading h2").innerText(),
      "Первый читатель",
    );
    assert.equal(
      await page.locator(".profile-heading img.avatar").count(),
      0,
      "cancel does not save avatar",
    );
    await page.locator('[data-action="edit-profile"]').click();
    await page
      .locator('#profile-form [name="name"]')
      .fill("Новое имя читателя");
    await page.locator('#profile-form [type="submit"]').click();
    await page.locator("#modal").waitFor({ state: "hidden" });
    await page.waitForFunction(
      () =>
        document.querySelector(".profile-heading h2")?.textContent ===
        "Новое имя читателя",
    );
    await page.locator(".nav-saved").click();
    await page.waitForURL("**/#/profile/saved");
    await page
      .locator('.profile-tabs a[aria-current="page"][href="#/profile/saved"]')
      .waitFor();
    assert.equal(
      await page.locator('.page-head [data-action="refresh"]').count(),
      0,
    );
    await page.locator('a[href="#/profile/settings"]').click();

    for (const width of [320, 375, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      assert(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        `profile overflow ${width}`,
      );
    }
    await page.setViewportSize({ width: 320, height: 700 });
    assert.equal(await page.locator(".profile-tabs a").count(), 5);
    assert(await page.locator(".profile-tabs a span").first().isHidden());
    assert(await page.locator(".profile-tabs a svg").first().isVisible());
    assert(
      await page
        .locator(".profile-tabs")
        .evaluate((el) => el.scrollWidth <= el.clientWidth),
    );
    await page.locator('[data-action="menu"]').click();
    const drawer = page.locator("#mobile-navigation");
    await drawer.waitFor({ state: "visible" });
    assert(await drawer.locator(".nav-saved svg").isHidden());
    assert(
      await page.evaluate(() =>
        document
          .querySelector("#mobile-navigation")
          .contains(document.activeElement),
      ),
    );
    await page.keyboard.press("Escape");
    await drawer.waitFor({ state: "hidden" });
    assert(
      await page
        .locator('[data-action="menu"]')
        .evaluate((el) => el === document.activeElement),
    );
    await page.locator('[data-action="menu"]').click();
    await page.mouse.click(310, 450);
    await drawer.waitFor({ state: "hidden" });
    await page.locator('[data-action="menu"]').click();
    await drawer.locator('a[href="#/profile/saved"]').click();
    await page.waitForURL("**/#/profile/saved");
    await drawer.waitFor({ state: "hidden" });
    assert.equal(
      await page.evaluate(() =>
        document.body.classList.contains("mobile-nav-open"),
      ),
      false,
    );
    await page.locator('[data-action="menu"]').click();
    await page.setViewportSize({ width: 1440, height: 900 });
    await drawer.waitFor({ state: "hidden" });
    await page.locator('a[href="#/profile/history"]').click();
    await page.locator(".news-grid .card").first().waitFor();
    await page.locator('a[href="#/profile/settings"]').click();
    await page.locator("[data-action=logout]").click();
    assert.match(
      await page.locator("[data-action=confirm]").innerText(),
      /^(Выйти|Sign out)$/,
    );
    assert.equal(
      await page
        .locator(".dialog-actions")
        .evaluate((el) => getComputedStyle(el).justifyContent),
      "flex-start",
    );
    await page.locator("[data-action=confirm]").click();
    await page.locator(".account-link").waitFor({ state: "hidden" });
    await register("reader2", "Второй читатель");
    await page.goto(origin + "/#/article/20260919/test-2118660370.html");
    await page.locator(".article-stream > .reader .comment").first().waitFor();
    assert.equal(
      await page
        .locator(".article-stream > .reader .reaction[data-code=s2]")
        .getAttribute("aria-pressed"),
      "false",
    );
    assert.equal(
      await page
        .locator("[data-action=save]")
        .first()
        .getAttribute("aria-pressed"),
      "false",
    );
    assert.equal(await page.locator("[data-action=edit-comment]").count(), 0);
    await page
      .locator(".article-stream > .reader .reaction[data-code=s2]")
      .click();
    await page.waitForFunction(
      () =>
        document.querySelector(
          ".article-stream > .reader .reaction[data-code=s2] strong",
        )?.textContent === "22",
    );
    await page
      .locator('.article-stream > .reader .comment[data-comment^="local:"]')
      .first()
      .locator("[data-action=reply]")
      .click();
    await page.locator("#comment-text").fill("Ответ второго аккаунта");
    await page.locator("#comment-form [type=submit]").click();
    await page.getByText("Ответ второго аккаунта", { exact: true }).waitFor();
    await page.locator(".account-link").click();
    await page.locator("[data-action=delete-account]").click();
    assert.match(
      await page.locator("[data-action=confirm]").innerText(),
      /^(Удалить аккаунт|Delete account)$/,
    );
    await page.locator("[data-action=confirm]").click();
    await page.locator(".lead-story").waitFor();
    assert.equal(await page.locator(".account-link").count(), 0);
    // Search is singular; navigation and all responsive widths work on the homepage.
    for (const width of [320, 375, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      assert(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        `home overflow ${width}`,
      );
    }
    await page.locator("#search-input").fill("Главная");
    await page.locator("#search").press("Enter");
    await page.locator(".news-grid .card").first().waitFor();
    assert.equal(await page.locator("input[type=search]").count(), 1);
    await page.getByText("Архивный результат 0", { exact: true }).waitFor();
    assert.equal(
      await page.locator('.page-head [data-action="refresh"]').count(),
      1,
    );
    assert.equal(
      await page.locator(".news-grid .card h2").innerText(),
      "Архивный результат 0",
    );
    await page.locator('[data-action="more"]').click();
    await page.getByText("Архивный результат 20", { exact: true }).waitFor();
    assert.equal(await page.locator(".news-grid .card").count(), 2);
    assert.equal(await page.locator('[data-action="more"]').count(), 0);
    await page.reload();
    await page.getByText("Архивный результат 0", { exact: true }).waitFor();
    assert.equal(
      await page.locator('.page-head [data-action="refresh"]').count(),
      1,
    );
    await page.goto(origin + "/#/article/20260919/test-2118660370.html");
    await page.locator(".article-stream > .reader .comment").first().waitFor();
    await page.waitForFunction(
      () =>
        document.querySelector(
          ".article-stream > .reader .reaction[data-code=s2] strong",
        )?.textContent === "21",
    );
    assert.equal(
      await page
        .locator(".article-stream > .reader .reaction[data-code=s2] strong")
        .innerText(),
      "21",
    );
    // Failed independent panels retain source errors instead of pretending to be empty.
    failComments = true;
    await page.reload();
    await page.locator("#discussion .notice").waitFor();
    assert.equal(
      await page
        .locator('.article-stream > .reader .comment[data-comment^="ria:"]')
        .count(),
      1,
    );
    await page.evaluate(async () => {
      const s = await import("./fokus-store.js");
      await s.change((d) => {
        d.cache = [];
      }, false);
    });
    await page.reload();
    await page.locator("#discussion .empty-state").waitFor();
    assert.equal(
      await page.locator(".article-stream > .reader > .article-body").count(),
      1,
    );
    failComments = false;
    await page.evaluate(async () => {
      const store = await import("./fokus-store.js");
      await store.change((s) => {
        s.cache = [];
      }, false);
    });
    failArticle = true;
    await page.reload();
    await page.waitForURL('**/#/latest');
    assert.equal(await page.locator(".article-body").count(), 0);
    assert.equal(await page.locator('.error-text').count(), 0);
    ((failArticle = false), (missingLikes = false), (failNext = false));
    await page.goto(origin + '/#/article/20260919/test-2118660370.html');
    await page.locator(".article-body").waitFor();
    // Broken storage never prevents reading and never reports a successful signup.
    const blocked = await context.newPage();
    blocked.on("pageerror", (e) => errors.push(e.message));
    await blocked.addInitScript(() => {
      IDBFactory.prototype.open = function () {
        throw new DOMException("Storage blocked", "SecurityError");
      };
    });
    await blocked.goto(origin);
    await blocked.locator(".lead-story").waitFor();
    await blocked.locator(".notice").waitFor();
    await blocked.locator("[data-action=auth]").first().click();
    await blocked.locator("[data-action=register]").click();
    await blocked.locator("#auth-form [name=name]").fill("Storage test");
    await blocked.locator("#auth-form [name=login]").fill("storageuser");
    await blocked.locator("#auth-form [name=password]").fill("password123");
    await blocked.locator("#auth-form [type=submit]").click();
    await blocked
      .locator("#auth-form .form-error")
      .filter({ hasText: /сохранить|save/i })
      .waitFor();
    assert.equal(await blocked.locator("dialog[open]").count(), 1);
    await blocked.close();
    // Direct links populate the rail, then append full stories without navigation.
    await page.evaluate(async () => {
      const store = await import("./fokus-store.js");
      await store.change((s) => {
        s.cache = [];
      }, false);
    });
    const stream = await context.newPage();
    stream.on("pageerror", (error) => errors.push(error.message));
    await stream.goto(origin + "/#/article/20260919/test-2118660370.html");
    await stream.locator(".article-aside .latest-item").first().waitFor();
    assert.equal(
      await stream
        .locator(".article-aside-sticky")
        .evaluate((el) => getComputedStyle(el).position),
      "sticky",
    );
    failNext = true;
    await stream.locator("#next-article").scrollIntoViewIfNeeded();
    await stream.locator(".continued-story .article-body").first().waitFor();
    assert.equal(await stream.locator("#next-article .error-text").count(), 0);
    assert.equal(await stream.locator('.continued-story[data-article-id="2118660371"], .continued-story[data-article-id="2118660372"]').count(), 0);
    failNext = false;
    const nextId = await stream
      .locator(".continued-story")
      .first()
      .getAttribute("data-article-id");
    assert.notEqual(nextId, "2118660370");
    assert.equal(
      await stream
        .locator(".continued-story")
        .first()
        .locator('[data-action="save"]')
        .getAttribute("data-article"),
      nextId,
    );
    assert.equal(
      new URL(stream.url()).hash,
      "#/article/20260919/test-2118660370.html",
    );
    assert.equal(await stream.locator("#discussion").count(), 1);
    await stream.evaluate(async () => {
      const store = await import("./fokus-store.js");
      const db = await store.read();
      store.signIn(db.accounts[0].id);
      navigator.clipboard.writeText = async (value) => {
        window.sharedStory = value;
      };
    });
    await stream.locator('[data-action="language"]').click();
    const nextSave = stream
      .locator(".continued-story")
      .first()
      .locator('[data-action="save"]');
    await nextSave.click();
    await stream.waitForFunction(
      (id) =>
        document
          .querySelector(
            `.continued-story [data-action="save"][data-article="${id}"]`,
          )
          ?.getAttribute("aria-pressed") === "true",
      nextId,
    );
    assert(
      await stream.evaluate(
        async (id) =>
          (await (await import("./fokus-store.js")).read()).bookmarks.some(
            (b) => b.article.id === id,
          ),
        nextId,
      ),
    );
    await stream
      .locator(".continued-story")
      .first()
      .locator('[data-action="share"]')
      .click();
    assert(
      (await stream.evaluate(() => window.sharedStory)).endsWith(
        `test-${nextId}.html`,
      ),
    );
    const nextStory = stream.locator(
      `.continued-story[data-article-id="${nextId}"]`,
    );
    const firstStory = stream.locator(".article-stream > .reader");
    await nextStory.locator(".comment").first().waitFor();
    assert.equal(await nextStory.locator(".reaction").count(), 6);
    await stream.waitForFunction(
      (id) =>
        document.querySelector(`[data-article-id="${id}"] .reaction strong`)
          ?.textContent === "20",
      nextId,
    );
    const firstReactions = await firstStory
      .locator(".reaction-grid")
      .innerText();
    await nextStory.locator('.reaction[data-code="s1"]').click();
    await stream.waitForFunction(
      (id) =>
        document
          .querySelector(`[data-article-id="${id}"] .reaction[data-code="s1"]`)
          ?.getAttribute("aria-pressed") === "true",
      nextId,
    );
    assert.equal(
      await firstStory.locator(".reaction-grid").innerText(),
      firstReactions,
    );
    await firstStory.locator("textarea").fill("Черновик первой статьи");
    await nextStory.locator("textarea").fill("Черновик следующей статьи");
    assert.equal(
      await nextStory
        .locator('.section-title [data-action$="-refresh"]')
        .count(),
      0,
    );
    await stream.locator('[data-action="language"]').click();
    assert.equal(
      await firstStory.locator("textarea").inputValue(),
      "Черновик первой статьи",
    );
    assert.equal(
      await nextStory.locator("textarea").inputValue(),
      "Черновик следующей статьи",
    );
    await nextStory.locator('.comment-form [type="submit"]').click();
    await nextStory
      .locator(".comment-text")
      .filter({ hasText: /^Черновик следующей статьи$/ })
      .waitFor();
    assert.equal(
      await firstStory
        .locator(".comment-text")
        .filter({ hasText: /^Черновик следующей статьи$/ })
        .count(),
      0,
    );
    const nextExternal = nextStory
      .locator('.comment[data-comment^="ria:"]')
      .first();
    const externalRef = await nextExternal.getAttribute("data-comment");
    await nextExternal.locator('[data-action="reply"]').click();
    assert.equal(
      await nextStory
        .locator(".comment-form")
        .evaluate((el) => el.previousElementSibling.dataset.comment),
      externalRef,
    );
    await nextStory.locator("textarea").fill("Ответ в следующей статье");
    await nextStory.locator('.comment-form [type="submit"]').click();
    const localReply = nextStory.locator(".comment").filter({
      has: stream
        .locator(".comment-text")
        .filter({ hasText: /^Ответ в следующей статье$/ }),
    });
    await localReply.waitFor();
    await localReply.locator('[data-action="edit-comment"]').click();
    await nextStory.locator("textarea").fill("Отредактированный ответ");
    await nextStory.locator('.comment-form [type="submit"]').click();
    await nextStory
      .getByText("Отредактированный ответ", { exact: true })
      .waitFor();
    await nextExternal.locator('[data-action="comment-like"]').click();
    await stream.waitForFunction(
      (id) =>
        document
          .querySelector(
            `[data-article-id="${id}"] .comment[data-comment^="ria:"] [data-action="comment-like"]`,
          )
          ?.getAttribute("aria-pressed") === "true",
      nextId,
    );
    for (const reader of [firstStory, nextStory]) {
      assert(
        !/РИА|ria\.ru|RIA|в этом браузере|in this browser/i.test(
          await reader.innerText(),
        ),
      );
      assert.equal(await reader.locator('a[href*="ria.ru"]').count(), 0);
    }
    const firstComments = await firstStory.locator(".comment").count();
    await nextStory
      .locator('[data-action="comments-more"]')
      .waitFor({ state: "hidden" });
    assert.equal(await firstStory.locator(".comment").count(), firstComments);
    assert.equal(
      await firstStory.locator("textarea").inputValue(),
      "Черновик первой статьи",
    );
    assert(
      await stream.evaluate(async (id) => {
        const db = await (await import("./fokus-store.js")).read();
        return db.comments
          .filter((c) => c.text === "Отредактированный ответ")
          .every((c) => c.articleId === id);
      }, nextId),
    );
    const editedReply = nextStory.locator(".comment").filter({
      has: stream.getByText("Отредактированный ответ", { exact: true }),
    });
    assert(
      !(await nextStory
        .locator(".comment-actions")
        .allTextContents()
        .then((items) => /Не нравится|Dislike/.test(items.join(" ")))),
    );
    const separate = nextStory.locator(".next-story-heading .text-link");
    await separate.hover();
    assert.equal(
      await separate.evaluate((el) => getComputedStyle(el).textDecorationLine),
      "none",
    );
    await editedReply.locator('[data-action="delete-comment"]').click();
    assert.match(
      await stream.locator('[data-action="confirm"]').innerText(),
      /^(Удалить|Delete)$/,
    );
    const deletedRef = await editedReply.getAttribute("data-comment");
    await stream.locator('[data-action="confirm"]').click();
    await nextStory
      .locator(`[data-comment="${deletedRef}"]`)
      .waitFor({ state: "detached" });
    await nextStory
      .getByText("Отредактированный ответ", { exact: true })
      .waitFor({ state: "hidden" });
    assert.equal(await firstStory.locator(".comment").count(), firstComments);
    const allIds = await stream
      .locator("[id]")
      .evaluateAll((els) => els.map((el) => el.id));
    assert.equal(
      new Set(allIds).size,
      allIds.length,
      "unique IDs for each discussion and composer",
    );
    await firstStory.locator('[data-action="reply"]').first().click();
    await firstStory.locator('textarea').fill('Черновик первого обсуждения');
    await nextStory.locator('[data-action="reply"]').first().click();
    assert.equal(await firstStory.locator('.reply-target').count(), 0);
    assert.equal(await nextStory.locator('.reply-target').count(), 1);
    await firstStory.locator('textarea').focus();
    assert.equal(await nextStory.locator('.reply-target').count(), 0);
    assert.equal(await firstStory.locator('textarea').inputValue(), 'Черновик первого обсуждения');
    const beforeNext = await stream.locator(".continued-story").count();
    await stream.locator("#next-article").scrollIntoViewIfNeeded();
    await stream.waitForFunction(
      (n) => document.querySelectorAll(".continued-story").length > n,
      beforeNext,
    );
    const ids = await stream
      .locator(".continued-story")
      .evaluateAll((els) => els.map((el) => el.dataset.articleId));
    assert.equal(new Set(ids).size, ids.length);
    for (const width of [320, 375, 768, 1024, 1440]) {
      await stream.setViewportSize({ width, height: 900 });
      assert(
        await stream.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        `stream overflow ${width}`,
      );
    }
    await stream.evaluate(async () => (await import('./fokus-store.js')).change(s => { s.cache = []; }, false));
    const exhausted = await context.newPage();
    await exhausted.route('**/_fokus/ria/feed', r => r.fulfill({ body: '<rss><channel>' + [9000,9001,9002].map(id => `<item><title>Story ${id}</title><link>https://ria.ru/20260919/test-${id}.html</link></item>`).join('') + '</channel></rss>', contentType: 'text/xml' }));
    await exhausted.route('**/_fokus/ria/home', r => r.fulfill({ body: '<div class="cell"><a href="https://ria.ru/20260919/test-9000.html"></a><span class="cell-video__title">Story 9000</span></div>' }));
    const failedIds = [];
    await exhausted.route('**/_fokus/ria/article/**', r => {
      if (r.request().url().endsWith('test-9000.html')) return r.fulfill({ body: article });
      failedIds.push(r.request().url());
      return r.fulfill({ status: 503, body: '' });
    });
    await exhausted.goto(origin + '/#/article/20260919/test-9000.html');
    await exhausted.locator('.article-body').waitFor();
    await exhausted.locator('#next-article').scrollIntoViewIfNeeded();
    await exhausted.locator('#next-article a[href="#/latest"]').waitFor();
    assert.equal(await exhausted.locator('#next-article .error-text').count(), 0);
    assert.equal(failedIds.length, 2);
    assert.equal(new Set(failedIds).size, 2, 'Each failed candidate is tried only once');
    await exhausted.evaluate(async () => (await import('./fokus-store.js')).change(s => { s.cache = []; }, false));
    failedIds.length = 0;
    await exhausted.goto(origin + '/#/article/20260919/test-9001.html');
    await exhausted.waitForURL('**/#/article/20260919/test-9000.html');
    await exhausted.locator('.article-body').waitFor();
    assert.equal(await exhausted.locator('.error-text').count(), 0, 'Direct article failure redirects silently');
    assert.equal(failedIds.length, 1);
    await exhausted.close();
    await stream.goto(origin + "/#/home");
    await stream.locator(".lead-story").waitFor();
    assert.equal(await stream.locator(".continued-story").count(), 0);
    await stream.goto(origin + "/#/saved");
    await stream.waitForURL("**/#/profile/saved");
    await stream.goto(origin + "/#/rooms");
    await stream.waitForURL("**/#/home");
    await stream.locator(".lead-story").waitFor();
    assert.equal(
      await stream.locator('.page-head [data-action="refresh"]').count(),
      1,
    );
    await stream.close();
    assert.deepEqual(bad, []);
    assert.deepEqual(errors, []);
    assert(!requests.some((x) => /add_emoji|\/like\/|\/chat\/add/.test(x)));
    console.log("Fokus browser checks passed");
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
