import { storage } from '../shared/storage.js';
import { normalizeList, validId } from './aster-data.js';

const KEY = 'aster:state:v1';
const blankLibrary = () => ({
  likes: [],
  later: [],
  follows: [],
  history: {},
  playlists: [],
  notes: {},
  comments: [],
  commentLikes: [],
  added: [],
});
const validStoredVideoId = (value) =>
  validId(value) ||
  (typeof value === 'string' &&
    /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(value));
const ids = (value) =>
  Array.isArray(value)
    ? [...new Set(value.filter(validStoredVideoId))].slice(0, 1000)
    : [];
export function cleanLibrary(value = {}) {
  const result = blankLibrary();
  if (!value || typeof value !== 'object') return result;
  result.likes = ids(value.likes);
  result.commentLikes = Array.isArray(value.commentLikes)
    ? [
        ...new Set(value.commentLikes.filter((id) => /^rt:\d{1,24}$/.test(id))),
      ].slice(0, 1000)
    : [];
  const seenComments = new Set();
  result.comments = (Array.isArray(value.comments) ? value.comments : [])
    .filter((c) => {
      if (
        !c ||
        typeof c.id !== 'string' ||
        !/^[a-zA-Z0-9-]{1,64}$/.test(c.id) ||
        seenComments.has(c.id) ||
        !validStoredVideoId(c.videoId) ||
        typeof c.text !== 'string' ||
        !c.text.trim() ||
        !Number.isFinite(c.createdAt)
      )
        return false;
      seenComments.add(c.id);
      return true;
    })
    .slice(0, 1000)
    .map((c) => ({
      id: c.id,
      videoId: c.videoId,
      text: c.text.slice(0, 5000),
      createdAt: c.createdAt,
      parentId:
        typeof c.parentId === 'string' &&
        (/^[a-zA-Z0-9-]{1,64}$/.test(c.parentId) ||
          /^rt:\d{1,24}$/.test(c.parentId))
          ? c.parentId
          : null,
      liked: c.liked === true,
    }));
  result.comments.forEach((c) => {
    if (c.parentId?.startsWith('rt:')) return;
    if (
      !result.comments.some(
        (p) => p.id === c.parentId && p.videoId === c.videoId && !p.parentId,
      )
    )
      c.parentId = null;
  });
  result.later = ids(value.later);
  result.follows = Array.isArray(value.follows)
    ? [...new Set(value.follows.filter((id) => /^\d{1,20}$/.test(id)))].slice(
        0,
        200,
      )
    : [];
  for (const [id, entry] of Object.entries(value.history || {}).slice(
    0,
    1000,
  )) {
    if (
      validStoredVideoId(id) &&
      entry &&
      Number.isFinite(entry.time) &&
      Number.isFinite(entry.at)
    )
      result.history[id] = {
        time: Math.min(86400, Math.max(0, entry.time)),
        at: entry.at,
        complete: entry.complete === true,
      };
  }
  result.playlists = Array.isArray(value.playlists)
    ? value.playlists
        .filter(
          (p) =>
            p &&
            /^[a-zA-Z0-9-]{1,64}$/.test(p.id) &&
            typeof p.name === 'string',
        )
        .slice(0, 100)
        .map((p) => ({
          id: p.id,
          name: p.name.slice(0, 80),
          videos: ids(p.videos),
        }))
    : [];
  for (const [id, note] of Object.entries(value.notes || {}).slice(0, 1000))
    if (validStoredVideoId(id) && typeof note === 'string')
      result.notes[id] = note.slice(0, 5000);
  result.added = normalizeList(value.added).map((video) => ({
    ...video,
    publicComments: [],
  }));
  return result;
}
function read(strict = false) {
  let value;
  if (strict) {
    try {
      value = JSON.parse(localStorage.getItem(`disguise:${KEY}`));
      if (value !== null && (value?.version !== 1 || !Array.isArray(value.accounts))) throw new Error();
    } catch { throw new Error('storageError'); }
  } else value = storage.get(KEY);
  if (value?.version !== 1 || !Array.isArray(value.accounts))
    return { version: 1, accounts: [], session: null };
  return {
    version: 1,
    session: typeof value.session === 'string' ? value.session : null,
    accounts: value.accounts
      .filter(
        (a) =>
          a &&
          typeof a.id === 'string' &&
          typeof a.email === 'string' &&
          a.password,
      )
      .map((a) => ({
        ...a,
        name: String(a.name || '').slice(0, 80),
        bio: String(a.bio || '').slice(0, 500),
        avatar:
          typeof a.avatar === 'string' &&
          /^data:image\/(jpeg|png|webp);base64,/.test(a.avatar)
            ? a.avatar
            : '',
        library: cleanLibrary(a.library),
      })),
  };
}
let state = read();
export const user = () =>
  state.accounts.find((a) => a.id === state.session) || null;
export const library = () => user()?.library || blankLibrary();
export const accounts = () => state.accounts;
export function commit(change) {
  // A storage event can be delayed in a background tab. Never write its old snapshot.
  const next = structuredClone(read(true));
  change(next);
  next.accounts.forEach((account) => {
    account.library = cleanLibrary(account.library);
  });
  if (!storage.set(KEY, next)) throw new Error('storageError');
  state = next;
}
export function updateUser(change) {
  const id = user()?.id;
  if (!id) throw new Error('needLogin');
  commit((next) => {
    const account = next.accounts.find(a => a.id === id);
    if (next.session !== id || !account) throw new Error('needLogin');
    change(account);
  });
}
export function toggle(field, id) {
  updateUser((account) => {
    const list = account.library[field];
    account.library[field] = list.includes(id)
      ? list.filter((item) => item !== id)
      : [...list, id].slice(-1000);
  });
}
export function remember(video, time, complete = false) {
  if (!user()) return;
  updateUser((account) => {
    account.library.history[video.id] = { time, complete, at: Date.now() };
    account.library.history = Object.fromEntries(
      Object.entries(account.library.history)
        .sort((a, b) => b[1].at - a[1].at)
        .slice(0, 1000),
    );
    account.library.added = normalizeList([video, ...account.library.added]);
  });
}
window.addEventListener('storage', (event) => {
  if (event.key === `disguise:${KEY}` || event.key === null) {
    try { state = read(true); }
    catch { return; } // A transient read failure must not replace the active session.
    window.dispatchEvent(new Event('aster:state'));
  }
});
