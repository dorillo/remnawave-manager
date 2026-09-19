/* Live, anonymous reads only. The development server is always stopped. */
const { chromium } = require("./.tmp/node_modules/playwright-core");
const { spawn } = require("node:child_process");
const assert = require("node:assert/strict");
const path = require("node:path");
const fs = require("node:fs/promises");
const port = Number(process.env.FOKUS_PORT || 15507),
  origin = `http://127.0.0.1:${port}`;
(async () => {
  const server = spawn(
    "python3",
    ["scripts/preview_fokus.py", "--port", String(port)],
    { cwd: path.resolve(__dirname, ".."), stdio: ["ignore", "pipe", "pipe"] },
  );
  let browser;
  try {
    await new Promise((resolve, reject) => {
      server.stdout.once("data", resolve);
      server.once("error", reject);
      server.once("exit", (code) => reject(new Error("preview exit " + code)));
    });
    browser = await chromium.launch({
      executablePath:
        process.env.FOKUS_BROWSER ||
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
      headless: true,
    });
    const page = await browser.newPage({
      viewport: { width: 1440, height: 1000 },
    });
    const errors = [],
      external = [];
    page.on("pageerror", (e) => errors.push(e.message));
    page.on("request", (r) => {
      if (!r.url().startsWith(origin) && !r.url().startsWith("data:"))
        external.push(r.url());
    });
    await page.goto(origin);
    await page.locator(".lead-story h2").waitFor({ timeout: 60000 });
    await fs.mkdir("/tmp/fokus-check", { recursive: true });
    await page.screenshot({
      path: "/tmp/fokus-check/home-1440.png",
      fullPage: true,
    });
    console.log("home", await page.locator(".lead-story h2").innerText());
    await page.locator('nav a[href="#/section/world"]').click();
    await page.locator(".news-grid .card").first().waitFor({ timeout: 30000 });
    await page.locator('[data-action="more"]').waitFor();
    const n = await page.locator(".news-grid .card").count();
    await page.locator('[data-action="more"]').click();
    await page.waitForFunction(
      (n) => document.querySelectorAll(".news-grid .card").length > n,
      n,
      { timeout: 30000 },
    );
    console.log(
      "pagination",
      n,
      await page.locator(".news-grid .card").count(),
    );
    await page.goto(origin + "/#/article/20260918/bespilotnik-2118644196.html");
    await page
      .locator(".article-body .paragraph")
      .first()
      .waitFor({ timeout: 30000 });
    await page.waitForFunction(
      () =>
        document.querySelectorAll(".reaction strong").length === 6 &&
        document.querySelector(".reaction strong").textContent !== "—",
      null,
      { timeout: 30000 },
    );
    await page.locator(".comment").first().waitFor({ timeout: 30000 });
    console.log(
      "article paragraphs",
      await page.locator(".paragraph").count(),
      "comments",
      await page.locator(".comment").count(),
    );
    await page.screenshot({
      path: "/tmp/fokus-check/article-1440.png",
      fullPage: true,
    });
    for (const width of [320, 375, 768, 1024, 1440]) {
      await page.setViewportSize({ width, height: 900 });
      await page.waitForTimeout(100);
      if (
        !(await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ))
      )
        console.log(
          await page.evaluate(() =>
            [...document.querySelectorAll("body *")]
              .filter((x) => x.getBoundingClientRect().right > innerWidth + 1)
              .map((x) => [
                x.tagName,
                x.className,
                x.getBoundingClientRect().width,
                x.getBoundingClientRect().right,
              ])
              .slice(0, 20),
          ),
        );
      assert(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        `overflow ${width}`,
      );
      if (width === 375)
        await page.screenshot({
          path: "/tmp/fokus-check/article-375.png",
          fullPage: true,
        });
    }
    await page.goto(origin + "/#/home");
    await page.locator(".lead-story").waitFor();
    await page.setViewportSize({ width: 375, height: 900 });
    await page.screenshot({
      path: "/tmp/fokus-check/home-375.png",
      fullPage: true,
    });
    await page.screenshot({
      path: "/tmp/fokus-check/home-mobile-viewport.png",
    });
    await page.locator("[data-action=theme]").click();
    await page.screenshot({ path: "/tmp/fokus-check/home-mobile-dark.png" });
    await page.goto(origin + "/#/article/20260918/bespilotnik-2118644196.html");
    await page.locator(".article-body").waitFor();
    await page.waitForFunction(() =>
      [...document.querySelectorAll(".article-cover img")].every(
        (i) => i.complete,
      ),
    );
    await page.screenshot({
      path: "/tmp/fokus-check/article-mobile-viewport.png",
    });
    await page.waitForFunction(() => document.querySelector('.reaction strong')?.textContent !== '—');
    await page.locator('.comment').first().waitFor();
    await page.locator("#reactions").scrollIntoViewIfNeeded();
    await page.screenshot({
      path: "/tmp/fokus-check/article-mobile-reactions.png",
    });
    assert.equal(await page.locator("input[type=search]").count(), 1);
    assert.deepEqual(errors, []);
    assert.deepEqual(external, []);
    console.log("Live checks passed; screenshots: /tmp/fokus-check");
  } finally {
    await browser?.close();
    server.kill("SIGTERM");
    if (server.exitCode === null)
      await new Promise((r) => server.once("exit", r));
  }
})().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
