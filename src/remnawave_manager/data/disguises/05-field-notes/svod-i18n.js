const words = {
  storageTitle: ["Данные недоступны", "Data is unavailable"],
  brand: ["Svod", "Svod"],
  tagline: ["Открывая знания", "Discovering knowledge"],
  home: ["Главная", "Home"],
  search: ["Поиск", "Search"],
  searchHint: ["Найти статью", "Search articles"],
  random: ["Случайная статья", "Random article"],
  library: ["Моя библиотека", "My library"],
  history: ["История чтения", "Reading history"],
  about: ["О проекте", "About"],
  login: ["Войти", "Sign in"],
  logout: ["Выйти", "Sign out"],
  logoutConfirm: ["Выйти из аккаунта?", "Sign out of this account?"],
  account: ["Аккаунт", "Account"],
  register: ["Создать аккаунт", "Create account"],
  name: ["Имя", "Name"],
  username: ["Логин", "Username"],
  password: ["Пароль", "Password"],
  passwordHint: ["От 8 до 128 символов", "8–128 characters"],
  close: ["Закрыть", "Close"],
  cancel: ["Отмена", "Cancel"],
  save: ["Сохранить", "Save"],
  saved: ["В библиотеке", "In library"],
  remove: ["Удалить", "Remove"],
  deleteAccount: ["Удалить аккаунт", "Delete account"],
  deleteConfirm: [
    "Удалить аккаунт вместе с библиотекой и историей? Это действие нельзя отменить.",
    "Delete this account, its library and history? This cannot be undone.",
  ],
  confirm: ["Подтвердить", "Confirm"],
  invalid: [
    "Проверьте поля. Логин: 3–40 букв, цифр, точек, дефисов или подчёркиваний.",
    "Check the fields. Username: 3–40 letters, numbers, dots, hyphens or underscores.",
  ],
  duplicate: ["Этот логин уже занят.", "This username is already taken."],
  authError: ["Неверный логин или пароль.", "Incorrect username or password."],
  storageError: [
    "Не удалось сохранить данные. Повторите попытку.",
    "Could not save data. Please try again.",
  ],
  secureError: [
    "Для аккаунтов откройте сайт по HTTPS или на localhost.",
    "Accounts require HTTPS or localhost.",
  ],
  loading: ["Загружаем…", "Loading…"],
  unavailable: ["Не удалось загрузить материал", "Unable to load content"],
  unavailableHint: [
    "Материал сейчас недоступен. Попробуйте ещё раз немного позже.",
    "The content is currently unavailable. Please try again shortly.",
  ],
  rateLimit: [
    "Слишком много запросов. Попробуйте позже.",
    "Too many requests. Please try again later.",
  ],
  missing: ["Статья не найдена", "Article not found"],
  retry: ["Повторить", "Try again"],
  emptySearch: ["Ничего не найдено", "No results found"],
  emptySearchHint: [
    "Попробуйте другое название или более короткий запрос.",
    "Try a different title or a shorter query.",
  ],
  more: ["Показать ещё", "Show more"],
  contents: ["Содержание", "Contents"],
  beginning: ["Начало", "Overview"],
  article: ["Статья", "Article"],
  category: ["Категория", "Category"],
  categories: ["Категории", "Categories"],
  appearance: ["Внешний вид", "Appearance"],
  theme: ["Тема", "Theme"],
  light: ["Светлая", "Light"],
  dark: ["Тёмная", "Dark"],
  system: ["Системная", "System"],
  font: ["Размер текста", "Text size"],
  standard: ["Обычный", "Standard"],
  large: ["Крупный", "Large"],
  width: ["Ширина текста", "Text width"],
  wide: ["Широкая", "Wide"],
  print: ["Печать", "Print"],
  emptyCollection: ["В этой коллекции пока нет статей", "No articles in this collection yet"],
  emptyCollectionHint: ["Добавляйте сохранённые статьи через кнопку управления коллекциями.", "Add saved articles using the collections button."],
  libraryLogin: ["Войти и собрать библиотеку", "Sign in to build your library"],
  historyLogin: ["Войти и открыть историю", "Sign in to view your history"],
  language: ["Язык интерфейса", "Interface language"],
  welcome: [
    "Мир начинается с вопроса",
    "A world of questions. A place to explore.",
  ],
  welcomeText: [
    "Читайте, исследуйте связи и собирайте свою библиотеку знаний.",
    "Read, follow connections and build your own library of knowledge.",
  ],
  discover: ["С чего начнём?", "Where shall we begin?"],
  discoverText: [
    "Несколько направлений для вашего следующего открытия.",
    "A few paths to your next discovery.",
  ],
  science: ["Наука", "Science"],
  nature: ["Природа", "Nature"],
  culture: ["Культура", "Culture"],
  technology: ["Технологии", "Technology"],
  geography: ["География", "Geography"],
  past: ["История", "History"],
  selected: ["Статьи для знакомства", "Explore these articles"],
  selectedHint: ["Подборка редакции Svod", "Selected by the Svod editors"],
  read: ["Читать статью →", "Read article →"],
  continue: ["Продолжить чтение", "Continue reading"],
  collection: ["Коллекция", "Collection"],
  collectionActions: ["Действия с коллекцией", "Collection actions"],
  collections: ["Коллекции", "Collections"],
  all: ["Все статьи", "All articles"],
  newCollection: ["Новая коллекция", "New collection"],
  collectionName: ["Название коллекции", "Collection name"],
  rename: ["Переименовать", "Rename"],
  manage: ["В коллекцию", "Add to collection"],
  collectionDelete: [
    "Удалить коллекцию? Статьи останутся в библиотеке.",
    "Delete this collection? Articles will remain in your library.",
  ],
  removeSavedConfirm: [
    "Удалить статью из библиотеки? Она также будет удалена из коллекций.",
    "Remove this article from your library? It will also be removed from collections.",
  ],
  emptyLibrary: [
    "Ваша библиотека начинается здесь",
    "Your library starts here",
  ],
  emptyLibraryHint: [
    "Сохраняйте интересные статьи, чтобы вернуться к ним позже.",
    "Save interesting articles to return to them later.",
  ],
  emptyHistory: ["Вы ещё не открывали статьи", "No reading history yet"],
  emptyHistoryHint: [
    "Прочитанные статьи появятся здесь после входа.",
    "Articles you read while signed in will appear here.",
  ],
  clearHistory: ["Очистить историю", "Clear history"],
  clearConfirm: [
    "Очистить всю историю чтения? Библиотека сохранится.",
    "Clear all reading history? Your library will be kept.",
  ],
  filter: ["Поиск в библиотеке", "Search your library"],
  newest: ["Сначала новые", "Newest first"],
  alphabet: ["По названию", "By title"],
  sort: ["Сортировка", "Sort order"],
  accountNeeded: [
    "Войдите, чтобы собрать свою библиотеку",
    "Sign in to build your library",
  ],
  accountNeededHint: [
    "После входа здесь появятся сохранённые статьи и история чтения.",
    "Sign in to access saved articles and reading history.",
  ],
  stale: ["Сохранённая копия от", "Saved copy from"],
  noImage: ["Изображение недоступно", "Image unavailable"],
  zoom: ["Увеличить изображение", "Enlarge image"],
  skip: ["Перейти к содержимому", "Skip to content"],
  menu: ["Навигация", "Navigation"],
  reading: ["Чтение", "Reading"],
  aboutText: [
    "Svod — пространство для чтения, исследования связей и создания собственной библиотеки знаний.",
    "Svod is a place to read, explore connections and build your own library of knowledge.",
  ],
  russian: ["Статьи на русском языке", "Articles are in Russian"],
  noItems: ["Здесь пока нет статей", "No articles here yet"],
  done: ["Сохранено", "Saved"],
  removed: ["Удалено", "Removed"],
  visit: ["Последнее чтение", "Last read"],
  results: ["Результаты поиска", "Search results"],
  libraryHint: [
    "Всё, к чему хочется вернуться.",
    "Everything worth coming back to.",
  ],
  historyHint: [
    "Продолжите с того места, где остановились.",
    "Pick up where you left off.",
  ],
  unavailableStorage: [
    "Сохранение данных сейчас недоступно. Читать статьи по-прежнему можно.",
    "Saving data is currently unavailable. You can still read articles.",
  ],
};
let preferences = {};
try {
  preferences =
    JSON.parse(localStorage.getItem("svod:preferences:v1") || "{}") || {};
} catch {}
export let lang = preferences.lang === "en" ? "en" : "ru";
export const t = (key) => words[key]?.[lang === "en" ? 1 : 0] || key;
export const prefs = {
  theme: "system",
  font: preferences.font === "large" ? "large" : "standard",
  width: preferences.width === "wide" ? "wide" : "standard",
};
export function setting(key, value) {
  if (key === "lang") lang = value === "en" ? "en" : "ru";
  else prefs[key] = value;
  try {
    localStorage.setItem(
      "svod:preferences:v1",
      JSON.stringify({ ...prefs, lang }),
    );
  } catch {}
  apply();
}
export function apply() {
  document.documentElement.lang = lang;
  for (const [key, value] of Object.entries(prefs))
    document.documentElement.dataset[key] = value;
}
export const date = (value) =>
  new Intl.DateTimeFormat(lang, { dateStyle: "medium" }).format(
    new Date(value),
  );
apply();
