import { formatDate, formatDateTime, relativeTime, refreshRelative, ago } from './date-utils.js';
import { storage, setTheme, getTheme } from './storage.js';
import { fetchJson } from './api-client.js';
import { escapeHtml } from './formatters.js';
import { announce } from './ui.js';

const monthPattern = /\b(\d{1,2})\s+(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)(?:\s+\d{4})?\b/gi;
const shortMonthPattern = /\b(\d{1,2})\s+(янв|фев|мар|апр|май|июн|июл|авг|сен|окт|ноя|дек)\b/gi;
const months = ['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря'];
const weekdays = ['воскресенье','понедельник','вторник','среда','четверг','пятница','суббота'];
function dynamicDate(seed, future = false) {
  const offset = ((Number(seed) || 1) % 21) + 1;
  const date = future ? new Date(Date.now() + offset * 86400000) : ago(offset * 86400000);
  return `${date.getDate()} ${months[date.getMonth()]}`;
}
function refreshStaticDates(root = document) {
  const walker = document.createTreeWalker(root.body || root, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  nodes.forEach((node, index) => {
    if (!node.nodeValue || node.parentElement?.closest('script,style')) return;
    let value = node.nodeValue.replace(/\b2026\b/g, String(new Date().getFullYear())).replace(/__CURRENT_YEAR__/g, String(new Date().getFullYear()));
    value = value.replace(/\b(воскресенье|понедельник|вторник|среда|четверг|пятница|суббота)\b/gi, weekdays[new Date().getDay()]);
    const future = Boolean(node.parentElement?.closest('[data-future-date], .maintenance, .events'));
    value = value.replace(monthPattern, (_, day) => dynamicDate(Number(day) + index, future));
    value = value.replace(shortMonthPattern, (_, day) => dynamicDate(Number(day) + index, future));
    value = value.replace(/\b\d+\s+минут(?:ы|у)?\s+назад\b/gi, () => relativeTime(ago(((index % 8) + 1) * 60000)));
    if (value !== node.nodeValue) node.nodeValue = value;
  });
}
function ensureState() {
  document.documentElement.dataset.theme = getTheme();
  document.querySelectorAll('[data-theme-toggle]').forEach((button) => button.addEventListener('click', () => setTheme(getTheme() === 'dark' ? 'light' : 'dark')));
  document.querySelectorAll('[data-save], .bookmark, .save').forEach((button, index) => {
    const key = `saved:${location.pathname}:${button.dataset.id || index}`;
    const saved = storage.get(key, false);
    button.setAttribute('aria-pressed', String(saved));
    button.addEventListener('click', () => { const value = !storage.get(key, false); storage.set(key, value); button.setAttribute('aria-pressed', String(value)); button.classList.toggle('is-saved', value); });
  });
}
function guardLinks() {
  document.addEventListener('click', (event) => {
    const link = event.target.closest('a[href="#"]');
    if (link) { event.preventDefault(); link.dispatchEvent(new CustomEvent('disguise:placeholder', { bubbles: true })); announce('Раздел доступен в демонстрационном режиме'); }
  });
}
async function loadOptionalFeeds() {
  const path = location.pathname;
  if (path.includes('07-fokus-news') || document.title.includes('Фокус')) {
    const fallback = [{ title: 'Открытые данные помогают планировать городские маршруты', url: '#', publishedAt: ago(86400000) }, { title: 'Исследователи представили карту сезонных изменений климата', url: '#', publishedAt: ago(3 * 86400000) }];
    const payload = await fetchJson('https://api.gdeltproject.org/api/v2/doc/doc?query=environment%20OR%20science&mode=artlist&format=json&maxrecords=6', { cacheKey: 'fokus:gdelt', ttl: 900000, fallback: { articles: fallback } });
    const items = Array.isArray(payload?.articles) && payload.articles.length ? payload.articles : fallback;
    const list = document.querySelector('.latest');
    if (list) {
      const header = list.querySelector('header');
      list.replaceChildren(header);
      items.slice(0, 6).forEach((item, index) => { const row = document.createElement('article'); const href = /^https:\/\//.test(item.url || '') ? item.url : '#'; const rawDate = item.publishedAt || (item.seendate ? item.seendate.replace(/^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2}).*$/, '$1-$2-$3T$4:$5:$6Z') : null); row.innerHTML = `<time>${escapeHtml(relativeTime(rawDate || ago((index + 1) * 3600000)))}</time><a rel="noopener" target="_blank" href="${escapeHtml(href)}">${escapeHtml(item.title || 'Публикация редакции')}</a>`; list.append(row); });
    }
  }
}
document.addEventListener('DOMContentLoaded', () => {
  refreshStaticDates();
  refreshRelative();
  ensureState();
  guardLinks();
  document.body.dataset.ready = 'true';
  loadOptionalFeeds();
});
export { refreshStaticDates, ensureState, formatDate, formatDateTime };
