const drawer = document.querySelector('.drawer');
const detailModal = document.querySelector('.detail-modal');
const subscribeModal = document.querySelector('.subscribe-modal');
const authModal = document.querySelector('.auth-modal');
const toast = document.querySelector('.toast');
const componentSearch = document.querySelector('.components input[type="search"]');
let componentState = 'all';
let toastTimer;

function showToast(text) {
  clearTimeout(toastTimer);
  toast.textContent = text;
  toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, 3000);
}

function openDetail(title, body) {
  detailModal.querySelector('h2').textContent = title;
  detailModal.querySelector('.detail-body').innerHTML = body;
  detailModal.hidden = false;
  detailModal.querySelector('.modal-close').focus();
}

function openSubscribe() {
  drawer.hidden = true;
  subscribeModal.querySelector('.subscribe-form').hidden = false;
  subscribeModal.querySelector('.subscribe-result').hidden = true;
  subscribeModal.hidden = false;
  subscribeModal.querySelector('input[type="email"]').focus();
}

function openAuth() {
  authModal.querySelector('.auth-login').hidden = false;
  authModal.querySelector('.auth-org').hidden = true;
  authModal.querySelector('.auth-error').hidden = true;
  authModal.hidden = false;
  authModal.querySelector('input').focus();
}

document.querySelectorAll('[data-range]').forEach((button) => button.addEventListener('click', () => {
  document.querySelectorAll('[data-range]').forEach((item) => item.classList.remove('active'));
  button.classList.add('active');
  const labels = { 24: '3 августа, 00:00 — 14:32 МСК', 168: '28 июля — 3 августа 2026', 720: '5 июля — 3 августа 2026', 2160: '6 мая — 3 августа 2026' };
  document.querySelector('.range-bar > span').textContent = labels[button.dataset.range];
  showToast(`Графики перестроены за период: ${button.textContent}`);
}));

document.querySelector('.export').addEventListener('click', (event) => {
  event.currentTarget.textContent = '✓ Отчёт готов';
  showToast('Публичный отчёт сформирован в этой вкладке. Персональные данные в него не входят.');
  setTimeout(() => { event.currentTarget.textContent = '⇩ Отчёт'; }, 2200);
});

function filterComponents() {
  const query = componentSearch.value.trim().toLocaleLowerCase('ru');
  let visible = 0;
  document.querySelectorAll('.component-row').forEach((row) => {
    const matches = (componentState === 'all' || row.dataset.state === componentState) && (!query || row.textContent.toLocaleLowerCase('ru').includes(query));
    row.hidden = !matches;
    if (matches) visible += 1;
  });
  document.querySelector('.components .empty').hidden = visible !== 0;
}

document.querySelectorAll('[data-status]').forEach((button) => button.addEventListener('click', () => {
  document.querySelectorAll('[data-status]').forEach((item) => item.classList.remove('active'));
  button.classList.add('active');
  componentState = button.dataset.status;
  filterComponents();
}));
componentSearch.addEventListener('input', filterComponents);

document.querySelectorAll('.component-row').forEach((row) => row.addEventListener('click', () => {
  drawer.querySelector('h2').textContent = row.dataset.name;
  drawer.querySelector('.drawer-description').textContent = row.dataset.description;
  drawer.querySelector('.drawer-status').textContent = row.dataset.state === 'maintenance' ? '◇ РАБОТАЕТ · ЗАПЛАНИРОВАНЫ РАБОТЫ' : '● РАБОТАЕТ ШТАТНО';
  drawer.querySelector('.drawer-status').classList.toggle('planned', row.dataset.state === 'maintenance');
  drawer.hidden = false;
  drawer.querySelector('header button').focus();
}));
drawer.querySelector('.uptime-days').append(...Array.from({ length: 30 }, () => document.createElement('i')));
drawer.querySelector('header button').addEventListener('click', () => { drawer.hidden = true; });
drawer.addEventListener('click', (event) => { if (event.target === drawer) drawer.hidden = true; });

document.querySelector('.region-details').addEventListener('click', () => openDetail('Точки наблюдения', '<p>Проверки запускаются из четырёх независимых сетей каждые 30 секунд. Инцидент подтверждается, если ошибка воспроизводится не менее чем в двух регионах.</p><h3>Текущая связность</h3><p>Москва 42 мс · Казань 58 мс · Екатеринбург 91 мс · Новосибирск 104 мс. Потерь пакетов за последний час нет.</p>'));
const incidents = {
  events: ['Задержка доставки событий', '<p><b>28 июля · 09:14–09:37 МСК</b></p><p>После планового изменения конфигурации одна очередь в регионе Урал перестала равномерно распределять нагрузку. События сохранялись и были доставлены после восстановления.</p><h3>Хронология</h3><p>09:14 — автоматическое обнаружение<br>09:19 — ограничено влияние<br>09:31 — очередь обработана<br>09:37 — инцидент закрыт</p>'],
  files: ['Ошибки загрузки крупных файлов', '<p><b>11 июня · 16:02–16:19 МСК</b></p><p>При повторной отправке последнего фрагмента могла неверно вычисляться контрольная сумма. Незавершённые загрузки не публиковались, повреждения данных не было.</p><h3>После инцидента</h3><p>Добавлена проверка повторной сборки файла и отдельный тест для граничного размера 500 МБ.</p>']
};
document.querySelectorAll('[data-incident]').forEach((button) => button.addEventListener('click', () => openDetail(...incidents[button.dataset.incident])));

const maintenance = {
  export: ['Обновление контура экспорта', '<p><b>8 августа, 02:00–02:30 МСК</b></p><p>Будет обновлён планировщик больших выгрузок. Новые задания останутся в очереди и начнут выполняться после окна. Готовые файлы и остальные компоненты доступны без ограничений.</p>'],
  routing: ['Проверка резервного маршрута', '<p><b>15 августа, 03:00–03:20 МСК</b></p><p>Трафик на короткое время переключится на резервный маршрут. Клиенты с корректной политикой повторов не заметят изменения; отдельный запрос может получить код 503.</p>']
};
document.querySelectorAll('[data-maintenance]').forEach((button) => button.addEventListener('click', () => openDetail(...maintenance[button.dataset.maintenance])));
document.querySelector('.calendar').addEventListener('click', () => showToast('Календарь подготовлен. Подписка станет активна после подтверждения уведомлений.'));
document.querySelector('[data-methodology]').addEventListener('click', () => openDetail('Методика расчёта', '<p>Доступность считается как доля успешных проверок за период. Для HTTP успешными считаются ответы, соответствующие опубликованному контракту, полученные до истечения порога.</p><h3>Исключения</h3><p>Заранее опубликованные окна обслуживания не входят в SLO. Неподтверждённые сбои одной точки наблюдения исключаются после автоматической сверки.</p>'));
document.querySelector('[data-api]').addEventListener('click', () => openDetail('JSON API статуса', '<p>Публичный снимок компонентов доступен в формате JSON без ключа. Лимит — 60 запросов в минуту с одного адреса.</p><p><code>GET /public/v1/status</code><br><code>GET /public/v1/incidents</code></p>'));
document.querySelector('[data-rss]').addEventListener('click', () => showToast('Адрес RSS-ленты подготовлен для добавления в программу чтения.'));
document.querySelector('[data-legal]').addEventListener('click', () => openDetail('Политика публикации', '<p>Мы публикуем подтверждённые инциденты, которые влияют на доступность, целостность запросов или задержку ключевых операций. Первое сообщение появляется после определения масштаба влияния.</p>'));

document.querySelectorAll('.subscribe-open').forEach((button) => button.addEventListener('click', openSubscribe));
subscribeModal.querySelector('form').addEventListener('submit', (event) => {
  event.preventDefault();
  subscribeModal.querySelector('.subscribe-form').hidden = true;
  subscribeModal.querySelector('.subscribe-result').hidden = false;
});
subscribeModal.querySelector('.subscribe-close').addEventListener('click', () => { subscribeModal.hidden = true; });

document.querySelectorAll('[data-auth]').forEach((button) => button.addEventListener('click', openAuth));
authModal.querySelector('.auth-login form').addEventListener('submit', (event) => {
  event.preventDefault();
  authModal.querySelector('.auth-login').hidden = true;
  authModal.querySelector('.auth-org').hidden = false;
  authModal.querySelector('.auth-org input').focus();
});
authModal.querySelector('[data-sso]').addEventListener('click', () => {
  authModal.querySelector('.auth-login').hidden = true;
  authModal.querySelector('.auth-org').hidden = false;
});
authModal.querySelector('.auth-org form').addEventListener('submit', (event) => {
  event.preventDefault();
  const error = authModal.querySelector('.auth-error');
  error.textContent = 'Токен не связан с активной организацией или срок приглашения истёк. Доступ не предоставлен.';
  error.hidden = false;
});
authModal.querySelector('[data-back]').addEventListener('click', () => {
  authModal.querySelector('.auth-org').hidden = true;
  authModal.querySelector('.auth-login').hidden = false;
});

[detailModal, subscribeModal, authModal].forEach((modal) => {
  modal.querySelector('.modal-close').addEventListener('click', () => { modal.hidden = true; });
  modal.addEventListener('click', (event) => { if (event.target === modal) modal.hidden = true; });
});
detailModal.querySelector('.detail-close').addEventListener('click', () => { detailModal.hidden = true; });

document.querySelector('.mobile-menu').addEventListener('click', () => document.querySelector('.nav').classList.toggle('open'));
document.querySelectorAll('.nav a').forEach((link) => link.addEventListener('click', () => document.querySelector('.nav').classList.remove('open')));
document.querySelector('.refresh').addEventListener('click', (event) => {
  event.currentTarget.classList.add('spinning');
  document.querySelector('.live').innerHTML = '<i></i>Проверено сейчас';
  showToast('Публичные показатели обновлены');
  setTimeout(() => event.currentTarget.classList.remove('spinning'), 500);
});

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') { drawer.hidden = true; detailModal.hidden = true; subscribeModal.hidden = true; authModal.hidden = true; }
});
