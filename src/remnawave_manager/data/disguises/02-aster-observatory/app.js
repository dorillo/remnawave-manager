import { guestPrompt, emptyPrompt } from '../shared/guest-state.js';
import { loadingNode } from '../shared/feedback.js';
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
import { comments } from './aster-comments.js';
import {
  t,
  locale,
  setLanguage,
  date,
  duration,
  relativeDate,
} from './aster-i18n.js';
import {
  TOPICS,
  loadCatalog,
  loadVideo,
  loadProfile,
  searchRutube,
  normalize,
  normalizeList,
} from './aster-data.js';
import {
  user,
  library,
  updateUser,
  toggle,
  remember,
} from './aster-store.js';
import { signIn, register, signOut, avatar } from './aster-auth.js';
import {
  el,
  button,
  link,
  toast,
  modal,
  confirmAction,
  field,
} from './aster-ui.js';
import { mountPlayer, mountUploadedPlayer } from './aster-player.js';
import { saveVideo, videosFor, openVideo, removeVideo } from './aster-uploads.js';

const root = document.querySelector('#app');
const navigation = [
  'home',
  'catalog',
  'subscriptions',
  'history',
  'likes',
  'later',
  'profile',
];
const navigationIcons = {
  globe: 'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0M3 12h18M12 3a18 18 0 0 1 0 18 18 18 0 0 1 0-18',
  home: 'm3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1Z',
  catalog: 'M4 4h6v6H4ZM14 4h6v6h-6ZM4 14h6v6H4ZM14 14h6v6h-6Z',
  subscriptions:
    'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M13 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8',
  history: 'M12 6v6l4 2M21 12a9 9 0 1 1-3-6.7M21 3v5h-5',
  likes:
    'M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8Z',
  later: 'M6 3h12v18l-6-4-6 4Z',
  profile: 'M20 21v-2a6 6 0 0 0-6-6h-4a6 6 0 0 0-6 6v2M16 6a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z',
};
function navigationIcon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  for (const [key, value] of Object.entries({
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    'stroke-width': '1.8',
    'stroke-linecap': 'round',
    'stroke-linejoin': 'round',
    'aria-hidden': 'true',
    class: 'sidebar-icon',
  }))
    svg.setAttribute(key, value);
  const path = document.createElementNS(svg.namespaceURI, 'path');
  path.setAttribute('d', navigationIcons[name] || navigationIcons.home);
  svg.append(path);
  return svg;
}
let catalog,
  loading = true,
  failed = false,
  request;
let searchRequest;
let searchState = {
  query: '',
  videos: [],
  page: 0,
  hasNext: false,
  loading: false,
  failed: false,
};
let disposePlayer = () => {};
let pageController = new AbortController();
const recentDiscussions = new Map();
function rememberDiscussion(video) {
  recentDiscussions.delete(video.id);
  recentDiscussions.set(video.id, video);
  if (recentDiscussions.size > 50) recentDiscussions.delete(recentDiscussions.keys().next().value);
}
function subscribersNode(value = null) {
  return el('small', { class: 'subscriber-count', hidden: value === null },
    value === null ? '' : `${new Intl.NumberFormat(locale()).format(value)} ${t('subscribers')}`);
}
function showSubscribers(node, value) {
  node.hidden = value === null;
  node.textContent = value === null ? '' : `${new Intl.NumberFormat(locale()).format(value)} ${t('subscribers')}`;
}
function route() {
  const [path, query = ''] = location.hash.slice(1).split('?');
  return {
    parts: (path || '/home').split('/').filter(Boolean),
    params: new URLSearchParams(query),
  };
}
const go = (path) => {
  location.hash = path;
};
function guard(action) {
  if (!user()) return auth();
  try {
    action();
  } catch {
    toast(t('storageError'));
  }
}
const channelVideos = (id) =>
  catalog?.channels?.find((channel) => channel.id === id)?.videos || [];
function discoveredVideos() {
  const channels = catalog?.channels || [];
  const rows = [];
  const longest = Math.max(0, ...channels.map((channel) => channel.videos.length));
  for (let index = 0; index < longest; index += 1)
    for (const channel of channels) {
      if (channel.videos[index]) rows.push(channel.videos[index]);
    }
  return rows;
}
const allVideos = () =>
  normalizeList(
    [
      ...(catalog?.videos || []),
      ...discoveredVideos(),
      ...library().added,
    ],
    5000,
  );
const findVideo = (id) =>
  searchState.videos.find((video) => video.id === id) ||
  allVideos().find((video) => video.id === id) ||
  catalog?.channels
    ?.flatMap((channel) => channel.videos)
    .find((video) => video.id === id);

function randomized(items) {
  const result = [...items];
  for (let index = result.length - 1; index > 0; index -= 1) {
    const selected = Math.floor(Math.random() * (index + 1));
    [result[index], result[selected]] = [result[selected], result[index]];
  }
  return result;
}

function channelAvatar(video, large = false) {
  const fallback = el(
    'span',
    {
      class: `channel-avatar${large ? ' large' : ''}`,
      'aria-hidden': true,
    },
    (video.channel || 'A').trim().slice(0, 1).toUpperCase(),
  );
  if (video.avatar)
    fallback.prepend(
      el('img', {
        src: video.avatar,
        alt: '',
        loading: 'lazy',
        decoding: 'async',
        onerror: (event) => event.target.remove(),
      }),
    );
  return fallback;
}

function auth(registration = false) {
  const name = el('input', {
    name: 'name',
    autocomplete: 'nickname',
    maxlength: 80,
    required: true,
  });
  const email = el('input', {
    type: 'email',
    name: 'email',
    autocomplete: 'username',
    maxlength: 254,
    required: true,
  });
  const password = el('input', {
    type: 'password',
    name: 'password',
    autocomplete: registration ? 'new-password' : 'current-password',
    minlength: 8,
    maxlength: 128,
    required: true,
  });
  const error = el('p', { role: 'alert', class: 'form-error' });
  const submit = el(
    'button',
    { type: 'submit', class: 'primary' },
    t(registration ? 'register' : 'login'),
  );
  const form = el(
    'form',
    { class: 'stack' },
    registration ? field(t('name'), name) : null,
    field(t('email'), email),
    field(t('password'), password),
    error,
  );
  const controls = modal(t(registration ? 'register' : 'login'), form);
  form.append(
    el(
      'div',
      { class: 'dialog-actions auth-actions' },
      button(t(registration ? 'login' : 'register'), () => {
        controls.close();
        auth(!registration);
      }),
      submit,
    ),
  );
  let busy = false;
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    if (busy) return;
    busy = true;
    submit.disabled = true;
    error.textContent = '';
    try {
      if (registration) await register(name.value, email.value, password.value);
      else await signIn(email.value, password.value);
      controls.close();
      render();
    } catch (reason) {
      error.textContent = t(
        ['authError', 'invalid', 'duplicate', 'storageError'].includes(
          reason.message,
        )
          ? reason.message
          : 'authError',
      );
    } finally {
      busy = false;
      submit.disabled = false;
    }
  });
}
function shell() {
  const { parts, params } = route();
  const search = el('input', {
    type: 'search',
    name: 'q',
    placeholder: t('search'),
    'aria-label': t('search'),
    value: params.get('q') || '',
    maxlength: 200,
  });
  const form = el(
    'form',
    {
      class: 'search',
      role: 'search',
      onsubmit: (event) => {
        event.preventDefault();
        go(`/catalog?q=${encodeURIComponent(search.value.trim())}`);
      },
    },
    search,
    el('button', { type: 'submit', 'aria-label': t('search') }, '⌕'),
  );
  const nav = el(
    'nav',
    { 'aria-label': t('menu') },
    navigation.map((item) =>
      link(
        [
          navigationIcon(item),
          el('span', {}, t(item)),
        ],
        `#/${item}`,
        {
          class: parts[0] === item ? 'active' : '',
          'aria-current': parts[0] === item ? 'page' : null,
        },
      ),
    ),
  );
  const sidebar = el('aside', { class: 'sidebar', id: 'sidebar' }, nav);
  const backdrop = el('button', {
    type: 'button',
    class: 'sidebar-backdrop',
    'aria-label': t('close'),
    onclick: () => {
      sidebar.classList.remove('open');
      menu.setAttribute('aria-expanded', 'false');
    },
  });
  const menu = button(
    '☰',
    () => {
      if (!matchMedia('(max-width: 700px)').matches) {
        const compact = root.classList.toggle('sidebar-compact');
        menu.setAttribute('aria-expanded', String(!compact));
        return;
      }
      menu.setAttribute(
        'aria-expanded',
        String(sidebar.classList.toggle('open')),
      );
    },
    {
      class: 'menu icon-button',
      'aria-label': t('menu'),
      'aria-controls': 'sidebar',
      'aria-expanded': String(
        !matchMedia('(max-width: 700px)').matches &&
          !root.classList.contains('sidebar-compact'),
      ),
    },
  );
  const language = button(locale() === 'ru' ? 'EN' : 'RU', () => {
    if (!setLanguage(locale() === 'ru' ? 'en' : 'ru')) toast(t('storageError'));
    render();
  }, { class: 'icon-button', 'aria-label': t('interfaceLanguage') });
  const appearanceButton = button('◐', toggleTheme, {
    class: 'icon-button', 'aria-label': 'Switch theme / Сменить тему',
  });
  const header = el(
    'header',
    { class: 'top' },
    el(
      'div',
      { class: 'top-brand' },
      menu,
      link(
        [
          el('span', { class: 'logo-mark', 'aria-hidden': true }, '▶'),
          el('span', {}, 'Aster'),
        ],
        '#/home',
        { class: 'logo', 'aria-label': 'Aster' },
      ),
    ),
    form,
    el(
      'div',
      { class: 'top-actions' },
      language,
      appearanceButton,
      button(
        [
          el(
            'span',
            { class: 'account-avatar', 'aria-hidden': true },
            (user()?.name || t('login')).trim().slice(0, 1).toUpperCase(),
          ),
          el('span', { class: 'account-name' }, user()?.name || t('login')),
        ],
        () => (user() ? go('/profile') : auth()),
        { class: 'account' },
      ),
    ),
  );
  const main = el('main', {
    id: 'content',
    tabindex: '-1',
    class: parts[0] === 'watch' ? 'watch-page' : '',
  });
  root.replaceChildren(
    link(t('skip'), '#content', {
      class: 'skip',
      onclick: (event) => {
        event.preventDefault();
        main.focus();
      },
    }),
    header,
    sidebar,
    backdrop,
    main,
  );
  return main;
}
function card(video) {
  const progress = library().history[video.id];
  const image = video.thumbnail
    ? el('img', {
        src: video.thumbnail,
        alt: '',
        loading: 'lazy',
        decoding: 'async',
        onerror: (event) => {
          event.target.hidden = true;
        },
      })
    : null;
  const thumb = link(
    [
      image,
      el('span', { class: 'thumb-play', 'aria-hidden': true }, '▶'),
      video.duration
        ? el('span', { class: 'duration' }, duration(video.duration))
        : null,
    ],
    `#/watch/${video.id}`,
    {
      class: 'thumb',
      'aria-label': `${t(progress?.time && !progress.complete ? 'resume' : 'watch')}: ${video.title}`,
    },
  );
  if (progress?.time && video.duration)
    thumb.append(
      el('progress', {
        max: video.duration,
        value: progress.complete
          ? video.duration
          : Math.min(progress.time, video.duration),
        'aria-label': t('resume'),
      }),
    );
  const save = button(
    library().later.includes(video.id) ? '✓' : '◷',
    () =>
      guard(() => {
        updateUser((a) => {
          a.library.added = normalizeList([video, ...a.library.added]);
          const list = a.library.later;
          a.library.later = list.includes(video.id)
            ? list.filter((id) => id !== video.id)
            : [...list, video.id];
        });
        save.textContent = library().later.includes(video.id) ? '✓' : '◷';
        save.setAttribute(
          'aria-pressed',
          String(library().later.includes(video.id)),
        );
      }),
    {
      class: 'card-save',
      'aria-label': t('later'),
      'aria-pressed': String(library().later.includes(video.id)),
    },
  );
  return el(
    'article',
    { class: 'video-card' },
    el('div', { class: 'thumb-wrap' }, thumb, save),
    el(
      'div',
      { class: 'card-body' },
      channelAvatar(video),
      el(
        'div',
        { class: 'card-copy' },
        el('h3', {}, link(video.title, `#/watch/${video.id}`)),
        video.channelId
          ? link(video.channel, `#/channel/${video.channelId}`, {
              class: 'channel-link',
            })
          : el('span', { class: 'channel-link' }, video.channel),
        el(
          'p',
          { class: 'meta' },
          [
            video.views !== null
              ? `${new Intl.NumberFormat(locale(), { notation: 'compact' }).format(video.views)} ${t('views')}`
              : '',
            date(video.published),
          ]
            .filter(Boolean)
            .join(' · '),
        ),
      ),
    ),
  );
}
function empty(main, title = 'empty') {
  main.append(
    el(
      'div',
      { class: 'empty-state' },
      el('span', { class: 'empty-symbol', 'aria-hidden': true }, '▷'),
      el('h2', {}, t(title)),
      el('p', {}, t('emptyHint')),
      link(t('back'), '#/catalog', { class: 'button' }),
    ),
  );
}
function grid(
  main,
  videos,
  paginate = true,
  className = '',
  pageSize = 24,
  moreLabel = 'more',
) {
  if (!videos.length) return empty(main);
  let visible = paginate ? pageSize : videos.length;
  const content = el(
    'div',
    { class: `video-grid ${className}`.trim() },
    videos.slice(0, visible).map(card),
  );
  main.append(content);
  if (paginate && videos.length > visible) {
    const more = button(
      t(moreLabel),
      () => {
        const start = visible;
        visible = Math.min(visible + pageSize, videos.length);
        content.append(...videos.slice(start, visible).map(card));
        more.hidden = visible >= videos.length;
      },
      { class: 'more' },
    );
    main.append(more);
  }
}
function heading(main, title, actions = []) {
  main.append(
    el(
      'div',
      { class: 'page-head' },
      el('h1', {}, title),
      el('div', { class: 'button-row' }, actions),
    ),
  );
}
function filters(main) {
  const { params } = route();
  const change = (key, value) => {
    const next = new URLSearchParams(params);
    value ? next.set(key, value) : next.delete(key);
    go(`/catalog?${next}`);
  };
  main.append(
    el(
      'div',
      { class: 'chips', 'aria-label': t('all') },
      ['', ...TOPICS].map((topic) =>
        button(t(topic || 'all'), () => change('topic', topic), {
          'aria-pressed': String((params.get('topic') || '') === topic),
        }),
      ),
    ),
  );
  const select = (key, label, choices) =>
    field(
      t(label),
      el(
        'select',
        { onchange: (event) => change(key, event.target.value) },
        choices.map(([value, text]) =>
          el(
            'option',
            { value, selected: (params.get(key) || '') === value },
            t(text),
          ),
        ),
      ),
    );
  main.append(
    el(
      'div',
      { class: 'filters' },
      select('sort', 'sort', [
        ['', 'recommended'],
        ['newest', 'newest'],
        ['title', 'title'],
      ]),
      select('duration', 'duration', [
        ['', 'any'],
        ['short', 'short'],
        ['long', 'long'],
      ]),
      select('language', 'language', [
        ['', 'any'],
        ['ru', 'ru'],
        ['en', 'en'],
      ]),
    ),
  );
}
function filtered(videos, matchQuery = true) {
  const { params } = route();
  const q = (params.get('q') || '').toLocaleLowerCase();
  const selected = videos.filter(
      (v) =>
        (!matchQuery ||
          !q ||
          `${v.title} ${v.channel} ${v.description}`
            .toLocaleLowerCase()
            .includes(q)) &&
        (!params.get('topic') || v.topic === params.get('topic')) &&
        (!params.get('language') || v.language === params.get('language')) &&
        (!params.get('duration') ||
          (params.get('duration') === 'short'
            ? v.duration > 0 && v.duration < 1200
            : v.duration >= 1200)),
    );
  if (params.get('sort') === 'title')
    return selected.sort((a, b) => a.title.localeCompare(b.title, locale()));
  if (params.get('sort') === 'newest')
    return selected.sort((a, b) => b.published.localeCompare(a.published));
  return randomized(selected);
}
async function loadSearchPage(query, page = 1) {
  if (searchState.loading && searchState.query === query && searchState.page)
    return;
  searchRequest?.abort();
  const active = new AbortController();
  searchRequest = active;
  searchState = {
    ...searchState,
    query,
    loading: true,
    failed: false,
    ...(page === 1 ? { videos: [], page: 0, hasNext: false } : {}),
  };
  render();
  try {
    const result = await searchRutube(query, page, active.signal);
    if (active.signal.aborted || route().params.get('q') !== query) return;
    searchState = {
      query,
      videos: normalizeList(
        [...(page === 1 ? [] : searchState.videos), ...result.videos],
        2000,
      ),
      page: result.page,
      hasNext: result.hasNext,
      loading: false,
      failed: false,
    };
  } catch {
    if (active.signal.aborted) return;
    searchState = { ...searchState, loading: false, failed: true };
  }
  render();
}
function searchResults(main, query) {
  if (searchState.query !== query) {
    searchState = {
      query,
      videos: [],
      page: 0,
      hasNext: false,
      loading: true,
      failed: false,
    };
    queueMicrotask(() => loadSearchPage(query));
  }
  if (searchState.failed) {
    grid(main, filtered(allVideos()), true, '', 24, 'moreVideos');
    return;
  }
  const videos = filtered(searchState.videos, false);
  if (videos.length) grid(main, videos, false);
  else if (!searchState.loading) empty(main);
  if (searchState.loading)
    main.append(el('div', { class: 'loading search-loading', role: 'status' }, loadingNode(t('loading'))));
  else if (searchState.hasNext)
    main.append(
      button(
        t('moreVideos'),
        () => loadSearchPage(query, searchState.page + 1),
        { class: 'more' },
      ),
    );
}
function home(main) {
  const videos = randomized(allVideos());
  heading(main, t('forYou'));
  grid(main, videos);
}
function followButton(id) {
  const control = button(
    t(library().follows.includes(id) ? 'following' : 'follow'),
    () =>
      guard(() => {
        toggle('follows', id);
        control.textContent = t(
          library().follows.includes(id) ? 'following' : 'follow',
        );
        control.setAttribute(
          'aria-pressed',
          String(library().follows.includes(id)),
        );
      }),
    { 'aria-pressed': String(library().follows.includes(id)) },
  );
  return control;
}
function uploadedVideo(record, account) {
  return {
    ...record,
    published: new Date(record.createdAt).toISOString(),
    channel: account.name,
    channelId: '',
    avatar: account.avatar,
    views: Math.max(0, Number(record.views) || 0),
    publicLikes: null,
    commentsCount: 0,
    publicComments: [],
    local: true,
  };
}

function rememberUploadedVideo(video, time, complete) {
  updateUser((account) => {
    account.library.history[video.id] = { time, complete, at: Date.now() };
    account.library.history = Object.fromEntries(
      Object.entries(account.library.history)
        .sort((a, b) => b[1].at - a[1].at)
        .slice(0, 1000),
    );
  });
}

function watch(main, id, uploaded = null) {
  const video = uploaded ? uploadedVideo(uploaded, user()) : findVideo(id);
  if (!video) return empty(main, 'notFound');
  const primary = el('div', { class: 'watch-primary' });
  const rail = el('aside', {
    class: 'watch-rail',
    'aria-label': t('related'),
  });
  main.append(el('div', { class: 'watch-layout' }, primary, rail));
  const stage = el('section', { class: 'watch-stage' });
  primary.append(stage);
  const position = library().history[id];
  const owner = user()?.id;
  let reportedFailure = false;
  const progress = (time, complete) => {
    if (!owner || user()?.id !== owner) return;
    try {
      if (video.local) rememberUploadedVideo(video, time, complete);
      else remember(video, time, complete);
    } catch {
      if (!reportedFailure) toast(t('storageError'));
      reportedFailure = true;
    }
  };
  disposePlayer = video.local
    ? mountUploadedPlayer(
        stage,
        video,
        position?.complete ? 0 : position?.time || 0,
        progress,
      )
    : mountPlayer(
        stage,
        video,
        position?.complete ? 0 : position?.time || 0,
        progress,
        (metadata) => {
          if (
            !owner ||
            user()?.id !== owner ||
            !library().added.some((v) => v.id === id)
          )
            return;
          const updated = normalize({
            ...video,
            title:
              typeof metadata.title === 'string' ? metadata.title : video.title,
            thumbnail: metadata.thumbnail_url || video.thumbnail,
            duration: Number(metadata.duration) || video.duration,
          });
          try {
            updateUser((a) => {
              a.library.added = normalizeList([updated, ...a.library.added]);
            });
          } catch {
            /* Optional enrichment never interrupts playback. */
          }
        },
      );
  primary.append(el('h1', { class: 'watch-title' }, video.title));
  if (video.views !== null)
    primary.append(
      el(
        'p',
        { class: 'meta' },
        `${new Intl.NumberFormat(locale()).format(video.views)} ${t('views')}`,
      ),
    );
  const action = (key) => {
    const likesLabel = () => {
      const selected = Number(library().likes.includes(id));
      const count =
        video.publicLikes === null && !selected
          ? ''
          : ` ${new Intl.NumberFormat(locale()).format(
              (video.publicLikes || 0) + selected,
            )}`;
      return count.trim();
    };
    const control = button(
      key === 'likes' ? likesLabel() : t(key),
      () =>
        guard(() => {
          updateUser((a) => {
            if (!video.local)
              a.library.added = normalizeList([video, ...a.library.added]);
            const list = a.library[key];
            a.library[key] = list.includes(id)
              ? list.filter((v) => v !== id)
              : [...list, id];
          });
          if (key === 'likes') control.querySelector('.like-count').textContent = likesLabel();
          control.setAttribute(
            'aria-pressed',
            String(library()[key].includes(id)),
          );
        }),
      {
        'aria-label': key === 'likes' ? t('likeVideo') : t(key),
        'aria-pressed': String(library()[key].includes(id)),
      },
    );
    if (key === 'likes') {
      const heart = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      heart.setAttribute('viewBox', '0 0 24 24');
      heart.setAttribute('aria-hidden', 'true');
      heart.innerHTML = '<path d="M20.8 4.6a5.5 5.5 0 0 0-7.8 0L12 5.7l-1.1-1.1a5.5 5.5 0 0 0-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 0 0 0-7.8Z"/>';
      control.classList.add('video-like');
      control.replaceChildren(heart, el('span', { class: 'like-count' }, likesLabel()));
    }
    return control;
  };
  const subscribers = subscribersNode(video.subscribers ?? null);
  primary.append(
    el(
      'div',
      { class: 'watch-actions' },
      el(
        'div',
        { class: 'watch-channel' },
        channelAvatar(video, true),
        el('div', { class: 'watch-channel-copy' }, video.local
          ? link(video.channel, '#/profile', { class: 'channel-title' })
          : video.channelId
          ? link(video.channel, `#/channel/${video.channelId}`, {
              class: 'channel-title',
            })
          : el('strong', {}, video.channel), subscribers),
        !video.local && video.channelId ? followButton(video.channelId) : null,
      ),
      el(
        'div',
        { class: 'button-row' },
        action('likes'),
        action('later'),
        button(t('share'), async () => {
          const url = new URL(location.href);
          url.hash = `/watch/${video.id}`;
          try {
            await navigator.clipboard.writeText(url.href);
            toast(t('copied'));
          } catch {
            modal(
              t('share'),
              el('input', {
                value: url.href,
                readonly: true,
                'aria-label': t('link'),
                onfocus: (event) => event.target.select(),
              }),
            );
          }
        }),
      ),
    ),
  );
  const description = el('p', {}, video.description || (video.local ? t('noDescription') : loadingNode(t('loading'))));
  primary.append(
    el(
      'details',
      { class: 'description', open: true },
      el('summary', {}, t('description'), ' · ', date(video.published)),
      description,
    ),
  );
  const commentsController = new AbortController();
  primary.append(comments(video, guard, commentsController.signal, rememberDiscussion));
  if (!video.local) {
    const signal = commentsController.signal;
    const refreshDescription = async () => {
      try {
        const current = await loadVideo(id, signal);
        if (signal.aborted) return;
        video.description = current.description;
        description.textContent = current.description || t('noDescription');
      } catch {
        if (signal.aborted || video.description) return;
        description.replaceChildren(t('descriptionUnavailable'), ' ', button(t('commentsRetry'), refreshDescription));
      }
    };
    refreshDescription();
    if (video.channelId) loadProfile(video.channelId, signal).then(profile => {
      if (!signal.aborted) showSubscribers(subscribers, profile.subscribers);
    }).catch(() => {});
  }
  const stopPlayer = disposePlayer;
  disposePlayer = () => { commentsController.abort(); stopPlayer(); };
  rail.append(el('h2', {}, t('related')));
  grid(
    rail,
    randomized(
      normalizeList([
        ...channelVideos(video.channelId),
        ...allVideos(),
      ]).filter((v) => v.id !== id),
    ),
    true,
    'related-grid',
    18,
  );
}

async function watchUploaded(main, id) {
  const account = user();
  main.append(el('div', { class: 'loading', role: 'status' }, loadingNode(t('loading'))));
  if (!account) {
    main.replaceChildren();
    empty(main, 'notFound');
    return;
  }
  try {
    const uploaded = await openVideo(account.id, id);
    if (
      !main.isConnected ||
      user()?.id !== account.id ||
      route().parts.join('/') !== `watch/${id}`
    )
      return;
    main.replaceChildren();
    if (uploaded) watch(main, id, uploaded);
    else empty(main, 'notFound');
  } catch {
    if (main.isConnected) {
      main.replaceChildren();
      empty(main, 'notFound');
    }
  }
}
function channel(main, id) {
  const signal = pageController.signal;
  const videos = normalizeList([
    ...channelVideos(id),
    ...searchState.videos.filter(video => video.channelId === id),
    ...allVideos().filter(video => video.channelId === id),
  ]);
  const author = videos[0];
  const header = el('section', { class: 'channel-banner' });
  main.append(header);
  const drawHeader = profile => {
    header.replaceChildren(
      channelAvatar({ channel: profile.name, avatar: profile.avatar }, true),
      el('div', { class: 'channel-copy' },
        el('span', { class: 'eyebrow' }, t('channel')),
        el('h1', {}, profile.name),
        subscribersNode(profile.subscribers ?? null),
        el('p', {}, `${profile.videoCount ?? videos.length} ${t('count')}`)),
      followButton(id));
  };
  if (author) drawHeader({ name: author.channel, avatar: author.avatar, subscribers: author.subscribers });
  else header.append(loadingNode(t('loading')));
  loadProfile(id, signal).then(profile => {
    if (!signal.aborted) drawHeader(profile);
  }).catch(error => {
    if (!signal.aborted && !author) header.replaceChildren(el('p', {}, t(error.status === 404 ? 'userNotFound' : 'profileUnavailable')));
  });
  grid(main, videos.sort((a, b) => b.published.localeCompare(a.published)));
}
function publicUser(main, id) {
  const signal = pageController.signal;
  const videos = new Map([...allVideos(), ...searchState.videos, ...recentDiscussions.values()].map(video => [video.id, video]));
  const entries = [...videos.values()].flatMap(video =>
    (video.publicComments || []).filter(comment => comment.authorId === id).map(comment => ({ comment, video })));
  const header = el('section', { class: 'channel-banner user-banner' });
  const drawHeader = profile => header.replaceChildren(
    channelAvatar({ channel: profile.name, avatar: profile.avatar }, true),
    el('div', { class: 'channel-copy' },
      el('span', { class: 'eyebrow' }, t('profile')),
      el('h1', {}, profile.name),
      subscribersNode(profile.subscribers ?? null)),
    link(t('channel'), `#/channel/${id}`, { class: 'primary' }));
  if (entries.length) drawHeader({ name: entries[0].comment.author, avatar: entries[0].comment.avatar });
  else header.append(loadingNode(t('loading')));
  main.append(header);
  const refreshProfile = async () => {
    try {
      const profile = await loadProfile(id, signal);
      if (!signal.aborted) drawHeader(profile);
    } catch (error) {
      if (signal.aborted || entries.length) return;
      header.replaceChildren(el('p', {}, t(error.status === 404 ? 'userNotFound' : 'profileUnavailable')),
        button(t('commentsRetry'), refreshProfile));
    }
  };
  refreshProfile();
  if (entries.length) main.append(
    el('h2', {}, t('userComments')),
    el('div', { class: 'user-comments' }, entries.map(({ comment, video }) =>
      el('article', { class: 'user-comment' }, el('p', {}, comment.text),
        el('div', { class: 'meta' }, relativeDate(comment.createdAt), ' · ', link(video.title, `#/watch/${video.id}`))))));
}
function accountPicture(account, className = 'avatar') {
  return account.avatar
    ? el('img', {
        class: className,
        src: account.avatar,
        alt: '',
        onerror: (event) => event.target.remove(),
      })
    : el(
        'span',
        { class: `${className} initials`, 'aria-hidden': true },
        account.name.trim().slice(0, 1).toUpperCase() || 'A',
      );
}

function editProfile(account) {
  const name = el('input', {
    value: account.name,
    required: true,
    maxlength: 80,
  });
  const bio = el('textarea', { rows: 3, maxlength: 500 }, account.bio);
  const photo = el('input', {
    type: 'file',
    accept: 'image/png,image/jpeg,image/webp',
  });
  let image = account.avatar;
  const preview = el('img', {
    class: 'profile-edit-avatar',
    src: image || 'favicon.svg',
    alt: '',
  });
  const error = el('p', { class: 'form-error', role: 'alert' });
  const submit = el('button', { type: 'submit', class: 'primary' }, t('save'));
  photo.addEventListener('change', async () => {
    if (!photo.files[0]) return;
    submit.disabled = true;
    try {
      image = await avatar(photo.files[0]);
      preview.src = image;
      error.textContent = '';
    } catch {
      error.textContent = t('avatarError');
    } finally {
      submit.disabled = false;
    }
  });
  const form = el(
    'form',
    {
      class: 'profile-form stack',
      onsubmit: (event) => {
        event.preventDefault();
        if (!name.value.trim() || user()?.id !== account.id) return;
        guard(() => {
          updateUser((a) => {
            a.name = name.value.trim();
            a.bio = bio.value;
            a.avatar = image;
          });
          controls.close();
          toast(t('saved'));
          render();
        });
      },
    },
    el('div', { class: 'profile-photo-editor' }, preview, field(t('avatar'), photo)),
    field(t('name'), name),
    field(t('bio'), bio),
    error,
    el(
      'div',
      { class: 'dialog-actions' },
      button(t('cancel'), () => controls.close()),
      submit,
    ),
  );
  const controls = modal(t('editProfile'), form);
}

function uploadVideo(account) {
  const file = el('input', { type: 'file', accept: 'video/*', required: true });
  const title = el('input', { required: true, maxlength: 200 });
  const description = el('textarea', { rows: 4, maxlength: 2000 });
  const error = el('p', { class: 'form-error', role: 'alert' });
  const submit = el('button', { type: 'submit', class: 'primary' }, t('publish'));
  file.addEventListener('change', () => {
    if (!title.value && file.files[0])
      title.value = file.files[0].name.replace(/\.[^.]+$/, '').slice(0, 200);
    error.textContent = '';
  });
  const form = el(
    'form',
    {
      class: 'stack upload-form',
      onsubmit: async (event) => {
        event.preventDefault();
        if (!file.files[0] || !title.value.trim()) {
          error.textContent = t('videoError');
          return;
        }
        submit.disabled = true;
        submit.replaceChildren(loadingNode(t('uploading')));
        try {
          if (user()?.id !== account.id) throw new Error('videoError');
          await saveVideo(account.id, file.files[0], title.value, description.value);
          controls.close();
          toast(t('uploaded'));
          render();
        } catch (failure) {
          error.textContent = t(
            failure?.message === 'videoError' ? 'videoError' : 'videoStorageError',
          );
        } finally {
          submit.disabled = false;
          submit.textContent = t('publish');
        }
      },
    },
    el('p', { class: 'upload-hint' }, t('uploadHint')),
    field(t('selectVideo'), file),
    field(t('videoTitle'), title),
    field(t('videoDescription'), description),
    error,
    el(
      'div',
      { class: 'dialog-actions' },
      button(t('cancel'), () => controls.close()),
      submit,
    ),
  );
  const controls = modal(t('uploadVideo'), form);
}

function uploadedVideoCard(video, account) {
  const details = uploadedVideo(video, account);
  const remove = button(
    '×',
    () =>
      confirmAction(t('confirmVideoDelete'), () => {
        removeVideo(account.id, video.id)
          .then(() => {
            let cleanupFailed = false;
            if (user()?.id === account.id) {
              try {
                updateUser((current) => {
                  current.library.likes = current.library.likes.filter(
                    (id) => id !== video.id,
                  );
                  current.library.later = current.library.later.filter(
                    (id) => id !== video.id,
                  );
                  current.library.comments = current.library.comments.filter(
                    (comment) => comment.videoId !== video.id,
                  );
                  delete current.library.history[video.id];
                  delete current.library.notes[video.id];
                });
              } catch {
                cleanupFailed = true;
              }
            }
            toast(t(cleanupFailed ? 'storageError' : 'saved'));
            if (user()?.id === account.id && route().parts[0] === 'profile') render();
          })
          .catch(() => toast(t('videoStorageError')));
      }),
    { class: 'uploaded-delete', 'aria-label': `${t('deleteVideo')}: ${video.title}` },
  );
  return el(
    'article',
    { class: 'video-card uploaded-card' },
    el(
      'div',
      { class: 'thumb-wrap' },
      link(
        [
          video.poster ? el('img', { src: video.poster, alt: '' }) : null,
          el('span', { class: 'thumb-play', 'aria-hidden': true }, '▶'),
          video.duration
            ? el('span', { class: 'duration' }, duration(video.duration))
            : null,
        ],
        `#/watch/${video.id}`,
        { class: 'thumb uploaded-thumb', 'aria-label': `${t('watch')}: ${video.title}` },
      ),
      remove,
    ),
    el(
      'div',
      { class: 'card-body' },
      channelAvatar(details),
      el(
        'div',
        { class: 'card-copy' },
        el('h3', {}, link(video.title, `#/watch/${video.id}`)),
        link(account.name, '#/profile', { class: 'channel-link' }),
        el(
          'p',
          { class: 'meta' },
          `${new Intl.NumberFormat(locale(), { notation: 'compact' }).format(video.views || 0)} ${t('views')} · ${date(video.createdAt)}`,
        ),
      ),
    ),
  );
}

async function fillProfileVideos(container, counter, account) {
  try {
    const videos = await videosFor(account.id);
    if (!container.isConnected || user()?.id !== account.id) return;
    counter.textContent = String(videos.length);
    container.replaceChildren(
      videos.length
        ? el(
            'div',
            { class: 'video-grid profile-video-grid' },
            videos.map((video) => uploadedVideoCard(video, account)),
          )
        : el(
            'div',
            { class: 'profile-empty' },
            el('span', { 'aria-hidden': true }, '▷'),
            el('h3', {}, t('noUploadedVideos')),
            el('p', {}, t('noUploadedHint')),
            button(t('uploadVideo'), () => uploadVideo(account), { class: 'primary' }),
          ),
    );
  } catch {
    if (container.isConnected)
      container.replaceChildren(el('p', { class: 'form-error' }, t('videoStorageError')));
  }
}

function profile(main) {
  const account = user();
  const count = el('strong', {}, '0');
  const videos = el(
    'section',
    { class: 'profile-videos', 'aria-labelledby': 'profile-videos-title' },
    el(
      'div',
      { class: 'profile-section-head' },
      el('h2', { id: 'profile-videos-title' }, t('profileVideos')),
      button(t('uploadVideo'), () => uploadVideo(account), { class: 'primary' }),
    ),
    el('div', { class: 'profile-videos-loading', role: 'status' }, loadingNode(t('loading'))),
  );
  main.append(
    el(
      'section',
      { class: 'profile-channel' },
      el('div', { class: 'profile-cover', 'aria-hidden': true }, el('span', {}, 'ASTER')),
      el(
        'div',
        { class: 'profile-channel-body' },
        accountPicture(account, 'profile-channel-avatar'),
        el(
          'div',
          { class: 'profile-channel-copy' },
          el('span', { class: 'eyebrow' }, t('yourChannel')),
          el('h1', {}, account.name),
          el('p', { class: 'profile-handle' }, account.email),
          el('p', { class: 'profile-stats' }, count, ` ${t('videos')}`),
          account.bio ? el('p', { class: 'profile-bio' }, account.bio) : null,
        ),
        el(
          'div',
          { class: 'profile-channel-actions' },
          button(t('editProfile'), () => editProfile(account)),
          button(t('logout'), () =>
            confirmAction(t('confirmLogout'), () => {
              signOut();
              render();
            }),
          ),
        ),
      ),
    ),
    videos,
  );
  fillProfileVideos(videos.lastElementChild, count, account);
}
function collection(main, kind) {
  heading(
    main,
    t(kind),
    kind === 'history'
      ? [
          button(t('clearHistory'), () =>
            confirmAction(t('confirmDelete'), () => {
              updateUser((a) => {
                a.library.history = {};
              });
              render();
            }),
          ),
        ]
      : [],
  );
  let videos = allVideos();
  if (kind === 'subscriptions') {
    const follows = library().follows;
    const requested = route().params.get('channel') || '';
    const selected = follows.includes(requested) ? requested : '';
    const channels = [
      ...new Map(
        videos
          .filter((v) => follows.includes(v.channelId))
          .map((v) => [v.channelId, v]),
      ).values(),
    ];
    main.append(
      el(
        'div',
        { class: 'chips channel-filters', 'aria-label': t('channels') },
        button(t('allChannels'), () => go('/subscriptions'), {
          'aria-pressed': String(!selected),
        }),
        channels.map((v) =>
          button(
            v.channel,
            () => go(`/subscriptions?channel=${encodeURIComponent(v.channelId)}`),
            { 'aria-pressed': String(selected === v.channelId) },
          ),
        ),
      ),
    );
    videos = videos.filter((v) => follows.includes(v.channelId));
    if (selected && follows.includes(selected))
      videos = videos.filter((v) => v.channelId === selected);
  } else if (kind === 'history')
    videos = videos
      .filter((v) => library().history[v.id])
      .sort((a, b) => library().history[b.id].at - library().history[a.id].at);
  else videos = videos.filter((v) => library()[kind].includes(v.id));
  if (!videos.length) { main.append(emptyPrompt(({ subscriptions: 'following', history: 'historyVideo', likes: 'likedVideos', later: 'later' })[kind], () => go('/home'))); return; }
  grid(main, videos, true, '', 24, kind === 'subscriptions' ? 'moreVideos' : 'more');
}
function render() {
  pageController.abort();
  pageController = new AbortController();
  disposePlayer();
  disposePlayer = () => {};
  const main = shell();
  const { parts, params } = route();
  const [page, id] = parts;
  const pageTitle = page === 'watch' ? findVideo(id)?.title || t('watch')
    : t(navigation.includes(page) ? page : page === 'channel' ? 'channel' : page === 'user' ? 'profile' : 'home');
  document.title = `Aster — ${pageTitle}`;
  if (
    [
      'profile',
      'subscriptions',
      'history',
      'likes',
      'later',
    ].includes(page) &&
    !user()
  ) {
    heading(main, t(page));
    main.append(guestPrompt(({ subscriptions: 'subscriptions', history: 'historyVideo', likes: 'likedVideos', later: 'later' })[page] || 'profile', () => auth()));
    return;
  }
  if (page === 'profile') { profile(main); return; }
  if (loading) {
    heading(main, pageTitle);
    main.append(
      el('div', { class: 'loading', role: 'status' }, loadingNode(t('loading'))),
      el(
        'div',
        { class: 'skeleton-grid', 'aria-hidden': true },
        Array.from({ length: 6 }, () => el('div')),
      ),
    );
    return;
  }
  if (failed) {
    main.append(
      el('p', { role: 'alert' }, t('unavailable')),
      button(t('retry'), initialize),
    );
    return;
  }
  if (page === 'watch') {
    const known = findVideo(id);
    if (known) watch(main, id);
    else watchUploaded(main, id);
  }
  else if (page === 'channel') channel(main, id);
  else if (page === 'user') publicUser(main, id);
  else if (['subscriptions', 'history', 'likes', 'later'].includes(page))
    collection(main, page);
  else if (page === 'catalog') {
    heading(
      main,
      params.get('q') ? `${t('results')}: ${params.get('q')}` : t('catalog'),
    );
    filters(main);
    if (params.get('q')) searchResults(main, params.get('q'));
    else grid(main, filtered(allVideos()), true, '', 24, 'moreVideos');
  } else home(main);
}
async function initialize() {
  request?.abort();
  request = new AbortController();
  const active = request;
  loading = true;
  failed = false;
  render();
  try {
    catalog = await loadCatalog(active.signal);
  } catch {
    if (!active.signal.aborted) failed = true;
  } finally {
    if (!active.signal.aborted) {
      loading = false;
      render();
    }
  }
}
window.addEventListener('hashchange', () => {
  render();
  window.scrollTo(0, 0);
  document.querySelector('main')?.focus({ preventScroll: true });
});
window.addEventListener('aster:state', render);
initialize();
