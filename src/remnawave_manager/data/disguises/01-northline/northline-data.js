// Network access is read-only and restricted to the configured instance.
export const INSTANCE = 'mastodon.social';
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
const PREFIX = 'northline:api:v4:';
const inflight = new Map();
let retryAfter = 0;

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
export function normalizeAccount(account = {}) {
  return {
    id: String(account.id || ''),
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
export function normalizeMastodonStatus(raw) {
  if (!raw || !raw.id || !raw.account) return null;
  const status = raw.reblog || raw;
  if (!status.id || !status.account) return null;
  return {
    id: String(status.id),
    cursor: String(raw.id),
    uri: status.uri || status.url || `${API}/@${status.account.acct}/${status.id}`,
    url: safeUrl(status.url),
    account: normalizeAccount(status.account),
    boostedBy: raw.reblog ? normalizeAccount(raw.account) : null,
    content: String(status.content || ''),
    text: plainText(status.content),
    createdAt: status.created_at,
    editedAt: status.edited_at,
    language: status.language || '',
    warning: String(status.spoiler_text || ''),
    sensitive: Boolean(status.sensitive),
    replyTo: status.in_reply_to_id,
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
  const saved = cached(path);
  if (!force && saved && Date.now() - saved.time < ttl) return { data: saved.data, stale: false };
  if (inflight.has(path)) return inflight.get(path);
  const task = (async () => {
    // Register the promise before any synchronous failure (including cooldown).
    await Promise.resolve();
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 12000);
    try {
      if (Date.now() < retryAfter)
        throw new Error('Сервер просит подождать перед следующим запросом');
      const response = await fetch(API + path, {
        signal: controller.signal,
        credentials: 'omit',
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
                : 'Не удалось загрузить данные Mastodon',
        );
      const data = await response.json();
      cache(path, data);
      return { data, stale: false };
    } catch (error) {
      if (saved && Date.now() - saved.time < 7 * 86400000) return { data: saved.data, stale: true };
      throw error.name === 'AbortError'
        ? new Error('Сервер не ответил вовремя. Попробуйте ещё раз')
        : error;
    } finally {
      clearTimeout(timer);
      inflight.delete(path);
    }
  })();
  inflight.set(path, task);
  return task;
}
export async function timeline(tag, { maxId = '', force = false } = {}) {
  const params = new URLSearchParams({ limit: '20' });
  if (maxId) params.set('max_id', maxId);
  const result = await request(`/api/v1/timelines/tag/${encodeURIComponent(tag)}?${params}`, {
    force,
  });
  if (!Array.isArray(result.data)) throw new Error('Сервер вернул некорректную ленту');
  return {
    posts: result.data.map(normalizeMastodonStatus).filter(Boolean),
    next: result.data.length ? String(result.data.at(-1).id) : '',
    stale: result.stale,
  };
}
export async function accountPosts(id, { maxId = '', media = false, pinned = false } = {}) {
  const params = new URLSearchParams({ limit: '20', exclude_replies: 'true' });
  if (maxId) params.set('max_id', maxId);
  if (media) params.set('only_media', 'true');
  if (pinned) params.set('pinned', 'true');
  const result = await request(`/api/v1/accounts/${encodeURIComponent(id)}/statuses?${params}`);
  if (!Array.isArray(result.data)) throw new Error('Не удалось прочитать публикации автора');
  return {
    posts: result.data.map(normalizeMastodonStatus).filter(Boolean),
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
export function arrangePosts(posts, language = 'all') {
  const pool = uniquePosts(posts)
    .filter((post) => language === 'all' || post.language.split('-')[0] === language)
    .sort((a, b) => Date.parse(b.createdAt) - Date.parse(a.createdAt));
  const result = [];
  while (pool.length) {
    let index = pool.findIndex((post) => post.account.id !== result.at(-1)?.account.id);
    if (index < 0) index = 0;
    result.push(pool.splice(index, 1)[0]);
  }
  return result;
}
