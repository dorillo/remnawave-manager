import { persistentSession } from '../shared/session.js';
import {
  createAccount,
  findAccount,
  read,
  deleteAccount,
} from "./morrow-db.js";
import { blankProfile } from "./morrow-store.js";
import { removeOwnerVideos } from "./morrow-uploads.js";
const session = persistentSession("morrow:session:v1", "storage");
const encode = (bytes) => btoa(String.fromCharCode(...bytes));
async function derive(password, salt) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(password),
    "PBKDF2",
    false,
    ["deriveBits"],
  );
  return encode(
    new Uint8Array(
      await crypto.subtle.deriveBits(
        { name: "PBKDF2", salt, iterations: 210000, hash: "SHA-256" },
        key,
        256,
      ),
    ),
  );
}
export async function restoreSession() {
  const id = session.get();
  if (!id) return null;
  const account = await read("accounts", id);
  if (!account) throw new Error("storage");
  return account;
}
export async function authenticate(login, password, register = false) {
  login = login.trim().toLowerCase();
  if (!/^[a-z0-9_.-]{3,40}$/.test(login)) throw new Error("invalidLogin");
  if (password.length < 8 || password.length > 128)
    throw new Error("invalidPassword");
  if (!crypto.subtle) throw new Error("secureContext");
  let account = await findAccount(login);
  if (register) {
    if (account) throw new Error("duplicate");
    const salt = crypto.getRandomValues(new Uint8Array(16));
    account = {
      id: crypto.randomUUID(),
      login,
      salt: encode(salt),
      hash: await derive(password, salt),
    };
    await createAccount(account, blankProfile(login));
  } else if (
    !account ||
    (await derive(
      password,
      Uint8Array.from(atob(account.salt), (c) => c.charCodeAt(0)),
    )) !== account.hash
  ) {
    throw new Error("credentials");
  }
  session.set(account.id);
  return account;
}
export function logout() { session.set(""); }
export async function removeAccount(id) {
  await removeOwnerVideos(id);
  await deleteAccount(id);
  logout();
}
