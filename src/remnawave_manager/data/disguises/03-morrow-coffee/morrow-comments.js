import { getComments } from "./morrow-data.js";
import { t, date, number } from "./morrow-i18n.js";
import {
  el,
  button,
  iconButton,
  portrait,
  modal,
  confirmAction,
  loadingIndicator,
} from "./morrow-ui.js";
export function openComments(video, { getStore, mutate, gate, openAuthor }) {
  const controller = new AbortController();
  let publicItems = [],
    page = 1,
    next = true,
    busy = false,
    target = null,
    editing = null;
  const list = el("div", { class: "comment-list" }),
    error = el("p", { class: "error", role: "alert" }),
    context = el("div", { class: "reply-context" }),
    loading = loadingIndicator("comments-loading");
  loading.hidden = true;
  const input = el("textarea", {
    rows: 2,
    maxlength: 5000,
    placeholder: t("commentPlaceholder"),
    "aria-label": t("commentPlaceholder"),
  });
  const send = button(t("send"), submit, "primary");
  const form = el(
    "form",
    {
      class: "comment-form",
      onsubmit: (e) => {
        e.preventDefault();
        submit();
      },
    },
    context,
    input,
    error,
    send,
  );
  const loadMore = button(t("moreComments"), load, "more-comments");
  const pagination = el("div", { class: "comments-pagination" }, loading, loadMore);
  list.append(pagination);
  const dialog = modal(
    t("comments"),
    el("div", { class: "discussion" }, list, form),
  );
  dialog.classList.add("comments-panel");
  dialog.addEventListener("close", () => controller.abort());
  function profileAuthor() {
    return getStore()?.data.profile || { name: t("unknownAuthor") };
  }
  function setTarget(c, edit = false) {
    target = edit ? null : c;
    editing = edit ? c : null;
    input.value = edit ? c.text : "";
    context.replaceChildren();
    if (c)
      context.append(
        el(
          "span",
          {},
          `${t(edit ? "edit" : "replying")} ${c.author?.name || profileAuthor().name}`,
        ),
        iconButton("close", t("cancel"), () => setTarget(null)),
      );
    input.focus();
  }
  async function submit() {
    if (!input.value.trim() || send.disabled) return;
    if (!getStore()) {
      gate();
      return;
    }
    const value = input.value.trim(),
      reply = target,
      edit = editing;
    send.disabled = true;
    error.textContent = "";
    try {
      await mutate((d) => {
        if (edit) {
          const c = d.comments.find((x) => x.id === edit.id);
          if (!c) throw new Error("conflict");
          c.text = value;
          c.updated = Date.now();
        } else
          d.comments.push({
            id: "local:" + crypto.randomUUID(),
            videoId: video.id,
            parentId: reply?.id || null,
            recipient: reply?.author?.name || "",
            text: value,
            created: Date.now(),
            updated: Date.now(),
            deleted: false,
          });
      });
      input.value = "";
      setTarget(null);
      render();
    } catch (e) {
      error.textContent = t(e.message);
    } finally {
      send.disabled = false;
    }
  }
  function branchIds(items, roots) {
    const ids = new Set(roots);
    let changed = true;
    while (changed) {
      changed = false;
      for (const item of items) if (ids.has(item.parentId) && !ids.has(item.id)) {
        ids.add(item.id);
        changed = true;
      }
    }
    return ids;
  }
  function render() {
    const scrollTop = list.scrollTop;
    const d = getStore()?.data,
      locals = (d?.comments || [])
        .filter((c) => c.videoId === video.id)
        .map((c) => ({ ...c, author: profileAuthor(), local: true, likes: 0 }));
    const items = [...publicItems, ...locals];
    const removed = branchIds(items, items.filter(c => c.deleted).map(c => c.id));
    const all = items.filter(c => !removed.has(c.id)),
      map = new Map(all.map((c) => [c.id, c]));
    const children = new Map();
    for (const c of all) {
      const parent = map.has(c.parentId) ? c.parentId : null;
      if (!children.has(parent)) children.set(parent, []);
      children.get(parent).push(c);
    }
    list.replaceChildren();
    if (!all.length && !busy && !error.textContent)
      list.append(el("p", { class: "empty-note" }, t("noComments")));
    function row(c, depth) {
      const liked = d?.commentLikes.includes(c.id),
        author = c.author || { name: t("unknownAuthor") };
      const body = el(
        "div",
        { class: "comment-body" },
        button(
          author.name,
          () => {
            if (!c.local && author.id) {
              dialog.close();
              openAuthor(author.id);
            }
          },
          "comment-author",
        ),
        el("p", { class: "comment-text" }, c.deleted ? t("deleted") : c.text),
        el("small", {}, date(c.created)),
      );
      if (c.recipient)
        body.insertBefore(
          el(
            "small",
            { class: "reply-recipient" },
            `${t("replying")} ${c.recipient}`,
          ),
          body.children[1],
        );
      const actions = el("div", { class: "comment-actions" });
      if (!c.deleted) {
        actions.append(
          button(t("reply"), () => {
            if (!getStore()) {
              gate();
              return;
            }
            setTarget(c);
          }),
        );
        const like = iconButton(
          "heart",
          t(liked ? "unlike" : "like"),
          async () => {
            if (!getStore()) {
              gate();
              return;
            }
            try {
              await mutate((x) => {
                const i = x.commentLikes.indexOf(c.id);
                if (i < 0) x.commentLikes.push(c.id);
                else x.commentLikes.splice(i, 1);
              });
              render();
            } catch (e) {
              error.textContent = t(e.message);
            }
          },
          liked ? "liked" : "",
        );
        like.setAttribute("aria-pressed", String(!!liked));
        like.append(el("span", {}, number((c.likes || 0) + (liked ? 1 : 0))));
        actions.append(like);
      }
      if (c.local && !c.deleted)
        actions.append(
          button(t("edit"), () => setTarget(c, true)),
          button(t("delete"), () =>
            confirmAction(t("delete"), t("deleteText"), async () => {
              let removed;
              await mutate((x) => {
                removed = branchIds(x.comments.filter(y => y.videoId === video.id), [c.id]);
                x.comments = x.comments.filter(y => !removed.has(y.id));
                x.commentLikes = x.commentLikes.filter(id => !removed.has(id));
              });
              if (removed.has(target?.id) || removed.has(editing?.id)) setTarget(null);
              render();
            }),
          ),
        );
      body.append(actions);
      const item = el(
        "article",
        {
          class: "comment" + (depth ? " nested" : ""),
          "data-comment-id": c.id,
        },
        portrait(author),
        body,
      );
      list.append(item);
      for (const child of children.get(c.id) || [])
        row(child, Math.min(depth + 1, 2));
    }
    for (const c of children.get(null) || []) row(c, 0);
    list.append(pagination);
    list.scrollTop = scrollTop;
  }
  async function load() {
    if (busy || !next || controller.signal.aborted) return;
    busy = true;
    loadMore.disabled = true;
    loadMore.hidden = true;
    loading.hidden = false;
    error.textContent = "";
    list.querySelector(".empty-note")?.remove();
    try {
      // The source can return just three comments per page. Collect a useful
      // batch per click, with a request bound for unusually short pages.
      const targetCount = Math.min(publicItems.length + 12, 1000);
      for (let step = 0; next && publicItems.length < targetCount && step < 20; step++) {
        const result = await getComments(video.id, page, controller.signal);
        if (controller.signal.aborted) return;
        if (result.stale) error.textContent = t("staleComments");
        const known = new Set(publicItems.map((c) => c.id));
        const fresh = result.items.filter((c) => !known.has(c.id) && (known.add(c.id), true));
        publicItems.push(...fresh);
        page++;
        next = !!result.next && !!fresh.length && publicItems.length < 1000;
      }
    } catch (e) {
      if (e.name !== "AbortError") error.textContent = t(e.message);
    } finally {
      busy = false;
      loadMore.disabled = false;
      loadMore.hidden = !next;
      loading.hidden = true;
      if (!controller.signal.aborted) render();
    }
  }
  load();
  return dialog;
}
