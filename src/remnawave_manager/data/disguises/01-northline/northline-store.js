import { readState, currentUser } from './northline-auth.js';
import { writeState } from './northline-auth.js';

const KEY = 'northline:reader:v1';
const object = (value) =>
  value && typeof value === 'object' && !Array.isArray(value) ? value : {};
export function readReader() {
  const state = readState();
  const account = currentUser(state);
  let value = account ? state.profiles?.[account.id]?.reader : state.guestReader;
  if (!value && !account) {
    try {
      value = JSON.parse(localStorage.getItem(KEY) || 'null');
    } catch {
      /* migrate the pre-account guest profile if it exists */
    }
  }
  if (!value)
    value = {
      name: account?.displayName || '',
      username: account?.username || '',
      bio: state.profiles?.[account?.id]?.bio || '',
      drafts: account
        ? []
        : state.posts.map((post) => ({ id: post.id, text: post.text || '', updatedAt: post.createdAt })),
    };
  return {
    name: account ? String(value.name || account.displayName || '') : '',
    username: account ? String(value.username || account.username || '') : '',
    bio: account ? String(value.bio || '') : '',
    avatar: account ? String(value.avatar || '') : '',
    topics: Array.isArray(value.topics)
      ? value.topics.filter((t) => typeof t === 'string').slice(0, 12)
      : ['photography', 'art', 'nature', 'books'],
    saved: object(value.saved),
    favourites: object(value.favourites),
    likes: object(value.likes),
    boosts: object(value.boosts),
    votes: object(value.votes),
    follows: object(value.follows),
    drafts: Array.isArray(value.drafts)
      ? value.drafts.filter((draft) => draft && typeof draft.text === 'string')
      : [],
    localPosts: Array.isArray(value.localPosts)
      ? value.localPosts.filter((post) => post && typeof post.text === 'string').slice(0, 100)
      : [],
  };
}
export function writeReader(value, state = readState()) {
  try {
    const account = currentUser(state);
    if (account) {
      state.profiles = state.profiles || {};
      state.profiles[account.id] = { ...(state.profiles[account.id] || {}), reader: value };
    } else state.guestReader = value;
    return writeState(state);
  } catch {
    return false;
  }
}
export function readLastFeed() {
  try {
    const saved = JSON.parse(localStorage.getItem('northline:last-feed:ml:v1') || 'null');
    return saved && Array.isArray(saved.posts) ? saved : null;
  } catch {
    return null;
  }
}
export function writeLastFeed(posts) {
  try {
    localStorage.setItem(
      'northline:last-feed:ml:v1',
      JSON.stringify({ time: Date.now(), posts: posts.slice(0, 80) }),
    );
  } catch {
    /* optional offline cache */
  }
}
