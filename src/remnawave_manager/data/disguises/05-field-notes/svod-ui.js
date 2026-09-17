import DOMPurify from "./purify.es.js";
import { t } from "./svod-i18n.js";
export function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== false)
      node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child !== null && child !== undefined && child !== false)
      node.append(
        child instanceof Node ? child : document.createTextNode(String(child)),
      );
  }
  return node;
}
// Local vector icons keep the same geometry across fonts and operating systems.
export function icon(name) {
  const paths = {
    home: "M3 10 12 3l9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1Z",
    random:
      "M3 6h3c5 0 7 12 12 12h3m-4-4 4 4-4 4M3 18h3c2 0 4-3 6-6s4-6 6-6h3m-4-4 4 4-4 4",
    library: "M4 3h4v18H4ZM11 3h4v18h-4Zm7 1 3 16M4 7h4m3 0h4",
    history: "M3 10a9 9 0 1 1 2 8M3 4v6h6m3-3v5l3 2",
    about: "M12 8h.01M11 12h1v5m-9-5a9 9 0 1 0 18 0 9 9 0 1 0-18 0",
    search: "M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0",
    appearance: "M21 13a9 9 0 0 1-10-10A9 9 0 1 0 21 13Z",
    menu: "M4 6h16M4 12h16M4 18h16",
    close: "m6 6 12 12M6 18 18 6",
    arrow: "M5 19 19 5M7 5h12v12",
  };
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [key, value] of Object.entries({
    viewBox: "0 0 24 24",
    width: 20,
    height: 20,
    fill: "none",
    stroke: "currentColor",
    "stroke-width": 1.7,
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true",
    focusable: "false",
    class: "icon",
  }))
    svg.setAttribute(key, value);
  const path = document.createElementNS(svg.namespaceURI, "path");
  path.setAttribute("d", paths[name] || paths.library);
  svg.append(path);
  return svg;
}
export const button = (label, action, cls = "") =>
  el("button", { type: "button", class: cls, onclick: action }, label);
export const link = (label, href, cls = "") =>
  el("a", { href, class: cls }, label);
export function notify(key) {
  const activeDialog = [...document.querySelectorAll("dialog[open]")].at(-1);
  let host = document.querySelector("#notifications");
  if (activeDialog) {
    host = activeDialog.querySelector(".dialog-notifications");
    if (!host) {
      host = el("p", { class: "dialog-notifications notice", role: "status" });
      activeDialog.append(host);
    }
  }
  if (notify.host && notify.host !== host) notify.host.textContent = "";
  notify.host = host;
  host.textContent = t(key);
  clearTimeout(notify.timer);
  notify.timer = setTimeout(() => (host.textContent = ""), 5000);
}
export function dialog(title, body) {
  const previous = document.activeElement;
  const d = el(
    "dialog",
    {},
    el(
      "div",
      { class: "dialog-head" },
      el("h2", {}, title),
      button(icon("close"), () => d.close(), "close-button"),
    ),
    ...body,
  );
  d.querySelector(".close-button").setAttribute("aria-label", t("close"));
  const titleID = "dialog-" + crypto.randomUUID();
  d.querySelector("h2").id = titleID;
  d.setAttribute("aria-labelledby", titleID);
  document.body.append(d);
  d.addEventListener("close", () => {
    d.remove();
    if (previous?.isConnected && !document.querySelector("dialog[open]"))
      previous.focus();
  });
  d.addEventListener("click", (e) => {
    if (e.target === d) {
      const r = d.getBoundingClientRect();
      if (
        e.clientX < r.left ||
        e.clientX > r.right ||
        e.clientY < r.top ||
        e.clientY > r.bottom
      )
        d.close();
    }
  });
  d.showModal();
  return d;
}
export function confirm(title, action) {
  let d;
  d = dialog(title, [
    el(
      "div",
      { class: "actions" },
      button(t("cancel"), () => d.close()),
      button(
        t("confirm"),
        async (e) => {
          const submit = e.currentTarget;
          submit.disabled = true;
          try {
            await action();
            d.close();
          } catch {
            notify("storageError");
            submit.disabled = false;
          }
        },
        "primary",
      ),
    ),
  ]);
}
export const field = (label, input) =>
  el("label", { class: "field" }, el("span", {}, label), input);
export const route = (type, value = "", anchor = "") =>
  "#/" +
  type +
  (value
    ? "?" +
      new URLSearchParams({ q: value, ...(anchor ? { section: anchor } : {}) })
    : "");
export const plain = (html) => {
  const clean = DOMPurify.sanitize(String(html || ""), {
    ALLOWED_TAGS: [],
    ALLOWED_ATTR: [],
    RETURN_DOM_FRAGMENT: true,
  });
  return clean.textContent || "";
};
