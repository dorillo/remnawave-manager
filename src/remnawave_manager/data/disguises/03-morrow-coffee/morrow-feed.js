import { el, button } from "./morrow-ui.js";
import { t } from "./morrow-i18n.js";
// Empty fixed-height shells retain the scroll position; only three cards own media.
export function createFeed({
  load,
  initial = null,
  renderCard,
  onActive = () => {},
  onStale = () => {},
  filter = () => true,
}) {
  const root = el("div", {
    class: "feed",
    tabindex: 0,
    "aria-label": t("forYou"),
  });
  const tail = el(
    "div",
    { class: "feed-tail" },
    button(t("more"), () => more()),
  );
  root.append(tail);
  let items = [],
    index = -1,
    busy = false,
    ended = false,
    dead = false,
    repeats = 0,
    controller = new AbortController();
  const mounted = new Map(),
    shells = [];
  function activate(next) {
    if (next === index || dead || !items[next]) return;
    index = next;
    for (const [i, card] of mounted)
      if (Math.abs(i - index) > 1) {
        card.destroy();
        shells[i].replaceChildren();
        mounted.delete(i);
      }
    for (
      let i = Math.max(0, index - 1);
      i <= Math.min(items.length - 1, index + 1);
      i++
    )
      if (!mounted.has(i)) {
        const card = renderCard(items[i]);
        mounted.set(i, card);
        shells[i].append(card.node);
      }
    for (const [i, card] of mounted)
      i === index ? card.activate() : card.deactivate();
    onActive(items[index]);
    if (index >= items.length - 3) more();
  }
  const observer = new IntersectionObserver(
    (entries) => {
      for (const e of entries)
        if (e.isIntersecting && e.intersectionRatio > 0.55)
          activate(Number(e.target.dataset.index));
    },
    { root, threshold: 0.6 },
  );
  function append(fresh) {
    for (const v of fresh) {
      const i = items.length;
      items.push(v);
      const shell = el("article", {
        class: "video-slide",
        "data-index": i,
        "aria-label": `${t("position")} ${i + 1}`,
      });
      shells.push(shell);
      root.insertBefore(shell, tail);
      observer.observe(shell);
    }
  }
  function start() {
    if (initial?.items.length) {
      append(initial.items.filter(filter));
      ended = initial.ended;
      const target = items.findIndex(
        (v) => v.id === initial.items[initial.index]?.id,
      );
      requestAnimationFrame(() => {
        if (dead) return;
        root.scrollTop = Math.max(0, target) * root.clientHeight;
        activate(Math.max(0, target));
      });
      tail.replaceChildren(
        ended ? el("p", {}, t("end")) : button(t("more"), () => more()),
      );
      if (!items.length) {
        ended = false;
        more();
      }
    } else more();
  }
  async function more() {
    if (busy || ended || dead) return;
    busy = true;
    tail.replaceChildren(el("p", {}, t("loading")));
    try {
      const result = await load(controller.signal);
      if (dead) return;
      const known = new Set(items.map((v) => v.id));
      const fresh = result.items
        .filter((v) => !known.has(v.id) && filter(v) && (known.add(v.id), true))
        .slice(0, 500 - items.length);
      repeats = fresh.length ? 0 : repeats + 1;
      ended =
        !result.next || repeats >= 3 || items.length + fresh.length >= 500;
      append(fresh);
      if (index < 0 && items.length) activate(0);
      tail.replaceChildren(
        el("p", {}, t(ended ? (items.length ? "end" : "empty") : "more")),
      );
      if (!ended) tail.append(button(t("more"), () => more()));
      onStale(!!result.stale);
      if (result.stale) tail.prepend(el("p", { role: "status" }, t("stale")));
      if (!items.length && !ended) {
        busy = false;
        return more();
      }
    } catch (e) {
      if (!dead && e.name !== "AbortError")
        tail.replaceChildren(
          el("p", { role: "alert" }, t(e.message)),
          button(t("retry"), () => more()),
        );
    } finally {
      busy = false;
    }
  }
  const key = (e) => {
    if (
      document.querySelector("dialog[open]") ||
      /INPUT|TEXTAREA|SELECT|BUTTON|A/.test(e.target.tagName) ||
      e.ctrlKey ||
      e.metaKey ||
      e.altKey
    )
      return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      move(e.key === "ArrowDown" ? 1 : -1);
    } else if (e.code === "Space") {
      e.preventDefault();
      mounted.get(index)?.toggle();
    } else if (e.key.toLowerCase() === "m") mounted.get(index)?.toggleSound();
  };
  document.addEventListener("keydown", key);
  function move(delta) {
    const next = Math.max(0, Math.min(items.length - 1, index + delta));
    shells[next]?.scrollIntoView({
      behavior: matchMedia("(prefers-reduced-motion: reduce)").matches
        ? "instant"
        : "smooth",
      block: "start",
    });
    if (next === index && delta > 0) more();
  }
  return {
    node: root,
    more,
    start,
    snapshot: () => ({ items: items.slice(), index, ended }),
    loader: load,
    move,
    refreshActions: () => {
      for (const c of mounted.values()) c.refresh?.();
    },
    destroy: () => {
      dead = true;
      controller.abort();
      observer.disconnect();
      document.removeEventListener("keydown", key);
      for (const c of mounted.values()) c.destroy();
    },
    current: () => items[index],
  };
}
