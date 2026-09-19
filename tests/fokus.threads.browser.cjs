/* Thread deletion, ownership and legacy tombstones in an isolated browser database. */
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium } = require("./.tmp/node_modules/playwright-core");
(async () => {
  const browser = await chromium.launch({
    executablePath:
      process.env.FOKUS_BROWSER ||
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    headless: true,
  });
  try {
    const page = await browser.newPage();
    await page.route("https://fokus.test/**", async (route) => {
      const pathname = new URL(route.request().url()).pathname;
      await route.fulfill(
        pathname === "/"
          ? {
              body: "<!doctype html><title>Thread checks</title>",
              contentType: "text/html",
            }
          : {
              body: await fs.readFile(
                path.join(
                  __dirname,
                  "../src/remnawave_manager/data/disguises/07-fokus-news",
                  pathname,
                ),
              ),
              contentType: "text/javascript",
            },
      );
    });
    await page.goto("https://fokus.test/");
    const result = await page.evaluate(async () => {
      const store = await import("./fokus-store.js?v=20260919-threads");
      const article = {
        id: "123",
        path: "/20260919/test-123.html",
        title: "Article",
      };
      const comment = (id, authorId, parent, deleted = false) => ({
        id,
        authorId,
        articleId: article.id,
        article,
        text: id,
        deleted,
        parent: parent
          ? { ref: `local:${parent}`, text: parent, name: "Author" }
          : null,
      });
      await store.change((s) => {
        s.accounts = ["a", "b"].map((id) => ({
          id,
          login: id,
          name: id,
          salt: "salt",
          hash: "hash",
        }));
        s.comments = [
          comment("grandchild", "a", "child"),
          comment("child", "b", "root"),
          comment("root", "a"),
          comment("neighbor", "b"),
        ];
        s.reactions = ["root", "child", "grandchild", "neighbor"].map((id) => ({
          accountId: "b",
          target: `local:${id}`,
          code: "s1",
        }));
      });
      store.signIn("b");
      let ownership = false;
      try {
        await store.removeComment("root");
      } catch (e) {
        ownership = e.message === "invalid";
      }
      const before = (await store.read()).comments.length;
      store.signIn("a");
      await store.removeComment("root");
      const state = await store.read();
      let staleReply = false;
      try {
        await store.comment(article, "Reply to deleted comment", {
          ref: "local:root",
        });
      } catch (e) {
        staleReply = e.message === "invalid";
      }
      // Simulate records saved by the old version; replies arrive before parents.
      await store.change((s) => {
        s.comments.push(
          comment("legacy-grandchild", "b", "legacy-child"),
          comment("legacy-child", "b", "legacy-root"),
          comment("legacy-root", "a", null, true),
        );
        s.reactions.push({
          accountId: "b",
          target: "local:legacy-child",
          code: "s1",
        });
      });
      const migrated = await store.read();
      await store.change(() => {}); // Persists normalized legacy data.
      return {
        ownership,
        before,
        staleReply,
        comments: state.comments.map((c) => c.id),
        reactions: state.reactions.map((r) => r.target),
        migrated: migrated.comments.map((c) => c.id),
        migratedReactions: migrated.reactions.map((r) => r.target),
      };
    });
    assert.equal(result.ownership, true);
    assert.equal(result.before, 4);
    assert.equal(result.staleReply, true);
    assert.deepEqual(result.comments, ["neighbor"]);
    assert.deepEqual(result.reactions, ["local:neighbor"]);
    assert.deepEqual(result.migrated, ["neighbor"]);
    assert.deepEqual(result.migratedReactions, ["local:neighbor"]);
    await page.reload();
    assert.deepEqual(
      await page.evaluate(async () =>
        (
          await (await import("./fokus-store.js?v=20260919-threads")).read()
        ).comments.map((c) => c.id),
      ),
      ["neighbor"],
    );
    console.log("Fokus thread deletion checks passed");
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
