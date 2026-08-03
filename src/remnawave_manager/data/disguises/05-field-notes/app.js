const panel = document.querySelector('.search-panel');
const authModal = document.querySelector('.auth-modal');
const infoModal = document.querySelector('.info-modal');
const toast = document.querySelector('.toast');
let toastTimer;

function showToast(text) {
  clearTimeout(toastTimer);
  toast.textContent = text;
  toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, 3000);
}

function closeModal(modal) { modal.hidden = true; }
function openAuth() {
  authModal.querySelector('.auth-login').hidden = false;
  authModal.querySelector('.auth-token').hidden = true;
  authModal.querySelector('.form-error').hidden = true;
  authModal.hidden = false;
  authModal.querySelector('input').focus();
}

const info = {
  discussion: ['Обсуждение статьи', '<p><b>12 реплик в 3 темах</b></p><p>Редакторы уточняют терминологию раздела о паводках и проверяют новый источник по возрасту Большого каньона.</p><p>Писать в обсуждении могут подтверждённые участники редакции.</p>'],
  history: ['История правок', '<p><b>28 июля · Анна Суворова</b><br>Уточнены данные о глубине и добавлена ссылка на геологическую съёмку.</p><p><b>21 июля · Илья Петров</b><br>Переработан раздел о вертикальной эрозии.</p><p><b>4 июля · редакторская проверка</b><br>Статья получила статус рекомендованной.</p>']
};

function openInfo(title, body) {
  infoModal.querySelector('h2').textContent = title;
  infoModal.querySelector('.info-body').innerHTML = body;
  infoModal.hidden = false;
  infoModal.querySelector('.modal-close').focus();
}

function openSearch() { panel.hidden = false; panel.querySelector('input').focus(); }
document.querySelector('.search').addEventListener('click', openSearch);
document.querySelector('#search').addEventListener('focus', openSearch);
panel.querySelector('label button').addEventListener('click', () => { panel.hidden = true; });
panel.addEventListener('click', (event) => { if (event.target === panel) panel.hidden = true; });
document.querySelector('.theme').addEventListener('click', () => {
  document.body.classList.toggle('dark');
  showToast(document.body.classList.contains('dark') ? 'Включена тёмная тема' : 'Включена светлая тема');
});
document.querySelector('.menu').addEventListener('click', () => document.querySelector('.contents').classList.toggle('open'));
document.querySelectorAll('.contents a').forEach((link) => link.addEventListener('click', () => document.querySelector('.contents').classList.remove('open')));
document.querySelector('.save').addEventListener('click', openAuth);
document.querySelector('.print').addEventListener('click', () => window.print());
document.querySelector('.listen').addEventListener('click', () => showToast('Аудиоверсия проходит редакторскую проверку и станет доступна 6 августа.'));
document.querySelectorAll('[data-auth]').forEach((button) => button.addEventListener('click', openAuth));
document.querySelectorAll('[data-info]').forEach((button) => button.addEventListener('click', () => openInfo(...info[button.dataset.info])));
document.querySelectorAll('.result').forEach((button) => button.addEventListener('click', () => {
  panel.hidden = true;
  openInfo(button.querySelector('b').textContent, `<p>${button.querySelector('span').textContent}</p><p>Материал находится в открытом каталоге. Полный текст готовится к очередной научной проверке; сейчас доступна библиографическая карточка и оглавление.</p>`);
}));

authModal.querySelector('.auth-login form').addEventListener('submit', (event) => {
  event.preventDefault();
  authModal.querySelector('.auth-login').hidden = true;
  authModal.querySelector('.auth-token').hidden = false;
  authModal.querySelector('.auth-token input').focus();
});
authModal.querySelector('.auth-token form').addEventListener('submit', (event) => {
  event.preventDefault();
  const error = authModal.querySelector('.form-error');
  error.textContent = 'Ключ не относится к текущему редакторскому набору. Доступ не предоставлен; проверьте письмо куратора.';
  error.hidden = false;
});
authModal.querySelector('[data-back]').addEventListener('click', () => {
  authModal.querySelector('.auth-token').hidden = true;
  authModal.querySelector('.auth-login').hidden = false;
});
authModal.querySelector('[data-request]').addEventListener('click', () => {
  closeModal(authModal);
  showToast('Заявки на осенний набор откроются 15 сентября. Напоминание сохранено только в этой вкладке.');
});

document.querySelectorAll('.modal').forEach((modal) => {
  modal.querySelector('.modal-close').addEventListener('click', () => closeModal(modal));
  modal.addEventListener('click', (event) => { if (event.target === modal) closeModal(modal); });
});
infoModal.querySelector('.info-action').addEventListener('click', () => closeModal(infoModal));

document.querySelectorAll('a[href^="#"]').forEach((link) => link.addEventListener('click', (event) => {
  const target = link.getAttribute('href');
  if (target !== '#' && document.querySelector(target)) return;
  event.preventDefault();
  const title = link.querySelector('b')?.textContent || link.textContent.trim() || 'Раздел энциклопедии';
  openInfo(title, '<p>Карточка связанного материала уже включена в каталог «Свода».</p><p>Текст проходит сверку источников и будет опубликован после научной рецензии. Пока можно продолжить чтение текущей статьи.</p>');
}));

document.addEventListener('keydown', (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLocaleLowerCase() === 'k') { event.preventDefault(); openSearch(); }
  if (event.key === 'Escape') { panel.hidden = true; closeModal(authModal); closeModal(infoModal); }
});

const sections = [...document.querySelectorAll('.copy h2')];
addEventListener('scroll', () => {
  let current = 'intro';
  sections.forEach((section) => { if (section.getBoundingClientRect().top < 140) current = section.id; });
  document.querySelectorAll('.contents nav a').forEach((link) => link.classList.toggle('active', link.getAttribute('href') === `#${current}`));
}, { passive: true });
