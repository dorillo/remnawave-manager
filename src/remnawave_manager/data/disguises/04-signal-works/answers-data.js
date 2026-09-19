import { imageURL } from '../shared/image-proxy.js';
const API = '/_answers/mail/';
const CACHE = 'answers:feed:v1';
const MAX_TEXT = 12000;
let cooldown = 0;

export function safeImage(value) {
  if (typeof value !== 'string' || !value) return '';
  if (/^(?:blob:|data:image\/[a-z0-9.+-]+;base64,)/i.test(value)) return imageURL(value);
  // Mail supplies both bare gallery filenames and relative avatar URLs.
  // The optional leading slash also repairs URLs saved by older versions.
  const file = value.match(/^(?:\/api\/pictures\/images\/|\/)?([a-f0-9]{64,128}\.(?:jpg|jpeg|png|webp)(?:\?size=(?:small|medium|large|origin))?)$/);
  if (file) value = 'https://otvet.cdn-vk.net/api/pictures/images/' + file[1];
  if (!/^(?:https:\/\/|\/_images\/)/.test(value)) return '';
  const url = imageURL(value);
  return /^\/_images\/(?:otvet\.cdn-vk\.net|otvet-static\.cdn-vk\.net|filin\.mail\.ru)\//.test(url) ? url : '';
}

export function plainDoc(doc, limit = MAX_TEXT) {
  const out = [];
  let length = 0;
  const push = (value) => { const part = String(value).slice(0, limit - length); out.push(part); length += part.length; };
  function visit(node, depth = 0) {
    if (!node || depth > 20 || length >= limit) return;
    if (Array.isArray(node)) { node.slice(0, 200).forEach((part) => visit(part, depth + 1)); return; }
    if (typeof node !== 'object') return;
    if (node.type === 'text') push(node.text || '');
    else if (node.type === 'hardBreak') push('\n');
    else if (node.type === 'imageGallery') return;
    else {
      visit(node.content, depth + 1);
      if (['paragraph', 'heading', 'listItem', 'bulletList', 'orderedList', 'blockquote'].includes(node.type)) push('\n');
    }
  }
  visit(doc);
  return out.join('').replace(/\n{3,}/g, '\n\n').trim().slice(0, limit);
}

export function images(doc) {
  const found = [];
  function visit(node, depth = 0) {
    if (!node || depth > 15 || found.length >= 4) return;
    if (Array.isArray(node)) { node.slice(0, 100).forEach((item) => visit(item, depth + 1)); return; }
    if (typeof node !== 'object') return;
    if (node.type === 'imageGallery' && Array.isArray(node.attrs?.gallery)) {
      for (const item of node.attrs.gallery.slice(0, 4)) {
        const url = safeImage(item?.src);
        if (url && !found.includes(url)) found.push(url);
      }
    }
    visit(node.content, depth + 1);
  }
  visit(doc);
  return found;
}

const count = (value) => Number.isSafeInteger(value) && value >= 0 ? value : 0;
const date = (value) => typeof value === 'string' && !Number.isNaN(Date.parse(value)) ? value : '';
function author(value) {
  return { name: String(value?.nick || value?.username || 'Участник').slice(0, 80), avatar: safeImage(value?.avatar), id: count(value?.id) };
}
export function question(raw) {
  if (!raw || !Number.isSafeInteger(raw.id) || raw.id < 1 || typeof raw.title !== 'string') return null;
  if (Array.isArray(raw.spaces) && raw.spaces.some((x) => x?.adult === true)) return null;
  const spaces = Array.isArray(raw.spaces) ? raw.spaces.slice(0, 8).map((x) => ({ name: String(x?.title || '').slice(0, 70), path: String(x?.path || '').slice(0, 70) })).filter((x) => x.name) : [];
  return { id: `mail:${raw.id}`, sourceId: raw.id, title: raw.title.slice(0, 300), body: plainDoc(raw.content), images: images(raw.content), author: author(raw.author), spaces, created: date(raw.created_at), replies: count(raw.replies_count), votes: Number(raw.reaction_counter?.rating || 0), original: `https://otvet.mail.ru/question/${raw.id}` };
}
export function answer(raw) {
  if (!raw || !Number.isSafeInteger(raw.id) || raw.id < 1) return null;
  return { id: `mail-answer:${raw.id}`, sourceId: raw.id, body: plainDoc(raw.content), images: images(raw.content), author: author(raw.author), created: date(raw.created_at), parentId: count(raw.reply_to) || null, replies: count(raw.replyToReplyCount), votes: Number(raw.reaction_counter?.rating || 0) };
}
async function get(path, params = {}) {
  if (Date.now() < cooldown) throw new Error('rate-limit');
  const url = new URL(API + path, location.origin);
  for (const [key, value] of Object.entries(params)) if (value !== undefined && value !== null && value !== '') url.searchParams.set(key, String(value));
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetch(url, { credentials: 'omit', headers: { Accept: 'application/json' }, signal: controller.signal, redirect: 'error' });
    if (response.status === 429) { cooldown = Date.now() + Math.min(300000, Math.max(30000, Number(response.headers.get('Retry-After')) * 1000 || 30000)); throw new Error('rate-limit'); }
    if (!response.ok) throw new Error(response.status === 404 ? 'missing' : 'network');
    if (!(response.headers.get('content-type') || '').includes('application/json')) throw new Error('schema');
    const raw = await response.text();
    if (raw.length > 4000000) throw new Error('schema');
    const result = JSON.parse(raw)?.result;
    if (!result) throw new Error('schema');
    return result;
  } catch (error) { if (error.name === 'AbortError') throw new Error('timeout'); throw error; }
  finally { clearTimeout(timer); }
}

export async function feed(pos, space = '') {
  const result = await get('feed', { limit: 20, pos, space });
  if (!Array.isArray(result.feed)) throw new Error('schema');
  const items = result.feed.map(question).filter(Boolean);
  if (!pos && !space && items.length) { try { localStorage.setItem(CACHE, JSON.stringify({ at: Date.now(), items })); } catch {} }
  return { items, pos: count(result.params?.pos) || null };
}
function cachedQuestion(value) {
  const sourceId = count(value?.sourceId);
  if (!sourceId || value?.id !== `mail:${sourceId}` || typeof value.title !== 'string' || typeof value.body !== 'string') return null;
  const spaces = Array.isArray(value.spaces) ? value.spaces.slice(0, 8).map((item) => ({ name: String(item?.name || '').slice(0, 70), path: String(item?.path || '').slice(0, 70) })).filter((item) => item.name) : [];
  return { id:value.id, sourceId, title:value.title.slice(0,300), body:value.body.slice(0,MAX_TEXT), images:[], author:{name:String(value.author?.name || 'Участник').slice(0,80),avatar:safeImage(value.author?.avatar),id:count(value.author?.id)}, spaces, created:date(value.created), replies:count(value.replies), votes:Number(value.votes)||0, original:`https://otvet.mail.ru/question/${sourceId}` };
}
export function cachedFeed() {
  try {
    const value = JSON.parse(localStorage.getItem(CACHE) || 'null');
    if (!Number.isFinite(value?.at) || value.at <= Date.now() - 7 * 86400000 || !Array.isArray(value.items)) return null;
    const items = value.items.slice(0, 20).map(cachedQuestion).filter(Boolean);
    return items.length ? { at:value.at, items } : null;
  } catch { return null; }
}
export async function search(text) {
  const result = await get('search', { text: String(text).trim().slice(0, 120) });
  if (result.feed !== null && !Array.isArray(result.feed)) throw new Error('schema');
  return (result.feed || []).map(question).filter(Boolean);
}
export async function spaces() {
  const result = await get('spaces');
  if (!Array.isArray(result)) throw new Error('schema');
  return result.slice(0, 20).map((x) => ({ id: count(x.id), name: String(x.title || '').slice(0, 70), path: String(x.path || '').slice(0, 70) })).filter((x) => x.id && x.name);
}
export async function detail(id) {
  if (!/^\d{1,12}$/.test(String(id))) throw new Error('missing');
  // A question is useful on its own. Do not discard it merely because the
  // independently fetched list of replies is temporarily unavailable.
  const [postResult, repliesResult] = await Promise.allSettled([
    get(`question/${id}`),
    get(`answers/${id}`, { limit: 50 }),
  ]);
  if (postResult.status !== 'fulfilled') throw postResult.reason;
  const normalized = question(postResult.value);
  if (!normalized) throw new Error('schema');
  if (repliesResult.status !== 'fulfilled' || !Array.isArray(repliesResult.value.replies)) {
    return { question: normalized, answers: [], answersAvailable: false };
  }
  return { question: normalized, answers: repliesResult.value.replies.map(answer).filter(Boolean), answersAvailable: true };
}
