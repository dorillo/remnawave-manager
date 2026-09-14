const keyFor = (key) => `disguise:api:${key}`;
export function normalizeItem(item = {}, index = 0) {
  const relative = typeof item.publishedAt === 'string' && item.publishedAt.match(/^relative:(\d+)d$/);
  const publishedAt = relative ? new Date(Date.now() - Number(relative[1]) * 86400000).toISOString() : item.publishedAt || new Date(Date.now() - index * 3600000).toISOString();
  return { id: item.id || `item-${index}`, title: String(item.title || ''), summary: String(item.summary || item.description || ''), image: item.image || '', url: item.url || '#', category: item.category || 'general', author: item.author || 'Редакция', publishedAt, source: item.source || 'Локальная коллекция' };
}
export function readCache(key, ttl = 300000) {
  try { const item = JSON.parse(localStorage.getItem(keyFor(key)) || 'null'); return item && Date.now() - item.time < ttl ? item.data : null; } catch { return null; }
}
export function writeCache(key, data) { try { localStorage.setItem(keyFor(key), JSON.stringify({ time: Date.now(), data })); } catch { /* private mode */ } }
export async function fetchJson(url, { fallback = [], timeout = 5000, cacheKey, ttl = 300000, headers = {}, map = (value) => value } = {}) {
  if (!url || typeof fetch !== 'function') return fallback;
  const cached = cacheKey ? readCache(cacheKey, ttl) : null;
  if (cached !== null) return cached;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(url, { signal: controller.signal, headers, credentials: 'omit' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const value = map(await response.json());
    if (cacheKey) writeCache(cacheKey, value);
    return value;
  } catch { return fallback; } finally { clearTimeout(timer); }
}
export async function fetchText(url, options = {}) {
  const { fallback = '', timeout = 5000, cacheKey, ttl = 300000, headers = {} } = options;
  if (!url || typeof fetch !== 'function') return fallback;
  const cached = cacheKey ? readCache(cacheKey, ttl) : null;
  if (cached !== null) return cached;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(url, { signal: controller.signal, headers, credentials: 'omit' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const value = await response.text();
    if (cacheKey) writeCache(cacheKey, value);
    return value;
  } catch { return fallback; } finally { clearTimeout(timer); }
}
