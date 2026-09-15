/* Deterministic browser integration tests. Live CDN/nginx checks are separate. */
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { execFileSync } = require("node:child_process");
const { chromium } = require("./.tmp/node_modules/playwright-core");
const root = path.resolve(
  __dirname,
  "../src/remnawave_manager/data/disguises/03-morrow-coffee",
);
const origin = "https://morrow.test";
const policy = execFileSync(
  process.env.PYTHON || "python3",
  ["-c", "from remnawave_manager.site_policy import NODE_CSP; print(NODE_CSP)"],
  {
    env: { ...process.env, PYTHONPATH: path.resolve(__dirname, "../src") },
    encoding: "utf8",
  },
).trim();
const id = (n) => n.toString(16).padStart(32, "0");
const creator = {
  uuid: id(900),
  nickname: "morning.stories",
  firstName: "Утренние истории",
  about: "Маленькие моменты большого города",
  avatar: "https://cdn-st.rutubelist.ru/avatar.jpg",
  subscribers: 12400,
};
const rawVideo = (n) => ({
  uuid: id(n),
  creator,
  description: "Город просыпается. Поймай этот момент 🌿 #утро #город",
  link: `https://vb-rtb.uma.media/vod/${n}.mp4`,
  thumbnail: "https://cdn-st.rutubelist.ru/poster.jpg",
  videoLinks: {
    sd: `https://vb-rtb.uma.media/vod/${n}.mp4`,
    hd: `https://vb-rtb.uma.media/vod/${n}.mp4`,
  },
  publishedAt: "2026-09-14T10:00:00Z",
  likesCount: 1234,
  commentsCount: 12,
  viewsCount: 45000,
  audio: { title: "Morning light" },
});
let browser;
(async () => {
  browser = await chromium.launch({
    executablePath:
      process.env.MORROW_BROWSER ||
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    headless: true,
  });
  const context = await browser.newContext({
    locale: "ru-RU",
    viewport: { width: 1440, height: 1000 },
  });
  const errors = [],
    csp = [],
    writes = [];
  let networkFail = false;
  // Produce a tiny playable test movie with browser-native APIs; no downloaded fixture.
  const seed = await context.newPage();
  const bytes = await seed.evaluate(async () => {
    const canvas = document.createElement("canvas");
    canvas.width = 360;
    canvas.height = 640;
    const c = canvas.getContext("2d");
    const stream = canvas.captureStream(12);
    const rec = new MediaRecorder(stream, { mimeType: "video/webm" }),
      chunks = [];
    rec.ondataavailable = (e) => chunks.push(e.data);
    const done = new Promise((r) => (rec.onstop = r));
    rec.start();
    for (let i = 0; i < 12; i++) {
      c.fillStyle = "#65897b";
      c.fillRect(0, 0, 360, 640);
      c.fillStyle = "#c4dbb1";
      c.beginPath();
      c.arc(250, 150, 90, 0, 7);
      c.fill();
      c.fillStyle = "#324641";
      c.fillRect(0, 350 + i, 200, 290);
      await new Promise((r) => setTimeout(r, 90));
    }
    rec.stop();
    await done;
    stream.getTracks().forEach((t) => t.stop());
    return [...new Uint8Array(await new Blob(chunks).arrayBuffer())];
  });
  await seed.close();
  const movie = Buffer.from(bytes);
  const poster =
    '<svg xmlns="http://www.w3.org/2000/svg" width="360" height="640"><rect width="360" height="640" fill="#65897b"/><circle cx="250" cy="150" r="90" fill="#c4dbb1"/><path d="M0 350h200v290H0z" fill="#324641"/></svg>';
  const routeHandler = async (route) => {
    const req = route.request(),
      u = new URL(req.url());
    if (req.method() !== "GET") writes.push(req.url());
    if (u.origin !== origin) {
      if (u.hostname === "vb-rtb.uma.media")
        return route.fulfill({ contentType: "video/webm", body: movie });
      if (u.hostname === "cdn-st.rutubelist.ru")
        return route.fulfill({ contentType: "image/svg+xml", body: poster });
      return route.abort();
    }
    if (u.pathname.startsWith("/_morrow/yappy/")) {
      if (networkFail)
        return route.fulfill({
          status: 429,
          headers: { "Retry-After": "1" },
          contentType: "application/json",
          body: "{}",
        });
      const p = u.pathname.split("/").slice(3),
        page = Number(u.searchParams.get("page") || 1);
      let data;
      if (p[0] === "feed" || p[0] === "search")
        data = {
          results: [1, 2, 3, 4].map((n) => rawVideo(n + (page - 1) * 4)),
          next: page < 3 ? "true" : null,
        };
      else if (p[0] === "video") data = rawVideo(parseInt(p[1], 16));
      else if (p[0] === "author") data = creator;
      else if (p[0] === "author-videos")
        data = { results: [rawVideo(1), rawVideo(2)], next: null };
      else if (p[0] === "comments")
        data = {
          results:
            page === 1
              ? [
                  {
                    hex: "abc123",
                    commenterHex: id(901),
                    commenter: {
                      hex: id(901),
                      name: "Аня",
                      photo: creator.avatar,
                    },
                    markdown: "<img src=x onerror=alert(1)> Красиво!",
                    creationDate: "2026-09-14T12:00:00Z",
                    likeCount: 3,
                  },
                ]
              : [],
          next: null,
        };
      return route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(data || {}),
      });
    }
    try {
      const file = path.resolve(
        root,
        u.pathname === "/" ? "index.html" : "." + u.pathname,
      );
      if (!file.startsWith(root + path.sep)) throw Error();
      await route.fulfill({
        contentType: file.endsWith(".js")
          ? "text/javascript"
          : file.endsWith(".css")
            ? "text/css"
            : file.endsWith(".svg")
              ? "image/svg+xml"
              : "text/html",
        body: await fs.readFile(file),
        headers: { "Content-Security-Policy": policy },
      });
    } catch {
      await route.fulfill({ status: 404, body: "Missing" });
    }
  };
  await context.route("**/*", routeHandler);
  const page = await context.newPage();
  page.on("pageerror", (e) => {
    errors.push(e.message);
    console.error("PAGE", e.message);
  });
  await page.addInitScript(() =>
    document.addEventListener("securitypolicyviolation", (e) =>
      console.error("CSP:" + e.violatedDirective),
    ),
  );
  page.on("console", (m) => {
    if (m.text().startsWith("CSP:")) csp.push(m.text());
  });
  const wait = () => page.waitForTimeout(150);
  const open = async (hash) => {
    await page.goto(origin + "/" + hash);
    await wait();
  };
  const state = () =>
    page.evaluate(async () => {
      const { restoreSession } = await import("/morrow-auth.js");
      const { read } = await import("/morrow-db.js");
      const a = await restoreSession();
      return a ? (await read("videos", a.id))?.data : null;
    });
  async function register(name) {
    await page.locator(".login-button").click();
    await page.locator("dialog .text-button").click();
    await page.locator("dialog input[autocomplete=username]").fill(name);
    await page.locator("dialog input[type=password]").fill("pass12345");
    await page.locator("dialog button[type=submit]").click();
    await page.waitForSelector("dialog.auth-dialog", { state: "detached" });
  }
  await open("#/feed");
  await page.waitForSelector("video");
  await page.waitForFunction(() =>
    [...document.querySelectorAll("video")].some((v) => v.currentTime > 0),
  );
  assert.ok((await page.locator("video").count()) <= 3);
  await register("morrow_one");
  await page
    .locator(".video-slide")
    .first()
    .getByRole("button", { name: "Нравится", exact: true })
    .click();
  await page.waitForFunction(async () => {
    const { read } = await import("/morrow-db.js"),
      { restoreSession } = await import("/morrow-auth.js");
    return (
      (await read("videos", (await restoreSession()).id))?.data.likes.length ===
      1
    );
  });
  await page
    .locator(".video-slide")
    .first()
    .getByRole("button", { name: "Сохранить", exact: true })
    .click();
  await wait();
  assert.equal((await state()).saved.length, 1);
  await page
    .locator(".video-slide")
    .first()
    .getByRole("button", { name: "Комментарии", exact: true })
    .click();
  await page.waitForSelector(".comment");
  assert.equal(await page.locator(".comment-text img").count(), 0);
  await page
    .locator(".comment")
    .first()
    .getByRole("button", { name: "Ответить", exact: true })
    .click();
  await page
    .locator(".comment-form textarea")
    .fill("Мой ответ на публичный комментарий");
  await page.getByRole("button", { name: "Отправить", exact: true }).click();
  await page.waitForFunction(
    () => document.querySelectorAll(".comment").length === 2,
  );
  await page
    .locator(".comment")
    .last()
    .getByRole("button", { name: "Ответить", exact: true })
    .click();
  await page.locator(".comment-form textarea").fill("Вложенный ответ");
  await page.getByRole("button", { name: "Отправить", exact: true }).click();
  await page.waitForFunction(
    () => document.querySelectorAll(".comment").length === 3,
  );
  assert.ok((await state()).comments[1].parentId.startsWith("local:"));
  await page
    .locator(".comment")
    .nth(1)
    .getByRole("button", { name: "Редактировать", exact: true })
    .click();
  await page.locator(".comment-form textarea").fill("Исправленный ответ");
  await page.getByRole("button", { name: "Отправить", exact: true }).click();
  await wait();
  assert.equal((await state()).comments[0].text, "Исправленный ответ");
  await page
    .locator(".comments-panel")
    .screenshot({ path: "tests/.tmp/morrow-comments.png" });
  // Removing a parent preserves its reply; explicit thread removal clears descendants.
  await page
    .locator(".comment")
    .nth(1)
    .getByRole("button", { name: "Удалить", exact: true })
    .click();
  await page
    .locator("dialog")
    .last()
    .getByRole("button", { name: "Удалить", exact: true })
    .click();
  await wait();
  assert.equal((await state()).comments[0].deleted, true);
  assert.equal((await state()).comments.length, 2);
  await page
    .getByRole("button", { name: "Удалить ветку", exact: true })
    .click();
  await page
    .locator("dialog")
    .last()
    .getByRole("button", { name: "Удалить", exact: true })
    .click();
  await wait();
  assert.equal((await state()).comments.length, 0);
  await page.locator(".comments-panel>header button").click();
  await open("#/profile");
  assert.ok((await page.locator(".video-tile").count()) > 0);
  await page
    .getByRole("button", { name: "Редактировать профиль", exact: true })
    .click();
  await page.getByLabel("Имя", { exact: true }).fill("Мой профиль");
  await page.getByLabel("О себе").fill("Текст <script>alert(1)</script>");
  await page.locator("dialog button[type=submit]").click();
  await page.waitForSelector("dialog", { state: "detached" });
  assert.equal((await state()).profile.name, "Мой профиль");
  await page.locator(".profile-tabs button").nth(4).click();
  await page.locator(".profile-uploads .primary").click();
  await page.locator("dialog input[type=file]").setInputFiles({
    name: "local-video.webm",
    mimeType: "video/webm",
    buffer: movie,
  });
  await page.locator("dialog textarea").fill("Локальное видео");
  await page.locator("dialog button[type=submit]").click();
  await page.waitForSelector("dialog", { state: "detached" });
  await page.waitForSelector(".profile-uploads .video-tile");
  await page.locator(".profile-uploads .video-tile").first().click();
  await page.waitForSelector('video[src^="blob:"]');
  await open("#/settings");
  await page.getByRole("button", { name: "Выйти", exact: true }).click();
  await page
    .locator("dialog")
    .getByRole("button", { name: "Выйти", exact: true })
    .click();
  await page.waitForSelector(".login-button");
  await register("morrow_two");
  assert.equal((await state()).likes.length, 0);
  assert.equal((await state()).comments.length, 0);
  // Revision conflict must reject the stale writer, never overwrite committed data.
  const conflict = await page.evaluate(async () => {
    const { ProfileStore } = await import("/morrow-store.js");
    const { restoreSession } = await import("/morrow-auth.js");
    const a = await restoreSession(),
      one = await new ProfileStore(a).load(),
      two = await new ProfileStore(a).load();
    await one.change((d) => (d.profile.about = "winner"));
    try {
      await two.change((d) => (d.profile.about = "lost"));
      return "missed";
    } catch (e) {
      return e.message;
    }
  });
  assert.equal(conflict, "conflict");
  await page.reload();
  await page.waitForSelector("video");
  // Untrusted imports and provider fields cannot create executable URLs or cycles.
  const rejected = await page.evaluate(async () => {
    const { blankProfile, validateProfile } = await import("/morrow-store.js");
    const { video, safeURL } = await import("/morrow-yappy.js");
    const d = blankProfile();
    d.profile.avatar = "data:image/svg+xml;base64,AAAA";
    let ok = false;
    try {
      validateProfile(d);
    } catch {
      ok = true;
    }
    return (
      ok &&
      safeURL("https://vb-rtb.uma.media.evil.test/x", true) === "" &&
      safeURL("https://user:pass@vb-rtb.uma.media/x", true) === "" &&
      video({ uuid: "__proto__" }) === null
    );
  });
  assert.ok(rejected);
  // Both languages, all major screens, narrow layouts and dialogs.
  for (const lang of ["ru", "en"]) {
    await page.evaluate(async (l) => {
      const { setLanguage } = await import("/morrow-i18n.js");
      setLanguage(l);
    }, lang);
    for (const width of [320, 390, 768, 1440]) {
      await page.setViewportSize({ width, height: width < 500 ? 844 : 1000 });
      for (const screen of [
        "feed",
        "explore",
        "profile",
        "settings",
        "author/yappy/" + id(900),
      ]) {
        await open("#/" + screen);
        await page.waitForTimeout(180);
        const over = await page.evaluate(
          () =>
            document.documentElement.scrollWidth > innerWidth ||
            document.querySelector("main").scrollWidth >
              document.querySelector("main").clientWidth + 1,
        );
        assert.equal(over, false, `${lang} ${width} ${screen}`);
        if (screen === "feed") {
          assert.ok((await page.locator("video").count()) <= 3);
          if (lang === "ru" && (width === 390 || width === 1440))
            await page.screenshot({
              path: `tests/.tmp/morrow-video-${width}.png`,
            });
        }
      }
    }
  }
  await open("#/feed");
  networkFail = true;
  await page.reload();
  await page.waitForSelector("video");
  assert.ok((await page.locator("video").count()) > 0);
  networkFail = false;
  // In-app navigation restores the current slide without recreating the feed at zero.
  await page.locator(".feed").focus();
  await page.keyboard.press("ArrowDown");
  await page.waitForTimeout(700);
  const before = await page.locator(".feed").evaluate((e) => e.scrollTop);
  await page.locator('a[href="#/profile"]').first().click();
  await page.locator('a[href="#/feed"]').first().click();
  await page.waitForTimeout(300);
  const after = await page.locator(".feed").evaluate((e) => e.scrollTop);
  assert.ok(Math.abs(after - before) < 3);
  // Rejected writes preserve the committed reaction state.
  const prior = (await state()).likes.length;
  await page.evaluate(() => {
    window.originalTransaction = IDBDatabase.prototype.transaction;
    IDBDatabase.prototype.transaction = function (stores, mode, ...args) {
      if (mode === "readwrite")
        throw new DOMException("Full", "QuotaExceededError");
      return window.originalTransaction.call(this, stores, mode, ...args);
    };
  });
  await page
    .locator(".video-slide .action-button")
    .filter({ has: page.locator("svg") })
    .first()
    .click();
  await wait();
  assert.equal((await state()).likes.length, prior);
  await page.evaluate(() => {
    IDBDatabase.prototype.transaction = window.originalTransaction;
  });
  // Upgrade a real v1 IndexedDB: preserve credentials and old AI data, add video data.
  const legacyContext = await browser.newContext({ locale: "ru-RU" });
  await legacyContext.route("**/*", routeHandler);
  const legacyPage = await legacyContext.newPage();
  await legacyPage.route(origin + "/", (r) =>
    r.fulfill({
      contentType: "text/html",
      body: "<!doctype html><title>Migration seed</title>",
    }),
  );
  await legacyPage.goto(origin + "/");
  await legacyPage.evaluate(async () => {
    const salt = crypto.getRandomValues(new Uint8Array(16));
    const key = await crypto.subtle.importKey(
      "raw",
      new TextEncoder().encode("pass12345"),
      "PBKDF2",
      false,
      ["deriveBits"],
    );
    const hash = new Uint8Array(
      await crypto.subtle.deriveBits(
        { name: "PBKDF2", salt, iterations: 210000, hash: "SHA-256" },
        key,
        256,
      ),
    );
    const encode = (b) => btoa(String.fromCharCode(...b));
    await new Promise((resolve, reject) => {
      const req = indexedDB.open("morrow:workspace:v1", 1);
      req.onupgradeneeded = () => {
        req.result
          .createObjectStore("accounts", { keyPath: "id" })
          .createIndex("login", "login", { unique: true });
        req.result.createObjectStore("workspaces", { keyPath: "id" });
      };
      req.onerror = reject;
      req.onsuccess = () => {
        const db = req.result,
          tx = db.transaction(["accounts", "workspaces"], "readwrite");
        tx.objectStore("accounts").add({
          id: "legacy-user",
          login: "legacy",
          salt: encode(salt),
          hash: encode(hash),
        });
        tx.objectStore("workspaces").add({
          id: "legacy-user",
          revision: 7,
          data: {
            version: 1,
            profile: { name: "Старый профиль", avatar: "" },
            chats: [{ text: "Сохранённая переписка" }],
          },
        });
        tx.oncomplete = () => {
          db.close();
          resolve();
        };
      };
    });
  });
  await legacyPage.unroute(origin + "/");
  await legacyPage.goto(origin + "/?app=1#/profile");
  await legacyPage.locator(".login-button").click();
  await legacyPage
    .locator("dialog input[autocomplete=username]")
    .fill("legacy");
  await legacyPage.locator("dialog input[type=password]").fill("pass12345");
  await legacyPage.locator("dialog button[type=submit]").click();
  await legacyPage.waitForSelector(".profile-header");
  assert.equal(
    await legacyPage.locator(".profile-header h1").textContent(),
    "Старый профиль",
  );
  const archive = await legacyPage.evaluate(async () => {
    const { read } = await import("/morrow-db.js");
    return read("workspaces", "legacy-user");
  });
  assert.equal(archive.revision, 7);
  assert.equal(archive.data.chats[0].text, "Сохранённая переписка");
  await legacyPage.evaluate(async () => {
    const { ProfileStore } = await import("/morrow-store.js");
    const { restoreSession } = await import("/morrow-auth.js");
    const p = await new ProfileStore(await restoreSession()).load();
    await p.change((d) => (d.profile.about = "Новый профиль"));
  });
  await legacyPage.reload();
  await legacyPage.waitForSelector(".profile-header");
  assert.ok(
    (await legacyPage.locator(".profile-header").textContent()).includes(
      "Новый профиль",
    ),
  );
  await legacyContext.close();
  assert.deepEqual(errors, []);
  assert.deepEqual(csp, []);
  assert.deepEqual(writes, []);
  console.log(
    "Morrow browser checks passed: playback, accounts, replies, persistence, conflict, XSS, RU/EN and 320–1440 px.",
  );
  await browser.close();
})().catch(async (e) => {
  console.error(e);
  if (browser) {
    const pages = browser.contexts().flatMap((c) => c.pages());
    await pages
      .at(-1)
      ?.screenshot({ path: "tests/.tmp/morrow-failure.png" })
      .catch(() => {});
    await browser.close();
  }
  process.exit(1);
});
