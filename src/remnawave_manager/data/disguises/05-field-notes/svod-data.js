import { get, cacheArticle } from "./svod-store.js";
const PREFIX = "/_svod/wikipedia/";
let retryAt = 0;
export async function request(route, params = {}, signal) {
  if (Date.now() < retryAt) throw new Error("rateLimit");
  const controller = new AbortController(),
    timeout = setTimeout(() => controller.abort(), 18000);
  const abort = () => controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  if (signal?.aborted) controller.abort();
  try {
    const response = await fetch(
      PREFIX + route + "?" + new URLSearchParams(params),
      {
        signal: controller.signal,
        credentials: "omit",
        redirect: "error",
        headers: { Accept: "application/json" },
      },
    );
    if (response.status === 429) {
      const retry = response.headers.get("Retry-After"),
        seconds = Number(retry);
      retryAt =
        Date.now() +
        Math.min(
          300000,
          Math.max(
            1000,
            Number.isFinite(seconds) && retry
              ? seconds * 1000
              : Date.parse(retry) - Date.now() || 10000,
          ),
        );
      throw new Error("rateLimit");
    }
    if (!response.ok) throw new Error("unavailable");
    if (Number(response.headers.get("Content-Length")) > 6 * 1024 * 1024)
      throw new Error("unavailable");
    const reader = response.body.getReader();
    let count = 0;
    const parts = [];
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      count += value.length;
      if (count > 6 * 1024 * 1024) {
        await reader.cancel();
        throw new Error("unavailable");
      }
      parts.push(value);
    }
    const bytes = new Uint8Array(count);
    let at = 0;
    for (const part of parts) {
      bytes.set(part, at);
      at += part.length;
    }
    const result = JSON.parse(new TextDecoder().decode(bytes));
    if (result.error)
      throw new Error(
        ["missingtitle", "invalidtitle"].includes(result.error.code)
          ? "missing"
          : "unavailable",
      );
    return result;
  } finally {
    clearTimeout(timeout);
    signal?.removeEventListener("abort", abort);
  }
}
export async function search(query, offset = 0, signal) {
  const data = await request("search", { q: query, offset }, signal);
  return {
    items: (data.query?.search || []).filter(
      (x) => Number.isSafeInteger(x.pageid) && typeof x.title === "string",
    ),
    next: data.continue?.sroffset,
  };
}
export async function article(title, signal) {
  try {
    const data = await request("article", { title }, signal),
      p = data.parse;
    if (!p || !Number.isSafeInteger(p.pageid) || typeof p.title !== "string")
      throw new Error("missing");
    const html = typeof p.text === "string" ? p.text : p.text?.["*"];
    if (typeof html !== "string") throw new Error("unavailable");
    const result = {
      pageid: p.pageid,
      title: p.title,
      html,
      revid: p.revid,
      categories: (p.categories || [])
        .filter((x) => !Object.hasOwn(x, "hidden"))
        .map((x) => x["*"] || x.category)
        .filter((x) => typeof x === "string"),
    };
    await cacheArticle(result);
    return result;
  } catch (e) {
    if (signal?.aborted) throw e;
    if (e.message === "missing") throw e;
    try {
      const entry = await get("cache", title);
      if (entry?.article && Date.now() - entry.at < 7 * 86400000)
        return { ...entry.article, stale: entry.at };
    } catch {}
    throw e;
  }
}
export async function random(signal) {
  const data = await request("random", {}, signal);
  const title = data.query?.random?.[0]?.title;
  if (typeof title !== "string") throw new Error("unavailable");
  return title;
}
export async function category(title, continuation = "", signal) {
  const data = await request(
    "category",
    { title, continue: continuation },
    signal,
  );
  return {
    items: (data.query?.categorymembers || []).filter(
      (x) => typeof x.title === "string",
    ),
    next: data.continue?.cmcontinue,
  };
}
export const original = (title) =>
  "https://ru.wikipedia.org/wiki/" + encodeURIComponent(title.replaceAll(" ", "_"));
