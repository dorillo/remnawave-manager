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
const article = `<h1 class="article__title">Заголовок проверяемой статьи</h1><meta property="article:published_time" content="2026-09-19T12:00:00+03:00"><div class="article__body"><div class="article__block" data-type="text"><div class="article__text">Полный текст новости <strong>с выделением</strong>${malicious}<a href="javascript:alert(1)">опасная ссылка</a></div></div><div class="article__block" data-type="quote"><div class="article__quote-text">Содержательная цитата.</div></div></div>`;
let count = 10,
  failComments = false,
  failArticle = false;
const dynamics = () =>
  `<div class="article__userbar-emoji"><div class="emoji" data-id="2118660370">${["s1", "s6", "s2", "s3", "s4", "s5"].map((x) => `<a data-type="${x}"><span class="m-value">${count}</span></a>`).join("")}</div></div>`;
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
        else if (u.pathname.includes("/article/")) {
          body = article;
          if (failArticle) status = 503;
        } else if (u.pathname.includes("/dynamics/")) body = dynamics();
        else if (u.pathname.endsWith("/comments")) {
          if (failComments) status = 503;
          body = JSON.stringify({
            chat: {
              messages: u.searchParams.has("date") ? [] : [comment],
              last_date: u.searchParams.has("date")
                ? null
                : { sec: 1789760000, usec: 1 },
              last_id: u.searchParams.has("date") ? null : comment.id,
            },
            emoji_chat: [{ object_id: comment.id, emotions: { s1: 5 } }],
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
          root,
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
    assert.equal(await page.locator("input[type=search]").count(), 1);
    await page.locator(".lead-story a").click();
    await page.locator(".comment").first().waitFor();
    assert.equal(await page.locator(".reaction").count(), 6);
    assert.equal(await page.evaluate(() => window.pwned), undefined);
    assert.equal(
      await page.locator(".reader iframe,.reader script").count(),
      0,
    );
    assert.equal(
      await page.locator(".reaction strong").first().innerText(),
      "10",
    );
    async function register(login, name) {
      await page.locator('[data-action="auth"]').first().click();
      await page.locator('[data-action="register"]').click();
      await page.locator("#auth-form [name=name]").fill(name);
      await page.locator("#auth-form [name=login]").fill(login);
      await page.locator("#auth-form [name=password]").fill("password123");
      await page.locator("#auth-form [type=submit]").click();
      await page.locator("dialog").waitFor({ state: "hidden" });
    }
    await register("reader1", "Первый читатель");
    await page.locator(".reaction[data-code=s1]").click();
    await page.waitForFunction(
      () => document.querySelector(".reaction strong").textContent === "11",
    );
    await page.locator(".reaction[data-code=s2]").click();
    await page.waitForFunction(
      () =>
        document.querySelector(".reaction[data-code=s1] strong").textContent ===
        "10",
    );
    assert.equal(
      await page.locator(".reaction[data-code=s2] strong").innerText(),
      "11",
    );
    await page.locator("[data-action=save]").click();
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
      .locator('.comment[data-comment^="ria:"] [data-action=reply]')
      .click();
    await page.locator("#comment-text").fill("Локальный ответ внешнему автору");
    await page.locator("#comment-form [type=submit]").click();
    await page
      .getByText("Локальный ответ внешнему автору", { exact: true })
      .waitFor();
    await page
      .locator('.comment[data-comment^="local:"]')
      .first()
      .locator("[data-action=edit-comment]")
      .click();
    await page.locator("#comment-text").fill("Исправленный ответ");
    await page.locator("#comment-form [type=submit]").click();
    await page.getByText("Исправленный ответ", { exact: true }).waitFor();
    count = 20;
    await page.locator("[data-action=reactions-refresh]").click();
    await page.waitForFunction(
      () =>
        document.querySelector(".reaction[data-code=s2] strong").textContent ===
        "21",
    );
    await page.locator("[data-action=comments-more]").click();
    await page
      .locator("[data-action=comments-more]")
      .waitFor({ state: "hidden" });
    assert.equal(
      await page.locator('.comment[data-comment^="ria:"]').count(),
      1,
    );
    await page.reload();
    await page.getByText("Исправленный ответ", { exact: true }).waitFor();
    assert.equal(
      await page
        .locator(".reaction[data-code=s2]")
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
    for (const width of [320, 375, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      assert(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        `profile overflow ${width}`,
      );
    }
    await page.locator('a[href="#/profile/history"]').click();
    await page.locator(".news-grid .card").first().waitFor();
    await page.locator('a[href="#/profile/settings"]').click();
    await page.locator("[data-action=logout]").click();
    await page.locator("[data-action=confirm]").click();
    await page.locator(".account-link").waitFor({ state: "hidden" });
    await register("reader2", "Второй читатель");
    await page.goto(origin + "/#/article/20260919/test-2118660370.html");
    await page.locator(".comment").first().waitFor();
    assert.equal(
      await page
        .locator(".reaction[data-code=s2]")
        .getAttribute("aria-pressed"),
      "false",
    );
    assert.equal(
      await page.locator("[data-action=save]").getAttribute("aria-pressed"),
      "false",
    );
    assert.equal(await page.locator("[data-action=edit-comment]").count(), 0);
    await page.locator(".reaction[data-code=s2]").click();
    await page.waitForFunction(
      () =>
        document.querySelector(".reaction[data-code=s2] strong").textContent ===
        "22",
    );
    await page
      .locator('.comment[data-comment^="local:"]')
      .first()
      .locator("[data-action=reply]")
      .click();
    await page.locator("#comment-text").fill("Ответ второго аккаунта");
    await page.locator("#comment-form [type=submit]").click();
    await page.getByText("Ответ второго аккаунта", { exact: true }).waitFor();
    await page.locator(".account-link").click();
    await page.locator("[data-action=delete-account]").click();
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
    await page.goto(origin + "/#/article/20260919/test-2118660370.html");
    await page.locator(".comment").first().waitFor();
    await page.waitForFunction(
      () =>
        document.querySelector(".reaction[data-code=s2] strong")
          ?.textContent === "21",
    );
    assert.equal(
      await page.locator(".reaction[data-code=s2] strong").innerText(),
      "21",
    );
    // Failed independent panels retain source errors instead of pretending to be empty.
    failComments = true;
    await page.locator("[data-action=comments-refresh]").click();
    await page.locator("#discussion .notice").waitFor();
    assert.equal(
      await page.locator('.comment[data-comment^="ria:"]').count(),
      1,
    );
    await page.evaluate(async () => {
      const s = await import("./fokus-store.js");
      await s.change((d) => {
        d.cache = [];
      }, false);
    });
    await page.locator("[data-action=comments-refresh]").click();
    await page.locator("#discussion .empty-state").waitFor();
    assert.equal(await page.locator(".article-body").count(), 1);
    failComments = false;
    failArticle = true;
    await page.reload();
    await page.locator("main [data-action=refresh]").waitFor();
    assert.equal(await page.locator(".article-body").count(), 0);
    failArticle = false;
    await page.locator("main [data-action=refresh]").click();
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
