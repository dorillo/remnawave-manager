const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium, webkit } = require("./.tmp/node_modules/playwright-core");
const root = path.resolve(
  __dirname,
  "../src/remnawave_manager/data/disguises/06-loop-archive",
);
const raw = (n) => ({
  id: n === 1 ? "8kyX1o" : n,
  fileType: 1,
  title: n === 1 ? "Кот с хорошим настроением" : `Реакция ${n}`,
  tags: ["кот", "радость", "ОченьДлинныйТегБезПробелов".repeat(4)],
  width: 480,
  height: n % 3 === 0 ? 600 : n % 3 === 1 ? 480 : 380,
  cloudSource: `https://media.gifs.ru/${String(n).padStart(40, "a")}.gif`,
  cloudSource300:
    n === 24
      ? null
      : `https://media.gifs.ru/${String(n).padStart(40, "a")}_300.webp`,
  username: "author",
});
let browser,
  fail = false,
  empty = false,
  requested = [];
(async () => {
  browser = await (
    process.env.LOOP_ENGINE === "webkit" ? webkit : chromium
  ).launch({
    executablePath:
      process.env.LOOP_ENGINE === "webkit"
        ? undefined
        : process.env.LOOP_BROWSER ||
          "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    headless: true,
  });
  const context = await browser.newContext({
      viewport: { width: 1440, height: 1000 },
      reducedMotion: "reduce",
    }),
    page = await context.newPage(),
    errors = [];
  await context.addInitScript(() => {
    window.policyViolations = [];
    document.addEventListener("securitypolicyviolation", (e) =>
      window.policyViolations.push(e.violatedDirective),
    );
  });
  page.on("pageerror", (e) => errors.push(e.message));
  const serve = async (route) => {
    const req = route.request(),
      u = new URL(req.url());
    requested.push([u.pathname, req.method()]);
    assert.equal(u.origin, "http://localhost:55306");
    if (u.pathname.startsWith("/_loop/media/"))
      return route.fulfill({
        contentType: "image/svg+xml",
        body: '<svg xmlns="http://www.w3.org/2000/svg" width="480" height="480"><rect width="480" height="480" fill="#c9b8e8"/><circle cx="240" cy="210" r="100" fill="#8870b8"/><path d="M180 235 Q240 300 300 235" stroke="white" stroke-width="15" fill="none"/></svg>',
      });
    if (u.pathname.startsWith("/_loop/gifs/")) {
      if (fail) return route.fulfill({ status: 503, body: "{}" });
      const skip = Number(
        u.searchParams.get("skip") || req.postDataJSON()?.skip || 0,
      );
      let result;
      if (u.pathname.endsWith("/categories"))
        result = [
          { id: 11, title: "Мемы" },
          { id: 12, title: "Природа и животные" },
        ];
      else if (u.pathname.endsWith("/trending"))
        result = [{ key: "кот" }, { key: "смех" }, { key: "пятница" }];
      else if (u.pathname.includes("/item/"))
        result = raw(u.pathname.endsWith("/8kyX1o") ? 1 : Number(u.pathname.split("/").pop()));
      else
        result = {
          isSuccess: true,
          result: empty
            ? []
            : Array.from({ length: skip === 48 ? 2 : 24 }, (_, i) =>
                raw(i + 1 + skip),
              ),
        };
      if (req.postDataJSON()?.query === "duplicate-pages")
        result = {
          result: Array.from({ length: skip < 48 ? 24 : 1 }, () =>
            raw(skip < 48 ? 1 : 99),
          ),
        };
      return route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(result),
      });
    }
    try {
      const file = path.join(
        u.pathname.startsWith("/shared/") ? path.dirname(root) : root,
        u.pathname === "/" ? "index.html" : decodeURIComponent(u.pathname),
      );
      assert(file.startsWith(root) || file.startsWith(path.join(path.dirname(root), "shared") + path.sep));
      return route.fulfill({
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
      return route.fulfill({ status: 404, body: "" });
    }
  };
  await context.route("**/*", serve);
  await page.goto("http://localhost:55306");
  await page.locator(".card").first().waitFor();
  assert.deepEqual(
    await page.evaluate(async () => {
      const { mediaURL, normalize } = await import("./loop-data.js");
      return [
        mediaURL("https://evil.example/" + "a".repeat(40) + ".gif"),
        mediaURL("javascript:alert(1)"),
        mediaURL(
          "https://media.gifs.ru/" + "a".repeat(40) + ".gif?redirect=evil",
        ),
        mediaURL(
          "https://user:secret@media.gifs.ru/" + "a".repeat(40) + ".gif",
        ),
        normalize(null),
        normalize({ id: 1, fileType: 1, isDeleted: true }),
      ];
    }),
    ["", "", "", "", null, null],
  );
  assert.deepEqual(await page.evaluate(async () => {
    const { normalize, item } = await import("./loop-data.js");
    const ids = ["8kyX1o", "123", 123, "000123", "../bad", "a/b", "a?b", "a".repeat(13), {}, -1, 0, "0", null];
    const normalized = ids.map(id => normalize({ id, fileType: 1 })?.id ?? null);
    return { normalized, item: (await item("8kyX1o")).id };
  }), { normalized: ["8kyX1o", 123, 123, "000123", null, null, null, null, null, null, null, "0", null], item: "8kyX1o" });
  assert.equal(await page.locator("input[type=search]").count(), 1);
  for (const width of [
    320, 375, 520, 600, 768, 801, 900, 1024, 1150, 1440, 1920,
  ]) {
    await page.setViewportSize({ width, height: 900 });
    await page.evaluate(
      () =>
        new Promise((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(resolve)),
        ),
    );
    assert(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
      `overflow at ${width}`,
    );
    const geometry = await page.locator(".gallery").evaluate((grid) => {
      const bounds = grid.getBoundingClientRect();
      return {
        bottom: bounds.bottom,
        gap: parseFloat(getComputedStyle(grid).columnGap),
        cards: [...grid.children].map((c) => {
          const r = c.getBoundingClientRect();
          return { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
        }),
      };
    });
    assert.equal(geometry.cards.length, 48);
    assert.equal(await page.locator('.nav-link[href="#/uploads"]').count(), 1);
    assert.equal(await page.locator(".upload-entry").count(), 0);
    assert(
      !/gifs\.ru|локальн|только в этом браузере|local account|only in this browser/i.test(
        await page.locator("body").innerText(),
      ),
    );
    assert.equal(await page.locator(".category-select span").count(), 0);
    const selectBox = await page
      .locator(".category-select select")
      .boundingBox();
    const pauseBox = await page.locator(".toolbar button").boundingBox();
    assert.equal(selectBox.height, 42);
    assert.equal(selectBox.y, pauseBox.y);
    if (width <= 800) {
      const nav = await page.locator(".sidebar").boundingBox();
      assert(Math.abs(nav.y + nav.height - 900) < 1);
      assert.equal(
        await page
          .locator(".sidebar")
          .evaluate((n) => getComputedStyle(n).position),
        "fixed",
      );
    }
    for (const [index, card] of geometry.cards.entries()) {
      assert(
        card.bottom <= geometry.bottom + 1,
        `clipped card ${index}/${width}`,
      );
      const above = geometry.cards
        .slice(0, index)
        .filter((c) => Math.abs(c.left - card.left) < 1)
        .at(-1);
      if (above)
        assert(
          Math.abs(card.top - above.bottom - geometry.gap) < 1,
          `masonry gap ${index}/${width}`,
        );
      for (const other of geometry.cards.slice(index + 1))
        assert(
          card.right <= other.left + 1 ||
            other.right <= card.left + 1 ||
            card.bottom <= other.top + 1 ||
            other.bottom <= card.top + 1,
          `overlap ${index}/${width}`,
        );
    }
    const bottoms = new Map();
    for (const card of geometry.cards)
      bottoms.set(
        card.left,
        Math.max(bottoms.get(card.left) || 0, card.bottom),
      );
    assert(
      Math.max(...bottoms.values()) - Math.min(...bottoms.values()) < 1,
      `uneven bottom ${width}`,
    );
    if (width > 800) {
      const search = await page.locator(".search").boundingBox();
      assert(
        Math.abs(search.x + search.width / 2 - width / 2) < 1,
        `search off center ${width}`,
      );
    }
    for (const node of await page
      .locator(".header-actions button,.nav-link")
      .all())
      assert(await node.isVisible());
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: "/tmp/loop-desktop.png", fullPage: true });
  const last = page.locator('.card-media[href="#/item/24"]');
  await last.scrollIntoViewIfNeeded();
  await page.waitForFunction(
    () =>
      document.querySelector('.card-media[href="#/item/24"] canvas')?.dataset
        .ready === "true",
  );
  await page.getByRole("button", { name: "Показать ещё" }).click();
  await page.waitForFunction(
    () => document.querySelectorAll(".card").length === 50,
  );
  await page.locator(".toolbar button[aria-pressed]").click();
  assert.match(
    await page.locator('.card-media[href="#/item/24"] img').getAttribute("src"),
    /24\.gif$/,
  );
  await page.locator(".toolbar button[aria-pressed]").click();
  await page.evaluate(() => scrollTo(0, 0));
  await page.locator(".header-account").focus();
  await page.locator(".header-account").press("Enter");
  await page.getByRole("dialog").waitFor();
  await page.keyboard.press("Escape");
  await page.waitForFunction(() => !document.querySelector("dialog"));
  assert(
    await page
      .locator(".header-account")
      .evaluate((node) => node === document.activeElement),
  );
  await page.keyboard.press("Control+k");
  assert(
    await page
      .getByRole("searchbox")
      .evaluate((node) => node === document.activeElement),
  );
  await page.locator(".card-like").first().click();
  await page.getByRole("dialog").waitFor();
  const switchBox = await page.locator(".auth-switch").boundingBox();
  const submitBox = await page
    .locator('.form button[type="submit"]')
    .boundingBox();
  assert.equal(await page.locator(".form-note").count(), 0);
  assert.equal(switchBox.width, submitBox.width);
  assert.equal(switchBox.height, submitBox.height);
  await page.setViewportSize({ width: 320, height: 568 });
  assert(
    await page
      .getByRole("dialog")
      .evaluate((d) => d.scrollWidth <= d.clientWidth),
  );
  await page.screenshot({ path: "/tmp/loop-auth-mobile.png" });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Создать аккаунт", exact: true })
    .click();
  await page.getByLabel("Имя", { exact: true }).fill("Тестовый пользователь");
  await page.getByLabel("Логин", { exact: true }).fill("tester");
  await page.getByLabel("Пароль", { exact: true }).fill("password123");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Создать аккаунт", exact: true })
    .click();
  await page.waitForFunction(() => !document.querySelector("dialog"));
  await page.waitForFunction(
    () =>
      document.querySelector(".card-like")?.getAttribute("aria-pressed") ===
      "true",
  );
  assert.equal(
    await page
      .locator(".card-like")
      .first()
      .evaluate(
        (node) => getComputedStyle(node.querySelector("svg")).fillOpacity,
      ),
    "1",
  );
  assert.notEqual(await page.locator('.card-like.active').first().evaluate(node => getComputedStyle(node).borderTopColor), 'rgba(0, 0, 0, 0)');
  await page.locator(".card-media").first().click();
  await page.locator(".detail").waitFor();
  await page.locator(".related .load-more button").waitFor();
  assert.equal(await page.locator(".related .card").count(), 47);
  fail = true;
  await page.locator(".related .load-more button").click();
  await page
    .locator(".related")
    .getByRole("button", { name: "Повторить" })
    .waitFor();
  assert.equal(await page.locator(".related .card").count(), 47);
  fail = false;
  await page
    .locator(".related")
    .getByRole("button", { name: "Повторить" })
    .click();
  await page.waitForFunction(
    () => document.querySelectorAll(".related .card").length === 49,
  );
  assert.equal(await page.locator(".related .load-more button").count(), 0);

  assert.equal(
    await page
      .getByRole("link", { name: "Скачать", exact: true })
      .getAttribute("download"),
    "loop-8kyX1o.gif",
  );
  await page.getByRole("button", { name: "Встроить", exact: true }).click();
  const code = await page
    .getByRole("textbox", { name: "HTML-код" })
    .inputValue();
  assert(code.startsWith("<img "));
  assert(code.includes('src="loop-8kyX1o.gif"'));
  assert(!code.includes("gifs.ru"));
  assert.equal(await page.locator(".source-link").count(), 0);
  assert(!code.includes("localhost"));
  assert(!code.includes("iframe"));
  await page.getByRole("button", { name: "Скопировать код" }).click();
  await page.keyboard.press("Escape");
  for (const [language, theme] of [
    ["en", "dark"],
    ["en", "light"],
    ["ru", "dark"],
    ["ru", "light"],
  ]) {
    if ((await page.locator("html").getAttribute("lang")) !== language)
      await page.locator(".header-actions button").first().click();
    if ((await page.locator("html").getAttribute("data-theme")) !== theme)
      await page.locator(".header-actions button").nth(1).click();
    await page.locator(".detail").waitFor();
    for (const width of [320, 375, 390, 430, 600, 768, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      await page.evaluate(
        () =>
          new Promise((resolve) =>
            requestAnimationFrame(() => requestAnimationFrame(resolve)),
          ),
      );
      assert(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        `detail overflow ${width}`,
      );
      const actions = await page
        .locator(".detail-info .detail-actions")
        .evaluate((node) => {
          const outer = node.getBoundingClientRect();
          return {
            left: outer.left,
            right: outer.right,
            buttons: [...node.children].map((button) => {
              const r = button.getBoundingClientRect();
              return {
                left: r.left,
                right: r.right,
                top: r.top,
                bottom: r.bottom,
                width: r.width,
                height: r.height,
                fits: button.scrollWidth <= button.clientWidth,
              };
            }),
          };
        });
      assert.equal(actions.buttons.length, 5);
      for (const b of actions.buttons)
        assert(
          b.fits && b.left >= actions.left - 1 && b.right <= actions.right + 1,
        );
      if (width <= 380)
        for (const b of actions.buttons)
          assert(Math.abs(b.width - actions.buttons[0].width) < 1);
      else if (width <= 800) {
        assert.equal(actions.buttons[1].top, actions.buttons[2].top);
        assert.equal(actions.buttons[3].top, actions.buttons[4].top);
        assert(
          Math.abs(actions.buttons[1].width - actions.buttons[2].width) < 1,
        );
      }
    }
  }
  await page.setViewportSize({ width: 375, height: 900 });
  await page.evaluate(
    () =>
      new Promise((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(resolve)),
      ),
  );
  await page
    .locator(".detail-info")
    .screenshot({ path: "/tmp/loop-mobile-actions.png" });
  await page.screenshot({
    path: "/tmp/loop-mobile-detail.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "В коллекцию", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Новая коллекция" })
    .click();
  await page.getByLabel("Название коллекции").fill("Для друзей");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Подтвердить" })
    .click();
  await page.waitForFunction(() => !document.querySelector("dialog"));
  await page.locator('.nav-link[href="#/collections"]').click();
  await page.getByRole("heading", { name: "Для друзей" }).waitFor();
  await page.getByRole("heading", { name: "Для друзей" }).click();
  await page.locator('.card-media').first().click();
  await page.getByRole('button', { name: 'В коллекцию', exact: true }).click();
  const selectedCollection = page.locator('.collection-picker button');
  await selectedCollection.waitFor();
  assert.equal(await selectedCollection.getAttribute('aria-pressed'), 'true');
  await selectedCollection.click();
  await page.waitForFunction(() => document.querySelector('.collection-picker button')?.getAttribute('aria-pressed') === 'false');
  await selectedCollection.click();
  await page.waitForFunction(() => document.querySelector('.collection-picker button')?.getAttribute('aria-pressed') === 'true');
  await page.keyboard.press('Escape');
  await page.locator('.nav-link[href="#/collections"]').click();
  await page.getByRole("heading", { name: "Для друзей" }).click();
  await page.locator(".card").waitFor();
  await page.getByRole("button", { name: "Переименовать" }).click();
  await page.getByLabel("Название коллекции").fill("Мои коты");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Подтвердить" })
    .click();
  await page.getByRole("heading", { name: "Мои коты" }).waitFor();
  await page.reload();
  await page.getByRole("heading", { name: "Мои коты" }).waitFor();
  assert.equal(await page.locator(".card").count(), 1);
  await page.locator('.nav-link[href="#/profile"]').click();
  const logoutBox = await page
    .getByRole("button", { name: "Выйти", exact: true })
    .boundingBox();
  const deleteBox = await page
    .getByRole("button", { name: "Удалить аккаунт", exact: true })
    .boundingBox();
  assert.equal(logoutBox.y, deleteBox.y);
  assert(Math.abs(deleteBox.x - logoutBox.x - logoutBox.width - 12) < 1);
  await page.getByRole("button", { name: "Выйти", exact: true }).click();
  const actionBox = await page
    .locator(".dialog-actions button")
    .first()
    .boundingBox();
  const messageBox = await page.locator("dialog p.muted").boundingBox();
  assert.equal(actionBox.x, messageBox.x);
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Подтвердить" })
    .click();
  await page.locator(".guest-prompt").waitFor();
  await page.locator(".header-account").click();
  await page.getByLabel("Логин", { exact: true }).fill("tester");
  await page.getByLabel("Пароль", { exact: true }).fill("wrongpassword");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Войти", exact: true })
    .click();
  await page.getByText("Неверный логин или пароль.").waitFor();
  await page.getByLabel("Пароль", { exact: true }).fill("password123");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Войти", exact: true })
    .click();
  await page.getByRole("button", { name: "Сохранить имя" }).waitFor();
  await page.locator('.nav-link[href="#/likes"]').click();
  await page.locator(".card").waitFor();
  assert.equal(await page.locator(".card").count(), 1);
  await page.getByRole("button", { name: "Язык интерфейса" }).click();
  await page.getByRole("heading", { name: "Liked", exact: true }).waitFor();
  await page.getByRole("button", { name: "Switch theme" }).click();
  await page.reload();
  await page.getByRole("heading", { name: "Liked", exact: true }).waitFor();
  assert.equal(await page.locator("html").getAttribute("lang"), "en");
  await page.locator('.nav-link[href="#/explore"]').click();
  await page.locator(".card").first().waitFor();
  await page.getByRole("searchbox").fill("<img src=x onerror=alert(1)>");
  await page.getByRole("searchbox").press("Enter");
  await page.getByRole("heading", { name: /Search results/ }).waitFor();
  assert.equal(await page.locator("h1 img").count(), 0);
  await page.locator(".card").first().waitFor();
  await page.getByRole("button", { name: "Show more" }).click();
  await page.waitForFunction(
    () => document.querySelectorAll(".card").length === 50,
  ); // The final short page is displayed in full.
  empty = true;
  await page.getByRole("searchbox").fill("no-results");
  await page.getByRole("searchbox").press("Enter");
  await page.getByRole("heading", { name: "Nothing here yet" }).waitFor();
  fail = true;
  empty = false;
  await page.getByRole("searchbox").fill("network-failure");
  await page.getByRole("searchbox").press("Enter");
  await page.getByRole("button", { name: "Try again" }).waitFor();
  fail = false;
  await page.getByRole("button", { name: "Try again" }).click();
  await page.locator(".card").first().waitFor();
  // A second account must not inherit private likes or collections.
  await page.locator('.nav-link[href="#/profile"]').click();
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Confirm", exact: true })
    .click();
  await page.locator(".header-account").click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Create account", exact: true })
    .click();
  await page.getByLabel("Name", { exact: true }).fill("Second account");
  await page.getByLabel("Username", { exact: true }).fill("second");
  await page.getByLabel("Password", { exact: true }).fill("password123");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Create account", exact: true })
    .click();
  await page.getByRole("button", { name: "Save name" }).waitFor();
  await page.locator('.nav-link[href="#/likes"]').click();
  await page.locator('[data-empty-section="likedMedia"]').waitFor();
  assert.equal(await page.locator(".card").count(), 0);
  await page.locator('.nav-link[href="#/collections"]').click();
  await page.locator('[data-empty-section="collections"]').waitFor();
  await page.locator('.nav-link[href="#/profile"]').click();
  await page
    .getByRole("button", { name: "Delete account", exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Confirm", exact: true })
    .click();
  await page
    .locator(".guest-prompt")
    .waitFor();
  await page.locator(".header-account").click();
  await page.getByLabel("Username", { exact: true }).fill("tester");
  await page.getByLabel("Password", { exact: true }).fill("password123");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Sign in", exact: true })
    .click();
  await page.getByRole("button", { name: "Save name" }).waitFor();
  await page.locator('.nav-link[href="#/collections"]').click();
  await page.getByRole("heading", { name: "Мои коты" }).click();
  await page.locator(".card").waitFor();
  await page.getByRole("button", { name: "Remove from collection" }).click();
  await page.locator('[data-empty-section="collection"]').waitFor();
  await page.getByRole("button", { name: "Delete", exact: true }).click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Confirm", exact: true })
    .click();
  await page.locator('[data-empty-section="collections"]').waitFor();
  await page.locator('.nav-link[href="#/likes"]').click();
  await page.locator(".card-like").click();
  await page.locator('[data-empty-section="likedMedia"]').waitFor();
  for (const language of ["en", "ru"]) {
    if ((await page.locator("html").getAttribute("lang")) !== language)
      await page.locator(".header-actions button").first().click();
    for (const theme of ["light", "dark"]) {
      if ((await page.locator("html").getAttribute("data-theme")) !== theme)
        await page.locator(".header-actions button").nth(1).click();
      for (const width of [320, 375, 768, 1440]) {
        await page.setViewportSize({ width, height: 900 });
        assert(
          await page.evaluate(
            () => document.documentElement.scrollWidth <= innerWidth,
          ),
          `${language}/${theme}/${width}`,
        );
      }
    }
  }
  await page.locator('.nav-link[href="#/explore"]').click();
  await page.getByRole("searchbox").fill("duplicate-pages");
  await page.getByRole("searchbox").press("Enter");
  await page.waitForFunction(
    () => document.querySelectorAll(".card").length === 1,
  );
  await page.locator(".load-more button").click();
  await page.waitForFunction(
    () => document.querySelectorAll(".card").length === 2,
  );
  assert.equal(await page.locator(".load-more button").count(), 0);
  assert.deepEqual(await page.evaluate(async () => {
    const { normalize, item } = await import("./loop-data.js");
    const ids = ["8kyX1o", "123", 123, "000123", "../bad", "a/b", "a?b", "a".repeat(13), {}, -1, 0, "0", null];
    const normalized = ids.map(id => normalize({ id, fileType: 1 })?.id ?? null);
    return { normalized, item: (await item("8kyX1o")).id };
  }), { normalized: ["8kyX1o", 123, 123, "000123", null, null, null, null, null, null, null, "0", null], item: "8kyX1o" });
  assert.equal(await page.locator("input[type=search]").count(), 1);
  // Exercise all local lists beyond two pages, including keeping the expanded
  // list after removing a liked item.
  const collectionId = await page.evaluate(async () => {
    const store = await import("./loop-store.js");
    const { normalize } = await import("./loop-data.js");
    const media = Array.from({ length: 101 }, (_, i) =>
      normalize({
        id: 1000 + i,
        fileType: 1,
        title: `Saved ${i}`,
        width: 480,
        height: 480,
        cloudSource: `https://media.gifs.ru/${String(i).padStart(40, "a")}.gif`,
      }),
    );
    for (const m of media)
      await store.put("likes", {
        id: `${store.session()}:${m.id}`,
        owner: store.session(),
        media: m,
        at: m.id,
      });
    const c = await store.createCollection("Paged collection");
    await store.editCollection(c.id, (value) => (value.items = media));
    for (let i = 0; i < 50; i++)
      await store.createCollection(`Collection ${i}`);
    return c.id;
  });
  for (const hash of ["#/likes", `#/collection/${collectionId}`]) {
    await page.evaluate((hash) => (location.hash = hash), hash);
    await page.waitForFunction(
      () => document.querySelectorAll(".card").length === 48,
    );
    await page.locator(".load-more button").click();
    assert.equal(await page.locator(".card").count(), 96);
    await page.locator(".load-more button").click();
    assert.equal(await page.locator(".card").count(), 101);
    assert.equal(await page.locator(".load-more button").count(), 0);
  }
  await page.evaluate(() => (location.hash = "#/likes"));
  await page.waitForFunction(
    () => document.querySelectorAll(".card").length === 101,
  );
  await page.locator(".card-like").last().click();
  await page.waitForFunction(
    () => document.querySelectorAll(".card").length === 100,
  );
  await page.evaluate(() => (location.hash = "#/collections"));
  await page.waitForFunction(
    () => document.querySelectorAll(".collection-tile").length === 48,
  );
  await page.locator(".load-more button").click();
  assert.equal(await page.locator(".collection-tile").count(), 51);
  assert.equal(await page.locator(".load-more button").count(), 0);
  // Final audit: transactional races, stale accounts, malformed dimensions,
  // storage denial, and explicit animation preferences.
  const race = await page.evaluate(async () => {
    const store = await import("./loop-store.js");
    const results = await Promise.allSettled([
      store.createCollection("Concurrent collection"),
      store.createCollection("Concurrent collection"),
    ]);
    return results
      .map((r) => (r.status === "fulfilled" ? "created" : r.reason.message))
      .sort();
  });
  assert.deepEqual(race, ["created", "duplicateCollection"]);
  assert.deepEqual(
    await page.evaluate(async () => {
      const store = await import("./loop-store.js");
      const a = await store.createCollection("Rename A"),
        b = await store.createCollection("Rename B");
      const result = await Promise.allSettled(
        [a, b].map((c) =>
          store.editCollection(c.id, (row) => {
            row.name = "Same name";
          }),
        ),
      );
      return result
        .map((r) => (r.status === "fulfilled" ? "renamed" : r.reason.message))
        .sort();
    }),
    ["duplicateCollection", "renamed"],
  );
  assert.deepEqual(
    await page.evaluate(async () => {
      const { mixedFeed } = await import("./loop-uploads.js");
      const result = await mixedFeed(
        {},
        { remoteSkip: 48, remoteMore: true, localSkip: 0, buffer: [{ id: 9 }] },
        [],
        async () => {
          throw new Error("networkError");
        },
      );
      return {
        ids: result.items.map((x) => x.id),
        more: result.more,
        failed: result.remoteError,
      };
    }),
    { ids: [9], more: true, failed: true },
  );

  assert.deepEqual(
    await page.evaluate(async () => {
      const { normalize } = await import("./loop-data.js");
      const item = normalize({
        id: 1,
        fileType: 1,
        width: 0.01,
        height: Infinity,
      });
      return [item.width, item.height];
    }),
    [1, 480],
  );
  await page.evaluate(() => {
    localStorage.setItem("loop:paused", "false");
    location.hash = "#/explore";
  });
  await page.reload();
  await page.locator(".card").first().waitFor();
  assert.equal(await page.locator("html").getAttribute("data-paused"), "false");
  await page.locator(".gallery").evaluate((grid) => {
    [...grid.children].forEach((card, i) => {
      card.querySelector(".card-media").style.aspectRatio =
        i % 4 === 0 ? "1 / 1000" : "1000 / 1";
    });
    window.dispatchEvent(new Event("resize"));
  });
  assert(
    await page.locator(".gallery").evaluate((grid) => {
      const bounds = grid.getBoundingClientRect();
      return [...grid.children].every((card) => {
        const r = card.getBoundingClientRect();
        return (
          r.width > 0 &&
          r.left >= bounds.left - 1 &&
          r.right <= bounds.right + 1
        );
      });
    }),
  );
  const stale = await page.evaluate(async () => {
    const store = await import("./loop-store.js");
    const previous = store.session(),
      id = crypto.randomUUID();
    await store.put("accounts", {
      id,
      login: "deleted-account",
      name: "Deleted",
    });
    store.signIn(id);
    await store.remove("accounts", id); // deletion in another tab
    let rejected = false;
    try {
      await store.updateName(id, "Resurrected");
    } catch {
      rejected = true;
    }
    const exists = !!(await store.get("accounts", id));
    let error;
    try { await store.current(); } catch (failure) { error = failure.message; }
    const preserved = store.session() === id;
    store.signIn(previous);
    return { rejected, exists, error, preserved };
  });
  assert.deepEqual(stale, {
    rejected: true,
    exists: false,
    error: "storageError",
    preserved: true,
  });
  const denied = await context.newPage();
  denied.on("pageerror", (e) => errors.push(e.message));
  await denied.addInitScript(() => {
    indexedDB.open = () => {
      throw new DOMException("Blocked storage", "SecurityError");
    };
  });
  await denied.goto("http://localhost:55306/#/explore?type=gif&category=11");
  await denied.getByRole("heading", { name: "Мемы", exact: true }).waitFor();
  await denied.locator(".card").first().waitFor();
  assert.equal(await denied.locator(".card").count(), 48);
  await denied.close();
  const touchContext = await browser.newContext({
    hasTouch: true,
    isMobile: true,
    viewport: { width: 390, height: 844 },
    reducedMotion: "reduce",
  });
  await touchContext.route("**/*", serve);
  const touch = await touchContext.newPage();
  touch.on("pageerror", (e) => errors.push(e.message));
  await touch.goto("http://localhost:55306/#/explore");
  await touch.locator(".card").first().waitFor();
  assert.equal(
    await touch
      .locator(".card-caption")
      .first()
      .evaluate((n) => getComputedStyle(n).opacity),
    "1",
  );
  for (const viewport of [
    { width: 320, height: 568 },
    { width: 844, height: 390 },
    { width: 390, height: 844 },
  ]) {
    await touch.setViewportSize(viewport);
    await touch.evaluate(
      () =>
        new Promise((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(resolve)),
        ),
    );
    assert(
      await touch.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    );
    await touch.locator(".header-account").click();
    const dialog = touch.getByRole("dialog");
    assert(
      await dialog.evaluate(
        (d) =>
          d.scrollWidth <= d.clientWidth &&
          d.getBoundingClientRect().height <= innerHeight,
      ),
    );
    await touch.keyboard.press("Escape");
  }
  await touch.screenshot({
    path: `/tmp/loop-audit-touch-${process.env.LOOP_ENGINE || "chrome"}.png`,
  });
  await touchContext.close();
  assert.deepEqual(errors, []);
  assert.deepEqual(await page.evaluate(() => window.policyViolations), []);
  assert(requested.some(([p, m]) => p.endsWith("search-gif") && m === "POST"));
  console.log("Loop browser checks passed; screenshots in /tmp/loop-*.png");
})()
  .catch((e) => {
    console.error(e);
    process.exitCode = 1;
  })
  .finally(async () => {
    await browser?.close();
  });
