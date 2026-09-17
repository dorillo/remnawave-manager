const words = {
  contentLicense: ["Текст: CC BY-SA 4.0", "Text: CC BY-SA 4.0"],
  adapted: [
    "Оформление адаптировано для «Свода». У изображений могут быть отдельные условия использования.",
    "Presentation adapted for Svod. Images may have separate terms of use.",
  ],
  authors: ["Авторы и история изменений", "Authors and revision history"],
  storageTitle: ["Локальные данные недоступны", "Local data is unavailable"],
  brand: ["Свод", "Svod"],
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
  logoutConfirm: [
    "Выйти из локального аккаунта?",
    "Sign out of this local account?",
  ],
  account: ["Аккаунт", "Account"],
  register: ["Создать аккаунт", "Create account"],
  name: ["Имя", "Name"],
  username: ["Логин", "Username"],
  password: ["Пароль", "Password"],
  passwordHint: ["От 8 до 128 символов", "8–128 characters"],
  localHint: [
    "Аккаунт хранится только в этом браузере. История и библиотека не синхронизируются между устройствами. Очистка данных сайта удалит их.",
    "Your account is stored in this browser only. History and library do not sync across devices. Clearing site data removes them.",
  ],
  noRecovery: [
    "Пароль нельзя восстановить через почту.",
    "There is no email password recovery.",
  ],
  localProfile: ["Локальный профиль", "Local profile"],
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
    "Не удалось сохранить данные браузера. Проверьте доступное место и настройки хранения.",
    "Could not save browser data. Check available space and storage settings.",
  ],
  secureError: [
    "Для аккаунтов откройте сайт по HTTPS или на localhost.",
    "Accounts require HTTPS or localhost.",
  ],
  loading: ["Загружаем…", "Loading…"],
  unavailable: ["Не удалось загрузить материал", "Unable to load content"],
  unavailableHint: [
    "Источник сейчас недоступен. Попробуйте ещё раз немного позже.",
    "The source is currently unavailable. Please try again shortly.",
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
  source: ["Читать в Википедии ↗", "Read on Wikipedia ↗"],
  sourceShort: ["Источник: Википедия", "Source: Wikipedia"],
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
  selectedHint: [
    "Подборка «Свода» · материалы Википедии",
    "Selected by Svod · articles from Wikipedia",
  ],
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
    "Сохранённые статьи и история чтения будут доступны в вашем локальном профиле.",
    "Saved articles and reading history will be available in your local profile.",
  ],
  stale: ["Сохранённая копия от", "Saved copy from"],
  noImage: ["Изображение недоступно", "Image unavailable"],
  zoom: ["Увеличить изображение", "Enlarge image"],
  skip: ["Перейти к содержимому", "Skip to content"],
  menu: ["Навигация", "Navigation"],
  reading: ["Чтение", "Reading"],
  aboutText: [
    "«Свод» — независимый интерфейс для чтения русскоязычных материалов Википедии. Поиск и статьи загружаются из источника; библиотека и история остаются в вашем браузере.",
    "Svod is an independent reader for Russian-language Wikipedia articles. Search and articles load from the source; your library and history stay in your browser.",
  ],
  attribution: [
    "Авторы, история изменений и условия использования текста и изображений указаны на странице оригинала и страницах файлов.",
    "Authors, revision history and terms for text and images are listed on the original article and file pages.",
  ],
  russian: ["Статьи на русском языке", "Articles are in Russian"],
  noItems: ["Здесь пока нет статей", "No articles here yet"],
  done: ["Сохранено", "Saved"],
  removed: ["Удалено", "Removed"],
  revision: ["Версия", "Revision"],
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
  offlineLibrary: [
    "Сохранение добавляет закладку. Для загрузки полного текста обычно нужен интернет.",
    "Saving adds a bookmark. Loading full articles usually requires an internet connection.",
  ],
  unavailableStorage: [
    "Локальное хранилище недоступно. Читать статьи по-прежнему можно.",
    "Local storage is unavailable. You can still read articles.",
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
  theme: ["light", "dark", "system"].includes(preferences.theme)
    ? preferences.theme
    : "system",
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
