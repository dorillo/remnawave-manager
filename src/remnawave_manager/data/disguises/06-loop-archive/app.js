const gate = document.querySelector('.gate');
const detail = document.querySelector('.detail');
const toast = document.querySelector('.toast');
const search = document.querySelector('#search');
let type = 'all';
let toastTimer;

function showToast(text) {
  clearTimeout(toastTimer);
  toast.textContent = text;
  toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, 2800);
}

function openAuth() {
  gate.querySelector('.login-stage').hidden = false;
  gate.querySelector('.captcha-stage').hidden = true;
  gate.querySelector('.captcha-error').hidden = true;
  gate.hidden = false;
  gate.querySelector('input').focus();
}

function openDetail(title, body) {
  detail.querySelector('h2').textContent = title;
  detail.querySelector('.detail-body').innerHTML = body;
  detail.hidden = false;
  detail.querySelector('.detail-close').focus();
}

document.querySelectorAll('[data-auth],.favorite').forEach((button) => button.addEventListener('click', openAuth));
gate.querySelector('.close').addEventListener('click', () => { gate.hidden = true; });
gate.addEventListener('click', (event) => { if (event.target === gate) gate.hidden = true; });
gate.querySelector('.login-stage form').addEventListener('submit', (event) => {
  event.preventDefault();
  gate.querySelector('.login-stage').hidden = true;
  gate.querySelector('.captcha-stage').hidden = false;
});
gate.querySelector('.register').addEventListener('click', () => {
  gate.hidden = true;
  showToast('Регистрация временно доступна только по приглашению авторов действующих стикерпаков.');
});
gate.querySelector('.back-login').addEventListener('click', () => {
  gate.querySelector('.captcha-stage').hidden = true;
  gate.querySelector('.login-stage').hidden = false;
});
gate.querySelectorAll('.captcha button,.retry').forEach((button) => button.addEventListener('click', () => {
  const error = gate.querySelector('.captcha-error');
  error.textContent = 'Контрольный фрагмент не загрузился полностью. Проверка не может быть завершена с этого устройства.';
  error.hidden = false;
}));

detail.querySelector('.detail-close').addEventListener('click', () => { detail.hidden = true; });
detail.querySelector('.detail-action').addEventListener('click', () => { detail.hidden = true; });
detail.addEventListener('click', (event) => { if (event.target === detail) detail.hidden = true; });

document.querySelectorAll('.copy').forEach((button) => button.addEventListener('click', () => {
  button.textContent = 'Готово ✓';
  showToast('Ссылка на реакцию подготовлена и доступна в этой вкладке');
  setTimeout(() => { button.textContent = 'Скопировать'; }, 1600);
}));

function filter() {
  const query = search.value.trim().toLocaleLowerCase('ru');
  let visible = 0;
  document.querySelectorAll('.tile').forEach((tile) => {
    const match = (type === 'all' || tile.dataset.type === type) && (!query || tile.dataset.search.includes(query));
    tile.hidden = !match;
    if (match) visible += 1;
  });
  document.querySelector('.empty').hidden = visible !== 0;
}

document.querySelectorAll('[data-filter]').forEach((button) => button.addEventListener('click', () => {
  document.querySelectorAll('[data-filter]').forEach((item) => item.classList.remove('active'));
  button.classList.add('active');
  type = button.dataset.filter;
  filter();
}));
document.querySelectorAll('[data-query]').forEach((button) => button.addEventListener('click', () => {
  search.value = button.dataset.query;
  filter();
  document.querySelector('#gallery').scrollIntoView({ behavior: 'smooth' });
}));
search.addEventListener('input', filter);

document.querySelectorAll('.tile').forEach((tile) => tile.addEventListener('click', (event) => {
  if (event.target.closest('button')) return;
  const tags = tile.querySelector('footer span').textContent;
  openDetail(tags, `<p>Формат: <b>${tile.dataset.type.toLocaleUpperCase('ru')}</b></p><p>Реакция доступна для копирования и личного использования. Добавление в наборы требует входа.</p><p>Ключевые слова: ${tile.dataset.search}</p>`);
}));
document.querySelectorAll('.sticker-card').forEach((card) => card.addEventListener('click', () => openDetail('Выбор редакции', `<p>${card.textContent.trim()}</p><p>Эта реакция вошла в августовскую подборку за ясный текст и хорошую читаемость в небольшом размере.</p>`)));
document.querySelectorAll('[data-pack]').forEach((button) => button.addEventListener('click', () => openDetail(button.dataset.pack, `<p>${button.querySelector('span').textContent}</p><p>Можно просмотреть набор без регистрации. Установка и синхронизация с устройствами доступны участникам «Петли».</p>`)));

document.querySelector('.creator form').addEventListener('submit', (event) => {
  event.preventDefault();
  const preview = document.querySelector('.creator-preview');
  const value = document.querySelector('#creator-text').value.trim();
  preview.className = `creator-preview ${document.querySelector('#creator-tone').value}`;
  preview.querySelector('strong').textContent = value.toLocaleUpperCase('ru');
  showToast('Предпросмотр обновлён локально');
});

document.querySelectorAll('a[href^="#"]').forEach((link) => link.addEventListener('click', (event) => {
  const target = link.getAttribute('href');
  if (target !== '#' && document.querySelector(target)) return;
  event.preventDefault();
  const title = link.textContent.trim();
  const descriptions = {
    'Правила': 'В каталоге публикуются оригинальные работы и материалы с подтверждёнными правами. Запрещены персональные данные, травля и вводящие в заблуждение подписи.',
    'Авторам': 'Приём новых авторов идёт пакетами после ручной проверки портфолио. Следующее окно заявок откроется в сентябре.',
    'Поддержка': 'Справочный центр работает с 10:00 до 19:00 МСК. Обращения без аккаунта принимаются через форму после антиспам-проверки.'
  };
  openDetail(title || 'Раздел', `<p>${descriptions[title] || 'Раздел готовится к публикации.'}</p>`);
}));

document.addEventListener('keydown', (event) => {
  if (event.key === '/' && document.activeElement !== search) { event.preventDefault(); search.focus(); }
  if (event.key === 'Escape') { gate.hidden = true; detail.hidden = true; }
});
document.querySelector('#sort').addEventListener('change', (event) => {
  const gallery = document.querySelector('.gallery');
  const tiles = [...gallery.querySelectorAll('.tile')];
  tiles.sort((a, b) => {
    if (event.target.value === 'short') return a.dataset.search.length - b.dataset.search.length;
    const key = event.target.value === 'new' ? 'new' : 'popular';
    return Number(b.dataset[key]) - Number(a.dataset[key]);
  }).forEach((tile) => gallery.insertBefore(tile, gallery.querySelector('.empty')));
  showToast(event.target.value === 'new' ? 'Сначала показаны новые реакции' : event.target.value === 'short' ? 'Короткие реакции подняты выше' : 'Сначала показаны популярные реакции');
});
