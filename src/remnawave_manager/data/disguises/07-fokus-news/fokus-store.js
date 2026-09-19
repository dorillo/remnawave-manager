const DB = "fokus:ria:v1";
export const empty = () => ({
  accounts: [],
  comments: [],
  reactions: [],
  bookmarks: [],
  history: [],
  cache: [],
});
let connection, opening;
export let available = true;
export const channel =
  typeof BroadcastChannel === "function" ? new BroadcastChannel(DB) : null;
export const session = () => {
  try {
    return sessionStorage.getItem(DB) || "";
  } catch {
    return "";
  }
};
export function signIn(id) {
  try {
    sessionStorage.setItem(DB, id);
  } catch {
    throw new Error("storage");
  }
}
export function signOut() {
  try {
    sessionStorage.removeItem(DB);
  } catch {
    throw new Error("storage");
  }
}
// Remove the entire local thread, including replies from other local profiles.
function removeThreads(state, ids) {
  if (!ids.length) return;
  const removed = new Set(ids.map((id) => `local:${id}`));
  const children = new Map();
  for (const c of state.comments) {
    const parent = c.parent?.ref;
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(`local:${c.id}`);
  }
  for (const ref of removed) {
    for (const child of children.get(ref) || []) removed.add(child);
  }
  state.comments = state.comments.filter((c) => !removed.has(`local:${c.id}`));
  state.reactions = state.reactions.filter((r) => !removed.has(r.target));
}
function normalize(value) {
  const result = empty();
  for (const key of Object.keys(result))
    result[key] = Array.isArray(value?.[key])
      ? value[key].filter((x) => x && typeof x === "object")
      : [];
  const article = (x) =>
    x &&
    typeof x.id === "string" &&
    typeof x.path === "string" &&
    /^\/\d{8}\/[a-z0-9-]+-\d{1,12}\.html$/.test(x.path) &&
    typeof x.title === "string";
  result.accounts = result.accounts.filter(
    (x) =>
      typeof x.id === "string" &&
      typeof x.login === "string" &&
      typeof x.name === "string" &&
      typeof x.salt === "string" &&
      typeof x.hash === "string",
  );
  result.comments = result.comments.filter(
    (x) =>
      typeof x.id === "string" &&
      typeof x.articleId === "string" &&
      typeof x.text === "string" &&
      article(x.article),
  );
  for (const comment of result.comments) {
    if (
      comment.parent &&
      (typeof comment.parent.ref !== "string" ||
        typeof comment.parent.name !== "string" ||
        typeof comment.parent.text !== "string")
    )
      comment.parent = null;
  }
  result.reactions = result.reactions.filter(
    (x) =>
      typeof x.accountId === "string" &&
      typeof x.target === "string" &&
      /^s[1-6]$/.test(x.code),
  );
  for (const key of ["bookmarks", "history"])
    result[key] = result[key].filter(
      (x) => typeof x.accountId === "string" && article(x.article),
    );
  result.cache = result.cache.filter(
    (x) =>
      typeof x.key === "string" &&
      Number.isFinite(x.at) &&
      x.value &&
      typeof x.value === "object",
  );
  removeThreads(
    result,
    result.comments.filter((c) => c.deleted).map((c) => c.id),
  );
  return result;
}
async function open() {
  if (connection) return connection;
  if (opening) return opening;
  opening = new Promise((resolve, reject) => {
    const r = indexedDB.open(DB, 1);
    let blocked = false;
    r.onupgradeneeded = () => r.result.createObjectStore("state");
    r.onsuccess = () => {
      if (blocked) {
        r.result.close();
        return;
      }
      const db = r.result;
      connection = db;
      db.onversionchange = () => {
        db.close();
        connection = null;
      };
      db.onclose = () => {
        if (connection === db) connection = null;
      };
      resolve(db);
    };
    r.onerror = () => reject(r.error);
    r.onblocked = () => {
      blocked = true;
      reject(new Error("storage"));
    };
  });
  try {
    return await opening;
  } finally {
    opening = null;
  }
}
export async function read() {
  try {
    const db = await open();
    return await new Promise((resolve, reject) => {
      const r = db.transaction("state").objectStore("state").get("data");
      r.onsuccess = () => {
        try {
          const state = normalize(r.result);
          available = true;
          resolve(state);
        } catch (error) {
          reject(error);
        }
      };
      r.onerror = () => reject(r.error);
    });
  } catch {
    available = false;
    return empty();
  }
}
export async function change(fn, notify = true) {
  try {
    const db = await open();
    const result = await new Promise((resolve, reject) => {
      const tx = db.transaction("state", "readwrite"),
        store = tx.objectStore("state"),
        r = store.get("data");
      let result;
      r.onsuccess = () => {
        try {
          const data = normalize(r.result);
          result = fn(data);
          store.put(data, "data");
        } catch (e) {
          reject(e);
          tx.abort();
        }
      };
      tx.oncomplete = () => resolve(result);
      tx.onerror = tx.onabort = () => reject(tx.error || new Error("storage"));
    });
    if (notify) channel?.postMessage("changed");
    return result;
  } catch (e) {
    if (!["duplicate", "authError", "invalid"].includes(e.message))
      throw new Error("storage");
    throw e;
  }
}
export async function cached(key) {
  const state = await read();
  const c = state.cache.find((x) => x.key === key);
  return c && Date.now() - c.at < 7 * 864e5 ? c : null;
}
export async function cache(key, value) {
  try {
    await change((s) => {
      s.cache = s.cache.filter((x) => x.key !== key);
      s.cache.push({ key, value, at: Date.now() });
      s.cache = s.cache.slice(-30);
      while (JSON.stringify(s.cache).length > 10 * 1024 * 1024) s.cache.shift();
    }, false);
  } catch {
    /* A cache failure must not interrupt reading. */
  }
}
export const account = (s) => s.accounts.find((x) => x.id === session());
export function requireAccount(s) {
  const a = account(s);
  if (!a) throw new Error("authError");
  return a;
}
export async function react(target, code) {
  await change((s) => {
    const a = requireAccount(s);
    const old = s.reactions.find(
      (r) => r.accountId === a.id && r.target === target,
    );
    s.reactions = s.reactions.filter(
      (r) => r.accountId !== a.id || r.target !== target,
    );
    if (old?.code !== code) s.reactions.push({ accountId: a.id, target, code });
  });
}
export async function save(article) {
  await change((s) => {
    const a = requireAccount(s);
    const old = s.bookmarks.some(
      (b) => b.accountId === a.id && b.article.id === article.id,
    );
    s.bookmarks = s.bookmarks.filter(
      (b) => b.accountId !== a.id || b.article.id !== article.id,
    );
    if (!old) s.bookmarks.unshift({ accountId: a.id, article, at: Date.now() });
  });
}
export async function visit(article) {
  if (!session()) return;
  await change((s) => {
    const a = account(s);
    if (!a) return;
    s.history = s.history.filter(
      (b) => b.accountId !== a.id || b.article.id !== article.id,
    );
    s.history.unshift({ accountId: a.id, article, at: Date.now() });
    s.history = s.history.filter(
      (b, i, all) =>
        all.slice(0, i).filter((x) => x.accountId === b.accountId).length < 100,
    );
  }, false);
}
export async function comment(article, text, parent = null, id = "") {
  text = text.trim();
  if (!text || text.length > 3000) throw new Error("invalid");
  await change((s) => {
    const a = requireAccount(s);
    if (id) {
      const c = s.comments.find((x) => x.id === id && x.authorId === a.id);
      if (!c) throw new Error("invalid");
      c.text = text;
      c.edited = Date.now();
    } else {
      if (
        parent?.ref?.startsWith("local:") &&
        !s.comments.some(
          (c) => `local:${c.id}` === parent.ref && c.articleId === article.id,
        )
      )
        throw new Error("invalid");
      s.comments.push({
        id: crypto.randomUUID(),
        articleId: article.id,
        article,
        authorId: a.id,
        text,
        parent,
        at: Date.now(),
      });
    }
  });
}
export async function removeComment(id) {
  await change((s) => {
    const a = requireAccount(s),
      c = s.comments.find((x) => x.id === id && x.authorId === a.id);
    if (!c) throw new Error("invalid");
    removeThreads(s, [id]);
  });
}
export async function removeAccount() {
  await change((s) => {
    const a = requireAccount(s);
    s.accounts = s.accounts.filter((x) => x.id !== a.id);
    for (const k of ["bookmarks", "history", "reactions"])
      s[k] = s[k].filter((x) => x.accountId !== a.id);
    removeThreads(
      s,
      s.comments.filter((c) => c.authorId === a.id).map((c) => c.id),
    );
  });
  signOut();
}
