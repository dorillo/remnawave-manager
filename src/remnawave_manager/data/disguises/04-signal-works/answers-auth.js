import { change, getSession, newId, setSession } from './answers-store.js';

const b64 = (bytes) => btoa(String.fromCharCode(...bytes));
const bytes = (string) => Uint8Array.from(atob(string), (c) => c.charCodeAt(0));
async function derive(password, salt) {
  const key = await crypto.subtle.importKey('raw', new TextEncoder().encode(password), 'PBKDF2', false, ['deriveBits']);
  return b64(new Uint8Array(await crypto.subtle.deriveBits({ name: 'PBKDF2', salt, iterations: 210000, hash: 'SHA-256' }, key, 256)));
}
export function currentUser(state) { return state.accounts.find((x) => x.id === getSession()) || null; }
export async function register(name, email, password) {
  name = String(name).trim().slice(0, 60); email = String(email).trim().toLowerCase();
  if (name.length < 2 || !/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(email) || password.length < 8 || password.length > 128) throw new Error('invalid-account');
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const account = { id: newId(), name, email, salt: b64(salt), hash: await derive(password, salt), created: Date.now() };
  await change((state) => { if (state.accounts.some((x) => x.email === email)) throw new Error('duplicate-account'); state.accounts.push(account); });
  setSession(account.id);
  return account;
}
export async function login(email, password, state) {
  const account = state.accounts.find((x) => x.email === String(email).trim().toLowerCase());
  if (!account || await derive(password, bytes(account.salt)) !== account.hash) throw new Error('invalid-login');
  setSession(account.id);
  return account;
}
export function logout() { setSession(''); }
