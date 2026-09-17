/* Optional smoke test against the real API. The temporary preview is always stopped. */
const assert = require("node:assert/strict");
const { spawn } = require("node:child_process");
const { chromium } = require("./.tmp/node_modules/playwright-core");
let server, browser;
(async () => {
  server = spawn(
    process.env.PYTHON || "python3",
    ["scripts/preview_svod.py", "--port", "0"],
    {
      cwd: require("node:path").resolve(__dirname, ".."),
      stdio: ["ignore", "pipe", "pipe"],
    },
  );
  const base = await new Promise((resolve, reject) => {
    const timer = setTimeout(
      () => reject(new Error("preview startup timeout")),
      10000,
    );
    server.once("error", reject);
    server.stdout.on("data", (chunk) => {
      const match = chunk.toString().match(/http:\/\/127\.0\.0\.1:\d+\//);
      if (match) {
        clearTimeout(timer);
        resolve(match[0]);
      }
    });
  });
  browser = await chromium.launch({
    executablePath:
      process.env.SVOD_BROWSER ||
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    headless: true,
  });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
  });
  for (const [route, params, key] of [
    ["search", { q: "Земля", offset: "0" }, "search"],
    ["article", { title: "Земля" }, "parse"],
    [
      "category",
      { title: "Категория:Планеты", continue: "" },
      "categorymembers",
    ],
    ["random", {}, "random"],
  ]) {
    const response = await context.request.get(
      base + "_svod/wikipedia/" + route,
      { params, timeout: 25000 },
    );
    assert.equal(response.status(), 200, route);
    const json = await response.json();
    assert.ok(!json.error, JSON.stringify(json.error));
    assert.ok(
      key === "parse" ? json.parse?.text : json.query?.[key]?.length,
      route + " content",
    );
    console.log("Live API:", route, "OK");
  }
  const page = await context.newPage(),
    errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(base + "#/article?q=" + encodeURIComponent("Земля"));
  await page.locator(".wiki-content").waitFor({ timeout: 30000 });
  assert.equal(await page.locator(".article-heading h1").innerText(), "Земля");
  assert.ok((await page.locator(".wiki-content").innerText()).length > 10000);
  assert.ok(
    (await page.locator('.wiki-content a[href^="#/article"]').count()) > 10,
  );
  await page.waitForFunction(
    () =>
      [...document.querySelectorAll(".wiki-content img")].some(
        (i) => i.complete && i.naturalWidth > 0,
      ),
    {},
    { timeout: 20000 },
  );
  await page.screenshot({ path: "/tmp/svod-live-desktop.png" });
  await page.setViewportSize({ width: 375, height: 850 });
  assert.ok(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  );
  await page.screenshot({ path: "/tmp/svod-live-mobile.png" });
  assert.deepEqual(errors, []);
  console.log(
    "Live browser: full article, internal links, actual images, mobile layout OK",
  );
})()
  .catch((e) => {
    console.error(e);
    process.exitCode = 1;
  })
  .finally(async () => {
    await browser?.close();
    if (server && !server.killed) {
      server.kill("SIGTERM");
      await new Promise((resolve) => server.once("exit", resolve));
    }
  });
