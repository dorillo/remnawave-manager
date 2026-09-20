/* Release audit: timeout recovery, resilient storage, mobile layout and modal races. */
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
    const context = await browser.newContext();
    await context.route("https://fokus.test/**", async (r) => {
      const p = new URL(r.request().url()).pathname;
      if (p.startsWith("/_fokus/"))
        return r.fulfill({
          body: p.endsWith("/feed")
            ? "<rss><channel></channel></rss>"
            : '<div class="cell"></div>',
          contentType: "text/plain",
        });
      return r.fulfill({
        body: await fs.readFile(path.join(p.startsWith("/shared/") ? path.dirname(root) : root, p === "/" ? "index.html" : p)),
        contentType: p.endsWith(".js")
          ? "text/javascript"
          : p.endsWith(".css")
            ? "text/css"
            : p.endsWith(".svg")
              ? "image/svg+xml"
              : "text/html",
      });
    });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto("https://fokus.test/#/profile");
    await page.locator("[data-action=auth]").first().waitFor();
    const version = await page.evaluate(
      () => new URL(document.querySelector("script[src]").src).search,
    );
    // A fresh module instance must open one connection for concurrent reads.
    const storage = await page.evaluate(async () => {
      const native = indexedDB.open.bind(indexedDB);
      let opens = 0;
      indexedDB.open = (...args) => {
        opens++;
        return native(...args);
      };
      const store = await import("./fokus-store.js?audit-storage");
      await Promise.all(Array.from({ length: 8 }, () => store.read()));
      indexedDB.open = native;
      return opens;
    });
    assert.equal(storage, 1);
    await page.evaluate(async (version) => {
      const store = await import("./fokus-store.js" + version);
      await store.change((s) => {
        s.accounts = [
          {
            id: "audit",
            login: "audit",
            name: "Д".repeat(80),
            salt: "salt",
            hash: "hash",
          },
        ];
      });
      store.signIn("audit");
    }, version);
    await page.reload();
    await page.locator(".settings-card").waitFor();
    for (const theme of ["light", "dark"])
      for (const lang of ["ru", "en"]) {
        await page.evaluate(
          ({ theme, lang }) => {
            localStorage.setItem("fokus:theme", theme);
            localStorage.setItem("fokus:lang", lang);
          },
          { theme, lang },
        );
        await page.reload();
        await page.locator(".settings-card").waitFor();
        for (const width of [320, 375, 650, 768, 1024, 1440]) {
          await page.setViewportSize({ width, height: 720 });
          assert(
            await page.evaluate(
              () => document.documentElement.scrollWidth <= innerWidth,
            ),
            `${theme}/${lang}/${width} overflow`,
          );
          const gap = await page
            .locator(".settings-card")
            .evaluate(
              (el) =>
                el
                  .querySelector("[data-action=edit-profile]")
                  .getBoundingClientRect().top -
                el.querySelector("h2").getBoundingClientRect().bottom,
            );
          assert(gap >= 20, "profile heading spacing");
        }
      }
    await page.setViewportSize({ width: 320, height: 568 });
    await page.screenshot({
      path: "/tmp/fokus-audit-profile.png",
      fullPage: true,
    });
    await page.locator("[data-action=edit-profile]").click();
    assert(
      await page
        .locator("#modal")
        .evaluate((el) => el.scrollWidth <= el.clientWidth),
      "profile dialog overflow",
    );
    await page.keyboard.press("Escape");
    await page.locator("[data-action=logout]").click();
    assert(
      await page
        .locator(".dialog-actions [data-action=close]")
        .evaluate((el) => el === document.activeElement),
      "confirmation focuses cancellation",
    );
    await page.locator("[data-action=confirm]").click();
    // A completed authentication must not close a new dialog opened after cancellation.
    await page.locator("[data-action=auth]").first().click();
    await page.locator("[data-action=register]").click();
    await page.locator("#auth-form [name=name]").fill("Audit");
    await page.locator("#auth-form [name=login]").fill("audit-new");
    await page.locator("#auth-form [name=password]").fill("audit-password");
    await page.evaluate(() => {
      const derive = crypto.subtle.deriveBits.bind(crypto.subtle);
      crypto.subtle.deriveBits = async (...args) => {
        window.deriving = true;
        await new Promise((resolve) => (window.finishDerive = resolve));
        return derive(...args);
      };
    });
    await page.locator("#auth-form [type=submit]").click();
    await page.waitForFunction(() => window.deriving);
    await page.keyboard.press("Escape");
    await page.locator("[data-action=auth]").first().click();
    await page.evaluate(() => window.finishDerive());
    await page.waitForFunction(() => !!localStorage.getItem("fokus:ria:v1"));
    await page.locator(".account-link").waitFor();
    assert(await page.locator("#modal").isVisible(), "new dialog stays open");
    await page.keyboard.press("Escape");
    // Simulate a hung source without waiting eighteen real seconds.
    const timeoutPage = await context.newPage();
    await timeoutPage.goto("https://fokus.test/#/profile");
    await timeoutPage.locator("#header .brand").waitFor();
    await timeoutPage.clock.install();
    await timeoutPage.evaluate(async (version) => {
      const { load } = await import("./fokus-data.js" + version);
      window.fetch = (_url, options) =>
        new Promise((_resolve, reject) => {
          window.requestStarted = true;
          options.signal.addEventListener(
            "abort",
            () => reject(new DOMException("Aborted", "AbortError")),
            { once: true },
          );
        });
      window.pendingRequest = load("search", {
        query: "never-responds",
        force: true,
      }).catch((e) => (window.failure = { name: e.name, message: e.message }));
    }, version);
    await timeoutPage.waitForFunction(() => window.requestStarted);
    await timeoutPage.clock.fastForward(19000);
    await timeoutPage.waitForFunction(() => window.failure);
    assert.deepEqual(await timeoutPage.evaluate(() => window.failure), {
      name: "Error",
      message: "network",
    });
    const aborted = await timeoutPage.evaluate(async (version) => {
      const { load } = await import("./fokus-data.js" + version);
      const c = new AbortController();
      c.abort();
      try {
        await load("search", { query: "cancelled", signal: c.signal });
      } catch (e) {
        return e.name;
      }
    }, version);
    assert.equal(aborted, "AbortError");
    assert.deepEqual(errors, []);
    console.log("Fokus release audit checks passed");
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
