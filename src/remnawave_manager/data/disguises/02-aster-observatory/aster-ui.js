import { imageURL } from '../shared/image-proxy.js';
import { t } from './aster-i18n.js';

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if ((tag === 'img' && key === 'src') || key === 'poster')
      node.setAttribute(key, imageURL(value));
    else if (value !== false && value != null)
      node.setAttribute(key, value === true ? '' : String(value));
  }
  node.append(...children.flat().filter((child) => child != null));
  return node;
}
export const button = (text, action, attrs = {}) =>
  el('button', { type: 'button', onclick: action, ...attrs }, text);
export const link = (text, href, attrs = {}) =>
  el('a', { href, ...attrs }, text);
let toastTimer;
export function toast(message) {
  const node = document.querySelector('#toast');
  const dialog = document.querySelector('dialog[open]');
  if (dialog) {
    let notice = dialog.querySelector('.dialog-notice');
    if (!notice) {
      notice = el('p', { class: 'dialog-notice', role: 'status' });
      dialog.append(notice);
    }
    notice.textContent = message;
  }
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    node.hidden = true;
  }, 5000);
}
export function modal(title, content) {
  const previous = document.activeElement;
  const titleId = `dialog-${crypto.randomUUID()}`;
  const dialog = el('dialog', { 'aria-labelledby': titleId });
  let filePickerOpen = false;
  const cleanup = () => {
    dialog.remove();
    if (previous?.isConnected) previous.focus();
  };
  const close = () => {
    dialog.close();
  };
  dialog.append(
    el(
      'header',
      { class: 'dialog-head' },
      el('h2', { id: titleId }, title),
      button('×', close, { class: 'icon-button', 'aria-label': t('close') }),
    ),
    content,
  );
  dialog.addEventListener('click', (event) => {
    if (!(event.target instanceof HTMLInputElement) || event.target.type !== 'file')
      return;
    filePickerOpen = true;
    const reset = () => {
      setTimeout(() => {
        filePickerOpen = false;
      }, 250);
    };
    event.target.addEventListener('change', reset, { once: true });
    event.target.addEventListener('cancel', reset, { once: true });
    window.addEventListener('focus', reset, { once: true });
  });
  dialog.addEventListener('cancel', (event) => {
    event.preventDefault();
    if (!filePickerOpen) close();
  });
  dialog.addEventListener('close', () => {
    if (dialog.isConnected) cleanup();
  });
  document.body.append(dialog);
  dialog.showModal();
  return { dialog, close };
}
export function confirmAction(title, action) {
  const content = el('div', { class: 'stack' });
  const controls = modal(title, content);
  content.append(
    el(
      'div',
      { class: 'dialog-actions' },
      button(t('cancel'), controls.close),
      button(
        t('confirm'),
        () => {
          try {
            action();
            controls.close();
          } catch {
            toast(t('storageError'));
          }
        },
        { class: 'primary' },
      ),
    ),
  );
}
export function field(label, input) {
  return el('label', { class: 'field' }, el('span', {}, label), input);
}
