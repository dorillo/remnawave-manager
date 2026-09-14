import { el, button } from './aster-ui.js';
import { t } from './aster-i18n.js';
import { validId } from './aster-data.js';

export function mountPlayer(
  container,
  video,
  position,
  onProgress,
  onMetadata,
) {
  if (!validId(video.id)) throw new Error('Invalid video ID');
  let frame,
    timer,
    disposed = false,
    current = Math.max(0, Number(position) || 0),
    playing = false,
    completed = false,
    lastSaved = 0;
  const status = el(
    'p',
    { class: 'player-status', role: 'status' },
    t('loading'),
  );
  const retry = button(t('retry'), start);
  const actions = el(
    'div',
    { class: 'player-links', hidden: true },
    retry,
  );
  const screen = el('div', { class: 'screen' });
  container.append(screen, status, actions);
  function start() {
    clearTimeout(timer);
    frame?.remove();
    playing = false;
    completed = false;
    actions.hidden = true;
    status.textContent = t('loading');
    status.hidden = false;
    const url = new URL(`https://rutube.ru/play/embed/${video.id}/`);
    url.searchParams.set('t', String(Math.floor(current)));
    url.searchParams.set('getPlayOptions', 'title,thumbnail_url,duration');
    frame = el('iframe', {
      src: url.href,
      title: video.title,
      allow: 'autoplay; fullscreen; encrypted-media; picture-in-picture',
      allowfullscreen: true,
      referrerpolicy: 'strict-origin-when-cross-origin',
    });
    screen.replaceChildren(frame);
    timer = setTimeout(() => {
      status.hidden = false;
      status.textContent = t('playerError');
      actions.hidden = false;
    }, 20000);
  }
  function save(force = false) {
    if (playing && (force || Date.now() - lastSaved > 10000)) {
      lastSaved = Date.now();
      onProgress(current, completed);
    }
  }
  function message(event) {
    if (
      disposed ||
      event.origin !== 'https://rutube.ru' ||
      event.source !== frame?.contentWindow
    )
      return;
    let value;
    try {
      value =
        typeof event.data === 'string' ? JSON.parse(event.data) : event.data;
    } catch {
      return;
    }
    if (
      !value ||
      typeof value.type !== 'string' ||
      !value.data ||
      typeof value.data !== 'object'
    )
      return;
    if (
      [
        'player:ready',
        'player:init',
        'player:playStart',
        'player:changeState',
        'player:currentTime',
        'player:playOptionsLoaded',
      ].includes(value.type)
    ) {
      clearTimeout(timer);
      status.hidden = true;
      actions.hidden = true;
    }
    if (
      value.type === 'player:playStart' ||
      (value.type === 'player:changeState' && value.data.state === 'playing')
    ) {
      playing = true;
      save(true);
    }
    if (
      value.type === 'player:currentTime' &&
      typeof value.data.time === 'number' &&
      Number.isFinite(value.data.time) &&
      value.data.time >= 0 &&
      value.data.time <= 86400
    ) {
      current = value.data.time;
      save();
    }
    if (value.type === 'player:playComplete') {
      completed = true;
      save(true);
    }
    if (value.type === 'player:changeState' && value.data.state === 'paused')
      save(true);
    if (
      ['player:playOptionsLoaded', 'player:playOptionLoaded'].includes(
        value.type,
      )
    )
      onMetadata?.(value.data);
    if (value.type === 'player:error') {
      clearTimeout(timer);
      status.hidden = false;
      status.textContent = t('playerError');
      actions.hidden = false;
    }
  }
  const visibility = () => {
    if (document.hidden) save(true);
  };
  const pagehide = () => save(true);
  window.addEventListener('message', message);
  window.addEventListener('pagehide', pagehide);
  document.addEventListener('visibilitychange', visibility);
  start();
  return () => {
    save(true);
    disposed = true;
    clearTimeout(timer);
    window.removeEventListener('message', message);
    window.removeEventListener('pagehide', pagehide);
    document.removeEventListener('visibilitychange', visibility);
    frame?.remove();
  };
}

export function mountUploadedPlayer(container, video, position, onProgress) {
  const source = URL.createObjectURL(video.file);
  const player = el('video', {
    src: source,
    controls: true,
    playsinline: true,
    preload: 'metadata',
  });
  const screen = el('div', { class: 'screen local-screen' }, player);
  let lastSaved = 0;
  let disposed = false;
  const save = (force = false, complete = false) => {
    if (
      disposed ||
      (!force && Date.now() - lastSaved < 10000) ||
      !Number.isFinite(player.currentTime)
    )
      return;
    lastSaved = Date.now();
    onProgress(player.currentTime, complete);
  };
  player.addEventListener('loadedmetadata', () => {
    const selected = Math.max(0, Number(position) || 0);
    if (selected && Number.isFinite(player.duration))
      player.currentTime = Math.min(selected, Math.max(0, player.duration - 0.25));
  });
  player.addEventListener('timeupdate', () => save());
  player.addEventListener('pause', () => save(true));
  player.addEventListener('ended', () => save(true, true));
  container.append(screen);
  return () => {
    save(true, player.ended);
    disposed = true;
    player.pause();
    player.removeAttribute('src');
    player.load();
    URL.revokeObjectURL(source);
  };
}
