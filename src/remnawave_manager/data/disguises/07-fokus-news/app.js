const searchModal = document.querySelector('.search-modal');
const articleModal = document.querySelector('.article-modal');
const authModal = document.querySelector('.auth-modal');
const toast = document.querySelector('.toast');
let toastTimer;

function showToast(text) {
  clearTimeout(toastTimer);
  toast.textContent = text;
  toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, 3200);
}

function openAuth() {
  authModal.querySelector('.auth-login').hidden = false;
  authModal.querySelector('.auth-code').hidden = true;
  authModal.querySelector('.auth-error').hidden = true;
  authModal.hidden = false;
  authModal.querySelector('input').focus();
}

function openArticle(source) {
  const titleNode = source.matches('h1,h3,a') ? source : source.querySelector('h1,h3,a');
  const title = titleNode?.textContent.trim() || source.textContent.trim();
  const category = source.querySelector?.('.category')?.textContent.trim() || source.querySelector?.(':scope > span')?.textContent.trim() || 'МАТЕРИАЛ';
  const lead = source.querySelector?.('p')?.textContent.trim() || 'Краткая новость текущего выпуска.';
  articleModal.querySelector('.article-category').textContent = category;
  articleModal.querySelector('h2').textContent = title;
  articleModal.querySelector('.article-lead').textContent = lead;
  articleModal.querySelector('.article-detail').textContent = `По теме «${title}» редакция собрала хронологию, справочные данные и мнения профильных специалистов. Полная версия доступна в текущем выпуске; существенные дополнения будут отмечены в ленте обновлений.`;
  articleModal.hidden = false;
  articleModal.querySelector('.modal-close').focus();
}

document.querySelector('.breaking-close').addEventListener('click', () => { document.querySelector('.breaking').hidden = true; });
document.querySelector('.menu').addEventListener('click', () => document.querySelector('.masthead > nav').classList.toggle('open'));
document.querySelector('.search-open').addEventListener('click', () => { searchModal.hidden = false; searchModal.querySelector('input').focus(); });
searchModal.querySelector('label button').addEventListener('click', () => { searchModal.hidden = true; });
searchModal.addEventListener('click', (event) => { if (event.target === searchModal) searchModal.hidden = true; });

const searchable = [...document.querySelectorAll('h1,h3,.latest a')];
searchModal.querySelector('input').addEventListener('input', (event) => {
  const query = event.target.value.trim().toLocaleLowerCase('ru');
  const results = searchModal.querySelector('.results');
  results.textContent = '';
  if (query.length < 2) return;
  const matches = searchable.filter((item) => item.textContent.toLocaleLowerCase('ru').includes(query)).slice(0, 6);
  matches.forEach((item) => {
    const button = document.createElement('button');
    button.textContent = item.textContent;
    button.addEventListener('click', () => { searchModal.hidden = true; openArticle(item.closest('article') || item); });
    results.append(button);
  });
  if (!matches.length) results.innerHTML = '<p>В текущем выпуске совпадений нет. Попробуйте название рубрики или фамилию автора.</p>';
});

document.querySelectorAll('[data-category]').forEach((button) => {
  if (button.tagName !== 'BUTTON') return;
  button.addEventListener('click', () => {
    document.querySelectorAll('.section-filter button').forEach((item) => item.classList.remove('active'));
    button.classList.add('active');
    document.querySelectorAll('.story-grid article').forEach((story) => { story.hidden = button.dataset.category !== 'all' && story.dataset.category !== button.dataset.category; });
  });
});

document.querySelector('.bookmark').addEventListener('click', openAuth);
document.querySelectorAll('[data-auth]').forEach((button) => button.addEventListener('click', openAuth));
document.querySelectorAll('[data-subscribe]').forEach((button) => button.addEventListener('click', () => document.querySelector('.newsletter').scrollIntoView({ behavior: 'smooth' })));
document.querySelector('.newsletter form').addEventListener('submit', (event) => {
  event.preventDefault();
  showToast('Адрес принят на проверку. Первое письмо придёт после подтверждения редакцией домена отправителя.');
  event.currentTarget.reset();
});

authModal.querySelector('.auth-login form').addEventListener('submit', (event) => {
  event.preventDefault();
  authModal.querySelector('.auth-login').hidden = true;
  authModal.querySelector('.auth-code').hidden = false;
  authModal.querySelector('.auth-code input').focus();
});
authModal.querySelector('.auth-code form').addEventListener('submit', (event) => {
  event.preventDefault();
  const error = authModal.querySelector('.auth-error');
  error.textContent = 'Код не найден среди активных подписок текущего выпуска. Проверьте карточку или дождитесь открытия общей регистрации.';
  error.hidden = false;
});
authModal.querySelector('[data-register]').addEventListener('click', () => {
  authModal.hidden = true;
  showToast('Новые аккаунты подключаются партиями. Заявка откроется после завершения переноса архива 10 августа.');
});
authModal.querySelector('[data-back]').addEventListener('click', () => {
  authModal.querySelector('.auth-code').hidden = true;
  authModal.querySelector('.auth-login').hidden = false;
});

document.querySelectorAll('.modal-close').forEach((button) => button.addEventListener('click', () => { button.closest('.article-modal,.auth-modal').hidden = true; }));
document.querySelector('.article-close').addEventListener('click', () => { articleModal.hidden = true; });
[articleModal, authModal].forEach((modal) => modal.addEventListener('click', (event) => { if (event.target === modal) modal.hidden = true; }));

document.querySelectorAll('.lead-story,.story-grid article,.columns article').forEach((card) => card.addEventListener('click', (event) => {
  if (event.target.closest('button')) return;
  event.preventDefault();
  openArticle(card);
}));
document.querySelectorAll('.latest a').forEach((link) => link.addEventListener('click', (event) => {
  event.preventDefault();
  openArticle(link.closest('article') || link);
}));
document.querySelector('.latest header button').addEventListener('click', () => openArticle(document.querySelector('.latest')));
document.querySelector('.explain button').addEventListener('click', () => openArticle(document.querySelector('.explain')));

document.querySelectorAll('a[href^="#"]').forEach((link) => link.addEventListener('click', (event) => {
  const target = link.getAttribute('href');
  if (target !== '#' && document.querySelector(target)) return;
  event.preventDefault();
  openArticle(link.closest('article,section,div') || link);
}));

document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') { searchModal.hidden = true; articleModal.hidden = true; authModal.hidden = true; }
});
