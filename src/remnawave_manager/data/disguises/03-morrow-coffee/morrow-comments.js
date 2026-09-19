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
  list.addEventListener(
    "scroll",
    () => {
      if (list.scrollTop + list.clientHeight >= list.scrollHeight - 240) load();
    },
    { signal: controller.signal },
  );
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
  const dialog = modal(
    t("comments"),
    el("div", { class: "discussion" }, list, loading, loadMore, form),
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
  function render() {
    const d = getStore()?.data,
      locals = (d?.comments || [])
        .filter((c) => c.videoId === video.id)
        .map((c) => ({ ...c, author: profileAuthor(), local: true, likes: 0 }));
    const all = [...publicItems, ...locals],
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
              await mutate((x) => {
                const own = x.comments.find((y) => y.id === c.id);
                if (own) {
                  own.text = "";
                  own.deleted = true;
                }
              });
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
  }
  async function load() {
    if (busy || !next) return;
    busy = true;
    loadMore.disabled = true;
    loading.hidden = false;
    error.textContent = "";
    render();
    try {
      let loaded = 0;
      while (next && loaded < 5) {
        const result = await getComments(video.id, page, controller.signal);
        const known = new Set(publicItems.map((c) => c.id));
        const fresh = result.items.filter((c) => !known.has(c.id));
        publicItems.push(...fresh);
        render();
        loaded++;
        page++;
        next =
          !!result.next &&
          !!fresh.length &&
          !!result.items.length &&
          publicItems.length < 1000;
      }
      loadMore.hidden = !next;
    } catch (e) {
      if (e.name !== "AbortError") error.textContent = t(e.message);
    } finally {
      busy = false;
      loadMore.disabled = false;
      loading.hidden = true;
      if (!controller.signal.aborted) render();
    }
  }
  load();
  return dialog;
}
