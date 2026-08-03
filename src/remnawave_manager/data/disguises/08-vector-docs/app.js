const searchModal = document.querySelector('.search-modal');
const versionMenu = document.querySelector('.version-menu');
const tryModal = document.querySelector('.try-modal');
const authModal = document.querySelector('.auth-modal');
const infoModal = document.querySelector('.info-modal');
const toast = document.querySelector('.toast');
let toastTimer;

function showToast(text) {
  clearTimeout(toastTimer);
  toast.textContent = text;
  toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, 2600);
}

function openSearch() {
  searchModal.hidden = false;
  searchModal.querySelector('input').focus();
}

function openAuth() {
  tryModal.hidden = true;
  authModal.querySelector('.auth-login').hidden = false;
  authModal.querySelector('.auth-token').hidden = true;
  authModal.querySelector('.auth-error').hidden = true;
  authModal.hidden = false;
  authModal.querySelector('input').focus();
}

function openInfo(title, body) {
  infoModal.querySelector('h2').textContent = title;
  infoModal.querySelector('.info-body').innerHTML = body;
  infoModal.hidden = false;
  infoModal.querySelector('.modal-close').focus();
}

document.querySelector('.search').addEventListener('click', openSearch);
searchModal.querySelector('label button').addEventListener('click', () => { searchModal.hidden = true; });
searchModal.addEventListener('click', (event) => { if (event.target === searchModal) searchModal.hidden = true; });
document.querySelectorAll('[data-jump]').forEach((button) => button.addEventListener('click', () => {
  searchModal.hidden = true;
  document.querySelector(button.dataset.jump).scrollIntoView({ behavior: 'smooth' });
}));
searchModal.querySelector('input').addEventListener('input', (event) => {
  const query = event.target.value.trim().toLocaleLowerCase('ru');
  searchModal.querySelectorAll('[data-jump]').forEach((button) => { button.hidden = Boolean(query) && !button.textContent.toLocaleLowerCase('ru').includes(query); });
});

document.querySelector('.version').addEventListener('click', () => { versionMenu.hidden = false; versionMenu.querySelector('[data-version]').focus(); });
versionMenu.querySelector('header button').addEventListener('click', () => { versionMenu.hidden = true; });
versionMenu.addEventListener('click', (event) => { if (event.target === versionMenu) versionMenu.hidden = true; });
versionMenu.querySelectorAll('[data-version]').forEach((button) => button.addEventListener('click', () => {
  versionMenu.hidden = true;
  if (button.dataset.version === 'v2.4') { showToast('Вы уже читаете стабильную документацию v2.4'); return; }
  openInfo(`Документация ${button.dataset.version}`, button.dataset.version === 'v1.0' ? '<p>Версия 1.0 перенесена в архив и больше не получает исправления. Примеры доступны только для чтения.</p><p>Для новых интеграций используйте v2.4.</p>' : '<p>Версия 2.3 находится в режиме долгосрочной поддержки. Описание миграции и таблица несовместимых изменений доступны в журнале версий.</p>');
}));

document.querySelectorAll('[data-lang]').forEach((button) => button.addEventListener('click', () => {
  document.querySelectorAll('[data-lang]').forEach((item) => item.classList.remove('active'));
  button.classList.add('active');
  document.querySelectorAll('[data-code]').forEach((code) => { code.hidden = code.dataset.code !== button.dataset.lang; });
}));
document.querySelectorAll('.copy-code,.copy-page').forEach((button) => button.addEventListener('click', () => showToast('Фрагмент подготовлен для копирования')));

document.querySelector('.api-method header button').addEventListener('click', () => {
  tryModal.querySelector('pre').hidden = true;
  tryModal.hidden = false;
  tryModal.querySelector('input').focus();
});
tryModal.querySelector('form').addEventListener('submit', (event) => {
  event.preventDefault();
  tryModal.querySelector('pre').hidden = false;
  showToast('Песочница отклонила ключ: проект не найден');
});

document.querySelectorAll('[data-auth]').forEach((button) => button.addEventListener('click', openAuth));
authModal.querySelector('.auth-login form').addEventListener('submit', (event) => {
  event.preventDefault();
  authModal.querySelector('.auth-login').hidden = true;
  authModal.querySelector('.auth-token').hidden = false;
  authModal.querySelector('.auth-token input').focus();
});
authModal.querySelector('[data-sso]').addEventListener('click', () => {
  authModal.querySelector('.auth-login').hidden = true;
  authModal.querySelector('.auth-token').hidden = false;
});
authModal.querySelector('.auth-token form').addEventListener('submit', (event) => {
  event.preventDefault();
  const error = authModal.querySelector('.auth-error');
  error.textContent = 'Токен приглашения недействителен или уже использован. Доступ к организации не предоставлен.';
  error.hidden = false;
});
authModal.querySelector('[data-back]').addEventListener('click', () => {
  authModal.querySelector('.auth-token').hidden = true;
  authModal.querySelector('.auth-login').hidden = false;
});

document.querySelectorAll('[data-feedback]').forEach((button) => button.addEventListener('click', () => {
  document.querySelectorAll('[data-feedback]').forEach((item) => item.classList.remove('selected'));
  button.classList.add('selected');
  showToast(button.dataset.feedback === 'yes' ? 'Спасибо. Оценка сохранена в этой сессии.' : 'Спасибо. Редакторы документации просмотрят эту страницу.');
}));
document.querySelector('.mobile-menu').addEventListener('click', () => document.querySelector('.sidebar').classList.toggle('open'));
document.querySelectorAll('.sidebar a').forEach((link) => link.addEventListener('click', () => document.querySelector('.sidebar').classList.remove('open')));
document.querySelectorAll('.nav-group').forEach((button) => button.addEventListener('click', () => {
  const submenu = button.nextElementSibling;
  if (submenu?.tagName === 'DIV') { button.classList.toggle('open'); submenu.hidden = !button.classList.contains('open'); return; }
  openInfo(button.childNodes[0].textContent.trim(), '<p>Справочник ресурсов содержит схемы объектов, параметры фильтрации, примеры запросов и полный перечень событий вебхуков.</p><p>Раздел доступен в версии 2.4 и регулярно обновляется.</p>');
}));

const sectionInfo = {
  API: '<p>Справочник REST API содержит методы коллекций, документов, событий и управления токенами. Все ответы используют JSON и единый формат ошибок.</p>',
  SDK: '<p>Официальные клиентские библиотеки поддерживают JavaScript, Python, Go и Kotlin. Пакеты публикуются вместе со спецификацией API.</p>',
  Изменения: '<p><b>v2.4.0 · 24 июля</b><br>Добавлены курсорная пагинация событий и новые коды ошибок песочницы.</p><p><b>v2.3.4 · 2 июля</b><br>Уточнена проверка подписей вебхуков.</p>',
  Репозиторий: '<p>Зеркало примеров содержит стартовые проекты, схемы OpenAPI и сценарии миграции. Запись доступна только сопровождающим, чтение открыто.</p>',
  'Изменить страницу': '<p>Правки документации принимаются из подтверждённых организаций. После входа редактор создаёт ветку и отправляет изменение на техническую проверку.</p>'
};

document.querySelectorAll('a[href^="#"]').forEach((link) => link.addEventListener('click', (event) => {
  const target = link.getAttribute('href');
  if (target !== '#' && document.querySelector(target)) return;
  event.preventDefault();
  const title = link.getAttribute('aria-label') || link.textContent.trim();
  if (target === '#edit') { openAuth(); return; }
  openInfo(title, sectionInfo[title] || '<p>Этот раздел входит в полный справочник Vector. Материал доступен для чтения, но интерактивные примеры требуют тестового проекта.</p>');
}));

document.querySelectorAll('.try-modal,.auth-modal,.info-modal').forEach((modal) => {
  modal.querySelector('.modal-close').addEventListener('click', () => { modal.hidden = true; });
  modal.addEventListener('click', (event) => { if (event.target === modal) modal.hidden = true; });
});
infoModal.querySelector('.info-close').addEventListener('click', () => { infoModal.hidden = true; });

document.addEventListener('keydown', (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLocaleLowerCase() === 'k') { event.preventDefault(); openSearch(); }
  if (event.key === 'Escape') { searchModal.hidden = true; versionMenu.hidden = true; tryModal.hidden = true; authModal.hidden = true; infoModal.hidden = true; }
});
