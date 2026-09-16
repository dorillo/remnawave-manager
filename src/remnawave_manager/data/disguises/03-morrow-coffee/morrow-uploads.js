const DATABASE = "morrow:media:v1";
const STORE = "videos";

function database() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE, 1);
    request.onupgradeneeded = () => {
      const store = request.result.createObjectStore(STORE, { keyPath: "id" });
      store.createIndex("ownerId", "ownerId");
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = request.onblocked = () => reject(new Error("storage"));
  });
}
function result(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(new Error("storage"));
  });
}
async function withStore(mode, action) {
  const db = await database();
  try {
    const tx = db.transaction(STORE, mode);
    const complete = new Promise((resolve, reject) => {
      tx.oncomplete = resolve;
      tx.onerror = tx.onabort = () => reject(new Error("storage"));
    });
    const value = await action(tx.objectStore(STORE));
    await complete;
    return value;
  } finally {
    db.close();
  }
}
function details(file) {
  return new Promise((resolve, reject) => {
    const source = URL.createObjectURL(file);
    const video = document.createElement("video");
    let complete = false,
      metadata = null;
    const finish = (value, failed = false) => {
      if (complete) return;
      complete = true;
      clearTimeout(timer);
      URL.revokeObjectURL(source);
      video.removeAttribute("src");
      video.load();
      if (failed) reject(new Error("videoError"));
      else resolve(value);
    };
    video.preload = "metadata";
    video.muted = true;
    video.playsInline = true;
    video.onloadedmetadata = () => {
      if (!video.videoWidth || !video.videoHeight || !Number.isFinite(video.duration))
        return finish(null, true);
      metadata = { duration: Math.round(video.duration), poster: "" };
      try {
        video.currentTime = Math.min(2, Math.max(0, video.duration / 10));
      } catch {
        finish(metadata);
      }
      video.onseeked = () => {
        try {
          const canvas = document.createElement("canvas");
          canvas.width = 360;
          canvas.height = 640;
          const scale = Math.max(360 / video.videoWidth, 640 / video.videoHeight);
          const width = video.videoWidth * scale;
          const height = video.videoHeight * scale;
          canvas.getContext("2d").drawImage(video, (360 - width) / 2, (640 - height) / 2, width, height);
          metadata.poster = canvas.toDataURL("image/jpeg", 0.78);
        } catch {}
        finish(metadata);
      };
    };
    video.onerror = () => finish(null, true);
    const timer = setTimeout(() => finish(metadata, !metadata), 3000);
    video.src = source;
  });
}
export async function saveVideo(ownerId, file, description) {
  if (
    typeof ownerId !== "string" ||
    !file?.type?.startsWith("video/") ||
    !file.size
  )
    throw new Error("videoError");
  const media = await details(file);
  const record = {
    id: crypto.randomUUID().replaceAll("-", ""),
    ownerId,
    description: String(description || "").trim().slice(0, 5000),
    createdAt: Date.now(),
    duration: media.duration,
    poster: media.poster,
    views: 0,
    file,
  };
  await withStore("readwrite", (store) => result(store.add(record)));
  return record;
}
export async function videosFor(ownerId) {
  const records = await withStore("readonly", (store) =>
    result(store.index("ownerId").getAll(ownerId)),
  );
  return records.sort((a, b) => b.createdAt - a.createdAt);
}
export async function openVideo(ownerId, id) {
  return withStore("readonly", async (store) => {
    const record = await result(store.get(id));
    return record?.ownerId === ownerId ? record : null;
  });
}
export async function removeVideo(ownerId, id) {
  return withStore("readwrite", async (store) => {
    const record = await result(store.get(id));
    if (!record || record.ownerId !== ownerId) throw new Error("videoNotFound");
    await result(store.delete(id));
  });
}
export async function removeOwnerVideos(ownerId) {
  return withStore("readwrite", async (store) => {
    const ids = await result(store.index("ownerId").getAllKeys(ownerId));
    for (const id of ids) store.delete(id);
  });
}
export async function recordView(ownerId, id) {
  return withStore("readwrite", async (store) => {
    const record = await result(store.get(id));
    if (!record || record.ownerId !== ownerId) throw new Error("videoNotFound");
    record.views = Math.max(0, Number(record.views) || 0) + 1;
    await result(store.put(record));
    return record.views;
  });
}
