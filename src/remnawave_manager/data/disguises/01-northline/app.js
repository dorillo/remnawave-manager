const modal = document.querySelector('#modal');
const modalTitle = document.querySelector('#modal-title');
const modalText = document.querySelector('#modal-text');
const authForm = document.querySelector('#auth-form');
const authResult = document.querySelector('.auth-result');

function openDialog(title, text, auth = false) {
  modalTitle.textContent = title;
  modalText.textContent = text;
  authForm.hidden = !auth;
  authResult.hidden = true;
  modal.hidden = false;
  modal.querySelector('.close').focus();
}
function openAuth() {
  openDialog('Вход в Линию', 'Введите данные профиля или запросите приглашение для своего района.', true);
}
document.querySelectorAll('[data-auth]').forEach((item) => item.addEventListener('click', (event) => { event.preventDefault(); openAuth(); }));
document.querySelectorAll('[data-info]').forEach((item) => item.addEventListener('click', () => openDialog(item.dataset.info, 'Публичная карта показывает районы, события и сообщества. Персональные настройки становятся доступны после входа.')));
document.querySelectorAll('[data-story]').forEach((button) => button.addEventListener('click', () => openDialog(`История: ${button.dataset.story}`, 'Новая история откроется здесь после публикации автором. Предыдущая подборка уже завершилась.')));
document.querySelectorAll('[data-event]').forEach((link) => link.addEventListener('click', (event) => { event.preventDefault(); const title = link.querySelector('strong').textContent; const place = link.querySelector('span').textContent; openDialog(title, `${place}. Подробная программа доступна публично, а запись участника потребует входа.`); }));
document.querySelectorAll('[data-post-menu]').forEach((button) => button.addEventListener('click', () => openDialog('Действия с публикацией', 'Ссылку можно открыть без аккаунта. Жалобы, скрытие автора и персональные настройки ленты доступны после входа.')));
modal.querySelector('.close').addEventListener('click', () => { modal.hidden = true; });
modal.addEventListener('click', (event) => { if (event.target === modal) modal.hidden = true; });
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') modal.hidden = true; });
authForm.addEventListener('submit', (event) => { event.preventDefault(); authForm.hidden = true; authResult.hidden = false; });
document.querySelector('#register').addEventListener('click', () => { authForm.hidden = true; authResult.hidden = false; modalTitle.textContent = 'Регистрация по приглашению'; modalText.textContent = 'Для создания профиля требуется код районного модератора.'; });
document.querySelector('#request-invite').addEventListener('click', () => { authResult.querySelector('strong').textContent = 'Запрос поставлен в очередь'; authResult.querySelector('p').textContent = 'Новые места распределяются раз в неделю. Письмо придёт после открытия района.'; });

let activeFeed = 'all';
const search = document.querySelector('#global-search');
function filterPosts() {
  const query = search.value.trim().toLocaleLowerCase('ru'); let visible = 0;
  document.querySelectorAll('.post').forEach((post) => { const matchesFeed = activeFeed === 'all' || post.dataset.kind === activeFeed; const matchesQuery = !query || post.dataset.search.includes(query); post.hidden = !(matchesFeed && matchesQuery); if (!post.hidden) visible += 1; });
  document.querySelector('.empty').hidden = visible !== 0;
}
search.addEventListener('input', filterPosts);
document.querySelectorAll('[data-feed]').forEach((tab) => tab.addEventListener('click', () => { document.querySelectorAll('[data-feed]').forEach((item) => item.classList.remove('active')); tab.classList.add('active'); activeFeed = tab.dataset.feed; filterPosts(); }));

document.querySelectorAll('a[href^="#"]').forEach((link) => link.addEventListener('click', (event) => {
  const target = link.getAttribute('href');
  if (target === '#' || (target.length > 1 && !document.querySelector(target) && !link.hasAttribute('data-auth') && !link.hasAttribute('data-event'))) {
    event.preventDefault(); openDialog(link.textContent.trim() || 'Раздел Линии', 'Раздел доступен в публичном каталоге. Персональные действия потребуют входа.');
  }
}));
