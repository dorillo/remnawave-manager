import { video, author, comment, validId } from "./morrow-yappy.js";
const cache = new Map();
const KEY = "morrow:video-cache:v1";
try {
  const stored = JSON.parse(localStorage.getItem(KEY));
  if (Array.isArray(stored))
    for (const [key, value] of stored.slice(-80))
      if (typeof key === "string" && value?.at > Date.now() - 604800000)
        cache.set(key, value);
} catch {
  /* Cache is optional. */
}
export function clearCache() {
  cache.clear();
  try {
    localStorage.removeItem(KEY);
  } catch {}
}
function saveCache() {
  while (cache.size > 80) cache.delete(cache.keys().next().value);
  try {
    const entries = [...cache];
    while (JSON.stringify(entries).length > 1800000) entries.shift();
    localStorage.setItem(KEY, JSON.stringify(entries));
  } catch {}
}
let activeRequests = 0;
const waiting = [];
async function acquire(signal) {
  if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
  if (activeRequests < 3) {
    activeRequests++;
    return;
  }
  await new Promise((resolve, reject) => {
    const entry = {
      resolve: () => {
        signal?.removeEventListener("abort", abort);
        resolve();
      },
    };
    const abort = () => {
      const i = waiting.indexOf(entry);
      if (i >= 0) waiting.splice(i, 1);
      reject(new DOMException("Aborted", "AbortError"));
    };
    waiting.push(entry);
    signal?.addEventListener("abort", abort, { once: true });
  });
}
function release() {
  const entry = waiting.shift();
  if (entry) entry.resolve();
  else activeRequests--;
}
let cooldown = 0;
async function request(path, signal, fresh = false) {
  if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
  const old = cache.get(path);
  if (!fresh && old?.at > Date.now() - 900000)
    return { raw: old.raw, stale: false };
  try {
    if (cooldown > Date.now()) throw new Error("rateLimit");
    await acquire(signal);
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(abort, 12000);
    try {
      const response = await fetch("/_morrow/yappy/" + path, {
        credentials: "omit",
        cache: fresh ? "no-store" : "default",
        signal: controller.signal,
        redirect: "error",
        headers: { Accept: "application/json" },
      });
      if (response.status === 429) {
        const value = response.headers.get("Retry-After");
        const seconds = Number(value);
        cooldown =
          Date.now() +
          Math.min(
            300000,
            Math.max(
              1000,
              Number.isFinite(seconds) && value
                ? seconds * 1000
                : Date.parse(value) - Date.now() || 30000,
            ),
          );
        throw new Error("rateLimit");
      }
      if (!response.ok)
        throw new Error(response.status === 404 ? "unavailable" : "network");
      if (
        !(response.headers.get("content-type") || "").includes(
          "application/json",
        )
      )
        throw new Error("network");
      const body = await response.text();
      if (body.length > 4000000) throw new Error("network");
      const raw = JSON.parse(body);
      if (!raw || typeof raw !== "object") throw new Error("network");
      cache.set(path, { raw, at: Date.now() });
      saveCache();
      return { raw, stale: false };
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      release();
    }
  } catch (e) {
    if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
    if (old?.at > Date.now() - 604800000) return { raw: old.raw, stale: true };
    throw new Error(
      e.message === "rateLimit"
        ? "rateLimit"
        : e.message === "unavailable"
          ? "unavailable"
          : "network",
    );
  }
}
const check = (id) => {
  if (!validId(id)) throw new Error("unavailable");
  return id;
};
const pageNumber = (page) =>
  Math.max(1, Math.min(9999, Math.floor(Number(page)) || 1));
async function list(path, signal, fresh = false, kind = "video") {
  const { raw, stale } = await request(path, signal, fresh);
  if (!Array.isArray(raw.results)) throw new Error("network");
  return {
    items: raw.results
      .slice(0, 100)
      .map(kind === "comment" ? comment : video)
      .filter(Boolean),
    next: raw.next && raw.next !== "false" ? raw.next : null,
    stale,
  };
}
export const getFeed = (page, signal, fresh = false) =>
  list(`feed?page=${pageNumber(page)}`, signal, fresh);
export const search = (query, page, signal) =>
  list(
    `search?query=${encodeURIComponent(query.slice(0, 120))}&page=${pageNumber(page)}`,
    signal,
  );
export const getComments = (id, page, signal) =>
  list(
    `comments/${check(id)}?page=${pageNumber(page)}`,
    signal,
    true,
    "comment",
  );
export async function getVideo(id, signal, fresh = false) {
  const { raw } = await request(`video/${check(id)}`, signal, fresh);
  const result = video(raw);
  if (!result) throw new Error("unavailable");
  return result;
}
export async function getAuthor(id, signal) {
  const { raw } = await request(`author/${check(id)}`, signal);
  const result = author(raw);
  if (!result) throw new Error("unavailable");
  return result;
}
export async function getAuthorVideos(id, cursor, signal) {
  const result = await list(
    `author-videos/${check(id)}${cursor ? "?created=" + encodeURIComponent(cursor) : ""}`,
    signal,
  );
  result.next = (result.next && result.items.at(-1)?.publishedAt) || null;
  return result;
}
export function cachedVideos() {
  const items = new Map();
  for (const { raw } of cache.values())
    for (const v of Array.isArray(raw.results) ? raw.results : [raw]) {
      const item = video(v);
      if (item) items.set(item.id, item);
    }
  return [...items.values()].slice(-500);
}
