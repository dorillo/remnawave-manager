import { guestPrompt, emptyPrompt } from '../shared/guest-state.js';
import { loadingNode } from '../shared/feedback.js';
import { t, preference } from "./loop-i18n.js";
import {
  el,
  icon,
  button,
  dialog,
  field,
  confirm,
  notice,
  state,
} from "./loop-ui.js";
import * as data from "./loop-data.js";
import * as store from "./loop-store.js";
import * as uploads from "./loop-uploads.js";
import { gifVideoPlayer } from "./loop-player.js";
let ownUploads = [];
import { authenticate } from "./loop-auth.js";

let account = null,
  liked = new Set(),
  controller,
  generation = 0,
  main,
  searchInput,
  observer;
let categories = [],
  trends = [],
  currentKey = "",
  previousList = "#/explore",
  pendingAction = null;
const positions = new Map(),
  pages = new Map();
const app = document.getElementById("app");
// CSSOM properties preserve the strict CSP; no inline style text or external library.
function layoutGalleries() {
  for (const grid of app.querySelectorAll(".gallery")) {
    const css = getComputedStyle(grid),
      columns = Number(css.getPropertyValue("--columns")) || 4,
      gap = parseFloat(css.columnGap) || 14,
      width = (grid.clientWidth - gap * (columns - 1)) / columns,
      stacks = Array.from({ length: columns }, () => []),
      heights = Array(columns).fill(0);
    grid.classList.add("masonry");
    for (const card of grid.children) {
      const column = heights.indexOf(Math.min(...heights)),
        media = card.querySelector(".card-media"),
        [w, h] = media.style.aspectRatio.split("/").map(Number),
        ratio = h / w;
      stacks[column].push({ card, media, ratio });
      heights[column] += width * ratio + gap;
    }
    // Slightly vary column widths to give every column the same bottom edge,
    // without cropping media, hiding cards or stretching their proportions.
    const sums = stacks.map((stack) =>
      stack.reduce((sum, item) => sum + item.ratio, 0),
    );
    let balanced = sums.every((sum) => sum > 0);
    let height = balanced
      ? (grid.clientWidth -
          gap * (columns - 1) +
          stacks.reduce(
            (sum, stack, i) => sum + (gap * (stack.length - 1)) / sums[i],
            0,
          )) /
        sums.reduce((sum, ratio) => sum + 1 / ratio, 0)
      : Math.max(0, ...heights) - (grid.children.length ? gap : 0);
    if (
      balanced &&
      stacks.some(
        (stack, i) =>
          (height - gap * (stack.length - 1)) / sums[i] < width * 0.5,
      )
    ) {
      balanced = false;
      height = Math.max(0, ...heights) - (grid.children.length ? gap : 0);
    }
    let left = 0;
    stacks.forEach((stack, i) => {
      const columnWidth = balanced
        ? (height - gap * (stack.length - 1)) / sums[i]
        : width;
      let top = 0;
      for (const { card, media, ratio } of stack) {
        card.style.width = `${columnWidth}px`;
        card.style.left = `${left}px`;
        card.style.top = `${top}px`;
        media.style.height = `${columnWidth * ratio}px`;
        top += columnWidth * ratio + gap;
      }
      left += columnWidth + gap;
    });
    grid.style.height = `${height}px`;
  }
}
let layoutFrame;
function scheduleLayout() {
  cancelAnimationFrame(layoutFrame);
  layoutFrame = requestAnimationFrame(layoutGalleries);
}
new MutationObserver(scheduleLayout).observe(app, {
  childList: true,
  subtree: true,
});
let layoutWidth = 0;
new ResizeObserver(([entry]) => {
  if (entry.contentRect.width !== layoutWidth) {
    layoutWidth = entry.contentRect.width;
    layoutGalleries();
  }
}).observe(app);
window.addEventListener("resize", layoutGalleries);
document.fonts.ready.then(scheduleLayout);
const safeError = (e) =>
  [
    "storageError",
    "secureError",
    "authError",
    "invalid",
    "duplicate",
    "duplicateCollection",
    "limit",
    "notFound",
    "rateError",
  ].includes(e?.message)
    ? e.message
    : "networkError";
function titleOf(item) {
  return item.title || item.tags.slice(0, 3).join(", ") || t("untitled");
}
function route() {
  const [path, query = ""] = (location.hash.slice(1) || "/explore").split("?");
  const params = new URLSearchParams(query),
    type = ["gif", "sticker", "clip"].includes(params.get("type"))
      ? params.get("type")
      : "gif";
  return {
    path,
    params,
    type,
    query: (params.get("q") || "").slice(0, 120),
    category:
      !(params.get("q") || "").trim() &&
      /^[0-9]{1,12}$/.test(params.get("category") || "")
        ? params.get("category")
        : "",
  };
}
function href(type = "gif", query = "", category = "") {
  const p = new URLSearchParams({ type });
  if (query) p.set("q", query);
  if (category && !query) p.set("category", category);
  return `#/${query ? "search" : "explore"}?${p}`;
}
function go(hash) {
  if (location.hash === hash) render();
  else location.hash = hash;
}
async function refreshAccount() {
  const previous = account?.id;
  account = await store.current();
  if (previous !== account?.id) pages.clear();
  liked = new Set(
    account ? (await store.owned("likes")).map((x) => String(x.media.id)) : [],
  );
}
function requireAccount(action) {
  if (account) return action();
  pendingAction = action;
  openAuth();
}
function openAuth(register = false) {
  const form = el("form", { class: "form" }),
    login = field(t("loginField"), {
      name: "username",
      autocomplete: "username",
      required: true,
      minlength: 3,
      maxlength: 40,
    }),
    password = field(t("password"), {
      name: "password",
      type: "password",
      autocomplete: register ? "new-password" : "current-password",
      required: true,
      minlength: register ? 8 : 1,
      maxlength: 128,
    }),
    name = field(t("name"), {
      name: "name",
      autocomplete: "nickname",
      required: true,
      maxlength: 80,
    });
  login.node.append(el("small", {}, t("loginHint")));
  if (register) password.node.append(el("small", {}, t("passwordHint")));
  const error = el("p", { class: "form-error", role: "alert" }),
    submit = el(
      "button",
      { class: "button primary", type: "submit" },
      t(register ? "register" : "login"),
    );
  form.append(
    ...(register ? [name.node] : []),
    login.node,
    password.node,
    error,
    submit,
  );
  const content = el("div", {}, form),
    d = dialog(t(register ? "registerTitle" : "loginTitle"), content);
  form.append(
    button(
      t(register ? "login" : "register"),
      () => {
        d.close();
        openAuth(!register);
      },
      { className: "auth-switch" },
    ),
  );
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (submit.disabled) return;
    submit.disabled = true;
    form.querySelector(".auth-switch").disabled = true;
    error.textContent = "";
    try {
      await authenticate(
        login.input.value,
        password.input.value,
        register ? name.input.value : undefined,
      );
      await refreshAccount();
      const next = pendingAction;
      pendingAction = null;
      d.close();
      await render();
      if (next) await next();
    } catch (err) {
      error.textContent = t(safeError(err));
    } finally {
      submit.disabled = false;
      form.querySelector(".auth-switch").disabled = false;
    }
  });
  (register ? name.input : login.input).focus();
}
function openNameForm(title, label, value, action) {
  const f = field(label, { value, required: true, maxlength: 60 }),
    form = el("form", { class: "form" }, f.node),
    error = el("p", { class: "form-error", role: "alert" }),
    submit = el(
      "button",
      { type: "submit", class: "button primary" },
      t("confirm"),
    );
  form.append(error, submit);
  const d = dialog(title, form);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (submit.disabled) return;
    submit.disabled = true;
    try {
      if (!f.input.value.trim()) throw new Error("invalid");
      await action(f.input.value.trim());
      d.close();
    } catch (err) {
      error.textContent = t(safeError(err));
    } finally {
      submit.disabled = false;
    }
  });
  f.input.focus();
}
function collectionDialog(item) {
  return requireAccount(async () => {
    const picker = el("div", { class: "collection-picker" }),
      content = el("div", {}, picker),
      d = dialog(t("choose"), content);
    const draw = async () => {
      const list = await store.owned("collections");
      picker.replaceChildren();
      if (!list.length)
        picker.append(el("p", { class: "muted" }, t("emptyCollections")));
      for (const c of list) {
        const added = c.items.some((x) => x.id === item.id);
        picker.append(
          button(
            c.name,
            async (control) => {
              control.disabled = true;
              try {
                await store.editCollection(c.id, (value) => {
                  if (added) value.items = value.items.filter(x => x.id !== item.id);
                  else if (!value.items.some(x => x.id === item.id)) {
                    if (value.items.length >= 500) throw new Error("limit");
                    value.items.push(store.mediaSnapshot(item));
                  }
                });
                pages.clear();
                notice(t(added ? "removedFromCollection" : "added"));
                if (picker.isConnected) await draw();
              } catch (error) { notice(t(safeError(error))); }
              finally { control.disabled = false; }
            },
            {
              symbol: added ? "check" : "folder",
              "aria-pressed": String(added),
              "aria-label": `${c.name}${added ? " — " + t("alreadyAdded") : ""}`,
            },
          ),
        );
      }
    };
    content.append(
      button(
        t("newCollection"),
        () => {
          d.close();
          openNameForm(
            t("newCollection"),
            t("collectionName"),
            "",
            async (name) => {
              const c = await store.createCollection(name);
              await store.editCollection(c.id, (value) =>
                value.items.push(store.mediaSnapshot(item)),
              );
              notice(t("added"));
              await render();
            },
          );
        },
        { symbol: "plus", className: "primary" },
      ),
    );
    await draw();
  });
}
async function like(item) {
  return requireAccount(async () => {
    await store.toggleLike(item);
    await refreshAccount();
    syncLikes();
    if (route().path === "/likes") await render();
  });
}
function syncLikes() {
  for (const node of document.querySelectorAll("[data-like]")) {
    const active = liked.has(node.dataset.like);
    node.classList.toggle("active", active);
    node.setAttribute("aria-pressed", String(active));
    node.setAttribute("aria-label", t(active ? "unlike" : "like"));
    const span = node.querySelector("span");
    if (span) span.textContent = t(active ? "unlike" : "like");
  }
}
function likeButton(item, card = false) {
  const active = liked.has(String(item.id));
  return button(card ? "" : t(active ? "unlike" : "like"), () => like(item), {
    symbol: "heart",
    className: `${card ? "card-like" : ""} ${active ? "active" : ""}`,
    "data-like": item.id,
    "aria-label": t(active ? "unlike" : "like"),
    "aria-pressed": String(active),
  });
}
function loading() {
  return el(
    "div",
    { class: "loading", role: "status" },
    loadingNode(t("loading")),
  );
}
function errorState(error, retry) {
  return state(
    t(safeError(error)),
    null,
    retry
      ? button(t("retry"), retry)
      : el("a", { href: "#/explore", class: "button" }, t("explore")),
  );
}
function itemCount(count) {
  if (document.documentElement.lang === "en")
    return `${count} ${count === 1 ? "reaction" : "reactions"}`;
  const mod = count % 100;
  return `${count} ${mod >= 11 && mod <= 14 ? "реакций" : count % 10 === 1 ? "реакция" : [2, 3, 4].includes(count % 10) ? "реакции" : "реакций"}`;
}
function frozenPreview(item) {
  if (item.local && !item.preview) {
    const holder = el("div", { class: "local-media-frame" });
    uploads
      .resolve(item.id)
      .then((value) => {
        if (holder.isConnected) holder.replaceWith(frozenPreview(value));
      })
      .catch(() => {
        holder.textContent = t("mediaError");
      });
    return holder;
  }
  const canvas = el("canvas", {
    role: "img",
    "aria-label": titleOf(item),
    width: 300,
    height: 300,
    "data-still": item.preview || item.original,
  });
  observer.observe(canvas);
  return canvas;
}
function media(item, detail = false) {
  if (item.local && !item.original) {
    const holder = el("div", {
      class: "local-media-frame",
      "data-upload": item.id,
    });
    observer.observe(holder);
    return holder;
  }
  if (
    detail &&
    !item.video &&
    item.original &&
    (item.type === "gif" || item.extension === "gif")
  )
    return gifVideoPlayer(item, {
      title: titleOf(item),
      signal: controller.signal,
      blob: item.local ? () => uploads.sourceBlob(item.id) : null,
    });
  const paused = document.documentElement.dataset.paused === "true";
  let node;
  if (item.video && (!paused || detail)) {
    node = el("video", {
      muted: true,
      playsinline: true,
      loop: true,
      preload: detail ? "metadata" : "none",
      poster: item.local || !paused ? item.preview || undefined : undefined,
      controls: detail,
    });
    node.muted = true;
    if (detail) {
      node.src = item.video;
      if (!paused) node.autoplay = true;
    } else {
      node.dataset.src = item.video;
      observer.observe(node);
    }
  } else if (
    paused &&
    (item.preview || (item.original && !item.original.endsWith(".mp4")))
  ) {
    node = frozenPreview(item);
  } else if (item.preview || (!paused && item.original)) {
    const src =
      (detail || item.local) && !paused && item.original && !item.video
        ? item.original
        : item.preview || item.original;
    node = el("img", {
      src: src || item.preview,
      alt: titleOf(item),
      loading: detail ? "eager" : "lazy",
      decoding: "async",
      width: item.width,
      height: item.height,
    });
  } else node = el("div", { class: "media-failed" }, t("mediaError"));
  node.addEventListener(
    "error",
    () => {
      if (node.tagName === "VIDEO" && item.preview) {
        const image = el("img", {
          src: item.preview,
          alt: titleOf(item),
          loading: "lazy",
        });
        image.addEventListener(
          "error",
          () =>
            image.replaceWith(
              el("div", { class: "media-failed" }, t("mediaError")),
            ),
          { once: true },
        );
        node.replaceWith(image);
      } else
        node.replaceWith(el("div", { class: "media-failed" }, t("mediaError")));
    },
    { once: true },
  );
  return node;
}
function card(item, collection = null) {
  const link = el(
    "a",
    {
      href: `#/item/${item.id}`,
      class: `card-media ${item.type === "sticker" ? "sticker" : ""} `,
      "aria-label": titleOf(item),
    },
    media(item),
    el(
      "span",
      { class: "media-kind" },
      item.local ? `${t(item.type)} · ${t("ownMedia")}` : t(item.type),
    ),
  );
  link.style.aspectRatio = `${item.width} / ${item.height}`;
  const caption = el(
    "div",
    { class: "card-caption" },
    el("a", { href: `#/item/${item.id}`, class: "card-title" }, titleOf(item)),
  );
  if (collection)
    caption.append(
      button(
        "",
        async () => {
          await store.editCollection(collection.id, (c) => {
            c.items = c.items.filter((x) => x.id !== item.id);
          });
          await render();
        },
        {
          symbol: "close",
          className: "card-remove",
          "aria-label": t("removeItem"),
        },
      ),
    );
  return el(
    "article",
    { class: "card" },
    link,
    likeButton(item, true),
    caption,
  );
}
function header(r) {
  const brand = el(
    "a",
    { href: "#/explore", class: "brand", "aria-label": "Loop" },
    el("img", { src: "favicon.svg", alt: "", width: 38, height: 38 }),
    "Loop",
  );
  searchInput = el("input", {
    type: "search",
    name: "q",
    value: r.query,
    placeholder: t("search"),
    "aria-label": t("searchLabel"),
    maxlength: 120,
    autocomplete: "off",
  });
  const form = el(
    "form",
    { class: "search", role: "search" },
    icon("search"),
    searchInput,
    el(
      "button",
      { type: "submit", class: "button icon-button", "aria-label": t("find") },
      icon("forward"),
    ),
  );
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const q = searchInput.value.trim();
    go(href(r.type, q));
  });
  const actions = el(
    "div",
    { class: "header-actions" },
    button(
      document.documentElement.lang === "ru" ? "EN" : "RU",
      () => {
        document.documentElement.lang =
          document.documentElement.lang === "ru" ? "en" : "ru";
        preference("lang", document.documentElement.lang);
        return render();
      },
      { className: "subtle icon-button", "aria-label": t("language") },
    ),
    button(
      "",
      () => {
        const root = document.documentElement;
        root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
        preference("theme", root.dataset.theme);
        return render();
      },
      {
        symbol:
          document.documentElement.dataset.theme === "dark" ? "sun" : "moon",
        className: "subtle icon-button",
        "aria-label": t("theme"),
      },
    ),
    button(
      account ? account.name : t("login"),
      () => {
        if (account) go("#/profile");
        else {
          pendingAction = null;
          openAuth();
        }
      },
      {
        symbol: "user",
        className: "header-account " + (account ? "" : "primary"),
      },
    ),
  );
  return el(
    "header",
    { class: "topbar" },
    el("div", { class: "topbar-inner" }, brand, form, actions),
  );
}
function sidebar(r) {
  const navigation = el("nav", {
    class: "nav-list",
    "aria-label": t("explore"),
  });
  for (const [key, symbol] of [
    ["explore", "grid"],
    ["likes", "heart"],
    ["collections", "folder"],
    ["uploads", "plus"],
    ["profile", "user"],
  ]) {
    const active =
      key === "explore"
        ? ["/explore", "/search", "/item"].some((p) => r.path.startsWith(p))
        : key === "collections"
          ? r.path.startsWith("/collection")
          : r.path === `/${key}`;
    navigation.append(
      el(
        "a",
        {
          href: `#/${key}`,
          class: `nav-link ${active ? "active" : ""}`,
          "aria-label": t(key === "uploads" ? "myMedia" : key),
          "aria-current": active ? "page" : null,
        },
        icon(symbol),
        el(
          "span",
          { class: "nav-label" },
          t(key === "uploads" ? "myMedia" : key),
        ),
        el(
          "span",
          { class: "nav-label-mobile", "aria-hidden": "true" },
          t(
            key === "uploads" ? "mediaNav" : key === "likes" ? "likesNav" : key,
          ),
        ),
      ),
    );
  }
  return el(
    "aside",
    { class: "sidebar" },
    navigation,
    el(
      "div",
      { class: "sidebar-note" },
      el("strong", {}, "Loop"),
      el("p", {}, t("about")),
    ),
  );
}
function pageIntro(title, description, action) {
  return el(
    "section",
    { class: "page-intro" },
    el("div", { class: "heading" }, el("h1", {}, title), action),
    description ? el("p", {}, description) : null,
  );
}
function renderTabs(r) {
  const tabs = el("div", { class: "tabs" });
  for (const type of ["gif", "sticker", "clip"])
    tabs.append(
      el(
        "a",
        {
          href: href(type, r.query, r.category),
          class: type === r.type ? "active" : "",
          "aria-current": type === r.type ? "page" : null,
        },
        t(type),
      ),
    );
  return tabs;
}
async function galleryPage(r, signal, token) {
  if (!r.query && !r.category)
    main.append(
      el(
        "section",
        { class: "hero" },
        el("div", { class: "eyebrow" }, t("eyebrow")),
        el("h1", {}, t("hero")),
        el("p", {}, t("intro")),
      ),
    );
  else
    main.append(
      pageIntro(
        r.query
          ? `${t("searchResults")}: «${r.query}»`
          : categories.find((c) => String(c.id) === r.category)?.title ||
              t("category"),
      ),
    );
  const trendArea = el("div", { class: "trends" });
  if (!r.query && !r.category) main.append(trendArea);
  const select = el(
    "select",
    { "aria-label": t("category") },
    el("option", { value: "" }, t("allCategories")),
  );
  const fillCategories = () => {
    select.replaceChildren(
      el("option", { value: "" }, t("allCategories")),
      ...categories.map((c) => el("option", { value: c.id }, c.title)),
    );
    select.value = r.category;
    if (r.category) {
      const heading = main.querySelector(".page-intro h1");
      if (heading)
        heading.textContent =
          categories.find((c) => String(c.id) === r.category)?.title ||
          t("category");
    }
  };
  fillCategories();
  select.addEventListener("change", () => go(href(r.type, "", select.value)));
  const paused = document.documentElement.dataset.paused === "true";
  main.append(
    el(
      "div",
      { class: "toolbar" },
      renderTabs(r),
      el("label", { class: "category-select" }, select),
      button(
        "",
        () => {
          document.documentElement.dataset.paused = String(!paused);
          preference("paused", String(!paused));
          return render();
        },
        {
          symbol: paused ? "play" : "pause",
          className: "icon-button",
          "aria-label": t(paused ? "play" : "pause"),
          "aria-pressed": String(paused),
        },
      ),
    ),
  );
  const fillTrends = () => {
    trendArea.replaceChildren(
      el("span", {}, t("trending")),
      ...trends.map((q) =>
        el("a", { class: "chip", href: href(r.type, q) }, q),
      ),
    );
  };
  if (trends.length) fillTrends();
  if (!categories.length)
    data
      .categories(signal)
      .then((values) => {
        if (token === generation) {
          categories = values;
          fillCategories();
        }
      })
      .catch(() => {});
  if (!trends.length && !r.query && !r.category)
    data
      .trending(signal)
      .then((values) => {
        if (token === generation) {
          trends = values;
          if (trends.length) fillTrends();
        }
      })
      .catch(() => {});
  if (!r.query && !r.category)
    main.append(el("h2", { class: "section-title" }, t("popular")));
  const grid = el("div", { class: "gallery" }),
    bottom = el("div", {});
  main.append(grid, bottom);
  const key = location.hash || "#/explore",
    cached = pages.get(key);
  let items = cached?.items || [],
    cursor = cached?.cursor,
    more = cached?.more ?? true,
    busy = false;
  if (items.length) grid.append(...items.map((x) => card(x)));
  const drawBottom = () => {
    bottom.replaceChildren(
      more
        ? el(
            "div",
            { class: "load-more" },
            button(t("more"), load, { className: "primary" }),
          )
        : items.length
          ? el("p", { class: "end" }, t("end"))
          : state(t("empty"), t("noResults")),
    );
  };
  async function load() {
    if (busy || signal.aborted) return;
    busy = true;
    const restoreFocus = bottom.contains(document.activeElement);
    bottom.replaceChildren(loading());
    try {
      const result = await uploads.mixedFeed(
        { type: r.type, query: r.query, category: r.category },
        cursor,
        uploads.matching(ownUploads, r),
        data.feed,
        signal,
      );
      if (token !== generation) return;
      const ids = new Set(items.map((x) => x.id)),
        next = result.items.filter((x) => {
          if (ids.has(x.id)) return false;
          ids.add(x.id);
          return true;
        });
      items = items.concat(next);
      cursor = result.cursor;
      more = result.more;
      grid.append(...next.map((x) => card(x)));
      pages.set(key, { items, cursor, more });
      if (pages.size > 8) pages.delete(pages.keys().next().value);
      drawBottom();
      if (result.remoteError) notice(t("remoteUnavailable"));
      layoutGalleries();
      if (restoreFocus) {
        const target = next.length
          ? grid.children[items.length - next.length]?.querySelector(
              ".card-media",
            )
          : bottom.querySelector("button, .end");
        if (target) {
          if (target.classList.contains("end")) target.tabIndex = -1;
          target.focus({ preventScroll: true });
        }
      }
    } catch (e) {
      if (token === generation && e.name !== "AbortError")
        bottom.replaceChildren(errorState(e, load));
    } finally {
      busy = false;
    }
  }
  if (cached) drawBottom();
  else await load();
}
function exportURL(item) {
  if (item.local) return item.original || "";
  const path = item.original || item.video || item.preview;
  return /^\/_loop\/media\/[a-f0-9]{32,64}(?:_(?:150|300|500|preview))?\.(?:gif|webp|mp4|png|jpg)$/.test(
    path,
  )
    ? new URL(path, location.origin).href
    : "";
}
function embedDialog(item) {
  const local = exportURL(item);
  if (!local) return notice(t("mediaError"));
  const extension = item.extension || new URL(local).pathname.split(".").pop();
  const url = `loop-${item.id}.${extension}`;
  const video = item.local ? item.type === "clip" : extension === "mp4",
    width = Math.min(item.width, 480),
    node = document.implementation
      .createHTMLDocument("")
      .createElement(video ? "video" : "img");
  const attributes = {
    src: url,
    width,
    height: Math.round((width * item.height) / item.width),
    ...(video
      ? {
          controls: true,
          loop: true,
          playsinline: true,
          preload: "metadata",
          "aria-label": titleOf(item),
        }
      : { alt: titleOf(item), loading: "lazy" }),
  };
  for (const [name, value] of Object.entries(attributes))
    node.setAttribute(name, value === true ? "" : String(value));
  node.style.maxWidth = "100%";
  node.style.height = "auto";
  const code = el("textarea", {
    class: "embed-code",
    readonly: true,
    rows: 7,
    "aria-label": t("embedCode"),
    spellcheck: "false",
  });
  code.value = node.outerHTML;
  dialog(
    t("embed"),
    el(
      "div",
      { class: "form" },
      el("p", { class: "form-note" }, t("embedHelp")),
      code,
      button(
        t("copyCode"),
        async () => {
          try {
            await navigator.clipboard.writeText(code.value);
            notice(t("codeCopied"));
          } catch {
            code.focus();
            code.select();
            notice(t("copyCodeHelp"));
          }
        },
        { className: "primary" },
      ),
    ),
  );
}
async function detailPage(id, signal, token) {
  main.append(
    el(
      "div",
      { class: "detail-top" },
      button(t("back"), () => go(previousList), {
        symbol: "arrow",
        className: "subtle",
      }),
    ),
    loading(),
  );
  try {
    const item = uploads.isLocal(id)
      ? await uploads.resolve(id)
      : await data.item(id, signal);
    if (token !== generation) return;
    main.querySelector(".loading")?.remove();
    document.title = `Loop — ${titleOf(item)}`;
    const info = el(
      "div",
      { class: "detail-info" },
      el("span", { class: "eyebrow" }, t(item.type)),
      el("h1", {}, titleOf(item)),
      item.author
        ? el("p", { class: "author" }, `${t("author")} · @${item.author}`)
        : null,
    );
    info.append(
      el(
        "div",
        { class: "detail-actions" },
        likeButton(item),
        exportURL(item)
          ? el(
              "a",
              {
                class: "button",
                href: exportURL(item),
                download: `loop-${item.id}.${item.extension || exportURL(item).split(".").pop()}`,
              },
              icon("download"),
              t("download"),
            )
          : null,
        exportURL(item)
          ? button(t("embed"), () => embedDialog(item), { symbol: "code" })
          : null,
        button(t("save"), () => collectionDialog(item), {
          symbol: "folder",
          className: "primary",
        }),
        button(
          t("share"),
          async () => {
            if (item.local) {
              try {
                const stored = await store.get("files", item.id);
                if (stored?.owner !== store.session())
                  throw new Error("notFound");
                const file = new File(
                  [stored.blob],
                  `loop-${item.id}.${item.extension}`,
                  { type: stored.blob.type },
                );
                if (navigator.canShare?.({ files: [file] }))
                  await navigator.share({
                    files: [file],
                    title: titleOf(item),
                  });
                else notice(t("localShareHelp"));
              } catch (e) {
                if (e.name !== "AbortError") notice(t("localShareHelp"));
              }
              return;
            }
            const url = new URL(location.href);
            url.hash = `/item/${item.id}`;
            try {
              if (navigator.share)
                await navigator.share({ title: titleOf(item), url: url.href });
              else {
                await navigator.clipboard.writeText(url.href);
                notice(t("copied"));
              }
            } catch (e) {
              if (e.name !== "AbortError") notice(t("copyError"));
            }
          },
          { symbol: "share" },
        ),
      ),
      el(
        "div",
        { class: "detail-tags" },
        item.tags.map((tag) =>
          el("a", { href: href(item.type, tag), class: "chip" }, tag),
        ),
      ),
      item.local
        ? el(
            "div",
            { class: "local-source" },
            button(
              t("deleteMedia"),
              () =>
                confirm(t("deleteMediaQuestion"), async () => {
                  await uploads.remove(item.id);
                  pages.clear();
                  await refreshAccount();
                  notice(t("mediaDeleted"));
                  go("#/uploads");
                }),
              { symbol: "trash", className: "danger" },
            ),
          )
        : null,
    );
    main.append(
      el(
        "section",
        { class: "detail" },
        el("div", { class: "detail-media" }, media(item, true)),
        info,
      ),
    );
    const related = el(
        "section",
        { class: "related" },
        el("h2", { class: "section-title" }, t("related")),
      ),
      grid = el("div", { class: "gallery" }),
      status = el("div", {}),
      ids = new Set([item.id]);
    let cursor,
      busy = false;
    related.append(grid, status);
    main.append(related);
    async function loadRelated() {
      if (busy || signal.aborted) return;
      busy = true;
      const restoreFocus = status.contains(document.activeElement);
      status.replaceChildren(loading());
      try {
        const result = await uploads.mixedFeed(
          item.local
            ? {
                type: item.type,
                category: item.category || "",
              }
            : { type: item.type, related: item.id },
          cursor,
          uploads.matching(ownUploads, { type: item.type, related: item }),
          data.feed,
          signal,
        );
        if (token !== generation) return;
        const next = result.items.filter((x) => {
          if (ids.has(x.id)) return false;
          ids.add(x.id);
          return true;
        });
        const firstNew = grid.children.length;
        grid.append(...next.map((x) => card(x)));
        cursor = result.cursor;
        status.replaceChildren(
          result.more
            ? el(
                "div",
                { class: "load-more" },
                button(t("more"), loadRelated, { className: "primary" }),
              )
            : el("p", { class: "end", tabindex: -1 }, t("end")),
        );
        if (!grid.children.length && !result.more) related.remove();
        layoutGalleries();
        if (restoreFocus)
          (
            grid.children[firstNew]?.querySelector(".card-media") ||
            status.querySelector("button, .end")
          )?.focus({ preventScroll: true });
      } catch (e) {
        if (token === generation && e.name !== "AbortError")
          status.replaceChildren(errorState(e, loadRelated));
      } finally {
        busy = false;
      }
    }
    await loadRelated();
  } catch (e) {
    if (token === generation && e.name !== "AbortError") {
      main.querySelector(".loading")?.remove();
      main.append(errorState(e, () => render()));
    }
  }
}
function gated(kind = 'profile') {
  main.append(guestPrompt(kind, () => { pendingAction = null; openAuth(); }));
}
const localPageSizes = new Map();
function localList(items, draw, className = "gallery") {
  const key = `${store.session()}:${location.hash}`,
    grid = el("div", { class: className }),
    bottom = el("div", {}),
    section = el("section", {}, grid, bottom);
  let shown = 0;
  function load(count = data.pageSize) {
    const restoreFocus = bottom.contains(document.activeElement),
      start = shown;
    shown = Math.min(items.length, shown + count);
    grid.append(...items.slice(start, shown).map(draw));
    localPageSizes.set(key, shown);
    if (localPageSizes.size > 40)
      localPageSizes.delete(localPageSizes.keys().next().value);
    bottom.replaceChildren(
      ...(shown < items.length
        ? [
            el(
              "div",
              { class: "load-more" },
              button(t("more"), () => load(), { className: "primary" }),
            ),
          ]
        : []),
    );
    layoutGalleries();
    if (restoreFocus)
      (grid.children[start]?.querySelector("a") || grid.children[start])?.focus(
        { preventScroll: true },
      );
  }
  load(Math.max(data.pageSize, localPageSizes.get(key) || 0));
  return section;
}
async function libraryPage(r, token) {
  const isLikes = r.path === "/likes",
    isCollection = r.path.startsWith("/collection/");
  if (!account) {
    main.append(
      pageIntro(t(isLikes ? "likes" : "collections"), t("libraryIntro")),
    );
    gated(isLikes ? 'likedMedia' : 'collections');
    return;
  }
  const collections = await store.owned("collections");
  if (token !== generation) return;
  if (isLikes) {
    main.append(pageIntro(t("likes"), t("libraryIntro")));
    const list = await store.owned("likes");
    if (token !== generation) return;
    main.append(
      list.length
        ? localList(list, (x) => card(x.media))
        : emptyPrompt('likedMedia', () => go('#/explore')),
    );
    return;
  }
  if (isCollection) {
    const c = collections.find(
      (x) => x.id === r.path.slice("/collection/".length),
    );
    if (!c) {
      main.append(state(t("badRoute")));
      return;
    }
    main.append(
      el(
        "div",
        { class: "detail-top" },
        el(
          "a",
          { href: "#/collections", class: "button subtle" },
          icon("arrow"),
          t("collections"),
        ),
      ),
      pageIntro(
        c.name,
        itemCount(c.items.length),
        el(
          "div",
          { class: "detail-actions" },
          button(t("rename"), () =>
            openNameForm(
              t("rename"),
              t("collectionName"),
              c.name,
              async (name) => {
                if (
                  collections.some(
                    (x) =>
                      x.id !== c.id &&
                      x.name.toLocaleLowerCase() === name.toLocaleLowerCase(),
                  )
                )
                  throw new Error("duplicateCollection");
                await store.editCollection(
                  c.id,
                  (value) => (value.name = name),
                );
                await render();
              },
            ),
          ),
          button(
            t("remove"),
            () =>
              confirm(t("deleteCollection"), async () => {
                await store.remove("collections", c.id);
                go("#/collections");
              }),
            { symbol: "trash", className: "danger" },
          ),
        ),
      ),
    );
    main.append(
      c.items.length
        ? localList(c.items, (x) => card(x, c))
        : emptyPrompt('collection', () => go('#/explore')),
    );
    return;
  }
  main.append(
    pageIntro(
      t("collections"),
      t("collectionIntro"),
      button(
        t("newCollection"),
        () =>
          openNameForm(
            t("newCollection"),
            t("collectionName"),
            "",
            async (name) => {
              const c = await store.createCollection(name);
              go(`#/collection/${c.id}`);
            },
          ),
        { symbol: "plus", className: "primary" },
      ),
    ),
  );
  if (!collections.length) {
    main.append(emptyPrompt('collections'));
    return;
  }
  main.append(
    localList(
      collections,
      (c) => {
        const covers = c.items.filter((x) => x.preview || x.local).slice(0, 2);
        return el(
          "a",
          { href: `#/collection/${c.id}`, class: "collection-tile" },
          el(
            "div",
            { class: "collection-cover" },
            covers.length ? covers.map(frozenPreview) : icon("folder"),
          ),
          el(
            "div",
            { class: "collection-text" },
            el("h3", {}, c.name),
            el("p", {}, itemCount(c.items.length)),
          ),
        );
      },
      "collection-grid",
    ),
  );
}
function uploadDialog() {
  return requireAccount(() => {
    const cancel = new AbortController(),
      file = field(t("uploadFile"), {
        type: "file",
        required: true,
        accept: "image/gif",
      }),
      title = field(t("mediaTitle"), { required: true, maxlength: 200 }),
      tags = field(t("mediaTags"), {
        maxlength: 2400,
        placeholder: t("tagsHint"),
      }),
      type = el(
        "select",
        { "aria-label": t("mediaType") },
        ["gif", "sticker", "clip"].map((value) =>
          el("option", { value }, t(value)),
        ),
      ),
      category = el(
        "select",
        { "aria-label": t("category") },
        el("option", { value: "" }, t("noCategory")),
        categories.map((c) => el("option", { value: c.id }, c.title)),
      ),
      error = el("p", { class: "form-error", role: "alert" }),
      submit = el(
        "button",
        { class: "button primary", type: "submit" },
        t("upload"),
      ),
      form = el(
        "form",
        { class: "form upload-form" },
        el("label", { class: "field" }, t("mediaType"), type),
        file.node,
        title.node,
        tags.node,
        el("label", { class: "field" }, t("category"), category),
        error,
        submit,
      ),
      d = dialog(t("upload"), form);
    const fileName = el(
      "span",
      { class: "file-picker-name", "aria-live": "polite" },
      t("noFile"),
    );
    const fileAction = el(
      "span",
      { class: "file-picker-action" },
      icon("share"),
      t("chooseFile"),
    );
    file.node.append(
      el(
        "span",
        { class: "file-picker" },
        file.input,
        el("span", { class: "file-picker-content" }, fileAction, fileName),
      ),
    );
    d.addEventListener("close", () => cancel.abort(), { once: true });
    if (!categories.length)
      data
        .categories(cancel.signal)
        .then((values) => {
          if (!d.isConnected) return;
          categories = values;
          category.append(
            ...values.map((c) => el("option", { value: c.id }, c.title)),
          );
        })
        .catch(() => {});
    type.addEventListener("change", () => {
      file.input.accept =
        type.value === "gif"
          ? "image/gif"
          : type.value === "sticker"
            ? "image/gif,image/png,image/webp,image/jpeg"
            : "video/mp4,video/webm";
    });
    file.input.addEventListener("change", () => {
      fileName.textContent = file.input.files[0]?.name || t("noFile");
      fileAction.replaceChildren(
        icon("share"),
        t(file.input.files.length ? "changeFile" : "chooseFile"),
      );
      if (!title.input.value.trim() && file.input.files[0])
        title.input.value = file.input.files[0].name
          .replace(/\.[^.]+$/, "")
          .slice(0, 200);
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (submit.disabled) return;
      submit.disabled = true;
      error.textContent = "";
      submit.replaceChildren(loadingNode(t("uploading")));
      form.setAttribute("aria-busy", "true");
      for (const input of form.querySelectorAll("input, select"))
        input.disabled = true;
      try {
        const result = await uploads.save(
          {
            file: file.input.files[0],
            type: type.value,
            title: title.input.value,
            tags: [
              ...new Set(
                tags.input.value
                  .split(",")
                  .map((value) => value.trim())
                  .filter(Boolean),
              ),
            ],
            category: category.value,
          },
          cancel.signal,
        );
        pages.clear();
        d.close();
        notice(t("uploaded"));
        go(`#/item/${result.id}`);
      } catch (e) {
        if (e.name !== "AbortError")
          error.textContent = t(
            [
              "uploadFormat",
              "uploadDecode",
              "uploadInvalid",
              "authError",
            ].includes(e.message)
              ? e.message
              : "uploadStorageError",
          );
      } finally {
        submit.disabled = false;
        submit.textContent = t("upload");
        form.removeAttribute("aria-busy");
        for (const input of form.querySelectorAll("input, select"))
          input.disabled = false;
      }
    });
  });
}
function uploadsPage() {
  main.append(
    pageIntro(
      t("myMedia"),
      null,
      button(t("upload"), uploadDialog, {
        symbol: "plus",
        className: "primary",
      }),
    ),
  );
  if (!account) {
    gated('uploadsMedia');
    return;
  }
  main.append(
    ownUploads.length
      ? localList(ownUploads, (item) => card(item))
      : emptyPrompt('uploads'),
  );
}
function profilePage() {
  main.append(pageIntro(t("profile")));
  if (!account) {
    gated();
    return;
  }
  const name = field(t("name"), {
      value: account.name,
      required: true,
      maxlength: 80,
    }),
    form = el("form", { class: "form" }, name.node),
    submit = el(
      "button",
      { type: "submit", class: "button primary" },
      t("editName"),
    ),
    error = el("p", { class: "form-error", role: "alert" });
  form.append(error, submit);
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (submit.disabled) return;
    submit.disabled = true;
    try {
      if (!name.input.value.trim()) throw new Error("invalid");
      await store.updateName(account.id, name.input.value);
      await refreshAccount();
      await render();
      notice(t("nameSaved"));
    } catch (err) {
      error.textContent = t(safeError(err));
    } finally {
      submit.disabled = false;
    }
  });
  main.append(
    el(
      "section",
      { class: "profile-panel" },
      el("p", { class: "eyebrow" }, `@${account.login}`),
      form,
      el(
        "div",
        { class: "profile-actions" },
        button(t("logout"), () =>
          confirm(t("logoutQuestion"), async () => {
            store.signOut();
            pendingAction = null;
            await refreshAccount();
            await render();
          }),
        ),
        button(
          t("deleteProfile"),
          () =>
            confirm(t("deleteAccount"), async () => {
              await store.deleteAccount();
              pendingAction = null;
              await refreshAccount();
              await render();
            }),
          { className: "danger" },
        ),
      ),
    ),
  );
}
async function render() {
  const focused = document.activeElement,
    hadFocus = app.contains(focused),
    searchFocused = focused === searchInput,
    headerFocus = [...app.querySelectorAll(".header-actions button")].indexOf(
      focused,
    );
  const nextKey = location.hash || "#/explore";
  if (currentKey) positions.set(currentKey, window.scrollY);
  if (positions.size > 60) positions.delete(positions.keys().next().value);
  if (currentKey && !route().path.startsWith("/item/")) previousList = nextKey;
  else if (currentKey && !currentKey.startsWith("#/item/"))
    previousList = currentKey;
  const changed = currentKey !== nextKey;
  const draft = !changed ? searchInput?.value : null;
  currentKey = nextKey;
  controller?.abort();
  observer?.disconnect();
  controller = new AbortController();
  const signal = controller.signal,
    token = ++generation;
  observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        const node = entry.target;
        if (node.dataset.upload) {
          if (entry.isIntersecting) {
            observer.unobserve(node);
            uploads
              .resolve(node.dataset.upload)
              .then((value) => {
                if (node.isConnected) node.replaceWith(media(value));
              })
              .catch(() => {
                node.textContent = t("mediaError");
              });
          }
          continue;
        }
        if (node.tagName === "CANVAS") {
          if (entry.isIntersecting) {
            observer.unobserve(node);
            const image = new Image();
            image.onload = () => {
              if (!node.isConnected) return;
              node.width = Math.min(image.naturalWidth, 480);
              node.height = Math.max(
                1,
                Math.round(
                  (node.width * image.naturalHeight) / image.naturalWidth,
                ),
              );
              node
                .getContext("2d")
                .drawImage(image, 0, 0, node.width, node.height);
              node.dataset.ready = "true";
            };
            image.onerror = () =>
              node.replaceWith(
                el("div", { class: "media-failed" }, t("mediaError")),
              );
            image.src = node.dataset.still;
          }
          continue;
        }
        if (
          entry.isIntersecting &&
          document.documentElement.dataset.paused !== "true" &&
          !document.hidden
        ) {
          if (!node.src && node.dataset.src) node.src = node.dataset.src;
          node.play().catch(() => {});
        } else node.pause();
      }
    },
    { threshold: 0, rootMargin: "250px 0px" },
  );
  const r = route(),
    renderedAccount = account;
  main = el("main", { id: "main", tabindex: -1 }, loading());
  app.replaceChildren(
    el(
      "a",
      {
        href: "#main",
        class: "skip",
        onclick: (e) => {
          e.preventDefault();
          main.focus();
        },
      },
      t("skip"),
    ),
    header(r),
    el("div", { class: "layout" }, sidebar(r), main),
  );
  if (draft != null) searchInput.value = draft;
  document.title = `Loop — ${t(r.path === "/likes" ? "likes" : r.path.startsWith("/collection") ? "collections" : r.path === "/profile" ? "profile" : r.path === "/uploads" ? "myMedia" : "explore")}`;
  if (changed) window.scrollTo(0, 0);
  const initialScroll = window.scrollY;
  try {
    try {
      await refreshAccount();
      if (token !== generation) return;
      if (
        renderedAccount?.id !== account?.id ||
        renderedAccount?.name !== account?.name
      ) {
        app.querySelector(".topbar").replaceWith(header(r));
        if (draft != null) searchInput.value = draft;
      }
      const currentUploads = await uploads.list();
      if (token !== generation) return;
      if (
        ownUploads.map((x) => x.id).join() !==
        currentUploads.map((x) => x.id).join()
      )
        pages.clear();
      ownUploads = currentUploads;
    } catch (error) {
      const publicPage =
        ["/explore", "/search"].includes(r.path) ||
        (r.path.startsWith("/item/") && !uploads.isLocal(r.path.slice(6)));
      if (!publicPage) throw error;
      ownUploads = [];
      if (token === generation) notice(t("storageError"));
    }
    if (token !== generation) return;
    main.replaceChildren();
    if (r.path === "/uploads") uploadsPage();
    else if (r.path === "/explore" || r.path === "/search")
      await galleryPage(r, signal, token);
    else if (r.path.startsWith("/item/"))
      await detailPage(r.path.slice(6), signal, token);
    else if (
      r.path === "/likes" ||
      r.path === "/collections" ||
      r.path.startsWith("/collection/")
    )
      await libraryPage(r, token);
    else if (r.path === "/profile") profilePage();
    else
      main.append(
        state(
          t("badRoute"),
          null,
          el("a", { href: "#/explore", class: "button primary" }, t("explore")),
        ),
      );
  } catch (e) {
    if (token === generation && e.name !== "AbortError")
      main.append(errorState(e, () => render()));
  }
  if (token === generation) {
    syncLikes();
    layoutGalleries();
    if (hadFocus && !document.querySelector("dialog[open]")) {
      const target =
        !changed && searchFocused
          ? searchInput
          : !changed && headerFocus >= 0
            ? app.querySelectorAll(".header-actions button")[headerFocus]
            : main;
      target?.focus({ preventScroll: true });
    }
    requestAnimationFrame(() => {
      if (token === generation && window.scrollY === initialScroll)
        window.scrollTo(0, positions.get(nextKey) || 0);
    });
  }
}
window.addEventListener("hashchange", () => render());
document.addEventListener("keydown", (e) => {
  if (
    (e.metaKey || e.ctrlKey) &&
    e.key.toLowerCase() === "k" &&
    !document.querySelector("dialog[open]")
  ) {
    e.preventDefault();
    searchInput?.focus();
  }
});
document.addEventListener("visibilitychange", () => {
  for (const video of document.querySelectorAll("video")) {
    if (document.hidden) video.pause();
    else if (!video.controls && !video.closest(".media-player")) {
      observer.unobserve(video);
      observer.observe(video);
    }
  }
});
await render();

window.addEventListener("storage", async event => {
  if (event.key !== "loop:session" || event.oldValue === event.newValue) return;
  for (const dialog of document.querySelectorAll("dialog[open]")) dialog.close();
  render();
});
