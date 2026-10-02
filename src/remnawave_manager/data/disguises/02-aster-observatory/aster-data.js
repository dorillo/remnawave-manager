import { imageURL } from '../shared/image-proxy.js';
import { storage } from '../shared/storage.js';

export const TOPICS = [
  'general',
  'movies',
  'series',
  'music',
  'entertainment',
  'kids',
  'sport',
  'news',
  'science',
  'travel',
  'culture',
  'education',
  'city',
];
const CACHE = 'aster:catalog:v1';
const MAX_VIDEOS = 500;
export const TOPIC_CATEGORIES = { movies: 4, series: 5, music: 6, entertainment: 57,
  kids: 7, sport: 16, news: 8, science: 52, travel: 11, culture: 64 };
const categoryTopics = { ...Object.fromEntries(Object.entries(TOPIC_CATEGORIES).map(([topic, id]) => [id, topic])),
  19: 'entertainment', 43: 'entertainment', 42: 'kids' };
function publicationDate(value) {
  const text = String(value || '').slice(0, 40);
  // Rutube's unzoned timestamps are Moscow time, not the viewer's local time.
  const zoned = /^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?$/.test(text) ? text + '+03:00' : text;
  const timestamp = Date.parse(zoned);
  return Number.isFinite(timestamp) ? new Date(timestamp).toISOString() : '';
}
export const validId = (value) =>
  typeof value === 'string' && /^[a-f0-9]{32}$/.test(value);
export function safeImage(value) {
  return imageURL(value);
}
export function normalizePublicComment(raw) {
  const selectedId = String(raw?.id || '').replace(/^rt:/, '');
  if (
    !raw ||
    !/^\d{1,24}$/.test(selectedId) ||
    typeof raw.text !== 'string' ||
    !raw.text.trim() ||
    raw.is_deleted ||
    raw.state === 0
  )
    return null;
  const author = raw.user || {};
  const selectedParent = String(raw.parentId ?? raw.parent_id ?? '').replace(
    /^rt:/,
    '',
  );
  return {
    id: `rt:${selectedId}`,
    text: raw.text.trim().slice(0, 5000),
    createdAt: String(raw.createdAt || raw.created_ts || '').slice(0, 40),
    author: String(raw.author || author.name || 'Астер').slice(0, 100),
    authorId: /^\d{1,20}$/.test(String(raw.authorId || author.id || ''))
      ? String(raw.authorId || author.id)
      : '',
    avatar: safeImage(raw.avatar || author.avatar_url || author.avatar),
    likes: counter(raw.likes ?? raw.likes_number) || 0,
    dislikes: counter(raw.dislikes ?? raw.dislikes_number) || 0,
    parentId: /^\d{1,24}$/.test(selectedParent) ? `rt:${selectedParent}` : null,
    pinned: raw.pinned === true || raw.is_pinned === true,
    repliesCount: counter(raw.repliesCount ?? raw.replies_number) || 0,
  };
}
export function normalize(raw, topic = 'general') {
  if (
    !raw ||
    !validId(raw.id) ||
    raw.is_adult ||
    raw.is_paid ||
    raw.is_club ||
    raw.is_hidden ||
    raw.is_deleted ||
    raw.is_livestream ||
    raw.is_on_air
  )
    return null;
  const author = raw.author || {};
  return {
    id: raw.id,
    title: String(raw.title || '').slice(0, 300) || 'Видео',
    description: String(raw.description || '').slice(0, 12000),
    thumbnail: safeImage(raw.thumbnail || raw.thumbnail_url),
    duration: Math.min(86400, Math.max(0, Number(raw.duration) || 0)),
    published: publicationDate(raw.publication_ts || raw.published || raw.created_ts),
    channelId: /^\d{1,20}$/.test(String(raw.channelId || author.id || ''))
      ? String(raw.channelId || author.id)
      : '',
    channel: String(raw.channel || author.name || 'Астер').slice(0, 200),
    avatar: safeImage(raw.avatar || author.avatar_url),
    subscribers: counter(raw.subscribers ?? author.subscribers_count),
    topic: TOPICS.includes(raw.topic) ? raw.topic : categoryTopics[raw.category?.id] || topic,
    views: counter(raw.views ?? raw.hits),
    publicLikes: counter(raw.publicLikes),
    age: counter(raw.age ?? raw.pg_rating?.age),
    commentsCount: counter(raw.commentsCount ?? raw.comments_count) || 0,
    publicComments: Array.isArray(raw.publicComments)
      ? raw.publicComments
          .map(normalizePublicComment)
          .filter(Boolean)
          .slice(0, 100)
      : [],
    language: ['ru', 'en'].includes(raw.language) ? raw.language : 'ru',
  };
}
function counter(value) {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
    ? value
    : null;
}
export function normalizeList(items, limit = MAX_VIDEOS) {
  if (!Array.isArray(items)) return [];
  const selectedLimit = Math.min(Math.max(Number(limit) || 0, 1), 5000);
  const unique = new Map();
  for (const raw of items.slice(0, selectedLimit * 2)) {
    const item = normalize(raw);
    if (item && !unique.has(item.id)) unique.set(item.id, item);
    if (unique.size >= selectedLimit) break;
  }
  return [...unique.values()];
}
function normalizeChannels(items) {
  if (!Array.isArray(items)) return [];
  return items
    .filter((item) => /^\d{1,20}$/.test(String(item?.id || '')))
    .slice(0, 100)
    .map((item) => ({
      id: String(item.id),
      name: String(item.name || 'Астер').slice(0, 200),
      avatar: safeImage(item.avatar),
      videos: normalizeList(
        Array.isArray(item.videos)
          ? item.videos.map((video) => ({
              ...video,
              channelId: String(item.id),
              channel: item.name,
              avatar: item.avatar,
            }))
          : [],
      ),
    }))
    .filter((item) => item.videos.length);
}
async function json(url, signal) {
  const timeout = AbortSignal.timeout(10000);
  const response = await fetch(url, {
    credentials: 'omit',
    redirect: 'error',
    signal: signal ? AbortSignal.any([timeout, signal]) : timeout,
    cache: 'no-cache',
  });
  if (!response.ok) throw Object.assign(new Error(`HTTP ${response.status}`), { status: response.status });
  return response.json();
}
export async function loadCatalog(signal) {
  try {
    const result = await json(
      new URL('./data/catalog.json', import.meta.url),
      signal,
    );
    const videos = normalizeList(result.videos);
    if (!videos.length) throw new Error('Empty catalog');
    const catalog = {
      videos,
      updated: String(result.updated || ''),
      channels: normalizeChannels(result.channels),
      mode: 'snapshot',
    };
    storage.set(CACHE, { ...catalog, channels: [], cachedAt: Date.now() });
    return catalog;
  } catch (error) {
    if (signal?.aborted) throw error;
    const cached = storage.get(CACHE);
    if (cached && Date.now() - cached.cachedAt < 7 * 86400000) {
      const videos = normalizeList(cached.videos);
      if (videos.length)
        return {
          videos,
          updated: cached.updated,
          channels: normalizeChannels(cached.channels),
          mode: 'snapshot',
        };
    }
    throw error;
  }
}
export async function searchRutube(query, page = 1, signal) {
  const text = String(query || '').trim().slice(0, 200);
  const selectedPage = Math.min(Math.max(Number(page) || 1, 1), 20);
  if (!text) return { videos: [], hasNext: false, page: 0 };
  const url = new URL('/_aster/rutube-search', location.origin);
  url.searchParams.set('query', text);
  url.searchParams.set('client', 'wdp');
  url.searchParams.set('page', String(selectedPage));
  const result = await json(url, signal);
  if (!Array.isArray(result.results))
    throw new Error('Invalid search response');
  return {
    videos: normalizeList(result.results, 200),
    hasNext: result.has_next === true && selectedPage < 20,
    page: selectedPage,
  };
}

const listCache = new Map();
async function publicList(path, signal, topic) {
  if (signal?.aborted) throw new DOMException('Aborted', 'AbortError');
  const cached = listCache.get(path);
  const result = cached?.at > Date.now() - 60000 ? cached.raw : await json(path, signal);
  if (!Array.isArray(result.results)) throw new Error('Invalid list response');
  if (result !== cached?.raw) {
    listCache.set(path, { raw: result, at: Date.now() });
    while (listCache.size > 80) listCache.delete(listCache.keys().next().value);
  }
  return { videos: normalizeList(result.results.map(raw => topic ? { ...raw, topic } : raw), 500),
    hasNext: result.has_next === true };
}
export async function loadFeed(offset = 0, signal) {
  if (!Number.isInteger(offset) || offset < 0 || offset > 999999) throw new Error('Invalid offset');
  const result = await publicList('/_aster/rutube-feed?offset=' + offset, signal);
  return { ...result, next: result.hasNext && offset + 24 <= 999999 ? offset + 24 : null };
}
export async function loadChannel(id, page = 1, signal) {
  if (!/^[0-9]{1,20}$/.test(id) || !Number.isInteger(page) || page < 1 || page > 9999)
    throw new Error('Invalid channel page');
  const result = await publicList(`/_aster/rutube-channel/${id}?page=${page}`, signal);
  return { ...result, next: result.hasNext && page < 9999 ? page + 1 : null };
}
export async function loadTopic(topic, page = 1, signal) {
  if (!TOPICS.includes(topic) || !Number.isInteger(page) || page < 1 || page > 9999)
    throw new Error('Invalid topic page');
  if (!TOPIC_CATEGORIES[topic]) {
    if (topic === 'general') return loadFeed((page - 1) * 24, signal).then(result => ({
      ...result, next: result.next === null ? null : page + 1,
    }));
    const result = await searchRutube({ education: 'образование', city: 'город' }[topic], page, signal);
    return { ...result, videos: result.videos.map(video => ({ ...video, topic })), next: result.hasNext ? page + 1 : null };
  }
  const result = await publicList(`/_aster/rutube-category/${TOPIC_CATEGORIES[topic]}?page=${page}`, signal, topic);
  return { ...result, next: result.hasNext && page < 9999 ? page + 1 : null };
}

export async function loadSubscriptions(cursors, signal) {
  const entries = Object.entries(cursors), next = {}, videos = [], failures = [];
  let index = 0;
  async function worker() {
    while (index < entries.length) {
      const [id, page] = entries[index++];
      try {
        const result = await loadChannel(id, page, signal);
        videos.push(...result.videos);
        if (result.next !== null) next[id] = result.next;
      } catch (error) {
        if (signal?.aborted) throw error;
        failures.push(id);
        next[id] = page;
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(3, entries.length) }, worker));
  if (failures.length === entries.length && entries.length) throw new Error('Subscriptions unavailable');
  return { videos: normalizeList(videos, 5000), next: Object.keys(next).length ? next : null,
    partial: failures.length > 0 };
}

export async function loadComments(videoId, { cursor = '', parent = '', signal } = {}) {
  if (!validId(videoId)) throw new Error('Invalid video ID');
  const params = new URLSearchParams();
  for (const [key, value] of [['comment_id', cursor], ['parent_id', parent]]) {
    if (value && !/^\d{1,24}$/.test(value)) throw new Error('Invalid comment ID');
    if (value) params.set(key, value);
  }
  const result = await json('/_aster/rutube-comments/' + videoId + (params.size ? '?' + params : ''), signal);
  if (!Array.isArray(result.results)) throw new Error('Invalid comments response');
  const rows = [...(result.pinned_comment && !parent && !cursor ? [result.pinned_comment] : []), ...result.results]
    .map(normalizePublicComment).filter(Boolean);
  return {
    rows: [...new Map(rows.map(row => [row.id, row])).values()],
    count: counter(result.comments_count),
    hasNext: result.has_next === true,
    cursor: String(result.results.at(-1)?.id || ''),
  };
}


export async function loadVideo(id, signal) {
  if (!validId(id)) throw new Error('Invalid video ID');
  const raw = await json('/_aster/rutube-video/' + id, signal);
  const video = normalize(raw);
  if (!video || video.id !== id) throw new Error('Invalid video response');
  return video;
}
export async function loadProfile(id, signal) {
  if (!/^[0-9]{1,20}$/.test(id)) throw new Error('Invalid profile ID');
  const raw = await json('/_aster/rutube-profile/' + id, signal);
  if (!raw || String(raw.id) !== id || typeof raw.name !== 'string') throw new Error('Invalid profile response');
  return {
    id, name: raw.name.slice(0, 200),
    avatar: safeImage(raw.avatar_url || raw.appearance?.avatar_image),
    description: typeof raw.description === 'string' ? raw.description.slice(0, 12000) : '',
    subscribers: counter(raw.subscribers_count),
    videoCount: counter(raw.video_count),
  };
}
