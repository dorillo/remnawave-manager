// Guest screens describe the selected section, rather than an empty account.
const messages = {
  profile: ['Ваш профиль ждёт', 'Войдите, чтобы настроить профиль, участвовать в обсуждениях и собрать своё пространство.', 'Your profile is waiting', 'Sign in to personalise your profile, join discussions and make this space yours.'],
  authors: ['Соберите свой круг авторов', 'Войдите, чтобы выбирать интересных людей и возвращаться к их публикациям.', 'Find your favourite authors', 'Sign in to follow interesting people and return to their posts.'],
  subscriptions: ['Любимые каналы — в одной ленте', 'Войдите, чтобы подписываться на каналы и следить за новыми видео.', 'Your favourite channels in one feed', 'Sign in to subscribe to channels and keep up with new videos.'],
  following: ['Смотрите тех, кто вам интересен', 'Войдите, чтобы подписываться на авторов и собрать свою ленту видео.', 'Keep up with your favourite creators', 'Sign in to follow creators and build your own video feed.'],
  posts: ['Место для ваших историй', 'Войдите, чтобы публиковать записи и находить своих читателей.', 'A place for your stories', 'Sign in to publish posts and find your readers.'],
  reposts: ['Делитесь интересными находками', 'Войдите, чтобы делать репосты и собирать публикации в своём профиле.', 'Share what inspires you', 'Sign in to repost discoveries and collect them on your profile.'],
  drafts: ['Сохраните идеи на потом', 'Войдите, чтобы работать над черновиками и возвращаться к ним перед публикацией.', 'Keep your ideas for later', 'Sign in to work on drafts and return to them before publishing.'],
  savedPosts: ['Ваши находки будут здесь', 'Войдите, чтобы сохранять публикации и легко находить их снова.', 'Keep your discoveries here', 'Sign in to save posts and find them again whenever you like.'],
  likedPosts: ['Соберите понравившиеся публикации', 'Войдите, чтобы отмечать записи и возвращаться к любимым историям.', 'Collect the posts you love', 'Sign in to like posts and return to your favourite stories.'],
  likedVideos: ['Любимые видео будут здесь', 'Войдите, чтобы ставить лайки и возвращаться к понравившимся видео.', 'Your favourite videos belong here', 'Sign in to like videos and watch your favourites again.'],
  savedVideos: ['Сохраняйте видео для себя', 'Войдите, чтобы собрать подборку видео и не потерять интересные находки.', 'Keep videos worth watching', 'Sign in to save videos and build your own collection.'],
  later: ['Отложите просмотр на удобное время', 'Войдите, чтобы добавлять видео в «Смотреть позже» и возвращаться к ним.', 'Make time for a good video', 'Sign in to add videos to Watch later and come back when you are ready.'],
  historyVideo: ['Ваша история просмотров ждёт', 'Войдите, чтобы находить просмотренные видео и продолжать просмотр.', 'Your watch history is waiting', 'Sign in to find videos you have watched and pick up where you left off.'],
  uploadsVideo: ['Здесь начнутся ваши видео', 'Войдите, чтобы загружать видео и управлять своими публикациями.', 'Your videos start here', 'Sign in to upload videos and manage your posts.'],
  questions: ['Ваши вопросы ждут своего места', 'Войдите, чтобы задавать вопросы, следить за ответами и управлять публикациями.', 'A place for your questions', 'Sign in to ask questions, follow answers and manage your posts.'],
  answers: ['Поделитесь своими знаниями', 'Войдите, чтобы отвечать на вопросы и находить свои ответы в профиле.', 'Share what you know', 'Sign in to answer questions and find your answers on your profile.'],
  notifications: ['Не пропускайте новые ответы', 'Войдите, чтобы узнавать об ответах и продолжать обсуждения.', 'Keep the conversation going', 'Sign in to keep track of replies and continue your discussions.'],
  savedQuestions: ['Сохраните интересные вопросы', 'Войдите, чтобы добавлять вопросы в избранное и возвращаться к обсуждениям.', 'Save interesting questions', 'Sign in to save questions and return to their discussions.'],
  library: ['Ваша библиотека ещё не открыта', 'Войдите, чтобы сохранять статьи и объединять их в собственные подборки.', 'Your library is waiting', 'Sign in to save articles and organise them into collections.'],
  historyReading: ['История чтения пока недоступна', 'Войдите, чтобы сохранять историю чтения и возвращаться к изученным темам.', 'Your reading history is waiting', 'Sign in to keep your reading history and revisit topics you have explored.'],
  likedMedia: ['Любимые реакции будут здесь', 'Войдите, чтобы отмечать понравившиеся GIF, стикеры и клипы.', 'Keep your favourite reactions', 'Sign in to like GIFs, stickers and clips.'],
  collections: ['Создавайте свои коллекции', 'Войдите, чтобы собирать GIF, стикеры и клипы по темам и настроению.', 'Create your own collections', 'Sign in to collect GIFs, stickers and clips by topic or mood.'],
  uploadsMedia: ['Добавьте что-то своё', 'Войдите, чтобы загружать материалы и управлять своей подборкой.', 'Make it your own', 'Sign in to upload media and manage your collection.'],
  savedNews: ['Сохраните важное', 'Войдите, чтобы добавлять новости в избранное и читать их в удобное время.', 'Keep the stories that matter', 'Sign in to save news and read it whenever you have time.'],
  historyNews: ['Возвращайтесь к прочитанному', 'Войдите, чтобы видеть историю чтения и снова находить интересные новости.', 'Return to the stories you have read', 'Sign in to see your reading history and find interesting news again.'],
  comments: ['Ваши обсуждения — в одном месте', 'Войдите, чтобы находить свои комментарии и продолжать разговор.', 'Your conversations in one place', 'Sign in to find your comments and continue the conversation.'],
  reactions: ['Соберите то, что вас заинтересовало', 'Войдите, чтобы отмечать новости и видеть свои реакции в профиле.', 'Keep track of what moves you', 'Sign in to react to news and find your reactions on your profile.'],
};

export function guestPrompt(kind = 'profile', onLogin, language = document.documentElement.lang) {
  const english = language.startsWith('en'), copy = messages[kind] || messages.profile;
  const section = document.createElement('section');
  section.className = 'guest-prompt';
  section.dataset.guestSection = kind;
  const symbol = document.createElement('span');
  symbol.className = 'guest-symbol';
  symbol.setAttribute('aria-hidden', 'true');
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  for (const [name, value] of Object.entries({ viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '1.7', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' })) svg.setAttribute(name, value);
  const path = document.createElementNS(svg.namespaceURI, 'path');
  path.setAttribute('d', 'M20 21v-2a6 6 0 0 0-6-6h-4a6 6 0 0 0-6 6v2M16 6a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z');
  svg.append(path); symbol.append(svg);
  const title = document.createElement('h2'), description = document.createElement('p'), button = document.createElement('button');
  title.textContent = copy[english ? 2 : 0];
  description.textContent = copy[english ? 3 : 1];
  button.type = 'button';
  button.className = 'button primary guest-login';
  button.textContent = english ? 'Sign in or create a profile' : 'Войти или создать профиль';
  if (onLogin) button.addEventListener('click', onLogin);
  section.append(symbol, title, description, button);
  return section;
}

export function guestMarkup(kind, action) {
  const node = guestPrompt(kind);
  node.querySelector('button').dataset.action = action;
  return node.outerHTML;
}

const emptyMessages = {
  "likedVideos": [
    "Пока нет понравившихся видео",
    "Отмечайте видео сердцем — они появятся здесь.",
    "No liked videos yet",
    "Like videos to find them here."
  ],
  "later": [
    "Список просмотра пока пуст",
    "Добавляйте видео в «Смотреть позже», чтобы не потерять их.",
    "Your watch list is empty",
    "Add videos to Watch later to find them here."
  ],
  "savedVideos": [
    "Пока нет сохранённых видео",
    "Сохраняйте интересные видео и возвращайтесь к ним.",
    "No saved videos yet",
    "Save interesting videos and return to them here."
  ],
  "historyVideo": [
    "Вы ещё не смотрели видео",
    "Начните просмотр — история появится здесь.",
    "No watch history yet",
    "Start watching and your history will appear here."
  ],
  "following": [
    "Вы пока ни на кого не подписаны",
    "Подписывайтесь на интересных авторов, чтобы видеть их новые видео.",
    "No subscriptions yet",
    "Follow creators to keep up with their new videos."
  ],
  "questions": [
    "Вы ещё не задавали вопросов",
    "Задайте первый вопрос и начните обсуждение.",
    "No questions yet",
    "Ask your first question and start a discussion."
  ],
  "savedQuestions": [
    "Пока нет сохранённых вопросов",
    "Нажмите закладку у вопроса, чтобы вернуться к нему позже.",
    "No saved questions yet",
    "Bookmark a question to return to it later."
  ],
  "answers": [
    "Вы ещё не отвечали на вопросы",
    "Поделитесь знаниями — ваши ответы появятся здесь.",
    "No answers yet",
    "Share what you know and find your answers here."
  ],
  "notifications": [
    "Пока нет новых ответов",
    "Здесь появятся уведомления об ответах на ваши вопросы.",
    "No new replies yet",
    "Replies to your questions will appear here."
  ],
  "likedMedia": [
    "Пока нет понравившихся материалов",
    "Отмечайте GIF, стикеры и клипы сердцем, чтобы собрать любимое.",
    "No favourites yet",
    "Like GIFs, stickers and clips to collect your favourites."
  ],
  "collections": [
    "У вас пока нет коллекций",
    "Создайте коллекцию и добавьте в неё любимые материалы.",
    "No collections yet",
    "Create a collection and add your favourite media."
  ],
  "collection": [
    "В коллекции пока пусто",
    "Добавляйте материалы через кнопку сохранения.",
    "This collection is empty",
    "Use the save button to add media here."
  ],
  "uploads": [
    "Вы ещё ничего не загрузили",
    "Добавьте первый материал — он появится в этом разделе.",
    "No uploads yet",
    "Upload your first item to see it here."
  ],
  "savedNews": [
    "Пока нет сохранённых новостей",
    "Нажмите закладку у новости, чтобы вернуться к ней позже.",
    "No saved news yet",
    "Bookmark a story to read it later."
  ],
  "historyNews": [
    "Вы ещё не читали новости",
    "Откройте интересную новость — она появится в истории.",
    "No reading history yet",
    "Open a story to start your reading history."
  ],
  "comments": [
    "Вы ещё не оставляли комментариев",
    "Присоединитесь к обсуждению новости — ваши комментарии появятся здесь.",
    "No comments yet",
    "Join a discussion to find your comments here."
  ],
  "reactions": [
    "Вы ещё не ставили реакций",
    "Отмечайте новости, которые вас заинтересовали.",
    "No reactions yet",
    "React to stories that interest you."
  ]
};

export function emptyPrompt(kind, onExplore, language = document.documentElement.lang) {
  const node = guestPrompt('profile', onExplore, language);
  const copy = emptyMessages[kind];
  const english = language.startsWith('en');
  node.removeAttribute('data-guest-section');
  node.dataset.emptySection = kind;
  node.querySelector('h2').textContent = copy[english ? 2 : 0];
  node.querySelector('p').textContent = copy[english ? 3 : 1];
  const button = node.querySelector('button');
  button.classList.remove('guest-login');
  button.classList.add('empty-action');
  button.textContent = english ? 'Explore' : 'Перейти в обзор';
  if (!onExplore) button.remove();
  return node;
}
export function emptyMarkup(kind, href = '#/home', action = '') {
  const node = emptyPrompt(kind, () => {});
  const button = node.querySelector('button');
  if (action) { button.dataset.action = action; button.textContent = document.documentElement.lang.startsWith('en') ? 'Ask a question' : 'Задать вопрос'; }
  else { const link = document.createElement('a'); link.className = button.className; link.href = href; link.textContent = button.textContent; button.replaceWith(link); }
  return node.outerHTML;
}
