import * as store from "./fokus-store.js";
import { authenticate } from "./fokus-auth.js";
import {
  load,
  sections,
  codes,
  escape as e,
  articleRef,
  route,
  unique,
} from "./fokus-data.js";
import { t, lang, setLanguage, date } from "./fokus-i18n.js";
import {
  icon,
  button,
  state,
  card,
  stale,
  avatar,
  toast,
  modal,
  closeModal,
  confirmAction,
} from "./fokus-ui.js";
let db = await store.read(),
  view = {},
  controller,
  token = 0,
  menu = false,
  query = "",
  theme = "light",
  reply = null,
  editing = "",
  pending = null;
let feed = [],
  home = [],
  known = new Map(),
  positions = new Map(),
  pageMemory = new Map(),
  lastHash = "",
  freshFeed = null;
try {
  theme = localStorage.getItem("fokus:theme") === "dark" ? "dark" : "light";
} catch {}
const me = () => store.account(db),
  main = document.querySelector("main");
const err = (error) =>
  t(
    [
      "network",
      "schema",
      "storage",
      "rateLimit",
      "invalid",
      "duplicate",
      "authError",
      "secureError",
      "avatarError",
    ].includes(error?.message)
      ? error.message
      : "network",
  );
const key = (a) => `article:${a.id}`;
function remember(items) {
  for (const a of items || []) known.set(a.id, { ...known.get(a.id), ...a });
}
function accountGate(fn) {
  if (me()) return fn();
  pending = fn;
  authModal();
}
function header() {
  document.querySelector("#header").innerHTML =
    `<div class="masthead wrap"><a class="brand" href="#/home" aria-label="Fokus">Fokus<span class="brand-dot"></span></a><span class="tagline">${e(t("tagline"))}</span><form id="search" role="search"><label class="sr-only" for="search-input">${e(t("search"))}</label>${icon("search")}<input id="search-input" name="q" type="search" placeholder="${e(t("search"))}" value="${e(query)}" maxlength="120" autocomplete="off"><button type="submit" aria-label="${e(t("searchAction"))}">${icon("arrow")}</button></form><div class="header-actions">${button("language", lang === "ru" ? "EN" : "RU", "", "language-button", `aria-label="${lang === "ru" ? "English" : "Русский"}"`)}${button("theme", t(theme === "dark" ? "light" : "dark"), theme === "dark" ? "sun" : "moon", "icon-button")}${me() ? `<a class="account-link" href="#/profile" aria-label="${e(t("profile"))}">${avatar(me())}<span>${e(me().name)}</span></a>` : button("auth", t("login"), "user", "login-button")}${button("menu", t("menu"), "menu", "icon-button menu-toggle", `aria-expanded="${menu}" aria-controls="navigation"`)}</div></div><div class="nav-border"><nav id="navigation" class="wrap ${menu ? "open" : ""}" aria-label="${e(t("menu"))}">${["home", ...sections, "rooms"].map((s) => `<a href="#/${s === "home" || s === "rooms" ? s : "section/" + s}" ${view.kind === s || view.section === s ? 'aria-current="page"' : ""}>${e(t(s))}</a>`).join("")}<a class="nav-saved" href="#/saved">${icon("bookmark")}${e(t("saved"))}</a></nav></div>`;
  document.documentElement.dataset.theme = theme;
  document.documentElement.lang = lang;
  document.querySelector(".skip-link").textContent = t("skip");
  document.querySelector("#footer").innerHTML =
    `<div class="wrap"><a class="brand" href="#/home">Fokus<span class="brand-dot"></span></a><span>${e(t("footer"))}</span><a href="https://ria.ru/" target="_blank" rel="noopener noreferrer">${e(t("source"))} ↗</a></div>`;
}
function pageHead(title, subtitle = "", refresh = true) {
  return `<div class="page-head"><div><div class="eyebrow">${e(t("edition"))}</div><h1>${e(title)}</h1>${subtitle ? `<p class="muted">${e(subtitle)}</p>` : ""}</div>${refresh ? button("refresh", t("refresh"), "refresh", "secondary") : ""}</div>`;
}
function latestList(items) {
  return `<aside class="latest-panel"><div class="section-title"><h2>${e(t("latest"))}</h2><span class="live-dot"></span></div>${items
    .slice(0, 10)
    .map(
      (a) =>
        `<a class="latest-item" href="${e(route(a))}"><time>${e(date(a.published))}</time><h3>${e(a.title)}</h3></a>`,
    )
    .join(
      "",
    )}<a class="text-link" href="#/latest">${e(t("allNews"))}${icon("arrow")}</a></aside>`;
}
function homeView() {
  const lead = home.length ? home : feed;
  return `${pageHead(t("home"), new Intl.DateTimeFormat(lang === "ru" ? "ru-RU" : "en-GB", { weekday: "long", day: "numeric", month: "long" }).format(new Date()))}${freshFeed ? `<div class="new-stories">${e(t("newsAvailable"))}${button("show-news", t("showNews"), "arrow", "text-button")}</div>` : ""}${stale(view.data)}${stale(view.homeData)}${view.error ? state(err(view.error), "refresh") : ""}${
    lead.length
      ? `<div class="home-layout"><section class="editorial"><div class="section-title"><h2>${e(t(home.length ? "editorial" : "latest"))}</h2></div>${card(lead[0], 0, "lead-story")}<div class="story-grid">${lead
          .slice(1, 5)
          .map((a, i) => card(a, i + 1))
          .join(
            "",
          )}</div></section>${latestList(feed)}</div><section class="more-news"><div class="section-title"><h2>${e(t("allNews"))}</h2><a class="text-link" href="#/latest">${e(t("more"))}${icon("arrow")}</a></div><div class="news-grid">${feed
          .filter((a) => !lead.slice(0, 5).some((b) => b.id === a.id))
          .slice(0, 9)
          .map((a, i) => card(a, i))
          .join("")}</div></section>`
      : view.loading
        ? state(t("loading"))
        : ""
  }`;
}
function listing(items, title, subtitle = "", canMore = false) {
  return `${pageHead(title, subtitle, !["saved", "history", "search", "profile"].includes(view.kind))}${stale(view.data)}${view.error ? state(err(view.error), "refresh") : ""}${items.length ? `<div class="news-grid">${items.map((a, i) => card(a, i, view.kind === "rooms" ? "card room-card" : "card")).join("")}</div>` : view.loading ? state(t("loading")) : !view.error ? state(t(view.kind === "search" ? "searchEmpty" : view.kind === "saved" ? "emptySaved" : view.kind === "history" ? "emptyHistory" : "empty")) : ""}${canMore ? `<div class="load-more">${button("more", t(view.moreLoading ? "loading" : "more"), "arrow", "secondary", view.moreLoading ? "disabled" : "")}</div>` : ""}`;
}
function personal(kind) {
  if (!me())
    return `${pageHead(t(kind), "", false)}<div class="account-gate">${icon("bookmark")}<h2>${e(t("localAccount"))}</h2><p>${e(t("accountHint"))}</p>${button("auth", t("login"), "user", "primary")}</div>`;
  const items = db[kind === "saved" ? "bookmarks" : "history"]
    .filter((b) => b.accountId === me().id)
    .map((b) => b.article);
  remember(items);
  return listing(items, t(kind));
}
function reactionPanel() {
  const a = view.article;
  if (!a) return "";
  const target = key(a),
    counts = view.reactions?.counts,
    own = db.reactions.find(
      (r) => r.accountId === me()?.id && r.target === target,
    );
  return `<div class="section-title"><h2>${e(t("reactions"))}</h2>${button("reactions-refresh", t("reactionsRefresh"), "refresh", "icon-button")}</div>${stale(view.reactions)}${view.reactionError ? `<p class="error-text">${e(err(view.reactionError))}</p>` : ""}<div class="reaction-grid">${codes
    .map((code, i) => {
      const local = db.reactions.filter(
        (r) => r.target === target && r.code === code,
      ).length;
      return `<button class="reaction ${own?.code === code ? "selected" : ""}" data-action="react" data-code="${code}" aria-pressed="${own?.code === code}" title="${e(t(code))}${local ? ` · +${local} ${e(t("localCount"))}` : ""}"><span class="reaction-face" aria-hidden="true">${["👍", "👎", "😄", "😮", "😔", "😠"][i]}</span><span>${e(t(code))}</span><strong>${counts ? counts[code] + local : local ? `— +${local}` : "—"}</strong></button>`;
    })
    .join("")}</div><p class="fine-print">${e(t("reactionHint"))}</p>`;
}
function localMessages() {
  return db.comments
    .filter((c) => c.articleId === view.article?.id)
    .map((c) => ({
      ...c,
      ref: `local:${c.id}`,
      name: db.accounts.find((a) => a.id === c.authorId)?.name || t("deleted"),
      local: true,
    }));
}
function message(c) {
  const current = me(),
    parent = c.parent;
  const target = c.ref,
    own = db.reactions.some(
      (r) =>
        r.target === target && r.accountId === current?.id && r.code === "s1",
    ),
    extra = db.reactions.filter(
      (r) => r.target === target && r.code === "s1",
    ).length;
  let parentText = parent?.text || "";
  if (parent?.ref?.startsWith("local:")) {
    const p = db.comments.find((x) => "local:" + x.id === parent.ref);
    parentText = p && !p.deleted ? p.text : t("parentMissing");
  }
  return `<article class="comment" data-comment="${e(c.ref)}"><div class="comment-avatar">${avatar({ name: c.name })}</div><div class="comment-content"><div class="comment-meta"><strong>${e(c.name)}</strong><span class="badge ${c.local ? "local-badge" : ""}">${e(t(c.local ? "local" : "external"))}</span><time>${e(date(c.at))}${c.edited ? " · " + e(t("edited")) : ""}</time></div>${parent ? `<blockquote class="parent-context"><strong>${e(parent.name)}</strong><p>${e(parentText).slice(0, 1000)}</p></blockquote>` : ""}<p class="comment-text">${e(c.deleted ? t("deleted") : c.text)}</p>${
    !c.deleted
      ? `<div class="comment-actions">${button("comment-like", c.local || c.counts ? String((c.counts?.s1 || 0) + extra) : extra ? `— +${extra}` : "—", "heart", "text-button " + (own ? "selected" : ""), `data-ref="${e(c.ref)}" aria-label="${e(t("s1"))}" aria-pressed="${own}"`)}${button("reply", t("reply"), "", "text-button", `data-ref="${e(c.ref)}"`)}${c.local && c.authorId === current?.id ? button("edit-comment", t("edit"), "", "text-button", `data-id="${e(c.id)}"`) + button("delete-comment", t("delete"), "", "text-button danger", `data-id="${e(c.id)}"`) : ""}${
          c.counts
            ? `<span class="other-reactions">${codes
                .filter((k) => k !== "s1" && c.counts[k] > 0)
                .map((k) => `${e(t(k))} ${c.counts[k]}`)
                .join(" · ")}</span>`
            : ""
        }</div>`
      : ""
  }</div></article>`;
}
function commentsPanel() {
  if (!view.article) return "";
  const list = view.comments?.items || [],
    locals = localMessages();
  return `<div class="section-title"><h2>${e(t("discussion"))}</h2>${button("comments-refresh", t("commentsRefresh"), "refresh", "icon-button")}</div><p class="fine-print">${e(t("loaded"))}: ${list.length} · ${e(t("local"))}: ${locals.filter((x) => !x.deleted).length}</p>${stale(view.comments)}${me() ? `<form id="comment-form"><label class="sr-only" for="comment-text">${e(t("write"))}</label>${reply ? `<div class="reply-target">${e(t("replyTo"))} ${e(reply.name)}${button("cancel-reply", t("cancel"), "close", "icon-button")}</div>` : ""}${editing ? `<div class="reply-target">${e(t("edit"))}${button("cancel-reply", t("cancel"), "close", "icon-button")}</div>` : ""}<textarea id="comment-text" name="text" placeholder="${e(t("write"))}" rows="3" maxlength="3000" required></textarea><div class="composer-footer"><p class="fine-print">${e(t("localHint"))}</p><button class="primary" type="submit">${e(t(editing ? "submit" : "publish"))}${icon("arrow")}</button></div><p class="form-error" role="alert"></p></form>` : `<div class="comment-gate">${button("auth", t("loginComment"), "user", "secondary")}<p class="fine-print">${e(t("localHint"))}</p></div>`}${view.commentsError ? state(err(view.commentsError), "comments-refresh") : !view.comments ? state(t("loading")) : !list.length && !locals.length ? state(t("commentsEmpty")) : ""}${locals.slice().reverse().map(message).join("")}${list.map(message).join("")}${view.comments?.cursor ? `<div class="load-more">${button("comments-more", t(view.commentsMore ? "loading" : "more"), "arrow", "secondary", view.commentsMore ? "disabled" : "")}</div>` : ""}`;
}
function articleView() {
  const a = view.article;
  if (!a)
    return `${pageHead(t("read"), " ", false)}${view.error ? state(err(view.error), "refresh") : state(t("loading"))}`;
  const saved = db.bookmarks.some(
    (b) => b.accountId === me()?.id && b.article.id === a.id,
  );
  return `<div class="article-layout"><article class="reader"><a class="back-link" href="#/home">${icon("back")}${e(t("back"))}</a>${stale(a)}<div class="eyebrow">${e(a.category || t("source"))}</div><h1>${e(a.title)}</h1><div class="article-meta"><span>${e(t("source"))}</span>${a.published ? `<time>${e(date(a.published))}</time>` : ""}${a.author ? `<span>${e(a.author)}</span>` : ""}</div><div class="article-tools">${button("save", t(saved ? "unsave" : "save"), saved ? "check" : "bookmark", "secondary", `aria-pressed="${saved}"`)}${button("share", t("share"), "share", "secondary")}<a class="text-link" href="${e(a.url)}" target="_blank" rel="noopener noreferrer">${e(t("original"))} ↗</a></div>${a.image ? `<figure class="article-cover"><img src="${e(a.image)}" alt="${e(a.title)}">${a.caption ? `<figcaption>${e(a.caption)}</figcaption>` : ""}</figure>` : ""}<div class="article-body">${a.blocks.map((b) => (b.type === "text" ? `<div class="paragraph">${b.html}</div>` : b.type === "quote" ? `<blockquote>${b.html}</blockquote>` : b.type === "image" ? `<figure><img src="${e(b.src)}" alt="" loading="lazy"><figcaption>${e(b.caption)}</figcaption></figure>` : `<p class="notice">${e(t("unsupported"))}</p>`)).join("")}</div><section class="reactions-panel" id="reactions">${reactionPanel()}</section><section class="comments-panel" id="discussion">${commentsPanel()}</section>${a.related.length ? `<section class="related"><div class="section-title"><h2>${e(t("related"))}</h2></div><div class="story-grid">${a.related.map((a, i) => card(a, i)).join("")}</div></section>` : ""}</article><aside class="article-aside"><div class="aside-note"><span class="eyebrow">Fokus</span><h2>${e(t("tagline"))}</h2><a href="#discussion" data-action="jump-comments" class="text-link">${e(t("discussion"))}${icon("chat")}</a></div>${latestList(feed.filter((x) => x.id !== a.id).slice(0, 5))}</aside></div>`;
}
function profileView() {
  if (!me()) return personal("profile");
  const a = me(),
    tab = view.tab || "settings";
  return `${pageHead(t("profile"), "", false)}<div class="profile-heading">${avatar(a)}<div><h2>${e(a.name)}</h2><span class="muted">@${e(a.login)}</span></div>${button("logout", t("logout"), "", "secondary")}</div><nav class="profile-tabs">${["settings", "saved", "history", "ownComments", "ownReactions"].map((x) => `<a href="#/profile/${x}" ${tab === x ? 'aria-current="page"' : ""}>${e(t(x))}</a>`).join("")}</nav>${
    tab === "settings"
      ? `<div class="settings-card"><h2>${e(t("settings"))}</h2><p class="muted">${e(t("accountHint"))}</p><form id="profile-form"><label>${e(t("name"))}<input name="name" value="${e(a.name)}" required maxlength="80"></label><div class="avatar-tools"><label class="secondary file-button">${icon("user")}${e(t("avatar"))}<input type="file" id="avatar-file" accept="image/png,image/jpeg,image/webp"></label>${a.avatar ? button("remove-avatar", t("removeAvatar"), "", "text-button") : ""}</div><p class="form-error" role="alert"></p><button class="primary" type="submit">${e(t("submit"))}</button></form><div class="danger-zone">${button("delete-account", t("deleteAccount"), "", "text-button danger")}</div></div>`
      : tab === "saved" || tab === "history"
        ? `<div class="news-grid">${
            db[tab === "saved" ? "bookmarks" : "history"]
              .filter((x) => x.accountId === a.id)
              .map((x, i) => card(x.article, i))
              .join("") ||
            state(t(tab === "saved" ? "emptySaved" : "emptyHistory"))
          }</div>`
        : tab === "ownComments"
          ? db.comments
              .filter((c) => c.authorId === a.id && !c.deleted)
              .map(
                (c) =>
                  `<article class="activity"><a href="${e(route(c.article))}">${e(c.article.title)}</a><p>${e(c.text)}</p><time>${e(date(c.at))}</time></article>`,
              )
              .join("") || state(t("empty"))
          : `<div class="news-grid">${
              db.reactions
                .filter((r) => r.accountId === a.id && r.article)
                .map((r, i) => card(r.article, i))
                .join("") || state(t("noReactions"))
            }</div>`
  }`;
}
function render(preserve = true) {
  const profileDraft = document.querySelector(
    "#profile-form [name=name]",
  )?.value;
  const draft = document.querySelector("#comment-text")?.value,
    focus = document.activeElement?.id,
    selection = document.activeElement?.selectionStart;
  header();
  let html = "";
  if (view.kind === "home") html = homeView();
  else if (view.kind === "article") html = articleView();
  else if (view.kind === "saved" || view.kind === "history")
    html = personal(view.kind);
  else if (view.kind === "profile") html = profileView();
  else if (view.kind === "search") {
    const items = unique([
      ...known.values(),
      ...db.bookmarks
        .filter((x) => x.accountId === me()?.id)
        .map((x) => x.article),
    ]).filter((a) =>
      a.title.toLocaleLowerCase().includes(query.toLocaleLowerCase()),
    );
    html = listing(query ? items : [], t("searchTitle"), t("searchHint"));
  } else
    html = listing(
      view.items || [],
      t(view.kind === "section" ? view.section : view.kind),
      "",
      !!view.cursor ||
        (view.kind === "latest" && (view.limit || 20) < feed.length),
    );
  main.innerHTML = `<div class="wrap content-wrap">${!store.available ? `<p class="notice">${e(t("storageHint"))}</p>` : ""}${html}</div>`;
  if (
    preserve &&
    profileDraft !== undefined &&
    document.querySelector("#profile-form [name=name]")
  )
    document.querySelector("#profile-form [name=name]").value = profileDraft;
  if (
    preserve &&
    draft !== undefined &&
    document.querySelector("#comment-text")
  )
    document.querySelector("#comment-text").value = draft;
  if (preserve && focus) {
    const el = document.getElementById(focus);
    el?.focus({ preventScroll: true });
    try {
      el?.setSelectionRange(selection, selection);
    } catch {}
  }
  document.title = view.article
    ? `${view.article.title} — Fokus`
    : `Fokus — ${t(view.section || view.kind || "home")}`;
}
async function sync() {
  db = await store.read();
  render();
}
async function social(kind, more = false) {
  if (!view.article || view[kind + "Busy"]) return;
  const current = token,
    ref = view.article,
    cursor = more ? view.comments?.cursor : "";
  if (more && !cursor) return;
  view[kind + "Busy"] = true;
  if (more) view.commentsMore = true;
  try {
    const data = await load(kind, {
      ref,
      cursor,
      signal: controller.signal,
      force: !more,
    });
    if (current !== token) return;
    if (kind === "comments") {
      const items = more
        ? uniqueComments([...(view.comments?.items || []), ...data.items])
        : data.items;
      view.comments = {
        ...data,
        items,
        cursor: data.cursor === cursor ? "" : data.cursor,
      };
      view.commentsError = null;
    } else {
      view.reactions = data;
      view.reactionError = null;
    }
  } catch (error) {
    if (current !== token) return;
    view[kind === "comments" ? "commentsError" : "reactionError"] = error;
  } finally {
    if (current === token) {
      view.commentsMore = false;
      view[kind + "Busy"] = false;
      render();
    }
  }
}
const uniqueComments = (items) => [
  ...new Map(items.map((x) => [x.id, x])).values(),
];
async function navigate(force = false) {
  const hash = location.hash || "#/home";
  if (lastHash && lastHash !== hash) {
    positions.set(lastHash, scrollY);
    if (["section", "latest"].includes(view.kind))
      pageMemory.set(lastHash, { ...view });
  }
  lastHash = hash;
  controller?.abort();
  controller = new AbortController();
  const current = ++token;
  menu = false;
  reply = null;
  editing = "";
  view = { kind: "home", loading: true };
  let path = hash.slice(2).split("/");
  if (path[0] === "article") {
    const ref = articleRef("/" + path.slice(1).join("/"));
    view = { kind: "article", ref, loading: true };
    if (!ref) {
      view.error = new Error("invalid");
      view.loading = false;
      render(false);
      return;
    }
  } else if (path[0] === "section" && sections.includes(path[1]))
    view = { kind: "section", section: path[1], loading: true, items: [] };
  else if (
    [
      "home",
      "latest",
      "rooms",
      "saved",
      "history",
      "profile",
      "search",
    ].includes(path[0])
  ) {
    view = { kind: path[0], loading: true, tab: path[1] };
    if (path[0] === "search")
      try {
        query = decodeURIComponent(path.slice(1).join("/"));
      } catch {
        query = "";
      }
  } else view = { kind: "home", loading: true };
  if (
    !force &&
    pageMemory.has(hash) &&
    ["section", "latest"].includes(view.kind)
  ) {
    view = { ...pageMemory.get(hash), loading: false, moreLoading: false };
    render(false);
    requestAnimationFrame(() => scrollTo(0, positions.get(hash) || 0));
    return;
  }
  render(false);
  window.scrollTo(0, 0);
  try {
    if (["home", "latest"].includes(view.kind)) {
      const results = await Promise.allSettled([
        load("feed", { signal: controller.signal, force }),
        load("home", { signal: controller.signal, force }),
      ]);
      if (current !== token) return;
      if (results[0].status === "fulfilled") {
        feed = results[0].value.items;
        remember(feed);
        view.data = results[0].value;
      } else view.error = results[0].reason;
      if (results[1].status === "fulfilled") {
        home = results[1].value.items;
        view.homeData = results[1].value;
        remember(home);
      }
      view.items = feed.slice(0, 20);
      view.limit = 20;
    } else if (view.kind === "section" || view.kind === "rooms") {
      const data = await load(view.kind, {
        section: view.section,
        signal: controller.signal,
        force,
      });
      if (current !== token) return;
      view.data = data;
      view.items = data.items;
      view.cursor = data.cursor;
      remember(data.items);
    } else if (view.kind === "article") {
      const data = await load("article", {
        ref: view.ref,
        signal: controller.signal,
        force,
      });
      if (current !== token) return;
      view.article = data;
      remember([data, ...data.related]);
      try {
        await store.visit(articleSummary(data));
        db = await store.read();
      } catch {}
      if (current !== token) return;
      social("dynamics");
      social("comments");
    }
  } catch (error) {
    if (current !== token) return;
    view.error = error;
  } finally {
    if (current === token) {
      view.loading = false;
      render(false);
      requestAnimationFrame(() => {
        scrollTo(0, positions.get(hash) || 0);
      });
    }
  }
}
function articleSummary(a) {
  return {
    id: a.id,
    date: a.date,
    path: a.path,
    url: a.url,
    title: a.title,
    image: a.image,
    published: a.published,
    category: a.category,
  };
}
function authModal(register = false) {
  modal(
    `<div class="eyebrow">Fokus</div><h2 id="dialog-title">${e(t(register ? "register" : "login"))}</h2><p class="muted">${e(t("accountHint"))}</p><form id="auth-form" data-register="${register}">${register ? `<label>${e(t("name"))}<input name="name" autocomplete="nickname" maxlength="80" required></label>` : ""}<label>${e(t("username"))}<input name="login" autocomplete="username" minlength="3" maxlength="40" required></label><label>${e(t("password"))}<input name="password" type="password" autocomplete="${register ? "new-password" : "current-password"}" minlength="8" maxlength="128" required></label><p class="fine-print">${e(t("registerHint"))}</p><p class="form-error" role="alert"></p><button class="primary" type="submit">${e(t(register ? "register" : "login"))}</button></form>${button(register ? "signin" : "register", t(register ? "haveAccount" : "noAccount"), "", "text-button auth-switch")}`,
  );
}
async function more() {
  if (view.moreLoading) return;
  view.moreLoading = true;
  render();
  const current = token;
  try {
    if (view.kind === "latest") {
      view.limit += 20;
      view.items = feed.slice(0, view.limit);
    } else if (view.cursor) {
      const previous = view.cursor,
        d = await load("section", {
          section: view.section,
          cursor: previous,
          signal: controller.signal,
        });
      if (current !== token) return;
      view.items = unique([...view.items, ...d.items]);
      view.cursor = d.cursor === previous ? "" : d.cursor;
      remember(d.items);
    }
  } catch (error) {
    if (current === token && error.name !== "AbortError") toast(err(error));
  } finally {
    if (current === token) {
      view.moreLoading = false;
      render();
    }
  }
}
document.addEventListener("click", async (event) => {
  const b = event.target.closest("[data-action]");
  if (!b) return;
  const action = b.dataset.action;
  event.preventDefault();
  try {
    if (action === "skip") {
      main.focus();
      main.scrollIntoView();
    } else if (action === "close") {
      pending = null;
      closeModal();
    } else if (action === "auth") authModal();
    else if (action === "register") authModal(true);
    else if (action === "signin") authModal();
    else if (action === "menu") {
      menu = !menu;
      header();
    } else if (action === "language") {
      setLanguage(lang === "ru" ? "en" : "ru");
      render();
    } else if (action === "theme") {
      theme = theme === "light" ? "dark" : "light";
      try {
        localStorage.setItem("fokus:theme", theme);
      } catch {}
      header();
    } else if (action === "refresh") {
      freshFeed = null;
      await navigate(true);
    } else if (action === "more") await more();
    else if (action === "comments-refresh") await social("comments");
    else if (action === "comments-more") {
      if (!view.commentsMore) await social("comments", true);
    } else if (action === "reactions-refresh") await social("dynamics");
    else if (action === "jump-comments")
      document
        .querySelector("#discussion")
        ?.scrollIntoView({ behavior: "smooth" });
    else if (action === "show-news") {
      feed = freshFeed.items;
      remember(feed);
      view.data = freshFeed;
      freshFeed = null;
      render();
    } else if (action === "save") {
      const a = articleSummary(view.article);
      await accountGate(async () => {
        await store.save(a);
        await sync();
      });
    } else if (action === "react") {
      const a = articleSummary(view.article),
        code = b.dataset.code;
      await accountGate(async () => {
        await store.react(key(a), code);
        await store.change((s) => {
          const r = s.reactions.find(
            (x) => x.accountId === store.session() && x.target === key(a),
          );
          if (r) r.article = a;
        });
        await sync();
      });
    } else if (action === "comment-like") {
      const ref = b.dataset.ref;
      await accountGate(async () => {
        await store.react(ref, "s1");
        await sync();
      });
    } else if (action === "reply") {
      const c = [...localMessages(), ...(view.comments?.items || [])].find(
        (x) => x.ref === b.dataset.ref,
      );
      if (c)
        await accountGate(() => {
          reply = { ref: c.ref, name: c.name, text: c.text.slice(0, 600) };
          editing = "";
          render();
          document.querySelector("#comment-text")?.focus();
        });
    } else if (action === "cancel-reply") {
      reply = null;
      editing = "";
      render();
      document.querySelector("#comment-text").value = "";
    } else if (action === "edit-comment") {
      const c = db.comments.find(
        (x) => x.id === b.dataset.id && x.authorId === me()?.id,
      );
      if (c) {
        editing = c.id;
        reply = null;
        render();
        document.querySelector("#comment-text").value = c.text;
        document.querySelector("#comment-text").focus();
      }
    } else if (action === "delete-comment") {
      if (await confirmAction(t("confirmDelete"))) {
        await store.removeComment(b.dataset.id);
        await sync();
      }
    } else if (action === "logout") {
      if (await confirmAction(t("confirmLogout"))) {
        store.signOut();
        reply = null;
        editing = "";
        await sync();
      }
    } else if (action === "delete-account") {
      if (await confirmAction(t("confirmAccount"), t("confirmAccountText"))) {
        await store.removeAccount();
        db = await store.read();
        location.hash = "#/home";
      }
    } else if (action === "remove-avatar") {
      await store.change((s) => {
        delete store.requireAccount(s).avatar;
      });
      await sync();
    } else if (action === "share") {
      const url = location.origin + location.pathname + route(view.article);
      try {
        await navigator.clipboard.writeText(url);
        toast(t("copied"));
      } catch {
        modal(
          `<h2 id="dialog-title">${e(t("copyLink"))}</h2><input value="${e(url)}" readonly>`,
        );
        document.querySelector("#modal input").select();
      }
    }
  } catch (error) {
    toast(err(error));
  }
});
document.addEventListener("submit", async (event) => {
  const form = event.target;
  if (
    !["search", "auth-form", "comment-form", "profile-form"].includes(form.id)
  )
    return;
  event.preventDefault();
  const data = new FormData(form),
    submit = form.querySelector('[type="submit"]');
  if (submit.disabled) return;
  submit.disabled = true;
  try {
    if (form.id === "search") {
      query = String(data.get("q")).trim();
      location.hash = "#/search/" + encodeURIComponent(query);
      if (view.kind === "search") render();
    } else if (form.id === "auth-form") {
      await authenticate(
        String(data.get("login")),
        String(data.get("password")),
        form.dataset.register === "true" ? String(data.get("name")) : undefined,
      );
      db = await store.read();
      closeModal();
      render();
      const fn = pending;
      pending = null;
      if (fn) await fn();
    } else if (form.id === "comment-form") {
      await store.comment(
        articleSummary(view.article),
        String(data.get("text")),
        reply,
        editing,
      );
      reply = null;
      editing = "";
      form.reset();
      await sync();
    } else if (form.id === "profile-form") {
      const name = String(data.get("name")).trim();
      if (!name) throw new Error("invalid");
      await store.change((s) => {
        store.requireAccount(s).name = name.slice(0, 80);
      });
      await sync();
      toast(t("done"));
    }
  } catch (error) {
    const target = form.querySelector(".form-error");
    if (target) target.textContent = err(error);
    else toast(err(error));
  } finally {
    submit.disabled = false;
  }
});
document.addEventListener("input", (event) => {
  if (event.target.id === "search-input") query = event.target.value;
});
document.addEventListener("change", async (event) => {
  if (event.target.id !== "avatar-file") return;
  const f = event.target.files?.[0];
  if (!f) return;
  try {
    if (!["image/jpeg", "image/png", "image/webp"].includes(f.type))
      throw new Error("avatarError");
    const bitmap = await createImageBitmap(f),
      canvas = document.createElement("canvas");
    canvas.width = canvas.height = 160;
    const ctx = canvas.getContext("2d"),
      size = Math.min(bitmap.width, bitmap.height);
    ctx.drawImage(
      bitmap,
      (bitmap.width - size) / 2,
      (bitmap.height - size) / 2,
      size,
      size,
      0,
      0,
      160,
      160,
    );
    bitmap.close();
    const data = canvas.toDataURL("image/jpeg", 0.85);
    await store.change((s) => {
      store.requireAccount(s).avatar = data;
    });
    await sync();
  } catch (error) {
    toast(err(error));
  }
});
document.addEventListener(
  "error",
  (event) => {
    if (event.target.tagName === "IMG") {
      event.target.classList.add("image-failed");
      event.target.removeAttribute("src");
      event.target.alt = t("imageUnavailable");
    }
  },
  true,
);
document.addEventListener("keydown", (event) => {
  if (
    (event.ctrlKey || event.metaKey) &&
    event.key === "k" &&
    !document.querySelector("#modal").open
  ) {
    event.preventDefault();
    document.querySelector("#search-input")?.focus();
  }
  if (event.key === "Escape" && menu) {
    menu = false;
    header();
    document.querySelector('[data-action="menu"]')?.focus();
  }
});
document.querySelector("#modal").addEventListener("click", (event) => {
  if (event.target.id === "modal") {
    const r = event.target.getBoundingClientRect();
    if (
      event.clientX < r.left ||
      event.clientX > r.right ||
      event.clientY < r.top ||
      event.clientY > r.bottom
    ) {
      pending = null;
      closeModal();
    }
  }
});
document
  .querySelector("#modal")
  .addEventListener("cancel", () => (pending = null));
store.channel?.addEventListener("message", () => sync());
window.addEventListener("hashchange", () => navigate());
window.addEventListener("pagehide", () => controller?.abort());
setInterval(async () => {
  if (view.kind !== "home" || document.hidden) return;
  const current = token;
  try {
    const d = await load("feed", { signal: controller.signal, force: true });
    if (current === token && d.items[0]?.id !== feed[0]?.id) {
      freshFeed = d;
      render();
    }
  } catch {}
}, 120000);
await navigate();
