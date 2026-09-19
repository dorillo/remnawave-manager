import * as store from "./fokus-store.js?v=20260919-release";
import { authenticate } from "./fokus-auth.js?v=20260919-release";
import {
  load,
  sections,
  codes,
  escape as e,
  articleRef,
  route,
  unique,
} from "./fokus-data.js?v=20260919-release";
import { t, lang, setLanguage, date } from "./fokus-i18n.js?v=20260919-release";
import {
  icon,
  button,
  state,
  card,
  newsGrid,
  stale,
  avatar,
  toast,
  modal,
  closeModal,
  confirmAction,
  cleanSourceMarkup,
  cleanSourceText,
} from "./fokus-ui.js?v=20260919-release";
let db = await store.read(),
  view = {},
  controller,
  token = 0,
  menu = false,
  query = "",
  theme = "light",
  pending = null,
  profileEdit = null;
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
    `<div class="masthead wrap"><a class="brand" href="#/home" aria-label="Fokus">Fokus<span class="brand-dot"></span></a><form id="search" role="search"><label class="sr-only" for="search-input">${e(t("search"))}</label>${icon("search")}<input id="search-input" name="q" type="search" placeholder="${e(t("search"))}" value="${e(query)}" maxlength="120" autocomplete="off"><button type="submit" aria-label="${e(t("searchAction"))}">${icon("arrow")}</button></form><div class="header-actions">${button("language", lang === "ru" ? "EN" : "RU", "", "language-button", `aria-label="${lang === "ru" ? "English" : "Русский"}"`)}${button("theme", t(theme === "dark" ? "light" : "dark"), theme === "dark" ? "sun" : "moon", "icon-button")}${me() ? `<a class="account-link" href="#/profile" aria-label="${e(t("profile"))}">${avatar(me())}<span>${e(me().name)}</span></a>` : button("auth", t("login"), "user", "login-button")}${button("menu", t("menu"), "menu", "icon-button menu-toggle", `aria-expanded="${menu}" aria-controls="mobile-navigation"`)}</div></div><div class="nav-border"><nav id="navigation" class="wrap ${menu ? "open" : ""}" aria-label="${e(t("menu"))}">${["home", ...sections].map((s) => `<a href="#/${s === "home" ? s : "section/" + s}" ${view.kind === s || view.section === s ? 'aria-current="page"' : ""}>${e(t(s))}</a>`).join("")}<a class="nav-saved" href="#/profile/saved">${icon("bookmark")}${e(t("saved"))}</a></nav></div>`;
  const drawer = document.querySelector("#mobile-navigation");
  const activeDrawerLink = drawer.contains(document.activeElement)
    ? document.activeElement.getAttribute("href")
    : null;
  drawer.setAttribute("aria-label", t("menu"));
  drawer.innerHTML = menu
    ? `<div class="drawer-heading"><span class="brand">Fokus</span>${button("menu-close", t("close"), "close", "icon-button")}</div><nav aria-label="${e(t("menu"))}">${document.querySelector("#navigation").innerHTML}</nav>`
    : "";
  if (menu && !drawer.open) drawer.showModal();
  if (menu && activeDrawerLink)
    [...drawer.querySelectorAll("a")]
      .find((a) => a.getAttribute("href") === activeDrawerLink)
      ?.focus();
  if (!menu && drawer.open) drawer.close();
  document.body.classList.toggle("mobile-nav-open", menu);
  document.documentElement.dataset.theme = theme;
  document.documentElement.lang = lang;
  document.querySelector(".skip-link").textContent = t("skip");
  document.querySelector("#footer").innerHTML =
    `<div class="wrap"><a class="brand" href="#/home">Fokus<span class="brand-dot"></span></a><span>${e(t("footer"))}</span></div>`;
}
function pageHead(title, subtitle = "", refresh = true) {
  return `<div class="page-head"><div><div class="eyebrow">${e(t("edition"))}</div><h1>${e(title)}</h1>${subtitle ? `<p class="muted">${e(subtitle)}</p>` : ""}</div>${refresh ? button("refresh", t("refresh"), "refresh", "secondary") : ""}</div>`;
}
function latestList(items) {
  return `<aside class="latest-panel"><div class="section-title"><h2>${e(t("latest"))}</h2><span class="live-dot"></span></div>${items
    .slice(0, 10)
    .map(
      (a) =>
        `<a class="latest-item" href="${e(route(a))}"><time>${e(date(a.published))}</time><h3>${e(cleanSourceText(a.title))}</h3></a>`,
    )
    .join(
      "",
    )}<a class="text-link" href="#/latest">${e(t("allNews"))}${icon("arrow")}</a></aside>`;
}
function homeView() {
  // Keep editorial choices first; fill missing slots from the current news feed.
  const editorialIds = new Set(home.map((a) => a.id));
  const lead = [...home, ...feed.filter((a) => !editorialIds.has(a.id))].slice(
    0,
    7,
  );
  const leadIds = new Set(lead.map((a) => a.id));
  return `${pageHead(t("home"), new Intl.DateTimeFormat(lang === "ru" ? "ru-RU" : "en-GB", { weekday: "long", day: "numeric", month: "long" }).format(new Date()))}${freshFeed ? `<div class="new-stories">${e(t("newsAvailable"))}${button("show-news", t("showNews"), "arrow", "text-button")}</div>` : ""}${stale(view.data)}${stale(view.homeData)}${view.error ? state(err(view.error), "refresh") : ""}${
    lead.length
      ? `<div class="home-layout"><section class="editorial"><div class="section-title"><h2>${e(t(home.length ? "editorial" : "latest"))}</h2></div>${card(lead[0], 0, "lead-story")}<div class="story-grid">${lead
          .slice(1)
          .map((a, i) => card(a, i + 1))
          .join(
            "",
          )}</div></section>${latestList(feed)}</div><section class="more-news"><div class="section-title"><h2>${e(t("allNews"))}</h2><a class="text-link" href="#/latest">${e(t("more"))}${icon("arrow")}</a></div>${newsGrid(
          feed.filter((a) => !leadIds.has(a.id)).slice(0, 12),
        )}</section>`
      : view.loading
        ? state(t("loading"))
        : ""
  }`;
}
function listing(items, title, subtitle = "", canMore = false) {
  return `${pageHead(title, subtitle, ["home", "section", "latest", "search"].includes(view.kind))}${stale(view.data)}${view.error ? state(err(view.error), "refresh") : ""}${items.length ? newsGrid(items) : view.loading ? state(t("loading")) : !view.error ? state(t(view.kind === "search" ? "searchEmpty" : view.kind === "saved" ? "emptySaved" : view.kind === "history" ? "emptyHistory" : "empty")) : ""}${canMore ? `<div class="load-more">${button("more", t(view.moreLoading ? "loading" : "more"), "arrow", "secondary", view.moreLoading ? "disabled" : "")}</div>` : ""}`;
}
function personal(kind) {
  if (!me())
    return `${pageHead(t(kind), "", false)}<div class="account-gate">${icon("bookmark")}<h2>${e(t("localAccount"))}</h2>${button("auth", t("login"), "user", "primary")}</div>`;
  const items = db[kind === "saved" ? "bookmarks" : "history"]
    .filter((b) => b.accountId === me().id)
    .map((b) => b.article);
  remember(items);
  return listing(items, t(kind));
}
function articleState(id) {
  if (!id || id === view.article?.id) return view;
  return view.discussions?.get(id);
}
function panelId(name, context) {
  return context === view ? name : `${name}-${context.article.id}`;
}
function commentInput(context) {
  return document.getElementById(panelId("comment-text", context));
}
function socialPanels(context) {
  return `<section class="reactions-panel" id="${panelId("reactions", context)}">${reactionPanel(context)}</section><section class="comments-panel" id="${panelId("discussion", context)}" aria-busy="${(!context.comments && !context.commentsError) || !!context.commentsBusy}">${commentsPanel(context)}</section>`;
}
function renderSocial(context, kind) {
  const panel = document.getElementById(
    panelId(kind === "comments" ? "discussion" : "reactions", context),
  );
  if (!panel) return;
  const input = commentInput(context),
    draft = input?.value;
  const focused = input && document.activeElement === input;
  const start = input?.selectionStart,
    end = input?.selectionEnd;
  panel.setAttribute("aria-busy", String(!!context[kind + "Busy"]));
  panel.innerHTML =
    kind === "comments" ? commentsPanel(context) : reactionPanel(context);
  const replacement = commentInput(context);
  if (replacement && draft !== undefined) replacement.value = draft;
  if (focused && replacement) {
    replacement.focus({ preventScroll: true });
    replacement.setSelectionRange(start, end);
  }
  observeNext();
}
function reactionPanel(context = view) {
  const a = context.article;
  if (!a) return "";
  const target = key(a),
    counts = context.reactions?.counts,
    own = db.reactions.find(
      (r) => r.accountId === me()?.id && r.target === target,
    );
  return `<div class="section-title"><h2>${e(t("reactions"))}</h2></div>${stale(context.reactions)}${context.reactionError ? `<p class="error-text">${e(err(context.reactionError))}</p>${button("reactions-refresh", t("retry"), "", "secondary", context.dynamicsBusy ? "disabled" : "")}` : ""}<div class="reaction-grid">${codes
    .map((code, i) => {
      const local = db.reactions.filter(
        (r) => r.target === target && r.code === code,
      ).length;
      return `<button class="reaction ${own?.code === code ? "selected" : ""}" data-action="react" data-code="${code}" aria-pressed="${own?.code === code}" title="${e(t(code))}"><span class="reaction-face" aria-hidden="true">${["👍", "👎", "😄", "😮", "😔", "😠"][i]}</span><span>${e(t(code))}</span><strong>${counts ? counts[code] + local : local ? `— +${local}` : "—"}</strong></button>`;
    })
    .join("")}</div>`;
}
function localMessages(context = view) {
  return db.comments
    .filter((c) => c.articleId === context.article?.id)
    .map((c) => ({
      ...c,
      ref: `local:${c.id}`,
      name: db.accounts.find((a) => a.id === c.authorId)?.name || t("deleted"),
      local: true,
    }));
}
function message(c) {
  if (!c.local)
    c = { ...c, name: cleanSourceText(c.name), text: cleanSourceText(c.text) };

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
  const sourceCount = Number(c.counts?.s1);
  const hasCount =
    c.local ||
    (c.counts?.s1 != null &&
      Number.isSafeInteger(sourceCount) &&
      sourceCount >= 0);
  const likes = (hasCount && !c.local ? sourceCount : 0) + extra;
  const likeLabel = `${t(own ? "unlike" : "s1")}: ${likes}${hasCount ? "" : `. ${t("commentLikesUnknown")}`}`;
  let parentText = parent?.text || "";
  if (parent?.ref?.startsWith("local:")) {
    const p = db.comments.find((x) => "local:" + x.id === parent.ref);
    parentText = p && !p.deleted ? p.text : t("parentMissing");
  }
  return `<article class="comment" data-comment="${e(c.ref)}"><div class="comment-avatar">${avatar({ name: c.name })}</div><div class="comment-content"><div class="comment-meta"><strong>${e(c.name)}</strong><time>${e(date(c.at))}${c.edited ? " · " + e(t("edited")) : ""}</time></div>${parent ? `<blockquote class="parent-context"><strong>${e(cleanSourceText(parent.name))}</strong><p>${e(cleanSourceText(parentText.slice(0, 1000)))}</p></blockquote>` : ""}<p class="comment-text">${e(c.deleted ? t("deleted") : c.text)}</p>${
    !c.deleted
      ? `<div class="comment-actions">${button("comment-like", String(likes), "heart", "text-button " + (own ? "selected" : ""), `data-ref="${e(c.ref)}" aria-label="${e(likeLabel)}" title="${e(likeLabel)}" aria-pressed="${own}"`)}${button("reply", t("reply"), "", "text-button", `data-ref="${e(c.ref)}"`)}${c.local && c.authorId === current?.id ? button("edit-comment", t("edit"), "", "text-button", `data-id="${e(c.id)}"`) + button("delete-comment", t("delete"), "", "text-button danger", `data-id="${e(c.id)}"`) : ""}${
          c.counts
            ? `<span class="other-reactions">${codes
                .filter((k) => k !== "s1" && k !== "s6" && c.counts[k] > 0)
                .map((k) => `${e(t(k))} ${c.counts[k]}`)
                .join(" · ")}</span>`
            : ""
        }</div>`
      : ""
  }</div></article>`;
}
function composer(context) {
  const { reply, editing } = context;
  return `<form class="comment-form" id="${panelId("comment-form", context)}" data-article="${e(context.article.id)}"><label class="sr-only" for="${panelId("comment-text", context)}">${e(t("write"))}</label>${reply ? `<div class="reply-target">${e(t("replyTo"))} ${e(cleanSourceText(reply.name))}${button("cancel-reply", t("cancel"), "close", "icon-button")}</div>` : ""}${editing ? `<div class="reply-target">${e(t("edit"))}${button("cancel-reply", t("cancel"), "close", "icon-button")}</div>` : ""}<textarea id="${panelId("comment-text", context)}" name="text" placeholder="${e(t("write"))}" rows="3" maxlength="3000" required></textarea><div class="composer-footer"><button class="primary" type="submit" ${context.commentSubmitting ? "disabled" : ""}>${e(t(editing ? "submit" : "publish"))}${icon("arrow")}</button></div><p class="form-error" role="alert"></p></form>`;
}
function threads(items, context) {
  const { reply, editing } = context;
  const byRef = new Map(items.map((c) => [c.ref, c]));
  const children = new Map();
  for (const c of items) {
    const parent = c.parent?.ref;
    if (parent && parent !== c.ref && byRef.has(parent)) {
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent).push(c);
    }
  }
  const roots = items.filter(
    (c) => !byRef.has(c.parent?.ref) || c.parent?.ref === c.ref,
  );
  const seen = new Set(),
    result = [];
  const target = reply?.ref || (editing ? `local:${editing}` : "");
  // Include disconnected/cyclic imported threads exactly once.
  for (const root of [...roots, ...items]) {
    const stack = [{ c: root, depth: 0 }];
    while (stack.length) {
      const { c, depth } = stack.pop();
      if (seen.has(c.ref)) continue;
      seen.add(c.ref);
      result.push(
        `<div class="comment-thread depth-${Math.min(depth, 3)}">${message(c)}${me() && target === c.ref ? composer(context) : ""}</div>`,
      );
      const replies = children.get(c.ref) || [];
      for (let i = replies.length - 1; i >= 0; i--)
        stack.push({ c: replies[i], depth: depth + 1 });
    }
  }
  return result.join("");
}
function commentsPanel(context = view) {
  const { reply, editing } = context;
  if (!context.article) return "";
  const list = context.comments?.items || [],
    locals = localMessages(context),
    all = [...locals.slice().reverse(), ...list],
    inlineTarget = all.some(
      (c) => c.ref === (reply?.ref || (editing ? `local:${editing}` : "")),
    );
  return `<div class="section-title"><h2>${e(t("discussion"))}</h2></div><p class="fine-print">${e(t("loaded"))}: ${list.length + locals.length}</p>${stale(context.comments)}${me() && !inlineTarget ? composer(context) : me() ? "" : `<div class="comment-gate">${button("auth", t("loginComment"), "user", "secondary")}</div>`}${context.commentsError ? state(err(context.commentsError), "comments-refresh") : !context.comments ? state(t("loading")) : !list.length && !locals.length ? state(t("commentsEmpty")) : ""}${threads(all, context)}${context.comments?.cursor ? `<div class="load-more">${button("comments-more", t(context.commentsMore ? "loading" : "more"), "arrow", "secondary", context.commentsMore ? "disabled" : "")}</div>` : ""}`;
}
function storyContent(a, continued = false) {
  const saved = db.bookmarks.some(
    (b) => b.accountId === me()?.id && b.article.id === a.id,
  );
  return cleanSourceMarkup(
    `${stale(a)}<div class="eyebrow">${e(a.category || t("source"))}</div><${continued ? "h2" : "h1"} class="article-title">${e(cleanSourceText(a.title))}</${continued ? "h2" : "h1"}><div class="article-meta">${a.published ? `<time>${e(date(a.published))}</time>` : ""}${a.author ? `<span>${e(a.author)}</span>` : ""}</div><div class="article-tools">${button("save", t(saved ? "unsave" : "save"), saved ? "check" : "bookmark", "secondary", `aria-pressed="${saved}" data-article="${e(a.id)}"`)}${button("share", t("share"), "share", "secondary", `data-article="${e(a.id)}"`)}</div>${a.image ? `<figure class="article-cover"><img src="${e(a.image)}" alt="${e(cleanSourceText(a.title))}">${a.caption ? `<figcaption>${e(a.caption)}</figcaption>` : ""}</figure>` : ""}<div class="article-body">${a.blocks.map((b) => (b.type === "text" ? `<div class="paragraph">${b.html}</div>` : b.type === "quote" ? `<blockquote>${b.html}</blockquote>` : b.type === "image" ? `<figure><img src="${e(b.src)}" alt="" loading="lazy"><figcaption>${e(b.caption)}</figcaption></figure>` : `<p class="notice">${e(t("unsupported"))}</p>`)).join("")}</div>`,
  );
}
function nextCandidate() {
  const articles = [view.article, ...(view.continued || [])].filter(Boolean);
  const seen = new Set(articles.map((a) => a.id));
  return unique([...(articles.at(-1)?.related || []), ...feed, ...home]).find(
    (a) => !seen.has(a.id),
  );
}
function continuationView(articles = view.continued || []) {
  return `${articles.map((a) => `<article class="reader continued-story" data-article-id="${e(a.id)}"><div class="next-story-heading"><span class="eyebrow">${e(t("nextArticle"))}</span><a class="text-link" href="${e(route(a))}">${e(t("openArticle"))}${icon("arrow")}</a></div>${storyContent(a, true)}${socialPanels(articleState(a.id))}</article>`).join("")}
    <div id="next-article" class="next-article" aria-busy="${!!view.nextBusy}">${view.nextError ? `<p role="status" class="error-text">${e(err(view.nextError))}</p>` : ""}${!view.nextDone ? button("next-article", t(view.nextBusy ? "loading" : view.nextError ? "retry" : "nextArticle"), "arrow", "secondary", view.nextBusy ? "disabled" : "") : `<a class="text-link" href="#/latest">${e(t("allNews"))}${icon("arrow")}</a>`}</div>`;
}
let nextObserver,
  nextVisible = false;
function observeNext() {
  nextObserver?.disconnect();
  nextVisible = false;
  const target = document.querySelector("#next-article");
  if (
    !target ||
    view.nextBusy ||
    view.nextError ||
    view.nextDone ||
    !("IntersectionObserver" in window)
  )
    return;
  const current = token;
  nextObserver = new IntersectionObserver((entries) => {
    if (current !== token) return;
    nextVisible = entries.some((x) => x.isIntersecting);
    if (nextVisible && scrollY > 0) nextArticle();
  });
  nextObserver.observe(target);
}
function renderContinuation() {
  const target = document.querySelector("#article-continuation");
  if (target) {
    const template = document.createElement("template");
    const existing = new Set(
      [...target.querySelectorAll("[data-article-id]")].map(
        (el) => el.dataset.articleId,
      ),
    );
    template.innerHTML = continuationView(
      (view.continued || []).filter((a) => !existing.has(a.id)),
    );
    const sentinel = target.querySelector("#next-article");
    for (const story of template.content.querySelectorAll("[data-article-id]"))
      if (!existing.has(story.dataset.articleId))
        target.insertBefore(story, sentinel);
    const next = template.content.querySelector("#next-article");
    if (sentinel) sentinel.replaceWith(next);
    else target.append(next);
  }
  observeNext();
}
window.addEventListener(
  "scroll",
  () => {
    if (nextVisible && scrollY > 0 && !view.nextError) nextArticle();
  },
  { passive: true },
);
async function nextArticle(manual = false) {
  if (
    view.kind !== "article" ||
    !view.article ||
    view.nextBusy ||
    view.nextDone
  )
    return;
  const current = token,
    signal = controller.signal;
  view.nextBusy = true;
  view.nextError = null;
  renderContinuation();
  try {
    if (!nextCandidate()) {
      const data = await load("feed", { signal });
      if (current !== token) return;
      feed = data.items;
      remember(feed);
    }
    const candidate = nextCandidate();
    if (!candidate) {
      view.nextDone = true;
      return;
    }
    const article = await load("article", { ref: candidate, signal });
    if (current !== token) return;
    const context = { article };
    (view.discussions ||= new Map()).set(article.id, context);
    (view.continued ||= []).push(article);
    renderContinuation();
    social("dynamics", false, context);
    social("comments", false, context);
    remember([article, ...article.related]);
    if (manual) view.focusNext = article.id;
  } catch (error) {
    if (current === token && error.name !== "AbortError")
      view.nextError = error;
  } finally {
    if (current === token) {
      view.nextBusy = false;
      renderContinuation();
      if (view.focusNext) {
        const heading = [...document.querySelectorAll(".continued-story")]
          .find((el) => el.dataset.articleId === view.focusNext)
          ?.querySelector(".article-title");
        if (heading) {
          heading.tabIndex = -1;
          heading.focus();
        }
        delete view.focusNext;
      }
    }
  }
}
function articleView() {
  const a = view.article;
  if (!a)
    return `${pageHead(t("read"), " ", false)}${view.error ? state(err(view.error), "refresh") : state(t("loading"))}`;
  return `<div class="article-layout"><div class="article-stream"><article class="reader" data-article-id="${e(a.id)}"><a class="back-link" href="#/home">${icon("back")}${e(t("back"))}</a>${storyContent(a)}${socialPanels(view)}</article><section id="article-continuation" aria-label="${e(t("nextArticle"))}">${continuationView()}</section></div><aside class="article-aside"><div class="article-aside-sticky">${latestList(feed.filter((x) => x.id !== a.id).slice(0, 5))}</div></aside></div>`;
}

function editProfile() {
  const a = me();
  if (!a) return;
  profileEdit = { accountId: a.id, avatar: a.avatar || "" };
  modal(
    `<h2 id="dialog-title">${e(t("editProfile"))}</h2><form id="profile-form"><label>${e(t("name"))}<input name="name" value="${e(a.name)}" required maxlength="80" autocomplete="nickname"></label><div id="profile-avatar-preview">${avatar(a)}</div><div class="avatar-tools"><label class="secondary file-button">${icon("user")}${e(t("avatar"))}<input type="file" id="avatar-file" accept="image/png,image/jpeg,image/webp"></label>${button("remove-avatar", t("removeAvatar"), "", "text-button", a.avatar ? "" : "hidden")}</div><p class="form-error" role="alert"></p><div class="dialog-actions">${button("close", t("cancel"), "", "secondary")}<button class="primary" type="submit">${e(t("submit"))}</button></div></form>`,
  );
}
function avatarPreview() {
  if (!profileEdit) return;
  document.querySelector("#profile-avatar-preview").innerHTML = avatar({
    name: me()?.name,
    avatar: profileEdit.avatar,
  });
  document.querySelector('[data-action="remove-avatar"]').hidden =
    !profileEdit.avatar;
}
function profileView() {
  if (!me()) return personal("profile");
  const a = me(),
    tab = view.tab || "settings";
  return `${pageHead(t("profile"), "", false)}<div class="profile-heading">${avatar(a)}<div><h2>${e(a.name)}</h2><span class="muted">@${e(a.login)}</span></div></div><nav class="profile-tabs">${["settings", "saved", "history", "ownComments", "ownReactions"].map((x) => `<a href="#/profile/${x}" ${tab === x ? 'aria-current="page"' : ""} aria-label="${e(t(x))}" title="${e(t(x))}">${icon({ settings: "user", saved: "bookmark", history: "clock", ownComments: "chat", ownReactions: "heart" }[x])}<span>${e(t(x))}</span></a>`).join("")}</nav>${
    tab === "settings"
      ? `<div class="settings-card"><h2>${e(t("settings"))}</h2>${button("edit-profile", t("edit"), "user", "primary")}<div class="profile-account-actions">${button("logout", t("logout"), "", "secondary")}${button("delete-account", t("deleteAccount"), "", "secondary danger")}</div></div>`
      : tab === "saved" || tab === "history"
        ? db[tab === "saved" ? "bookmarks" : "history"].some(
            (x) => x.accountId === a.id,
          )
          ? newsGrid(
              db[tab === "saved" ? "bookmarks" : "history"]
                .filter((x) => x.accountId === a.id)
                .map((x) => x.article),
            )
          : state(t(tab === "saved" ? "emptySaved" : "emptyHistory"))
        : tab === "ownComments"
          ? db.comments
              .filter((c) => c.authorId === a.id && !c.deleted)
              .map(
                (c) =>
                  `<article class="activity"><a href="${e(route(c.article))}">${e(cleanSourceText(c.article.title))}</a><p>${e(c.text)}</p><time>${e(date(c.at))}</time></article>`,
              )
              .join("") || state(t("empty"))
          : db.reactions.some((r) => r.accountId === a.id && r.article)
            ? newsGrid(
                unique(
                  db.reactions
                    .filter((r) => r.accountId === a.id && r.article)
                    .map((r) => r.article),
                ),
              )
            : state(t("noReactions"))
  }`;
}
function render(preserve = true) {
  const profileDraft = document.querySelector(
    "#profile-form [name=name]",
  )?.value;
  const drafts = [...document.querySelectorAll(".comment-form textarea")].map(
      (el) => [el.id, el.value],
    ),
    focus = document.activeElement?.id,
    selection = document.activeElement?.selectionStart;
  nextObserver?.disconnect();
  header();
  let html = "";
  if (view.kind === "home") html = homeView();
  else if (view.kind === "article") html = articleView();
  else if (view.kind === "saved" || view.kind === "history")
    html = personal(view.kind);
  else if (view.kind === "profile") html = profileView();
  else if (view.kind === "search") {
    html = listing(
      view.items || [],
      t("searchTitle"),
      `${t("searchHint")}${view.searchQuery ? ` · «${view.searchQuery}»` : ""}`,
      !!view.cursor,
    );
  } else
    html = listing(
      view.items || [],
      t(view.kind === "section" ? view.section : view.kind),
      "",
      !!view.cursor ||
        (view.kind === "latest" && (view.limit || 24) < feed.length),
    );
  main.innerHTML = `<div class="wrap content-wrap">${!store.available ? `<p class="notice">${e(t("storageHint"))}</p>` : ""}${html}</div>`;
  if (
    preserve &&
    profileDraft !== undefined &&
    document.querySelector("#profile-form [name=name]")
  )
    document.querySelector("#profile-form [name=name]").value = profileDraft;
  if (preserve)
    for (const [id, value] of drafts) {
      const input = document.getElementById(id);
      if (input) input.value = value;
    }
  if (preserve && focus) {
    const el = document.getElementById(focus);
    el?.focus({ preventScroll: true });
    try {
      el?.setSelectionRange(selection, selection);
    } catch {}
  }
  observeNext();
  document.title = view.article
    ? `${cleanSourceText(view.article.title)} — Fokus`
    : `Fokus — ${t(view.section || view.kind || "home")}`;
}
async function sync() {
  db = await store.read();
  const remaining = new Set(db.comments.map((c) => c.id));
  for (const context of [view, ...(view.discussions?.values() || [])]) {
    if (context.editing && !remaining.has(context.editing))
      context.editing = "";
    if (
      context.reply?.ref?.startsWith("local:") &&
      !remaining.has(context.reply.ref.slice(6))
    )
      context.reply = null;
  }
  render();
}
async function social(kind, more = false, context = view) {
  if (!context.article || context[kind + "Busy"]) return;
  const current = token,
    ref = context.article,
    cursor = more ? context.comments?.cursor : "";
  if (more && !cursor) return;
  context[kind + "Busy"] = true;
  if (more) context.commentsMore = true;
  renderSocial(context, kind);
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
        ? uniqueComments([...(context.comments?.items || []), ...data.items])
        : data.items;
      context.comments = {
        ...data,
        items,
        cursor:
          !data.items.length ||
          data.cursor === cursor ||
          (more && items.length === context.comments.items.length)
            ? ""
            : data.cursor,
      };
      context.commentsError = null;
    } else {
      context.reactions = data;
      context.reactionError = null;
    }
  } catch (error) {
    if (current !== token) return;
    context[kind === "comments" ? "commentsError" : "reactionError"] = error;
  } finally {
    if (current === token) {
      if (kind === "comments") context.commentsMore = false;
      context[kind + "Busy"] = false;
      renderSocial(context, kind);
    }
  }
}
const uniqueComments = (items) => [
  ...new Map(items.map((x) => [x.id, x])).values(),
];
async function navigate(force = false) {
  const hash = location.hash || "#/home";
  if (hash === "#/saved" || hash === "#/rooms") {
    location.replace(hash === "#/saved" ? "#/profile/saved" : "#/home");
    return;
  }
  if (profileEdit) {
    profileEdit = null;
    closeModal();
  }
  if (lastHash && lastHash !== hash) {
    positions.set(lastHash, scrollY);
    if (["section", "latest"].includes(view.kind))
      pageMemory.set(lastHash, { ...view });
  }
  lastHash = hash;
  nextObserver?.disconnect();
  controller?.abort();
  controller = new AbortController();
  const current = ++token;
  menu = false;
  pending = null;
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
    ["home", "latest", "saved", "history", "profile", "search"].includes(
      path[0],
    )
  ) {
    view = { kind: path[0], loading: true, tab: path[1] };
    if (path[0] === "search")
      try {
        query = decodeURIComponent(path.slice(1).join("/"))
          .trim()
          .slice(0, 120);
        view.searchQuery = query;
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
      view.items = feed.slice(0, 24);
      view.limit = 24;
    } else if (view.kind === "section" || (view.kind === "search" && query)) {
      const data = await load(view.kind, {
        section: view.section,
        query: view.searchQuery || query,
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
      if (!feed.length)
        load("feed", { signal: controller.signal })
          .then((result) => {
            if (current !== token) return;
            feed = result.items;
            remember(feed);
            render();
          })
          .catch(() => {});
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
    `<div class="eyebrow">Fokus</div><h2 id="dialog-title">${e(t(register ? "register" : "login"))}</h2><form id="auth-form" data-register="${register}">${register ? `<label>${e(t("name"))}<input name="name" autocomplete="nickname" maxlength="80" required></label>` : ""}<label>${e(t("username"))}<input name="login" autocomplete="username" minlength="3" maxlength="40" required></label><label>${e(t("password"))}<input name="password" type="password" autocomplete="${register ? "new-password" : "current-password"}" minlength="8" maxlength="128" required></label><p class="fine-print">${e(t("registerHint"))}</p><p class="form-error" role="alert"></p><button class="primary" type="submit">${e(t(register ? "register" : "login"))}</button></form>${button(register ? "signin" : "register", t(register ? "haveAccount" : "noAccount"), "", "secondary auth-switch")}`,
  );
}
async function more() {
  if (view.moreLoading) return;
  view.moreLoading = true;
  render();
  const current = token;
  try {
    if (view.kind === "latest") {
      view.limit += 24;
      view.items = feed.slice(0, view.limit);
    } else if (view.cursor) {
      const previous = view.cursor,
        d = await load(view.kind === "search" ? "search" : "section", {
          section: view.section,
          query: view.searchQuery,
          cursor: previous,
          signal: controller.signal,
        });
      if (current !== token) return;
      const previousLength = view.items.length;
      view.items = unique([...view.items, ...d.items]);
      view.cursor =
        view.items.length === previousLength || d.cursor === previous
          ? ""
          : d.cursor;
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
  const discussion = articleState(
    b.closest(".reader[data-article-id]")?.dataset.articleId,
  );
  event.preventDefault();
  try {
    if (action === "skip") {
      main.focus();
      main.scrollIntoView();
    } else if (action === "close") {
      pending = null;
      closeModal();
    } else if (action === "edit-profile") editProfile();
    else if (action === "auth") authModal();
    else if (action === "register") authModal(true);
    else if (action === "signin") authModal();
    else if (action === "menu") {
      menu = true;
      header();
    } else if (action === "menu-close") {
      closeNavigation();
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
    else if (action === "comments-refresh")
      await social("comments", false, discussion);
    else if (action === "comments-more") {
      if (!discussion.commentsMore) await social("comments", true, discussion);
    } else if (action === "reactions-refresh")
      await social("dynamics", false, discussion);
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
    } else if (action === "next-article") await nextArticle(true);
    else if (action === "save") {
      const target = known.get(b.dataset.article) || view.article;
      const a = articleSummary(target);
      await accountGate(async () => {
        await store.save(a);
        await sync();
      });
    } else if (action === "react") {
      const a = articleSummary(discussion.article),
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
      const c = [
        ...localMessages(discussion),
        ...(discussion.comments?.items || []),
      ].find((x) => x.ref === b.dataset.ref);
      if (c)
        await accountGate(() => {
          discussion.reply = {
            ref: c.ref,
            name: c.name,
            text: c.text.slice(0, 600),
          };
          discussion.editing = "";
          render();
          commentInput(discussion)?.focus();
        });
    } else if (action === "cancel-reply") {
      discussion.reply = null;
      discussion.editing = "";
      render();
      commentInput(discussion).value = "";
    } else if (action === "edit-comment") {
      const c = db.comments.find(
        (x) =>
          x.id === b.dataset.id &&
          x.authorId === me()?.id &&
          x.articleId === discussion.article.id,
      );
      if (c) {
        discussion.editing = c.id;
        discussion.reply = null;
        render();
        commentInput(discussion).value = c.text;
        commentInput(discussion).focus();
      }
    } else if (action === "delete-comment") {
      const id = b.dataset.id;
      if (
        await confirmAction({
          title: t("confirmDelete"),
          description: t("confirmDeleteText"),
          confirmLabel: t("delete"),
        })
      ) {
        await store.removeComment(id);
        if (
          discussion.editing === id ||
          discussion.reply?.ref === `local:${id}`
        ) {
          discussion.editing = "";
          discussion.reply = null;
        }
        await sync();
      }
    } else if (action === "logout") {
      if (
        await confirmAction({
          title: t("confirmLogout"),
          confirmLabel: t("logout"),
        })
      ) {
        store.signOut();
        for (const context of [view, ...(view.discussions?.values() || [])]) {
          context.reply = null;
          context.editing = "";
        }
        await sync();
      }
    } else if (action === "delete-account") {
      if (
        await confirmAction({
          title: t("confirmAccount"),
          description: t("confirmAccountText"),
          confirmLabel: t("deleteAccount"),
        })
      ) {
        await store.removeAccount();
        db = await store.read();
        location.hash = "#/home";
      }
    } else if (action === "remove-avatar") {
      if (profileEdit) {
        profileEdit.upload = (profileEdit.upload || 0) + 1;
        profileEdit.avatarBusy = false;
        profileEdit.avatar = "";
        document.querySelector('#profile-form [type="submit"]').disabled =
          false;
        avatarPreview();
      }
    } else if (action === "share") {
      const target = known.get(b.dataset.article) || view.article;
      const url = location.origin + location.pathname + route(target);
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
    !["search", "auth-form", "profile-form"].includes(form.id) &&
    !form.matches(".comment-form")
  )
    return;
  event.preventDefault();
  const submittedAt = token;
  const formContext = form.matches(".comment-form")
    ? articleState(form.dataset.article)
    : null;
  const data = new FormData(form),
    submit = form.querySelector('[type="submit"]');
  if (submit.disabled || formContext?.commentSubmitting) return;
  if (formContext) formContext.commentSubmitting = true;
  submit.disabled = true;
  try {
    if (form.id === "search") {
      query = String(data.get("q")).trim();
      const hash = "#/search/" + encodeURIComponent(query);
      if (location.hash === hash) await navigate(true);
      else location.hash = hash;
    } else if (form.id === "auth-form") {
      await authenticate(
        String(data.get("login")),
        String(data.get("password")),
        form.dataset.register === "true" ? String(data.get("name")) : undefined,
      );
      db = await store.read();
      const currentAuth =
        form.isConnected && document.querySelector("#modal").open;
      if (currentAuth) closeModal();
      render();
      const fn = currentAuth ? pending : null;
      if (currentAuth) pending = null;
      if (fn) await fn();
    } else if (form.matches(".comment-form")) {
      const discussion = articleState(form.dataset.article);
      await store.comment(
        articleSummary(discussion.article),
        String(data.get("text")),
        discussion.reply,
        discussion.editing,
      );
      discussion.reply = null;
      discussion.editing = "";
      discussion.commentSubmitting = false;
      if (submittedAt === token) document.getElementById(form.id)?.reset();
      await sync();
    } else if (form.id === "profile-form") {
      const name = String(data.get("name")).trim();
      const draft = profileEdit;
      if (!name || !draft || draft.avatarBusy) throw new Error("invalid");
      await store.change((s) => {
        const account = store.requireAccount(s);
        if (account.id !== draft.accountId) throw new Error("authError");
        account.name = name.slice(0, 80);
        if (draft.avatar) account.avatar = draft.avatar;
        else delete account.avatar;
      });
      profileEdit = null;
      closeModal();
      await sync();
      toast(t("done"));
    }
  } catch (error) {
    const currentForm =
      submittedAt === token ? document.getElementById(form.id) : null;
    const target = currentForm?.querySelector(".form-error");
    if (target) target.textContent = err(error);
    else toast(err(error));
  } finally {
    if (formContext) formContext.commentSubmitting = false;
    submit.disabled = false;
    if (submittedAt === token) {
      const currentSubmit = document
        .getElementById(form.id)
        ?.querySelector('[type="submit"]');
      if (currentSubmit) currentSubmit.disabled = false;
    }
  }
});
document.addEventListener("input", (event) => {
  if (event.target.id === "search-input") query = event.target.value;
});
document.addEventListener("change", async (event) => {
  if (event.target.id !== "avatar-file") return;
  const f = event.target.files?.[0];
  const draft = profileEdit;
  if (!f || !draft) return;
  const upload = (draft.upload || 0) + 1;
  draft.upload = upload;
  draft.avatarBusy = true;
  document.querySelector('#profile-form [type="submit"]').disabled = true;
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
    if (profileEdit !== draft || draft.upload !== upload) return;
    draft.avatar = data;
    avatarPreview();
  } catch (error) {
    if (profileEdit === draft)
      document.querySelector("#profile-form .form-error").textContent =
        err(error);
  } finally {
    if (profileEdit === draft && draft.upload === upload) {
      draft.avatarBusy = false;
      document.querySelector('#profile-form [type="submit"]').disabled = false;
    }
  }
});
document.addEventListener(
  "error",
  (event) => {
    if (event.target.tagName === "IMG" && event.target.hasAttribute("src")) {
      event.target.classList.add("image-failed");
      event.target.removeAttribute("src");
      event.target.alt = t("imageUnavailable");
      const frame = event.target.closest(".story-image");
      if (frame) {
        event.target.hidden = true;
        frame.classList.add("unavailable-image");
        frame.setAttribute("role", "img");
        frame.setAttribute("aria-label", t("imageUnavailable"));
      }
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
    closeNavigation();
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
window.addEventListener("pageshow", (event) => {
  if (event.persisted) navigate();
});
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
document.querySelector("#modal").addEventListener("close", () => {
  if (!document.querySelector("#modal").open) profileEdit = null;
});
function closeNavigation() {
  const wasOpen = menu;
  menu = false;
  document.querySelector("#mobile-navigation").close();
  document.querySelector("#mobile-navigation").innerHTML = "";
  if (wasOpen) document.querySelector('[data-action="menu"]')?.focus();
  document.body.classList.remove("mobile-nav-open");
  document
    .querySelector('[data-action="menu"]')
    ?.setAttribute("aria-expanded", "false");
}
const navigationDrawer = document.querySelector("#mobile-navigation");
navigationDrawer.addEventListener("close", closeNavigation);
navigationDrawer.addEventListener("click", (event) => {
  if (event.target.closest("a")) closeNavigation();
  if (event.target === navigationDrawer) {
    const rect = navigationDrawer.getBoundingClientRect();
    if (
      event.clientX < rect.left ||
      event.clientX > rect.right ||
      event.clientY < rect.top ||
      event.clientY > rect.bottom
    )
      closeNavigation();
  }
});
matchMedia("(max-width: 650px)").addEventListener("change", (event) => {
  if (!event.matches) closeNavigation();
});

await navigate();
