import { guestPrompt } from '../shared/guest-state.js';
// Each visit starts with the system theme; a manual toggle applies to this visit.
const appearanceMedia = matchMedia('(prefers-color-scheme: dark)');
let appearance = 'system';
function applyAppearance() {
  document.documentElement.dataset.theme = appearance === 'system'
    ? (appearanceMedia.matches ? 'dark' : 'light') : appearance;
}
appearanceMedia.addEventListener('change', applyAppearance);
applyAppearance();
function toggleTheme() {
  appearance = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  applyAppearance();
}
import {
  instanceForId,
  isRussian,
  TOPICS,
  request,
  timeline,
  accountPosts,
  normalizeAccount,
  normalizeMastodonStatus,
  uniquePosts,
  arrangePosts,
  safeUrl,
  plainText,
} from './northline-data.js';
import { readReader, writeReader, readLastFeed, writeLastFeed } from './northline-store.js';
import {
  readState,
  writeState,
  currentUser,
  createSession,
  logout,
  hashPassword,
  verifyPassword,
  validatePassword,
  newId,
  KEY as AUTH_KEY,
} from './northline-auth.js';
import {
  el,
  icon,
  button,
  iconButton,
  externalLink,
  picture,
  avatar,
  richText,
  empty,
  skeleton,
  toast,
  dialog,
} from './northline-ui.js';
import {
  getInterfaceLanguage,
  getInterfaceLocale,
  setInterfaceLanguage,
  translate,
} from './northline-i18n.js';
import {
  relativeTime as sharedRelativeTime,
  formatDateTime as sharedFormatDateTime,
  formatDate as sharedFormatDate,
} from '../shared/date-utils.js';

const root = document.querySelector('#app');
const relativeTime = (value) => {
  if (getInterfaceLanguage() === 'ru') return sharedRelativeTime(value);
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return '';
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  const formatter = new Intl.RelativeTimeFormat(getInterfaceLocale(), { numeric: 'auto' });
  if (seconds < 60) return formatter.format(-seconds, 'second');
  if (seconds < 3600) return formatter.format(-Math.floor(seconds / 60), 'minute');
  if (seconds < 86400) return formatter.format(-Math.floor(seconds / 3600), 'hour');
  return formatter.format(-Math.floor(seconds / 86400), 'day');
};
const formatDateTime = (value) => sharedFormatDateTime(value, getInterfaceLocale());
const formatDate = (value) => sharedFormatDate(value, getInterfaceLocale());
function replace(target, ...children) {
  target.replaceChildren(
    ...children.flat(Infinity).filter((child) => child != null && child !== false),
  );
}
let reader = readReader();
const MAX_ATTACHMENTS = 4;
function localMedia(value) {
  return Array.isArray(value)
    ? value
        .filter((media) => media && typeof media === 'object' && /^data:(?:image|video)\/[a-z0-9.+-]+;base64,/i.test(media.url || ''))
        .slice(0, MAX_ATTACHMENTS)
        .map((media) => ({
          id: String(media.id || newId('media')),
          type: media.type === 'video' ? 'video' : 'image',
          url: String(media.url),
          preview: media.type === 'video' ? '' : String(media.url),
          alt: String(media.alt || ''),
        }))
    : [];
}
function readFileAsDataUrl(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('Не удалось прочитать файл'));
    reader.onload = () => resolve(String(reader.result || ''));
    reader.readAsDataURL(file);
  });
}
async function imageDataUrl(file) {
  const source = URL.createObjectURL(file);
  try {
    const image = await new Promise((resolve, reject) => {
      const node = new Image();
      node.onload = () => resolve(node);
      node.onerror = () => reject(new Error('Не удалось открыть изображение'));
      node.src = source;
    });
    const scale = Math.min(1, 1600 / Math.max(image.naturalWidth || image.width, image.naturalHeight || image.height));
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round((image.naturalWidth || image.width) * scale));
    canvas.height = Math.max(1, Math.round((image.naturalHeight || image.height) * scale));
    canvas.getContext('2d').drawImage(image, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/webp', 0.82));
    return blob ? readFileAsDataUrl(blob) : readFileAsDataUrl(file);
  } catch {
    return readFileAsDataUrl(file);
  } finally {
    URL.revokeObjectURL(source);
  }
}
const activeUser = () => currentUser(readState());
const signedIn = () => Boolean(activeUser());
const escapeHtml = (value) =>
  String(value || '').replace(/[&<>"']/g, (character) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[character]);
function requireAuth(action = 'Это действие') {
  if (signedIn()) return true;
  showAuthDialog(action);
  return false;
}
const feeds = new Map();
const posts = new Map();
const accounts = new Map();
const profilePages = new Map();
const contexts = new Map();
const searches = new Map();
const scrollPositions = new Map();
let trends = [];
let renderVersion = 0;
let previousHash = location.hash || '#/feed';
let polling = false;
const number = (value) =>
  new Intl.NumberFormat(getInterfaceLocale(), {
    notation: Number(value) >= 10000 ? 'compact' : 'standard',
    maximumFractionDigits: 1,
  }).format(Number(value) || 0);
const routeLink = (path, children, className = '') =>
  el('a', { href: `#/${path}`, class: className }, children);
function route() {
  const raw = (location.hash || '#/feed').replace(/^#\/?/, '');
  const [path, query = ''] = raw.split('?');
  const pieces = path.split('/').map((s) => {
    try {
      return decodeURIComponent(s);
    } catch {
      return s;
    }
  });
  return {
    page: pieces[0] || 'feed',
    id: pieces.slice(1).join('/'),
    params: new URLSearchParams(query),
  };
}
function persist(state) {
  const saved = writeReader(reader, state);
  if (!saved)
    toast('Не удалось сохранить изменения. Попробуйте ещё раз.');
  return saved;
}
function reloadReader() {
  reader = readReader();
  feeds.delete('following');
  feeds.delete('overview');
  for (const [id, post] of posts) if (post.local) posts.delete(id);
  for (const [id] of accounts) if (id.startsWith('local:')) accounts.delete(id);
  for (const post of reader.localPosts) remember([localPostObject(post)]);
}
function signOut() {
  confirmAction(
    'Выйти из профиля?',
    'Несохранённый текст в открытых формах будет потерян. Публикации, реакции и черновики останутся в профиле.',
    'Выйти',
    () => {
      const state = readState();
      if (!logout(state)) { toast('Не удалось выйти из профиля. Попробуйте ещё раз.'); return; }
      reloadReader();
      render();
      toast('Вы вышли из профиля');
    },
  );
}
function confirmAction(title, description, label, action) {
  const modal = dialog(title);
  modal.content.append(
    el('p', { text: description }),
    el('div', { class: 'form-actions confirm-actions' }, [
      button('Отмена', () => modal.close(), { className: 'button secondary' }),
      button(label, () => { action(); modal.close(); }, { className: 'button danger' }),
    ]),
  );
}
function showAuthDialog() {
  const modal = dialog('Добро пожаловать в Line');
  let register = false;
  const content = el('div');
  const draw = () => {
    const login = el('input', {
      name: 'login',
      autocomplete: register ? 'username' : 'username',
      maxlength: '30',
      placeholder: register ? 'например, quiet_river' : 'Ваш логин',
      required: true,
    });
    const name = el('input', {
      name: 'name',
      autocomplete: 'name',
      maxlength: '60',
      placeholder: 'Как к вам обращаться?',
      required: true,
    });
    const password = el('input', {
      name: 'password',
      type: 'password',
      autocomplete: register ? 'new-password' : 'current-password',
      minlength: '8',
      maxlength: '128',
      placeholder: 'Не менее 8 символов',
      required: true,
    });
    const authError = el('p', { class: 'auth-error', role: 'alert', hidden: true });
    const showError = (message, control) => {
      authError.textContent = translate(message);
      authError.hidden = false;
      control?.focus();
    };
    replace(
      content,
      el('p', {
        text: register
          ? 'Зарегистрируйте профиль, чтобы присоединиться к Line.'
          : 'Войдите в свой профиль.',
      }),
      el(
        'form',
        {
          class: 'form auth-form',
          onsubmit: async (event) => {
            event.preventDefault();
            const form = event.currentTarget;
            const submit = form.querySelector('[type="submit"]');
            if (submit.disabled) return;
            submit.disabled = true;
            authError.hidden = true;
            try {
            const username = login.value.trim().toLowerCase();
            const secret = password.value;
            if (register) {
              if (!/^[a-z0-9_]{3,30}$/.test(username)) {
                showError('Логин: 3–30 латинских букв, цифр или подчёркиваний.', login);
                return;
              }
              if (!validatePassword(secret)) {
                showError('Пароль должен содержать от 8 до 128 символов.', password);
                return;
              }
              const state = readState();
              if (state.accounts.some((account) => account.username === username)) {
                showError('Этот логин уже занят.', login);
                return;
              }
              const guest = reader;
              const account = {
                id: newId('user'),
                username,
                displayName: name.value.trim() || username,
                password: await hashPassword(secret),
                createdAt: new Date().toISOString(),
              };
              state.accounts.push(account);
              state.profiles[account.id] = {
                reader: { ...guest, name: account.displayName, username, bio: '', avatar: '', saved: {}, favourites: {}, likes: {}, boosts: {}, votes: {}, follows: {}, drafts: [], localPosts: [] },
              };
              if (!form.isConnected) return;
              if (!createSession(state, account.id)) { showError('Не удалось сохранить профиль. Освободите место и повторите попытку.'); return; }
              reloadReader();
              modal.close();
              render();
              toast(`Профиль @${username} создан`);
              return;
            }
            const state = readState();
            const account = state.accounts.find((item) => item.username === username);
            if (!account || !(await verifyPassword(secret, account.password))) {
              showError('Неверный логин или пароль.', password);
              return;
            }
            if (!form.isConnected) return;
            if (!createSession(state, account.id)) { showError('Не удалось сохранить вход. Повторите попытку.'); return; }
            reloadReader();
            modal.close();
            render();
            toast(`С возвращением, ${reader.name || account.displayName}`);
            } catch {
              showError('Не удалось выполнить вход. Проверьте доступность хранилища и повторите попытку.');
            } finally {
              submit.disabled = false;
            }
          },
        },
        [
          authError,
          el('label', {}, ['Логин', login]),
          register && el('label', {}, ['Имя в профиле', name]),
          el('label', {}, ['Пароль', password]),
          el('button', { type: 'submit', class: 'button primary', text: register ? 'Зарегистрироваться' : 'Войти' }),
        ],
      ),
      button(register ? 'У меня уже есть профиль' : 'Регистрация', () => {
        register = !register;
        draw();
      }, { className: 'button secondary auth-switch' }),
    );
    (register ? login : login).focus();
  };
  modal.content.append(content);
  draw();
}
function remember(items) {
  for (const post of items) {
    posts.set(post.id, post);
    accounts.set(post.account.id, post.account);
  }
}
for (const post of [...Object.values(reader.saved), ...Object.values(reader.favourites)])
  if (post?.account && post.id) remember([post]);
for (const post of reader.localPosts) remember([localPostObject(post)]);
for (const account of Object.values(reader.follows))
  if (account?.id) accounts.set(account.id, account);
const lastFeed = readLastFeed();
if (lastFeed) remember(lastFeed.posts.filter((post) => post?.account && post.id));

const main = el('main', { id: 'content', tabindex: '-1' });
const rail = el('aside', { class: 'right-rail', 'aria-label': 'Темы и авторы' });
const nav = el('nav', { class: 'primary-nav', 'aria-label': 'Основная навигация' });
const profileLink = routeLink('me', [], 'mini-profile');
const authSlot = el('div', { class: 'auth-slot' });
const searchInput = el('input', {
  type: 'search',
  name: 'q',
  placeholder: 'Люди, записи, #темы',
  'aria-label': 'Поиск в Line',
  autocomplete: 'off',
});
const searchForm = el(
  'form',
  {
    class: 'global-search',
    role: 'search',
    onsubmit: (event) => {
      event.preventDefault();
      location.hash = `#/search?q=${encodeURIComponent(searchInput.value.trim())}`;
    },
  },
  [
    icon('search'),
    searchInput,
    el('button', { type: 'submit', class: 'search-submit', 'aria-label': 'Найти' }, icon('arrow')),
  ],
);
function interfaceLanguageSelect() {
  return el('div', { class: 'interface-language' }, [
    el('button', { type: 'button', 'aria-label': 'Язык интерфейса',
      onclick: () => setInterfaceLanguage(getInterfaceLanguage() === 'ru' ? 'en' : 'ru') }, getInterfaceLanguage() === 'ru' ? 'EN' : 'RU'),
    el('button', { type: 'button', 'aria-label': 'Switch theme / Сменить тему', onclick: toggleTheme }, '◐'),
  ]);
}
replace(
  root,
  el('a', {
    class: 'skip-link',
    href: '#content',
    text: 'Перейти к содержимому',
    onclick: (event) => {
      event.preventDefault();
      main.focus();
    },
  }),
  el('header', { class: 'topbar' }, [
    el(
      'a',
      { href: '#/feed', class: 'brand', 'aria-label': 'Line' },
      [
        el('span', { class: 'brand-mark', 'aria-hidden': 'true' }, icon('brand-route')),
        el('span', { class: 'brand-name', text: 'Line' }),
      ],
    ),
    searchForm,
    el('div', { class: 'topbar-actions' }, [
      interfaceLanguageSelect(),
      iconButton('Настроить ленту', 'settings', showPreferences),
      authSlot,
    ]),
  ]),
  el('div', { class: 'shell' }, [
    el('aside', { class: 'left-nav' }, [
      nav,
      button('Написать', () => showComposer(), {
        className: 'button primary compose-button',
        symbol: 'edit',
      }),
      el('div', { class: 'sidebar-caption', text: 'ВАШЕ ПРОСТРАНСТВО' }),
      routeLink('feed?tab=following', [icon('users'), 'Выбранные авторы'], 'sidebar-link'),
      routeLink('me?tab=drafts', [icon('edit'), 'Черновики'], 'sidebar-link'),
      profileLink,
      el('footer', { class: 'legal' }, [
        button('О Line', showAbout, { className: 'text-button' }),
        el('span', { text: `© ${new Date().getFullYear()} Line` }),
      ]),
    ]),
    main,
    rail,
  ]),
);
function renderNavigation() {
  const current = route();
  nav.replaceChildren(
    ...[
      ['feed', 'compass', 'Обзор'],
      ['topics', 'globe', 'Темы'],
      ['authors', 'users', 'Авторы'],
      ['me', 'user', 'Профиль'],
    ].map(([path, symbol, name]) => {
      const active = current.page === path || (path === 'topics' && current.page === 'tag');
      return el(
        'a',
        {
          href: `#/${path}`,
          class: active ? 'nav-item active' : 'nav-item',
          'aria-current': active ? 'page' : null,
        },
        [icon(symbol), el('span', { text: name })],
      );
    }),
  );
  replace(
    profileLink,
    avatar({ name: reader.name, avatar: reader.avatar }),
    el('div', {}, [
      el('strong', { text: reader.name || 'Гость Line' }),
      el('small', { text: signedIn() ? `@${reader.username}` : 'Войдите, чтобы действовать' }),
    ]),
  );
  replace(
    authSlot,
    signedIn()
      ? [
          el('a', { href: '#/me', class: 'topbar-avatar', 'aria-label': 'Профиль' },
            avatar({ name: reader.name, avatar: reader.avatar })),
          button('Выйти', signOut, { className: 'text-button auth-exit' }),
        ]
      : button('Войти', () => showAuthDialog('Войдите, чтобы пользоваться возможностями Line'), {
          className: 'button primary auth-enter',
        }),
  );
}
function renderRail() {
  const people = [...accounts.values()]
    .filter((a) => a.avatar && !a.bot && !reader.follows[a.id])
    .slice(0, 4);
  const tags = trends.length
    ? trends.slice(0, 5)
    : TOPICS.slice(0, 4).map((t) => ({ name: t.tag, caption: t.name }));
  replace(
    rail,
    el('section', { class: 'rail-intro' }, [
      el('span', { class: 'eyebrow', text: 'БЛИЖЕ ДРУГ К ДРУГУ' }),
      el('h2', {}, ['Маленькие истории.', el('br'), 'Большой мир.']),
      el('p', { text: 'Люди, идеи и моменты, которыми хочется поделиться.' }),
      el('div', { class: 'line-art', 'aria-hidden': 'true' }, [el('span'), el('span'), el('span')]),
    ]),
    el('section', { class: 'rail-section' }, [
      el('div', { class: 'section-heading' }, [
        el('h2', { text: trends.length ? 'Сейчас обсуждают' : 'Найдите свою тему' }),
        routeLink('topics', 'Все'),
      ]),
      ...tags.map((tag, index) =>
        routeLink(
          `tag/${encodeURIComponent(tag.name)}`,
          [
            el('span', { class: 'trend-index', text: String(index + 1).padStart(2, '0') }),
            el('div', {}, [
              el('strong', { text: `#${tag.name}` }),
              el('small', { text: tag.caption || 'Сейчас обсуждают' }),
            ]),
          ],
          'trend-row',
        ),
      ),
    ]),
    people.length
      ? el('section', { class: 'rail-section' }, [
          el('div', { class: 'section-heading' }, [
            el('h2', { text: 'Интересные люди' }),
            routeLink('authors', 'Все'),
          ]),
          ...people.map(personRow),
        ])
      : null,
  );
}
function heading(title, subtitle, action) {
  return el('header', { class: 'page-heading' }, [
    el('div', {}, [el('h1', { text: title }), subtitle && el('p', { text: subtitle })]),
    action,
  ]);
}
function tabs(items, active) {
  return el(
    'nav',
    { class: 'tabs', 'aria-label': 'Разделы страницы' },
    items.map(([path, value, label]) =>
      el('a', {
        href: `#/${path}`,
        class: value === active ? 'active' : '',
        'aria-current': value === active ? 'page' : null,
        text: label,
      }),
    ),
  );
}
function personRow(account) {
  return el('div', { class: 'person-row' }, [
    routeLink(
      `profile/${account.id}`,
      [
        avatar(account),
        el('div', { class: 'person-info' }, [
          el('strong', { text: account.name }),
          el('small', { text: `@${account.username}` }),
        ]),
      ],
      'person-link',
    ),
    followButton(account, true),
  ]);
}
function followButton(account, compact = false) {
  const selected = Boolean(reader.follows[account.id]);
  const control = button(
    selected ? 'В подборке' : 'Добавить автора',
    () => {
      if (!requireAuth('Войдите, чтобы добавлять авторов в свою подборку')) return;
      if (reader.follows[account.id]) delete reader.follows[account.id];
      else reader.follows[account.id] = account;
      persist();
      feeds.delete('following');
      toast(
        reader.follows[account.id] ? 'Автор добавлен в вашу подборку' : 'Автор удалён из подборки',
      );
      render();
    },
    {
      className: compact ? 'follow-button compact' : 'button secondary',
      symbol: selected ? 'check' : 'plus',
      'aria-pressed': String(selected),
      title: 'Добавить автора в подборку',
    },
  );
  return control;
}
async function pooled(items, action) {
  const results = new Array(items.length);
  let index = 0;
  await Promise.all(
    Array.from({ length: Math.min(3, items.length) }, async () => {
      while (index < items.length) {
        const next = index++;
        try {
          results[next] = { status: 'fulfilled', value: await action(items[next]) };
        } catch (reason) {
          results[next] = { status: 'rejected', reason };
        }
      }
    }),
  );
  return results;
}
function feedKey() {
  const current = route();
  return current.page === 'tag'
    ? `tag:${current.id}`
    : current.params.get('tab') === 'following'
      ? 'following'
      : 'overview';
}
function getFeed(key) {
  if (!feeds.has(key))
    feeds.set(key, {
      posts: key === 'overview' && lastFeed ? lastFeed.posts.filter((p) => p?.account && p.id) : [],
      cursors: {},
      done: new Set(),
      loading: false,
      loaded: false,
      stale: Boolean(key === 'overview' && lastFeed),
      error: '',
      pending: [],
      updated: 0,
    });
  return feeds.get(key);
}
async function loadFeed(key, { more = false, stage = false } = {}) {
  const feed = getFeed(key);
  if (feed.loading) return;
  const sources =
    key === 'following'
      ? Object.keys(reader.follows)
      : key.startsWith('tag:')
        ? [key.slice(4)]
        : reader.topics;
  feed.loading = true;
  feed.error = '';
  if (!stage && ['feed', 'tag'].includes(route().page) && feedKey() === key) renderFeed(key);
  const active = sources.filter((source) => !more || !feed.done.has(source));
  const results = await pooled(active, (source) =>
    key === 'following'
      ? accountPosts(source, { maxId: more ? feed.cursors[source] : '', originalsOnly: true, force: !more })
      : timeline(source, { maxId: more ? feed.cursors[source] : '', force: !more }),
  );
  // A subscription or local profile may have changed while requests were pending.
  if (feeds.get(key) !== feed) return;
  const fetched = [];
  let failures = 0;
  let stale = false;
  results.forEach((result, index) => {
    if (result.status === 'fulfilled') {
      fetched.push(...result.value.posts);
      stale ||= result.value.stale;
      if (!stage) {
        if (result.value.partial) {
          feed.done.delete(active[index]);
          return;
        }
        if (!result.value.next || (more && result.value.next === feed.cursors[active[index]]))
          feed.done.add(active[index]);
        else feed.done.delete(active[index]);
        feed.cursors[active[index]] = result.value.next;
      }
    } else failures++;
  });
  remember(fetched);
  if (stage && feed.posts.length) {
    const known = new Set(feed.posts.map((p) => p.uri));
    feed.pending = uniquePosts(fetched.filter((p) => !known.has(p.uri)));
  } else if (fetched.length || !failures) {
    const additions = arrangePosts(fetched);
    feed.posts = more ? uniquePosts([...feed.posts, ...additions]) : additions;
    if (key === 'overview') writeLastFeed(feed.posts);
  }
  feed.stale = stale || failures > 0;
  feed.error = failures
    ? failures === active.length
      ? 'Не удалось обновить ленту. Проверьте подключение или попробуйте позже.'
      : 'Часть источников временно недоступна. Показаны доступные записи.'
    : '';
  feed.loading = false;
  feed.loaded = true;
  feed.updated = Date.now();
  if (['feed', 'tag'].includes(route().page) && feedKey() === key) {
    if (stage) updateNewPosts(feed);
    else renderFeed(key);
  }
  renderRail();
}
function updateNewPosts(feed) {
  const region = main.querySelector('#feed-updates');
  if (!region) return;
  replace(
    region,
    feed.pending.length
      ? button(
          `Новые записи · ${feed.pending.length}`,
          () => {
            feed.posts = uniquePosts([...arrangePosts(feed.pending), ...feed.posts]);
            feed.pending = [];
            if (feedKey() === 'overview') writeLastFeed(feed.posts);
            renderFeed(feedKey());
            window.scrollTo({ top: 0, behavior: 'smooth' });
          },
          { className: 'new-posts', symbol: 'refresh' },
        )
      : null,
  );
}
function renderFeed(key) {
  const feed = getFeed(key);
  const tagged = key.startsWith('tag:');
  const following = key === 'following';
  if (following && !signedIn()) {
    replace(main, heading('Ваши авторы', 'Новые записи людей, которых вы выбрали'), guestPrompt('authors', () => showAuthDialog()));
    return;
  }
  const items = feed.posts.filter(
    (post) => (key !== 'overview' || !post.account.bot) && isRussian(post) &&
      (!following || (!post.boostedBy && Object.hasOwn(reader.follows, post.account.id))),
  );
  const moreAvailable = (
    following ? Object.keys(reader.follows) : tagged ? [key.slice(4)] : reader.topics
  ).some((source) => !feed.done.has(source));
  replace(
    main,
    heading(
      tagged ? `#${key.slice(4)}` : following ? 'Ваши авторы' : 'Обзор',
      tagged
        ? 'Истории и разговоры на одну тему'
        : following
          ? 'Новые записи людей, которых вы выбрали'
          : 'То, чем хочется поделиться',
      iconButton(
        'Проверить новые записи',
        'refresh',
        async () => {
          await loadFeed(key, { stage: true });
          if (!feed.pending.length) toast(feed.error || 'Вы читаете последние доступные записи');
        },
        { disabled: feed.loading },
      ),
    ),
    !tagged && !following
      ? el('section', { class: 'welcome-banner' }, [
          el('div', {}, [
            el('span', { class: 'eyebrow', text: 'НА СВОЕЙ ВОЛНЕ' }),
            el('h2', { text: 'Здесь есть о чём поговорить.' }),
            el('p', { text: 'Откройте новые имена. Найдите близкие темы.' }),
          ]),
          button('Мои интересы', showPreferences, {
            className: 'button banner-button',
            symbol: 'settings',
          }),
        ])
      : null,
    !tagged
      ? tabs(
          [
            ['feed', 'overview', 'Для вас'],
            ['feed?tab=following', 'following', 'Ваши авторы'],
          ],
          following ? 'following' : 'overview',
        )
      : null,
    el('div', { class: 'feed-toolbar' }, [
      el(
        'div',
        { class: 'topic-chips' },
        (tagged || following ? [] : TOPICS.filter((t) => reader.topics.includes(t.tag)).slice(0, 4)).map((t) =>
          routeLink(`tag/${t.tag}`, `#${t.tag}`, 'chip'),
        ),
      ),
    ]),
    el('div', { id: 'feed-updates', 'aria-live': 'polite' }),
    feed.error || feed.stale
      ? el('div', { class: 'notice', role: 'status' }, [
          icon('globe'),
          el('span', {
            text: feed.error || 'Показана сохранённая лента. Обновляем, когда сеть доступна.',
          }),
          !feed.loading && button('Повторить', () => loadFeed(key), { className: 'text-button' }),
        ])
      : null,
    items.length
      ? el(
          'section',
          { class: 'posts', 'aria-label': 'Публикации' },
          items.map((post) => renderPostCard(post)),
        )
      : feed.loading
        ? skeleton()
        : empty(
            following ? 'Соберите свой круг авторов' : 'Пока нет записей',
            following
              ? 'Добавляйте интересных людей в подборку — их публикации появятся здесь.'
              : 'Выберите другие темы или попробуйте обновить ленту.',
            following
              ? routeLink('authors', 'Найти авторов', 'button primary')
              : button('Настроить ленту', showPreferences, { className: 'button secondary' }),
          ),
    moreAvailable && feed.loaded
      ? button(feed.loading ? 'Загружаем…' : 'Показать ещё', () => loadFeed(key, { more: true }), {
          className: 'button load-more',
          symbol: 'down',
          disabled: feed.loading,
        })
      : null,
    feed.loaded && !moreAvailable && items.length
      ? el('p', { class: 'end-note', text: 'Вы дошли до конца доступной ленты' })
      : null,
  );
  updateNewPosts(feed);
  if (!feed.loaded && !feed.loading) loadFeed(key);
}

function postHref(post) {
  return `post/${encodeURIComponent(post.id)}`;
}
function localPostObject(post) {
  const account = activeUser() || { id: 'local-guest', username: reader.username || 'local', displayName: reader.name || 'Участник' };
  return {
    id: `local:${post.id}`,
    uri: `local:${post.id}`,
    url: '',
    local: true,
    account: {
      id: `local:${account.id}`,
      username: account.username || reader.username || 'local',
      name: reader.name || account.displayName || 'Участник',
      avatar: reader.avatar,
      bot: false,
    },
    content: `<p>${escapeHtml(post.text).replace(/\n/g, '<br>')}</p>`,
    text: post.text,
    createdAt: post.createdAt,
    editedAt: post.editedAt || '',
    media: localMedia(post.media), card: null, poll: null, warning: '', sensitive: false,
    replies: 0,
    favourites: 0, boosts: 0,
  };
}
function saveButton(post) {
  return iconButton(
    reader.saved[post.id] ? 'Убрать из сохранённого' : 'Сохранить запись',
    'bookmark',
    (event) => {
      if (!requireAuth('Войдите, чтобы сохранять записи')) return;
      if (reader.saved[post.id]) delete reader.saved[post.id];
      else reader.saved[post.id] = post;
      persist();
      event.currentTarget.replaceWith(saveButton(post));
      toast(
        reader.saved[post.id]
          ? 'Запись сохранена'
          : 'Запись удалена из сохранённого',
      );
      if (route().page === 'saved' || route().page === 'me') render();
    },
    {
      className: `icon-button save-button${reader.saved[post.id] ? ' selected' : ''}`,
      'aria-pressed': String(Boolean(reader.saved[post.id])),
    },
  );
}
function reactionCount(post, type) {
  const base = type === 'like' ? post.favourites : post.boosts;
  const active = type === 'like'
    ? Boolean(reader.likes[post.id] || reader.favourites[post.id])
    : Boolean(reader.boosts[post.id]);
  return Number(base || 0) + (active ? 1 : 0);
}
function reactionButton(post, type) {
  const collection = type === 'like' ? reader.likes : reader.boosts;
  const active = type === 'like'
    ? Boolean(reader.likes[post.id] || reader.favourites[post.id])
    : Boolean(collection[post.id]);
  const label = type === 'like' ? 'Нравится' : 'Поделиться в Line';
  return button(number(reactionCount(post, type)), () => {
    if (!requireAuth(`Войдите, чтобы ${type === 'like' ? 'ставить отметки' : 'делиться записями'}`)) return;
    if (active) {
      delete collection[post.id];
      if (type === 'like') delete reader.favourites[post.id];
    } else {
      collection[post.id] = type === 'boost'
        ? { at: new Date().toISOString(), post }
        : { at: new Date().toISOString() };
      if (type === 'like') reader.favourites[post.id] = post;
    }
    persist();
    render();
    toast(!active ? `${label}: добавлено` : `${label}: отменено`);
  }, {
    symbol: type === 'like' ? 'heart' : 'repeat',
    className: `post-action${active ? ' selected' : ''}`,
    'aria-label': `${label}, ${number(reactionCount(post, type))}`,
    'aria-pressed': String(active),
  });
}
function localComments(postId) {
  return readState().comments
    .filter((comment) => comment.postId === postId && !comment.deleted)
    .sort((a, b) => Date.parse(a.createdAt) - Date.parse(b.createdAt));
}
function postReplyCount(post) {
  return Number(post.replies || 0) + localComments(post.id).length;
}
const commentReactionKey = (comment) => `comment:${comment.id}`;
function commentReactionButton(comment, type, author) {
  const key = commentReactionKey(comment);
  const collection = type === 'like' ? reader.likes : reader.boosts;
  const active = Boolean(collection[key]);
  const label = type === 'like' ? 'Нравится' : 'Поделиться в Line';
  return button(active ? '1' : '0', () => {
    if (!requireAuth('Войдите, чтобы взаимодействовать с комментариями')) return;
    if (active) delete collection[key];
    else collection[key] = type === 'boost'
      ? { at: new Date().toISOString(), comment: { ...comment, author } }
      : { at: new Date().toISOString() };
    persist();
    render();
    toast(active ? `${label}: отменено` : `${label}: добавлено`);
  }, {
    symbol: type === 'like' ? 'heart' : 'repeat',
    className: `post-action comment-action${active ? ' selected' : ''}`,
    'aria-label': `${label}, ${active ? 1 : 0}`,
    'aria-pressed': String(active),
  });
}
function commentSaveButton(comment, author) {
  const key = commentReactionKey(comment);
  const active = Boolean(reader.saved[key]);
  return iconButton(active ? 'Убрать комментарий из сохранённого' : 'Сохранить комментарий', 'bookmark', () => {
    if (!requireAuth('Войдите, чтобы сохранять комментарии')) return;
    if (active) delete reader.saved[key];
    else reader.saved[key] = { savedComment: { ...comment, author } };
    persist();
    render();
    toast(active ? 'Комментарий удалён из сохранённого' : 'Комментарий сохранён');
  }, {
    className: `icon-button save-button${active ? ' selected' : ''}`,
    'aria-pressed': String(active),
  });
}
async function copyCommentLink(post, comment) {
  const url = `${location.href.split('#')[0]}#/post/${encodeURIComponent(post.id)}?comment=${encodeURIComponent(comment.id)}`;
  try {
    await navigator.clipboard.writeText(url);
    toast('Ссылка на комментарий скопирована');
  } catch {
    toast('Не удалось скопировать ссылку');
  }
}
function deleteCommentThread(comment) {
  const state = readState();
  const ids = new Set([comment.id]);
  let changed = true;
  while (changed) {
    changed = false;
    for (const item of state.comments) {
      if (ids.has(item.parentId) && !ids.has(item.id)) {
        ids.add(item.id);
        changed = true;
      }
    }
  }
  state.comments = state.comments.filter((item) => !ids.has(item.id));
  for (const id of ids) {
    delete reader.likes[`comment:${id}`];
    delete reader.boosts[`comment:${id}`];
    delete reader.saved[`comment:${id}`];
  }
  if (!persist(state)) {
    reader = readReader();
    return;
  }
  render();
  toast(ids.size > 1 ? 'Комментарий и ответы удалены' : 'Комментарий удалён');
}
function renderLocalComment(comment, state, post, depth = 0) {
  const author = state.accounts.find((account) => account.id === comment.userId) || { displayName: 'Участник', username: 'local' };
  const profile = state.profiles?.[author.id]?.reader;
  const own = state.session?.userId === comment.userId;
  const parent = state.comments.find((item) => item.id === comment.parentId);
  const parentAuthor = parent
    ? state.accounts.find((account) => account.id === parent.userId)
    : posts.get(comment.parentId)?.account || (comment.parentUsername ? { username: comment.parentUsername } : null);
  const node = el('article', { class: 'local-comment card' }, [
    el('header', { class: 'post-header' }, [
      avatar({ name: author.displayName, avatar: profile?.avatar }),
      el('div', { class: 'post-author' }, [
        el('strong', { class: 'author-name', text: author.displayName }),
        el('div', { class: 'post-meta' }, [
          el('span', { class: 'handle', text: `@${author.username}` }),
          el('span', { text: '·' }),
          el('time', { datetime: comment.createdAt, text: relativeTime(comment.createdAt), title: formatDateTime(comment.createdAt) }),
        ]),
      ]),
      own && iconButton('Удалить комментарий', 'close', () =>
        confirmAction('Удалить комментарий?', 'Комментарий и ответы на него исчезнут из обсуждения.', 'Удалить', () => deleteCommentThread(comment)),
      ),
    ]),
    (comment.text || parentAuthor) && el('p', { class: 'local-comment-text' }, [
      parentAuthor && el('span', { class: 'comment-mention', text: `@${parentAuthor.username} ` }),
      comment.text,
    ]),
    localMedia(comment.media).length ? el('div', { class: 'local-comment-media' }, renderMedia({ media: localMedia(comment.media) })) : null,
    el('footer', { class: 'post-actions comment-actions' }, [
      button('Ответить', () => showCommentComposer(post, {
        id: comment.id,
        username: author.username,
        text: comment.text,
      }), { symbol: 'message', className: 'post-action', 'aria-label': `Ответить @${author.username}` }),
      commentReactionButton(comment, 'boost', author),
      commentReactionButton(comment, 'like', author),
      el('span', { class: 'action-spacer' }),
      iconButton('Копировать ссылку на комментарий', 'share', () => copyCommentLink(post, comment)),
      commentSaveButton(comment, author),
    ]),
  ]);
  return el('div', {
    class: `thread-reply${depth ? ' nested' : ''} depth-${Math.min(depth, 3)}`,
    'data-comment-id': comment.id,
  }, node);
}
function showCommentComposer(post, parent = null) {
  if (!requireAuth('Войдите, чтобы написать комментарий')) return;
  const modal = dialog(parent ? 'Ответить на комментарий' : 'Новый комментарий');
  modal.content.classList.add('editor-dialog', 'comment-editor');
  const input = el('textarea', {
    rows: '5', maxlength: '5000', class: 'compose-text',
    placeholder: 'Напишите бережный комментарий…', 'aria-label': 'Текст комментария',
  });
  const count = el('span', { class: 'muted', text: '0 / 5000' });
  input.addEventListener('input', () => {
    count.textContent = `${input.value.length} / 5000`;
  });
  let attachments = [];
  const attachmentList = el('div', { class: 'composer-attachments comment-attachments', 'aria-live': 'polite' });
  const attachmentInput = el('input', {
    type: 'file', accept: 'image/*,video/*', multiple: true, hidden: true,
    'aria-label': 'Добавить фото или видео к комментарию',
  });
  const mediaStatus = el('span', { class: 'muted', role: 'status' });
  const renderAttachments = () => replace(
    attachmentList,
    ...attachments.map((media, index) =>
      el('figure', { class: 'composer-attachment' }, [
        media.type === 'video'
          ? el('video', { src: media.url, muted: true, preload: 'metadata', playsinline: true })
          : picture(media.url, media.alt || 'Выбранное изображение'),
        el('figcaption', { text: media.alt || (media.type === 'video' ? 'Видео' : 'Фото') }),
        iconButton('Удалить вложение', 'close', () => {
          attachments = attachments.filter((_, item) => item !== index);
          renderAttachments();
        }),
      ]),
    ),
  );
  attachmentInput.addEventListener('change', async () => {
    if (attachmentInput.disabled) return;
    const files = [...attachmentInput.files];
    attachmentInput.value = '';
    if (!files.length) return;
    if (attachments.length + files.length > MAX_ATTACHMENTS) {
      toast(`Можно добавить не более ${MAX_ATTACHMENTS} вложений`);
      return;
    }
    mediaStatus.textContent = translate('Подготавливаем вложения…');
    attachmentInput.disabled = true;
    try {
      const added = [];
      for (const file of files) {
        if (file.type.startsWith('image/')) {
          const url = await imageDataUrl(file);
          added.push({ id: newId('media'), type: 'image', url, preview: url, alt: file.name });
        } else if (file.type.startsWith('video/')) {
          const url = await readFileAsDataUrl(file);
          added.push({ id: newId('media'), type: 'video', url, preview: '', alt: file.name });
        } else toast(`Файл «${file.name}» не является изображением или видео`);
      }
      if (!attachmentInput.isConnected) return;
      attachments = [...attachments, ...added];
      renderAttachments();
      mediaStatus.textContent = attachments.length ? translate(`${attachments.length} вложения добавлено`) : '';
    } catch (error) {
      mediaStatus.textContent = '';
      toast(error.message || 'Не удалось подготовить вложение');
    } finally {
      attachmentInput.disabled = false;
    }
  });
  modal.content.append(el('form', {
    class: 'comment-form editor-form',
    onsubmit: (event) => {
      event.preventDefault();
      if (attachmentInput.disabled) { toast('Дождитесь подготовки вложений'); return; }
      const text = input.value.trim();
      if (!text && !attachments.length) { input.focus(); return; }
      const state = readState();
      const account = currentUser(state);
      if (!account) { toast('Войдите в профиль для отправки комментария'); return; }
      state.comments.push({
        id: newId('comment'),
        postId: post.id,
        parentId: parent?.id || '',
        parentUsername: parent?.username || '',
        userId: account.id,
        text,
        media: attachments,
        createdAt: new Date().toISOString(),
      });
      if (!writeState(state)) {
        toast('Не удалось сохранить комментарий. Возможно, хранилище заполнено.');
        return;
      }
      modal.close();
      if (route().page === 'post') render();
      toast(parent ? 'Ответ опубликован' : 'Комментарий опубликован');
    },
  }, [
    parent && el('div', { class: 'comment-replying-to' }, [
      el('span', { text: 'Ответ для' }),
      el('strong', { text: `@${parent.username || 'user'}` }),
      parent.text && el('small', { text: parent.text.slice(0, 120) }),
    ]),
    input,
    attachmentInput,
    el('div', { class: 'composer-media-actions' }, [
      button('Добавить фото или видео', () => attachmentInput.click(), {
        className: 'button secondary', symbol: 'camera',
      }),
      el('small', { text: `До ${MAX_ATTACHMENTS} вложений` }),
    ]),
    attachmentList,
    el('div', { class: 'compose-status' }, [mediaStatus, count]),
    el('div', { class: 'form-actions' }, [
      el('button', { type: 'submit', class: 'button primary' }, [icon('message'), el('span', { text: 'Опубликовать' })]),
    ]),
  ]));
  renderAttachments();
  input.focus();
}
function showPoll(post) {
  if (!requireAuth('Войдите, чтобы голосовать')) return;
  const modal = dialog('Ваш голос');
  const current = Number.isInteger(reader.votes[post.id]) ? reader.votes[post.id] : -1;
  let selected = current;
  const options = el('div', { class: 'poll-vote-options' }, post.poll.options.map((option, index) => {
    const input = el('input', {
      type: 'radio', name: 'poll-option', value: String(index), checked: index === current,
      onchange: () => { selected = index; },
    });
    return el('label', { class: 'poll-vote-option' }, [input, el('span', { text: option.title })]);
  }));
  modal.content.append(el('form', {
    class: 'poll-vote-form',
    onsubmit: (event) => {
      event.preventDefault();
      if (selected < 0) { toast('Выберите один вариант'); return; }
      reader.votes[post.id] = selected;
      persist();
      modal.close();
      render();
      toast('Голос учтён');
    },
  }, [
    options,
    el('div', { class: 'form-actions' }, [
      el('button', { type: 'submit', class: 'button primary', text: current < 0 ? 'Проголосовать' : 'Изменить голос' }),
    ]),
  ]));
}
function renderPostCard(post, { full = false, pinned = false } = {}) {
  const account = post.account;
  const body = el('div', { class: 'post-content' });
  if (post.content) {
    const text = richText(post.content);
    if (!full && post.text.length > 650) {
      text.classList.add('text-collapsed');
      const expand = button(
        'Читать дальше',
        () => {
          text.classList.remove('text-collapsed');
          expand.remove();
        },
        { className: 'text-button read-more' },
      );
      body.append(text, expand);
    } else body.append(text);
  }
  if (post.media.length) body.append(renderMedia(post));
  else if (post.card)
    body.append(
      el(
        'div',
        { class: 'link-preview' },
        externalLink(
          [
            post.card.image && picture(post.card.image, '', 'link-image'),
            el('div', {}, [
              el('small', { text: post.card.provider || new URL(post.card.url).hostname }),
              el('strong', { text: post.card.title || post.card.url }),
              el('p', { text: post.card.description }),
            ]),
          ],
          post.card.url,
        ),
      ),
    );
  if (post.poll)
    body.append(
      el('div', { class: 'poll' }, [
        el('span', { class: 'eyebrow', text: 'ОПРОС' }),
        ...post.poll.options.map((option, index) =>
          el('div', { class: `poll-option${reader.votes[post.id] === index ? ' selected' : ''}` }, [
            el('span', { text: option.title }),
            el('strong', { text: option.votes == null ? '—' : number(option.votes + (reader.votes[post.id] === index ? 1 : 0)) }),
          ]),
        ),
        el('small', {
          text: `${number(post.poll.votes + (Number.isInteger(reader.votes[post.id]) ? 1 : 0))} голосов · ${post.poll.expired ? 'Завершён' : 'Открыто для голосования'}`,
        }),
        !post.poll.expired &&
          button(Number.isInteger(reader.votes[post.id]) ? 'Изменить голос' : 'Проголосовать', () => showPoll(post), {
            className: 'text-button',
          }),
      ]),
    );
  const content = post.warning
    ? el('details', { class: 'content-warning' }, [
        el('summary', {}, [
          el('strong', { text: post.warning }),
          el('span', { text: 'Показать содержимое' }),
        ]),
        body,
      ])
    : body;
  const card = el(
    'article',
    { class: `post card${full ? ' full-post' : ''}`, 'data-post-id': post.id },
    [
      pinned || post.boostedBy
        ? el('div', { class: 'post-context' }, [
            icon(pinned ? 'bookmark' : 'repeat'),
            pinned ? 'Закреплённая запись' : `${post.boostedBy.name} поделился записью`,
          ])
        : null,
      el('header', { class: 'post-header' }, [
        routeLink(post.local ? 'me' : `profile/${account.id}`, avatar(account), 'avatar-link'),
        el('div', { class: 'post-author' }, [
          routeLink(post.local ? 'me' : `profile/${account.id}`, account.name, 'author-name'),
          el('div', { class: 'post-meta' }, [
            el('span', { class: 'handle', text: `@${account.username}` }),
            el('span', { text: '·' }),
            routeLink(
              postHref(post),
              el('time', {
                datetime: post.createdAt,
                'data-relative-time': post.createdAt,
                title: formatDateTime(post.createdAt),
                text: relativeTime(post.createdAt),
              }),
            ),
            account.bot && el('span', { class: 'bot-label', text: 'бот' }),
          ]),
        ]),
        iconButton('Действия с записью', 'more', () => showPostMenu(post)),
      ]),
      content,
      full
        ? el('div', {
            class: 'full-post-date',
            text: `${formatDateTime(post.createdAt)}${post.editedAt ? ' · Изменено ' + formatDateTime(post.editedAt) : ''}`,
          })
        : null,
      el('footer', { class: 'post-actions' }, [
        routeLink(
          postHref(post),
          [icon('message'), el('span', { text: number(postReplyCount(post)) })],
          'post-action',
        ),
        reactionButton(post, 'boost'),
        reactionButton(post, 'like'),
        el('span', { class: 'action-spacer' }),
        iconButton('Копировать ссылку', 'share', () => copyLink(post)),
        saveButton(post),
      ]),
    ],
  );
  card
    .querySelector('.post-action')
    ?.setAttribute('aria-label', `Открыть обсуждение, ${number(postReplyCount(post))} ответов`);
  return card;
}
function renderMedia(post) {
  const images = post.media.filter((m) => m.type === 'image');
  const gallery = el('div', { class: `media-grid media-count-${Math.min(post.media.length, 4)}` });
  for (const media of post.media) {
    if (media.type === 'image')
      gallery.append(
        el(
          'button',
          {
            type: 'button',
            class: 'media-button',
            'aria-label': media.alt || 'Открыть фотографию',
            onclick: () => showGallery(images, images.indexOf(media)),
          },
          picture(media.preview || media.url, media.alt, 'post-image'),
        ),
      );
    else {
      const player = el(media.type === 'audio' ? 'audio' : 'video', {
        src: media.url,
        poster: media.type === 'audio' ? null : media.preview,
        controls: true,
        preload: 'none',
        playsinline: true,
        'aria-label': media.alt || 'Медиафайл',
        class: 'post-player',
      });
      player.addEventListener(
        'error',
        () =>
          player.replaceWith(el('span', { class: 'media-error', text: 'Медиа недоступно' })),
        { once: true },
      );
      gallery.append(
        el('figure', {}, [player, media.alt && el('figcaption', { text: media.alt })]),
      );
    }
  }
  return post.sensitive
    ? el('details', { class: 'sensitive-media' }, [
        el('summary', { text: 'Чувствительный контент · Показать медиа' }),
        gallery,
      ])
    : gallery;
}
function showGallery(images, start) {
  let index = start;
  const modal = dialog('Фотография', { wide: true });
  function display() {
    const media = images[index];
    replace(
      modal.content,
      picture(media.url || media.preview, media.alt, 'lightbox-image'),
      el('div', { class: 'gallery-caption' }, [
        el('p', { text: media.alt || 'Фотография автора' }),
        el('span', { text: `${index + 1} / ${images.length}` }),
      ]),
      el('div', { class: 'gallery-controls' }, [
        button(
          'Назад',
          () => {
            index = (index - 1 + images.length) % images.length;
            display();
          },
          { disabled: images.length < 2 },
        ),
        button(
          'Вперёд',
          () => {
            index = (index + 1) % images.length;
            display();
          },
          { disabled: images.length < 2 },
        ),
      ]),
    );
  }
  modal.modal.addEventListener('keydown', (event) => {
    if (['ArrowLeft', 'ArrowRight'].includes(event.key)) {
      event.preventDefault();
      index = (index + (event.key === 'ArrowRight' ? 1 : -1) + images.length) % images.length;
      display();
    }
  });
  display();
}
async function copyLink(post) {
  const url = post.local
    ? `${location.origin}${location.pathname}#/${postHref(post)}`
    : post.url || `https://${instanceForId(post.id)}/@${post.account.username}/${post.id.replace(/^ml:/, '')}`;
  try {
    await navigator.clipboard.writeText(url);
    toast('Ссылка скопирована');
  } catch {
    const modal = dialog('Ссылка на запись');
    const input = el('input', {
      value: url,
      readonly: true,
      class: 'copy-input',
      'aria-label': 'Ссылка на запись',
    });
    modal.content.append(input);
    input.select();
  }
}
function removeLocalPost(post) {
  const id = post.id.replace(/^local:/, '');
  reader.localPosts = reader.localPosts.filter((item) => item.id !== id);
  for (const collection of [reader.likes, reader.boosts, reader.saved, reader.favourites])
    delete collection[post.id];
  const state = readState();
  const commentIds = state.comments.filter((comment) => comment.postId === post.id).map((comment) => comment.id);
  for (const commentId of commentIds)
    for (const collection of [reader.likes, reader.boosts, reader.saved])
      delete collection[`comment:${commentId}`];
  state.comments = state.comments.filter((comment) => comment.postId !== post.id);
  if (!persist(state)) {
    reader = readReader();
    return;
  }
  posts.delete(post.id);
  contexts.delete(post.id);
  if (route().page === 'post' && route().id === post.id) location.hash = '#/me';
  else render();
  toast('История удалена');
}
function showStoryEditor(post) {
  const source = reader.localPosts.find((item) => `local:${item.id}` === post.id);
  if (!source) return;
  const modal = dialog('Редактировать историю');
  const input = el('textarea', {
    rows: '8', maxlength: '5000', class: 'compose-text', text: source.text,
    'aria-label': 'Текст истории',
  });
  modal.content.append(el('form', {
    class: 'story-edit-form',
    onsubmit: (event) => {
      event.preventDefault();
      const text = input.value.trim();
      if (!text && !localMedia(source.media).length) { input.focus(); return; }
      const previous = { text: source.text, editedAt: source.editedAt };
      source.text = text;
      source.editedAt = new Date().toISOString();
      if (!persist()) {
        Object.assign(source, previous);
        return;
      }
      remember([localPostObject(source)]);
      modal.close();
      render();
      toast('История обновлена');
    },
  }, [
    input,
    el('div', { class: 'form-actions' }, [
      el('button', { type: 'submit', class: 'button primary', text: 'Сохранить изменения' }),
    ]),
  ]));
  input.focus();
}
function showPostMenu(post) {
  const modal = dialog('Действия с записью');
  const ownStory = post.local && reader.localPosts.some((item) => `local:${item.id}` === post.id);
  modal.content.append(
    el('div', { class: 'menu-actions' }, [
      button(
        reader.favourites[post.id] ? 'Убрать из избранного' : 'Добавить в избранное',
        () => {
          if (!requireAuth('Войдите, чтобы добавлять записи в избранное')) return;
          if (reader.favourites[post.id]) {
            delete reader.favourites[post.id];
            delete reader.likes[post.id];
          } else {
            reader.favourites[post.id] = post;
            reader.likes[post.id] = { at: new Date().toISOString() };
          }
          persist();
          modal.close();
          toast('Избранное обновлено');
          render();
        },
        { symbol: 'heart' },
      ),
      button(
        'Копировать ссылку',
        () => {
          modal.close();
          copyLink(post);
        },
        { symbol: 'share' },
      ),
      button('Написать комментарий', () => { modal.close(); showCommentComposer(post); }, { symbol: 'message' }),
      ownStory && button('Редактировать историю', () => { modal.close(); showStoryEditor(post); }, { symbol: 'edit' }),
      ownStory && button('Удалить историю', () => {
        modal.close();
        confirmAction('Удалить историю?', 'История и её комментарии будут удалены.', 'Удалить', () => removeLocalPost(post));
      }, { symbol: 'close', className: 'button danger' }),
    ]),
    el('p', {
      class: 'muted',
      text: 'Реакции и избранное сохраняются в Line и не меняют исходную публикацию.',
    }),
  );
}

async function renderProfile(id, version) {
  const mediaOnly = route().params.get('tab') === 'media';
  const key = `${id}:${mediaOnly}`;
  if (!profilePages.has(key)) {
    const results = await Promise.allSettled([
      request(`/api/v1/accounts/${encodeURIComponent(id)}`),
      accountPosts(id, { media: mediaOnly }),
      accountPosts(id, { pinned: true }),
    ]);
    if (results[0].status === 'rejected') throw results[0].reason;
    const account = normalizeAccount(results[0].value.data, results[0].value.instance);
    accounts.set(id, account);
    const listing = results[1].status === 'fulfilled' ? results[1].value : { posts: [], next: '' };
    const pinned = !mediaOnly && results[2].status === 'fulfilled' ? results[2].value.posts : [];
    remember([...listing.posts, ...pinned]);
    profilePages.set(key, {
      account,
      ...listing,
      pinned,
      error: results[1].status === 'rejected' ? results[1].reason.message : '',
      stale: results[0].value.stale || listing.stale,
      loading: false,
    });
  }
  if (version !== renderVersion) return;
  const data = profilePages.get(key);
  const account = data.account;
  const profile = el('section', { class: 'profile-card card' }, [
    el(
      'div',
      { class: 'profile-cover' },
      account.header && picture(account.header, '', 'cover-image'),
    ),
    el('div', { class: 'profile-details' }, [
      el('div', { class: 'profile-top' }, [avatar(account, true), followButton(account)]),
      el('h1', { text: account.name }),
      el('p', { class: 'muted profile-handle', text: `@${account.username}` }),
      richText(account.bio),
      el(
        'div',
        { class: 'profile-stats' },
        [
          [account.statuses, 'публикаций'],
          [account.followers, 'подписчиков'],
          [account.following, 'подписок'],
        ].map(([count, label]) =>
          el('span', {}, [el('strong', { text: number(count) }), ` ${label}`]),
        ),
      ),
      account.joined &&
        el('p', { class: 'muted', text: `Дата регистрации: ${formatDate(account.joined)}` }),
      account.fields.length
        ? el(
            'dl',
            { class: 'profile-fields' },
            account.fields.map((field) => [
              el('dt', { text: field.name }),
              el('dd', {}, richText(field.value)),
            ]),
          )
        : null,
    ]),
  ]);
  replace(
    main,
    routeLink('authors', [icon('arrow'), 'Авторы'], 'back-link'),
    profile,
    tabs(
      [
        [`profile/${id}`, 'posts', 'Публикации'],
        [`profile/${id}?tab=media`, 'media', 'Медиа'],
      ],
      mediaOnly ? 'media' : 'posts',
    ),
    data.stale && el('div', { class: 'notice', text: 'Показаны сохранённые данные профиля.' }),
    data.error &&
      el('div', { class: 'notice' }, [
        data.error,
        button(
          'Повторить',
          () => {
            profilePages.delete(key);
            render();
          },
          { className: 'text-button' },
        ),
      ]),
    el('section', { class: 'posts' }, [
      ...data.pinned.map((p) => renderPostCard(p, { pinned: true })),
      ...data.posts
        .filter((p) => !data.pinned.some((pin) => pin.id === p.id))
        .map((p) => renderPostCard(p)),
    ]),
    !data.posts.length && !data.pinned.length && !data.error
      ? empty(
          'Здесь пока тихо',
          mediaOnly ? 'У автора нет доступных медиа.' : 'Публичных записей пока нет.',
        )
      : null,
    data.next
      ? button(
          data.loading ? 'Загружаем…' : 'Ещё публикации',
          async () => {
            data.loading = true;
            render();
            try {
              const page = await accountPosts(id, { maxId: data.next, media: mediaOnly });
              remember(page.posts);
              data.posts = uniquePosts([...data.posts, ...page.posts]);
              data.next = page.next === data.next ? '' : page.next;
              data.stale = page.stale;
            } catch (error) {
              toast(error.message);
            } finally {
              data.loading = false;
              if (route().page === 'profile' && route().id === id) render();
            }
          },
          { className: 'button load-more', disabled: data.loading },
        )
      : null,
  );
  renderRail();
}
async function renderDiscussion(id, version) {
  let post = posts.get(id);
  if (!contexts.has(id)) {
    if (post?.local) {
      contexts.set(id, { ancestors: [], replies: [], error: '', stale: false });
    } else {
      const results = await Promise.allSettled([
        request(`/api/v1/statuses/${encodeURIComponent(id)}`),
        request(`/api/v1/statuses/${encodeURIComponent(id)}/context`),
      ]);
    if (results[0].status === 'fulfilled') {
      post = normalizeMastodonStatus(results[0].value.data, results[0].value.instance);
      if (post) remember([post]);
    }
    if (!post)
      throw results[0].status === 'rejected'
        ? results[0].reason
        : new Error('Запись больше недоступна');
    const context = results[1].status === 'fulfilled' ? results[1].value.data : {};
    const ancestors = (context.ancestors || [])
      .map((raw) => normalizeMastodonStatus(raw, instanceForId(id)))
      .filter(Boolean);
    const replies = (context.descendants || [])
      .map((raw) => normalizeMastodonStatus(raw, instanceForId(id)))
      .filter(Boolean);
    remember([...ancestors, ...replies]);
      contexts.set(id, {
        ancestors,
        replies,
        error: results[1].status === 'rejected' ? results[1].reason.message : '',
        stale:
          results[0].status === 'rejected' ||
          results[0].value.stale ||
          (results[1].status === 'fulfilled' && results[1].value.stale),
      });
    }
  }
  if (version !== renderVersion) return;
  const context = contexts.get(id);
  const state = readState();
  const threadIds = new Set([id, ...context.replies.map((reply) => reply.id)]);
  const comments = state.comments.filter((comment) => threadIds.has(comment.postId) && !comment.deleted)
    .sort((a, b) => Date.parse(a.createdAt) - Date.parse(b.createdAt));
  // Remote and local replies share one parent map and one traversal.
  const entries = new Map();
  for (const reply of context.replies) entries.set(reply.id, { remote: reply, parent: reply.replyTo });
  for (const comment of comments) entries.set(comment.id, { comment, parent: comment.parentId || comment.postId });
  const children = new Map();
  for (const [key, entry] of entries) {
    const parent = entries.has(entry.parent) && entry.parent !== key ? entry.parent : id;
    if (!children.has(parent)) children.set(parent, []);
    children.get(parent).push(key);
  }
  const replyNodes = [];
  const visited = new Set();
  const appendEntry = (key, depth) => {
    if (visited.has(key)) return;
    visited.add(key);
    const entry = entries.get(key);
    let node;
    if (entry.comment) node = renderLocalComment(entry.comment, state, post, depth);
    else {
      const reply = entry.remote;
      const card = renderPostCard(reply);
      card.querySelector('.post-actions > .post-action').replaceWith(
        button('Ответить', () => showCommentComposer(post, {
          id: reply.id, username: reply.account.username, text: reply.text,
        }), { className: 'post-action comment-reply-button', symbol: 'message', 'aria-label': `Ответить @${reply.account.username}` }),
      );
      node = el('div', { class: `thread-reply${depth ? ' nested' : ''} depth-${Math.min(depth, 3)}` }, card);
    }
    replyNodes.push(node);
    for (const child of children.get(key) || []) appendEntry(child, depth + 1);
  };
  for (const key of children.get(id) || []) appendEntry(key, 0);
  for (const key of entries.keys()) if (!visited.has(key)) appendEntry(key, 0);
  replace(
    main,
    heading('Обсуждение', '', routeLink('feed', [icon('arrow'), 'В ленту'], 'text-button')),
    context.stale &&
      el('div', {
        class: 'notice',
        text: 'Показана сохранённая запись. Оригинал может быть изменён или недоступен.',
      }),
    context.ancestors.length
      ? el(
          'section',
          { class: 'thread-ancestors', 'aria-label': 'Начало обсуждения' },
          context.ancestors.map((p) => renderPostCard(p)),
        )
      : null,
    renderPostCard(post, { full: true }),
    el('section', { class: 'reply-prompt card' }, [
      el('div', {}, [
        el('strong', { text: 'Присоединиться к разговору' }),
        el('p', { text: 'Оставьте комментарий и присоединитесь к разговору.' }),
      ]),
      button('Написать комментарий', () => showCommentComposer(post), {
        className: 'button primary',
        symbol: 'message',
      }),
    ]),
    el('div', { class: 'section-heading discussion-heading' }, [
      el('h2', { text: 'Ответы и комментарии' }),
      el('span', { class: 'muted', text: `${context.replies.length + comments.length} доступно` }),
    ]),
    context.error
      ? el('div', { class: 'notice' }, [
          context.error,
          button(
            'Повторить',
            () => {
              contexts.delete(id);
              render();
            },
            { className: 'text-button' },
          ),
        ])
      : null,
    replyNodes.length
      ? el('section', { class: 'thread', 'aria-label': 'Ответы' }, replyNodes)
      : !context.error
        ? empty(
            'Разговор ещё впереди',
            post.replies
              ? 'В этой ветке пока нет доступных ответов.'
              : 'Станьте первым, кто ответит автору.',
          )
        : null,
  );
  const selectedComment = route().params.get('comment');
  if (selectedComment)
    requestAnimationFrame(() => {
      const node = main.querySelector(`[data-comment-id="${CSS.escape(selectedComment)}"]`);
      node?.scrollIntoView({ block: 'center' });
      node?.classList.add('selected-comment');
    });
}
function renderTopics() {
  replace(
    main,
    heading('Найдите свою тему', 'Интересы, которые объединяют'),
    el(
      'section',
      { class: 'topic-grid' },
      TOPICS.map((topic, index) =>
        routeLink(
          `tag/${topic.tag}`,
          [
            el('span', { class: `topic-art topic-art-${index}` }, icon(topic.icon)),
            el('div', {}, [
              el('h2', { text: topic.name }),
              el('p', { text: topic.caption }),
              el('span', { class: 'topic-tag', text: `#${topic.tag}` }),
            ]),
            icon('down'),
          ],
          'topic-card card',
        ),
      ),
    ),
    trends.length
      ? el('section', { class: 'card trending-card' }, [
          el('h2', { text: 'Сейчас обсуждают' }),
          el(
            'div',
            { class: 'topic-chips wrap' },
            trends.map((t) => routeLink(`tag/${encodeURIComponent(t.name)}`, `#${t.name}`, 'chip')),
          ),
        ])
      : null,
    button('Настроить интересы для обзора', showPreferences, {
      className: 'button load-more',
      symbol: 'settings',
    }),
  );
}
async function renderAuthors(version) {
  const selected = route().params.get('tab') === 'selected';
  if (!selected && accounts.size < 8) {
    try {
      const result = await request('/api/v1/directory?local=true&order=active&limit=20');
      if (Array.isArray(result.data))
        for (const raw of result.data) {
          const account = normalizeAccount(raw);
          accounts.set(account.id, account);
        }
    } catch {
      if (!accounts.size) {
        const page = await timeline('photography');
        remember(page.posts);
      }
    }
  }
  if (version !== renderVersion) return;
  const people = selected
    ? Object.values(reader.follows)
    : [...accounts.values()].filter((a) => !a.bot);
  replace(
    main,
    heading('Люди, которых интересно читать', 'Соберите свою подборку авторов'),
    tabs(
      [
        ['authors', 'all', 'Открыть новые имена'],
        ['authors?tab=selected', 'selected', 'Мои авторы'],
      ],
      selected ? 'selected' : 'all',
    ),
    selected && !signedIn() ? guestPrompt('authors', () => showAuthDialog()) : people.length
      ? el(
          'section',
          { class: 'authors-grid' },
          people.map((account) =>
            el('article', { class: 'author-card card' }, [
              routeLink(`profile/${account.id}`, [
                avatar(account, true),
                el('h2', { text: account.name }),
                el('small', { text: `@${account.username}` }),
              ]),
              el('p', {
                text: plainText(account.bio).slice(0, 160) || 'Откройте профиль, чтобы узнать автора лучше.',
              }),
              followButton(account),
            ]),
          ),
        )
      : empty(
          'Вы пока не выбрали авторов',
          'Добавьте людей, чьи записи хочется читать чаще.',
          routeLink('authors', 'Открыть новые имена', 'button primary'),
        ),
  );
  renderRail();
}
async function renderSearch(version) {
  const query = route().params.get('q')?.trim() || '';
  searchInput.value = query;
  if (!query) {
    replace(
      main,
      heading('Поиск'),
      empty('Что вам интересно?', 'Введите имя, слово или #тему в строке поиска.'),
    );
    return;
  }
  if (!searches.has(query)) {
    try {
      const result = await request(
        `/api/v2/search?${new URLSearchParams({ q: query, resolve: 'false', limit: '20' })}`,
      );
      const remotePosts = (result.data.statuses || [])
        .map((raw) => normalizeMastodonStatus(raw))
        .filter((post) => post && isRussian(post));
      const people = (result.data.accounts || []).map((raw) => normalizeAccount(raw));
      remember(remotePosts);
      people.forEach((a) => accounts.set(a.id, a));
      searches.set(query, {
        people,
        posts: remotePosts,
        tags: result.data.hashtags || [],
        stale: result.stale,
      });
    } catch {
      searches.set(query, { people: [], posts: [], tags: [], restricted: true });
    }
  }
  if (version !== renderVersion) return;
  const result = searches.get(query);
  const needle = query.toLocaleLowerCase('ru').replace(/^#/, '');
  const matchingPosts = uniquePosts(
    [...result.posts, ...posts.values()].filter(
      (p) =>
        result.posts.includes(p) ||
        `${p.text} ${p.account.name}`.toLocaleLowerCase('ru').includes(needle),
    ),
  );
  const people = [
    ...new Map(
      [
        ...result.people,
        ...[...accounts.values()].filter((a) =>
          `${a.name} ${a.username}`.toLocaleLowerCase('ru').includes(needle),
        ),
      ].map((a) => [a.id, a]),
    ).values(),
  ];
  const tags = [
    ...new Set([
      ...result.tags.map((t) => t.name),
      ...TOPICS.filter((t) => `${t.tag} ${t.name}`.toLocaleLowerCase('ru').includes(needle)).map(
        (t) => t.tag,
      ),
      ...(/^[\p{L}\p{N}_]+$/u.test(needle) ? [needle] : []),
    ]),
  ];
  replace(
    main,
    heading(`Поиск: ${query}`, 'Люди, темы и публичные записи'),
    result.restricted || result.stale
      ? el('div', { class: 'notice' }, [
          result.restricted
            ? 'Гостевой поиск ограничен сервером. Показаны совпадения в загруженных данных; темы можно открыть отдельно.'
            : 'Показаны сохранённые результаты поиска.',
          button(
            'Повторить',
            () => {
              searches.delete(query);
              render();
            },
            { className: 'text-button' },
          ),
        ])
      : el('p', {
          class: 'muted search-note',
          text: 'Полнота поиска публичных записей зависит от настроек сервера.',
        }),
    tags.length
      ? el(
          'div',
          { class: 'topic-chips wrap search-tags' },
          tags.map((tag) => routeLink(`tag/${encodeURIComponent(tag)}`, `#${tag}`, 'chip')),
        )
      : null,
    people.length
      ? el('section', { class: 'card search-people' }, [
          el('h2', { text: 'Люди' }),
          ...people.slice(0, 20).map(personRow),
        ])
      : null,
    matchingPosts.length
      ? el(
          'section',
          { class: 'posts' },
          matchingPosts.map((p) => renderPostCard(p)),
        )
      : empty('Записи не найдены', 'Попробуйте другой запрос или откройте страницу темы.'),
  );
}
function renderProfileComment(comment, contextLabel, { removable = false } = {}) {
  const author = comment.author || { displayName: 'Участник', username: 'local' };
  return el('article', { class: 'card reposted-comment' }, [
    el('div', { class: 'post-context' }, [icon(removable ? 'bookmark' : 'repeat'), contextLabel]),
    el('header', { class: 'post-header' }, [
      avatar({ name: author.displayName }),
      el('div', { class: 'post-author' }, [
        el('strong', { class: 'author-name', text: author.displayName }),
        el('span', { class: 'handle', text: `@${author.username}` }),
      ]),
    ]),
    comment.text && el('p', { class: 'local-comment-text', text: comment.text }),
    localMedia(comment.media).length ? el('div', { class: 'local-comment-media' }, renderMedia({ media: localMedia(comment.media) })) : null,
    el('footer', { class: 'post-actions' }, [
      routeLink(`post/${encodeURIComponent(comment.postId)}?comment=${encodeURIComponent(comment.id)}`, [icon('message'), 'К обсуждению'], 'post-action'),
      removable ? null : commentReactionButton(comment, 'boost', author),
      el('span', { class: 'action-spacer' }),
      removable ? commentSaveButton(comment, author) : null,
    ]),
  ]);
}
function renderMe() {
  if (!signedIn()) {
    replace(
      main,
      heading('Профиль', 'Войдите, чтобы создать своё пространство'),
      guestPrompt(({ posts: 'posts', reposts: 'reposts', likes: 'likedPosts', saved: 'savedPosts', drafts: 'drafts' })[route().params.get('tab')] || 'profile', () => showAuthDialog()),
    );
    return;
  }
  const tab = ['posts', 'reposts', 'likes', 'saved', 'drafts'].includes(route().params.get('tab'))
    ? route().params.get('tab')
    : 'posts';
  const repostedPosts = Object.entries(reader.boosts)
    .filter(([id]) => !id.startsWith('comment:'))
    .map(([id, value]) => value?.post || posts.get(id))
    .filter((post) => post?.id && post?.account);
  const repostedComments = Object.values(reader.boosts)
    .map((value) => value?.comment)
    .filter(Boolean);
  const repostCount = repostedPosts.length + repostedComments.length;
  const profile = el('section', { class: 'card local-profile' }, [
    el('div', { class: 'local-profile-cover', 'aria-hidden': 'true' }, [
      el('span'), el('span'), el('span'),
    ]),
    el('div', { class: 'local-profile-body' }, [
      el('div', { class: 'local-profile-identity' }, [
        avatar({ name: reader.name, avatar: reader.avatar }, true),
        el('div', {}, [
          el('h2', { text: reader.name || 'Рады вас видеть' }),
          el('span', { class: 'profile-handle muted', text: `@${reader.username}` }),
        ]),
      ]),
      el('p', {
        class: 'local-profile-bio',
        text: reader.bio || 'Выбирайте интересы, сохраняйте находки и собирайте свой круг авторов.',
      }),
      el('div', { class: 'local-stats' }, [
        routeLink('authors?tab=selected', [el('strong', { text: Object.keys(reader.follows).length }), ' авторов']),
        routeLink('me?tab=likes', [el('strong', { text: Object.keys(reader.favourites).length }), ' лайков']),
        routeLink('me?tab=saved', [el('strong', { text: Object.keys(reader.saved).length }), ' сохранённых']),
        el('span', {}, [el('strong', { text: reader.localPosts.length }), ' публикаций']),
        el('span', {}, [el('strong', { text: repostCount }), ' репостов']),
        button('Настроить интересы', showPreferences, { className: 'text-button' }),
      ]),
      el('div', { class: 'profile-controls' }, [
        button('Редактировать профиль', showProfileEditor, { className: 'button secondary', symbol: 'edit' }),
        button('Создать публикацию', () => showComposer(), { className: 'button primary', symbol: 'plus' }),
      ]),
    ]),
  ]);
  const drafts = reader.drafts
    .slice()
    .sort((a, b) => Date.parse(b.updatedAt) - Date.parse(a.updatedAt))
    .map((draft) => {
      const remove = () => confirmAction('Удалить черновик?', 'Черновик нельзя будет восстановить.', 'Удалить', () => {
        reader.drafts = reader.drafts.filter((item) => item.id !== draft.id);
        persist();
        render();
        toast('Черновик удалён');
      });
      return el('article', { class: 'card draft' }, [
        el('p', { text: draft.text.slice(0, 240) }),
        el('div', { class: 'draft-footer' }, [
          el('time', { text: formatDateTime(draft.updatedAt) }),
          button('Продолжить', () => showComposer(draft.id), { className: 'text-button' }),
          button('Удалить', remove, { className: 'text-button muted' }),
        ]),
      ]);
    });
  const tabs = el('nav', { class: 'tabs my-line-tabs', 'aria-label': 'Разделы профиля' }, [
    routeLink('me?tab=posts', `Публикации ${reader.localPosts.length}`, tab === 'posts' ? 'active' : ''),
    routeLink('me?tab=reposts', `Репосты ${repostCount}`, tab === 'reposts' ? 'active' : ''),
    routeLink('me?tab=likes', `Лайки ${Object.keys(reader.favourites).length}`, tab === 'likes' ? 'active' : ''),
    routeLink('me?tab=saved', `Сохранённое ${Object.keys(reader.saved).length}`, tab === 'saved' ? 'active' : ''),
    routeLink('me?tab=drafts', `Черновики ${drafts.length}`, tab === 'drafts' ? 'active' : ''),
  ]);
  const tabIcons = ['message', 'repeat', 'heart', 'bookmark', 'edit'];
  [...tabs.children].forEach((link, index) => {
    const label = link.textContent;
    link.setAttribute('aria-label', label);
    link.title = label;
    link.setAttribute('aria-current', link.classList.contains('active') ? 'page' : 'false');
    link.replaceChildren(icon(tabIcons[index]), el('span', { text: label }));
  });
  const repostNodes = [
    ...repostedPosts.map((post) => renderPostCard({ ...post, boostedBy: { name: reader.name || 'Вы' } })),
    ...repostedComments.map((comment) => renderProfileComment(comment, `${reader.name || 'Вы'} поделились комментарием`)),
  ];
  const section = tab === 'posts'
    ? (reader.localPosts.length
        ? el('section', { class: 'posts' }, reader.localPosts.map((post) => renderPostCard(localPostObject(post))))
        : empty('Здесь появятся ваши публикации', 'Расскажите о том, что вам интересно.', button('Написать', () => showComposer(), { className: 'button primary', symbol: 'edit' })))
    : tab === 'reposts'
      ? (repostNodes.length
          ? el('section', { class: 'posts reposts' }, repostNodes)
          : empty('Вы пока ничем не поделились', 'Нажмите кнопку репоста под публикацией или комментарием — материал появится здесь.'))
      : tab === 'likes' || tab === 'saved'
        ? (() => {
            const collection = tab === 'likes' ? reader.favourites : reader.saved;
            const items = Object.values(collection).filter((post) => post?.id && post?.account).reverse();
            const savedComments = tab === 'saved'
              ? Object.values(collection).map((item) => item?.savedComment).filter(Boolean).reverse()
              : [];
            return items.length || savedComments.length
              ? el('section', { class: 'posts personal-collection' }, [
                  ...items.map((post) => renderPostCard(post)),
                  ...savedComments.map((comment) => renderProfileComment(comment, 'Сохранённый комментарий', { removable: true })),
                ])
              : empty(
                  tab === 'likes' ? 'Пока нет лайков' : 'Пока нет сохранённых публикаций',
                  tab === 'likes' ? 'Отмечайте понравившиеся публикации сердцем — они появятся здесь.' : 'Нажмите закладку под публикацией, чтобы быстро найти её позже.',
                  routeLink('feed', 'Перейти в обзор', 'button primary'),
                );
          })()
        : (drafts.length
          ? el('section', { id: 'drafts', class: 'drafts' }, drafts)
          : empty('Нет черновиков', 'Начните публикацию и закройте редактор — текст и вложения сохранятся здесь.', button('Создать черновик', () => showComposer(), { className: 'button primary', symbol: 'edit' })));
  replace(
    main,
    heading(
      'Профиль',
      'Ваш профиль, публикации и всё, чем вы делитесь',
    ),
    profile,
    tabs,
    section,
  );
}
function showProfileEditor() {
  if (!requireAuth('Войдите, чтобы редактировать профиль')) return;
  const modal = dialog('Ваш профиль');
  const name = el('input', {
    name: 'name',
    value: reader.name,
    maxlength: '60',
    placeholder: 'Как вас зовут?',
    autocomplete: 'given-name',
  });
  const bio = el('textarea', { name: 'bio', maxlength: '250', rows: '3', text: reader.bio });
  let selectedAvatar = reader.avatar;
  const avatarInput = el('input', {
    name: 'avatar', type: 'file', accept: 'image/*', hidden: true,
    'aria-label': 'Загрузить фото профиля',
  });
  const avatarPreview = el('div', { class: 'profile-avatar-preview' });
  const renderAvatarPreview = () => replace(
    avatarPreview,
    avatar({ name: name.value || reader.name, avatar: selectedAvatar }, true),
    el('div', {}, [
      el('strong', { text: selectedAvatar ? 'Фото профиля выбрано' : 'Фото профиля' }),
      el('small', { text: 'Квадратное изображение' }),
    ]),
    button(selectedAvatar ? 'Заменить' : 'Загрузить', () => avatarInput.click(), { className: 'button secondary', symbol: 'camera' }),
    selectedAvatar && button('Удалить', () => { selectedAvatar = ''; renderAvatarPreview(); }, { className: 'text-button muted' }),
  );
  avatarInput.addEventListener('change', async () => {
    const file = avatarInput.files?.[0];
    avatarInput.value = '';
    if (!file) return;
    if (!file.type.startsWith('image/')) { toast('Выберите изображение'); return; }
    avatarInput.disabled = true;
    try {
      selectedAvatar = await imageDataUrl(file);
      if (avatarInput.isConnected) renderAvatarPreview();
    } catch {
      toast('Не удалось подготовить изображение');
    } finally {
      avatarInput.disabled = false;
    }
  });
  modal.content.append(
    el(
      'form',
      {
        class: 'form',
        onsubmit: (event) => {
          event.preventDefault();
          reader.name = name.value.trim();
          reader.bio = bio.value.trim();
          if (avatarInput.disabled) { toast('Дождитесь подготовки изображения'); return; }
          reader.avatar = selectedAvatar;
          const state = readState();
          const account = currentUser(state);
          if (account) account.displayName = reader.name || account.username;
          if (!persist(state)) {
            reader = readReader();
            return;
          }
          modal.close();
          renderNavigation();
          root.querySelector('.topbar-avatar')?.replaceChildren(avatar({ name: reader.name, avatar: reader.avatar }));
          render();
        },
      },
      [
        el('label', {}, ['Имя', name]),
        el('label', {}, ['О себе', bio]),
        avatarInput,
        avatarPreview,
        el('button', { type: 'submit', class: 'button primary', text: 'Сохранить' }),
      ],
    ),
  );
  renderAvatarPreview();
  name.focus();
}
function showPreferences() {
  const modal = dialog('На вашей волне');
  const selected = new Set(reader.topics);
  const options = el(
    'div',
    { class: 'interest-options' },
    TOPICS.map((topic) => {
      const input = el('input', {
        type: 'checkbox',
        value: topic.tag,
        checked: selected.has(topic.tag),
        onchange: () => (input.checked ? selected.add(topic.tag) : selected.delete(topic.tag)),
      });
      return el('label', { class: 'interest-option' }, [
        icon(topic.icon),
        el('span', {}, [el('strong', { text: topic.name }), el('small', { text: topic.caption })]),
        input,
      ]);
    }),
  );
  modal.content.append(
    el('p', { text: 'Выберите темы, которые вам близки. Из них мы соберём обзор.' }),
    options,
    button(
      'Сохранить интересы',
      () => {
        if (!selected.size) {
          toast('Выберите хотя бы одну тему');
          return;
        }
        reader.topics = [...selected];
        persist();
        feeds.delete('overview');
        modal.close();
        if (route().page === 'feed' && feedKey() === 'overview') {
          const feed = getFeed('overview');
          feed.posts = [];
          render();
        } else location.hash = '#/feed';
      },
      { className: 'button primary full-width' },
    ),
  );
}
function showComposer(id) {
  if (!requireAuth('Войдите, чтобы создавать истории')) return;
  let draft = reader.drafts.find((d) => d.id === id);
  const textarea = el('textarea', {
    class: 'compose-text',
    maxlength: '5000',
    rows: '8',
    placeholder: 'О чём вы думаете? Поделитесь своей историей…',
    'aria-label': 'Текст черновика',
    text: draft?.text || '',
  });
  let attachments = localMedia(draft?.media);
  const attachmentList = el('div', { class: 'composer-attachments', 'aria-live': 'polite' });
  const attachmentInput = el('input', {
    type: 'file', accept: 'image/*,video/*', multiple: true, hidden: true,
    'aria-label': 'Добавить фото или видео',
  });
  const status = el('span', { class: 'muted', role: 'status' });
  let timer;
  let published = false;
  const renderAttachments = () => replace(
    attachmentList,
    ...attachments.map((media, index) =>
      el('figure', { class: 'composer-attachment' }, [
        media.type === 'video'
          ? el('video', { src: media.url, muted: true, preload: 'metadata', playsinline: true })
          : picture(media.url, media.alt || 'Выбранное изображение'),
        el('figcaption', { text: media.alt || (media.type === 'video' ? 'Видео' : 'Фото') }),
        iconButton('Удалить вложение', 'close', () => {
          attachments = attachments.filter((_, item) => item !== index);
          renderAttachments();
          save();
        }),
      ]),
    ),
  );
  attachmentInput.addEventListener('change', async () => {
    if (attachmentInput.disabled) return;
    const files = [...attachmentInput.files];
    attachmentInput.value = '';
    if (!files.length) return;
    if (attachments.length + files.length > MAX_ATTACHMENTS) {
      toast(`Можно добавить не более ${MAX_ATTACHMENTS} вложений`);
      return;
    }
    status.textContent = translate('Подготавливаем вложения…');
    attachmentInput.disabled = true;
    try {
      const added = [];
      for (const file of files) {
        if (file.type.startsWith('image/')) {
          const url = await imageDataUrl(file);
          added.push({ id: newId('media'), type: 'image', url, preview: url, alt: file.name });
        } else if (file.type.startsWith('video/')) {
          const url = await readFileAsDataUrl(file);
          added.push({ id: newId('media'), type: 'video', url, preview: '', alt: file.name });
        } else toast(`Файл «${file.name}» не является изображением или видео`);
      }
      if (!attachmentInput.isConnected) return;
      attachments = [...attachments, ...added];
      renderAttachments();
      save();
      status.textContent = attachments.length ? translate(`${attachments.length} вложения добавлено`) : '';
    } catch (error) {
      status.textContent = '';
      toast(error.message || 'Не удалось подготовить вложение');
    } finally {
      attachmentInput.disabled = false;
    }
  });
  function save() {
    const text = textarea.value;
    if (!text.trim() && !attachments.length && !draft) return;
    if (!text.trim() && !attachments.length && draft) {
      reader.drafts = reader.drafts.filter((item) => item.id !== draft.id);
      draft = null;
      status.textContent = translate(writeReader(reader) ? 'Пустой черновик удалён' : 'Не удалось удалить черновик');
      return;
    }
    if (!draft) {
      draft = { id: crypto.randomUUID(), text: '', updatedAt: '' };
      reader.drafts.push(draft);
    }
    draft.text = text;
    draft.media = attachments;
    draft.updatedAt = new Date().toISOString();
    status.textContent = translate(writeReader(reader)
      ? 'Черновик сохранён'
      : 'Не удалось сохранить черновик');
  }
  const modal = dialog('Новая история', {
    onClose: () => {
      clearTimeout(timer);
      if (!published) save();
      if (route().page === 'me') render();
    },
  });
  modal.content.classList.add('editor-dialog', 'story-editor');
  const count = el('span', { class: 'muted', text: `${textarea.value.length} / 5000` });
  textarea.addEventListener('input', () => {
    count.textContent = `${textarea.value.length} / 5000`;
    status.textContent = translate('Сохраняем…');
    clearTimeout(timer);
    timer = setTimeout(save, 500);
  });
  modal.content.append(
    el('div', { class: 'composer-author' }, [
      avatar({ name: reader.name, avatar: reader.avatar }),
      el('div', {}, [
        el('strong', { text: reader.name || 'Ваша история' }),
        el('small', { text: 'Новая история' }),
      ]),
    ]),
    textarea,
    attachmentInput,
    el('div', { class: 'composer-media-actions' }, [
      button('Добавить фото или видео', () => attachmentInput.click(), {
        className: 'button secondary', symbol: 'camera',
      }),
      el('small', { text: `До ${MAX_ATTACHMENTS} вложений` }),
    ]),
    attachmentList,
    el('div', { class: 'compose-status' }, [status, count]),
    el('p', {
      class: 'muted',
      text: 'Черновик сохраняется автоматически.',
    }),
    el('div', { class: 'form-actions' }, [
      button('Опубликовать', () => {
        if (attachmentInput.disabled) { toast('Дождитесь подготовки вложений'); return; }
        const text = textarea.value.trim();
        if (!text && !attachments.length) { textarea.focus(); return; }
        const post = { id: newId('post'), text, media: attachments, createdAt: new Date().toISOString() };
        reader.localPosts.unshift(post);
        if (draft) reader.drafts = reader.drafts.filter((item) => item.id !== draft.id);
        published = true;
        if (!persist()) {
          published = false;
          reader.localPosts.shift();
          if (draft) reader.drafts.push(draft);
          return;
        }
        remember([localPostObject(post)]);
        modal.close();
        if ((location.hash || '#/feed') === '#/me') render();
        else location.hash = '#/me';
        toast('История опубликована в Line');
      }, { className: 'button primary', symbol: 'edit' }),
    ]),
  );
  renderAttachments();
  textarea.focus();
}
function showAbout() {
  const modal = dialog('Line — люди и истории');
  modal.content.append(
    el('p', {
      text: 'Line — место для историй, фотографий и разговоров с интересными людьми.',
    }),
    el('p', {
      text: 'Создайте профиль, собирайте интересные истории и присоединяйтесь к разговорам.',
    }),
  );
}
async function render() {
  const version = ++renderVersion;
  const current = route();
  renderNavigation();
  renderRail();
  document.title = `Line — ${translate({ feed: 'Обзор', topics: 'Темы', authors: 'Авторы', saved: 'Сохранённое', me: 'Профиль', search: 'Поиск', profile: 'Профиль', post: 'Обсуждение', tag: '#' + current.id }[current.page] || 'Line')}`;
  if (current.page !== 'search') searchInput.value = '';
  try {
    if (current.page === 'feed' || current.page === 'tag') renderFeed(feedKey());
    else if (current.page === 'topics') renderTopics();
    else if (current.page === 'saved') {
      location.hash = `#/me?tab=${current.params.get('tab') === 'favourites' ? 'likes' : 'saved'}`;
      return;
    }
    else if (current.page === 'me') renderMe();
    else if (current.page === 'profile' && current.id === 'me') {
      location.hash = '#/me';
      return;
    } else if (['profile', 'post', 'authors', 'search'].includes(current.page)) {
      replace(main, skeleton());
      if (current.page === 'profile') await renderProfile(current.id, version);
      if (current.page === 'post') await renderDiscussion(current.id, version);
      if (current.page === 'authors') await renderAuthors(version);
      if (current.page === 'search') await renderSearch(version);
    } else
      replace(
        main,
        empty(
          'Страница не найдена',
          'Вернитесь в обзор, чтобы продолжить читать.',
          routeLink('feed', 'В обзор', 'button primary'),
        ),
      );
  } catch (error) {
    if (version === renderVersion)
      replace(
        main,
        empty(
          'Не удалось открыть страницу',
          error.message || 'Проверьте подключение и попробуйте ещё раз.',
          button('Повторить', render, { className: 'button primary', symbol: 'refresh' }),
        ),
      );
  }
  if (version === renderVersion)
    requestAnimationFrame(() => {
      const position = scrollPositions.get(location.hash || '#/feed');
      if (position != null) window.scrollTo(0, position);
    });
}
window.addEventListener('hashchange', () => {
  scrollPositions.set(previousHash, window.scrollY);
  previousHash = location.hash || '#/feed';
  window.scrollTo(0, scrollPositions.get(previousHash) || 0);
  render();
});
window.addEventListener(
  'scroll',
  () => scrollPositions.set(location.hash || '#/feed', window.scrollY),
  { passive: true },
);
window.addEventListener('storage', (event) => {
  if (event.key === `disguise:${AUTH_KEY}` && !document.querySelector('dialog[open]')) {
    reloadReader();
    render();
  }
});
async function checkNewPosts() {
  if (document.hidden || polling || !['feed', 'tag'].includes(route().page)) return;
  const key = feedKey();
  const feed = getFeed(key);
  if (!feed.loaded || Date.now() - feed.updated < 120000) return;
  polling = true;
  try {
    await loadFeed(key, { stage: true });
  } finally {
    polling = false;
  }
}
setInterval(checkNewPosts, 150000);
window.addEventListener('online', () => {
  if (['feed', 'tag'].includes(route().page)) loadFeed(feedKey(), { stage: true });
});
render();
request('/api/v1/trends/tags?limit=8', { ttl: 900000 })
  .then(({ data }) => {
    if (Array.isArray(data))
      trends = data.map((tag) => ({
        name: tag.name,
        caption: tag.history?.[0]?.accounts
          ? `${number(tag.history[0].accounts)} участников за день`
          : 'Сейчас обсуждают',
      }));
    renderRail();
    if (route().page === 'topics') renderTopics();
  })
  .catch(() => {
    /* Curated topics remain available. */
  });
