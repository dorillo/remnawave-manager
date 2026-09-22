import { confirmedPages } from '../shared/pagination.js';
export const pageSize = 48;
const sourcePageSize = 24;
const kinds = { gif: 1, sticker: 2, clip: 3 };
const memory = new Map();
export function mediaURL(value) {
  try {
    const u = new URL(value);
    if (
      u.origin !== "https://media.gifs.ru" ||
      u.search ||
      u.hash ||
      u.username ||
      u.password ||
      !/^\/[a-f0-9]{32,64}(?:_(?:150|300|500|preview))?\.(?:gif|webp|mp4|png|jpg)$/.test(
        u.pathname,
      )
    )
      return "";
    return "/_loop/media" + u.pathname;
  } catch {
    return "";
  }
}
function dimension(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0
    ? Math.max(1, Math.min(Math.round(number), 10000))
    : 480;
}
function validId(id) {
  return (typeof id === "string" && /^[A-Za-z0-9]{1,12}$/.test(id)) ||
    (Number.isSafeInteger(id) && id > 0 && id <= 999999999999);
}
export function normalize(x) {
  if (
    !x ||
    !validId(x.id) ||
    x.isDeleted ||
    ![1, 2, 3].includes(x.fileType)
  )
    return null;
  const type = Object.keys(kinds).find((k) => kinds[k] === x.fileType);
  const tags = Array.isArray(x.tags)
    ? x.tags
        .filter((v) => typeof v === "string")
        .slice(0, 20)
        .map((v) => v.slice(0, 120))
    : [];
  const original = mediaURL(x.cloudSource),
    preview = mediaURL(x.cloudSource300) || mediaURL(x.cloudSource500);
  return {
    // Keep legacy numeric snapshots compatible without losing public-ID leading zeros.
    id: /^[1-9][0-9]*$/.test(x.id) ? Number(x.id) : x.id,
    type,
    title: typeof x.title === "string" ? x.title.slice(0, 200) : "",
    tags,
    author: String(x.author?.username || x.username || "").slice(0, 80),
    width: dimension(x.width),
    height: dimension(x.height),
    preview,
    original,
    video:
      mediaURL(x.cloudSourceMp4) || (original.endsWith(".mp4") ? original : ""),
  };
}
async function request(path, body, signal) {
  const key = path + JSON.stringify(body || null),
    cached = memory.get(key);
  if (cached && Date.now() - cached.at < 60000) return cached.value;
  const timeout = new AbortController(),
    timer = setTimeout(() => timeout.abort(), 20000);
  const cancel = () => timeout.abort();
  if (signal?.aborted) {
    clearTimeout(timer);
    throw new DOMException("Aborted", "AbortError");
  }
  signal?.addEventListener("abort", cancel, { once: true });
  try {
    const response = await fetch("/_loop/gifs/" + path, {
      method: body ? "POST" : "GET",
      credentials: "omit",
      redirect: "error",
      signal: timeout.signal,
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!response.ok)
      throw new Error(
        response.status === 404
          ? "notFound"
          : response.status === 429
            ? "rateError"
            : "networkError",
      );
    const value = await response.json();
    if (value?.isSuccess === false) throw new Error("networkError");
    memory.set(key, { at: Date.now(), value });
    if (memory.size > 40) memory.delete(memory.keys().next().value);
    return value;
  } catch (e) {
    if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
    throw new Error(
      ["notFound", "rateError"].includes(e.message)
        ? e.message
        : "networkError",
    );
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", cancel);
  }
}
async function feedPage(
  { type = "gif", query = "", category = "", skip = 0, related = null },
  signal,
) {
  if (!kinds[type] || !Number.isInteger(skip) || skip < 0 || skip > 999999)
    throw new Error("networkError");
  let path, body;
  if (related) {
    path = `related-${type}`;
    body = { mediaId: related, skip, take: sourcePageSize };
  } else if (query) {
    path = `search-${type}`;
    body = { query: query.slice(0, 120), skip, take: sourcePageSize };
  } else if (/^[0-9]{1,12}$/.test(category))
    path = `category?communityId=${category}&contentType=${kinds[type]}&skip=${skip}&take=24`;
  else path = `popular?contentType=${kinds[type]}&skip=${skip}&take=24`;
  const value = await request(path, body, signal),
    raw = Array.isArray(value) ? value : value.result;
  if (!Array.isArray(raw)) throw new Error("networkError");
  return {
    items: raw.map(normalize).filter(Boolean),
    more: raw.length >= sourcePageSize,
  };
}
export async function item(id, signal) {
  if (!validId(id)) throw new Error("notFound");
  const raw = await request(`item/${id}`, null, signal),
    result = normalize(raw?.result || raw);
  if (!result) throw new Error("notFound");
  return result;
}
export async function categories(signal) {
  const value = await request("categories", {}, signal);
  return (Array.isArray(value) ? value : [])
    .filter(
      (x) =>
        x &&
        Number.isSafeInteger(x.id) &&
        x.id > 0 &&
        !x.parentId &&
        typeof x.title === "string",
    )
    .map((x) => ({ id: x.id, title: x.title.slice(0, 100) }));
}
export async function trending(signal) {
  const value = await request("trending?period=24h&take=8", null, signal);
  return (Array.isArray(value) ? value : [])
    .filter((x) => x && typeof x.key === "string")
    .map((x) => x.key.slice(0, 120))
    .slice(0, 8);
}

// Keep the existing proxy contract (24 per request), combine two pages atomically.
async function rawFeed(options, signal) {
  const skip = options.skip || 0;
  const first = await feedPage(options, signal);
  if (!first.more) return { ...first, nextSkip: skip + sourcePageSize };
  const second = await feedPage(
    { ...options, skip: skip + sourcePageSize },
    signal,
  );
  return {
    items: [...first.items, ...second.items],
    more: second.more,
    nextSkip: skip + pageSize,
  };
}

const feedPages = confirmedPages(async (options, skip, { signal }) => {
  const page = await rawFeed({ ...options, skip }, signal);
  return { ...page, continuation: page.more ? page.nextSkip : null };
}, { cursor: 'continuation' });
export async function feed({ skip = 0, ...options }, signal) {
  const page = await feedPages(options, skip, { signal });
  return { ...page, more: page.continuation !== null, nextSkip: page.continuation ?? page.nextSkip };
}
