/* Run against a site served by the real generated nginx routes. Read-only. */
const assert = require("node:assert/strict");
const { chromium } = require("./.tmp/node_modules/playwright-core");
const origin = process.env.MORROW_LIVE_ORIGIN;
if (!origin)
  throw new Error("Set MORROW_LIVE_ORIGIN to the installed site origin.");
(async () => {
  const browser = await chromium.launch({
    executablePath:
      process.env.MORROW_BROWSER ||
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    headless: true,
  });
  try {
    const page = await browser.newPage({
      viewport: { width: 1440, height: 1000 },
    });
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await page.addInitScript(() => {
      window.violations = [];
      document.addEventListener("securitypolicyviolation", (e) =>
        window.violations.push(e.violatedDirective),
      );
    });
    await page.goto(origin + "/#/feed");
    const api = await page.evaluate(async () => {
      const { search, getAuthor, getAuthorVideos, getComments, getVideo } =
        await import("./morrow-data.js");
      const s = new AbortController().signal;
      const result = await search("кот", 1, s);
      const profile = await getAuthor("443d625866434869ba23e4495d90190b", s);
      const first = await getAuthorVideos(profile.id, null, s);
      const second = await getAuthorVideos(profile.id, first.next, s);
      const discussion = await getComments(
        "96581f8233f74e108b7200c37d839703",
        1,
        s,
      );
      const video = await getVideo("96581f8233f74e108b7200c37d839703", s);
      return {
        search: result.items.length,
        author: profile.id,
        first: first.items.map((v) => v.id),
        second: second.items.map((v) => v.id),
        comments: discussion.items.length,
        video: video.id,
      };
    });
    assert.ok(api.search > 0, "Live search must return actual results.");
    assert.ok(
      api.first.length > 0 && api.second.some((id) => !api.first.includes(id)),
      "Author cursor must advance.",
    );
    const played = [];
    for (let i = 0; i < 10; i++) {
      await page.waitForFunction(
        () =>
          [...document.querySelectorAll("video")].some(
            (v) => !v.paused && v.currentTime > 0.3,
          ),
        {},
        { timeout: 20000 },
      );
      const active = await page.evaluate(() =>
        [...document.querySelectorAll("video")]
          .filter((v) => !v.paused)
          .map((v) => ({
            src: v.currentSrc,
            width: v.videoWidth,
            height: v.videoHeight,
          })),
      );
      assert.equal(active.length, 1);
      assert.ok(active[0].height > 0);
      played.push(active[0].src);
      assert.ok((await page.locator("video").count()) <= 3);
      if (i === 0)
        await page.screenshot({ path: "tests/.tmp/morrow-live-desktop.png" });
      if (i < 9) {
        await page.locator(".feed").focus();
        await page.keyboard.press("ArrowDown");
        await page.waitForTimeout(800);
      }
    }
    assert.equal(new Set(played).size, 10, "Ten different videos should play.");
    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: "tests/.tmp/morrow-live-mobile.png" });
    assert.deepEqual(errors, []);
    assert.deepEqual(await page.evaluate(() => window.violations), []);
    console.log(
      JSON.stringify({
        searchResults: api.search,
        authorPages: [api.first.length, api.second.length],
        publicComments: api.comments,
        played: new Set(played).size,
        jsErrors: errors.length,
        cspViolations: 0,
      }),
    );
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error(e);
  process.exit(1);
});
