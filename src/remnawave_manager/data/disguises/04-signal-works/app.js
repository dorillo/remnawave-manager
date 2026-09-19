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
import { feed, cachedFeed, search, spaces, detail, safeImage } from './answers-data.js';
import { read, change, newId, watch } from './answers-store.js';
import { currentUser, register, login, logout } from './answers-auth.js';
import { t, lang, setLang, dateText } from './answers-i18n.js';

const root = document.querySelector('#app');
const toastNode = document.querySelector('#notice');
let state = { accounts: [], questions: [], answers: [], comments: [], votes: {}, saves: {} };
let publicFeed = [];
let cursor = null;
let feedStatus = 'loading';
let staleAt = null;
let spaceList = [];
let searchItems = [];
let searchStatus = '';
const spaceFeeds = new Map();
let details = new Map();
let detailStatus = new Map();
let modal = '';
let modalPayload = null;
let returnFocus = null;
let replyTarget = null;
let requestToken = 0;
let busy = false;
let renderedViewKey = '';
let routeIndex = Number.isSafeInteger(history.state?.answersRouteIndex) ? history.state.answersRouteIndex : 0;
let pendingRouteIndex = null;
const REMOTE_PROFILES = 'answers:remote-profiles:v1';
const remoteProfiles = new Map();
const e = (value) => String(value ?? '').replace(/[&<>"']/g, (x) => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' })[x]);
function qurl(id) {
  if (typeof id !== 'string') return '#/home';
  if (/^mail:\d{1,12}$/.test(id)) return `#/question/mail/${id.slice(5)}`;
  if (/^local:[0-9a-f-]{36}$/.test(id)) return `#/question/local/${id.slice(6)}`;
  return '#/home';
}
const accountName = (id) => state.accounts.find((x) => x.id === id)?.name || (lang() === 'ru' ? 'Удалённый профиль' : 'Deleted profile');
const user = () => currentUser(state);
const backLink = (fallback = '#/home') => `<button class="back-link" type="button" data-action="back" data-fallback="${e(fallback)}">← ${e(t('back'))}</button>`;
function rememberRemoteProfile(person) {
  if (!Number.isSafeInteger(person?.id) || person.id < 1 || typeof person?.name !== 'string') return;
  remoteProfiles.set(String(person.id),{id:person.id,name:person.name.slice(0,80),avatar:typeof person.avatar==='string'?person.avatar:''});
  try { localStorage.setItem(REMOTE_PROFILES,JSON.stringify([...remoteProfiles.values()].slice(-120))); } catch {}
}
function restoreRemoteProfiles() {
  try { for (const person of JSON.parse(localStorage.getItem(REMOTE_PROFILES)||'[]')) if (Number.isSafeInteger(person?.id) && person.id>0 && typeof person.name==='string') remoteProfiles.set(String(person.id),{id:person.id,name:person.name.slice(0,80),avatar:typeof person.avatar==='string'?person.avatar:''}); } catch {}
}
const iconPaths = { appearance:'M21 13a9 9 0 0 1-10-10A9 9 0 1 0 21 13Z', globe:'M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0M3 12h18M12 3a18 18 0 0 1 0 18 18 18 0 0 1 0-18', home:'M3 10.8 12 3l9 7.8v9.7a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 20.5zM9 22v-6h6v6', file:'M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9zM14 3v6h6', bookmark:'M6 3h12a1 1 0 0 1 1 1v17l-7-4-7 4V4a1 1 0 0 1 1-1z', user:'M20 21a8 8 0 0 0-16 0M12 13a5 5 0 1 0 0-10 5 5 0 0 0 0 10z', plus:'M12 5v14M5 12h14', search:'m21 21-4.4-4.4M19 11a8 8 0 1 1-16 0 8 8 0 0 1 16 0z', arrow:'M5 12h14M13 6l6 6-6 6', up:'M12 4 4 12h5v8h6v-8h5z', down:'M12 20 4 12h5V4h6v8h5z', comment:'M21 11.5a8 8 0 0 1-8.5 8A9.7 9.7 0 0 1 8 18.4L3 20l1.6-4.3A8 8 0 1 1 21 11.5z', reply:'M9 8 4 12l5 4M4 12h9a7 7 0 0 1 7 7', edit:'M4 20h4l11-11a2.8 2.8 0 0 0-4-4L4 16zM13.5 6.5l4 4', trash:'M4 7h16M10 11v6M14 11v6M6 7l1 14h10l1-14M9 7V4h6v3', check:'m5 12 4 4L19 6', close:'M6 6l12 12M18 6 6 18', attach:'M21.4 11.6 12 21a6 6 0 0 1-8.5-8.5l9.2-9.2a4 4 0 1 1 5.7 5.7l-9.2 9.2a2 2 0 0 1-2.8-2.8l8.5-8.5' };
function icon(name) { return `<svg class="ui-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="${iconPaths[name] || iconPaths.file}"/></svg>`; }

function toast(message) {
  toastNode.textContent = message;
  toastNode.classList.add('show');
  clearTimeout(toastNode._timer);
  toastNode._timer = setTimeout(() => toastNode.classList.remove('show'), 3300);
}

function attachmentControl() {
  return `<label class="attachment-picker"><input name="attachments" type="file" multiple data-attachment-input>${icon('attach')}${e(t('attachFile'))}</label><span class="attachment-selection" data-attachment-selection aria-live="polite"></span><small class="attachment-hint">${e(t('attachmentHint'))}</small>`;
}
function storedAttachments(value) {
  return Array.isArray(value?.attachments) ? value.attachments.filter((item) => typeof item?.name === 'string' && item.name.length <= 160 && typeof item?.data === 'string' && item.data.startsWith('data:')).slice(0, 3) : [];
}
function attachments(item) {
  const files = storedAttachments(item);
  return files.length ? `<div class="attachment-list">${files.map((file) => /^data:image\/(?:png|jpeg|gif|webp|avif);base64,/i.test(file.data)
    ? `<a class="attachment-image" href="${e(file.data)}" download="${e(file.name)}"><img src="${e(file.data)}" alt="${e(file.name)}" loading="lazy"><span>${e(file.name)}</span></a>`
    : `<a class="attachment" href="${e(file.data)}" download="${e(file.name)}"><span aria-hidden="true">⌁</span>${e(file.name)}</a>`).join('')}</div>` : '';
}
async function selectedAttachments(form) {
  const files = [...(form.elements.attachments?.files || [])];
  if (files.length > 3) throw new Error('attachment');
  return Promise.all(files.map((file) => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => typeof reader.result === 'string' ? resolve({ name:file.name.slice(0,160) || 'file', data:reader.result }) : reject(new Error('attachment'));
    reader.onerror = () => reject(new Error('attachment'));
    reader.readAsDataURL(file);
  })));
}
async function profileImage(file) {
  if (!file || !['image/jpeg','image/png','image/webp'].includes(file.type) || file.size > 5000000) throw new Error('avatar');
  const bitmap = await createImageBitmap(file);
  try {
    const canvas = document.createElement('canvas');
    const size = Math.min(bitmap.width, bitmap.height);
    canvas.width = 256; canvas.height = 256;
    canvas.getContext('2d').drawImage(bitmap,(bitmap.width-size)/2,(bitmap.height-size)/2,size,size,0,0,256,256);
    return canvas.toDataURL('image/jpeg',.82);
  } finally { bitmap.close(); }
}

function route() {
  const hash = location.hash.slice(1) || '/home';
  if (hash.startsWith('/question/mail/')) { const id = hash.slice(15); return /^\d{1,12}$/.test(id) ? { page:'question', id:`mail:${id}` } : { page:'home' }; }
  if (hash.startsWith('/question/local/')) { const id = hash.slice(16); return /^[0-9a-f-]{36}$/.test(id) ? { page:'question', id:`local:${id}` } : { page:'home' }; }
  if (hash.startsWith('/search/')) { let term = ''; try { term = decodeURIComponent(hash.slice(8)); } catch {} return { page:'search', term:term.slice(0,120) }; }
  if (hash.startsWith('/space/')) { const slug = hash.slice(7); return /^[a-z0-9_-]{1,70}$/.test(slug) ? { page:'space', slug } : { page:'home' }; }
  if (hash.startsWith('/profile/')) { const tab=hash.slice(9); return ['questions','answers','notifications'].includes(tab) ? { page:'profile', tab } : { page:'profile', tab:'questions' }; }
  if (hash.startsWith('/user/local/')) { const parts=hash.slice(12).split('/'); return /^[0-9a-f-]{36}$/.test(parts[0]) ? { page:'user', source:'local', id:parts[0], tab:parts[1]==='answers'?'answers':'questions' } : { page:'home' }; }
  if (hash.startsWith('/user/mail/')) { const parts=hash.slice(11).split('/'); return /^\d{1,12}$/.test(parts[0]) ? { page:'user', source:'mail', id:Number(parts[0]), tab:parts[1]==='answers'?'answers':'questions' } : { page:'home' }; }
  if (['/home','/local','/saved','/profile'].includes(hash)) return { page:hash.slice(1) };
  return { page:'home' };
}

function avatar(person, name) {
  if (person?.avatar) return `<img class="author-avatar" src="${e(safeImage(person.avatar))}" alt="" loading="lazy">`;
  return `<span class="author-avatar avatar-small" aria-hidden="true">${e((name || '?').slice(0,1).toUpperCase())}</span>`;
}
function profileAvatar(person, className = 'profile-avatar') {
  return person?.avatar ? `<img class="${className}" src="${e(safeImage(person.avatar))}" alt="">` : `<div class="${className}" aria-hidden="true">${e((person?.name || '?').slice(0,1).toUpperCase())}</div>`;
}
function navLink(path, name, label, active) { return `<a class="nav-link ${active ? 'active' : ''}" href="#/${path}"><span class="nav-icon">${icon(name)}</span>${e(label)}</a>`; }
function mobileLink(path, name, label, active) { return `<a class="${active ? 'active' : ''}" href="#/${path}"><span class="nav-icon">${icon(name)}</span>${e(label)}</a>`; }
function shell(page, content) {
  const person = user();
  const nav = [['home','home',t('home')],['local','file',t('local')],['saved','bookmark',t('saved')],['profile','user',t('profile')]];
  const spacesHtml = spaceList.slice(0,8).map((x) => `<a class="space-link" href="#/space/${e(x.path)}">${e(x.name)}</a>`).join('');
  root.innerHTML = `<header class="site-head"><div class="head-inner">
    <a class="brand" href="#/home" aria-label="Spros"><span class="brand-mark">?</span><span class="brand-name">Spr<b>os</b></span></a>
    <form class="head-search" data-form="search">${icon('search')}<input name="term" type="search" placeholder="${e(t('search'))}" maxlength="120" aria-label="${e(t('search'))}" value="${page.page === 'search' ? e(page.term) : ''}"><button aria-label="${e(t('searchButton'))}" title="${e(t('searchButton'))}">${icon('arrow')}</button></form>
    <div class="head-actions"><button class="icon-btn" data-action="language" aria-label="${e(t('language'))}">${lang() === 'ru' ? 'EN' : 'RU'}</button><button class="icon-btn" data-action="theme" aria-label="${lang()==='ru'?'Сменить тему':'Switch theme'}">${icon('appearance')}</button>
    ${person ? `<button class="ghost" data-action="profile" aria-label="${e(t('profile'))}">${e(person.name.slice(0,12))}</button>` : `<button class="ghost" data-action="login">${e(t('login'))}</button>`}</div></div></header>
    <div class="layout"><aside class="side"><nav class="nav-box" aria-label="Navigation">${nav.map(([path,icon,label]) => navLink(path,icon,label,page.page===path)).join('')}</nav><button class="primary side-ask" data-action="ask">${icon('plus')}${e(t('ask'))}</button><h2 class="side-label">${e(t('explore'))}</h2>${spacesHtml}<div class="side-foot">© ${new Date().getFullYear()} Spros</div></aside>
    <main class="main" id="main">${content}</main></div>
    <nav class="mobile-nav" aria-label="Mobile navigation">${nav.map(([path,icon,label]) => mobileLink(path,icon,label,page.page===path)).join('')}</nav>${modalHtml()}`;
  if (modal) root.querySelector('.modal [autofocus]')?.focus();
}

function meta(item, local = false) {
  const person = local ? state.accounts.find((x) => x.id === item.authorId) : item.author;
  if (!local) rememberRemoteProfile(person);
  const name = local ? accountName(item.authorId) : person?.name || '';
  const href = local && person ? (person.id===user()?.id ? '#/profile' : `#/user/local/${e(person.id)}`) : !local && person?.id ? `#/user/mail/${e(person.id)}` : '';
  const identity = `${avatar(person,name)}<span class="author">${e(name)}</span>`;
  return `<div class="card-meta">${href ? `<a class="author-link" href="${href}">${identity}</a>` : identity}<span>·</span><time>${e(dateText(item.created))}</time></div>`;
}
function questionCard(item, local = false) {
  const person = user();
  const saved = person && (state.saves[person.id] || []).includes(item.id);
  const currentVote = person ? state.votes[person.id]?.[item.id] || 0 : 0;
  return `<article class="card question-card">${meta(item,local)}<h3><a href="${qurl(item.id)}">${e(item.title)}</a></h3><p class="excerpt">${e(item.body || '')}</p><div class="tag-list">${(local ? [item.space] : item.spaces.map((x) => x.name)).filter(Boolean).slice(0,3).map((x) => `<span class="tag">${e(x)}</span>`).join('')}</div><div class="card-footer"><span class="reply-count">${icon('comment')}${local ? state.answers.filter((a) => a.questionId === item.id).length : item.replies}</span><div class="icon-actions"><button class="icon-action ${currentVote===1?'selected':''}" data-action="vote" data-id="${e(item.id)}" data-value="1" aria-label="${e(t('voteUp'))}" title="${e(t('voteUp'))}">${icon('up')}</button><button class="icon-action ${currentVote===-1?'selected':''}" data-action="vote" data-id="${e(item.id)}" data-value="-1" aria-label="${e(t('voteDown'))}" title="${e(t('voteDown'))}">${icon('down')}</button><button class="icon-action ${saved?'selected':''}" data-action="save" data-id="${e(item.id)}" aria-label="${e(saved?t('unsave'):t('save'))}" title="${e(saved?t('unsave'):t('save'))}">${icon('bookmark')}</button></div></div></article>`;
}
function list(items, local = false) { return items.length ? items.map((x) => questionCard(x,local)).join('') : `<div class="empty">${e(t('noQuestions'))}</div>`; }
function renderHome() {
  const status = feedStatus === 'loading' && !publicFeed.length ? `<div class="empty">${e(t('loading'))}</div>` : '';
  const note = staleAt ? `<div class="notice-inline">${e(t('cached'))} · ${e(dateText(staleAt))}</div>` : '';
  const cards = publicFeed.length ? publicFeed.map((x) => questionCard(x)).join('') : feedStatus === 'error' ? `<div class="empty"><h3>${e(t('network'))}</h3><button class="ghost" data-action="refresh">${e(t('retry'))}</button></div>` : feedStatus === 'ready' ? `<div class="empty">${e(t('noQuestions'))}</div>` : '';
  const more = cursor ? `<div class="load-row">${feedStatus==='loading' ? loadingMore() : `<button class="ghost" data-action="more">${e(t('loadMore'))}</button>`}</div>` : '';
  return `<section class="hero"><span class="eyebrow">${lang()==='ru'?'СООБЩЕСТВО ВОПРОСОВ':'QUESTION COMMUNITY'}</span><h1>${lang()==='ru'?'Любой вопрос — начало разговора.':'Every question starts a conversation.'}</h1><p>${lang()==='ru'?'Читайте ответы, находите нужное и обсуждайте на своём устройстве.':'Explore answers and start discussions on your device.'}</p></section><div class="section-title"><h2>${e(t('latest'))}</h2><small>${publicFeed.length || ''}</small></div><div class="tabs"><button class="tab active">${e(t('all'))}</button>${spaceList.slice(0,5).map((x) => `<a class="tab" href="#/space/${e(x.path)}">${e(x.name)}</a>`).join('')}</div>${note}${status}${cards}${more}`;
}
function renderSpace(slug) {
  const name = spaceList.find((x)=>x.path===slug)?.name || slug;
  const result = spaceFeeds.get(slug);
  const posts = result?.items || [];
  const more = result?.pos ? `<div class="load-row">${result.status==='loading' ? loadingMore() : `<button class="ghost" data-action="more-space">${e(t('loadMore'))}</button>`}</div>` : '';
  return `${backLink()}<div class="section-title"><h2>${e(name)}</h2></div><div class="tabs"><a class="tab" href="#/home">${e(t('all'))}</a><span class="tab active">${e(name)}</span></div>${result?.status==='error' ? `<div class="notice-inline">${e(t('network'))}<button class="action" data-action="retry-space">${e(t('retry'))}</button></div>` : ''}${result?.status==='loading' && !posts.length ? `<div class="empty">${e(t('loading'))}</div>` : posts.length ? posts.map((x)=>questionCard(x)).join('') : result?.status==='ready' ? `<div class="empty">${e(t('noQuestions'))}</div>` : ''}${more}`;
}
function loadingMore() { return `<span class="load-indicator" role="status"><span class="loading-spinner" aria-hidden="true"></span>${e(t('loadingMore'))}</span>`; }
function renderLocal() {
  const items = [...state.questions].sort((a,b) => Date.parse(b.created)-Date.parse(a.created));
  return `<div class="section-title"><h2>${e(t('myQuestions'))}</h2><button class="primary" data-action="ask">＋ ${e(t('ask'))}</button></div>${list(items,true)}`;
}
function renderSaved() {
  const person = user();
  if (!person) return locked();
  const ids = state.saves[person.id] || [];
  const items = ids.map((id) => state.questions.find((x) => x.id === id) || publicFeed.find((x) => x.id === id) || details.get(id)?.question || state.snapshots?.[id]).filter(Boolean);
  return `<div class="section-title"><h2>${e(t('saved'))}</h2></div>${items.length ? items.map((x)=>questionCard(x,x.id.startsWith('local:'))).join('') : `<div class="empty">${e(t('noQuestions'))}</div>`}`;
}
function locked() { return `<div class="empty"><h3>${e(t('authNeeded'))}</h3><button class="primary" data-action="login">${e(t('login'))}</button></div>`; }
function renderSearch(term) {
  let items = searchItems;
  const local = state.questions.filter((x) => `${x.title} ${x.body}`.toLowerCase().includes(term.toLowerCase()));
  const note = searchStatus === 'error' ? `<div class="notice-inline">${e(t('network'))}</div>` : '';
  return `<div class="section-title"><h2>${e(t('searchButton'))}: ${e(term)}</h2></div>${note}${searchStatus === 'loading' ? `<div class="empty">${e(t('loading'))}</div>` : items.length || local.length ? `${items.map((x) => questionCard(x)).join('')}${local.map((x) => questionCard(x,true)).join('')}` : `<div class="empty">${e(t('noResults'))}</div>`}`;
}
function commentTree(parentId, depth = 0) {
  if (depth > 8) return '';
  const comments = state.comments.filter((x) => x.parentId === parentId).sort((a,b) => Date.parse(a.created)-Date.parse(b.created));
  if (!comments.length) return '';
  return `<div class="thread">${comments.map((x) => `<div class="comment-item">${meta(x,true)}<p>${e(x.body)}</p>${attachments(x)}<div class="detail-actions icon-actions">${depth < 8 ? `<button class="icon-action" data-action="comment" data-parent="${e(x.id)}" aria-label="${e(t('reply'))}" title="${e(t('reply'))}">${icon('reply')}</button>`:''}${user()?.id===x.authorId ? `<button class="icon-action" data-action="edit" data-kind="comment" data-id="${e(x.id)}" aria-label="${e(t('edit'))}" title="${e(t('edit'))}">${icon('edit')}</button><button class="icon-action danger" data-action="delete" data-kind="comment" data-id="${e(x.id)}" aria-label="${e(t('remove'))}" title="${e(t('remove'))}">${icon('trash')}</button>`:''}</div>${inlineReplyForm(x.id, x.questionId)}${commentTree(x.id,depth+1)}</div>`).join('')}</div>`;
}
function inlineReplyForm(parentId, questionId) {
  if (!user() || replyTarget?.parentId !== parentId) return '';
  return `<form class="inline-reply" data-form="comment" data-question="${e(questionId)}" data-parent="${e(parentId)}"><label class="field">${e(t('writeComment'))}<textarea name="body" required minlength="2" maxlength="5000" rows="3" autofocus></textarea></label>${attachmentControl()}<div class="inline-reply-actions"><button class="primary">${e(t('publish'))}</button><button class="ghost" type="button" data-action="cancel-reply">${e(t('cancel'))}</button></div><p class="form-error" aria-live="polite"></p></form>`;
}
function answerCard(answer, question, local) {
  const person = user();
  const currentVote = person ? state.votes[person.id]?.[answer.id] || 0 : 0;
  const best = local && question.bestId === answer.id;
  return `<article class="card answer-card ${best?'best':''}">${meta(answer,local)}${best ? `<span class="local-badge">${e(t('best'))}</span>`:''}<div class="body-text">${e(answer.body)}</div>${media(answer)}${attachments(answer)}<div class="detail-actions icon-actions"><button class="icon-action ${currentVote===1?'selected':''}" data-action="vote" data-id="${e(answer.id)}" data-value="1" aria-label="${e(t('voteUp'))}" title="${e(t('voteUp'))}">${icon('up')}</button><button class="icon-action ${currentVote===-1?'selected':''}" data-action="vote" data-id="${e(answer.id)}" data-value="-1" aria-label="${e(t('voteDown'))}" title="${e(t('voteDown'))}">${icon('down')}</button><button class="icon-action" data-action="comment" data-parent="${e(answer.id)}" aria-label="${e(t('comment'))}" title="${e(t('comment'))}">${icon('comment')}</button>${local && person?.id===answer.authorId ? `<button class="icon-action" data-action="edit" data-kind="answer" data-id="${e(answer.id)}" aria-label="${e(t('edit'))}" title="${e(t('edit'))}">${icon('edit')}</button><button class="icon-action danger" data-action="delete" data-kind="answer" data-id="${e(answer.id)}" aria-label="${e(t('remove'))}" title="${e(t('remove'))}">${icon('trash')}</button>`:''}${question.id.startsWith('local:') && person?.id===question.authorId && local ? `<button class="icon-action" data-action="best" data-id="${e(answer.id)}" aria-label="${e(t('markBest'))}" title="${e(t('markBest'))}">${icon('check')}</button>`:''}</div>${inlineReplyForm(answer.id, question.id)}${commentTree(answer.id)}</article>`;
}
function media(item) {
  return Array.isArray(item.images) && item.images.length
    ? `<div class="media-grid">${item.images.slice(0,4).map((src)=>`<img src="${e(safeImage(src))}" alt="" loading="lazy">`).join('')}</div>`
    : '';
}
function renderDetail(page) {
  const local = page.id.startsWith('local:');
  const record = local ? { question:state.questions.find((x) => x.id===page.id), answers:[] } : details.get(page.id);
  if (!record?.question) return local ? `${backLink('#/local')}<div class="empty">${e(t('noQuestions'))}</div>` : detailStatus.get(page.id) === 'error' ? `${backLink()}<div class="empty"><h3>${e(t('network'))}</h3><button class="ghost" data-action="retry-detail">${e(t('retry'))}</button></div>` : `<div class="empty">${e(t('loading'))}</div>`;
  const question = record.question;
  const person = user();
  const answerList = [...record.answers.map((x) => ({ ...x, local:false })), ...state.answers.filter((x) => x.questionId === question.id).map((x) => ({ ...x, local:true }))];
  const currentVote = person ? state.votes[person.id]?.[question.id] || 0 : 0;
  const saved = person && (state.saves[person.id] || []).includes(question.id);
  return `${backLink(local?'#/local':'#/home')}<article class="card detail-card">${meta(question,local)}<h1>${e(question.title)}</h1><div class="tag-list">${(local?[question.space]:question.spaces.map((x)=>x.name)).filter(Boolean).slice(0,4).map((x)=>`<span class="tag">${e(x)}</span>`).join('')}</div><div class="body-text">${e(question.body)}</div>${media(question)}${attachments(question)}<div class="detail-actions icon-actions"><button class="icon-action ${currentVote===1?'selected':''}" data-action="vote" data-id="${e(question.id)}" data-value="1" aria-label="${e(t('voteUp'))}" title="${e(t('voteUp'))}">${icon('up')}</button><button class="icon-action ${currentVote===-1?'selected':''}" data-action="vote" data-id="${e(question.id)}" data-value="-1" aria-label="${e(t('voteDown'))}" title="${e(t('voteDown'))}">${icon('down')}</button><button class="icon-action ${saved?'selected':''}" data-action="save" data-id="${e(question.id)}" aria-label="${e(saved?t('unsave'):t('save'))}" title="${e(saved?t('unsave'):t('save'))}">${icon('bookmark')}</button>${local && person?.id === question.authorId ? `<button class="icon-action" data-action="edit" data-kind="question" data-id="${e(question.id)}" aria-label="${e(t('edit'))}" title="${e(t('edit'))}">${icon('edit')}</button><button class="icon-action danger" data-action="delete" data-kind="question" data-id="${e(question.id)}" aria-label="${e(t('remove'))}" title="${e(t('remove'))}">${icon('trash')}</button>` : ''}</div></article>${person ? `<form class="card composer" data-form="answer" data-question="${e(question.id)}"><h3>${e(t('answer'))}</h3><label class="field">${e(t('writeAnswer'))}<textarea name="body" required minlength="2" maxlength="5000" rows="5"></textarea></label>${attachmentControl()}<button class="primary">${e(t('publish'))}</button></form>` : locked()}<h2 class="answers-title">${answerList.length} ${e(t('replies'))}</h2>${answerList.length ? answerList.map((x) => answerCard(x,question,x.local)).join('') : `<div class="empty">${e(!local && record.answersAvailable === false ? t('responseUnavailable') : t('noAnswers'))}</div>`}`;
}
function notifications(person) {
  const myQuestions = new Set(state.questions.filter((x) => x.authorId === person.id).map((x) => x.id));
  return [...state.answers.filter((x) => myQuestions.has(x.questionId) && x.authorId !== person.id).map((x) => ({ id:x.id, date:x.created, questionId:x.questionId, who:x.authorId })), ...state.comments.filter((x) => { const answer = state.answers.find((a) => a.id === x.parentId); return answer?.authorId === person.id && x.authorId !== person.id; }).map((x) => ({ id:x.id,date:x.created,questionId:state.answers.find((a)=>a.id===x.parentId)?.questionId,who:x.authorId }))].sort((a,b)=>Date.parse(b.date)-Date.parse(a.date)).slice(0,20);
}
function profileTabs(base, tab, notificationsTab = false) {
  const tabs = [['questions',t('questions')],['answers',t('answers')],...(notificationsTab?[['notifications',t('notifications')]]:[])];
  return `<nav class="profile-tabs" aria-label="${e(t('profile'))}">${tabs.map(([key,label])=>`<a class="profile-tab ${tab===key?'active':''}" href="${base}/${key}" ${tab===key?'aria-current="page"':''}>${e(label)}</a>`).join('')}</nav>`;
}
function answerList(items) {
  return items.length ? items.map((x) => `<article class="card question-card profile-answer"><a href="${qurl(x.questionId)}"><strong>${e(x.body.slice(0,180))}</strong></a><div class="muted">${e(dateText(x.created))}</div></article>`).join('') : `<div class="empty">${e(t('noAnswers'))}</div>`;
}
function renderProfile(page) {
  const person = user(); if (!person) return locked();
  const tab = page.tab || 'questions';
  const myQuestions = state.questions.filter((x) => x.authorId===person.id);
  const myAnswers = state.answers.filter((x) => x.authorId===person.id);
  const alerts = notifications(person);
  const content = tab==='answers' ? answerList(myAnswers) : tab==='notifications' ? (alerts.length ? alerts.map((x)=>`<article class="card question-card notification-card"><a class="notification-avatar" href="#/user/local/${e(x.who)}" aria-label="${e(accountName(x.who))}">${avatar(state.accounts.find((a)=>a.id===x.who),accountName(x.who))}</a><div class="notification-copy"><a href="#/user/local/${e(x.who)}"><strong>${e(accountName(x.who))}</strong></a> <a href="${qurl(x.questionId)}">· ${e(t('answer'))}</a></div><div class="muted">${e(dateText(x.date))}</div></article>`).join('') : `<div class="empty">${e(t('noNotifications'))}</div>`) : list(myQuestions,true);
  return `<section class="card profile-card"><div class="profile-hero">${profileAvatar(person)}<div><h1>${e(person.name)}</h1><p>${e(person.email)}</p>${person.bio?`<div class="profile-bio">${e(person.bio)}</div>`:''}</div></div><div class="profile-actions"><button class="primary" data-action="edit-profile">${icon('edit')}${e(t('editProfile'))}</button><button class="ghost" data-action="logout">${e(t('logout'))}</button></div></section>${profileTabs('#/profile',tab,true)}${content}`;
}
function knownRemoteProfiles() {
  const questions = [...publicFeed,...searchItems,...[...spaceFeeds.values()].flatMap((x)=>x.items||[]),...[...details.values()].map((x)=>x.question),...Object.values(state.snapshots||{})].filter(Boolean);
  const answers = [...details.values()].flatMap((record)=>(record.answers||[]).map((x)=>({...x,questionId:record.question.id})));
  return { questions:[...new Map(questions.map((x)=>[x.id,x])).values()], answers:[...new Map(answers.map((x)=>[x.id,x])).values()] };
}
function renderUser(page) {
  const tab=page.tab||'questions';
  if(page.source==='local') {
    const person=state.accounts.find((x)=>x.id===page.id);
    if(!person) return `${backLink()}<div class="empty">${e(t('profileNotFound'))}</div>`;
    const questions=state.questions.filter((x)=>x.authorId===person.id);
    const answers=state.answers.filter((x)=>x.authorId===person.id);
    return `<div class="profile-page-head">${backLink()}</div><section class="card profile-card"><div class="profile-hero">${profileAvatar(person)}<div><h1>${e(person.name)}</h1>${person.bio?`<div class="profile-bio">${e(person.bio)}</div>`:''}</div></div></section>${profileTabs(`#/user/local/${e(person.id)}`,tab)}${tab==='answers'?answerList(answers):list(questions,true)}`;
  }
  const known=knownRemoteProfiles();
  const questions=known.questions.filter((x)=>x.author?.id===page.id);
  const answers=known.answers.filter((x)=>x.author?.id===page.id);
  const person=questions[0]?.author||answers[0]?.author||remoteProfiles.get(String(page.id));
  if(!person) return `<div class="profile-page-head">${backLink()}</div><div class="empty">${e(feedStatus==='loading'?t('loading'):t('profileNotFound'))}</div>`;
  return `<div class="profile-page-head">${backLink()}</div><section class="card profile-card"><div class="profile-hero">${profileAvatar(person)}<div><h1>${e(person.name)}</h1></div></div></section>${profileTabs(`#/user/mail/${e(page.id)}`,tab)}${tab==='answers'?answerList(answers):list(questions)}`;
}

function modalHtml() {
  if (!modal) return '';
  let content = '';
  if (modal === 'login' || modal === 'register') content = `<div class="modal-head"><h2>${e(t(modal))}</h2><button class="icon-btn" data-action="close" aria-label="${e(t('close'))}">×</button></div><form data-form="auth">${modal==='register'?`<label class="field">${e(t('name'))}<input name="name" autocomplete="name" required minlength="2" maxlength="60" autofocus></label>`:''}<label class="field">${e(t('email'))}<input name="email" type="email" autocomplete="email" required autofocus></label><label class="field">${e(t('password'))}<input name="password" type="password" autocomplete="${modal==='register'?'new-password':'current-password'}" required minlength="8" maxlength="128"></label><p class="form-error" aria-live="polite"></p><div class="modal-footer"><button class="primary">${e(t(modal==='register'?'register':'login'))}</button><button class="ghost" type="button" data-action="switch-auth">${e(t(modal==='register'?'login':'register'))}</button></div></form>`;
  if (modal === 'logout-confirm') content = `<div class="confirm-dialog"><div class="modal-head"><span></span><button class="icon-btn" data-action="close" aria-label="${e(t('close'))}">×</button></div><div class="confirm-icon">${icon('user')}</div><h2>${e(t('logoutConfirmTitle'))}</h2><p>${e(t('logoutConfirmText'))}</p><div class="modal-footer"><button class="primary" data-action="confirm-logout">${e(t('logout'))}</button><button class="ghost" type="button" data-action="close">${e(t('cancel'))}</button></div></div>`;
  if (modal === 'delete-confirm') content = `<div class="confirm-dialog danger-confirm"><div class="modal-head"><span></span><button class="icon-btn" data-action="close" aria-label="${e(t('close'))}">×</button></div><div class="confirm-icon">${icon('trash')}</div><h2>${e(t('deleteConfirmTitle'))}</h2><p>${e(t('deleteConfirmText'))}</p><div class="modal-footer"><button class="danger-button" data-action="confirm-delete">${e(t('remove'))}</button><button class="ghost" type="button" data-action="close">${e(t('cancel'))}</button></div></div>`;
  if (modal === 'ask' || modal === 'edit-question') { const item = modalPayload?.item; content = `<div class="modal-head"><h2>${e(modal==='ask'?t('ask'):t('edit'))}</h2><button class="icon-btn" data-action="close" aria-label="${e(t('close'))}">×</button></div><form data-form="question"><label class="field">${e(t('title'))}<input name="title" required minlength="5" maxlength="240" value="${e(item?.title||'')}" autofocus></label><label class="field">${e(t('details'))}<textarea name="body" maxlength="5000" rows="7">${e(item?.body||'')}</textarea></label><label class="field">${e(t('explore'))}<input name="space" maxlength="60" value="${e(item?.space||'')}"></label>${attachmentControl()}<p class="form-error" aria-live="polite"></p><div class="modal-footer"><button class="primary">${e(t('publish'))}</button><button type="button" class="ghost" data-action="close">${e(t('cancel'))}</button></div></form>`; }
  if (modal === 'edit-answer' || modal === 'edit-comment') { const item = modalPayload?.item; content = `<div class="modal-head"><h2>${e(t('edit'))}</h2><button class="icon-btn" data-action="close" aria-label="${e(t('close'))}">×</button></div><form data-form="text"><label class="field">${e(t('details'))}<textarea name="body" required minlength="2" maxlength="5000" rows="5" autofocus>${e(item?.body||'')}</textarea></label>${attachmentControl()}<p class="form-error" aria-live="polite"></p><div class="modal-footer"><button class="primary">${e(t('publish'))}</button><button type="button" class="ghost" data-action="close">${e(t('cancel'))}</button></div></form>`; }
  if (modal === 'profile-edit') { const person=user(); content = `<div class="modal-head"><h2>${e(t('editProfile'))}</h2><button class="icon-btn" data-action="close" aria-label="${e(t('close'))}">×</button></div><form data-form="profile"><div class="profile-photo-editor"><div data-profile-preview>${profileAvatar({name:person.name,avatar:modalPayload?.avatar},'profile-edit-preview')}</div><div><label class="attachment-picker">${icon('attach')}${e(t('chooseAvatar'))}<input name="avatar" type="file" accept="image/png,image/jpeg,image/webp" data-profile-avatar></label><button class="text-action" type="button" data-action="remove-avatar">${e(t('removeAvatar'))}</button></div></div><label class="field">${e(t('name'))}<input name="name" value="${e(person.name)}" required minlength="2" maxlength="60" autofocus></label><label class="field">${e(t('bio'))}<textarea name="bio" maxlength="300" rows="4">${e(person.bio||'')}</textarea></label><p class="form-error" aria-live="polite"></p><div class="modal-footer"><button class="primary">${e(t('saveChanges'))}</button><button class="ghost" type="button" data-action="close">${e(t('cancel'))}</button></div></form>`; }
  return `<div class="modal-backdrop" data-backdrop><div class="modal" role="dialog" aria-modal="true" aria-label="${e(t(modal==='login'||modal==='register'?modal:'details'))}">${content}</div></div>`;
}

function draftKey(form) {
  return [form.dataset.form, form.dataset.question || '', form.dataset.parent || ''].join(':');
}
function captureDrafts() {
  return [...root.querySelectorAll('[data-form]')].map((form) => {
    const fields = [...form.querySelectorAll('input,textarea,select')];
    const focused = fields.indexOf(document.activeElement);
    return {
      key:draftKey(form),
      fields:fields.map((field) => {
        const caret = field === document.activeElement && (field.tagName === 'TEXTAREA' || ['text','search','password'].includes(field.type));
        return { value:field.value, files:field.type === 'file' ? field.files : null, fileNode:field.type === 'file' ? field : null,
          start:caret ? field.selectionStart : null, end:caret ? field.selectionEnd : null };
      }),
      focused,
      error:form.querySelector('.form-error')?.textContent || '',
      selection:form.querySelector('[data-attachment-selection]')?.textContent || '',
    };
  });
}
function restoreDrafts(drafts) {
  const forms = [...root.querySelectorAll('[data-form]')];
  for (const draft of drafts) {
    const form = forms.find((candidate) => draftKey(candidate) === draft.key);
    if (!form) continue;
    const fields = [...form.querySelectorAll('input,textarea,select')];
    if (fields.length !== draft.fields.length) continue;
    fields.forEach((field, index) => {
      const old = draft.fields[index];
      if (field.type === 'file') {
        if (old.files?.length) {
          try { field.files = old.files; }
          catch { field.replaceWith(old.fileNode); fields[index] = old.fileNode; }
        }
      } else field.value = old.value;
    });
    const error = form.querySelector('.form-error');
    if (error) error.textContent = draft.error;
    const selection = form.querySelector('[data-attachment-selection]');
    if (selection) selection.textContent = draft.selection;
    if (draft.focused >= 0) {
      const field = fields[draft.focused];
      field.focus({ preventScroll:true });
      const old = draft.fields[draft.focused];
      if (old.start !== null && old.end !== null) field.setSelectionRange(old.start, old.end);
    }
  }
}
function render(preserveDrafts = true) {
  const page = route();
  document.title = `Spros — ${page.page==='question' ? (page.id.startsWith('local:') ? state.questions.find((x)=>x.id===page.id)?.title : details.get(page.id)?.question.title) || 'Spros' : page.page==='home' ? t('home') : page.page==='space' ? spaceList.find((x)=>x.path===page.slug)?.name || t('explore') : t(page.page==='search'?'searchButton':page.page==='user'?'profile':page.page)}`;
  const viewKey = JSON.stringify([location.hash || '#/home', modal, modalPayload?.item?.id || '', user()?.id || '']);
  if (preserveDrafts && modal && viewKey === renderedViewKey && root.querySelector('.modal')) return;
  const drafts = preserveDrafts && viewKey === renderedViewKey ? captureDrafts() : [];
  let content = page.page === 'home' ? renderHome() : page.page === 'local' ? renderLocal() : page.page === 'saved' ? renderSaved() : page.page === 'profile' ? renderProfile(page) : page.page === 'user' ? renderUser(page) : page.page === 'search' ? renderSearch(page.term) : page.page === 'space' ? renderSpace(page.slug) : renderDetail(page);
  shell(page, content);
  restoreDrafts(drafts);
  renderedViewKey = viewKey;
}
function openModal(type, payload = null) { returnFocus = document.activeElement; modal = type; modalPayload = payload; render(); }
function closeModal() { modal = ''; modalPayload = null; render(); returnFocus?.focus?.(); }
function requireUser() { if (user()) return true; toast(t('authNeeded')); openModal('login'); return false; }

async function reloadState() { state = await read(); render(); }
async function mutate(mutator, preserveDrafts = true) { if (busy) return false; busy = true; try { await change(mutator); state = await read(); render(preserveDrafts); return true; } catch (error) { toast(error.message === 'duplicate-account' ? t('duplicate') : t('noStorage')); return false; } finally { busy = false; } }
async function loadFeed(more = false) {
  if (feedStatus === 'loading' && more) return;
  feedStatus = 'loading'; if (!more) { publicFeed = []; cursor = null; staleAt = null; } render();
  try { const result = await feed(more ? cursor : null); const seen = new Set(publicFeed.map((x)=>x.id)); const extra = result.items.filter((x)=>!seen.has(x.id)); publicFeed = more ? [...publicFeed,...extra] : result.items; cursor = extra.length ? result.pos : null; feedStatus = 'ready'; staleAt = null; }
  catch { feedStatus = 'error'; if (!more) { const cached = cachedFeed(); if (cached) { publicFeed = cached.items; staleAt = cached.at; } } }
  render();
}
async function loadSearch(term) {
  const token = ++requestToken; searchStatus = 'loading'; searchItems=[]; render();
  try { const items = await search(term); if (token !== requestToken) return; searchItems = items; searchStatus = 'ready'; }
  catch { if (token !== requestToken) return; searchStatus = 'error'; }
  render();
}
async function loadDetail(id, refresh = false) {
  if (!refresh && (details.has(id) || detailStatus.get(id)==='loading')) return;
  detailStatus.set(id,'loading'); render();
  try { details.set(id, await detail(id.slice(5))); detailStatus.set(id,'ready'); }
  catch { detailStatus.set(id,'error'); }
  if (route().id===id) render();
}
async function loadSpace(slug, more = false) {
  const old = spaceFeeds.get(slug);
  if (!more && old?.status === 'loading') return;
  const entry = more && old ? old : { items:[], pos:null };
  entry.status = 'loading'; spaceFeeds.set(slug,entry); render();
  try { const result = await feed(more ? entry.pos : null, slug); const known=new Set(entry.items.map((x)=>x.id)); const extra=result.items.filter((x)=>!known.has(x.id) && x.spaces.some((s)=>s.path===slug)); entry.items=more?[...entry.items,...extra]:extra; entry.pos=extra.length?result.pos:null; entry.status='ready'; }
  catch { entry.status='error'; }
  if(route().slug===slug) render();
}
function onRoute() {
  const page = route();
  modal = ''; render();
  if ((page.page==='home' || page.page==='user'&&page.source==='mail') && feedStatus==='loading' && !publicFeed.length) loadFeed();
  if (page.page==='search' && page.term) loadSearch(page.term);
  if (page.page==='space' && !spaceFeeds.has(page.slug)) loadSpace(page.slug);
  if (page.page==='question' && page.id.startsWith('mail:')) loadDetail(page.id);
  window.scrollTo(0,0);
}
async function addItem(type, body, questionId, parentId, files = []) {
  const person = user();
  if (!person) throw new Error('auth');
  if (body.length < 2 || body.length > 5000) throw new Error('invalid');
  const item = { id:`local:${newId()}`, authorId:person.id, body, created:new Date().toISOString(), questionId, ...(parentId ? { parentId } : {}), ...(files.length ? { attachments:files } : {}) };
  const ok = await mutate((data) => {
    if (!(/^mail:\d{1,12}$/.test(questionId) && details.has(questionId)) && !data.questions.some((x)=>x.id===questionId)) throw new Error('missing');
    if (type === 'comment') {
      const external = /^mail-answer:\d{1,12}$/.test(parentId) && details.get(questionId)?.answers.some((x)=>x.id===parentId);
      const ownAnswer = data.answers.some((x)=>x.id===parentId && x.questionId===questionId);
      const ownComment = data.comments.find((x)=>x.id===parentId && x.questionId===questionId);
      if (!external && !ownAnswer && !ownComment) throw new Error('missing-parent');
      let current = ownComment, depth=0;
      while(current && depth<9) { depth++; current=data.comments.find((x)=>x.id===current.parentId); }
      if(depth>8) throw new Error('depth');
    }
    const target = type==='answer' ? data.answers : data.comments;
    if (target.length >= 5000) throw new Error('limit');
    target.push(item);
  }, false);
  if (ok) toast(t('posted'));
  return ok;
}
function pruneComments(data, roots) {
  const removed = new Set(roots);
  let count;
  do { count = removed.size; data.comments.forEach((x)=>{ if(removed.has(x.parentId)) removed.add(x.id); }); } while(removed.size > count);
  data.comments = data.comments.filter((x)=>!removed.has(x.id));
}
async function deleteRecord(kind, id) {
  const ok=await mutate((data)=>{ const collection=data[`${kind}s`]; const item=collection?.find((x)=>x.id===id); if (!item || item.authorId!==user()?.id) throw new Error('permission'); data[`${kind}s`]=collection.filter((x)=>x.id!==id); if (kind==='question') { const answers=data.answers.filter((x)=>x.questionId===id).map((x)=>x.id); data.answers=data.answers.filter((x)=>x.questionId!==id); pruneComments(data,answers); for(const saves of Object.values(data.saves)) { const index=saves.indexOf(id); if(index>=0) saves.splice(index,1); } delete data.snapshots?.[id]; } if (kind==='answer') { pruneComments(data,[id]); const q=data.questions.find((x)=>x.bestId===id); if(q) q.bestId=''; } if(kind==='comment') pruneComments(data,[id]); });
  if(ok) { toast(t('removed')); if(kind==='question') location.hash='#/local'; }
}

root.addEventListener('click', async (event) => {
  if (event.target.matches('[data-backdrop]')) { closeModal(); return; }
  const button = event.target.closest('[data-action]'); if (!button) return;
  const action = button.dataset.action;
  if (action==='language') { setLang(lang()==='ru'?'en':'ru'); render(); return; }
  if (action==='theme') { toggleTheme(); return; }
  if (action==='close') return closeModal();
  if (action==='switch-auth') return openModal(modal==='login'?'register':'login');
  if (action==='login') return openModal('login');
  if (action==='back') { if (routeIndex > 0) { history.back(); } else { location.hash=button.dataset.fallback || '#/home'; } return; }
  if (action==='profile') { location.hash='#/profile'; return; }
  if (action==='edit-profile') { if (requireUser()) openModal('profile-edit',{avatar:user().avatar||''}); return; }
  if (action==='remove-avatar') { if(modalPayload) modalPayload.avatar=''; const preview=root.querySelector('[data-profile-preview]'); if(preview) preview.innerHTML=profileAvatar({name:root.querySelector('[data-form="profile"] [name="name"]')?.value||user()?.name,avatar:''},'profile-edit-preview'); return; }
  if (action==='ask') { if (requireUser()) openModal('ask'); return; }
  if (action==='logout') return openModal('logout-confirm');
  if (action==='confirm-logout') { logout(); closeModal(); return; }
  if (action==='confirm-delete') { const payload=modalPayload; closeModal(); if(payload) await deleteRecord(payload.kind,payload.id); return; }
  if (action==='refresh') return loadFeed();
  if (action==='more') return loadFeed(true);
  if (action==='retry-detail') return loadDetail(route().id,true);
  if (action==='retry-space') return loadSpace(route().slug);
  if (action==='more-space') return loadSpace(route().slug,true);
  if (action==='vote') { if (!requireUser()) return; const id=button.dataset.id; const value=Number(button.dataset.value); return mutate((data)=>{ data.votes[user().id] ||= {}; const old=data.votes[user().id][id]||0; data.votes[user().id][id]=old===value?0:value; }); }
  if (action==='save') { if (!requireUser()) return; const id=button.dataset.id; const snapshot=publicFeed.find((x)=>x.id===id)||details.get(id)?.question||spaceFeeds.get(route().slug)?.items.find((x)=>x.id===id); return mutate((data)=>{ const saves=data.saves[user().id] ||= []; const index=saves.indexOf(id); if (index>=0) saves.splice(index,1); else { saves.unshift(id); if(snapshot) { data.snapshots ||= {}; data.snapshots[id]=snapshot; } } }); }
  if (action==='comment') { if (requireUser()) { replyTarget={ parentId:button.dataset.parent, questionId:route().id }; modal=''; render(); root.querySelector('.inline-reply textarea')?.focus(); } return; }
  if (action==='cancel-reply') { replyTarget=null; render(); return; }
  if (action==='best') { if (!requireUser()) return; const id=button.dataset.id, questionId=route().id; return mutate((data)=>{ const q=data.questions.find((x)=>x.id===questionId); if (!q || q.authorId!==user().id || !data.answers.some((x)=>x.id===id&&x.questionId===questionId)) throw new Error('permission'); q.bestId=q.bestId===id?'':id; }); }
  if (action==='edit') { if (!requireUser()) return; const kind=button.dataset.kind, item=state[`${kind}s`]?.find((x)=>x.id===button.dataset.id); if (!item || item.authorId!==user().id) return; openModal(kind==='question'?'edit-question':kind==='answer'?'edit-answer':'edit-comment',{item,kind}); return; }
  if (action==='delete') { if(requireUser()) openModal('delete-confirm',{kind:button.dataset.kind,id:button.dataset.id}); return; }
});

root.addEventListener('submit', async (event) => {
  const form = event.target.closest('[data-form]'); if (!form) return; event.preventDefault();
  const type=form.dataset.form;
  if (type==='search') { const term=form.elements.term.value.trim(); if(term) location.hash=`#/search/${encodeURIComponent(term)}`; return; }
  if (type==='auth') { const email=form.elements.email.value, password=form.elements.password.value; const error=form.querySelector('.form-error'); try { if(modal==='register') await register(form.elements.name.value,email,password); else await login(email,password,state); await reloadState(); closeModal(); } catch(err) { error.textContent = err.message==='duplicate-account'?t('duplicate'):err.message==='invalid-login'?t('wrongLogin'):t('formError'); } return; }
  if (type==='profile') { if(form.dataset.processing==='true') return; const person=user(),name=form.elements.name.value.trim(),bio=form.elements.bio.value.trim(); if(!person||name.length<2) { form.querySelector('.form-error').textContent=t('formError'); return; } const avatar=modalPayload?.avatar||''; const ok=await mutate((data)=>{ const account=data.accounts.find((x)=>x.id===person.id); if(!account) throw new Error('permission'); Object.assign(account,{name,bio,avatar}); }); if(ok){closeModal();toast(t('profileUpdated'));} return; }
  if (type==='question') { if (!requireUser()) return; const title=form.elements.title.value.trim(),body=form.elements.body.value.trim(),space=form.elements.space.value.trim(); if(title.length<5) { form.querySelector('.form-error').textContent=t('formError'); return; } let files; try { files=await selectedAttachments(form); } catch { form.querySelector('.form-error').textContent=t('attachmentError'); return; } const person=user(); const old=modalPayload?.item; const id=old?.id || `local:${newId()}`; const ok=await mutate((data)=>{ if(old) { const item=data.questions.find((x)=>x.id===id&&x.authorId===person.id); if (!item) throw new Error('permission'); Object.assign(item,{title,body,space,updated:new Date().toISOString(),attachments:[...storedAttachments(item),...files].slice(0,3)}); } else { data.questions.push({id,title,body,space,authorId:person.id,created:new Date().toISOString(),bestId:'',...(files.length?{attachments:files}:{})}); } }); if(ok){closeModal();location.hash=qurl(id);toast(t('posted'));} return; }
  if (type==='answer' || type==='comment') { if (!requireUser()) return; const body=form.elements.body.value.trim(); if(body.length<2) return; let files; try { files=await selectedAttachments(form); } catch { form.querySelector('.form-error').textContent=t('attachmentError'); return; } const ok=await addItem(type,body,form.dataset.question,form.dataset.parent,files); if(ok!==false) { replyTarget=null; render(); } return; }
  if (type==='text') { if (!requireUser()) return; const body=form.elements.body.value.trim(); const payload=modalPayload; if(body.length<2) { form.querySelector('.form-error').textContent=t('formError'); return; } let files; try { files=await selectedAttachments(form); } catch { form.querySelector('.form-error').textContent=t('attachmentError'); return; } const kind=payload.kind, id=payload.item.id; const ok=await mutate((data)=>{ const item=data[`${kind}s`].find((x)=>x.id===id&&x.authorId===user().id); if(!item) throw new Error('permission'); Object.assign(item,{body,updated:new Date().toISOString(),attachments:[...storedAttachments(item),...files].slice(0,3)}); }); if(ok){closeModal();toast(t('edited'));} }
});
root.addEventListener('change', async (event) => {
  if(event.target.dataset.change==='language') { setLang(event.target.value); render(); return; }
  if(event.target.matches('[data-profile-avatar]')) {
    const form=event.target.closest('form'),error=form.querySelector('.form-error'),file=event.target.files?.[0];
    if(!file) return;
    event.target.disabled=true; form.dataset.processing='true'; form.querySelector('button[type="submit"],.modal-footer .primary').disabled=true;
    try { const image=await profileImage(file); if(modal==='profile-edit'&&modalPayload&&form.isConnected) { modalPayload.avatar=image; form.querySelector('[data-profile-preview]').innerHTML=profileAvatar({name:form.elements.name.value,avatar:image},'profile-edit-preview'); error.textContent=''; } }
    catch { if(form.isConnected) error.textContent=t('avatarError'); }
    finally { event.target.disabled=false; form.dataset.processing='false'; form.querySelector('button[type="submit"],.modal-footer .primary').disabled=false; }
    return;
  }
  if (event.target.matches('[data-attachment-input]')) {
    const files = [...event.target.files];
    const selection = event.target.closest('form')?.querySelector('[data-attachment-selection]');
    if (selection) selection.textContent = files.length ? files.map((file) => file.name).join(', ') : '';
  }
});
document.addEventListener('keydown',(event)=>{ if(event.key==='Escape'&&modal) closeModal(); });
window.addEventListener('hashchange',()=>{
  const nextIndex = history.state?.answersRouteIndex;
  if (pendingRouteIndex !== null) { routeIndex=pendingRouteIndex; pendingRouteIndex=null; history.replaceState({ ...(history.state || {}), answersRouteIndex:routeIndex },'',location.href); }
  else if (Number.isSafeInteger(nextIndex) && nextIndex !== routeIndex) routeIndex=nextIndex;
  else { routeIndex += 1; history.replaceState({ ...(history.state || {}), answersRouteIndex:routeIndex },'',location.href); }
  onRoute();
});
root.addEventListener('click',(event)=>{
  const link=event.target.closest('a[href^="#/"]');
  if (!link || event.defaultPrevented || event.button!==0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || link.getAttribute('target')) return;
  if (link.getAttribute('href')===location.hash) return;
  pendingRouteIndex=routeIndex+1;
});
root.addEventListener('focusin', (event) => {
  if (replyTarget && event.target.matches('[data-form="answer"] textarea')) {
    replyTarget = null;
    render();
    root.querySelector('[data-form="answer"] textarea')?.focus();
  }
});
watch(async()=>{ try { state=await read(); render(); } catch {} });

render();
try { state = await read(); } catch { toast(t('noStorage')); }
restoreRemoteProfiles();
history.replaceState({ ...(history.state || {}), answersRouteIndex:routeIndex },'',location.href);
onRoute();
spaces().then((items)=>{spaceList=items;render();}).catch(()=>{});
