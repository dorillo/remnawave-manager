/* Optional real-content layout check. Supply downloaded MediaWiki parse responses. */
const fs = require("node:fs/promises"),
  path = require("node:path"),
  assert = require("node:assert/strict");
const { chromium } = require("./.tmp/node_modules/playwright-core");
const root = path.resolve(
  __dirname,
  "../src/remnawave_manager/data/disguises/05-field-notes",
);
const fixtureDir = process.env.SVOD_CONTENT_DIR;
if (!fixtureDir) {
  console.log(
    "Set SVOD_CONTENT_DIR to a directory of MediaWiki parse JSON files.",
  );
  process.exit(0);
}
let browser;
(async () => {
  browser = await chromium.launch({
    executablePath:
      process.env.SVOD_BROWSER ||
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    headless: true,
  });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 950 },
  });
  let content;
  await context.route("**/*", async (route) => {
    const u = new URL(route.request().url());
    if (u.pathname.startsWith("/_svod/"))
      return route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(content),
      });
    if (u.origin !== "https://svod.test")
      return route.fulfill({
        contentType: "image/svg+xml",
        body: '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200"><rect width="300" height="200" fill="#d9e2da"/></svg>',
      });
    const p = path.resolve(
      root,
      "." + (u.pathname === "/" ? "/index.html" : u.pathname),
    );
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
  const page = await context.newPage(),
    errors = [];
  page.on("pageerror", (e) => errors.push(e.message));
  for (const file of await fs.readdir(fixtureDir)) {
    if (!file.endsWith(".json")) continue;
    content = JSON.parse(
      await fs.readFile(path.join(fixtureDir, file), "utf8"),
    );
    if (!content.parse) continue;
    for (const width of [320, 768, 900, 1440]) {
      await page.setViewportSize({ width, height: 950 });
      await page.goto(
        "https://svod.test/#/article?q=" +
          encodeURIComponent(content.parse.title),
      );
      await page.locator(".wiki-content").waitFor();
      assert.equal(
        await page
          .locator(
            ".wiki-content script,.wiki-content style,.wiki-content iframe,.wiki-content form",
          )
          .count(),
        0,
      );
      if (
        await page.evaluate(
          () => document.documentElement.scrollWidth > innerWidth,
        )
      )
        console.log(
          await page.evaluate(() =>
            [...document.querySelectorAll("*")]
              .filter(
                (e) =>
                  e.getBoundingClientRect().right > innerWidth &&
                  !e.closest(".table-scroll"),
              )
              .slice(0, 15)
              .map((e) => [
                e.tagName,
                e.className,
                e.getBoundingClientRect().width,
                e.textContent.slice(0, 80),
              ]),
          ),
        );
      assert.ok(
        await page.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        `${file} overflow at ${width}`,
      );
      assert.ok((await page.locator(".wiki-content").innerText()).length > 500);
      await page.screenshot({
        path: `/tmp/svod-real-${content.parse.pageid}-${width}.png`,
      });
    }
    console.log("Real content layout:", content.parse.title);
  }
  assert.deepEqual(errors, []);
})()
  .catch((e) => {
    console.error(e);
    process.exitCode = 1;
  })
  .finally(async () => browser?.close());
