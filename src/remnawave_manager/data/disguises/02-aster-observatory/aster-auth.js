import { accounts, commit } from './aster-store.js';

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
export async function signIn(email, password) {
  const account = accounts().find(
    (a) => a.email === email.trim().toLowerCase(),
  );
  let matches = false;
  try {
    if (account)
      matches =
        (await derive(
          password,
          Uint8Array.from(atob(account.password.salt), (c) => c.charCodeAt(0)),
        )) === account.password.hash;
  } catch {
    /* Malformed stored credentials are not valid credentials. */
  }
  if (!matches) throw new Error('authError');
  commit((state) => {
    if (!state.accounts.some(a => a.id === account.id && a.password?.hash === account.password.hash))
      throw new Error('authError');
    state.session = account.id;
  });
}
export async function register(name, email, password) {
  email = email.trim().toLowerCase();
  name = name.trim();
  if (
    !name ||
    name.length > 80 ||
    !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) ||
    email.length > 254 ||
    password.length < 8 ||
    password.length > 128
  )
    throw new Error('invalid');
  if (accounts().some((a) => a.email === email)) throw new Error('duplicate');
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const hash = await derive(password, salt);
  const id = crypto.randomUUID();
  commit((state) => {
    if (state.accounts.some((a) => a.email === email))
      throw new Error('duplicate');
    state.accounts.push({
      id,
      name,
      email,
      bio: '',
      avatar: '',
      password: { salt: encode(salt), hash },
      library: {
        likes: [],
        later: [],
        follows: [],
        history: {},
        playlists: [],
        notes: {},
        comments: [],
        commentLikes: [],
        added: [],
      },
    });
    state.session = id;
  });
}
export const signOut = () =>
  commit((state) => {
    state.session = null;
  });
export async function avatar(file) {
  if (!file?.type.startsWith('image/'))
    throw new Error('avatarError');
  const bitmap = await createImageBitmap(file);
  try {
    const canvas = document.createElement('canvas');
    canvas.width = 256;
    canvas.height = 256;
    const size = Math.min(bitmap.width, bitmap.height);
    canvas
      .getContext('2d')
      .drawImage(
        bitmap,
        (bitmap.width - size) / 2,
        (bitmap.height - size) / 2,
        size,
        size,
        0,
        0,
        256,
        256,
      );
    return canvas.toDataURL('image/jpeg', 0.82);
  } finally {
    bitmap.close();
  }
}
