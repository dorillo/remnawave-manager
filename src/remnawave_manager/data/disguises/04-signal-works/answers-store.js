import { persistentSession } from '../shared/session.js';
const activeSession = persistentSession('answers:session:v1', 'storage');
const DB = 'answers:workspace:v1';
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
let database, opening;

async function open() {
  if (database) return database;
  if (opening) return opening;
  opening = new Promise((resolve, reject) => {
    let failed = false;
    const request = indexedDB.open(DB, 1);
    request.onupgradeneeded = () => request.result.createObjectStore('kv');
    request.onsuccess = () => {
      const db = request.result;
      if (failed) { db.close(); return; }
      database = db;
      db.onversionchange = db.onclose = () => {
        db.close();
        if (database === db) database = null;
      };
      resolve(db);
    };
    request.onerror = request.onblocked = () => {
      failed = true;
      reject(new Error('storage'));
    };
  });
  try { return await opening; }
  finally { opening = null; }
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
export function getSession() { return activeSession.get(); }
export function setSession(id) { activeSession.set(id); }
export function watch(callback) { try { const channel = new BroadcastChannel(DB); channel.onmessage = callback; return () => channel.close(); } catch { return () => {}; } }
