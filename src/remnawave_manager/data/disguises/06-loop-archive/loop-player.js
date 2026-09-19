import { el, button } from "./loop-ui.js";
import { loadingNode } from '../shared/feedback.js';
import { t } from "./loop-i18n.js";

// Cache only the last derived video, never replace the downloadable original.
let cached;
const aborted = () => new DOMException("Aborted", "AbortError");
function delay(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) return reject(aborted());
    const abort = () => {
      clearTimeout(timer);
      reject(aborted());
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, ms);
    signal.addEventListener("abort", abort, { once: true });
  });
}

async function convert(blob, signal, background) {
  const { GifReader } = await import("./loop-gif-reader.js");
  const reader = new GifReader(new Uint8Array(await blob.arrayBuffer()));
  signal.throwIfAborted();
  if (!reader.numFrames()) throw new Error("Empty animation");
  const mimeType = [
    "video/webm;codecs=vp9",
    "video/webm;codecs=vp8",
    "video/mp4",
  ].find((type) => globalThis.MediaRecorder?.isTypeSupported(type));
  if (!mimeType) throw new Error("MediaRecorder unavailable");
  const canvas = el("canvas", { width: reader.width, height: reader.height });
  const context = canvas.getContext("2d");
  const pixels = context.createImageData(reader.width, reader.height);
  const output = el("canvas", { width: reader.width, height: reader.height });
  const paint = output.getContext("2d");
  let previous, restored;
  function draw(index) {
    if (previous?.disposal === 2) {
      for (let y = previous.y; y < previous.y + previous.height; y++)
        pixels.data.fill(
          0,
          (y * reader.width + previous.x) * 4,
          (y * reader.width + previous.x + previous.width) * 4,
        );
    } else if (previous?.disposal === 3 && restored) pixels.data.set(restored);
    const frame = reader.frameInfo(index);
    if (
      frame.x + frame.width > reader.width ||
      frame.y + frame.height > reader.height
    )
      throw new Error("Invalid frame bounds");
    restored = frame.disposal === 3 ? pixels.data.slice() : null;
    reader.decodeAndBlitFrameRGBA(index, pixels.data);
    context.putImageData(pixels, 0, 0);
    paint.fillStyle = background;
    paint.fillRect(0, 0, output.width, output.height);
    paint.drawImage(canvas, 0, 0);
    previous = frame;
  }
  draw(0);
  const stream = output.captureStream(30);
  const chunks = [];
  let recorder, repaint;
  try {
    recorder = new MediaRecorder(stream, { mimeType });
    const finished = new Promise((resolve, reject) => {
      recorder.addEventListener("dataavailable", (event) => {
        if (event.data.size) chunks.push(event.data);
      });
      recorder.addEventListener(
        "error",
        () => reject(new Error("Recording failed")),
        { once: true },
      );
      recorder.addEventListener("stop", resolve, { once: true });
    });
    // Observe errors immediately, even if recording fails before the frame loop ends.
    finished.catch(() => {});
    recorder.start();
    // A capture stream needs fresh paints even while a GIF frame is held.
    // Otherwise short animations can lose their first frame in Chromium.
    repaint = setInterval(() => {
      paint.fillStyle = background;
      paint.fillRect(0, 0, output.width, output.height);
      paint.drawImage(canvas, 0, 0);
    }, 1000 / 30);
    const start = performance.now();
    let deadline = start;
    for (let i = 0; i < reader.numFrames(); i++) {
      signal.throwIfAborted();
      if (i) draw(i);
      const hundredths = reader.frameInfo(i).delay;
      deadline += hundredths < 2 ? 100 : hundredths * 10;
      await delay(Math.max(0, deadline - performance.now()), signal);
    }
    const duration = performance.now() - start;
    recorder.stop();
    await finished;
    signal.throwIfAborted();
    let result = new Blob(chunks, { type: mimeType });
    if (!result.size) throw new Error("Empty recording");
    if (mimeType.startsWith("video/webm")) {
      const { default: fixDuration } = await import("./loop-webm-duration.js");
      result = await fixDuration(result, duration, { logger: false });
    }
    return result;
  } finally {
    clearInterval(repaint);
    if (recorder && recorder.state !== "inactive") recorder.stop();
    stream.getTracks().forEach((track) => track.stop());
    canvas.width = output.width = 0;
  }
}

export function gifVideoPlayer(item, { title, signal, blob }) {
  const video = el("video", {
    controls: true,
    playsinline: true,
    loop: true,
    preload: "metadata",
    poster: item.preview,
    "aria-label": title,
  });
  video.muted = true;
  const status = el("div", { class: "gif-player-status", role: "status" });
  const root = el("div", { class: "gif-player media-player" }, video, status);
  let job,
    url,
    ready = false;
  const background = getComputedStyle(document.documentElement)
    .getPropertyValue("--hover")
    .trim();
  async function prepare() {
    if (ready || signal.aborted || document.hidden) return;
    job?.abort();
    const current = (job = new AbortController());
    if (url) {
      URL.revokeObjectURL(url);
      url = null;
    }
    video.removeAttribute("src");
    video.load();
    if (!status.isConnected) root.append(status);
    status.replaceChildren(loadingNode(t("preparingPlayer")));
    root.setAttribute("aria-busy", "true");
    try {
      const source = blob
        ? await blob()
        : await fetch(item.original, { signal: current.signal }).then((r) => {
            if (!r.ok) throw new Error("Media unavailable");
            return r.blob();
          });
      current.signal.throwIfAborted();
      const result =
        cached?.src === item.original && cached.background === background
          ? cached.blob
          : await convert(source, current.signal, background);
      current.signal.throwIfAborted();
      cached = { src: item.original, background, blob: result };
      url = URL.createObjectURL(result);
      video.src = url;
      ready = true;
      status.remove();
      root.removeAttribute("aria-busy");
      if (
        document.documentElement.dataset.paused !== "true" &&
        !document.hidden
      )
        video.play().catch(() => {});
    } catch (error) {
      if (current.signal.aborted || signal.aborted) return;
      root.removeAttribute("aria-busy");
      status.replaceChildren(t("playerFailed"), button(t("retry"), prepare));
    }
  }
  video.addEventListener("error", () => {
    if (signal.aborted || !ready) return;
    ready = false;
    cached = null;
    video.pause();
    root.removeAttribute("aria-busy");
    root.append(status);
    status.replaceChildren(t("playerFailed"), button(t("retry"), prepare));
  });
  document.addEventListener(
    "visibilitychange",
    () => {
      if (document.hidden) job?.abort();
      else if (!ready) prepare();
    },
    { signal },
  );
  signal.addEventListener(
    "abort",
    () => {
      job?.abort();
      video.pause();
      video.removeAttribute("src");
      video.load();
      if (url) URL.revokeObjectURL(url);
    },
    { once: true },
  );
  prepare();
  return root;
}
