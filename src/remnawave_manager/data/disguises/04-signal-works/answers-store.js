const DB = 'answers:workspace:v1';
const SESSION = 'answers:session:v1';
const empty = () => ({ accounts: [], questions: [], answers: [], comments: [], votes: {}, saves: {}, snapshots: {}, version: 1 });
const record = (value) => value && typeof value === 'object' && !Array.isArray(value);
const records = (value) => Array.isArray(value) ? value.filter(record) : [];
const dictionary = (value) => record(value) ? { ...value } : {};
const normalize = (value) => {
  if (!record(value)) return empty();
  return {
    accounts: records(value.accounts),
    questions: records(value.questions),
    answers: records(value.answers),
    comments: records(value.comments),
    votes: dictionary(value.votes),
    saves: dictionary(value.saves),
    snapshots: dictionary(value.snapshots),
    version: 1,
  };
};
let database;

async function open() {
  if (database) return database;
  database = await new Promise((resolve, reject) => {
    const request = indexedDB.open(DB, 1);
    request.onupgradeneeded = () => request.result.createObjectStore('kv');
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  database.onversionchange = () => { database.close(); database = null; };
  return database;
}

export async function read() {
  const db = await open();
  return new Promise((resolve, reject) => {
    const request = db.transaction('kv').objectStore('kv').get('state');
    request.onsuccess = () => resolve(normalize(request.result));
    request.onerror = () => reject(request.error);
  });
}

export async function change(mutator) {
  const db = await open();
  return new Promise((resolve, reject) => {
    const transaction = db.transaction('kv', 'readwrite');
    const store = transaction.objectStore('kv');
    let outcome;
    const request = store.get('state');
    request.onsuccess = () => {
      try {
        const state = normalize(request.result);
        outcome = mutator(state);
        store.put(state, 'state');
      } catch (error) { transaction.abort(); reject(error); }
    };
    transaction.oncomplete = () => { try { const channel = new BroadcastChannel(DB); channel.postMessage('changed'); channel.close(); } catch {} resolve(outcome); };
    transaction.onerror = () => reject(transaction.error);
    transaction.onabort = () => reject(transaction.error || new Error('storage'));
  });
}

export const newId = () => crypto.randomUUID();
export function getSession() { try { return sessionStorage.getItem(SESSION) || ''; } catch { return ''; } }
export function setSession(id) { try { if (id) sessionStorage.setItem(SESSION, id); else sessionStorage.removeItem(SESSION); } catch {} }
export function watch(callback) { try { const channel = new BroadcastChannel(DB); channel.onmessage = callback; return () => channel.close(); } catch { return () => {}; } }
