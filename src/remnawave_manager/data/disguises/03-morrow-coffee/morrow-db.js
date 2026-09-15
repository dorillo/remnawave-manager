let connection;
export function openDatabase() {
  if (!connection)
    connection = new Promise((resolve, reject) => {
      const request = indexedDB.open("morrow:workspace:v1", 2);
      request.onupgradeneeded = () => {
        const db = request.result;
        if (!db.objectStoreNames.contains("accounts")) {
          db.createObjectStore("accounts", { keyPath: "id" }).createIndex(
            "login",
            "login",
            { unique: true },
          );
          db.createObjectStore("workspaces", { keyPath: "id" });
        }
        if (!db.objectStoreNames.contains("videos"))
          db.createObjectStore("videos", { keyPath: "id" });
      };
      request.onerror = request.onblocked = () => {
        connection = null;
        reject(new Error("storage"));
      };
      request.onsuccess = () => {
        request.result.onversionchange = () => {
          request.result.close();
          connection = null;
        };
        resolve(request.result);
      };
    });
  return connection;
}
export async function transaction(stores, mode, work) {
  const db = await openDatabase();
  return new Promise((resolve, reject) => {
    let tx;
    try {
      tx = db.transaction(stores, mode);
    } catch {
      reject(new Error("storage"));
      return;
    }
    let result, failure;
    tx.oncomplete = () => resolve(result);
    tx.onabort = tx.onerror = () => reject(failure || new Error("storage"));
    try {
      work(
        tx,
        (value) => {
          result = value;
        },
        (error) => {
          failure = error;
          tx.abort();
        },
      );
    } catch (error) {
      failure = error;
      tx.abort();
    }
  });
}
export function read(store, key) {
  return transaction([store], "readonly", (tx, done) => {
    tx.objectStore(store).get(key).onsuccess = (event) =>
      done(event.target.result);
  });
}
export function findAccount(login) {
  return transaction(["accounts"], "readonly", (tx, done) => {
    tx.objectStore("accounts").index("login").get(login).onsuccess = (event) =>
      done(event.target.result);
  });
}
export function createAccount(account, data) {
  return transaction(["accounts", "videos"], "readwrite", (tx, done, fail) => {
    const request = tx.objectStore("accounts").add(account);
    request.onerror = () =>
      fail(
        new Error(
          request.error?.name === "ConstraintError" ? "duplicate" : "storage",
        ),
      );
    tx.objectStore("videos").add({ id: account.id, revision: 0, data });
    done(account);
  });
}
export function writeProfile(id, revision, data) {
  return transaction(["accounts", "videos"], "readwrite", (tx, done, fail) => {
    tx.objectStore("accounts").get(id).onsuccess = (event) => {
      if (!event.target.result) return fail(new Error("sessionExpired"));
      const store = tx.objectStore("videos");
      store.get(id).onsuccess = (current) => {
        if ((current.target.result?.revision ?? 0) !== revision)
          return fail(new Error("conflict"));
        try {
          store.put({ id, revision: revision + 1, data });
          done(revision + 1);
        } catch {
          fail(new Error("storage"));
        }
      };
    };
  });
}
export function deleteAccount(id) {
  return transaction(
    ["accounts", "videos", "workspaces"],
    "readwrite",
    (tx) => {
      tx.objectStore("accounts").delete(id);
      tx.objectStore("workspaces").delete(id);
      tx.objectStore("videos").delete(id);
    },
  );
}
