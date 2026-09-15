import { el, iconButton, select, icon } from "./morrow-ui.js";
import { t } from "./morrow-i18n.js";
import { getVideo } from "./morrow-data.js";
let muted = true;
const players = new Set();
export function pauseAll() {
  for (const p of players) p.pause();
}
export function createPlayer(
  item,
  { economy = false, onPlay = () => {} } = {},
) {
  let active = false,
    destroyed = false,
    refreshed = false,
    manualPause = false;
  const controller = new AbortController();
  const video = el("video", {
    playsinline: true,
    loop: true,
    preload: economy ? "none" : "metadata",
    poster: item.poster || null,
    "aria-label": item.description.slice(0, 100) || t("play"),
  });
  video.muted = muted;
  const wrapper = el("div", { class: "video-surface" }, video);
  const feedback = el("div", { class: "video-feedback", hidden: true });
  const play = iconButton("play", t("play"), togglePlay, "round play-control");
  const sound = iconButton(
    muted ? "mute" : "volume",
    t(muted ? "unmute" : "mute"),
    toggleSound,
    "round",
  );
  const progress = el("input", {
    type: "range",
    min: 0,
    max: 100,
    step: 0.1,
    value: 0,
    "aria-label": t("progress"),
    class: "video-progress",
    oninput: () => {
      if (Number.isFinite(video.duration))
        video.currentTime = (video.duration * Number(progress.value)) / 100;
    },
  });
  const choices = Object.keys(item.sources);
  let quality = economy
    ? choices.includes("sd")
      ? "sd"
      : choices[0]
    : choices.includes("hd")
      ? "hd"
      : choices[0];
  const qualitySelect = select(
    choices.map((k) => [
      k,
      k === "fullHd" ? "1080p" : k === "hd" ? "720p" : "480p",
    ]),
    quality,
    (k) => {
      const time = video.currentTime;
      quality = k;
      video.src = item.sources[k];
      video.addEventListener(
        "loadedmetadata",
        () => {
          video.currentTime = time;
          if (active && !manualPause) start();
        },
        { once: true },
      );
    },
  );
  qualitySelect.setAttribute("aria-label", t("quality"));
  const controls = el(
    "div",
    { class: "player-controls" },
    play,
    sound,
    qualitySelect,
  );
  wrapper.append(controls, feedback, progress);
  const api = {
    node: wrapper,
    video,
    pause: () => video.pause(),
    activate: () => {
      active = true;
      manualPause = false;
      if (!video.getAttribute("src")) video.src = item.sources[quality];
      start();
    },
    deactivate: () => {
      active = false;
      video.pause();
    },
    destroy: () => {
      destroyed = true;
      active = false;
      controller.abort();
      video.pause();
      video.removeAttribute("src");
      video.load();
      players.delete(api);
    },
    toggle: togglePlay,
    toggleSound,
  };
  players.add(api);
  async function start() {
    if (!active || destroyed || document.hidden) return;
    for (const p of players) if (p !== api) p.pause();
    try {
      await video.play();
      if (!active || destroyed) video.pause();
    } catch {
      if (!destroyed) {
        play.hidden = false;
      }
    }
  }
  function togglePlay() {
    if (video.paused) {
      manualPause = false;
      start();
    } else {
      manualPause = true;
      video.pause();
    }
  }
  function toggleSound() {
    muted = !muted;
    for (const p of players) p.video.muted = muted;
  }
  // Keep every player's sound control current without coupling their DOM lifetimes.
  video.addEventListener("volumechange", () => {
    const old = controls.children[1];
    old.replaceWith(
      iconButton(
        video.muted ? "mute" : "volume",
        t(video.muted ? "unmute" : "mute"),
        () => {
          muted = !muted;
          for (const p of players) p.video.muted = muted;
        },
        "round",
      ),
    );
  });
  video.addEventListener("click", togglePlay);
  video.addEventListener("playing", () => {
    if (!active) {
      video.pause();
      return;
    }
    feedback.hidden = true;
    play.replaceChildren(icon("pause"));
    play.setAttribute("aria-label", t("pause"));
    onPlay(item);
  });
  video.addEventListener("pause", () => {
    play.replaceChildren(icon("play"));
    play.setAttribute("aria-label", t("play"));
  });
  video.addEventListener("timeupdate", () => {
    if (Number.isFinite(video.duration))
      progress.value = (video.currentTime / video.duration) * 100;
  });
  video.addEventListener("error", async () => {
    if (destroyed) return;
    if (!refreshed) {
      refreshed = true;
      try {
        const latest = await getVideo(item.id, controller.signal, true);
        if (destroyed) return;
        item.sources = latest.sources;
        quality = latest.sources[quality]
          ? quality
          : Object.keys(latest.sources)[0];
        video.src = latest.sources[quality];
        if (active) start();
        return;
      } catch {}
    }
    if (!destroyed) {
      feedback.hidden = false;
      feedback.replaceChildren(
        el("p", {}, t("unavailable")),
        iconButton(
          "play",
          t("retry"),
          () => {
            feedback.hidden = true;
            refreshed = false;
            video.load();
            start();
          },
          "round",
        ),
      );
    }
  });
  return api;
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) pauseAll();
});
