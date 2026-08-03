const login = document.querySelector('.login');
const threadModal = document.querySelector('.thread-modal');
const infoModal = document.querySelector('.info-modal');
const toast = document.querySelector('.toast');
const search = document.querySelector('#search');
let topic = 'all';
let toastTimer;

function showToast(text) {
  clearTimeout(toastTimer);
  toast.textContent = text;
  toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, 2900);
}

function openAuth(event) {
  event?.preventDefault();
  login.querySelector('.login-main').hidden = false;
  login.querySelector('.login-invite').hidden = true;
  login.querySelector('.login-error').hidden = true;
  login.hidden = false;
  login.querySelector('input').focus();
}

function openInfo(title, body) {
  infoModal.querySelector('h2').textContent = title;
  infoModal.querySelector('.info-body').innerHTML = body;
  infoModal.hidden = false;
  infoModal.querySelector('.modal-close').focus();
}

function openThread(source) {
  const title = source.querySelector?.('h1,h2,h3,a')?.textContent.trim() || source.textContent.trim();
  const lead = source.querySelector?.('p')?.textContent.trim() || 'Практический материал с примерами и обсуждением сообщества.';
  const meta = source.querySelector?.('.thread-body footer')?.textContent.replace(/\s+/g, ' ').trim() || 'Материал из открытой ленты сообщества';
  threadModal.querySelector('h2').textContent = title;
  threadModal.querySelector('.thread-lead').textContent = lead;
  threadModal.querySelector('.thread-meta').textContent = meta;
  threadModal.hidden = false;
  threadModal.querySelector('.modal-close').focus();
}

document.querySelectorAll('[data-auth]').forEach((button) => button.addEventListener('click', openAuth));
document.querySelectorAll('.vote button').forEach((button) => button.addEventListener('click', openAuth));
login.querySelector('.close').addEventListener('click', () => { login.hidden = true; });
login.addEventListener('click', (event) => { if (event.target === login) login.hidden = true; });
login.querySelector('.login-main form').addEventListener('submit', (event) => {
  event.preventDefault();
  login.querySelector('.login-main').hidden = true;
  login.querySelector('.login-invite').hidden = false;
  login.querySelector('.login-invite input').focus();
});
login.querySelector('.login-invite form').addEventListener('submit', (event) => {
  event.preventDefault();
  const error = login.querySelector('.login-error');
  error.textContent = 'Код отсутствует в реестре участников, подтверждённых до 1 августа. Вход пока недоступен.';
  error.hidden = false;
});
login.querySelector('[data-register]').addEventListener('click', () => {
  login.hidden = true;
  showToast('Новые приглашения начнут выдавать после завершения переноса репутации. Окно заявок откроется 12 августа.');
});
login.querySelector('[data-back]').addEventListener('click', () => {
  login.querySelector('.login-invite').hidden = true;
  login.querySelector('.login-main').hidden = false;
});

function filter() {
  const query = search.value.trim().toLocaleLowerCase('ru');
  let count = 0;
  document.querySelectorAll('.thread').forEach((thread) => {
    const visible = (topic === 'all' || thread.dataset.topic === topic) && (!query || thread.dataset.search.includes(query));
    thread.hidden = !visible;
    if (visible) count += 1;
  });
  document.querySelector('.empty').hidden = count !== 0;
  document.querySelector('#feed-count').textContent = `${count} ${count === 1 ? 'публикация' : count < 5 ? 'публикации' : 'публикаций'}`;
}

document.querySelectorAll('[data-topic]').forEach((button) => button.addEventListener('click', () => {
  topic = button.dataset.topic;
  filter();
  document.querySelector('#questions').scrollIntoView({ behavior: 'smooth' });
}));
search.addEventListener('input', filter);

function sortFeed(mode) {
  document.querySelectorAll('[data-sort]').forEach((item) => item.classList.toggle('active', item.dataset.sort === mode));
  const feed = document.querySelector('.feed');
  const threads = [...feed.querySelectorAll('.thread')];
  if (mode === 'new') threads.sort((a, b) => Number(a.dataset.time) - Number(b.dataset.time));
  else if (mode === 'unanswered') threads.sort((a, b) => Number(a.dataset.answers) - Number(b.dataset.answers));
  else threads.sort((a, b) => Number(b.querySelector('.vote b').textContent) - Number(a.querySelector('.vote b').textContent));
  threads.forEach((thread) => feed.insertBefore(thread, feed.querySelector('.empty')));
  document.querySelector('#questions').scrollIntoView({ behavior: 'smooth' });
}
document.querySelectorAll('[data-sort]').forEach((button) => button.addEventListener('click', () => sortFeed(button.dataset.sort)));

document.querySelectorAll('.thread').forEach((thread) => thread.addEventListener('click', (event) => {
  if (event.target.closest('.vote button')) return;
  openThread(thread);
}));
document.querySelectorAll('.weekly a').forEach((link) => link.addEventListener('click', (event) => {
  event.preventDefault();
  openThread(link.closest('article'));
}));

let extraLoaded = false;
document.querySelector('.load').addEventListener('click', (event) => {
  if (extraLoaded) { showToast('Все публикации за сегодня уже показаны'); return; }
  const feed = document.querySelector('.feed');
  const extra = document.createElement('article');
  extra.className = 'thread';
  extra.dataset.topic = 'backend';
  extra.dataset.time = '205';
  extra.dataset.answers = '12';
  extra.dataset.search = 'очереди идемпотентность backend события повторная доставка';
  extra.innerHTML = '<div class="vote"><button aria-label="Поддержать">△</button><b>33</b><button aria-label="Понизить рейтинг">▽</button></div><div class="thread-body"><header><span class="tag backend">Backend</span><span class="tag postgres">Архитектура</span><time>3 часа назад</time></header><h2>Идемпотентность обработчиков: где хранить ключи повторной доставки?</h2><p>Сравниваем отдельную таблицу, ключ в бизнес-сущности и дедупликацию на уровне брокера для событий с длинным сроком жизни.</p><footer><span class="avatar av-one">ТВ</span><b>Тимур Валеев</b><i>6 230 репутации</i><span class="answers">12 ответов</span><span>◉ 906</span></footer></div>';
  extra.querySelectorAll('.vote button').forEach((button) => button.addEventListener('click', openAuth));
  extra.addEventListener('click', (clickEvent) => { if (!clickEvent.target.closest('.vote button')) openThread(extra); });
  feed.insertBefore(extra, feed.querySelector('.empty'));
  extraLoaded = true;
  event.currentTarget.textContent = 'Все публикации загружены';
  filter();
  showToast('Добавлена ещё одна публикация из сегодняшней ленты');
});

document.querySelector('.all-topics').addEventListener('click', () => openInfo('Все темы', '<p><b>Разработка:</b> Frontend, Backend, Mobile, DevOps, Data & ML, тестирование.</p><p><b>Практика:</b> архитектура, управление командой, карьера, технические тексты и образование.</p><p>Выберите тему в левой колонке, чтобы отфильтровать открытую ленту.</p>'));
document.querySelectorAll('.events article').forEach((card) => card.addEventListener('click', () => openInfo(card.querySelector('strong').textContent, `<p>${card.querySelector('span').textContent}</p><p>Программа включает 45 минут разбора, вопросы участников и краткий список материалов. Регистрация доступна подтверждённым участникам сообщества.</p><button class="inline-auth">Зарегистрироваться</button>`)));

const info = {
  Статьи: '<p>Редакционные статьи, технические разборы и отчёты команд собраны в общей ленте. Материалы проходят проверку примеров и источников.</p>',
  Команды: '<p>Публичные страницы инженерных команд рассказывают о стеке, процессах и открытых проектах. Создать страницу можно после подтверждения организации.</p>',
  События: '<p><b>7 августа · онлайн</b> — открытый разбор архитектуры.<br><b>12 августа · Казань</b> — Frontend-митап.<br><b>20 августа · онлайн</b> — клуб технических авторов.</p>',
  Правила: '<p>Публикуйте воспроизводимые примеры, указывайте контекст и критикуйте решения, а не людей. Реклама, сбор персональных данных и ответы без раскрытия конфликта интересов удаляются.</p>',
  Помощь: '<p>Справочный раздел содержит требования к вопросам, руководство по форматированию кода и порядок обжалования модерации.</p>',
  'О проекте': '<p>Код.Круг — независимое инженерное сообщество для подробных вопросов, практических статей и открытых технических встреч.</p>'
};

document.querySelectorAll('a[href^="#"]').forEach((link) => link.addEventListener('click', (event) => {
  if (link.hasAttribute('data-auth')) return;
  const target = link.getAttribute('href');
  if (target !== '#' && document.querySelector(target)) return;
  event.preventDefault();
  const title = link.textContent.trim();
  if (target === '#fresh') { sortFeed('new'); return; }
  if (target === '#discussions') { sortFeed('unanswered'); return; }
  if (target === '#featured') { openThread(document.querySelector('.welcome')); return; }
  openInfo(title, info[title] || '<p>Раздел доступен в открытой части сообщества. Расширенные действия требуют подтверждённого аккаунта участника.</p>');
}));

threadModal.querySelector('.modal-close').addEventListener('click', () => { threadModal.hidden = true; });
threadModal.querySelector('.thread-close').addEventListener('click', () => { threadModal.hidden = true; });
threadModal.addEventListener('click', (event) => { if (event.target === threadModal) threadModal.hidden = true; });
infoModal.querySelector('.modal-close').addEventListener('click', () => { infoModal.hidden = true; });
infoModal.querySelector('.info-close').addEventListener('click', () => { infoModal.hidden = true; });
infoModal.addEventListener('click', (event) => { if (event.target === infoModal) infoModal.hidden = true; });
infoModal.addEventListener('click', (event) => { if (event.target.matches('.inline-auth')) openAuth(event); });

document.querySelector('.mobile-menu').addEventListener('click', () => document.querySelector('.left').classList.toggle('open'));
document.querySelectorAll('.left a,.left button').forEach((item) => item.addEventListener('click', () => document.querySelector('.left').classList.remove('open')));
document.addEventListener('keydown', (event) => {
  if (event.key === '/' && document.activeElement !== search) { event.preventDefault(); search.focus(); }
  if (event.key === 'Escape') { login.hidden = true; threadModal.hidden = true; infoModal.hidden = true; }
});
