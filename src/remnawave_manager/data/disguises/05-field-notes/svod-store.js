const DB = "svod:wikipedia:v1";
let connection;
export function database() {
  return (connection ||= new Promise((resolve, reject) => {
    const request = indexedDB.open(DB, 1);
    request.onupgradeneeded = () => {
      for (const name of [
        "accounts",
        "library",
        "history",
        "collections",
        "cache",
      ]) {
        const s = request.result.createObjectStore(name, { keyPath: "id" });
        if (name === "accounts")
          s.createIndex("login", "login", { unique: true });
      }
    };
    request.onsuccess = () => {
      request.result.onversionchange = () => request.result.close();
      resolve(request.result);
    };
    request.onerror = request.onblocked = () =>
      reject(new Error("storageError"));
  }));
}
export async function transaction(store, mode, operation) {
  const db = await database();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(store, mode);
    let result;
    try {
      result = operation(tx.objectStore(store));
    } catch {
      tx.abort();
      reject(new Error("storageError"));
      return;
    }
    tx.oncomplete = () => resolve(result?.result);
    tx.onerror = tx.onabort = () => reject(new Error("storageError"));
  });
}
export const list = (store) =>
  transaction(store, "readonly", (s) => s.getAll());
export const get = (store, id) =>
  transaction(store, "readonly", (s) => s.get(id));
export const put = (store, value) =>
  transaction(store, "readwrite", (s) => s.put(value));
export const remove = (store, id) =>
  transaction(store, "readwrite", (s) => s.delete(id));
export function session() {
  try {
    return sessionStorage.getItem("svod:wikipedia:session");
  } catch {
    return null;
  }
}
export async function current() {
  const id = session();
  if (!id) return null;
  const value = await get("accounts", id);
  if (
    value &&
    (!value.name ||
      typeof value.name !== "string" ||
      typeof value.login !== "string" ||
      !value.password)
  )
    throw new Error("storageError");
  return value;
}
export function signOut() {
  sessionStorage.removeItem("svod:wikipedia:session");
}
export function signIn(id) {
  sessionStorage.setItem("svod:wikipedia:session", id);
}
export const owned = async (store) => {
  const rows = (await list(store)).filter((x) => x.owner === session());
  if (
    rows.some((x) =>
      store === "collections"
        ? typeof x.name !== "string" || !Array.isArray(x.pages)
        : typeof x.title !== "string" ||
          !Number.isSafeInteger(x.pageid) ||
          !Number.isFinite(x.at),
    )
  )
    throw new Error("storageError");
  return rows;
};
export async function deleteOwned(store, owner) {
  const db = await database();
  await new Promise((resolve, reject) => {
    const tx = db.transaction(store, "readwrite"),
      s = tx.objectStore(store),
      r = s.openCursor();
    r.onsuccess = () => {
      const c = r.result;
      if (c) {
        if (c.value.owner === owner) c.delete();
        c.continue();
      }
    };
    tx.oncomplete = resolve;
    tx.onerror = () => reject(new Error("storageError"));
  });
}
export async function deleteAccount() {
  const id = session(),
    db = await database();
  await new Promise((resolve, reject) => {
    const tx = db.transaction(
      ["accounts", "library", "history", "collections"],
      "readwrite",
    );
    tx.objectStore("accounts").delete(id);
    for (const key of ["library", "history", "collections"]) {
      const r = tx.objectStore(key).openCursor();
      r.onsuccess = () => {
        const c = r.result;
        if (c) {
          if (c.value.owner === id) c.delete();
          c.continue();
        }
      };
    }
    tx.oncomplete = resolve;
    tx.onerror = () => reject(new Error("storageError"));
  });
  signOut();
}
export function record(article, owner = session()) {
  return {
    id: `${owner}:${article.pageid}`,
    owner,
    pageid: article.pageid,
    title: article.title,
    at: Date.now(),
  };
}
export async function saveArticle(article) {
  const owner = session();
  if (!owner) throw new Error("authError");
  await put("library", record(article, owner));
}
export async function remember(
  article,
  position = 0,
  owner = session(),
  section = "",
) {
  if (!owner) return;
  await put("history", {
    ...record(article, owner),
    position: Math.max(0, Math.min(1, position)),
    section,
  });
}
export async function cacheArticle(article) {
  try {
    const entry = {
      id: article.title,
      article,
      at: Date.now(),
      size: JSON.stringify(article).length * 2,
    };
    if (entry.size > 4 * 1024 * 1024) return;
    await put("cache", entry);
    const rows = (await list("cache")).sort((a, b) => b.at - a.at);
    let total = 0;
    for (let i = 0; i < rows.length; i++) {
      total += rows[i].size || 0;
      if (i >= 30 || total > 20 * 1024 * 1024)
        await remove("cache", rows[i].id);
    }
  } catch {
    /* Cache failure does not prevent reading. */
  }
}

export async function removeSaved(id) {
  const owner = session(),
    record = await get("library", id);
  if (!record || record.owner !== owner) return;
  const db = await database();
  await new Promise((resolve, reject) => {
    const tx = db.transaction(["library", "collections"], "readwrite");
    tx.objectStore("library").delete(id);
    const r = tx.objectStore("collections").openCursor();
    r.onsuccess = () => {
      const c = r.result;
      if (c) {
        if (c.value.owner === owner && Array.isArray(c.value.pages))
          c.update({
            ...c.value,
            pages: c.value.pages.filter((p) => p !== record.pageid),
          });
        c.continue();
      }
    };
    tx.oncomplete = resolve;
    tx.onerror = () => reject(new Error("storageError"));
  });
}
