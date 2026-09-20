import { storage } from '../shared/storage.js';

const KEY = 'northline:state';
const VERSION = 1;
const blank = () => ({ version: VERSION, accounts: [], session: null, profiles: {}, posts: [], comments: [], likes: {}, saves: {}, follows: {}, notifications: [] });

export function readState() {
  const value = storage.get(KEY, null);
  if (!value || typeof value !== 'object') return blank();
  if (value.version !== VERSION) return migrate(value);
  const base = { ...blank(), ...value };
  base.accounts = Array.isArray(value.accounts) ? value.accounts.filter((item) => item && typeof item === 'object') : [];
  base.comments = Array.isArray(value.comments) ? value.comments.filter((item) => item && typeof item === 'object') : [];
  base.posts = Array.isArray(value.posts) ? value.posts.filter((item) => item && typeof item === 'object') : [];
  base.notifications = Array.isArray(value.notifications) ? value.notifications.filter((item) => item && typeof item === 'object') : [];
  base.profiles = value.profiles && typeof value.profiles === 'object' ? value.profiles : {};
  base.likes = value.likes && typeof value.likes === 'object' ? value.likes : {};
  base.saves = value.saves && typeof value.saves === 'object' ? value.saves : {};
  base.follows = value.follows && typeof value.follows === 'object' ? value.follows : {};
  return base;
}
export function writeState(state) { return storage.set(KEY, { ...state, version: VERSION }); }
function migrate(value) { return { ...blank(), profiles: value.profiles || {}, posts: Array.isArray(value.posts) ? value.posts : [] }; }

function bytesToBase64(bytes) { let binary = ''; bytes.forEach((byte) => { binary += String.fromCharCode(byte); }); return btoa(binary); }
function base64ToBytes(value) { const binary = atob(value); return Uint8Array.from(binary, (character) => character.charCodeAt(0)); }
async function derivePassword(password, salt, iterations = 120000) {
  const material = await crypto.subtle.importKey('raw', new TextEncoder().encode(password), 'PBKDF2', false, ['deriveBits']);
  const bits = await crypto.subtle.deriveBits({ name: 'PBKDF2', salt, iterations, hash: 'SHA-256' }, material, 256);
  return bytesToBase64(new Uint8Array(bits));
}
export async function hashPassword(password) {
  if (!globalThis.crypto?.subtle) throw new Error('Web Crypto API недоступен');
  const salt = crypto.getRandomValues(new Uint8Array(16));
  return { salt: bytesToBase64(salt), hash: await derivePassword(password, salt), algorithm: 'PBKDF2-SHA-256', iterations: 120000 };
}
export async function verifyPassword(password, record) { try { const iterations = Number(record?.iterations) || 120000; return Boolean(record?.salt && record?.hash) && (await derivePassword(password, base64ToBytes(record.salt), iterations)) === record.hash; } catch { return false; } }
export function currentUser(state) { return state.session ? state.accounts.find((account) => account.id === state.session.userId) || null : null; }
export function createSession(state, userId) { state.session = { userId, authenticatedAt: Date.now(), lastActiveAt: Date.now() }; return writeState(state); }
export function logout(state) { if (!writeState({ ...state, session: null })) return false; state.session = null; return true; }
export function validateEmail(value) { return /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/i.test(String(value || '').trim()); }
export function validatePassword(value) { return typeof value === 'string' && value.length >= 8 && value.length <= 128; }
export function newId(prefix = 'id') { return `${prefix}-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`; }
export { KEY };
