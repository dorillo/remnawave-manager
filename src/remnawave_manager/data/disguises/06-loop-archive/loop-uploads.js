import * as store from "./loop-store.js";

const urls = new Map();
let urlOwner;
export const isLocal = (id) => /^local-[0-9a-f-]{36}$/.test(String(id));
export function release(id) {
  for (const [key, value] of urls) {
    if (!id || key === id) {
      URL.revokeObjectURL(value.original);
      URL.revokeObjectURL(value.preview);
      urls.delete(key);
    }
  }
}
export async function list() {
  const owner = store.session();
  if (urlOwner !== owner) {
    release();
    urlOwner = owner;
  }
  return owner ? store.owned("uploads") : [];
}
export async function item(id) {
  const owner = store.session(),
    metadata = await store.get("uploads", id);
  if (!owner || metadata?.owner !== owner) throw new Error("notFound");
  return metadata;
}
export async function resolve(id) {
  const metadata = await item(id);
  if (urls.has(id)) return { ...metadata, ...urls.get(id) };
  const file = await store.get("files", id);
  if (!file || file.owner !== store.session()) throw new Error("notFound");
  // Recheck after the asynchronous read: another request may already have resolved it.
  if (!urls.has(id)) {
    const original = URL.createObjectURL(file.blob),
      preview = URL.createObjectURL(file.poster);
    urls.set(id, {
      original,
      preview,
      video: metadata.type === "clip" ? original : "",
    });
  }
  return { ...metadata, ...urls.get(id) };
}
export async function sourceBlob(id) {
  await item(id);
  const file = await store.get("files", id);
  if (!file || file.owner !== store.session()) throw new Error("notFound");
  return file.blob;
}
async function inspect(file, type, signal) {
  const bytes = new Uint8Array(await file.slice(0, 16).arrayBuffer());
  const ascii = (start, end) => String.fromCharCode(...bytes.slice(start, end));
  let mime = "",
    extension = "";
  if (/^GIF8[79]a$/.test(ascii(0, 6))) {
    mime = "image/gif";
    extension = "gif";
  } else if (
    bytes[0] === 137 &&
    ascii(1, 4) === "PNG" &&
    bytes[4] === 13 &&
    bytes[5] === 10 &&
    bytes[6] === 26 &&
    bytes[7] === 10
  ) {
    mime = "image/png";
    extension = "png";
  } else if (ascii(0, 4) === "RIFF" && ascii(8, 12) === "WEBP") {
    mime = "image/webp";
    extension = "webp";
  } else if (bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255) {
    mime = "image/jpeg";
    extension = "jpg";
  } else if (ascii(4, 8) === "ftyp") {
    mime = "video/mp4";
    extension = "mp4";
  } else if (
    bytes[0] === 26 &&
    bytes[1] === 69 &&
    bytes[2] === 223 &&
    bytes[3] === 163
  ) {
    mime = "video/webm";
    extension = "webm";
  }
  if (
    !mime ||
    !["gif", "sticker", "clip"].includes(type) ||
    (type === "gif" && extension !== "gif") ||
    (type === "clip") !== mime.startsWith("video/")
  )
    throw new Error("uploadFormat");
  if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
  const blob = file.slice(0, file.size, mime),
    url = URL.createObjectURL(blob);
  const media = document.createElement(type === "clip" ? "video" : "img");
  try {
    await new Promise((resolve, reject) => {
      const finish = (error) => {
        media.onload = media.onloadeddata = media.onerror = null;
        signal?.removeEventListener("abort", abort);
        error ? reject(error) : resolve();
      };
      const abort = () => finish(new DOMException("Aborted", "AbortError"));
      signal?.addEventListener("abort", abort, { once: true });
      media.onerror = () => finish(new Error("uploadDecode"));
      if (type === "clip") {
        media.preload = "auto";
        media.muted = true;
        media.playsInline = true;
        media.onloadeddata = () => finish();
      } else media.onload = () => finish();
      media.src = url;
    });
    const width = media.videoWidth || media.naturalWidth,
      height = media.videoHeight || media.naturalHeight;
    if (!width || !height) throw new Error("uploadDecode");
    const canvas = document.createElement("canvas"),
      scale = Math.min(1, 480 / Math.max(width, height));
    canvas.width = Math.max(1, Math.round(width * scale));
    canvas.height = Math.max(1, Math.round(height * scale));
    canvas.getContext("2d").drawImage(media, 0, 0, canvas.width, canvas.height);
    const poster = await new Promise((resolve) =>
      canvas.toBlob(resolve, "image/png"),
    );
    if (!poster) throw new Error("uploadDecode");
    return { blob, poster, width, height, extension };
  } finally {
    if (type === "clip") media.pause();
    media.removeAttribute("src");
    if (type === "clip") media.load();
    URL.revokeObjectURL(url);
  }
}
export async function save({ file, type, title, tags, category }, signal) {
  const owner = store.session(),
    account = await store.current();
  if (!account || account.id !== owner) throw new Error("authError");
  title = title.trim();
  if (
    !file ||
    !title ||
    title.length > 200 ||
    !Array.isArray(tags) ||
    tags.length > 20 ||
    tags.some((tag) => typeof tag !== "string" || tag.length > 120)
  )
    throw new Error("uploadInvalid");
  const inspected = await inspect(file, type, signal);
  if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
  if (store.session() !== owner) throw new Error("authError");
  const id = `local-${crypto.randomUUID()}`;
  const metadata = {
    id,
    owner,
    local: true,
    type,
    title,
    tags,
    category: /^\d{1,12}$/.test(category) ? category : "",
    author: account.login,
    width: inspected.width,
    height: inspected.height,
    extension: inspected.extension,
    at: Date.now(),
  };
  // Metadata, original and poster are committed together. No application file-size cap.
  await store.tx(["accounts", "uploads", "files"], "readwrite", (tx) => {
    const request = tx.objectStore("accounts").get(owner);
    request.onsuccess = () => {
      if (!request.result || signal?.aborted) {
        tx.abort();
        return;
      }
      tx.objectStore("uploads").add(metadata);
      tx.objectStore("files").add({
        id,
        owner,
        blob: inspected.blob,
        poster: inspected.poster,
      });
    };
  });
  return metadata;
}
export async function remove(id) {
  const owner = store.session();
  if (!owner) throw new Error("authError");
  await store.tx(
    ["uploads", "files", "likes", "collections"],
    "readwrite",
    (tx) => {
      const request = tx.objectStore("uploads").get(id);
      request.onsuccess = () => {
        if (request.result?.owner !== owner) {
          tx.abort();
          return;
        }
        tx.objectStore("uploads").delete(id);
        tx.objectStore("files").delete(id);
        for (const name of ["likes", "collections"]) {
          const cursor = tx.objectStore(name).openCursor();
          cursor.onsuccess = () => {
            const row = cursor.result;
            if (!row) return;
            if (name === "likes" && row.value.media.id === id) row.delete();
            if (
              name === "collections" &&
              row.value.items.some((media) => media.id === id)
            ) {
              row.update({
                ...row.value,
                items: row.value.items.filter((media) => media.id !== id),
              });
            }
            row.continue();
          };
        }
      };
    },
  );
  release(id);
}
export function matching(
  list,
  { type, query = "", category = "", related } = {},
) {
  const words = query.toLocaleLowerCase().trim().split(/\s+/).filter(Boolean);
  return list
    .filter(
      (item) =>
        (!type || item.type === type) &&
        (!category || item.category === category) &&
        item.id !== related?.id &&
        words.every((word) =>
          `${item.title} ${item.tags.join(" ")} ${item.author}`
            .toLocaleLowerCase()
            .includes(word),
        ),
    )
    .sort((a, b) => {
      if (related) {
        const score = (item) =>
          item.tags.filter((tag) =>
            related.tags.some(
              (other) => other.toLocaleLowerCase() === tag.toLocaleLowerCase(),
            ),
          ).length;
        const difference = score(b) - score(a);
        if (difference) return difference;
      }
      return b.at - a.at;
    });
}
export function interleave(remote, local) {
  const result = [...remote];
  local.forEach((item, index) =>
    result.splice(Math.min(2 + index * 5, result.length), 0, item),
  );
  return result;
}

// Cursor contains metadata only; local files never enter a network request.
export async function mixedFeed(options, cursor, local, fetchRemote, signal) {
  const previous = cursor || {
    remoteSkip: 0,
    remoteMore: true,
    localSkip: 0,
    buffer: [],
  };
  let buffer = [...previous.buffer],
    remoteMore = previous.remoteMore,
    remoteSkip = previous.remoteSkip;
  let remoteError = false;
  if (buffer.length < 48 && remoteMore) {
    try {
      const result = await fetchRemote(
        { ...options, skip: remoteSkip },
        signal,
      );
      buffer.push(...result.items);
      remoteMore = result.more;
      remoteSkip = result.nextSkip;
    } catch (error) {
      if (
        signal?.aborted ||
        (!buffer.length && previous.localSkip >= local.length)
      )
        throw error;
      remoteError = true;
    }
  }
  const additions = local.slice(
    previous.localSkip,
    previous.localSkip + (buffer.length ? 8 : 48),
  );
  const count = 48 - additions.length;
  const items = interleave(buffer.slice(0, count), additions);
  buffer = buffer.slice(count);
  const next = {
    remoteSkip,
    remoteMore,
    buffer,
    localSkip: previous.localSkip + additions.length,
  };
  return {
    items,
    cursor: next,
    more: remoteMore || buffer.length > 0 || next.localSkip < local.length,
    remoteError,
  };
}
