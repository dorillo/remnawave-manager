let connection;
export function openDatabase() {
  if (!connection)
    connection = new Promise((resolve, reject) => {
      const request = indexedDB.open('morrow:workspace:v1', 1);
      request.onupgradeneeded = () => {
        const db = request.result;
        db.createObjectStore('accounts', { keyPath: 'id' }).createIndex(
          'login',
          'login',
          { unique: true },
        );
        db.createObjectStore('workspaces', { keyPath: 'id' });
      };
      request.onerror = request.onblocked = () => {
        connection = null;
        reject(new Error('storage'));
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
    const tx = db.transaction(stores, mode);
    let result, failure;
    tx.oncomplete = () => resolve(result);
    tx.onabort = tx.onerror = () => reject(failure || new Error('storage'));
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
  return transaction([store], 'readonly', (tx, done) => {
    tx.objectStore(store).get(key).onsuccess = (event) =>
      done(event.target.result);
  });
}
export function findAccount(login) {
  return transaction(['accounts'], 'readonly', (tx, done) => {
    tx.objectStore('accounts').index('login').get(login).onsuccess = (event) =>
      done(event.target.result);
  });
}
export function createAccount(account, data) {
  return transaction(
    ['accounts', 'workspaces'],
    'readwrite',
    (tx, done, fail) => {
      const request = tx.objectStore('accounts').add(account);
      request.onerror = () =>
        fail(
          new Error(
            request.error?.name === 'ConstraintError' ? 'duplicate' : 'storage',
          ),
        );
      tx.objectStore('workspaces').add({ id: account.id, revision: 0, data });
      done(account);
    },
  );
}
export function writeWorkspace(id, revision, data) {
  return transaction(
    ['accounts', 'workspaces'],
    'readwrite',
    (tx, done, fail) => {
      tx.objectStore('accounts').get(id).onsuccess = (event) => {
        if (!event.target.result) return fail(new Error('sessionExpired'));
        const store = tx.objectStore('workspaces');
        store.get(id).onsuccess = (current) => {
          if (current.target.result?.revision !== revision)
            return fail(new Error('conflict'));
          try {
            store.put({ id, revision: revision + 1, data });
            done(revision + 1);
          } catch {
            fail(new Error('storage'));
          }
        };
      };
    },
  );
}
export function deleteAccount(id) {
  return transaction(['accounts', 'workspaces'], 'readwrite', (tx) => {
    tx.objectStore('accounts').delete(id);
    tx.objectStore('workspaces').delete(id);
  });
}
