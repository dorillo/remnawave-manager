const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { chromium, webkit } = require("./.tmp/node_modules/playwright-core");
const root = path.resolve(
  __dirname,
  "../src/remnawave_manager/data/disguises/06-loop-archive",
);
const stillGif = Buffer.from(
  "R0lGODlhEAAQAIAAAKp43P///ywAAAAAEAAQAAACwQRBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEARBEAUAOw==",
  "base64",
);
// Two looping frames with different palettes exercise actual animated uploads.
const frame = stillGif.subarray(19, -1);
const secondDescriptor = Buffer.from(frame.subarray(0, 10));
secondDescriptor[9] = 0x80;
const delay = Buffer.from([0x21, 0xf9, 4, 0, 20, 0, 0, 0]);
const gif = Buffer.concat([
  stillGif.subarray(0, 19),
  Buffer.from([0x21, 0xff, 11]),
  Buffer.from("NETSCAPE2.0"),
  Buffer.from([3, 1, 0, 0, 0]),
  delay,
  frame,
  delay,
  secondDescriptor,
  Buffer.from([255, 255, 255, 170, 120, 220]),
  frame.subarray(10),
  Buffer.from([0x3b]),
]);
let browser,
  userData,
  fail = false;
(async () => {
  userData = await fs.mkdtemp(
    path.join(require("node:os").tmpdir(), "loop-uploads-"),
  );
  const options = {
    headless: true,
    viewport: { width: 390, height: 844 },
    reducedMotion: "reduce",
    acceptDownloads: true,
  };
  const useWebKit = process.env.LOOP_ENGINE === "webkit";
  // WebKit's ephemeral context does not support IndexedDB Blob persistence.
  browser = useWebKit
    ? await webkit.launchPersistentContext(userData, options)
    : await chromium.launch({
        executablePath:
          "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        headless: true,
      });
  const context = useWebKit ? browser : await browser.newContext(options);
  const page = await context.newPage(),
    errors = [],
    requests = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await context.addInitScript(() => {
    window.policyViolations = [];
    document.addEventListener("securitypolicyviolation", (e) =>
      window.policyViolations.push(e.violatedDirective),
    );
  });
  const raw = (id) => ({
    id,
    fileType: 1,
    title: `Remote ${id}`,
    tags: ["kitten"],
    width: 480,
    height: 480,
    cloudSource: `https://media.gifs.ru/${String(id).padStart(40, "a")}.gif`,
    cloudSource300: `https://media.gifs.ru/${String(id).padStart(40, "a")}_300.webp`,
  });
  await context.route("**/*", async (route) => {
    const request = route.request(),
      url = new URL(request.url());
    if (url.protocol === "blob:") return route.continue();
    assert.equal(url.origin, "http://localhost:55307");
    if (url.pathname.startsWith("/_loop/media/"))
      return route.fulfill({ contentType: "image/gif", body: gif });
    if (url.pathname.startsWith("/_loop/gifs/")) {
      requests.push({ path: url.pathname, body: request.postData() });
      if (fail) return route.fulfill({ status: 503, body: "{}" });
      let data;
      if (url.pathname.endsWith("categories"))
        data = [{ id: 11, title: "Cats" }];
      else if (url.pathname.endsWith("trending")) data = [];
      else if (url.pathname.includes("/item/"))
        data = raw(Number(url.pathname.split("/").pop()));
      else {
        const skip = Number(
          url.searchParams.get("skip") || request.postDataJSON()?.skip || 0,
        );
        data = {
          result:
            skip >= 96
              ? []
              : Array.from({ length: 24 }, (_, i) => raw(i + skip + 1)),
        };
      }
      return route.fulfill({
        contentType: "application/json",
        body: JSON.stringify(data),
      });
    }
    const base = url.pathname.startsWith("/shared/") ? path.dirname(root) : root;
    const file = path.join(
      base,
      url.pathname === "/" ? "index.html" : url.pathname,
    );
    assert(file.startsWith(base + path.sep));
    try {
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
  });
  await page.goto("http://localhost:55307");
  // Upgrade a populated v1 database without losing old accounts or saved data.
  await page.evaluate(async () => {
    const store = await import("./loop-store.js");
    (await store.db()).close();
    await new Promise((resolve, reject) => {
      const r = indexedDB.deleteDatabase("loop:gifs:v1");
      r.onsuccess = resolve;
      r.onerror = reject;
    });
    await new Promise((resolve, reject) => {
      const r = indexedDB.open("loop:gifs:v1", 1);
      r.onupgradeneeded = () => {
        const a = r.result.createObjectStore("accounts", { keyPath: "id" });
        a.createIndex("login", "login", { unique: true });
        for (const n of ["likes", "collections"])
          r.result.createObjectStore(n, { keyPath: "id" });
        a.put({ id: "legacy", login: "legacy", name: "Legacy" });
      };
      r.onsuccess = () => {
        r.result.close();
        resolve();
      };
      r.onerror = reject;
    });
  });
  await page.reload();
  assert(
    await page.evaluate(
      async () =>
        !!(await (await import("./loop-store.js")).get("accounts", "legacy")),
    ),
  );
  async function register(login) {
    await page.locator(".header-account").click();
    await page
      .getByRole("dialog")
      .getByRole("button", { name: "Создать аккаунт", exact: true })
      .click();
    await page.getByLabel("Имя", { exact: true }).fill(login);
    await page.getByLabel("Логин", { exact: true }).fill(login);
    await page.getByLabel("Пароль", { exact: true }).fill("password123");
    await page
      .getByRole("dialog")
      .getByRole("button", { name: "Создать аккаунт", exact: true })
      .click();
    await page.waitForFunction(() => !document.querySelector("dialog"));
  }
  await register("creator");
  async function upload(type, name, mimeType, buffer, title) {
    await page.evaluate(() => (location.hash = "#/uploads"));
    await page
      .getByRole("button", { name: "Загрузить медиа", exact: true })
      .click();
    await page.getByRole("dialog").waitFor();
    if (type === "gif") {
      for (const theme of ["dark", "light"]) {
        await page.evaluate(
          (theme) => (document.documentElement.dataset.theme = theme),
          theme,
        );
        for (const width of [320, 390, 768]) {
          await page.setViewportSize({ width, height: 844 });
          assert(
            await page
              .getByRole("dialog")
              .evaluate((d) => d.scrollWidth <= d.clientWidth),
          );
          assert.equal(
            (await page.getByLabel("Тип материала").boundingBox()).height,
            46,
          );
        }
      }
      await page.setViewportSize({ width: 390, height: 844 });
      await page
        .getByRole("dialog")
        .screenshot({ path: "/tmp/loop-upload-mobile.png" });
    }
    await page.getByLabel("Тип материала").selectOption(type);
    await page
      .getByLabel("Файл", { exact: true })
      .setInputFiles({ name, mimeType, buffer });
    assert.equal(await page.locator(".file-picker-name").textContent(), name);
    assert.equal(await page.locator(".upload-formats").count(), 0);
    await page.getByLabel("Название", { exact: true }).fill(title);
    await page.getByLabel("Теги через запятую").fill("kitten, happy");
    await page.getByLabel("Категория", { exact: true }).selectOption("11");
    await page
      .getByRole("dialog")
      .getByRole("button", { name: "Загрузить медиа", exact: true })
      .click();
    await page.waitForFunction(
      () =>
        (location.hash.startsWith("#/item/local-") &&
          !document.querySelector("dialog")) ||
        document.querySelector(".upload-form .form-error")?.textContent,
      {},
      { timeout: 10000 },
    );
    assert.equal(
      (await page.locator(".upload-form .form-error").count())
        ? await page.locator(".upload-form .form-error").textContent()
        : "",
      "",
      "upload form error",
    );
    await page.locator(".detail").waitFor();
    return (await page.evaluate(() => location.hash)).slice("#/item/".length);
  }
  const first = await upload(
    "gif",
    "mine.gif",
    "image/gif",
    gif,
    "<img src=x onerror=alert(1)> Kitten",
  );
  assert.equal(await page.locator(".detail h1 img").count(), 0);
  await page.locator(".detail [data-like]").click();
  await page.waitForFunction(
    () =>
      document
        .querySelector(".detail [data-like]")
        ?.getAttribute("aria-pressed") === "true",
  );
  assert.equal(
    await page.locator(".detail [data-like]").getAttribute("aria-pressed"),
    "true",
  );
  await page.getByRole("button", { name: "В коллекцию", exact: true }).click();
  await page
    .getByRole("button", { name: "Новая коллекция", exact: true })
    .click();
  await page.getByLabel("Название коллекции").fill("Mine");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Подтвердить", exact: true })
    .click();
  await page.waitForFunction(() => !document.querySelector("dialog"));
  const collection = await page.evaluate(
    async () =>
      (await (await import("./loop-store.js")).owned("collections"))[0].id,
  );
  await page.reload();
  await page
    .waitForFunction(() => {
      const video = document.querySelector(".detail video");
      return video?.readyState >= 1 && Number.isFinite(video.duration);
    })
    .catch(async (error) => {
      console.error(
        await page.locator(".gif-player").evaluate((root) => {
          const v = root.querySelector("video");
          return {
            text: root.textContent,
            src: v.src,
            duration: v.duration,
            state: v.readyState,
            error: v.error?.message,
          };
        }),
      );
      throw error;
    });
  assert.equal(
    await page.locator(".detail [data-like]").getAttribute("aria-pressed"),
    "true",
  );
  const player = page.locator(".gif-player video");
  assert(
    await player.evaluate(
      (video) => video.controls && video.paused && video.duration > 0,
    ),
  );
  await player.evaluate((video) => video.play());
  await page.waitForFunction(
    () => document.querySelector(".gif-player video")?.currentTime > 0.05,
  );
  await player.evaluate((video) => video.pause());
  const position = await player.evaluate((video) => video.currentTime);
  await page.waitForTimeout(120);
  assert.equal(await player.evaluate((video) => video.currentTime), position);
  await player.evaluate(
    (video) =>
      new Promise((resolve) => {
        video.addEventListener("seeked", resolve, { once: true });
        video.currentTime = video.duration * 0.75;
      }),
  );
  assert(
    await player.evaluate(
      (video) => Math.abs(video.currentTime - video.duration * 0.75) < 0.1,
    ),
  );
  const colors = await player.evaluate(async (video) => {
    const canvas = document.createElement("canvas");
    canvas.width = canvas.height = 1;
    const context = canvas.getContext("2d");
    const colors = [];
    for (const ratio of [0.15, 0.8]) {
      await new Promise((resolve) => {
        video.addEventListener("seeked", resolve, { once: true });
        video.currentTime = video.duration * ratio;
      });
      context.drawImage(video, 0, 0, 1, 1);
      colors.push([...context.getImageData(0, 0, 1, 1).data]);
    }
    return colors;
  });
  assert.notDeepEqual(
    colors[0],
    colors[1],
    `Native player must contain both GIF frames: ${JSON.stringify(colors)}`,
  );
  await player.screenshot({
    path: `/tmp/loop-gif-player-${useWebKit ? "webkit" : "chrome"}.png`,
  });
  await player.evaluate((video) => video.dispatchEvent(new Event("error")));
  await page.locator(".gif-player-status button").click();
  await page.waitForFunction(
    () => document.querySelector(".gif-player video")?.readyState >= 1,
  );
  assert.equal(await page.locator(".gif-player-status").count(), 0);
  assert(
    await page.evaluate(
      async (bytes) => {
        const { GifReader } = await import("./loop-gif-reader.js");
        const original = new Uint8Array(bytes);
        const reader = new GifReader(original);
        const invalidCode = original.slice();
        invalidCode[reader.frameInfo(0).data_offset] = 0;
        const invalidScreen = original.slice();
        invalidScreen[6] = invalidScreen[7] = 0;
        return [original.slice(0, 12), invalidCode, invalidScreen].every(
          (value) => {
            try {
              new GifReader(value);
              return false;
            } catch {
              return true;
            }
          },
        );
      },
      [...gif],
    ),
  );
  const downloading = page.waitForEvent("download");
  await page.getByRole("link", { name: "Скачать", exact: true }).click();
  const downloaded = await downloading;
  assert.equal(await downloaded.failure(), null);
  assert.deepEqual(await fs.readFile(await downloaded.path()), gif);
  await page.getByRole("button", { name: "Встроить", exact: true }).click();
  const code = await page.getByLabel("HTML-код").inputValue();
  assert(code.includes(`loop-${first}.gif`));
  assert(!code.includes("blob:"));
  assert(!code.includes("<script"));
  await page.keyboard.press("Escape");
  const png = Buffer.from(
    await page.evaluate(() => {
      const c = document.createElement("canvas");
      c.width = 32;
      c.height = 24;
      c.getContext("2d").fillRect(0, 0, 10, 10);
      return c.toDataURL("image/png").split(",")[1];
    }),
    "base64",
  );
  const sticker = await upload(
    "sticker",
    "sticker.png",
    "image/png",
    png,
    "My sticker",
  );
  await upload("sticker", "animated.gif", "image/gif", gif, "Animated sticker");
  await page.waitForFunction(
    () => document.querySelector(".gif-player video")?.readyState >= 1,
  );
  assert(
    await page
      .locator(".gif-player video")
      .evaluate((video) => video.controls && Number.isFinite(video.duration)),
  );
  await page.locator(".gif-player video").evaluate((video) => video.play());
  await page.waitForFunction(
    () => document.querySelector(".gif-player video")?.currentTime > 0.05,
  );
  await page
    .getByRole("button", { name: "Удалить материал", exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Подтвердить", exact: true })
    .click();
  await page
    .getByRole("heading", { name: "Мои материалы", exact: true })
    .waitFor();
  const recording = await page.evaluate(async () => {
    const c = document.createElement("canvas");
    c.width = 32;
    c.height = 32;
    const ctx = c.getContext("2d");
    ctx.fillStyle = "red";
    ctx.fillRect(0, 0, 32, 32);
    const stream = c.captureStream(10),
      mime = ["video/mp4", "video/webm"].find((m) =>
        MediaRecorder.isTypeSupported(m),
      );
    const recorder = new MediaRecorder(stream, { mimeType: mime }),
      chunks = [];
    recorder.ondataavailable = (e) => chunks.push(e.data);
    const blob = await new Promise((resolve) => {
      recorder.onstop = () => resolve(new Blob(chunks, { type: mime }));
      recorder.start();
      const timer = setInterval(() => {
        ctx.fillStyle = Math.random() > 0.5 ? "red" : "blue";
        ctx.fillRect(0, 0, 32, 32);
      }, 30);
      setTimeout(() => {
        clearInterval(timer);
        recorder.stop();
        stream.getTracks().forEach((t) => t.stop());
      }, 350);
    });
    return {
      mime,
      bytes: Array.from(new Uint8Array(await blob.arrayBuffer())),
    };
  });
  const clip = await upload(
    "clip",
    "clip." + recording.mime.split("/")[1],
    recording.mime,
    Buffer.from(recording.bytes),
    "My clip",
  );
  await page.waitForFunction(
    () => document.querySelector(".detail video")?.readyState >= 1,
  );
  await page.locator(".detail video").evaluate((video) => video.play());
  await page.waitForFunction(
    () => document.querySelector(".detail video")?.readyState >= 2,
  );
  await page.getByRole("button", { name: "Встроить", exact: true }).click();
  assert(
    (await page.getByLabel("HTML-код").inputValue()).startsWith("<video "),
  );
  await page.keyboard.press("Escape");
  assert(
    !requests.some((r) =>
      /kitten|happy|My kitten|My sticker|Animated sticker/.test(r.body || ""),
    ),
    "Opening own media must not send its metadata to remote search",
  );
  for (const [type, id] of [
    ["gif", first],
    ["sticker", sticker],
    ["clip", clip],
  ]) {
    await page.evaluate(
      (type) => (location.hash = `#/explore?type=${type}`),
      type,
    );
    await page.locator(`.card-media[href="#/item/${id}"]`).waitFor();
    const order = await page
      .locator(".card-media")
      .evaluateAll((nodes) => nodes.map((n) => n.getAttribute("href")));
    assert.equal(order.indexOf(`#/item/${id}`), 2);
    assert.equal(order.length, 48);
    await page.locator(".load-more button").click();
    await page.waitForFunction(
      () => document.querySelectorAll(".card").length === 96,
    );
    assert.equal(
      await page.locator(`.card-media[href="#/item/${id}"]`).count(),
      1,
    );
  }
  await page.evaluate(
    () => (location.hash = "#/search?type=gif&q=kitten&category=11"),
  );
  await page.locator(`.card-media[href="#/item/${first}"]`).waitFor();
  await page.evaluate(() => (location.hash = "#/item/1"));
  await page.locator(`.related .card-media[href="#/item/${first}"]`).waitFor();
  fail = true;
  await page.evaluate(() => (location.hash = "#/search?type=gif&q=happy"));
  await page.locator(`.card-media[href="#/item/${first}"]`).waitFor();
  fail = false;
  // Only an explicit user search may send a title/tag to the remote source.
  const privateQueries = requests.filter(
    (r) =>
      r.body && /My kitten|My sticker|Animated sticker|kitten/.test(r.body),
  );
  assert(
    privateQueries.every((r) => {
      const body = JSON.parse(r.body);
      return body.query === "kitten" && r.path.endsWith("search-gif");
    }),
  );
  // Format checks reject executable and corrupted content regardless of declared MIME.
  await page.evaluate(() => (location.hash = "#/uploads"));
  await page
    .getByRole("button", { name: "Загрузить медиа", exact: true })
    .click();
  await page.getByLabel("Файл", { exact: true }).setInputFiles({
    name: "fake.gif",
    mimeType: "image/gif",
    buffer: Buffer.from('<svg onload="alert(1)"></svg>'),
  });
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Загрузить медиа", exact: true })
    .click();
  await page
    .getByText("Формат файла не соответствует выбранному типу.", {
      exact: false,
    })
    .waitFor();
  await page.keyboard.press("Escape");
  // No application size cap: a valid GIF with >50 MiB of trailing data is stored intact.
  const large = await page.evaluate(
    async (bytes) => {
      const uploads = await import("./loop-uploads.js"),
        store = await import("./loop-store.js");
      const file = new File(
        [new Uint8Array(bytes), new Uint8Array(51 * 1024 * 1024)],
        "large.gif",
        { type: "image/gif" },
      );
      const saved = await uploads.save({
        file,
        type: "gif",
        title: "Large GIF",
        tags: [],
        category: "",
      });
      const size = (await store.get("files", saved.id)).blob.size;
      await uploads.remove(saved.id);
      return { size, expected: file.size };
    },
    [...gif],
  );
  assert.equal(large.size, large.expected);
  const mixed = await page.evaluate(async () => {
    const { mixedFeed } = await import("./loop-uploads.js");
    const local = Array.from({ length: 53 }, (_, i) => ({ id: `local-${i}` }));
    const all = [];
    let cursor,
      more = true;
    while (more) {
      const result = await mixedFeed({}, cursor, local, async ({ skip }) => ({
        items: Array.from(
          { length: Math.max(0, Math.min(48, 93 - skip)) },
          (_, i) => ({ id: i + skip }),
        ),
        more: skip + 48 < 93,
        nextSkip: skip + 48,
      }));
      if (result.items.length > 48) throw new Error("oversized batch");
      all.push(...result.items.map((item) => item.id));
      cursor = result.cursor;
      more = result.more;
    }
    return { total: all.length, unique: new Set(all).size };
  });
  assert.deepEqual(mixed, { total: 146, unique: 146 });
  // Another account cannot read or delete the creator's files.
  await page.evaluate(async () => {
    (await import("./loop-store.js")).signOut();
    location.hash = "#/profile";
  });
  await page.reload();
  await register("second");
  await page.evaluate((id) => (location.hash = `#/item/${id}`), first);
  await page
    .getByText("Материал больше не доступен.", { exact: true })
    .waitFor();
  assert.equal(await page.locator(".detail").count(), 0);
  assert.equal(
    await page.evaluate(async (id) => {
      const uploads = await import("./loop-uploads.js");
      try {
        await uploads.remove(id);
        return false;
      } catch {
        return true;
      }
    }, first),
    true,
  );
  await page.evaluate(() => (location.hash = "#/explore"));
  await page.locator(".card").first().waitFor();
  assert.equal(await page.locator('.card-media[href*="local-"]').count(), 0);
  await page.evaluate(async () => {
    const store = await import("./loop-store.js");
    const creator = (await store.all("accounts")).find(
      (a) => a.login === "creator",
    );
    store.signIn(creator.id);
  });
  await page.reload();
  await page.evaluate((id) => (location.hash = `#/item/${id}`), first);
  await page
    .getByRole("button", { name: "Удалить материал", exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Подтвердить", exact: true })
    .click();
  await page
    .getByRole("heading", { name: "Мои материалы", exact: true })
    .waitFor();
  assert.deepEqual(
    await page.evaluate(
      async ({ id, collection }) => {
        const store = await import("./loop-store.js");
        return [
          !!(await store.get("uploads", id)),
          !!(await store.get("files", id)),
          (await store.owned("likes")).some((x) => x.media.id === id),
          (await store.get("collections", collection)).items.some(
            (x) => x.id === id,
          ),
        ];
      },
      { id: first, collection },
    ),
    [false, false, false, false],
  );
  await page.evaluate(() => (location.hash = "#/profile"));
  await page
    .getByRole("button", { name: "Удалить аккаунт", exact: true })
    .click();
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Подтвердить", exact: true })
    .click();
  await page.getByRole("heading", { name: "Ваш профиль ждёт" }).waitFor();
  assert.deepEqual(
    await page.evaluate(async () => {
      const store = await import("./loop-store.js");
      return [
        (await store.all("uploads")).length,
        (await store.all("files")).length,
      ];
    }),
    [0, 0],
  );
  assert(
    !requests.some(
      (r) => /local-/.test(r.path) || /blob:|base64|local-/.test(r.body || ""),
    ),
  );
  assert.deepEqual(errors, []);
  assert.deepEqual(await page.evaluate(() => window.policyViolations), []);
  console.log(
    "Loop uploads: migration, GIF/sticker/video, mixed feeds, offline, persistence, downloads, >50 MiB, ownership and atomic deletion passed.",
  );
})()
  .catch((error) => {
    console.error(error);
    process.exitCode = 1;
  })
  .finally(async () => {
    await browser?.close();
    if (userData) await fs.rm(userData, { recursive: true, force: true });
  });
