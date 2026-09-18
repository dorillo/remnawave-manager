const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("./.tmp/node_modules/playwright-core");
const root = path.resolve(
  __dirname,
  "../src/remnawave_manager/data/disguises/05-field-notes",
);
let browser,
  unavailable = false,
  delayedArticle = false,
  releaseArticle;
const html = `<p><b>Земля</b> — третья планета Солнечной системы. Энциклопедия помогает исследовать мир и находить связи между понятиями.</p><table class="infobox"><tr><th colspan="2">Земля</th></tr><tr><td>Тип</td><td>Планета</td></tr><tr><td>Спутник</td><td><a href="/wiki/Луна">Луна</a></td></tr></table><h2><span id="Строение">Строение</span></h2><p>Планета состоит из нескольких слоёв.<sup><a href="#cite_note-1">[1]</a></sup></p>${"<p>Земля — наш общий дом. Изучение планеты охватывает географию, биологию и астрономию.</p>".repeat(12)}<h3 id="Ядро">Ядро</h3><table class="wikitable"><tr>${"<th>Длинный заголовок таблицы</th>".repeat(8)}</tr><tr>${"<td>Пример данных</td>".repeat(8)}</tr></table><h2 id="Примечания">Примечания</h2><ol><li id="cite_note-1">Научный источник <a href="#Строение">↑</a></li></ol><script>window.hacked=1</script><img src="javascript:alert(1)" onerror="window.hacked=2"><a href="javascript:alert(1)">bad</a><a href="http://example.com/plain">insecure</a><a href="https://example.com/plain">external</a><div id="app" class="header">Безопасный текст</div>`;
(async () => {
  browser = await chromium.launch({
    executablePath:
      process.env.SVOD_BROWSER ||
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    headless: true,
  });
  const context = await browser.newContext({
      locale: "ru-RU",
      viewport: { width: 1440, height: 1000 },
    }),
    errors = [];
  await context.route("**/*", async (route) => {
    const u = new URL(route.request().url());
    assert.equal(route.request().method(), "GET");
    if (u.pathname.startsWith("/_svod/")) {
      if (delayedArticle && u.pathname.endsWith("/article"))
        await new Promise((resolve) => {
          releaseArticle = resolve;
        });
      if (unavailable) return route.fulfill({ status: 503, body: "{}" });
      if (u.searchParams.get("q") === "empty")
        return route.fulfill({
          contentType: "application/json",
          body: '{"query":{"search":[]}}',
        });
      if (u.searchParams.get("title") === "Missing")
        return route.fulfill({
          contentType: "application/json",
          body: '{"error":{"code":"missingtitle"}}',
        });
      let result;
      if (u.pathname.endsWith("/search"))
        result = {
          query: {
            search: [
              {
                pageid: 1,
                title: "Земля",
                snippet: "Третья <b>планета</b> Солнечной системы.",
              },
              {
                pageid: 2,
                title: "Солнечная система",
                snippet: "Планеты, спутники и звёзды.",
              },
            ],
          },
        };
      else if (u.pathname.endsWith("/article"))
        result = {
          parse: {
            pageid: u.searchParams.get("title") === "Луна" ? 2 : 1,
            title: u.searchParams.get("title"),
            text: { "*": html },
            categories: [{ "*": "Планеты" }],
            revid: 42,
          },
        };
      else if (u.pathname.endsWith("/random"))
        result = { query: { random: [{ title: "Земля" }] } };
      else
        result = {
          query: { categorymembers: [{ pageid: 1, title: "Земля", ns: 0 }] },
        };
      return route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(result),
      });
    }
    const p = path.resolve(
      root,
      "." + (u.pathname === "/" ? "/index.html" : u.pathname),
    );
    if (!p.startsWith(root + path.sep))
      return route.fulfill({ status: 404, body: "" });
    try {
      return route.fulfill({
        body: await fs.readFile(p),
        contentType: p.endsWith(".js")
          ? "text/javascript"
          : p.endsWith(".css")
            ? "text/css"
            : p.endsWith(".svg")
              ? "image/svg+xml"
              : "text/html",
      });
    } catch {
      return route.fulfill({ status: 404, body: "" });
    }
  });
  const page = await context.newPage();
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("https://svod.test/");
  await page.getByRole("heading", { name: "Земля", exact: true }).waitFor();
  assert.equal(await page.getByText(/Википед|Wikipedia/).count(), 0);
  assert.equal(await page.getByRole("search").count(), 1);
  await page.keyboard.press("Control+k");
  assert.equal(
    await page
      .locator(".header .search-form input")
      .evaluate((n) => n === document.activeElement),
    true,
  );
  assert.equal(await page.locator("main .search-form").count(), 0);
  for (const width of [320, 375, 600, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    assert.ok(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      `home overflow at ${width}`,
    );
  }
  assert.ok(
    await page.locator(".header .search-form").evaluate((form) => {
      const rect = form.getBoundingClientRect();
      return Math.abs(rect.left + rect.width / 2 - innerWidth / 2) < 1;
    }),
  );
  await page.screenshot({ path: "/tmp/svod-home-desktop.png", fullPage: true });
  await page.setViewportSize({ width: 375, height: 850 });
  await page.getByRole("button", { name: "Навигация", exact: true }).click();
  await page.waitForFunction(
    () => document.querySelector(".sidebar").getBoundingClientRect().left > -1,
  );
  const drawerBox = await page.locator(".sidebar").evaluate((n) => {
    const r = n.getBoundingClientRect();
    return {
      left: r.left,
      top: r.top,
      height: r.height,
      viewport: innerHeight,
    };
  });
  assert.ok(drawerBox.left >= -1 && drawerBox.top === 0);
  assert.ok(drawerBox.height >= drawerBox.viewport);
  assert.equal(await page.locator(".nav-backdrop").isVisible(), true);
  assert.equal(await page.locator(".header").getAttribute("inert"), "");
  assert.equal(await page.locator("main").getAttribute("inert"), "");
  await page.waitForTimeout(300);
  assert.equal(
    await page
      .locator(".sidebar")
      .evaluate((n) => n.contains(document.activeElement)),
    true,
  );
  await page.screenshot({ path: "/tmp/svod-mobile-navigation.png" });
  await page.getByRole("link", { name: "Моя библиотека", exact: true }).click();
  await page.locator(".empty .primary").waitFor();
  assert.equal(
    await page
      .locator("#app")
      .evaluate((n) => n.classList.contains("nav-open")),
    false,
  );
  assert.equal(
    await page
      .locator('.sidebar a[aria-current="page"]')
      .getAttribute("href"),
    "#/library",
  );
  await page.locator(".empty .primary").click();
  await page.locator(".auth-switch").click();
  const formButtons = await page.evaluate(() =>
    [
      document.querySelector("dialog form .primary"),
      document.querySelector(".auth-switch"),
    ].map((n) => {
      const r = n.getBoundingClientRect();
      return [r.x, r.width, r.height];
    }),
  );
  assert.deepEqual(formButtons[0], formButtons[1]);
  await page.screenshot({ path: "/tmp/svod-register-mobile.png" });
  await page.getByRole("button", { name: "Закрыть", exact: true }).click();
  await page.goto("https://svod.test/");
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.getByRole("heading", { name: "Земля", exact: true }).waitFor();
  delayedArticle = true;
  await page.getByRole("link", { name: "Земля", exact: true }).click();
  await page.locator(".load-status").waitFor();
  assert.ok(
    await page.locator(".load-status").evaluate((n) => {
      const r = n.getBoundingClientRect(),
        m = n.parentElement.getBoundingClientRect();
      const range = document.createRange();
      range.selectNodeContents(n);
      const text = range.getBoundingClientRect();
      return (
        Math.abs((text.left + text.right - r.left - r.right) / 2) < 2 &&
        Math.abs((text.top + text.bottom - r.top - r.bottom) / 2) < 2 &&
        r.height > 300
      );
    }),
  );
  delayedArticle = false;
  releaseArticle();
  await page.locator(".wiki-content").waitFor();
  assert.equal(
    await page
      .locator('a[href*="wikipedia.org"],a[href*="wikimedia.org"]')
      .count(),
    0,
  );
  // A tall infobox must not push the next section below it.
  await page.evaluate(async () => {
    const { renderArticle } = await import("/svod-article.js");
    const { container } = renderArticle({
      title: "Проверка",
      html:
        '<table class="infobox"><tr><td>' +
        "<p>Данные</p>".repeat(20) +
        "</td></tr></table><p>Введение</p><h2>Раздел</h2><p>Продолжение текста</p>",
    });
    container.id = "layout-fixture";
    document.querySelector(".article-page").append(container);
  });
  assert.ok(
    await page
      .locator("#layout-fixture")
      .evaluate(
        (n) =>
          n.querySelector("h2").getBoundingClientRect().top <
          n.querySelector("table").getBoundingClientRect().bottom - 200,
      ),
  );
  await page.locator("#layout-fixture").evaluate((n) => n.remove());
  const secondHeading = page.locator(".wiki-content h2").nth(1);
  await secondHeading.evaluate((n) => n.scrollIntoView());
  await page.waitForTimeout(300);
  const tocItems = await page.locator(".toc button").evaluateAll((items) =>
    items.map((item) => ({
      text: item.textContent,
      section: item.dataset.section,
      active: item.classList.contains("active"),
    })),
  );
  assert.equal(
    tocItems.find((item) => item.active)?.text,
    "Примечания",
    JSON.stringify(tocItems),
  );
  const activeSection = page.locator(".toc button.active");
  assert.equal(await activeSection.getAttribute("aria-current"), "location");
  assert.equal(await page.evaluate(() => window.hacked), undefined);
  assert.equal(await page.locator("#app").count(), 1);
  assert.equal(
    await page.locator('.wiki-content [href^="javascript"]').count(),
    0,
  );
  assert.equal(
    await page.getByRole("link", { name: "insecure", exact: true }).count(),
    0,
  );
  const external = page.getByRole("link", { name: "external", exact: true });
  assert.equal(await external.getAttribute("target"), "_blank");
  assert.equal(await external.getAttribute("rel"), "noopener noreferrer");
  await page.screenshot({
    path: "/tmp/svod-article-desktop.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "Сохранить", exact: true }).click();
  await page
    .getByRole("button", { name: "Создать аккаунт", exact: true })
    .click();
  await page.getByLabel("Имя", { exact: true }).fill("Читатель");
  await page.getByLabel("Логин", { exact: true }).fill("reader");
  await page.getByLabel("Пароль", { exact: true }).fill("Password123");
  await page.locator("dialog form button[type=submit]").click();
  await page.locator("dialog").waitFor({ state: "detached" });
  await page
    .getByRole("button", { name: "В библиотеке", exact: true })
    .waitFor();
  await page.getByRole("button", { name: "В библиотеке", exact: true }).click();
  await page
    .getByRole("heading", {
      name: "Удалить статью из библиотеки? Она также будет удалена из коллекций.",
      exact: true,
    })
    .waitFor();
  await page.getByRole("button", { name: "Отмена", exact: true }).click();
  await page
    .getByRole("button", { name: "В библиотеке", exact: true })
    .waitFor();
  await page.getByRole("button", { name: "В коллекцию", exact: true }).click();
  await page
    .getByRole("button", { name: "Новая коллекция", exact: true })
    .click();
  await page.getByLabel("Название коллекции").fill("Космос");
  await page
    .locator("dialog")
    .last()
    .getByRole("button", { name: "Сохранить", exact: true })
    .click();
  await page.getByLabel("Космос").check();
  await page
    .locator("dialog")
    .getByRole("button", { name: "Сохранить", exact: true })
    .click();
  await page.locator("dialog").waitFor({ state: "detached" });
  await page
    .getByRole("link", { name: "Моя библиотека", exact: false })
    .click();
  await page.locator(".personal-title").waitFor();
  assert.equal(await page.locator(".personal-title").textContent(), "Земля");
  assert.equal(
    await page.locator(".library-controls .collection-options").count(),
    0,
  );
  assert.equal(
    await page.locator(".collection-tabs .collection-tab").count(),
    2,
  );
  await page
    .locator(
      '.collection-options summary[aria-label="Действия с коллекцией: Космос"]',
    )
    .click();
  await page
    .locator(".collection-menu")
    .getByRole("button", { name: "Переименовать", exact: true })
    .waitFor();
  await page.screenshot({ path: "/tmp/svod-library-collections.png" });
  await page
    .locator(".personal-item")
    .getByRole("button", { name: "Удалить", exact: true })
    .click();
  await page
    .getByRole("heading", {
      name: "Удалить статью из библиотеки? Она также будет удалена из коллекций.",
      exact: true,
    })
    .waitFor();
  await page.getByRole("button", { name: "Отмена", exact: true }).click();
  assert.equal(await page.locator(".personal-title").textContent(), "Земля");
  await page.reload();
  await page.locator(".personal-title").waitFor();
  await page
    .getByRole("link", { name: "История чтения", exact: false })
    .click();
  await page.locator(".personal-title").waitFor();
  await page.getByRole("button", { name: "Язык интерфейса" }).click();
  await page
    .getByRole("heading", { name: "Reading history", exact: true })
    .waitFor();
  await page.getByRole("link", { name: "Земля", exact: true }).click();
  await page.locator(".wiki-content").waitFor();
  for (const width of [320, 375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 850 });
    await page.waitForTimeout(250);
    assert.ok(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      `overflow at ${width}`,
    );
    await page.screenshot({
      path: `/tmp/svod-article-${width}.png`,
      fullPage: true,
    });
  }
  await page.setViewportSize({ width: 375, height: 850 });
  await page.getByRole("button", { name: "Appearance", exact: true }).click();
  await page.getByLabel("Theme", { exact: true }).selectOption("dark");
  await page
    .getByRole("dialog", { name: "Appearance" })
    .getByRole("button", { name: "Close", exact: true })
    .click();
  await page.screenshot({ path: "/tmp/svod-dark-mobile.png", fullPage: true });
  unavailable = true;
  await page.reload();
  await page.getByText("Saved copy from", { exact: false }).waitFor();
  ((unavailable = false), (delayedArticle = false), releaseArticle);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByRole("button", { name: "Читатель", exact: true }).click();
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await page
    .getByRole("heading", {
      name: "Sign out of this account?",
      exact: true,
    })
    .waitFor();
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await page.getByRole("button", { name: "Confirm", exact: true }).click();
  await page.getByRole("button", { name: "Sign in", exact: true }).waitFor();
  await page.getByRole("link", { name: "My library", exact: false }).click();
  await page
    .getByRole("heading", { name: "Sign in to build your library" })
    .waitFor();
  // A second local account must never inherit the first account's library.
  await page
    .getByRole("button", { name: "Sign in", exact: true })
    .first()
    .click();
  await page
    .getByRole("button", { name: "Create account", exact: true })
    .click();
  await page.getByLabel("Name", { exact: true }).fill("Second");
  await page.getByLabel("Username", { exact: true }).fill("second");
  await page.getByLabel("Password", { exact: true }).fill("Password456");
  await page.locator("dialog form button[type=submit]").click();
  await page.waitForFunction(
    () => document.querySelectorAll("dialog").length === 0,
  );
  await page
    .getByRole("heading", { name: "Your library starts here" })
    .waitFor();
  assert.equal(await page.locator(".personal-title").count(), 0);
  await page.getByRole("button", { name: "Second", exact: true }).click();
  await page
    .getByRole("button", { name: "Delete account", exact: true })
    .click();
  await page.getByRole("button", { name: "Confirm", exact: true }).click();
  await page.waitForFunction(
    () => document.querySelectorAll("dialog").length === 0,
  );
  await page
    .getByRole("button", { name: "Sign in", exact: true })
    .first()
    .click();
  await page.getByLabel("Username", { exact: true }).fill("reader");
  await page.getByLabel("Password", { exact: true }).fill("wrong-password");
  await page.locator("dialog form button[type=submit]").click();
  await page.getByText("Incorrect username or password.").waitFor();
  await page.getByLabel("Password", { exact: true }).fill("Password123");
  await page.locator("dialog form button[type=submit]").click();
  await page.waitForFunction(
    () => document.querySelectorAll("dialog").length === 0,
  );
  await page.locator(".personal-title").waitFor();
  await page.goto("https://svod.test/#/search?q=empty");
  await page.getByRole("heading", { name: "No results found" }).waitFor();
  assert.equal(await page.getByRole("search").count(), 1);
  await page.goto("https://svod.test/#/article?q=Missing");
  await page.getByRole("heading", { name: "Article not found" }).waitFor();
  await page.goto(
    "https://svod.test/#/category?q=" + encodeURIComponent("Категория:Планеты"),
  );
  await page.getByRole("link", { name: "Земля", exact: true }).click();
  await page.locator(".wiki-content").waitFor();
  await page.locator(".wiki-content a").filter({ hasText: "Луна" }).click();
  await page
    .locator(".article-heading h1")
    .filter({ hasText: "Луна" })
    .waitFor();
  await page.goBack();
  await page
    .locator(".article-heading h1")
    .filter({ hasText: "Земля" })
    .waitFor();
  await page.locator(".wiki-content a").filter({ hasText: "[1]" }).click();
  assert.match(page.url(), /section=wiki-/);
  await page.reload();
  await page.locator(".wiki-content").waitFor();
  await page.waitForFunction(() => scrollY > 0);
  await page.emulateMedia({ media: "print" });
  assert.equal(await page.locator(".header").isVisible(), false);
  assert.equal(
    await page.evaluate(() =>
      getComputedStyle(document.documentElement)
        .getPropertyValue("--bg")
        .trim(),
    ),
    "white",
  );
  await page.emulateMedia({ media: "screen" });
  // Stored passwords are hashes, never the submitted password.
  const accounts = await page.evaluate(async () => {
    const s = await import("/svod-store.js");
    return s.list("accounts");
  });
  assert.equal(accounts.length, 1);
  assert.ok(!JSON.stringify(accounts).includes("Password123"));
  // Failed confirmation remains usable and its error is visible in the modal.
  await page.evaluate(async () => {
    const { confirm } = await import("/svod-ui.js");
    confirm("Failure test", async () => {
      throw new Error("storageError");
    });
  });
  await page.getByRole("button", { name: "Confirm", exact: true }).click();
  await page.locator("dialog .dialog-notifications").waitFor();
  assert.equal(
    await page
      .getByRole("button", { name: "Confirm", exact: true })
      .isEnabled(),
    true,
  );
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  assert.deepEqual(errors, []);
  console.log(
    "Svod browser: reading, sanitization, account, collections, history, reload, RU/EN, widths, dark theme, cached errors, logout OK",
  );
})()
  .catch((e) => {
    console.error(e);
    process.exitCode = 1;
  })
  .finally(async () => {
    await browser?.close();
  });
