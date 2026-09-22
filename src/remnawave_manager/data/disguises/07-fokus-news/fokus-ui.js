import { loadingMarkup } from '../shared/feedback.js';
import {
  escape as e,
  route,
  inert,
} from "./fokus-data.js?v=20260919-release";
import { t, date } from "./fokus-i18n.js?v=20260919-release";
const paths = {
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/>',
  arrow: '<path d="M5 12h14m-6-6 6 6-6 6"/>',
  back: '<path d="M19 12H5m6-6-6 6 6 6"/>',
  bookmark: '<path d="M6 4h12v17l-6-4-6 4z"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.5 1.5m11 11L19 19M5 19l1.5-1.5m11-11L19 5"/>',
  moon: '<path d="M20 14A8 8 0 0 1 10 4a8 8 0 1 0 10 10z"/>',
  close: '<path d="m6 6 12 12M6 18 18 6"/>',
  menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
  refresh:
    '<path d="M20 7v5h-5M4 17v-5h5M5 8a8 8 0 0 1 13-3l2 3M4 16l2 3a8 8 0 0 0 13-3"/>',
  chat: '<path d="M4 4h16v13H9l-5 4z"/>',
  share: '<path d="M12 16V3m-5 5 5-5 5 5M5 13v8h14v-8"/>',
  check: '<path d="m5 12 4 4L19 6"/>',
  heart: '<path d="M12 21 3 12C-2 5 7 0 12 7 17 0 26 5 21 12z"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 6v6l4 2"/>',
};
export const icon = (name) =>
  `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.arrow}</svg>`;
// Presentation only: keep source addresses in the data layer for fetching.
export function cleanSourceText(value = "") {
  return String(value)
    .replace(/\s*[-–—]\s*(?:РИА Новости|RIA Novosti)\.\s*/gi, ". ")
    .replace(/^(?:РИА Новости|RIA Novosti)\.\s*/i, "")
    .replace(/(?:https?:\/\/)?(?:www\.)?ria\.ru(?:\/[^\s<>"«»]*)?/gi, "")
    .replace(
      /(?<![\p{L}\p{N}])(?:РИА(?:\s+Новости)?|RIA(?:\s+Novosti)?)(?![\p{L}\p{N}])/giu,
      "",
    )
    .replace(/[ \t]+([,.;:!?])/g, "$1")
    .replace(/[ \t]{2,}/g, " ");
}
export function cleanSourceMarkup(html) {
  const fragment = inert(html);
  const walker = document.createTreeWalker(fragment, NodeFilter.SHOW_TEXT);
  while (walker.nextNode())
    walker.currentNode.textContent = cleanSourceText(
      walker.currentNode.textContent,
    );
  for (const el of fragment.querySelectorAll("[alt],[title],[aria-label]")) {
    for (const attr of ["alt", "title", "aria-label"]) {
      if (el.hasAttribute(attr))
        el.setAttribute(attr, cleanSourceText(el.getAttribute(attr)));
    }
  }
  for (const link of fragment.querySelectorAll("a[href]")) {
    try {
      const url = new URL(link.getAttribute("href"), location.href);
      if (url.hostname === "ria.ru" || url.hostname.endsWith(".ria.ru"))
        link.replaceWith(...link.childNodes);
    } catch {}
  }
  const template = document.createElement("template");
  template.content.append(fragment);
  return template.innerHTML;
}
const isLoadingLabel = label => ['loading', 'loadingComments', 'loadingMoreComments', 'loadingArticle'].some(key => label === t(key));
export const button = (action, label, ico = "", cls = "", extra = "") =>
  `<button type="button" class="${cls}" data-action="${action}" ${extra}>${ico ? icon(ico) : ""}<span>${isLoadingLabel(label) ? loadingMarkup(label) : e(label)}</span></button>`;
export const state = (message, action = "") =>
  isLoadingLabel(message) ? `<div class="empty-state loading-state">${loadingMarkup(message)}</div>` :
  `<div class="empty-state"><span class="state-symbol">${icon("chat")}</span><p>${e(message)}</p>${action ? button(action, t("retry"), "refresh", "secondary") : ""}</div>`;
export function card(a, index = 0, kind = "card") {
  return `<article class="${kind}"><a class="story-link" href="${e(route(a))}">${a.image ? `<div class="story-image"><img src="${e(a.image)}" alt="" loading="${index === 0 ? "eager" : "lazy"}" referrerpolicy="no-referrer"></div>` : ""}<div class="story-copy"><div class="eyebrow">${e(t(a.category) || t("source"))}</div><h2>${e(cleanSourceText(a.title))}</h2><div class="meta">${e(date(a.published)) || e(t("source"))}<span>${a.count !== undefined && a.count !== null ? icon("chat") + " " + e(a.count) : icon("arrow")}</span></div></div></a></article>`;
}
// Pair short text stories in the same column as one illustrated story.
export function newsGrid(items, kind = "card") {
  const columns = [];
  let textColumn = null;
  items.forEach((item, index) => {
    if (!item.image && textColumn) {
      textColumn.push(card(item, index, kind));
      textColumn = null;
    } else {
      const column = [card(item, index, kind)];
      columns.push(column);
      if (!item.image) textColumn = column;
    }
  });
  return `<div class="news-grid">${columns.map((column) => `<div class="news-column">${column.join("")}</div>`).join("")}</div>`;
}
export const stale = (data) =>
  data?.stale
    ? `<p class="notice">${e(t("stale"))} ${e(date(data.fetchedAt))}</p>`
    : "";
export function avatar(a) {
  return a?.avatar &&
    /^data:image\/(jpeg|png|webp);base64,[A-Za-z0-9+/=]+$/.test(a.avatar)
    ? `<img class="avatar" src="${e(a.avatar)}" alt="">`
    : `<span class="avatar initials">${e((a?.name || "?").slice(0, 1).toUpperCase())}</span>`;
}
let timer;
export function toast(message) {
  const el = document.querySelector("#toast");
  el.textContent = message;
  el.hidden = false;
  clearTimeout(timer);
  timer = setTimeout(() => (el.hidden = true), 4000);
}
let returnFocus;
export function modal(html) {
  const d = document.querySelector("#modal");
  if (!d.open) returnFocus = document.activeElement;
  d.innerHTML = `<div class="dialog-top">${button("close", t("close"), "close", "icon-button")}</div>${html}`;
  if (!d.open) d.showModal();
  d.querySelector('input,textarea,button:not([data-action="close"])')?.focus();
}
export function closeModal() {
  document.querySelector("#modal").close();
  returnFocus?.focus();
}
export function confirmAction({ title, confirmLabel, description = "" }) {
  if (!confirmLabel?.trim())
    throw new Error("Confirmation action label is required");
  return new Promise((resolve) => {
    modal(
      `<h2 id="dialog-title">${e(title)}</h2>${description ? `<p class="muted">${e(description)}</p>` : ""}<div class="dialog-actions">${button("close", t("cancel"), "", "secondary")}${button("confirm", confirmLabel, "", "primary")}</div>`,
    );
    const d = document.querySelector("#modal");
    const yes = d.querySelector('[data-action="confirm"]');
    d.querySelector('.dialog-actions [data-action="close"]').focus();
    const close = () => {
      d.removeEventListener("close", close);
      resolve(false);
    };
    d.addEventListener("close", close);
    yes.onclick = () => {
      d.removeEventListener("close", close);
      closeModal();
      resolve(true);
    };
  });
}
