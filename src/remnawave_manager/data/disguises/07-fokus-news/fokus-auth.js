import { change, read, signIn } from "./fokus-store.js?v=20260919-release";
const encode = (b) => btoa(String.fromCharCode(...b));
async function derive(password, salt) {
  if (!crypto.subtle) throw new Error("secureError");
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
export async function authenticate(login, password, name) {
  login = login.trim().toLowerCase();
  if (
    !/^[-.\p{L}\p{N}_]{3,40}$/u.test(login) ||
    password.length < 8 ||
    password.length > 128 ||
    (name !== undefined && (!name.trim() || name.trim().length > 80))
  )
    throw new Error("invalid");
  if (name !== undefined) {
    const salt = crypto.getRandomValues(new Uint8Array(16)),
      hash = await derive(password, salt),
      id = crypto.randomUUID();
    await change((s) => {
      if (s.accounts.some((a) => a.login === login))
        throw new Error("duplicate");
      s.accounts.push({
        id,
        login,
        name: name.trim(),
        salt: encode(salt),
        hash,
      });
    });
    signIn(id);
  } else {
    const a = (await read()).accounts.find((x) => x.login === login);
    if (
      !a ||
      (await derive(
        password,
        Uint8Array.from(atob(a.salt), (c) => c.charCodeAt(0)),
      )) !== a.hash
    )
      throw new Error("authError");
    signIn(a.id);
  }
}
