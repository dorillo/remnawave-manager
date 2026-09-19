// Network access is read-only and restricted to the configured instance.
export const INSTANCE = 'mastodon.ml';
export const API = `https://${INSTANCE}`;
export const TOPICS = [
  { tag: 'photography', name: 'Фотография', caption: 'Мир через объектив', icon: 'camera' },
  { tag: 'art', name: 'Творчество', caption: 'Идеи, рисунки и процесс', icon: 'palette' },
  { tag: 'nature', name: 'Природа', caption: 'Немного свежего воздуха', icon: 'leaf' },
  { tag: 'books', name: 'Книги', caption: 'Прочитанное и любимое', icon: 'book' },
  { tag: 'urbanism', name: 'Город', caption: 'Улицы, места и люди', icon: 'home' },
  {
    tag: 'technology',
    name: 'Технологии',
    caption: 'То, что меняет повседневность',
    icon: 'globe',
  },
];
// Public account timelines remain available when anonymous tag timelines are closed.
// Keep the selection explicit: no dependency on the changing public directory.
const TOPIC_AUTHORS = {
  photography: ['109384478461363898', '108281552968695950'],
  art: ['107983254157324780', '110960851520412034'],
  nature: ['114477800339671583', '108281552968695950'],
  books: ['116749385197641639', '110960851520412034'],
  urbanism: ['108281552968695950', '109384478461363898'],
  technology: ['109778674318793136', '114431977632678872'],
};
const PREFIX = 'northline:api:ml:v1:';
const remoteId = (id, instance) =>
  id == null ? null : instance === INSTANCE ? `ml:${id}` : String(id);
export const instanceForId = (id) => (String(id).startsWith('ml:') ? INSTANCE : 'mastodon.social');

// The API language field is sometimes missing. Cyrillic alone is insufficient
// (e.g. Ukrainian); use Russian-specific letters or common words as a fallback.
export function isRussian(post) {
  const language = String(post.language || '')
    .toLowerCase()
    .split('-')[0];
  if (language) return language === 'ru';
  const text = String(post.text || '').replace(/https?:\/\/\S+|@[\w.@-]+/g, '');
  const cyrillic = (text.match(/[а-яё]/gi) || []).length;
  const latin = (text.match(/[a-z]/gi) || []).length;
  return (
    cyrillic >= 4 &&
    cyrillic > latin &&
    !/[іїєґў]/i.test(text) &&
    (/[ыэъё]/i.test(text) ||
      /(?:^|[^а-яё])(и|в|на|не|это|что|как|для|сегодня|очень|или)(?=$|[^а-яё])/i.test(text))
  );
}

const inflight = new Map();
let retryAfter = 0;
let activeRequests = 0;
const waitingRequests = [];
async function acquireRequest() {
  if (activeRequests < 3) activeRequests++;
  else await new Promise((resolve) => waitingRequests.push(resolve));
}
function releaseRequest() {
  const next = waitingRequests.shift();
  if (next) next();
  else activeRequests--;
}

export function safeUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : '';
  } catch {
    return '';
  }
}
export function plainText(html) {
  const doc = new DOMParser().parseFromString(String(html || ''), 'text/html');
  doc.querySelectorAll('script,style').forEach((el) => el.remove());
  doc.querySelectorAll('br').forEach((el) => el.replaceWith('\n'));
  doc.querySelectorAll('p').forEach((el) => el.append('\n\n'));
  return (doc.body.textContent || '').trim();
}
export function normalizeAccount(account = {}, instance = INSTANCE) {
  return {
    id: remoteId(account.id || '', instance),
    username: String(account.acct || account.username || ''),
    name: plainText(account.display_name) || account.username || 'Автор',
    bio: String(account.note || ''),
    avatar: safeUrl(account.avatar_static || account.avatar),
    header: safeUrl(account.header_static || account.header),
    url: safeUrl(account.url),
    followers: Number(account.followers_count) || 0,
    following: Number(account.following_count) || 0,
    statuses: Number(account.statuses_count) || 0,
    joined: account.created_at,
    bot: Boolean(account.bot),
    fields: Array.isArray(account.fields)
      ? account.fields
          .slice(0, 6)
          .map((f) => ({ name: plainText(f.name), value: String(f.value || '') }))
      : [],
  };
}
export function normalizeMastodonStatus(raw, instance = INSTANCE) {
  if (!raw || !raw.id || !raw.account) return null;
  const status = raw.reblog || raw;
  if (!status.id || !status.account) return null;
  return {
    id: remoteId(status.id, instance),
    cursor: String(raw.id),
    uri: status.uri || status.url || `https://${instance}/@${status.account.acct}/${status.id}`,
    url: safeUrl(status.url) || `https://${instance}/@${status.account.acct}/${status.id}`,
    account: normalizeAccount(status.account, instance),
    boostedBy: raw.reblog ? normalizeAccount(raw.account, instance) : null,
    content: String(status.content || ''),
    text: plainText(status.content),
    createdAt: status.created_at,
    editedAt: status.edited_at,
    language: status.language || '',
    warning: String(status.spoiler_text || ''),
    sensitive: Boolean(status.sensitive),
    replyTo: remoteId(status.in_reply_to_id, instance),
    favourites: Number(status.favourites_count) || 0,
    replies: Number(status.replies_count) || 0,
    boosts: Number(status.reblogs_count) || 0,
    tags: (status.tags || []).map((tag) => ({ name: String(tag.name), url: safeUrl(tag.url) })),
    media: (status.media_attachments || [])
      .filter((m) => ['image', 'video', 'gifv', 'audio'].includes(m.type))
      .map((m) => ({
        id: String(m.id),
        type: m.type,
        url: safeUrl(m.url || m.remote_url),
        preview: safeUrl(m.preview_url),
        alt: String(m.description || ''),
        width: Number(m.meta?.original?.width) || 0,
        height: Number(m.meta?.original?.height) || 0,
      }))
      .filter((m) => m.url || m.preview),
    card:
      status.card && safeUrl(status.card.url)
        ? {
            url: safeUrl(status.card.url),
            title: plainText(status.card.title),
            description: plainText(status.card.description),
            image: safeUrl(status.card.image),
            provider: plainText(status.card.provider_name),
          }
        : null,
    poll: status.poll
      ? {
          expired: Boolean(status.poll.expired),
          expires: status.poll.expires_at,
          votes: Number(status.poll.votes_count) || 0,
          options: (status.poll.options || []).map((o) => ({
            title: String(o.title),
            votes: o.votes_count == null ? null : Number(o.votes_count),
          })),
        }
      : null,
  };
}
function cached(path) {
  try {
    return JSON.parse(localStorage.getItem(PREFIX + path) || 'null');
  } catch {
    return null;
  }
}
function cache(path, data) {
  try {
    const keys = Object.keys(localStorage).filter((key) => key.startsWith(PREFIX));
    if (keys.length >= 45)
      keys
        .sort(
          (a, b) =>
            (JSON.parse(localStorage.getItem(a))?.time || 0) -
            (JSON.parse(localStorage.getItem(b))?.time || 0),
        )
        .slice(0, 10)
        .forEach((key) => localStorage.removeItem(key));
    localStorage.setItem(PREFIX + path, JSON.stringify({ time: Date.now(), data }));
  } catch {
    /* Viewing works when storage is full or disabled. */
  }
}
export async function request(path, { force = false, ttl = 180000 } = {}) {
  if (!path.startsWith('/api/')) throw new Error('Недопустимый адрес API');
  let instance = INSTANCE;
  const match = path.match(/^\/api\/v1\/(accounts|statuses)\/([^/?]+)/);
  if (match) {
    const id = decodeURIComponent(match[2]);
    if (!/^(?:ml:)?[0-9]{1,20}$/.test(id)) throw new Error('Недопустимый адрес API');
    instance = instanceForId(id);
    path = path.replace(match[2], id.replace(/^ml:/, ''));
  }
  const source = instance === INSTANCE ? 'mastodon' : 'legacy';
  const key = source + path;
  const saved = cached(key);
  if (!force && saved && Date.now() - saved.time < ttl)
    return { data: saved.data, stale: false, instance };
  if (inflight.has(key)) return inflight.get(key);
  const task = (async () => {
    // Register the promise before any synchronous failure (including cooldown).
    await acquireRequest();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    try {
      if (Date.now() < retryAfter)
        throw new Error('Сервер просит подождать перед следующим запросом');
      const response = await fetch(`/_northline/${source}${path}`, {
        signal: controller.signal,
        credentials: 'omit',
        redirect: 'error',
        headers: { Accept: 'application/json' },
      });
      if (response.status === 429) {
        const wait = response.headers.get('Retry-After');
        retryAfter =
          Date.now() +
          (Number(wait) > 0
            ? Number(wait) * 1000
            : Math.max(30000, Date.parse(wait) - Date.now() || 0));
      }
      if (!response.ok)
        throw new Error(
          response.status === 401 || response.status === 403
            ? 'Сервер ограничил гостевой доступ к этому разделу'
            : response.status === 404
              ? 'Запись или профиль больше недоступны'
              : response.status === 429
                ? 'Слишком много запросов. Попробуйте немного позже'
                : 'Не удалось загрузить публикации. Попробуйте ещё раз.',
        );
      const data = await response.json();
      cache(key, data);
      return { data, stale: false, instance };
    } catch (error) {
      if (saved && Date.now() - saved.time < 7 * 86400000)
        return { data: saved.data, stale: true, instance };
      throw error.name === 'AbortError'
        ? new Error('Сервер не ответил вовремя. Попробуйте ещё раз')
        : error;
    } finally {
      clearTimeout(timer);
      releaseRequest();
      inflight.delete(key);
    }
  })();
  inflight.set(key, task);
  return task;
}
export async function timeline(tag, { maxId = '', force = false } = {}) {
  const authors = Object.hasOwn(TOPIC_AUTHORS, tag) ? TOPIC_AUTHORS[tag] : null;
  const sources = authors || [...new Set(Object.values(TOPIC_AUTHORS).flat())];
  const pages = [];
  // Bound upstream concurrency, including the less common arbitrary hashtag view.
  for (let index = 0; index < sources.length; index += 3) {
    const batch = await Promise.allSettled(
      sources
        .slice(index, index + 3)
        .map((id) => accountPosts(`ml:${id}`, { maxId, force, russianOnly: false })),
    );
    pages.push(...batch);
  }
  const successful = pages.filter((page) => page.status === 'fulfilled');
  if (!successful.length) throw pages[0].reason;
  // On partial failure retain the cursor so a recovering author is not skipped.
  const partial = successful.length !== pages.length || successful.some((page) => page.value.stale);
  const candidates = successful
    .flatMap((page) => page.value.posts)
    .sort((a, b) =>
      a.cursor.length === b.cursor.length
        ? b.cursor.localeCompare(a.cursor)
        : b.cursor.length - a.cursor.length,
    );
  const scanned = candidates.slice(0, 20);
  const needle = tag.toLocaleLowerCase('ru');
  return {
    posts: uniquePosts(
      scanned
        .filter(isRussian)
        .filter(
          (post) =>
            authors || post.tags.some((item) => item.name.toLocaleLowerCase('ru') === needle),
        ),
    ),
    next: partial ? maxId : scanned.at(-1)?.cursor || '',
    stale: partial || successful.some((page) => page.value.stale),
    partial,
  };
}
export async function accountPosts(
  id,
  {
    maxId = '',
    media = false,
    pinned = false,
    force = false,
    russianOnly = true,
    originalsOnly = false,
  } = {},
) {
  const params = new URLSearchParams({ limit: '20', exclude_replies: 'true' });
  if (originalsOnly) params.set('exclude_reblogs', 'true');
  if (maxId) params.set('max_id', maxId);
  if (media) params.set('only_media', 'true');
  if (pinned) params.set('pinned', 'true');
  const result = await request(`/api/v1/accounts/${encodeURIComponent(id)}/statuses?${params}`, {
    force,
  });
  if (!Array.isArray(result.data)) throw new Error('Не удалось прочитать публикации автора');
  return {
    posts: result.data
      .map((raw) => normalizeMastodonStatus(raw, result.instance))
      .filter(
        (post) =>
          post &&
          (!russianOnly || isRussian(post)) &&
          (!originalsOnly || (!post.boostedBy && post.account.id === id)),
      ),
    next: result.data.length ? String(result.data.at(-1).id) : '',
    stale: result.stale,
  };
}
export function uniquePosts(posts) {
  const seen = new Set();
  return posts.filter((post) => {
    const key = post.uri || post.id;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
export function arrangePosts(posts) {
  const pool = uniquePosts(posts).sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt));
  const result = [];
  while (pool.length) {
    let index = pool.findIndex((post) => post.account.id !== result.at(-1)?.account.id);
    if (index < 0) index = 0;
    result.push(pool.splice(index, 1)[0]);
  }
  return result;
}
