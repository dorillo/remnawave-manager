/* Dates are always derived from the visitor's current clock. */
export const now = () => Date.now();
export const ago = (ms, clock = now()) => new Date(clock - ms);
export const fromNow = (ms, clock = now()) => new Date(clock + ms);
export function relativeTime(value, clock = now()) {
  const timestamp = value instanceof Date ? value.getTime() : new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return '';
  const delta = Math.max(0, clock - timestamp);
  const minutes = Math.floor(delta / 60000);
  if (minutes < 1) return 'только что';
  if (minutes < 60) return `${minutes} мин. назад`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} ч. назад`;
  return `${Math.floor(hours / 24)} дн. назад`;
}
export const formatDate = (value, locale = 'ru-RU') => {
  const date = value instanceof Date ? value : new Date(value);
  return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat(locale, { dateStyle: 'medium' }).format(date) : '';
};
export const formatDateTime = (value, locale = 'ru-RU') => {
  const date = value instanceof Date ? value : new Date(value);
  return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short' }).format(date) : '';
};
export const rollingDate = (days = 0, hours = 0, minutes = 0) => ago((days * 86400000) + (hours * 3600000) + (minutes * 60000));
export const dateRange = (hours, clock = now()) => `${formatDate(new Date(clock - hours * 3600000))} — ${formatDateTime(new Date(clock))}`;
export function refreshRelative(root = document) {
  root.querySelectorAll('[data-relative-time]').forEach((el) => { if (el.dataset.relativeTime) el.textContent = relativeTime(el.dataset.relativeTime); });
  root.querySelectorAll('[data-current-year]').forEach((el) => { el.textContent = String(new Date().getFullYear()); });
  root.querySelectorAll('[data-current-date]').forEach((el) => { el.textContent = formatDate(new Date()); });
}
if (typeof document !== 'undefined') {
  document.addEventListener('DOMContentLoaded', () => refreshRelative());
  setInterval(() => refreshRelative(), 60000);
}
