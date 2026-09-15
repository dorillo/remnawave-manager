import { t } from './morrow-i18n.js';
export function el(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attributes)) {
    if (key.startsWith('on') && typeof value === 'function')
      node.addEventListener(key.slice(2), value);
    else if (key === 'class') node.className = value;
    else if (key === 'value') node.value = value;
    else if (value !== false && value != null)
      node.setAttribute(key, value === true ? '' : value);
  }
  node.append(...children.flat().filter((child) => child != null));
  return node;
}
export const button = (label, action, className = '') =>
  el('button', { type: 'button', class: className, onclick: action }, label);
export const field = (label, input) =>
  el('label', { class: 'field' }, el('span', {}, label), input);
export function select(values, value, change) {
  const input = el(
    'select',
    { onchange: (event) => change?.(event.target.value) },
    values.map(([key, label]) => el('option', { value: key }, label)),
  );
  input.value = value;
  return input;
}
let timer;
export function notice(message) {
  const dialog = [...document.querySelectorAll('dialog[open]')].at(-1);
  let node = document.getElementById('notice');
  if (dialog) {
    node.hidden = true;
    node = dialog.querySelector('.dialog-notice');
    if (!node) {
      node = el('p', { class: 'dialog-notice', role: 'status' });
      dialog.append(node);
    }
  }
  node.textContent = message;
  node.hidden = false;
  clearTimeout(timer);
  timer = setTimeout(() => {
    node.hidden = true;
  }, 7000);
}
export function modal(title, content) {
  const previous = document.activeElement;
  const heading = el('h2', { id: 'dialog-' + crypto.randomUUID() }, title);
  const dialog = el(
    'dialog',
    { 'aria-labelledby': heading.id },
    el(
      'header',
      {},
      heading,
      button(t('close'), () => dialog.close()),
    ),
    content,
  );
  document.body.append(dialog);
  dialog.showModal();
  const close = dialog.close.bind(dialog);
  dialog.close = () => {
    close();
    dialog.remove();
  };
  dialog.addEventListener('close', () => {
    dialog.remove();
    if (!document.querySelector('dialog[open]') && previous?.isConnected)
      previous.focus();
  });
  return dialog;
}
export function confirmAction(title, description, action, label = 'delete') {
  const error = el('p', { class: 'error', role: 'alert' });
  const accept = button(
    t(label),
    async () => {
      accept.disabled = true;
      try {
        await action();
        dialog.close();
      } catch (e) {
        error.textContent = t(e.message);
        accept.disabled = false;
      }
    },
    'danger',
  );
  const dialog = modal(
    title,
    el(
      'div',
      {},
      el('p', {}, description),
      error,
      el(
        'div',
        { class: 'actions' },
        button(t('cancel'), () => dialog.close()),
        accept,
      ),
    ),
  );
}
export async function copy(text) {
  try {
    await navigator.clipboard.writeText(text);
    notice(t('copied'));
  } catch {
    notice(t('copyFailed'));
  }
}
function inline(node, text) {
  const pattern = /(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^\s)]+\))/g;
  let offset = 0;
  for (const match of text.matchAll(pattern)) {
    node.append(text.slice(offset, match.index));
    const value = match[0];
    if (value.startsWith('`')) node.append(el('code', {}, value.slice(1, -1)));
    else if (value.startsWith('**'))
      node.append(el('strong', {}, value.slice(2, -2)));
    else {
      const link = /^\[([^\]]+)\]\((.+)\)$/.exec(value);
      let url;
      try {
        url = new URL(link[2]);
      } catch {
        /* Render invalid links as text. */
      }
      if (url?.protocol === 'https:' && !url.username && !url.password)
        node.append(
          el(
            'a',
            { href: url.href, target: '_blank', rel: 'noopener noreferrer' },
            link[1],
          ),
        );
      else node.append(value);
    }
    offset = match.index + value.length;
  }
  node.append(text.slice(offset));
}
// A deliberately small Markdown renderer. All content enters the DOM as text.
export function markdown(text) {
  const root = el('div', { class: 'markdown' });
  const lines = text.split('\n');
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (line.startsWith('```')) {
      const code = [];
      while (++i < lines.length && !lines[i].startsWith('```'))
        code.push(lines[i]);
      const value = code.join('\n');
      root.append(
        el(
          'div',
          { class: 'code-block' },
          button(t('copy'), () => copy(value)),
          el('pre', {}, el('code', {}, value)),
        ),
      );
    } else if (
      line.includes('|') &&
      /^\s*\|?\s*:?-{3,}/.test(lines[i + 1] || '')
    ) {
      const cells = (row) =>
        row
          .trim()
          .replace(/^\||\|$/g, '')
          .split('|')
          .slice(0, 20);
      const row = (value, tag) =>
        el(
          'tr',
          {},
          cells(value).map((c) => {
            const node = el(tag);
            inline(node, c.trim());
            return node;
          }),
        );
      const table = el('table', {}, el('thead', {}, row(line, 'th')));
      const body = el('tbody');
      i++;
      while (i + 1 < lines.length && lines[i + 1].includes('|'))
        body.append(row(lines[++i], 'td'));
      table.append(body);
      root.append(el('div', { class: 'table-scroll' }, table));
    } else {
      const heading = /^(#{1,4})\s+(.*)/.exec(line);
      const item = /^\s*(?:[-*]|\d+\.)\s+(.*)/.exec(line);
      const quote = /^>\s?(.*)/.exec(line);
      const node = el(
        heading ? `h${heading[1].length + 1}` : quote ? 'blockquote' : 'p',
        { class: item ? 'list-line' : '' },
      );
      inline(
        node,
        heading ? heading[2] : item ? '• ' + item[1] : quote ? quote[1] : line,
      );
      root.append(node);
    }
  }
  return root;
}
