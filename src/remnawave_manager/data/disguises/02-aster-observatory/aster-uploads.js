const DATABASE = 'aster:media:v1';
const STORE = 'videos';
export const MAX_VIDEO_SIZE = 500 * 1024 * 1024;

function database() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DATABASE, 1);
    request.onupgradeneeded = () => {
      const store = request.result.createObjectStore(STORE, { keyPath: 'id' });
      store.createIndex('ownerId', 'ownerId');
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
    request.onblocked = () => reject(new Error('storageError'));
  });
}

function requestResult(request) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

async function withStore(mode, action) {
  const db = await database();
  try {
    const transaction = db.transaction(STORE, mode);
    const completed = new Promise((resolve, reject) => {
      transaction.oncomplete = resolve;
      transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error);
    });
    const result = await action(transaction.objectStore(STORE));
    await completed;
    return result;
  } finally {
    db.close();
  }
}

function videoDetails(file) {
  return new Promise((resolve, reject) => {
    const source = URL.createObjectURL(file);
    const video = document.createElement('video');
    let settled = false;
    let metadata = null;
    const finish = (details, error = false) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      URL.revokeObjectURL(source);
      video.removeAttribute('src');
      video.load();
      if (error) reject(new Error('videoError'));
      else resolve(details || metadata || {});
    };
    const capture = () => {
      try {
        const canvas = document.createElement('canvas');
        canvas.width = 640;
        canvas.height = 360;
        const scale = Math.max(canvas.width / video.videoWidth, canvas.height / video.videoHeight);
        const width = video.videoWidth * scale;
        const height = video.videoHeight * scale;
        canvas
          .getContext('2d')
          .drawImage(video, (canvas.width - width) / 2, (canvas.height - height) / 2, width, height);
        finish({
          duration: Number.isFinite(video.duration) ? Math.round(video.duration) : 0,
          poster: canvas.toDataURL('image/jpeg', 0.78),
        });
      } catch {
        finish({ duration: Number.isFinite(video.duration) ? Math.round(video.duration) : 0 });
      }
    };
    video.preload = 'metadata';
    video.muted = true;
    video.playsInline = true;
    video.onloadedmetadata = () => {
      if (
        !video.videoWidth ||
        !video.videoHeight ||
        !Number.isFinite(video.duration) ||
        video.duration <= 0
      )
        return finish(null, true);
      metadata = { duration: Math.round(video.duration) };
      video.currentTime = Math.min(2, Math.max(0, video.duration / 10 || 0));
    };
    video.onseeked = capture;
    video.onerror = () => finish(null, true);
    const timer = setTimeout(() => finish(metadata, !metadata), 8000);
    video.src = source;
  });
}

export async function saveVideo(ownerId, file, title, description = '') {
  if (
    typeof ownerId !== 'string' ||
    !file?.type?.startsWith('video/') ||
    !file.size ||
    file.size > MAX_VIDEO_SIZE ||
    !title.trim()
  )
    throw new Error('videoError');
  const details = await videoDetails(file);
  const record = {
    id: crypto.randomUUID().replaceAll('-', ''),
    ownerId,
    title: title.trim().slice(0, 200),
    description: description.trim().slice(0, 2000),
    createdAt: Date.now(),
    duration: details.duration || 0,
    poster: details.poster || '',
    views: 0,
    size: file.size,
    type: file.type,
    file,
  };
  await withStore('readwrite', (store) => requestResult(store.add(record)));
  return record;
}

export async function videosFor(ownerId) {
  const records = await withStore('readonly', (store) =>
    requestResult(store.index('ownerId').getAll(ownerId)),
  );
  return records.sort((a, b) => b.createdAt - a.createdAt);
}

export async function openVideo(ownerId, id) {
  return withStore('readwrite', async (store) => {
    const record = await requestResult(store.get(id));
    if (!record || record.ownerId !== ownerId) return null;
    record.views = Math.min(Number.MAX_SAFE_INTEGER, Math.max(0, record.views || 0) + 1);
    await requestResult(store.put(record));
    return record;
  });
}

export async function removeVideo(ownerId, id) {
  return withStore('readwrite', async (store) => {
    const record = await requestResult(store.get(id));
    if (!record || record.ownerId !== ownerId) throw new Error('videoNotFound');
    return requestResult(store.delete(id));
  });
}
