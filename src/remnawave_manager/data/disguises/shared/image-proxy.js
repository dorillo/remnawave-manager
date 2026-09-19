// Keep in sync with image_proxy.HOSTS; unknown origins never bypass the proxy.
export const imageHosts = new Set([
  'mastodon.ml',
  'mastodon.social',
  'files.mastodon.social',
  'pic.rtbcdn.ru',
  'pic.rutube.ru',
  'static.rutubelist.ru',
  'cdn-st.rutubelist.ru',
  'cdn-st.yappy.media',
  'otvet.cdn-vk.net',
  'filin.mail.ru',
  'otvet-static.cdn-vk.net',
  'ru.wikipedia.org',
  'upload.wikimedia.org',
  'thumb.wikimedia.org',
  'wikimedia.org',
]);
export function imageURL(value) {
  if (typeof value !== 'string' || !value) return '';
  // Locally uploaded images never leave the browser.
  if (/^(?:blob:|data:image\/[a-z0-9.+-]+;base64,)/i.test(value)) return value;
  try {
    const origin = globalThis.location?.origin || 'https://preview.invalid';
    let url = new URL(value, origin);
    if (url.username || url.password) return '';
    if (url.origin === origin && !url.pathname.startsWith('/_images/')) {
      return url.pathname + url.search;
    }
    if (url.origin === origin && url.pathname.startsWith('/_images/')) {
      url = new URL('https://' + url.pathname.slice('/_images/'.length) + url.search);
    }
    if (url.protocol !== 'https:' || url.username || url.password || url.port ||
        !imageHosts.has(url.hostname) ||
        /%(?:00|0a|0d|2f|5c|25)|\\|\/\//i.test(url.pathname)) return '';
    const allowed = url.hostname === 'filin.mail.ru' ? url.pathname === '/pic'
      : /\.(?:png|jpe?g|gif|webp|avif|svg|ico)$/i.test(url.pathname)
        || (url.hostname === 'ru.wikipedia.org' && /^\/api\/rest_v1\/media\/math\/render\/(?:svg|png)\/[a-f0-9]{1,128}$/.test(url.pathname));
    if (!allowed) return '';
    return '/_images/' + url.hostname + url.pathname + url.search;
  } catch { return ''; }
}
