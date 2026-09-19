import { loadingNode } from '../shared/feedback.js';
import { t, lang, setting, prefs, date } from "./svod-i18n.js";
import {
  el,
  icon,
  button,
  link,
  field,
  dialog,
  confirm,
  notify,
  route,
  plain,
} from "./svod-ui.js";
import * as data from "./svod-data.js";
import * as store from "./svod-store.js";
import { authenticate } from "./svod-auth.js";
import { renderArticle } from "./svod-article.js";
const app = document.querySelector("#app");
history.scrollRestoration = "manual";
let account = null,
  main,
  side,
  controller,
  epoch = 0,
  reading = null,
  tocState = null,
  tocFrame = 0,
  lastRoute = "",
  storageOK = true;
const topics = [
  ["science", "Наука", "01"],
  ["nature", "Природа", "02"],
  ["culture", "Искусство", "03"],
  ["technology", "Технология", "04"],
  ["geography", "География", "05"],
  ["past", "История", "06"],
];
function parseRoute() {
  const [path, query = ""] = location.hash.slice(2).split("?");
  const params = new URLSearchParams(query);
  return {
    path: [
      "home",
      "search",
      "article",
      "category",
      "random",
      "library",
      "history",
      "about",
    ].includes(path)
      ? path
      : "home",
    q: (params.get("q") || "").slice(0, 200),
    section: params.get("section") || "",
  };
}
function searchForm(value = "") {
  const input = el("input", {
    type: "search",
    name: "q",
    value,
    placeholder: t("searchHint"),
    "aria-label": t("searchHint"),
    maxlength: 200,
    required: true,
  });
  const form = el(
    "form",
    {
      class: "search-form",
      role: "search",
      onsubmit: (e) => {
        e.preventDefault();
        const q = input.value.trim();
        if (q) {
          location.hash = route("search", q);
          input.blur();
        }
      },
    },
    input,
    el("button", { type: "submit", "aria-label": t("search") }, icon("search")),
  );
  return form;
}
function selection(label, key, options) {
  const select = el(
    "select",
    { "aria-label": t(label), onchange: (e) => setting(key, e.target.value) },
    options.map((v) =>
      el("option", { value: v, selected: prefs[key] === v }, t(v)),
    ),
  );
  return field(t(label), select);
}
function appearance() {
  dialog(t("appearance"), [
    selection("theme", "theme", ["system", "light", "dark"]),
    selection("font", "font", ["standard", "large"]),
    selection("width", "width", ["standard", "wide"]),
  ]);
}
function shell() {
  const current = parseRoute();
  const navItems = ["home", "random", "library", "history", "about"];
  app.classList.remove("nav-open");
  document.body.classList.remove("mobile-nav-open");
  const nav = el(
    "nav",
    { "aria-label": t("menu") },
    navItems.map((key) => {
      const item = link(
        [icon(key), t(key)],
        route(key),
        current.path === key ? "active" : "",
      );
      if (current.path === key) item.setAttribute("aria-current", "page");
      return item;
    }),
  );
  side = el(
    "aside",
    { class: "sidebar", id: "navigation" },
    el("p", { class: "rail-label" }, t("reading")),
    nav,
  );
  const language = button(
    lang === "ru" ? "EN" : "RU",
    () => {
      const position = scrollY;
      setting("lang", lang === "ru" ? "en" : "ru");
      render().then(() => scrollTo(0, position));
    },
    "quiet",
  );
  language.setAttribute("aria-label", t("language"));
  const setNavigation = (open, restoreFocus = false) => {
    app.classList.toggle("nav-open", open);
    menu.setAttribute("aria-expanded", String(open));
    document.body.classList.toggle("mobile-nav-open", open);
    for (const node of app.querySelectorAll(".header,main,.footer"))
      node.inert = open;
    if (open) {
      const focusNavigation = () => {
        if (app.classList.contains("nav-open")) side.querySelector("a")?.focus();
      };
      requestAnimationFrame(focusNavigation);
      side.addEventListener("transitionend", focusNavigation, { once: true });
    } else if (restoreFocus) menu.focus();
  };
  const menu = button(
    icon("menu"),
    () => setNavigation(!app.classList.contains("nav-open")),
    "mobile-menu",
  );
  menu.setAttribute("aria-label", t("menu"));
  menu.setAttribute("aria-expanded", "false");
  menu.setAttribute("aria-controls", "navigation");
  const drawerClose = button(
    icon("close"),
    () => setNavigation(false, true),
    "drawer-close",
  );
  drawerClose.setAttribute("aria-label", t("close"));
  side.prepend(drawerClose);
  const appearanceButton = button(icon("appearance"), appearance, "quiet");
  appearanceButton.setAttribute("aria-label", t("appearance"));
  main = el("main", { id: "main", tabindex: -1 });
  app.replaceChildren(
    link(t("skip"), "#main", "skip-link"),
    el(
      "header",
      { class: "header" },
      el(
        "div",
        { class: "brand-wrap" },
        menu,
        link(
          [
            el("img", { src: "favicon.svg", alt: "", width: 36, height: 36 }),
            el(
              "span",
              {},
              el("b", {}, t("brand")),
              el("small", {}, t("tagline")),
            ),
          ],
          route("home"),
          "brand",
        ),
      ),
      searchForm(current.path === "search" ? current.q : ""),
      el(
        "div",
        { class: "header-actions" },
        language,
        appearanceButton,
        button(
          account ? account.name : t("login"),
          () => (account ? accountDialog() : authDialog()),
          "account-button",
        ),
      ),
    ),
    el(
      "div",
      { class: "layout" },
      button(t("close"), () => setNavigation(false, true), "nav-backdrop"),
      side,
      main,
    ),
    el(
      "footer",
      { class: "footer" },
      el("span", {}, t("brand") + " · " + t("tagline")),
      link(t("about"), route("about")),
    ),
  );
  app.querySelector(".skip-link").addEventListener("click", (e) => {
    e.preventDefault();
    main.focus();
    main.scrollIntoView({ block: "start" });
  });
  if (!storageOK)
    main.append(el("p", { class: "notice" }, t("unavailableStorage")));
}
function heading(title, subtitle) {
  return el(
    "header",
    { class: "page-heading" },
    el("p", { class: "eyebrow" }, t("brand") + " / " + t("reading")),
    el("h1", {}, title),
    subtitle ? el("p", { class: "muted" }, subtitle) : null,
  );
}
function empty(title, hint, action = link(t("home"), route("home"), "button")) {
  return el(
    "div",
    { class: "empty" },
    el("span", { class: "empty-icon", "aria-hidden": true }, icon("library")),
    el("h2", {}, t(title)),
    el("p", {}, t(hint)),
    action,
  );
}
function errorView(error, host, action) {
  host.replaceChildren(
    el(
      "div",
      { class: "error-box", role: "alert" },
      el(
        "h2",
        {},
        t(
          error.message === "storageError"
            ? "storageTitle"
            : error.message === "missing"
              ? "missing"
              : "unavailable",
        ),
      ),
      el(
        "p",
        {},
        t(
          error.message === "storageError"
            ? "storageError"
            : error.message === "rateLimit"
              ? "rateLimit"
              : "unavailableHint",
        ),
      ),
      button(t("retry"), action),
    ),
  );
}
function authDialog(after) {
  let register = false,
    d;
  const build = () => {
    d?.close();
    d?.remove();
    const username = el("input", {
        name: "username",
        autocomplete: "username",
        required: true,
        maxlength: 40,
      }),
      password = el("input", {
        name: "password",
        type: "password",
        autocomplete: register ? "new-password" : "current-password",
        required: true,
        minlength: register ? 8 : 1,
        maxlength: 128,
      }),
      name = el("input", {
        name: "name",
        autocomplete: "nickname",
        required: true,
        maxlength: 80,
      }),
      error = el("p", { class: "form-error", role: "alert" }),
      submit = el(
        "button",
        { type: "submit", class: "primary" },
        t(register ? "register" : "login"),
      );
    const form = el(
      "form",
      {
        onsubmit: async (e) => {
          e.preventDefault();
          submit.disabled = true;
          const switchButton = d.querySelector(".auth-switch");
          switchButton.disabled = true;
          error.textContent = "";
          try {
            await authenticate(
              username.value,
              password.value,
              register ? name.value : undefined,
            );
            account = await store.current();
            d.close();
            await render();
            if (after) await after();
          } catch (err) {
            error.textContent = t(
              [
                "invalid",
                "duplicate",
                "authError",
                "secureError",
                "storageError",
              ].includes(err.message)
                ? err.message
                : "storageError",
            );
          } finally {
            submit.disabled = false;
            switchButton.disabled = false;
          }
        },
      },
      register ? field(t("name"), name) : null,
      field(t("username"), username),
      field(t("password"), password),
      register ? el("small", { class: "muted" }, t("passwordHint")) : null,
      error,
      submit,
    );
    d = dialog(t(register ? "register" : "login"), [
      form,
      button(
        t(register ? "login" : "register"),
        () => {
          register = !register;
          build();
        },
        "auth-switch",
      ),
    ]);
  };
  build();
}
function accountDialog() {
  let d;
  d = dialog(t("account"), [
    el(
      "p",
      {},
      account.name,
      el("br"),
      el("span", { class: "muted" }, "@" + account.login),
    ),
    button(t("logout"), () =>
      confirm(t("logoutConfirm"), async () => {
        await flushReading();
        store.signOut();
        reading = null;
        d.close();
        await render();
      }),
    ),
    button(
      t("deleteAccount"),
      () =>
        confirm(t("deleteConfirm"), async () => {
          reading = null;
          await store.deleteAccount();
          d.close();
          await render();
        }),
      "danger",
    ),
  ]);
}
function requireAccount(historyMode = false) {
  main.append(
    heading(t(historyMode ? "history" : "library")),
    empty(
      "accountNeeded",
      "accountNeededHint",
      button(t("login"), () => authDialog(), "primary"),
    ),
  );
}
async function home(token, signal) {
  main.append(
    el(
      "section",
      { class: "welcome" },
      el("div", { class: "eyebrow" }, t("russian")),
      el("h1", {}, t("welcome")),
      el("p", {}, t("welcomeText")),
    ),
    el(
      "section",
      { class: "topics-section" },
      el(
        "div",
        { class: "section-heading" },
        el("h2", {}, t("discover")),
        el("p", { class: "muted" }, t("discoverText")),
      ),
      el(
        "div",
        { class: "topics" },
        topics.map(([key, title, num]) =>
          link(
            [
              el("span", { class: "topic-number" }, num),
              el("strong", {}, t(key)),
              icon("arrow"),
            ],
            route("search", title),
            "topic",
          ),
        ),
      ),
    ),
  );
  if (account) {
    const history = (await store.owned("history"))
      .sort((a, b) => b.at - a.at)
      .slice(0, 3);
    if (token !== epoch) return;
    if (history.length)
      main.append(
        el(
          "section",
          { class: "continue-section" },
          el("h2", {}, t("continue")),
          el(
            "div",
            { class: "reading-cards" },
            history.map((item) =>
              link(
                [
                  el("span", { class: "eyebrow" }, t("continue")),
                  el("h3", { lang: "ru" }, item.title),
                  el("small", { class: "muted" }, date(item.at)),
                ],
                route("article", item.title),
                "reading-card",
              ),
            ),
          ),
        ),
      );
  }
  const list = el(
    "div",
    { class: "discovery-list" },
    el("p", { class: "load-status", role: "status" }, loadingNode(t("loading"))),
  );
  main.append(
    el(
      "section",
      { class: "selected-section" },
      el(
        "div",
        { class: "section-heading" },
        el("h2", {}, t("selected")),
        el("p", { class: "muted" }, t("selectedHint")),
      ),
      list,
    ),
  );
  try {
    const result = await data.search("Солнечная система", 0, signal);
    if (token !== epoch) return;
    list.replaceChildren(
      ...result.items.slice(0, 4).map((item) => resultCard(item)),
    );
    if (!result.items.length)
      list.append(empty("emptySearch", "emptySearchHint"));
  } catch (e) {
    if (token === epoch) errorView(e, list, () => render());
  }
}
function resultCard(item) {
  return el(
    "article",
    { class: "result-card" },
    el("div", { class: "result-kicker" }, t("article")),
    el("h2", { lang: "ru" }, link(item.title, route("article", item.title))),
    item.snippet ? el("p", { lang: "ru" }, plain(item.snippet)) : null,
    link(t("read"), route("article", item.title), "read-link"),
  );
}
async function searchPage(q, token, signal, isCategory = false) {
  main.append(
    heading(
      isCategory ? q.replace(/^Категория:/, "") : t("results"),
      isCategory ? t("category") : q,
    ),
  );
  const results = el("div", { class: "results-list" }),
    status = el("div", { class: "load-status", role: "status" }, loadingNode(t("loading")));
  main.append(results, status);
  let next = isCategory ? "" : 0;
  const seen = new Set();
  const load = async () => {
    status.replaceChildren(el("p", {}, loadingNode(t("loading"))));
    try {
      const result = isCategory
        ? await data.category(q, next, signal)
        : await data.search(q, next, signal);
      if (token !== epoch) return;
      for (const item of result.items) {
        if (seen.has(item.title)) continue;
        seen.add(item.title);
        results.append(
          isCategory && item.ns === 14
            ? el(
                "article",
                { class: "result-card" },
                link(
                  item.title.replace(/^Категория:/, ""),
                  route("category", item.title),
                ),
              )
            : resultCard(item),
        );
      }
      next = result.next;
      status.replaceChildren();
      if (next !== undefined) status.append(button(t("more"), load));
      if (!seen.size)
        results.replaceChildren(empty("emptySearch", "emptySearchHint"));
    } catch (e) {
      if (token === epoch) errorView(e, status, load);
    }
  };
  if (q) await load();
  else status.replaceChildren(empty("emptySearch", "emptySearchHint"));
}
async function toggleSaved(article, btn) {
  if (!account) {
    authDialog(async () => {
      await store.saveArticle(article);
      notify("done");
      await render();
    });
    return;
  }
  try {
    const id = `${account.id}:${article.pageid}`;
    if (await store.get("library", id)) {
      confirm(t("removeSavedConfirm"), async () => {
        await store.removeSaved(id);
        btn.textContent = t("save");
        btn.setAttribute("aria-pressed", "false");
      });
    } else {
      await store.saveArticle(article);
      btn.textContent = t("saved");
      btn.setAttribute("aria-pressed", "true");
    }
  } catch {
    notify("storageError");
  }
}
async function articlePage(title, section, token, signal) {
  const loading = el(
    "div",
    { class: "load-status", role: "status" },
    loadingNode(t("loading")),
  );
  main.append(loading);
  try {
    const article = await data.article(title, signal);
    if (token !== epoch) return;
    const { container, headings, ids } = renderArticle(article);
    const saved = account
      ? await store
          .get("library", `${account.id}:${article.pageid}`)
          .catch(() => {
            notify("storageError");
            return null;
          })
      : null;
    if (token !== epoch) return;
    const save = button(t(saved ? "saved" : "save"), async (e) => {
      const target = e.currentTarget;
      target.disabled = true;
      try {
        await toggleSaved(article, target);
      } finally {
        target.disabled = false;
      }
    });
    save.setAttribute("aria-pressed", Boolean(saved));
    const tools = el(
      "div",
      { class: "article-tools" },
      save,
      button(t("manage"), () => manageCollections(article)),
      button(t("print"), () => window.print()),
    );
    const tocNav = el("nav", { "aria-label": t("contents") });
    const beginningButton = button(t("beginning"), () => {
      document.querySelector(".article-heading").scrollIntoView();
      history.replaceState(null, "", route("article", article.title));
    });
    beginningButton.dataset.section = "";
    tocNav.append(
      beginningButton,
      ...headings.map((h) => {
        const item = button(
          h.text,
          () => {
            document.getElementById(h.id)?.scrollIntoView();
            history.replaceState(
              null,
              "",
              route("article", article.title, h.id),
            );
          },
          "toc-level-" + h.level,
        );
        item.dataset.section = h.id;
        return item;
      }),
    );
    const toc = el(
      "details",
      { class: "toc", open: innerWidth > 1000 },
      el("summary", {}, t("contents")),
      tocNav,
    );
    const body = el(
      "article",
      { class: "article-page" },
      el(
        "header",
        { class: "article-heading" },
        el("div", { class: "eyebrow" }, t("article")),
        el("h1", { lang: "ru" }, article.title),
        tools,
      ),
      article.stale
        ? el("p", { class: "notice" }, t("stale") + " " + date(article.stale))
        : null,
      container,
      el(
        "div",
        { class: "article-categories" },
        el("strong", {}, t("categories")),
        article.categories
          .slice(0, 30)
          .map((c) =>
            link(c.replaceAll("_", " "), route("category", "Категория:" + c)),
          ),
      ),
    );
    main.replaceChildren(el("div", { class: "article-layout" }, toc, body));
    tocState = { headings, buttons: [...tocNav.querySelectorAll("button")] };
    updateToc();
    document.title = "Svod — " + article.title;
    const old = account
      ? await store
          .get("history", `${account.id}:${article.pageid}`)
          .catch(() => {
            notify("storageError");
            return null;
          })
      : null;
    if (token !== epoch) return;
    if (account) {
      reading = { article, owner: account.id, headings };
      await store
        .remember(article, old?.position || 0, account.id, old?.section || "")
        .catch(() => notify("storageError"));
    }
    requestAnimationFrame(() => {
      if (token !== epoch) return;
      if (section) {
        document.getElementById(ids.get(section) || section)?.scrollIntoView();
      } else if (old?.section && document.getElementById(old.section)) {
        document.getElementById(old.section).scrollIntoView();
      } else if (old?.position) {
        scrollTo(
          0,
          old.position *
            Math.max(0, document.documentElement.scrollHeight - innerHeight),
        );
      }
    });
  } catch (e) {
    if (token === epoch) errorView(e, loading, () => render());
  }
}
async function manageCollections(article) {
  if (!account) {
    authDialog(() => manageCollections(article));
    return;
  }
  try {
    const rows = await store.owned("collections"),
      checks = [];
    let d;
    const content = rows.map((c) => {
      const input = el("input", {
        type: "checkbox",
        checked: c.pages.includes(article.pageid),
      });
      checks.push([c, input]);
      return el("label", { class: "check-row" }, input, c.name);
    });
    d = dialog(t("collections"), [
      ...content,
      rows.length ? null : el("p", { class: "muted" }, t("noItems")),
      button(t("newCollection"), () =>
        editCollection(null, () => {
          d.close();
          manageCollections(article);
        }),
      ),
      button(
        t("save"),
        async (event) => {
          const submit = event.currentTarget;
          submit.disabled = true;
          try {
            if (
              !(await store.get("library", `${account.id}:${article.pageid}`))
            )
              await store.saveArticle(article);
            for (const [c, input] of checks)
              await store.put("collections", {
                ...c,
                pages: input.checked
                  ? [...new Set([...c.pages, article.pageid])]
                  : c.pages.filter((id) => id !== article.pageid),
              });
            d.close();
            notify("done");
            await render();
          } catch {
            notify("storageError");
            submit.disabled = false;
          }
        },
        "primary",
      ),
    ]);
  } catch {
    notify("storageError");
  }
}
function editCollection(collection, after = () => render()) {
  const input = el("input", {
    required: true,
    maxlength: 80,
    value: collection?.name || "",
  });
  let d;
  const submit = el(
    "button",
    { type: "submit", class: "primary" },
    t("save"),
  );
  d = dialog(t(collection ? "rename" : "newCollection"), [
    el(
      "form",
      {
        onsubmit: async (e) => {
          e.preventDefault();
          const name = input.value.trim();
          if (!name) {
            input.value = "";
            input.reportValidity();
            return;
          }
          submit.disabled = true;
          try {
            await store.put("collections", {
              id: collection?.id || crypto.randomUUID(),
              owner: account.id,
              name,
              pages: collection?.pages || [],
            });
            d.close();
            await after();
          } catch {
            notify("storageError");
            submit.disabled = false;
          }
        },
      },
      field(t("collectionName"), input),
      submit,
    ),
  ]);
}
async function personalPage(historyMode, token) {
  if (!account) {
    requireAccount(historyMode);
    return;
  }
  const rows = (await store.owned(historyMode ? "history" : "library")).sort(
      (a, b) => b.at - a.at,
    ),
    collections = historyMode ? [] : await store.owned("collections");
  if (token !== epoch) return;
  main.append(
    heading(
      t(historyMode ? "history" : "library"),
      t(historyMode ? "historyHint" : "libraryHint"),
    ),
  );
  const content = el("div", { class: "personal-list" });
  let selected = "",
    query = "",
    sort = "newest";
  const paint = () => {
    const coll = collections.find((c) => c.id === selected);
    const filtered = rows
      .filter(
        (r) =>
          (!coll || coll.pages.includes(r.pageid)) &&
          r.title.toLocaleLowerCase().includes(query.toLocaleLowerCase()),
      )
      .sort((a, b) =>
        sort === "alphabet"
          ? a.title.localeCompare(b.title, "ru")
          : b.at - a.at,
      );
    content.replaceChildren(
      ...filtered.map((item) =>
        el(
          "article",
          { class: "personal-item" },
          el(
            "div",
            {},
            link(item.title, route("article", item.title), "personal-title"),
            el("small", { class: "muted" }, date(item.at)),
          ),
          el(
            "div",
            { class: "actions" },
            historyMode
              ? null
              : button(t("manage"), () => manageCollections(item)),
            button(
              t("remove"),
              () => {
                const remove = async () => {
                  try {
                    if (historyMode) await store.remove("history", item.id);
                    else await store.removeSaved(item.id);
                    await render();
                  } catch {
                    notify("storageError");
                  }
                };
                if (historyMode) remove();
                else confirm(t("removeSavedConfirm"), remove);
              },
              "quiet",
            ),
          ),
        ),
      ),
    );
    if (!filtered.length)
      content.append(
        empty(
          rows.length
            ? "emptySearch"
            : historyMode
              ? "emptyHistory"
              : "emptyLibrary",
          rows.length
            ? "emptySearchHint"
            : historyMode
              ? "emptyHistoryHint"
              : "emptyLibraryHint",
        ),
      );
  };
  const controls = el("div", { class: "library-controls" });
  if (historyMode) {
    if (rows.length)
      controls.append(
        button(t("clearHistory"), () =>
          confirm(t("clearConfirm"), async () => {
            reading = null;
            await store.deleteOwned("history", account.id);
            await render();
          }),
        ),
      );
  } else {
    const filter = el("input", {
        type: "search",
        placeholder: t("filter"),
        "aria-label": t("filter"),
        oninput: (e) => {
          query = e.target.value;
          paint();
        },
      }),
      select = el(
        "select",
        {
          "aria-label": t("sort"),
          onchange: (e) => {
            sort = e.target.value;
            paint();
          },
        },
        ["newest", "alphabet"].map((v) => el("option", { value: v }, t(v))),
      );
    controls.append(filter, select);
    const tabs = el("div", { class: "collection-tabs" });
    const choose = (id) => {
      selected = id;
      for (const b of tabs.querySelectorAll(".collection-select")) {
        const active = b.dataset.id === id;
        b.classList.toggle("active", active);
        b.setAttribute("aria-pressed", String(active));
      }
      paint();
    };
    for (const c of [{ id: "", name: t("all") }, ...collections]) {
      const b = button(
        c.name,
        () => choose(c.id),
        "collection-select" + (c.id ? "" : " active"),
      );
      b.dataset.id = c.id;
      b.setAttribute("aria-pressed", String(!c.id));
      const options = c.id
        ? el(
            "details",
            {
              class: "collection-options",
              ontoggle: (event) => {
                if (!event.currentTarget.open) return;
                for (const other of tabs.querySelectorAll(
                  ".collection-options[open]",
                ))
                  if (other !== event.currentTarget) other.open = false;
              },
            },
            el(
              "summary",
              { "aria-label": `${t("collectionActions")}: ${c.name}` },
              "⋯",
            ),
            el(
              "div",
              { class: "collection-menu" },
              button(t("rename"), () => editCollection(c)),
              button(
                t("remove"),
                () =>
                  confirm(t("collectionDelete"), async () => {
                    await store.remove("collections", c.id);
                    await render();
                  }),
                "danger",
              ),
            ),
          )
        : null;
      tabs.append(el("div", { class: "collection-tab" }, b, options));
    }
    tabs.append(
      button(t("newCollection"), () => editCollection(), "new-collection"),
    );
    main.append(tabs);
  }
  main.append(controls, content);
  paint();
}
function readingSection(value) {
  let id = "";
  for (const heading of value.headings || []) {
    const node = document.getElementById(heading.id);
    if (node && node.getBoundingClientRect().top < 150) id = heading.id;
  }
  return id;
}
function updateToc() {
  if (!tocState) return;
  let current = "";
  for (const heading of tocState.headings) {
    const node = document.getElementById(heading.id);
    if (node && node.getBoundingClientRect().top <= 160) current = heading.id;
  }
  if (
    tocState.headings.length &&
    scrollY + innerHeight >= document.documentElement.scrollHeight - 2
  )
    current = tocState.headings.at(-1).id;
  for (const item of tocState.buttons) {
    const active = item.dataset.section === current;
    item.classList.toggle("active", active);
    if (active) item.setAttribute("aria-current", "location");
    else item.removeAttribute("aria-current");
  }
}
async function flushReading() {
  clearTimeout(scrollTimer);
  if (!reading) return;
  const currentReading = reading;
  reading = null;
  try {
    await store.remember(
      currentReading.article,
      scrollY /
        Math.max(1, document.documentElement.scrollHeight - innerHeight),
      currentReading.owner,
      readingSection(currentReading),
    );
  } catch {
    notify("storageError");
  }
}
async function render() {
  const r = parseRoute();
  document.title = "Svod — " + (r.path === "article" && r.q ? r.q : t(r.path));
  const token = ++epoch;
  controller?.abort();
  controller = new AbortController();
  tocState = null;
  if (!main) {
    shell();
    main.append(loadingNode(t("loading")));
  }
  await flushReading();
  try {
    account = await store.current();
    storageOK = true;
  } catch {
    account = null;
    storageOK = false;
  }
  if (token !== epoch) return;
  shell();

  if (lastRoute !== location.hash) {
    scrollTo(0, 0);
    lastRoute = location.hash;
  }
  try {
    if (r.path === "article")
      await articlePage(r.q, r.section, token, controller.signal);
    else if (r.path === "search" || r.path === "category")
      await searchPage(r.q, token, controller.signal, r.path === "category");
    else if (r.path === "library" || r.path === "history")
      await personalPage(r.path === "history", token);
    else if (r.path === "random") {
      main.append(
        el("p", { class: "load-status", role: "status" }, loadingNode(t("loading"))),
      );
      const title = await data.random(controller.signal);
      if (token === epoch) location.replace(route("article", title));
    } else if (r.path === "about")
      main.append(
        heading(t("about")),
        el("section", { class: "about-copy" }, el("p", {}, t("aboutText"))),
      );
    else await home(token, controller.signal);
  } catch (e) {
    if (token === epoch) errorView(e, main, () => render());
  }
}
let scrollTimer;
addEventListener(
  "scroll",
  () => {
    if (!tocFrame)
      tocFrame = requestAnimationFrame(() => {
        tocFrame = 0;
        updateToc();
      });
    clearTimeout(scrollTimer);
    scrollTimer = setTimeout(() => {
      if (reading)
        store
          .remember(
            reading.article,
            scrollY /
              Math.max(1, document.documentElement.scrollHeight - innerHeight),
            reading.owner,
            readingSection(reading),
          )
          .catch(() => notify("storageError"));
    }, 600);
  },
  { passive: true },
);
addEventListener("hashchange", () => {
  if (location.hash === "#main") {
    main?.focus();
    return;
  }
  render();
});
addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    if (!document.querySelector("dialog[open]"))
      document.querySelector(".search-form input")?.focus();
  }
  if (e.key === "Escape") {
    app.classList.remove("nav-open");
    document.body.classList.remove("mobile-nav-open");
    const menu = app.querySelector(".mobile-menu");
    menu?.setAttribute("aria-expanded", "false");
    for (const node of app.querySelectorAll(".header,main,.footer"))
      node.inert = false;
    if (!document.querySelector("dialog[open]") && menu) menu.focus();
  }
});
render();

// Keep the reading area visible when rotating or resizing to a narrow screen.
matchMedia("(max-width: 1000px)").addEventListener("change", (event) => {
  const contents = document.querySelector(".toc");
  if (contents) contents.open = !event.matches;
});
matchMedia("(max-width: 800px)").addEventListener("change", (event) => {
  if (!event.matches) {
    app.classList.remove("nav-open");
    document.body.classList.remove("mobile-nav-open");
    app.querySelector(".mobile-menu")?.setAttribute("aria-expanded", "false");
    for (const node of app.querySelectorAll(".header,main,.footer"))
      node.inert = false;
  }
});
