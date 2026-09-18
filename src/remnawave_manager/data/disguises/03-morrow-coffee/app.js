import { t, getLanguage, setLanguage, number } from "./morrow-i18n.js";
import {
  el,
  button,
  field,
  select,
  modal,
  confirmAction,
  notice,
  copy,
  icon,
  iconButton,
  loadingIndicator,
  loadingLabel,
  portrait,
} from "./morrow-ui.js";
import {
  authenticate,
  restoreSession,
  logout,
  removeAccount,
} from "./morrow-auth.js";
import {
  ProfileStore,
  toggle,
  remember,
} from "./morrow-store.js";
import { avatar } from "./morrow-files.js";
import * as data from "./morrow-data.js";
import { validId } from "./morrow-yappy.js";
import { createFeed } from "./morrow-feed.js";
import { createPlayer, pauseAll } from "./morrow-player.js";
import { openComments } from "./morrow-comments.js";
import {
  openVideo as openUpload,
  recordView,
  removeVideo,
  saveVideo,
  videosFor,
} from "./morrow-uploads.js";
let account = null,
  store = null,
  feed = null,
  routeController = null,
  routeVersion = 0;
let theme = "system",
  economy = false;
const themeMedia = matchMedia("(prefers-color-scheme: dark)");
function applyTheme() {
  document.documentElement.dataset.theme =
    theme === "system" ? (themeMedia.matches ? "dark" : "light") : theme;
  try {
    localStorage.setItem(
      "morrow:appearance:v1",
      JSON.stringify({ language: getLanguage(), theme }),
    );
  } catch {}
}
themeMedia.addEventListener("change", applyTheme);
applyTheme();
const root = document.getElementById("app");
const nav = el("aside", { class: "sidebar" }),
  header = el("header", { class: "topbar" }),
  main = el("main", { id: "main", class: "main" });
root.append(nav, header, main);
function link(label, href, name) {
  return el(
    "a",
    { href, class: "nav-link" + (location.hash === href ? " active" : "") },
    icon(name),
    el("span", {}, label),
  );
}
function go(path) {
  if (location.hash === path) route();
  else location.hash = path;
}
function updateTitle() {
  const page = (location.hash.slice(1).split('?')[0] || '/feed').split('/')[1];
  const key = { feed: "forYou", following: "following", explore: "explore", search: "search", profile: "profile", settings: "settings", author: "profile", video: "myVideos" }[page] || "forYou";
  document.title = t(key) + " — Morrow";
}
function renderChrome() {
  updateTitle();
  const logo = el(
    "a",
    { href: "#/feed", class: "brand", "aria-label": t("brand") },
    el("img", { src: "favicon.svg", alt: "", width: 38, height: 38 }),
    el("span", {}, t("brand")),
    el("small", {}, "°"),
  );
  nav.replaceChildren(
    logo,
    el(
      "nav",
      { "aria-label": t("brand") },
      link(t("forYou"), "#/feed", "home"),
      link(t("following"), "#/following", "users"),
      link(t("explore"), "#/explore", "compass"),
      link(t("profile"), "#/profile", "user"),
    ),
    el(
      "div",
      { class: "sidebar-bottom" },
      el("p", { class: "tagline" }, t("tagline")),
    ),
  );
  const query = el("input", {
    type: "search",
    placeholder: t("searchPlaceholder"),
    "aria-label": t("search"),
    maxlength: 120,
  });
  const searchForm = el(
    "form",
    {
      class: "searchbar",
      onsubmit: (e) => {
        e.preventDefault();
        if (query.value.trim())
          go("#/search?q=" + encodeURIComponent(query.value.trim()));
      },
    },
    icon("search"),
    query,
  );
  const language = button(
    getLanguage() === "ru" ? "EN" : "RU",
    () => {
      setLanguage(getLanguage() === "ru" ? "en" : "ru");
      for (const d of document.querySelectorAll("dialog")) d.close();
      renderChrome();
      route();
    },
    "language-button",
  );
  language.setAttribute("aria-label", t("language"));
  const appearanceButton = button(icon("appearance"), () => {
    theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    applyTheme();
  }, "language-button");
  appearanceButton.setAttribute("aria-label", t("theme"));
  header.replaceChildren(
    el(
      "a",
      { href: "#/feed", class: "mobile-brand" },
      el("img", { src: "favicon.svg", alt: t("brand"), width: 32, height: 32 }),
    ),
    searchForm,
    el(
      "div",
      { class: "topbar-actions" },
      language,
      appearanceButton,
      account
        ? button(
            store?.data.profile.name || account.login,
            () => go("#/profile"),
            "account-chip",
          )
        : button(t("login"), () => authDialog(), "primary login-button"),
    ),
  );
}
async function mutate(edit) {
  const owner = store;
  if (!owner) throw new Error("sessionExpired");
  await owner.change(edit);
  if (owner === store) {
    feed?.refreshActions();
  }
  return owner.data;
}
function gate(action) {
  if (store) {
    action?.();
    return true;
  }
  authDialog(action);
  return false;
}
function authDialog(after) {
  const login = el("input", {
      autocomplete: "username",
      required: true,
      minlength: 3,
      maxlength: 40,
    }),
    password = el("input", {
      type: "password",
      autocomplete: "current-password",
      required: true,
      minlength: 8,
      maxlength: 128,
    });
  const error = el("p", { class: "error", role: "alert" });
  let registering = false;
  const submit = button(t("login"), () => {}, "primary");
  submit.type = "submit";
  const switcher = button(
    t("register"),
    () => {
      registering = !registering;
      submit.textContent = t(registering ? "register" : "login");
      switcher.textContent = t(registering ? "login" : "register");
      password.autocomplete = registering ? "new-password" : "current-password";
      error.textContent = "";
    },
    "text-button",
  );
  const form = el(
    "form",
    {
      class: "auth-form",
      onsubmit: async (e) => {
        e.preventDefault();
        submit.disabled = true;
        switcher.disabled = true;
        error.textContent = "";
        try {
          const signed = await authenticate(
            login.value,
            password.value,
            registering,
          );
          const loaded = await new ProfileStore(signed).load();
          account = signed;
          store = loaded;
          economy = store.data.economy;
          dialog.close();
          renderChrome();
          feed?.refreshActions();
          if (after) after();
          else if (
            location.hash.startsWith("#/profile") ||
            location.hash.startsWith("#/settings") ||
            location.hash.startsWith("#/following")
          )
            route();
        } catch (e) {
          error.textContent = t(e.message);
        } finally {
          submit.disabled = false;
          switcher.disabled = false;
        }
      },
    },
    el("p", { class: "intro" }, t("accountIntro")),
    field(t("username"), login),
    field(t("password"), password),
    error,
    submit,
    switcher,
  );
  const dialog = modal(t("login"), form);
  dialog.classList.add("auth-dialog");
  login.focus();
}
function empty(
  title = t("empty"),
  description = t("emptyHint"),
  action = button(t("start"), () => go("#/feed"), "primary"),
  iconName = "compass",
) {
  return el(
    "section",
    { class: "empty-state" },
    icon(iconName),
    el("h2", {}, title),
    el("p", {}, description),
    action,
  );
}
function emptyActions(showLogin = false) {
  return el(
    "div",
    { class: "empty-actions" },
    button(t("start"), () => go("#/feed"), "primary"),
    showLogin ? button(t("login"), () => authDialog()) : null,
  );
}
function errorView(e, retry) {
  return el(
    "div",
    { class: "empty-state" },
    el("p", { role: "alert" }, t(e.message)),
    button(t("retry"), retry, "primary"),
  );
}
function openAuthor(id) {
  go("#/author/remote/" + id);
}
function comments(v) {
  if (matchMedia("(max-width: 760px)").matches) pauseAll();
  return openComments(v, {
    getStore: () => store,
    mutate: (edit) =>
      mutate((d) => {
        if (!v.local) remember(d, v);
        edit(d);
      }),
    gate: () => authDialog(),
    openAuthor,
  });
}
function share(v) {
  const url = new URL(location.href);
  url.hash = "/video/remote/" + v.id;
  const link = el("input", {
    readonly: true,
    value: url.href,
    "aria-label": t("share"),
  });
  modal(
    t("share"),
    el(
      "div",
      { class: "stack" },
      link,
      button(t("share"), () => copy(url.href), "primary"),
    ),
  );
}
function toggleVideo(v, key) {
  gate(async () => {
    try {
      await mutate((d) => {
        if (!v.local) remember(d, v);
        toggle(d[key], v.id);
      });
    } catch (e) {
      notice(t(e.message));
    }
  });
}
function follow(author) {
  gate(async () => {
    try {
      await mutate((d) => {
        toggle(d.following, author.id);
        d.authors = [author, ...d.authors.filter((a) => a.id !== author.id)];
      });
      renderChrome();
      document
        .querySelectorAll('[data-follow="' + author.id + '"]')
        .forEach((b) => {
          b.textContent = t(
            store.data.following.includes(author.id) ? "unfollow" : "follow",
          );
        });
    } catch (e) {
      notice(t(e.message));
    }
  });
}
function videoCard(v) {
  const capturedStore = () => store;
  let recorded = null;
  const player = createPlayer(v, {
    economy,
    onPlay: () => {
      const owner = capturedStore();
      if (!owner || recorded === owner) return;
      recorded = owner;
      if (v.local) {
        recordView(owner.account.id, v.id)
          .then((views) => {
            v.stats.views = views;
          })
          .catch((e) => {
            recorded = null;
            notice(t(e.message));
          });
        return;
      }
      owner
        .change((d) => {
          if (v.stats.views != null)
            v.stats.views = Math.max(0, Number(v.stats.views) || 0) + 1;
          remember(d, v);
          d.history = [
            { id: v.id, at: Date.now() },
            ...d.history.filter((x) => x.id !== v.id),
          ].slice(0, 1000);
        })
        .catch((e) => {
          recorded = null;
          notice(t(e.message));
        });
    },
  });
  const actions = el("div", { class: "video-actions" });
  const authorAvatar = button(
    "",
    () => (v.local ? go("#/profile") : openAuthor(v.author.id)),
    "action-author",
  );
  authorAvatar.append(portrait(v.author));
  const authorAction = el("div", { class: "action-author-wrap" }, authorAvatar);
  const followBadge = v.local
    ? null
    : button(
        store?.data.following.includes(v.author.id) ? "✓" : "+",
        () => follow(v.author),
        "author-follow-badge",
      );
  if (followBadge) {
    followBadge.setAttribute("aria-label", t("follow"));
    authorAction.append(followBadge);
  }
  const caption = el("p", { class: "caption" }, v.description);
  const more = button(
    t("details"),
    () => {
      caption.classList.toggle("expanded");
      more.textContent = t(
        caption.classList.contains("expanded") ? "less" : "details",
      );
    },
    "caption-more",
  );
  const authorButton = button(
    "",
    () => openAuthor(v.author.id),
    "video-author",
  );
  authorButton.append(
    portrait(v.author),
    el("strong", {}, "@" + v.author.nickname),
  );
  const info = el(
    "div",
    { class: "video-info" },
    authorButton,
    caption,
    v.description.length > 120 ? more : null,
    v.music ? el("p", { class: "music" }, "♫  " + v.music) : null,
  );
  const canvas = el("div", { class: "video-canvas" }, player.node, info);
  const card = el("div", { class: "video-card" }, canvas, actions);
  function action(name, key, onClick, count, pressed, displayKey = key) {
    const b = iconButton(
      name,
      t(key),
      onClick,
      "action-button" + (pressed ? " selected" : ""),
    );
    if (pressed != null) b.setAttribute("aria-pressed", String(pressed));
    b.append(el("span", {}, count == null ? t(displayKey) : number(count)));
    return b;
  }
  function refresh() {
    const d = store?.data;
    if (followBadge) {
      const following = !!d?.following.includes(v.author.id);
      followBadge.textContent = following ? "✓" : "+";
      followBadge.setAttribute("aria-label", t(following ? "unfollow" : "follow"));
    }
    const menuAction = () => {
      const hide = async (kind) => {
        try {
          await mutate((state) => {
            remember(state, v);
            const id = kind === "hidden" ? v.id : v.author.id;
            if (!state[kind].includes(id)) state[kind].push(id);
          });
          menu.close();
          route();
        } catch (e) {
          notice(t(e.message));
        }
      };
      const menu = modal(t("details"), el("div", { class: "stack" }));
      const content = menu.querySelector(".stack");
      if (v.local) {
        content.append(
          button(t("delete"), () =>
            confirmAction(t("deleteVideo"), t("deleteVideoText"), async () => {
              await removeVideo(account.id, v.id);
              menu.close();
              go("#/profile?tab=myVideos");
            }),
          ),
        );
      } else {
        content.append(
          button(t("notInterested"), () => gate(() => hide("hidden"))),
          button(t("hideAuthor"), () => gate(() => hide("hiddenAuthors"))),
        );
      }
    };
    actions.replaceChildren(
      authorAction,
      action(
        "heart",
        d?.likes.includes(v.id) ? "unlike" : "like",
        () => toggleVideo(v, "likes"),
        v.stats.likes == null
          ? null
          : v.stats.likes + (d?.likes.includes(v.id) ? 1 : 0),
        !!d?.likes.includes(v.id),
      ),
      action(
        "comment",
        "comments",
        () => comments(v),
        v.stats.comments == null
          ? null
          : v.stats.comments +
              (d?.comments.filter((c) => c.videoId === v.id && !c.deleted)
                .length || 0),
      ),
      action(
        "bookmark",
        d?.saved.includes(v.id) ? "unsave" : "save",
        () => toggleVideo(v, "saved"),
        null,
        !!d?.saved.includes(v.id),
        "save",
      ),
      action("share", "share", () => share(v)),
      action("dots", "details", menuAction),
    );
  }
  refresh();
  return {
    node: card,
    activate: player.activate,
    deactivate: player.deactivate,
    destroy: player.destroy,
    toggle: player.toggle,
    toggleSound: player.toggleSound,
    refresh,
  };
}
const feedStates = new Map();
let mountedFeedKey = null;
function mountFeed(load) {
  mountedFeedKey = (account?.id || "guest") + location.hash;
  const previous = feedStates.get(mountedFeedKey);
  main.className = "main watching";
  const tabs = el(
    "div",
    { class: "feed-tabs" },
    el(
      "a",
      { href: "#/feed", class: location.hash === "#/feed" ? "active" : "" },
      t("forYou"),
    ),
    el(
      "a",
      {
        href: "#/following",
        class: location.hash === "#/following" ? "active" : "",
      },
      t("following"),
    ),
  );
  tabs.append(
    iconButton(
      "refresh",
      t("refreshFeed"),
      () => {
        feedStates.delete(mountedFeedKey);
        feed?.destroy();
        feed = null;
        route();
      },
      "feed-refresh",
    ),
  );
  const status = el("div", {
    class: "feed-status",
    role: "status",
    hidden: true,
  });
  feed = createFeed({
    load: previous?.load || load,
    initial: previous?.snapshot,
    onStale: (value) => {
      status.hidden = !value;
      status.textContent = t("stale");
    },
    renderCard: videoCard,
    filter: (v) =>
      !store?.data.hidden.includes(v.id) &&
      !store?.data.hiddenAuthors.includes(v.author.id),
  });
  const arrows = el(
    "div",
    { class: "feed-arrows" },
    iconButton("up", t("previous"), () => feed?.move(-1), "round"),
    iconButton("down", t("next"), () => feed?.move(1), "round"),
  );
  main.replaceChildren(
    tabs,
    status,
    feed.node,
    arrows,
    el("div", { class: "shortcut-hint" }, t("shortcuts")),
  );
  feed.start();
}
function appendTiles(container, items) {
  for (const v of items) {
    const a = el(
      "a",
      {
        href: v.local ? "#/video/local/" + v.id : "#/video/remote/" + v.id,
        class: "video-tile",
      },
      el("img", {
        src: v.poster || "favicon.svg",
        alt: "",
        loading: "lazy",
        onerror: (e) => {
          e.target.src = "favicon.svg";
        },
      }),
      el(
        "div",
        { class: "tile-shade" },
        el("small", {}, "▶ " + number(v.stats.views)),
        el("p", {}, v.description || "@" + v.author.nickname),
        el("span", {}, "@" + v.author.nickname),
      ),
    );
    container.append(a);
  }
}
function grid(items) {
  const container = el("div", { class: "video-grid" });
  appendTiles(container, items);
  return container;
}
function localVideo(record) {
  const source = URL.createObjectURL(record.file);
  return {
    id: record.id,
    local: true,
    description: record.description,
    poster: record.poster,
    sources: { hd: source },
    revokeSource: () => URL.revokeObjectURL(source),
    author: {
      id: account.id,
      name: store.data.profile.name || account.login,
      nickname: account.login,
      avatar: store.data.profile.avatar,
    },
    stats: { likes: 0, comments: 0, views: Math.max(0, Number(record.views) || 0) },
    publishedAt: new Date(record.createdAt).toISOString(),
    music: "",
  };
}
function uploadVideo() {
  if (!store) {
    authDialog(uploadVideo);
    return;
  }
  const owner = store;
  const file = el("input", {
    id: "video-file-" + crypto.randomUUID(),
    type: "file",
    accept: "video/*",
    required: true,
  });
  const fileName = el("small", { class: "file-picker-name" }, t("noFile"));
  const filePicker = el(
    "label",
    { class: "file-picker", for: file.id },
    el(
      "span",
      { class: "file-picker-button" },
      icon("play"),
      el("span", {}, t("chooseVideo")),
    ),
    fileName,
    file,
  );
  file.onchange = () => {
    fileName.textContent = file.files[0]?.name || t("noFile");
  };
  const description = el("textarea", { rows: 4, maxlength: 5000 });
  const error = el("p", { class: "error", role: "alert" });
  const submit = button(t("publish"), () => {}, "primary");
  submit.type = "submit";
  const form = el(
    "form",
    {
      class: "stack upload-form",
      onsubmit: async (event) => {
        event.preventDefault();
        if (!file.files[0]) {
          error.textContent = t("videoError");
          return;
        }
        submit.disabled = true;
        submit.replaceChildren(...loadingLabel());
        try {
          if (store !== owner) throw new Error("sessionExpired");
          await saveVideo(owner.account.id, file.files[0], description.value);
          dialog.close();
          notice(t("uploaded"));
          profilePage();
        } catch (e) {
          error.textContent = t(e.message);
        } finally {
          submit.disabled = false;
          submit.textContent = t("publish");
        }
      },
    },
    el("p", { class: "upload-hint" }, t("uploadHint")),
    field(t("selectVideo"), filePicker),
    field(t("description"), description),
    error,
    submit,
  );
  const dialog = modal(t("uploadVideo"), form);
}
async function renderUploads(target, owner) {
  try {
    const records = await videosFor(owner.account.id);
    if (!target.isConnected || store !== owner) return;
    target.replaceChildren(
      records.length
        ? grid(records.map(localVideo))
        : empty(
            t("noUploadedVideos"),
            t("noUploadedVideosHint"),
            null,
            "play",
          ),
    );
  } catch (e) {
    if (target.isConnected) target.replaceChildren(el("p", { class: "error" }, t(e.message)));
  }
}
async function localVideoPage(id, signal) {
  if (!store) throw new Error("sessionExpired");
  const record = await openUpload(account.id, id);
  if (signal.aborted) return;
  if (!record) throw new Error("unavailable");
  const video = localVideo(record);
  mountFeed(async () => ({ items: [video], next: null }));
}
async function authorPage(id, signal) {
  const author = await data.getAuthor(id, signal);
  if (signal.aborted) return;
  const followButton = button(
    t(store?.data.following.includes(id) ? "unfollow" : "follow"),
    () => follow(author),
    "primary",
  );
  followButton.dataset.follow = id;
  const head = el(
    "header",
    { class: "profile-header" },
    portrait(author, "large"),
    el(
      "div",
      {},
      el("h1", {}, author.name),
      el("p", {}, "@" + author.nickname),
      el("p", {}, author.about),
      author.followers == null
        ? null
        : el("p", {}, number(author.followers) + " " + t("followers")),
      followButton,
    ),
  );
  const results = el("div"),
    more = button(t("more"), load, "load-button");
  let cursor = null,
    busy = false,
    done = false;
  const seen = new Set();
  main.replaceChildren(
    el("div", { class: "page-container" }, head, results, more),
  );
  async function load() {
    if (busy || done || signal.aborted) return;
    busy = true;
    more.disabled = true;
    more.replaceChildren(...loadingLabel());
    try {
      const result = await data.getAuthorVideos(id, cursor, signal);
      if (signal.aborted) return;
      const fresh = result.items.filter(
        (v) => !seen.has(v.id) && (seen.add(v.id), true),
      );
      results.append(grid(fresh));
      done = !result.next || result.next === cursor || !fresh.length;
      cursor = result.next;
      more.hidden = done;
      if (!seen.size) results.replaceChildren(empty());
    } catch (e) {
      if (!signal.aborted) notice(t(e.message));
    } finally {
      busy = false;
      more.disabled = false;
      if (!done) more.textContent = t("more");
    }
  }
  await load();
}
async function searchPage(query, signal) {
  const heading = el(
    "h1",
    {},
    query ? t("searchResults") + " · " + query : t("explore"),
  );
  const intro = el(
    "section",
    { class: "discover-banner" },
    el("small", {}, "MORROW / DISCOVER"),
    el("h1", {}, t("discover")),
    el("p", {}, t("discoverText")),
    button(t("start"), () => go("#/feed"), "primary"),
  );
  const results = el("div"),
    resultsGrid = grid([]),
    status = el("p", { role: "status" }),
    more = button(t("more"), load, "load-button");
  let page = 1,
    busy = false,
    done = false;
  const known = new Set();
  main.replaceChildren(
    el(
      "div",
      { class: "page-container" },
      query ? heading : intro,
      status,
      results,
      more,
    ),
  );
  results.append(resultsGrid);
  async function load() {
    if (busy || done || signal.aborted) return;
    busy = true;
    more.disabled = true;
    more.replaceChildren(...loadingLabel());
    try {
      let loaded = 0;
      while (!done && loaded < 3) {
        const result = query
          ? await data.search(query, page, signal)
          : await data.getFeed(page, signal);
        if (signal.aborted) return;
        const fresh = result.items.filter(
          (v) => !known.has(v.id) && (known.add(v.id), true),
        );
        appendTiles(resultsGrid, fresh);
        page++;
        loaded++;
        done = !result.next || !fresh.length;
      }
      more.hidden = done;
      if (!known.size) results.replaceChildren(empty());
    } catch (e) {
      if (signal.aborted) return;
      const local = data
        .cachedVideos()
        .filter(
          (v) =>
            !query ||
            (v.description + " " + v.author.nickname)
              .toLowerCase()
              .includes(query.toLowerCase()),
        );
      status.textContent = t(local.length ? "localSearch" : e.message);
      if (local.length) {
        results.replaceChildren(grid(local));
        more.hidden = true;
      } else more.textContent = t("retry");
    } finally {
      busy = false;
      more.disabled = false;
      if (!done && !status.textContent) more.textContent = t("more");
    }
  }
  main.addEventListener(
    "scroll",
    () => {
      if (main.scrollTop + main.clientHeight >= main.scrollHeight - 480)
        load();
    },
    { signal },
  );
  await load();
}
function profilePage() {
  if (!store) {
    main.replaceChildren(
      el(
        "div",
        { class: "page-container" },
        empty(t("profile"), t("emptyHint"), emptyActions(true)),
      ),
    );
    return;
  }
  const d = store.data;
  const selectedTab = new URLSearchParams(location.hash.split("?")[1] || "").get(
    "tab",
  );
  const tab = ["likes", "saved", "following", "history", "myVideos"].includes(
    selectedTab,
  )
    ? selectedTab
    : "likes";
  const head = el(
    "header",
    { class: "profile-header" },
    portrait(d.profile, "large"),
    el(
      "div",
      {},
      el("h1", {}, d.profile.name),
      el("p", {}, "@" + account.login),
      el("p", { class: "profile-followers" }, number(0) + " " + t("followers")),
      el("p", {}, d.profile.about),
      el(
        "div",
        { class: "actions profile-actions" },
        button(t("editProfile"), editProfile, "primary"),
        button(t("uploadVideo"), uploadVideo),
        button(t("settings"), () => go("#/settings")),
      ),
    ),
  );
  const tabs = el("div", { class: "profile-tabs", role: "tablist" });
  const content = el("div", { role: "tabpanel" });
  const uploads = el(
    "section",
    { class: "profile-uploads" },
    el("div", { class: "uploads-content" }, loadingIndicator()),
  );
  for (const key of [
    "likes",
    "saved",
    "following",
    "history",
    "myVideos",
  ]) {
    const tabIcons = {
      likes: "heart",
      saved: "bookmark",
      following: "users",
      history: "clock",
      myVideos: "play",
    };
    const b = button(
      "",
      () => {
        go("#/profile?tab=" + key);
      },
      tab === key ? "active" : "",
    );
    b.setAttribute("aria-label", t(key));
    b.title = t(key);
    b.append(icon(tabIcons[key]), el("span", { class: "profile-tab-label" }, t(key)));
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", String(tab === key));
    tabs.append(b);
  }
  if (tab === "myVideos") content.append(uploads);
  else if (tab === "following") {
    for (const id of d.following) {
      const a = d.authors.find((x) => x.id === id) || {
        id,
        name: t("unknownAuthor"),
      };
      content.append(
        el(
          "a",
          { class: "author-row", href: "#/author/remote/" + id },
          portrait(a),
          el("strong", {}, a.name),
        ),
      );
    }
  } else {
    const ids =
      tab === "history" ? d.history.map((x) => x.id) : d[tab].toReversed();
    const videos = ids
      .map((id) => d.videos.find((v) => v.id === id))
      .filter(Boolean);
    content.append(grid(videos));
    if (!videos.length) content.replaceChildren(empty());
  }
  if (!content.children.length) content.append(empty());
  main.replaceChildren(
    el("div", { class: "page-container" }, head, tabs, content),
  );
  if (tab === "myVideos")
    renderUploads(uploads.querySelector(".uploads-content"), store);
}
function editProfile() {
  const owner = store;
  const name = el("input", {
    value: owner.data.profile.name,
    maxlength: 80,
    required: true,
  }),
    about = el(
      "textarea",
      { maxlength: 2000, rows: 3 },
      owner.data.profile.about,
    );
  let photo = owner.data.profile.avatar;
  const preview = el("div", {}, portrait(owner.data.profile, "large")),
    file = el("input", {
      id: "profile-photo-" + crypto.randomUUID(),
      type: "file",
      accept: "image/png,image/jpeg,image/webp",
    }),
    fileName = el("small", { class: "file-picker-name" }, t("noFile")),
    filePicker = el(
      "label",
      { class: "file-picker", for: file.id },
      el(
        "span",
        { class: "file-picker-button" },
        icon("image"),
        el("span", {}, t("choosePhoto")),
      ),
      fileName,
      file,
    ),
    error = el("p", { class: "error", role: "alert" });
  let processing = false;
  file.onchange = async () => {
    if (!file.files[0]) return;
    processing = true;
    fileName.textContent = file.files[0].name;
    try {
      photo = await avatar(file.files[0]);
      preview.replaceChildren(
        portrait({ name: name.value, avatar: photo }, "large"),
      );
    } catch (e) {
      error.textContent = t(e.message);
    } finally {
      processing = false;
    }
  };
  const submit = button(t("save"), () => {}, "primary");
  submit.type = "submit";

  const dialog = modal(
    t("editProfile"),
    el(
      "form",
      {
        class: "stack",
        onsubmit: async (e) => {
          e.preventDefault();
          if (processing || submit.disabled) return;
          submit.disabled = true;
          try {
            if (store !== owner) throw new Error("sessionExpired");
            await mutate((d) => {
              d.profile = {
                name: name.value.trim() || account.login,
                about: about.value,
                avatar: photo,
              };
            });
            dialog.close();
            renderChrome();
            profilePage();
          } catch (e) {
            error.textContent = t(e.message);
          } finally {
            submit.disabled = false;
          }
        },
      },
      preview,
      el(
        "div",
        { class: "field" },
        el("span", {}, t("avatar")),
        filePicker,
      ),
      button(t("removeAvatar"), () => {
        photo = "";
        file.value = "";
        fileName.textContent = t("noFile");
        preview.replaceChildren(portrait({ name: name.value }, "large"));
      }),
      field(t("name"), name),
      field(t("about"), about),
      error,
      submit,
    ),
  );
}
function settingsPage() {
  const content = el(
    "div",
    { class: "settings-content" },
    el("h1", {}, t("settings")),
    field(t("language"), button(getLanguage() === "ru" ? "EN" : "RU", () => {
      setLanguage(getLanguage() === "ru" ? "en" : "ru");
      route();
    }, "language-button")),
    field(
      t("theme"),
      select(
        ["system", "dark", "light"].map((v) => [v, t(v)]),
        theme,
        (v) => {
          theme = v;
          applyTheme();
        },
      ),
    ),
  );
  if (store) {
    const owner = store;
    content.append(
      el("hr"),
      button(t("logout"), () =>
        confirmAction(
          t("logout"),
          t("logoutText"),
          async () => {
            await owner.queue;
            logout();
            account = null;
            store = null;
            for (const d of document.querySelectorAll("dialog")) d.close();
            renderChrome();
            go("#/feed");
          },
          "logout",
        ),
      ),
      button(
        t("deleteAccount"),
        () =>
          confirmAction(
            t("deleteAccount"),
            t("deleteAccountText"),
            async () => {
              await owner.queue;
              await removeAccount(owner.account.id);
              account = null;
              store = null;
              for (const d of document.querySelectorAll("dialog")) d.close();
              renderChrome();
              go("#/feed");
            },
          ),
        "danger",
      ),
    );
  } else content.append(button(t("login"), () => authDialog(), "primary"));
  main.replaceChildren(
    el("div", { class: "page-container settings-page" }, content),
  );
}
async function route() {
  const version = ++routeVersion;
  routeController?.abort();
  routeController = new AbortController();
  const signal = routeController.signal;
  if (feed && mountedFeedKey) {
    feedStates.set(mountedFeedKey, {
      snapshot: feed.snapshot(),
      load: feed.loader,
    });
    while (feedStates.size > 4)
      feedStates.delete(feedStates.keys().next().value);
  }
  feed?.destroy();
  feed = null;
  pauseAll();
  for (const d of document.querySelectorAll("dialog")) d.close();
  main.className = "main";
  main.replaceChildren(el("div", { class: "empty-state" }, loadingIndicator()));
  renderChrome();
  const hash = location.hash.slice(1) || "/feed";
  const [path, queryString] = hash.split("?");
  const parts = path.split("/").filter(Boolean);
  updateTitle();
  try {
    if (parts[0] === "feed" || !parts.length) {
      let page = 1;
      mountFeed(async (s) => {
        const result = await data.getFeed(page, s, page === 1);
        page++;
        return result;
      });
    } else if (
      parts[0] === "video" &&
      parts[1] === "remote" &&
      validId(parts[2])
    ) {
      const v = await data.getVideo(parts[2], signal);
      if (signal.aborted) return;
      let first = true,
        page = 1;
      mountFeed(async (s) => {
        if (first) {
          first = false;
          return { items: [v], next: true };
        }
        const result = await data.getFeed(page, s);
        page++;
        return result;
      });
    } else if (
      parts[0] === "video" &&
      parts[1] === "local" &&
      validId(parts[2])
    )
      await localVideoPage(parts[2], signal);
    else if (
      parts[0] === "author" &&
      parts[1] === "remote" &&
      validId(parts[2])
    )
      await authorPage(parts[2], signal);
    else if (parts[0] === "following") {
      if (!store || !store.data.following.length) {
        main.replaceChildren(
          el(
            "div",
            { class: "page-container" },
            empty(t("noFollowing"), t("emptyHint"), emptyActions(!store)),
          ),
        );
        return;
      }
      const authors = store.data.following.slice(),
        cursors = new Map();
      let turn = 0;
      mountFeed(async (s) => {
        if (!authors.length) return { items: [], next: null };
        const id = authors[turn % authors.length];
        const result = await data.getAuthorVideos(id, cursors.get(id), s);
        if (!result.next || result.next === cursors.get(id))
          authors.splice(turn % authors.length, 1);
        else {
          cursors.set(id, result.next);
          turn++;
        }
        return { ...result, next: authors.length > 0 };
      });
    } else if (parts[0] === "profile") profilePage();
    else if (parts[0] === "settings") settingsPage();
    else if (parts[0] === "explore" || parts[0] === "search")
      await searchPage(
        new URLSearchParams(queryString).get("q")?.slice(0, 120) || "",
        signal,
      );
    else main.replaceChildren(empty());
  } catch (e) {
    if (version === routeVersion && !signal.aborted)
      main.replaceChildren(errorView(e, route));
  }
}
window.addEventListener("hashchange", route);
window.addEventListener("pagehide", () => pauseAll());
renderChrome();
try {
  account = await restoreSession();
  if (account) {
    store = await new ProfileStore(account).load();
    economy = store.data.economy;
  }
} catch (e) {
  account = null;
  store = null;
  notice(t(e.message));
}
if (!location.hash) history.replaceState(null, "", "#/feed");
route();
