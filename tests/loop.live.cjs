// Optional network test. Temporary preview is stopped even on failure.
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const net = require("node:net");
const path = require("node:path");
const { chromium } = require("./.tmp/node_modules/playwright-core");
let server, browser;
(async () => {
  const port = await new Promise((resolve) => {
    const s = net.createServer().listen(0, "127.0.0.1", () => {
      const p = s.address().port;
      s.close(() => resolve(p));
    });
  });
  server = spawn(
    "python3",
    ["scripts/preview_loop.py", "--port", String(port)],
    { cwd: path.resolve(__dirname, ".."), stdio: ["ignore", "pipe", "pipe"] },
  );
  await new Promise((resolve, reject) => {
    server.stdout.once("data", resolve);
    server.once("error", reject);
    server.once("exit", (code) => reject(new Error(`preview exited ${code}`)));
  });
  browser = await chromium.launch({
    executablePath:
      process.env.LOOP_BROWSER ||
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  });
  const page = await browser.newPage({
      viewport: { width: 1440, height: 1000 },
    }),
    errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("request", (r) =>
    assert.equal(new URL(r.url()).origin, `http://127.0.0.1:${port}`),
  );
  await page.goto(
    `http://127.0.0.1:${port}/#/search?type=gif&q=${encodeURIComponent("кот")}`,
  );
  await page.locator(".card").first().waitFor({ timeout: 30000 });
  await page.waitForFunction(
    () =>
      [...document.querySelectorAll(".card video")].some(
        (v) => v.readyState >= 2,
      ) ||
      [...document.querySelectorAll(".card img")].some(
        (i) => i.naturalWidth > 0,
      ),
    {},
    { timeout: 30000 },
  );
  await page.screenshot({ path: "/tmp/loop-live-desktop.png" });
  await page.setViewportSize({ width: 375, height: 850 });
  await page.screenshot({ path: "/tmp/loop-live-mobile.png" });
  assert(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  );
  await page.getByRole("button", { name: "Остановить анимации" }).click();
  await page.waitForFunction(
    () => document.querySelector(".card canvas")?.dataset.ready === "true",
  );
  const frame = await page
    .locator(".card canvas")
    .first()
    .evaluate((c) => c.toDataURL());
  await new Promise((resolve) => setTimeout(resolve, 350));
  assert.equal(
    await page
      .locator(".card canvas")
      .first()
      .evaluate((c) => c.toDataURL()),
    frame,
  );
  await page.getByRole("button", { name: "Переключить тему" }).click();
  await page.waitForFunction(
    () => document.querySelector(".card canvas")?.dataset.ready === "true",
  );
  await page.screenshot({ path: "/tmp/loop-live-dark-mobile.png" });
  await page.locator(".card-media").first().click();
  await page.locator(".detail").waitFor({ timeout: 30000 });
  await page.waitForFunction(
    () => {
      const v = document.querySelector(".detail video");
      const i = document.querySelector(".detail img");
      return v?.readyState >= 2 || i?.naturalWidth > 0;
    },
    {},
    { timeout: 30000 },
  );
  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("link", { name: "Скачать", exact: true }).click();
  const downloaded = await downloadEvent;
  assert.match(
    downloaded.suggestedFilename(),
    /^loop-[A-Za-z0-9]{1,12}\.(gif|webp|mp4|png|jpg)$/,
  );
  assert.equal(await downloaded.failure(), null);
  await page.getByRole("button", { name: "Встроить", exact: true }).click();
  const embedCode = await page
    .getByRole("textbox", { name: "HTML-код" })
    .inputValue();
  const embeddedPage = await browser.newPage();
  const downloadedBytes = await require("node:fs/promises").readFile(
    await downloaded.path(),
  );
  await embeddedPage.route("https://embed.test/**", (route) => {
    if (route.request().url().endsWith("index.html"))
      return route.fulfill({ contentType: "text/html", body: embedCode });
    return route.fulfill({
      contentType: downloaded.suggestedFilename().endsWith(".mp4")
        ? "video/mp4"
        : "image/gif",
      body: downloadedBytes,
    });
  });
  await embeddedPage.goto("https://embed.test/index.html");
  await embeddedPage.waitForFunction(
    () =>
      document.querySelector("img")?.naturalWidth > 0 ||
      document.querySelector("video")?.readyState >= 1,
    {},
    { timeout: 30000 },
  );
  await embeddedPage.close();
  await page.keyboard.press("Escape");
  await page.setViewportSize({ width: 1920, height: 1080 });
  for (const type of ["gif", "sticker", "clip"]) {
    await page.goto(`http://127.0.0.1:${port}/#/explore?type=${type}`);
    await page.locator(".card").first().waitFor({ timeout: 30000 });
    await page.waitForFunction(() => {
      const grid = document.querySelector(".gallery.masonry");
      if (!grid || grid.children.length < 5) return false;
      const bottoms = new Map();
      for (const card of grid.children) {
        const r = card.getBoundingClientRect();
        bottoms.set(r.left, Math.max(bottoms.get(r.left) || 0, r.bottom));
      }
      return Math.max(...bottoms.values()) - Math.min(...bottoms.values()) < 1;
    });
    await page.locator(".load-more").scrollIntoViewIfNeeded();
    await page.waitForFunction(
      () =>
        [...document.querySelectorAll(".card canvas")]
          .filter((c) => {
            const r = c.getBoundingClientRect();
            return r.bottom > 0 && r.top < innerHeight;
          })
          .every((c) => c.dataset.ready === "true"),
      {},
      // A visible batch queues behind earlier media requests on the same origin.
      { timeout: 60000 },
    );
    await page.screenshot({ path: `/tmp/loop-bottom-${type}.png` });
    const count = await page.locator(".card").count();
    await page.locator(".load-more button").click();
    await page.waitForFunction(
      (count) => document.querySelectorAll(".card").length > count,
      count,
      { timeout: 30000 },
    );
  }
  assert.deepEqual(errors, []);
  console.log(
    "Live Loop: search, proxied media, detail, mobile layout passed.",
  );
})()
  .catch((e) => {
    console.error(e);
    process.exitCode = 1;
  })
  .finally(async () => {
    await browser?.close();
    server?.kill("SIGTERM");
  });
