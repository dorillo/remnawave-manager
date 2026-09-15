import {
  createAccount,
  findAccount,
  read,
  deleteAccount,
} from './morrow-db.js';
import { blankWorkspace } from './morrow-store.js';
const SESSION = 'morrow:session:v1';
const encode = (bytes) => btoa(String.fromCharCode(...bytes));
async function derive(password, salt) {
  const key = await crypto.subtle.importKey(
    'raw',
    new TextEncoder().encode(password),
    'PBKDF2',
    false,
    ['deriveBits'],
  );
  return encode(
    new Uint8Array(
      await crypto.subtle.deriveBits(
        { name: 'PBKDF2', salt, iterations: 210000, hash: 'SHA-256' },
        key,
        256,
      ),
    ),
  );
}
export async function restoreSession() {
  let id;
  try {
    id = sessionStorage.getItem(SESSION);
  } catch {
    return null;
  }
  return id ? (await read('accounts', id)) || null : null;
}
export async function authenticate(login, password, register = false) {
  login = login.trim().toLowerCase();
  if (!/^[a-z0-9_.-]{3,40}$/.test(login)) throw new Error('invalidLogin');
  if (password.length < 8 || password.length > 128)
    throw new Error('invalidPassword');
  if (!crypto.subtle) throw new Error('secureContext');
  let account = await findAccount(login);
  if (register) {
    if (account) throw new Error('duplicate');
    const salt = crypto.getRandomValues(new Uint8Array(16));
    account = {
      id: crypto.randomUUID(),
      login,
      salt: encode(salt),
      hash: await derive(password, salt),
    };
    await createAccount(account, blankWorkspace(login));
  } else if (
    !account ||
    (await derive(
      password,
      Uint8Array.from(atob(account.salt), (c) => c.charCodeAt(0)),
    )) !== account.hash
  ) {
    throw new Error('credentials');
  }
  try {
    sessionStorage.setItem(SESSION, account.id);
  } catch {
    /* Remains signed in until reload. */
  }
  return account;
}
export function logout() {
  try {
    sessionStorage.removeItem(SESSION);
  } catch {
    /* No persistent session. */
  }
}
export async function removeAccount(id) {
  await deleteAccount(id);
  logout();
}
