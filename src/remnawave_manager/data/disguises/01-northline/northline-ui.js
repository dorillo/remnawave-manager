import { loadingNode } from '../shared/feedback.js';
import { imageURL } from '../shared/image-proxy.js';
import { safeUrl } from './northline-data.js';
import { translate } from './northline-i18n.js';
export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = translate(value);
    else if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(), value);
    else if (key === 'poster') node.setAttribute(key, imageURL(value));
    else node.setAttribute(key, value === true ? '' : String(['aria-label', 'title', 'placeholder', 'alt'].includes(key) ? translate(value) : value));
  }
  for (const child of [children].flat(Infinity))
    if (child != null && child !== false)
      node.append(child instanceof Node ? child : translate(String(child)));
  return node;
}
const paths = {
  'brand-route': 'M4 17c3-8 6-11 9-9 2 1.3 1.1 5.4 3.5 6.8 1.2.7 2.5.2 3.5-.8M5 7h.01M19 17h.01',
  home: 'm3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1Z',
  compass: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM16 8l-3 5-5 3 3-5Z',
  search: 'M20 20l-5-5M17 10a7 7 0 1 1-14 0 7 7 0 0 1 14 0Z',
  bookmark: 'M6 3h12v18l-6-4-6 4Z',
  heart:
    'M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8Z',
  users:
    'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8',
  user: 'M20 21v-2a6 6 0 0 0-6-6h-4a6 6 0 0 0-6 6v2M16 6a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z',
  message: 'M21 11.5a8.5 8.5 0 0 1-8.5 8.5H3l2-5a8.5 8.5 0 1 1 16-3.5Z',
  repeat: 'm17 2 4 4-4 4M3 11V8a2 2 0 0 1 2-2h16M7 22l-4-4 4-4m14-1v3a2 2 0 0 1-2 2H3',
  share: 'M12 16V3m-5 5 5-5 5 5M5 13v7h14v-7',
  external: 'M15 3h6v6m0-6L10 14M11 3H4a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h16a1 1 0 0 0 1-1v-7',
  plus: 'M12 5v14M5 12h14',
  check: 'm5 12 4 4L19 6',
  close: 'm6 6 12 12M6 18 18 6',
  arrow: 'm12 5-7 7 7 7M5 12h14',
  down: 'm6 9 6 6 6-6',
  refresh: 'M20 7a9 9 0 1 0 1 8M20 2v6h-6',
  edit: 'm16 3 5 5-12 12-6 1 1-6Z',
  camera: 'M3 6h4l2-3h6l2 3h4v15H3ZM16 13a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z',
  palette:
    'M12 3a9 9 0 1 0 0 18h1a2 2 0 0 0 1-3.7 1 1 0 0 1 .5-1.8H17a4 4 0 0 0 4-4A9 9 0 0 0 12 3ZM7 10h.01M10 7h.01M15 7h.01M17 11h.01',
  leaf: 'M20 3C8 2 1 9 5 16s17 3 15-13ZM4 21l11-12',
  book: 'M12 5C8 2 4 3 2 4v16c4-2 7-1 10 1 3-2 6-3 10-1V4c-2-1-6-2-10 1Zm0 0v16',
  globe: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0ZM3 12h18M12 3c5 5 5 13 0 18-5-5-5-13 0-18Z',
  settings: 'M4 7h16M4 17h16M8 4v6m8 4v6',
  more: 'M5 12h.01M12 12h.01M19 12h.01',
};
export function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  for (const [key, val] of Object.entries({
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    'stroke-width': '1.7',
    'stroke-linecap': 'round',
    'stroke-linejoin': 'round',
    'aria-hidden': 'true',
    class: 'icon',
  }))
    svg.setAttribute(key, val);
  const path = document.createElementNS(svg.namespaceURI, 'path');
  path.setAttribute('d', paths[name] || paths.globe);
  svg.append(path);
  return svg;
}
export function button(label, action, { symbol, className = 'button', ...attrs } = {}) {
  return el('button', { type: 'button', class: className, onclick: action, ...attrs }, [
    symbol && icon(symbol),
    attrs.disabled && label === 'Загружаем…' ? loadingNode(translate(label)) : el('span', { text: label }),
  ]);
}
export function iconButton(label, symbol, action, attrs = {}) {
  return button(label, action, {
    symbol,
    className: 'icon-button',
    'aria-label': label,
    title: label,
    ...attrs,
  });
}
export function externalLink(label, url, className = '') {
  const href = safeUrl(url);
  return href
    ? el('a', { href, class: className, target: '_blank', rel: 'noopener noreferrer' }, label)
    : el('span', { class: className }, label);
}
export function picture(url, alt = '', className = '') {
  const img = el('img', {
    src: imageURL(url),
    alt,
    class: className,
    loading: 'lazy',
    decoding: 'async',
    referrerpolicy: 'no-referrer',
  });
  img.addEventListener(
    'error',
    () => {
      img.replaceWith(
        el('span', {
          class: `image-unavailable ${className}`,
          text: alt || 'Изображение недоступно',
        }),
      );
    },
    { once: true },
  );
  return img;
}
export function avatar(account, large = false) {
  const box = el(
    'span',
    { class: `avatar${large ? ' avatar-large' : ''}`, 'aria-hidden': 'true' },
    (account?.name || 'L').slice(0, 1).toUpperCase(),
  );
  if (imageURL(account?.avatar)) {
    const image = picture(account.avatar, '', 'avatar-image');
    image.addEventListener('error', () => {
      box.textContent = (account.name || 'L').slice(0, 1).toUpperCase();
    });
    box.replaceChildren(image);
  }
  return box;
}
// Rebuild a small allowlist of elements. Never insert remote HTML into the live DOM.
export function richText(html) {
  const parsed = new DOMParser().parseFromString(String(html || ''), 'text/html');
  const output = el('div', { class: 'rich-text' });
  const allowed = new Set([
    'P',
    'BR',
    'A',
    'SPAN',
    'STRONG',
    'B',
    'EM',
    'I',
    'CODE',
    'PRE',
    'BLOCKQUOTE',
    'UL',
    'OL',
    'LI',
    'DEL',
  ]);
  const visit = (source, parent) => {
    if (source.nodeType === Node.TEXT_NODE) {
      parent.append(source.textContent);
      return;
    }
    if (
      source.nodeType !== Node.ELEMENT_NODE ||
      ['SCRIPT', 'STYLE', 'IFRAME', 'OBJECT', 'SVG', 'FORM'].includes(source.tagName)
    )
      return;
    if (source.classList.contains('invisible')) return;
    if (!allowed.has(source.tagName)) {
      for (const child of source.childNodes) visit(child, parent);
      return;
    }
    const target = el(source.tagName.toLowerCase());
    if (source.tagName === 'A') {
      const href = safeUrl(source.getAttribute('href'));
      const tag = source.classList.contains('hashtag')
        ? source.textContent.replace(/^#/, '').trim()
        : '';
      if (tag && /^[\p{L}\p{N}_]+$/u.test(tag))
        target.setAttribute('href', `#/tag/${encodeURIComponent(tag)}`);
      else if (href) {
        target.setAttribute('href', href);
        target.setAttribute('target', '_blank');
        target.setAttribute('rel', 'noopener noreferrer');
      }
    }
    for (const child of source.childNodes) visit(child, target);
    if (source.classList.contains('ellipsis')) target.append('…');
    parent.append(target);
  };
  for (const child of parsed.body.childNodes) visit(child, output);
  return output;
}
export function empty(title, description, action) {
  return el('section', { class: 'empty-state' }, [
    el('span', { class: 'empty-icon' }, icon('compass')),
    el('h2', { text: title }),
    el('p', { text: description }),
    action,
  ]);
}
export function skeleton() {
  return el(
    'div',
    { class: 'skeletons', role: 'status', 'aria-label': 'Загрузка публикаций' },
    [1, 2, 3].map(() =>
      el('div', { class: 'skeleton card', 'aria-hidden': 'true' }, [
        el('div', { class: 'skeleton-author' }),
        el('div', { class: 'skeleton-line' }),
        el('div', { class: 'skeleton-line short' }),
        el('div', { class: 'skeleton-media' }),
      ]),
    ),
  );
}
let toastTimer;
export function toast(message) {
  let node = document.querySelector('#toast');
  if (!node) {
    node = el('div', { id: 'toast', class: 'toast', role: 'status' });
    document.body.append(node);
  }
  node.textContent = translate(message);
  const dialogs = [...document.querySelectorAll('dialog[open]')];
  (dialogs.at(-1)?.querySelector('.modal-content') || document.body).append(node);
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    node.hidden = true;
  }, 4500);
}
let dialogId = 0;
export function dialog(title, { wide = false, onClose = () => {} } = {}) {
  const previous = document.activeElement;
  const titleId = `dialog-title-${++dialogId}`;
  const modal = el('dialog', {
    class: `modal${wide ? ' modal-wide' : ''}`,
    'aria-labelledby': titleId,
  });
  const content = el('div', { class: 'modal-content' });
  const close = () => modal.close();
  modal.append(
    el('header', { class: 'modal-header' }, [
      el('h2', { id: titleId, text: title }),
      iconButton('Закрыть', 'close', close),
    ]),
    content,
  );
  modal.addEventListener('click', (event) => {
    if (
      event.target === modal &&
      (event.clientX < modal.getBoundingClientRect().left ||
        event.clientX > modal.getBoundingClientRect().right ||
        event.clientY < modal.getBoundingClientRect().top ||
        event.clientY > modal.getBoundingClientRect().bottom)
    )
      close();
  });
  modal.addEventListener(
    'close',
    () => {
      onClose();
      const notification = modal.querySelector('#toast');
      if (notification) {
        const remaining = [...document.querySelectorAll('dialog[open]')].filter((item) => item !== modal);
        (remaining.at(-1)?.querySelector('.modal-content') || document.body).append(notification);
      }
      modal.remove();
      document.body.classList.toggle('dialog-open', Boolean(document.querySelector('dialog[open]')));
      if (previous?.isConnected) previous.focus();
    },
    { once: true },
  );
  document.body.append(modal);
  document.body.classList.add('dialog-open');
  modal.showModal();
  return { modal, content, close };
}
