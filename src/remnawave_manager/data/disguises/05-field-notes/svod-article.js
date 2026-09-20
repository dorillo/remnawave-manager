import { imageURL } from '../shared/image-proxy.js';
import DOMPurify from "./purify.es.js";
import { el, route, dialog } from "./svod-ui.js";
import { t } from "./svod-i18n.js";
import { original } from "./svod-data.js";
const images = new Set([
  "ru.wikipedia.org",
  "upload.wikimedia.org",
  "thumb.wikimedia.org",
  "wikimedia.org",
]);
function safeURL(value, base) {
  try {
    const url = new URL(value, base);
    return url.protocol === "https:" && !url.username && !url.password
      ? url
      : null;
  } catch {
    return null;
  }
}
export function renderArticle(article) {
  const fragment = DOMPurify.sanitize(article.html, {
    RETURN_DOM_FRAGMENT: true,
    ALLOWED_TAGS: [
      "p",
      "div",
      "span",
      "a",
      "b",
      "strong",
      "i",
      "em",
      "u",
      "s",
      "small",
      "sup",
      "sub",
      "h2",
      "h3",
      "h4",
      "h5",
      "h6",
      "ul",
      "ol",
      "li",
      "dl",
      "dt",
      "dd",
      "blockquote",
      "pre",
      "code",
      "br",
      "hr",
      "table",
      "thead",
      "tbody",
      "tfoot",
      "tr",
      "th",
      "td",
      "caption",
      "figure",
      "figcaption",
      "img",
      "abbr",
      "cite",
      "math",
      "mrow",
      "mi",
      "mn",
      "mo",
      "msup",
      "msub",
      "mfrac",
      "msqrt",
      "mtext",
      "msubsup",
      "munder",
      "mover",
      "munderover",
      "mtable",
      "mtr",
      "mtd",
      "mstyle",
      "mpadded",
      "mphantom",
      "mmultiscripts",
      "mspace",
      "semantics",
      "annotation",
    ],
    ALLOWED_ATTR: [
      "href",
      "src",
      "alt",
      "title",
      "id",
      "class",
      "colspan",
      "rowspan",
      "scope",
      "start",
      "reversed",
      "display",
      "encoding",
      "width",
      "height",
    ],
    ALLOW_DATA_ATTR: false,
    ALLOW_ARIA_ATTR: false,
  });
  const container = el("div", { class: "wiki-content", lang: "ru" }, fragment),
    base = original(article.title);
  container
    .querySelectorAll(
      ".mw-editsection,.toc,.noprint,.mw-empty-elt,.metadata,.ambox,.sistersitebox,.navbox",
    )
    .forEach((n) => n.remove());
  // Native MathML remains visible; the duplicate image fallback is unnecessary.
  for (const math of container.querySelectorAll(".mwe-math-element")) {
    if (math.querySelector("math"))
      math.querySelectorAll("img").forEach((img) => img.remove());
  }
  for (const node of container.querySelectorAll("[width],[height]")) {
    if (node.tagName !== "IMG") {
      node.removeAttribute("width");
      node.removeAttribute("height");
    }
  }
  const ids = new Map(),
    usedIDs = new Set();
  let n = 0;
  for (const node of container.querySelectorAll("[id]")) {
    const old = node.id;
    let id = "wiki-" + old;
    while (usedIDs.has(id)) id = "wiki-" + old + "-duplicate-" + ++n;
    usedIDs.add(id);
    if (!ids.has(old)) ids.set(old, id);
    node.id = id;
  }
  // Source classes are never allowed to control the application shell.
  const classes = new Set([
    "infobox",
    "wikitable",
    "thumb",
    "thumbinner",
    "thumbcaption",
    "tright",
    "tleft",
    "hatnote",
    "reference",
    "references",
    "reflist",
    "navbox",
    "nowrap",
    "mw-headline",
    "mw-parser-output",
    "mw-default-size",
    "mw-halign-right",
    "mw-halign-left",
  ]);
  for (const node of container.querySelectorAll("[class]")) {
    node.className = [...node.classList]
      .filter((x) => classes.has(x))
      .join(" ");
  }
  for (const a of container.querySelectorAll("a")) {
    const href = a.getAttribute("href");
    if (!href) {
      a.removeAttribute("href");
      continue;
    }
    const url = safeURL(href, base);
    if (!url) {
      a.removeAttribute("href");
      continue;
    }
    let anchor = "";
    try {
      anchor = decodeURIComponent(url.hash.slice(1));
    } catch {}
    if (
      href.startsWith("#") ||
      (url.origin === new URL(base).origin &&
        url.pathname === new URL(base).pathname &&
        anchor)
    ) {
      const id = ids.get(anchor);
      if (id) {
        a.href = route("article", article.title, id);
        a.addEventListener("click", (e) => {
          e.preventDefault();
          document.getElementById(id)?.scrollIntoView({ block: "start" });
          history.replaceState(null, "", route("article", article.title, id));
        });
      } else a.removeAttribute("href");
    } else if (
      url.hostname === "ru.wikipedia.org" &&
      url.pathname.startsWith("/wiki/")
    ) {
      let title;
      try {
        title = decodeURIComponent(url.pathname.slice(6)).replaceAll("_", " ");
      } catch {
        title = "";
      }
      if (title.startsWith("Категория:")) a.href = route("category", title);
      else if (title && !title.includes(":") && !url.search)
        a.href = route("article", title, anchor);
      else a.removeAttribute("href");
    } else if (
      url.hostname === "wikipedia.org" ||
      url.hostname.endsWith(".wikipedia.org")
    ) {
      a.removeAttribute("href");
    } else if (
      url.hostname === "wikimedia.org" ||
      url.hostname.endsWith(".wikimedia.org")
    ) {
      a.remove();
    } else {
      a.href = url.href;
      a.target = "_blank";
      a.rel = "noopener noreferrer";
    }
  }
  for (const img of container.querySelectorAll("img")) {
    const url = safeURL(img.getAttribute("src"), base);
    if (!url || url.protocol !== "https:" || !images.has(url.hostname)) {
      img.replaceWith(
        el("span", { class: "image-unavailable" }, img.alt || t("noImage")),
      );
      continue;
    }
    for (const key of ["width", "height"]) {
      const size = Number(img.getAttribute(key));
      if (!Number.isFinite(size) || size <= 0 || size > 4096)
        img.removeAttribute(key);
    }
    img.src = imageURL(url.href);
    img.loading = "lazy";
    img.decoding = "async";
    img.referrerPolicy = "no-referrer";
    img.addEventListener(
      "error",
      () =>
        img.replaceWith(
          el("span", { class: "image-unavailable" }, img.alt || t("noImage")),
        ),
      { once: true },
    );
    {
      img.tabIndex = 0;
      img.setAttribute("role", "button");
      img.setAttribute("aria-label", t("zoom"));
      const zoom = () =>
        dialog(img.alt || t("zoom"), [
          el("img", { src: imageURL(url.href), alt: img.alt, class: "zoom-image" }),
        ]);
      img.addEventListener("click", (e) => {
        e.preventDefault();
        zoom();
      });
      img.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          zoom();
        }
      });
    }
  }
  for (const table of container.querySelectorAll("table:not(.infobox)")) {
    if (table.parentElement.closest("table")) continue;
    const wrap = el("div", { class: "table-scroll", tabindex: 0 });
    table.replaceWith(wrap);
    wrap.append(table);
  }
  const headings = [...container.querySelectorAll("h2,h3,h4")].map((h, i) => {
    if (!h.id) {
      const span = h.querySelector("[id]");
      if (span) {
        h.id = span.id;
        span.removeAttribute("id");
      } else h.id = "svod-heading-" + i;
    }
    return { id: h.id, text: h.textContent, level: Number(h.tagName[1]) };
  });
  return { container, headings, ids };
}
