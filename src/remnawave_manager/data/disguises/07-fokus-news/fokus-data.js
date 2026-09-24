import { confirmedPages } from '../shared/pagination.js';
import { cached, cache } from "./fokus-store.js?v=20260919-release";
export const sections = [
  "politics",
  "world",
  "economy",
  "society",
  "science",
  "culture",
];
export const codes = ["s1", "s6", "s2", "s3", "s4", "s5"];
export const escape = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
// This lexical pass only neutralizes style attributes before the HTML parser
// checks CSP. It is not a sanitizer; upstream nodes are never mounted, and
// safeInline below still reconstructs the small allowed subset from scratch.
function withoutInlineStyles(html) {
  const tags = /<!--[\s\S]*?(?:--!?>|$)|<([a-z][^\t\n\f\r />]*)/gi;
  const space = /[\t\n\f\r ]/;
  const parts = [];
  let copied = 0,
    match;
  while ((match = tags.exec(html))) {
    if (!match[1]) continue;
    let pos = tags.lastIndex;
    while (pos < html.length && html[pos] !== ">") {
      if (space.test(html[pos]) || html[pos] === "/") {
        pos++;
        continue;
      }
      const start = pos;
      while (
        pos < html.length &&
        !space.test(html[pos]) &&
        !"/=>".includes(html[pos])
      )
        pos++;
      // An unexpected '=' is itself an attribute-name character in HTML.
      if (pos === start) pos++;
      if (html.slice(start, pos).toLowerCase() === "style") {
        parts.push(html.slice(copied, start), "data-fokus-inline-style");
        copied = pos;
      }
      while (space.test(html[pos] || "") && pos < html.length) pos++;
      if (html[pos] === "=") {
        pos++;
        while (pos < html.length && space.test(html[pos])) pos++;
        const quote = html[pos];
        if (quote === '"' || quote === "'") {
          const end = html.indexOf(quote, pos + 1);
          pos = end < 0 ? html.length : end + 1;
        } else {
          while (
            pos < html.length &&
            !space.test(html[pos]) &&
            html[pos] !== ">"
          )
            pos++;
        }
      }
    }
    tags.lastIndex = pos + 1;
    const name = match[1].toLowerCase();
    if (
      [
        "script",
        "style",
        "textarea",
        "title",
        "xmp",
        "iframe",
        "noembed",
        "noframes",
      ].includes(name)
    ) {
      const close = new RegExp(`</${name}(?=[\\t\\n\\f\\r />])`, "gi");
      close.lastIndex = tags.lastIndex;
      const end = close.exec(html);
      tags.lastIndex = end ? end.index : html.length;
    }
  }
  parts.push(html.slice(copied));
  return parts.join("");
}
// A template keeps upstream scripts and resources inert during inspection.
export function inert(html) {
  const t = document.createElement("template");
  t.innerHTML = withoutInlineStyles(String(html));
  t.content
    .querySelectorAll("script,style,iframe,object,embed,link,base,form")
    .forEach((x) => x.remove());
  t.content
    .querySelectorAll("[data-fokus-inline-style]")
    .forEach((x) => x.removeAttribute("data-fokus-inline-style"));
  return t.content;
}
export function articleRef(value) {
  try {
    const u = new URL(value, "https://ria.ru");
    if (u.origin !== "https://ria.ru" || u.username || u.password) return null;
    const m = u.pathname.match(
      /^\/(\d{8})\/([a-z0-9-]{1,160}-(\d{1,12})\.html)$/,
    );
    return m
      ? { id: m[3], date: m[1], path: u.pathname, url: u.origin + u.pathname }
      : null;
  } catch {
    return null;
  }
}
export const route = (a) => "#/article" + a.path;
// Both RIA CDN names serve the same image paths. Keep the local proxy URL
// stable so cached articles and existing nginx installations remain compatible.
const mediaHosts = new Set(["cdnn21.img.ria.ru", "cdnn21.imgria.ru"]);
export function media(value) {
  try {
    const u = new URL(value, "https://ria.ru");
    if (
      u.protocol !== "https:" ||
      !mediaHosts.has(u.hostname) ||
      u.username ||
      u.password ||
      u.port ||
      !/^\/images\/[A-Za-z0-9_/:.-]{1,400}\.(jpg|jpeg|png|webp)$/.test(
        u.pathname,
      ) ||
      u.pathname.includes("..") ||
      u.pathname.includes("//")
    )
      return "";
    return "/_fokus/media" + u.pathname;
  } catch {
    return "";
  }
}
const text = (root, s) => root.querySelector(s)?.textContent.trim() || "";
const image = (root) => {
  if (!root) return "";
  // Skip placeholders and unsupported candidates instead of trusting the first
  // img/source. Responsive and lazy markup can put the usable URL in srcset.
  for (const node of root.querySelectorAll("img,source")) {
    const candidates = [node.getAttribute("data-src"), node.getAttribute("src")];
    for (const attr of ["data-srcset", "srcset"]) {
      for (const entry of (node.getAttribute(attr) || "").split(","))
        candidates.push(entry.trim().split(/\s+/)[0]);
    }
    const src = candidates.map(media).find(Boolean);
    if (src) return src;
  }
  return "";
};
export function summary(ref, title, img = "", date = "", category = "") {
  return {
    ...ref,
    title: String(title).slice(0, 600),
    image: img,
    published: date,
    category,
  };
}
export const unique = (items) => [
  ...new Map(items.filter(Boolean).map((a) => [a.id, a])).values(),
];
function rss(raw) {
  const d = new DOMParser().parseFromString(raw, "text/xml");
  if (d.querySelector("parsererror")) throw new Error("schema");
  const items = [...d.querySelectorAll("item")].map((x) => {
    const r = articleRef(text(x, "link"));
    return (
      r &&
      summary(
        r,
        text(x, "title"),
        media(x.querySelector("enclosure")?.getAttribute("url")),
        text(x, "pubDate"),
        text(x, "category"),
      )
    );
  });
  if (!items.length) throw new Error("schema");
  return { items: unique(items) };
}
function cards(raw, section = "") {
  const d = inert(raw);
  const items = [...d.querySelectorAll(".list-item")].map((x) => {
    const a = x.querySelector(".list-item__title"),
      r = articleRef(a?.getAttribute("href"));
    return (
      r &&
      summary(
        r,
        a.textContent.trim(),
        image(x),
        x.querySelector("time")?.getAttribute("datetime") || "",
        section,
      )
    );
  });
  const next =
    d.querySelector("[data-next-url]")?.getAttribute("data-next-url") ||
    d.querySelector(".list-more[data-url]")?.getAttribute("data-url") ||
    "";
  let cursor = "";
  try {
    const u = new URL(next, "https://ria.ru");
    if (
      u.origin === "https://ria.ru" &&
      u.pathname === `/services/${section}/more.html` &&
      /^id=\d{1,12}&date=\d{8}T\d{6}(&view=tags)?$/.test(u.search.slice(1))
    )
      cursor = u.search.slice(1);
  } catch {}
  if (!items.length && !raw.includes("list-items-loaded"))
    throw new Error("schema");
  return { items: unique(items), cursor: items.length ? cursor : "" };
}
function search(raw, query, offset) {
  const d = inert(raw),
    root = d.querySelector(".list-items-loaded");
  if (!root) throw new Error("schema");
  const result = cards(raw);
  let cursor = "";
  const next = root.getAttribute("data-next-url");
  if (next) {
    const u = new URL(next, "https://ria.ru");
    const value = u.searchParams.get("offset");
    if (
      u.origin === "https://ria.ru" &&
      u.pathname === "/services/search/getmore/" &&
      u.searchParams.get("query") === query &&
      /^\d{1,7}$/.test(value || "") &&
      Number(value) > Number(offset) &&
      result.items.length
    )
      cursor = value;
  }
  const count = root.getAttribute("data-count");
  if (/^\d+$/.test(count || "") && Number(cursor) >= Number(count)) cursor = "";
  return {
    ...result,
    cursor,
    total: /^\d+$/.test(count || "") ? Number(count) : null,
  };
}
function home(raw) {
  const d = inert(raw),
    items = [];
  for (const x of d.querySelectorAll(
    ".cell:not(.cell-list), .cell-list__item",
  )) {
    const a = x.querySelector('a[href*=".html"]'),
      r = articleRef(a?.getAttribute("href"));
    const title =
      text(
        x,
        ".cell-video__title,.cell-main-photo__title,.cell-photo__title,.cell-media-cover__title,.cell-vv__title,.cell-list__item-title",
      ) ||
      a?.getAttribute("title") ||
      "";
    if (r && title) items.push(summary(r, title, image(x)));
  }
  if (!items.length) throw new Error("schema");
  const merged = new Map();
  for (const item of items) {
    const old = merged.get(item.id);
    merged.set(
      item.id,
      old ? { ...old, image: old.image || item.image } : item,
    );
  }
  return { items: [...merged.values()].slice(0, 8) };
}
export function safeInline(node) {
  if (node.nodeType === 3) return escape(node.textContent);
  if (node.nodeType !== 1) return "";
  if (
    [
      "SCRIPT",
      "STYLE",
      "IFRAME",
      "OBJECT",
      "SVG",
      "MATH",
      "FORM",
      "INPUT",
      "BUTTON",
    ].includes(node.tagName)
  )
    return "";
  const inner = [...node.childNodes].map(safeInline).join("");
  if (
    [
      "STRONG",
      "B",
      "EM",
      "I",
      "P",
      "UL",
      "OL",
      "LI",
      "H2",
      "H3",
      "BLOCKQUOTE",
      "BR",
    ].includes(node.tagName)
  )
    return `<${node.tagName.toLowerCase()}>${inner}</${node.tagName.toLowerCase()}>`;
  if (node.tagName === "A") {
    const r = articleRef(node.getAttribute("href"));
    if (r) return `<a href="${escape(route(r))}">${inner}</a>`;
  }
  return inner;
}
function article(raw, ref) {
  const published =
    raw.match(/"datePublished"\s*:\s*"([^"]+)"/)?.[1]?.trim() || "";
  const modified =
    raw.match(/"dateModified"\s*:\s*"([^"]+)"/)?.[1]?.trim() || "";
  const d = inert(raw),
    body = d.querySelector(".article__body, .article.m-white_online");
  if (!body) throw new Error("schema");
  const blocks = [],
    related = [];
  for (const x of body.querySelectorAll(
    ".article__block, .white-longread__block, .online__item-text, .online__item-time",
  )) {
    if (x.matches('.online__item-text, .online__item-time')) {
      if (x.textContent.trim()) blocks.push({ type: 'text', html: x.matches('.online__item-time') ? `<h3>${safeInline(x)}</h3>` : safeInline(x) });
      continue;
    }
    const type = x.getAttribute("data-type");
    if (type === "text" || type === "quote") {
      const node = x.querySelector(
        type === "text"
          ? ".article__text, .white-longread__text-body"
          : ".article__quote-text, .white-longread__quote-text",
      );
      if (node?.textContent.trim())
        blocks.push({ type, html: safeInline(node) });
    } else if (["h2", "h3"].includes(type)) {
      const heading = x.querySelector(".white-longread__width") || x;
      if (heading.textContent.trim())
        blocks.push({
          type: "text",
          html: `<${type}>${safeInline(heading)}</${type}>`,
        });
    } else if (["image", "media-image", "photo-site"].includes(type)) {
      const src = image(x);
      if (src)
        blocks.push({
          type: "image",
          src,
          caption:
            text(x, ".media__title, .white-longread__media-description") ||
            x.querySelector("img")?.getAttribute("alt") ||
            "",
        });
    } else if (type === "list") {
      const list = x.querySelector('ul, ol');
      if (list) blocks.push({ type: 'text', html: safeInline(list) });
    } else if (type === "photolenta") {
      for (const item of x.querySelectorAll(".article__photo-item")) {
        const src = image(item);
        if (src)
          blocks.push({
            type: "image",
            src,
            caption:
              text(item, ".article__photo-item-text") ||
              item.querySelector("img")?.getAttribute("alt") ||
              "",
          });
      }
    } else if (type === "article") {
      const a = x.querySelector('a[href*=".html"]'),
        r = articleRef(a?.getAttribute("href"));
      if (r)
        related.push(
          summary(
            r,
            text(x, ".article__article-title") || a.textContent.trim(),
            image(x),
          ),
        );
    } else if (["video", "embed", "media-video", "media-embed"].includes(type)) {
      const video = x.querySelector('video');
      const poster = media(video?.getAttribute('poster')) || image(x);
      if (poster) blocks.push({ type: 'videoPreview', src: poster, caption: video?.getAttribute('data-title') || text(x, '.media__title, .white-longread__media-description') });
    }
  }
  if (!blocks.length) throw new Error("articleFormat");
  const meta = (name) =>
    d.querySelector(`meta[property="${name}"]`)?.getAttribute("content") || "";
  return {
    ...summary(
      ref,
      text(d, ".article__title") || meta("og:title"),
      image(
        d.querySelector(".article__announce, .white-longread__header-media") ||
          document.createElement("div"),
      ) ||
        (body.querySelector('[data-type="photolenta"]')
          ? ""
          : media(meta("og:image"))),
      published ||
        d.querySelector("time")?.getAttribute("datetime") ||
        meta("article:published_time"),
      text(d, ".article__tags-item"),
    ),
    blocks,
    related: unique(related).slice(0, 4),
    author: text(d, ".article__author-name, .white-longread__header-author"),
    caption:
      text(d, ".article__announce .media__title") ||
      d
        .querySelector(
          ".article__announce img, .white-longread__header-media img",
        )
        ?.getAttribute("title") ||
      "",
    updated: modified || meta("article:modified_time"),
  };
}
function dynamics(raw, id) {
  const d = inert(raw),
    root = [...d.querySelectorAll(".article__userbar-emoji .emoji")].find(
      (x) => x.dataset.id === id,
    );
  if (!root) throw new Error("schema");
  const counts = {};
  for (const code of codes) {
    const v = root
      .querySelector(`[data-type="${code}"] .m-value`)
      ?.textContent.trim();
    if (!/^\d+$/.test(v || "")) throw new Error("schema");
    counts[code] = Number(v);
  }
  return { counts };
}
const cleanText = (s) => String(s ?? "").slice(0, 10000);
function externalComment(c, id) {
  if (!c || !c.id || !c.text) return null;
  return {
    id: String(c.id),
    ref: `ria:${id}:${c.id}`,
    name: cleanText(c.nickname),
    text: cleanText(c.text),
    at: Number(c.created?.sec || c.ts_gmt) * 1000,
    parent: c.parent_comment
      ? {
          name: cleanText(c.parent_comment.nickname),
          text: cleanText(c.parent_comment.text),
          ref: `ria:${id}:${c.parent_comment.id}`,
        }
      : null,
  };
}
function comments(raw, id) {
  const j = JSON.parse(raw),
    c = j.chat;
  if (!c || !Array.isArray(c.messages)) throw new Error("schema");
  const reactions = new Map(
    (Array.isArray(j.emoji_chat) ? j.emoji_chat : []).map((x) => [
      String(x.object_id),
      x.emotions,
    ]),
  );
  const items = c.messages
    .map((x) => externalComment(x, id))
    .filter(Boolean)
    .map((x) => ({
      ...x,
      counts: reactions.has(x.id)
        ? Object.fromEntries(
            codes.map((k) => [
              k,
              Math.max(0, Number(reactions.get(x.id)?.[k]) || 0),
            ]),
          )
        : null,
    }));
  let cursor = "";
  if (
    items.length &&
    c.last_date &&
    Number.isFinite(Number(c.last_date.sec)) &&
    Number.isFinite(Number(c.last_date.usec)) &&
    /^[a-f0-9]{24}$/.test(c.last_id || "")
  )
    cursor = `&date=${Number(c.last_date.sec)}&date_usec=${Number(c.last_date.usec)}&id_exc=${c.last_id}`;
  return { items, cursor };
}
async function request(path, signal) {
  const controller = new AbortController(),
    timer = setTimeout(() => controller.abort(), 18000),
    abort = () => controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  if (signal?.aborted) controller.abort();
  try {
    const fetchOptions = {
      signal: controller.signal,
      credentials: "omit",
      redirect: "error",
      referrerPolicy: "no-referrer",
    };
    let r = await fetch("/_fokus" + path, fetchOptions);
    if (r.status === 429) {
      const header = r.headers.get("Retry-After");
      const delay =
        header && /^\d+$/.test(header)
          ? Number(header) * 1000
          : header
            ? Date.parse(header) - Date.now()
            : NaN;
      // At most one short retry. Longer server cooldowns are left to the user.
      if (Number.isFinite(delay) && delay >= 0 && delay <= 3000) {
        await r.body?.cancel();
        await new Promise((resolve, reject) => {
          const aborted = () => {
            clearTimeout(wait);
            reject(new DOMException("Aborted", "AbortError"));
          };
          const wait = setTimeout(
            () => {
              controller.signal.removeEventListener("abort", aborted);
              resolve();
            },
            Math.max(500, delay),
          );
          controller.signal.addEventListener("abort", aborted, { once: true });
          if (controller.signal.aborted) aborted();
        });
        r = await fetch("/_fokus" + path, fetchOptions);
      }
    }
    if (!r.ok) throw new Error(r.status === 429 ? "rateLimit" : "network");
    const reader = r.body.getReader();
    let total = 0;
    const chunks = [];
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.length;
      if (total > 8 * 1024 * 1024) {
        await reader.cancel();
        throw new Error("schema");
      }
      chunks.push(value);
    }
    const data = new Uint8Array(total);
    let pos = 0;
    for (const b of chunks) {
      data.set(b, pos);
      pos += b.length;
    }
    return new TextDecoder().decode(data);
  } catch (error) {
    // A request timeout is a retryable failure, not navigation cancellation.
    if (controller.signal.aborted && !signal?.aborted)
      throw new Error("network");
    throw error;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}
async function rawLoad(
  kind,
  { ref, section = "", cursor = "", query = "", signal, force = false } = {},
) {
  let path,
    parser,
    ttl = 60000;
  switch (kind) {
    case "feed":
      path = "/ria/feed";
      parser = rss;
      break;
    case "home":
      path = "/ria/home";
      parser = home;
      break;
    case "search":
      path = `/ria/search?${new URLSearchParams({ query, offset: cursor || "0" })}`;
      parser = (x) => search(x, query, cursor || "0");
      break;
    case "section":
      path = cursor
        ? `/ria/more/${section}?${cursor}`
        : `/ria/section/${section}`;
      parser = (x) => cards(x, section);
      break;
    case "article":
      path = "/ria/article" + ref.path;
      parser = (x) => article(x, ref);
      ttl = 300000;
      break;
    case "dynamics":
      path = `/ria/dynamics/${ref.date}/${ref.id}`;
      parser = (x) => dynamics(x, ref.id);
      ttl = 30000;
      break;
    case "comments":
      path = `/ria/comments?article_id=${ref.id}&limit=20${cursor}`;
      parser = (x) => comments(x, ref.id);
      ttl = 30000;
      break;
    default:
      throw new Error("invalid");
  }
  const key = kind === 'article' ? 'article-v3:' + path : path;
  let old = await cached(key);
  if (old) {
    const v = old.value;
    const ok =
      v &&
      typeof v === "object" &&
      (kind === "article"
        ? Array.isArray(v.blocks) &&
          typeof v.title === "string" &&
          v.id === ref.id &&
          Array.isArray(v.related)
        : kind === "dynamics"
          ? v.counts &&
            codes.every((c) => Number.isFinite(v.counts[c]) && v.counts[c] >= 0)
          : Array.isArray(v.items));
    if (!ok) old = null;
  }
  if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
  if (!force && old && Date.now() - old.at < ttl)
    return { ...old.value, fetchedAt: old.at };
  try {
    const value = parser(await request(path, signal));
    if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
    await cache(key, value);
    return { ...value, fetchedAt: Date.now() };
  } catch (e) {
    if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
    if (old) return { ...old.value, stale: true, fetchedAt: old.at };
    throw e;
  }
}

const commentPages = confirmedPages((ref, cursor, options) => rawLoad('comments', { ...options, ref, cursor: cursor || '' }), { cursor: 'cursor' });
export function load(kind, options = {}) {
  return kind === 'comments' ? commentPages(options.ref, options.cursor || '', options) : rawLoad(kind, options);
}
