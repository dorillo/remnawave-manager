import { t } from "./morrow-i18n.js";
export function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (key.startsWith("on") && typeof value === "function")
      node.addEventListener(key.slice(2), value);
    else if (key === "class") node.className = value;
    else if (key === "value") node.value = value;
    else if (value !== false && value != null)
      node.setAttribute(key, value === true ? "" : value);
  }
  node.append(...children.flat().filter((child) => child != null));
  return node;
}
export const button = (label, action, className = "") =>
  el("button", { type: "button", class: className, onclick: action }, label);
export const loadingIndicator = (className = "") =>
  el(
    "div",
    { class: "loading-indicator " + className, role: "status" },
    el("span", { class: "loading-spinner", "aria-hidden": true }),
    el("span", {}, t("loading")),
  );
export const loadingLabel = () => [
  el("span", { class: "loading-spinner", "aria-hidden": true }),
  el("span", {}, t("loading")),
];
export const field = (label, input) =>
  el("label", { class: "field" }, el("span", {}, label), input);
export function select(values, value, change) {
  const input = el(
    "select",
    { onchange: (event) => change?.(event.target.value) },
    values.map(([key, label]) => el("option", { value: key }, label)),
  );
  input.value = value;
  return input;
}
let timer;
export function notice(message) {
  const dialog = [...document.querySelectorAll("dialog[open]")].at(-1);
  let node = document.getElementById("notice");
  if (dialog) {
    node.hidden = true;
    node = dialog.querySelector(".dialog-notice");
    if (!node) {
      node = el("p", { class: "dialog-notice", role: "status" });
      dialog.append(node);
    }
  }
  node.textContent = message;
  node.hidden = false;
  clearTimeout(timer);
  timer = setTimeout(() => {
    node.hidden = true;
  }, 7000);
}
export function modal(title, content) {
  const previous = document.activeElement;
  const heading = el("h2", { id: "dialog-" + crypto.randomUUID() }, title);
  const dialog = el(
    "dialog",
    { "aria-labelledby": heading.id },
    el(
      "header",
      {},
      heading,
      iconButton("close", t("close"), () => dialog.close(), "modal-close"),
    ),
    content,
  );
  document.body.append(dialog);
  dialog.showModal();
  const close = dialog.close.bind(dialog);
  dialog.close = () => {
    close();
    dialog.remove();
  };
  dialog.addEventListener("close", () => {
    dialog.remove();
    if (!document.querySelector("dialog[open]") && previous?.isConnected)
      previous.focus();
  });
  return dialog;
}
export function confirmAction(title, description, action, label = "delete") {
  const error = el("p", { class: "error", role: "alert" });
  const accept = button(
    t(label),
    async () => {
      accept.disabled = true;
      try {
        await action();
        dialog.close();
      } catch (e) {
        error.textContent = t(e.message);
        accept.disabled = false;
      }
    },
    "danger",
  );
  const dialog = modal(
    title,
    el(
      "div",
      {},
      el("p", {}, description),
      error,
      el(
        "div",
        { class: "actions" },
        button(t("cancel"), () => dialog.close()),
        accept,
      ),
    ),
  );
}
export async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    notice(t("copied"));
  } catch {
    notice(t("copyFailed"));
  }
}
export function icon(name) {
  const paths = {
    refresh: "M20 7a9 9 0 1 0 1 8M20 3v5h-5",
    home: "m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1z",
    heart:
      "M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8Z",
    comment: "M21 11.5a8.5 8.5 0 0 1-8.5 8.5H3l2-5a8.5 8.5 0 1 1 16-3.5Z",
    bookmark: "M6 3h12v18l-6-4-6 4Z",
    share: "m14 3 7 7-7 7v-4c-5 0-8 2-11 6 0-8 4-12 11-12Z",
    search: "M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0",
    user: "M20 21v-2a7 7 0 0 0-14 0v2M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0",
    users:
      "M15 21v-2a6 6 0 0 0-12 0v2M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0M18 3a4 4 0 0 1 0 8m0 4a5 5 0 0 1 3 5",
    play: "m8 4 12 8-12 8Z",
    pause: "M8 4v16M16 4v16",
    volume: "m11 4-6 5H2v6h3l6 5Zm4 4a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14",
    mute: "m11 4-6 5H2v6h3l6 5Zm5 5 6 6m0-6-6 6",
    up: "m6 15 6-6 6 6",
    down: "m6 9 6 6 6-6",
    close: "m6 6 12 12M6 18 18 6",
    dots: "M5 12h.01M12 12h.01M19 12h.01",
    settings:
      "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8M9 3h6l1 3 3 1 2 5-2 5-3 1-1 3H9l-1-3-3-1-2-5 2-5 3-1Z",
    compass: "m16 8-3 5-5 3 3-5ZM22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0",
    clock: "M12 6v6l4 2M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0",
    image: "M4 5a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2Zm3 12 3-3 2 2 3-4 3 5M8 9h.01",
  };
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [k, v] of Object.entries({
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "1.8",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true",
  }))
    svg.setAttribute(k, v);
  const path = document.createElementNS(svg.namespaceURI, "path");
  path.setAttribute("d", paths[name] || paths.play);
  svg.append(path);
  return svg;
}
export function iconButton(name, label, action, cls = "") {
  const b = button("", action, cls);
  b.setAttribute("aria-label", label);
  b.title = label;
  b.append(icon(name));
  return b;
}
export function portrait(person, cls = "") {
  return person?.avatar
    ? el("img", {
        class: "avatar " + cls,
        src: person.avatar,
        alt: "",
        loading: "lazy",
        referrerpolicy: "no-referrer",
        onerror: (e) => {
          e.target.hidden = true;
        },
      })
    : el(
        "span",
        { class: "avatar placeholder " + cls, "aria-hidden": "true" },
        (person?.name || "?").slice(0, 1).toUpperCase(),
      );
}
