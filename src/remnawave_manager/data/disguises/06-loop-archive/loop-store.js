import { persistentSession } from '../shared/session.js';
const activeSession = persistentSession('loop:session', 'storageError');
let connection;
export function db() {
  if (!connection)
    connection = new Promise((resolve, reject) => {
      let failed = false;
      const req = indexedDB.open("loop:gifs:v1", 2);
      req.onupgradeneeded = () => {
        if (!req.result.objectStoreNames.contains("accounts")) {
          const accounts = req.result.createObjectStore("accounts", {
            keyPath: "id",
          });
          accounts.createIndex("login", "login", { unique: true });
        }
        for (const name of ["likes", "collections", "uploads", "files"])
          if (!req.result.objectStoreNames.contains(name))
            req.result.createObjectStore(name, { keyPath: "id" });
      };
      req.onsuccess = () => {
        if (failed) {
          req.result.close();
          return;
        }
        req.result.onversionchange = () => {
          req.result.close();
          connection = null;
        };
        resolve(req.result);
      };
      req.onerror = req.onblocked = () => {
        failed = true;
        connection = null;
        reject(new Error("storageError"));
      };
    }).catch((error) => {
      connection = null;
      throw error;
    });
  return connection;
}
export async function tx(names, mode, operation) {
  try {
    const database = await db();
    return await new Promise((resolve, reject) => {
      const transaction = database.transaction(names, mode);
      let result;
      try {
        result = operation(transaction);
      } catch {
        transaction.abort();
        reject(new Error("storageError"));
      }
      transaction.oncomplete = () => resolve(result?.result);
      transaction.onabort = transaction.onerror = () =>
        reject(new Error("storageError"));
    });
  } catch {
    throw new Error("storageError");
  }
}
export const get = (name, id) =>
  tx(name, "readonly", (t) => t.objectStore(name).get(id));
export const all = (name) =>
  tx(name, "readonly", (t) => t.objectStore(name).getAll());
export const put = (name, value) =>
  tx(name, "readwrite", (t) => t.objectStore(name).put(value));
export const remove = (name, id) =>
  tx(name, "readwrite", (t) => t.objectStore(name).delete(id));
export const session = () => activeSession.get();
export function signIn(id) { activeSession.set(id); }
export function signOut() { activeSession.set(''); }
export async function current() {
  const id = session();
  if (!id) return null;
  const account = await get("accounts", id);
  if (!account) throw new Error("storageError");
  return account || null;
}
export async function owned(name) {
  const id = session();
  return (await all(name))
    .filter((x) => x.owner === id)
    .sort((a, b) => b.at - a.at);
}
export function mediaSnapshot(media) {
  if (!media.local) return media;
  const { original, preview, video, ...metadata } = media;
  return metadata;
}
export async function toggleLike(media) {
  media = mediaSnapshot(media);
  const owner = session();
  if (!owner) throw new Error("authError");
  const id = `${owner}:${media.id}`;
  return tx(
    media.local ? ["accounts", "likes", "uploads"] : ["accounts", "likes"],
    "readwrite",
    (t) => {
      const likes = t.objectStore("likes");
      const toggle = () => {
        const q = likes.get(id);
        q.onsuccess = () =>
          q.result
            ? likes.delete(id)
            : likes.put({ id, owner, media, at: Date.now() });
      };
      const account = t.objectStore("accounts").get(owner);
      account.onsuccess = () => {
        if (!account.result || session() !== owner) return t.abort();
        if (media.local) {
          const q = t.objectStore("uploads").get(media.id);
          q.onsuccess = () =>
            q.result?.owner === owner ? toggle() : t.abort();
        } else toggle();
      };
    },
  );
}
export async function updateName(id, name) {
  name = name.trim();
  if (!name || name.length > 80) throw new Error("invalid");
  if (!id || session() !== id) throw new Error("authError");
  return tx("accounts", "readwrite", (t) => {
    const accounts = t.objectStore("accounts"),
      q = accounts.get(id);
    q.onsuccess = () => {
      if (!q.result || session() !== id) return t.abort();
      accounts.put({ ...q.result, name });
    };
  });
}
export async function createCollection(name) {
  name = name.trim();
  if (!name || name.length > 60) throw new Error("invalid");
  const owner = session();
  if (!owner) throw new Error("authError");
  const value = {
    id: crypto.randomUUID(),
    owner,
    name,
    items: [],
    at: Date.now(),
  };
  let duplicate = false;
  try {
    await tx(["accounts", "collections"], "readwrite", (t) => {
      const account = t.objectStore("accounts").get(owner);
      account.onsuccess = () => {
        if (!account.result || session() !== owner) return t.abort();
        const collection = t.objectStore("collections"),
          q = collection.getAll();
        q.onsuccess = () => {
          duplicate = q.result.some(
            (c) =>
              c.owner === owner &&
              c.name.toLocaleLowerCase() === name.toLocaleLowerCase(),
          );
          if (duplicate) t.abort();
          else collection.add(value);
        };
      };
    });
  } catch (error) {
    if (duplicate) throw new Error("duplicateCollection");
    throw error;
  }
  return value;
}
export async function editCollection(id, mutate) {
  const owner = session();
  if (!owner) throw new Error("authError");
  let failure;
  try {
    await tx(["collections", "uploads"], "readwrite", (t) => {
      const collections = t.objectStore("collections"),
        request = collections.get(id);
      request.onsuccess = () => {
        if (request.result?.owner !== owner || session() !== owner)
          return t.abort();
        const c = request.result;
        try {
          mutate(c);
        } catch {
          failure = "invalid";
          t.abort();
          return;
        }
        if (
          !c.name.trim() ||
          c.name.trim().length > 60 ||
          c.items.length > 500
        ) {
          failure = "invalid";
          t.abort();
          return;
        }
        c.name = c.name.trim();
        c.items = c.items.map(mediaSnapshot);
        const names = collections.getAll();
        names.onsuccess = () => {
          if (
            names.result.some(
              (x) =>
                x.id !== id &&
                x.owner === owner &&
                x.name.toLocaleLowerCase() === c.name.toLocaleLowerCase(),
            )
          ) {
            failure = "duplicateCollection";
            t.abort();
            return;
          }
          const local = c.items.filter((item) => item.local);
          if (!local.length) {
            collections.put(c);
            return;
          }
          let pending = local.length;
          for (const item of local) {
            const check = t.objectStore("uploads").get(item.id);
            check.onsuccess = () => {
              if (check.result?.owner !== owner) {
                t.abort();
                return;
              }
              if (--pending === 0) collections.put(c);
            };
          }
        };
      };
    });
  } catch (error) {
    if (failure) throw new Error(failure);
    throw error;
  }
}
export async function deleteAccount() {
  const owner = session();
  await tx(
    ["accounts", "likes", "collections", "uploads", "files"],
    "readwrite",
    (t) => {
      t.objectStore("accounts").delete(owner);
      for (const n of ["likes", "collections", "uploads", "files"]) {
        const s = t.objectStore(n),
          r = s.openCursor();
        r.onsuccess = () => {
          const c = r.result;
          if (c) {
            if (c.value.owner === owner) c.delete();
            c.continue();
          }
        };
      }
    },
  );
  signOut();
}
