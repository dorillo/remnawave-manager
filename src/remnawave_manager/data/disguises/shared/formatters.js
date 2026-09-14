export const escapeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);
export const number = (value, locale = 'ru-RU') => new Intl.NumberFormat(locale).format(value);
export const compactNumber = (value, locale = 'ru-RU') => new Intl.NumberFormat(locale, { notation: 'compact', maximumFractionDigits: 1 }).format(value);
export const readTime = (text, wordsPerMinute = 180) => Math.max(1, Math.ceil(String(text || '').trim().split(/\s+/).length / wordsPerMinute));
