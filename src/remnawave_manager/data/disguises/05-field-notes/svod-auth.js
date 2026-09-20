import { list, put, signIn } from "./svod-store.js";
const encode = (bytes) => btoa(String.fromCharCode(...bytes));
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
  login = login.trim().toLocaleLowerCase();
  if (name !== undefined) {
    if (
      !name.trim() ||
      name.trim().length > 80 ||
      !/^[-.\p{L}\p{N}_]{3,40}$/u.test(login) ||
      password.length < 8 ||
      password.length > 128
    )
      throw new Error("invalid");
    if ((await list("accounts")).some((a) => a.login === login))
      throw new Error("duplicate");
    const salt = crypto.getRandomValues(new Uint8Array(16)),
      hash = await derive(password, salt),
      id = crypto.randomUUID();
    await put("accounts", {
      id,
      name: name.trim(),
      login,
      password: { salt: encode(salt), hash },
    });
    signIn(id);
    return;
  }
  const account = (await list("accounts")).find((a) => a.login === login);
  let matches = false;
  if (account)
    try {
      matches =
        (await derive(
          password,
          Uint8Array.from(atob(account.password.salt), (c) => c.charCodeAt(0)),
        )) === account.password.hash;
    } catch (e) {
      if (e.message === "secureError") throw e;
    }
  if (!matches) throw new Error("authError");
  signIn(account.id);
}
