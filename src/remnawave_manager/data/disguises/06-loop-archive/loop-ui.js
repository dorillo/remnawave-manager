import { t } from "./loop-i18n.js";
export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity))
    if (child != null)
      node.append(
        child.nodeType ? child : document.createTextNode(String(child)),
      );
  return node;
}
const paths = {
  download: "M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5",
  code: "m8 5-6 7 6 7m8-14 6 7-6 7m-3-16-2 18",
  forward: "m12 5 7 7-7 7M19 12H5",
  search: "m21 21-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0",
  heart:
    "M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8Z",
  grid: "M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z",
  folder: "M3 7V4h6l3 3h9v13H3Z",
  user: "M20 21v-2a7 7 0 0 0-14 0v2M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0",
  sun: "M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1 1m12 12 1 1M5 19l1-1M18 6l1-1M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0",
  moon: "M21 13A9 9 0 0 1 11 3a9 9 0 1 0 10 10",
  pause: "M8 4v16M16 4v16",
  play: "m8 4 12 8-12 8Z",
  plus: "M12 5v14M5 12h14",
  close: "m6 6 12 12M6 18 18 6",
  arrow: "m12 5-7 7 7 7M5 12h15",
  share: "M12 16V3m-5 5 5-5 5 5M5 13v8h14v-8",
  external: "M14 3h7v7m0-7L10 14M10 3H3v18h18v-7",
  check: "m5 12 4 4L19 6",
  trash: "M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7m4-7v7",
};
export function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [k, v] of Object.entries({
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "1.7",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true",
    class: "icon",
  }))
    svg.setAttribute(k, v);
  const p = document.createElementNS(svg.namespaceURI, "path");
  p.setAttribute("d", paths[name] || paths.grid);
  svg.append(p);
  return svg;
}
export function button(label, fn, { symbol, className = "", ...attrs } = {}) {
  const node = el(
    "button",
    { type: "button", class: `button ${className}`, ...attrs },
    symbol ? icon(symbol) : null,
    label ? el("span", {}, label) : null,
  );
  node.addEventListener("click", async () => {
    if (node.disabled) return;
    node.disabled = true;
    try {
      await fn(node);
    } catch (e) {
      notice(t(e.message) === e.message ? t("storageError") : t(e.message));
    } finally {
      node.disabled = false;
    }
  });
  return node;
}
let noticeTimer;
export function notice(message) {
  const root =
    document.querySelector("dialog[open] .dialog-notice") ||
    document.getElementById("notices");
  root.textContent = message;
  root.classList.add("visible");
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => root.classList.remove("visible"), 4500);
}
export function dialog(title, content) {
  const origin = document.activeElement;
  const d = el("dialog", {
    class: "dialog",
    "aria-labelledby": "dialog-title",
  });
  const close = button("", () => d.close(), {
    symbol: "close",
    className: "icon-button",
    "aria-label": t("close"),
  });
  d.append(
    el(
      "div",
      { class: "dialog-heading" },
      el("h2", { id: "dialog-title" }, title),
      close,
    ),
    content,
    el("p", { class: "dialog-notice", role: "status" }),
  );
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
  const nativeClose = d.close.bind(d);
  d.close = () => {
    nativeClose();
    d.remove();
  };
  d.addEventListener(
    "close",
    () => {
      d.remove();
      if (origin?.isConnected && !document.querySelector("dialog[open]"))
        origin.focus();
    },
    { once: true },
  );
  document.body.append(d);
  d.showModal();
  return d;
}
export function field(label, attrs = {}) {
  const input = el("input", { "aria-label": label, ...attrs });
  return {
    input,
    node: el("label", { class: "field" }, el("span", {}, label), input),
  };
}
export function confirm(message, action) {
  return new Promise((resolve) => {
    const d = dialog(
      t("confirm"),
      el(
        "div",
        {},
        el("p", { class: "muted" }, message),
        el(
          "div",
          { class: "dialog-actions" },
          button(t("cancel"), () => d.close()),
          button(
            t("confirm"),
            async () => {
              await action();
              d.close();
            },
            { className: "danger" },
          ),
        ),
      ),
    );
    d.addEventListener("close", () => resolve());
  });
}
export function state(title, description, action) {
  return el(
    "section",
    { class: "state" },
    el("span", { class: "state-symbol" }, icon("folder")),
    el("h2", {}, title),
    description ? el("p", {}, description) : null,
    action,
  );
}
