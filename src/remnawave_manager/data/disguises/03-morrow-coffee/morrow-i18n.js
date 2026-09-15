const words = {
  brand: ['Морроу', 'Morrow'],
  chats: ['Диалоги', 'Chats'],
  projects: ['Проекты', 'Projects'],
  files: ['Файлы', 'Files'],
  settings: ['Настройки', 'Settings'],
  profile: ['Профиль', 'Profile'],
  newChat: ['Новый диалог', 'New chat'],
  search: ['Поиск диалогов', 'Search chats'],
  guest: ['Гость', 'Guest'],
  login: ['Войти', 'Sign in'],
  register: ['Создать аккаунт', 'Create account'],
  logout: ['Выйти', 'Sign out'],
  welcome: ['С чего начнём?', 'Where shall we begin?'],
  intro: [
    'Место для мыслей, вопросов и новых идей.',
    'A space for thoughts, questions and new ideas.',
  ],
  starter1: [
    'Объясни сложную тему простыми словами',
    'Explain a complex topic simply',
  ],
  starter2: ['Помоги улучшить мой текст', 'Help me improve my writing'],
  starter3: ['Предложи план проекта', 'Suggest a project plan'],
  prompt: ['Сообщение Морроу', 'Message Morrow'],
  send: ['Отправить', 'Send'],
  stop: ['Остановить', 'Stop'],
  thinking: ['Морроу готовит ответ…', 'Morrow is preparing a reply…'],
  disclosure: [
    'Сообщение, контекст диалога и выбранные файлы отправляются Prexzy. Ответы могут содержать ошибки.',
    'Your message, conversation context and selected files are sent to Prexzy. Answers may contain errors.',
  ],
  guestHint: [
    'Войдите, чтобы сохранять диалоги и проекты.',
    'Sign in to save chats and projects.',
  ],
  empty: ['Здесь пока ничего нет', 'Nothing here yet'],
  rename: ['Переименовать', 'Rename'],
  pin: ['Закрепить', 'Pin'],
  unpin: ['Открепить', 'Unpin'],
  delete: ['Удалить', 'Delete'],
  cancel: ['Отмена', 'Cancel'],
  save: ['Сохранить', 'Save'],
  close: ['Закрыть', 'Close'],
  copy: ['Копировать', 'Copy'],
  copied: ['Скопировано', 'Copied'],
  copyFailed: [
    'Не удалось скопировать. Выделите текст вручную.',
    'Could not copy. Select the text manually.',
  ],
  edit: ['Изменить запрос', 'Edit prompt'],
  retry: ['Повторить ответ', 'Retry reply'],
  editWarning: [
    'Ответ и последующие сообщения будут заменены.',
    'The reply and subsequent messages will be replaced.',
  ],
  exportChat: ['Экспорт диалога', 'Export chat'],
  exportData: ['Экспорт данных', 'Export data'],
  importData: ['Импорт данных', 'Import data'],
  importWarning: [
    'Импорт заменит ваши диалоги, проекты, файлы и настройки. Сначала сохраните экспорт.',
    'Import replaces your chats, projects, files and settings. Save an export first.',
  ],
  importDone: ['Данные импортированы', 'Data imported'],
  name: ['Название', 'Name'],
  displayName: ['Имя', 'Display name'],
  description: ['Описание', 'Description'],
  instruction: ['Инструкция для AI', 'AI instructions'],
  newProject: ['Новый проект', 'New project'],
  editProject: ['Изменить проект', 'Edit project'],
  project: ['Проект', 'Project'],
  noProject: ['Без проекта', 'No project'],
  openProject: ['Открыть проект', 'Open project'],
  deleteProjectWarning: [
    'Удалить проект? Его диалоги и файлы сохранятся без проекта.',
    'Delete this project? Its chats and files will remain without a project.',
  ],
  deleteWarning: [
    'Удалить без возможности восстановления?',
    'Delete permanently?',
  ],
  upload: ['Загрузить файлы', 'Upload files'],
  fileHint: [
    'TXT, Markdown, CSV · UTF-8 · до 256 КБ каждый',
    'TXT, Markdown, CSV · UTF-8 · up to 256 KB each',
  ],
  attach: ['Выбрать файлы', 'Select files'],
  selected: ['Выбранные файлы', 'Selected files'],
  preview: ['Просмотр', 'Preview'],
  download: ['Скачать', 'Download'],
  fileContext: [
    'Текст выбранных файлов войдёт в следующий запрос. Общий контекст ограничен 24 000 символов.',
    'Selected files are included in the next request. Total context is limited to 24,000 characters.',
  ],
  language: ['Язык интерфейса', 'Interface language'],
  answerLanguage: ['Язык ответов', 'Answer language'],
  auto: ['Как в запросе', 'Match the prompt'],
  theme: ['Оформление', 'Appearance'],
  system: ['Системное', 'System'],
  light: ['Светлое', 'Light'],
  dark: ['Тёмное', 'Dark'],
  style: ['Стиль ответа', 'Response style'],
  balanced: ['Сбалансированно', 'Balanced'],
  brief: ['Кратко', 'Brief'],
  detailed: ['Подробно', 'Detailed'],
  provider: ['Провайдер', 'Provider'],
  loginName: ['Логин', 'Username'],
  password: ['Пароль', 'Password'],
  loginHint: [
    '3–40 символов: латинские буквы, цифры, _, . или -',
    '3–40 characters: Latin letters, digits, _, . or -',
  ],
  passwordHint: ['От 8 до 128 символов', '8 to 128 characters'],
  avatar: ['Изменить фото', 'Change photo'],
  removeAvatar: ['Убрать фото', 'Remove photo'],
  deleteAccount: ['Удалить аккаунт', 'Delete account'],
  deleteAccountWarning: [
    'Аккаунт и все его данные будут удалены. Для подтверждения введите свой логин.',
    'Your account and all its data will be deleted. Enter your username to confirm.',
  ],
  backupHint: [
    'Экспорт сохраняет переписку и файлы. Храните резервную копию в надёжном месте. Очистка данных сайта удалит сохранённые материалы.',
    'Export includes conversations and files. Keep a backup in a safe place. Clearing site data removes saved materials.',
  ],
  storageUsage: ['Использовано', 'Used'],
  saved: ['Сохранено', 'Saved'],
  menu: ['Меню', 'Menu'],
  back: ['Назад', 'Back'],
  you: ['Вы', 'You'],
  storage: [
    'Не удалось прочитать или сохранить данные. Проверьте доступ к хранилищу браузера.',
    'Could not read or save data. Check browser storage access.',
  ],
  storageFull: [
    'Лимит хранилища — 16 МБ на профиль. Экспортируйте и удалите ненужные материалы.',
    'Storage limit is 16 MB per profile. Export and remove unused materials.',
  ],
  invalidData: [
    'Некорректные данные или превышен лимит записей.',
    'Invalid data or record limit exceeded.',
  ],
  conflict: [
    'Данные изменены в другой вкладке. Перезагрузите страницу перед продолжением.',
    'Data changed in another tab. Reload before continuing.',
  ],
  sessionExpired: [
    'Сессия завершена. Войдите снова.',
    'Session ended. Sign in again.',
  ],
  invalidLogin: [
    'Некорректный логин. Используйте 3–40 латинских букв, цифр, _, . или -.',
    'Invalid username. Use 3–40 Latin letters, digits, _, . or -.',
  ],
  invalidPassword: [
    'Пароль должен содержать от 8 до 128 символов.',
    'Password must contain 8 to 128 characters.',
  ],
  credentials: [
    'Неверный логин или пароль.',
    'Incorrect username or password.',
  ],
  duplicate: ['Этот логин уже занят.', 'This username is already taken.'],
  secureContext: [
    'Для входа требуется HTTPS или localhost.',
    'Sign-in requires HTTPS or localhost.',
  ],
  fileType: [
    'Поддерживаются файлы TXT, MD и CSV.',
    'Supported files: TXT, MD and CSV.',
  ],
  fileSize: ['Файл превышает 256 КБ.', 'File exceeds 256 KB.'],
  fileEncoding: [
    'Нужен текстовый файл в кодировке UTF-8.',
    'A UTF-8 text file is required.',
  ],
  avatarError: [
    'Выберите PNG, JPEG или WebP до 5 МБ.',
    'Choose PNG, JPEG or WebP up to 5 MB.',
  ],
  contextLimit: [
    'Контекст превышает 24 000 символов. Уберите вложения или начните новый диалог.',
    'Context exceeds 24,000 characters. Remove attachments or start a new chat.',
  ],
  rateLimit: [
    'Лимит запросов. Подождите и повторите попытку.',
    'Rate limit reached. Wait and try again.',
  ],
  providerAccess: [
    'Провайдер ограничил доступ к генерации. Попробуйте позже.',
    'The provider restricted generation access. Try again later.',
  ],
  unavailable: [
    'AI-сервис временно недоступен. Повторите запрос позже.',
    'AI service is temporarily unavailable. Retry later.',
  ],
  invalidResponse: [
    'Провайдер вернул пустой или некорректный ответ.',
    'The provider returned an empty or invalid response.',
  ],
  timeout: [
    'Время ожидания ответа истекло. Можно повторить запрос.',
    'Response timed out. You can retry.',
  ],
  cancelled: ['Генерация остановлена', 'Generation stopped'],
  network: [
    'Нет соединения с AI-сервисом. Проверьте интернет и повторите запрос.',
    'Cannot reach the AI service. Check your connection and retry.',
  ],
};
let language = navigator.language.startsWith('ru') ? 'ru' : 'en';
export function setLanguage(value) {
  language = value === 'ru' ? 'ru' : 'en';
  document.documentElement.lang = language;
}
export const getLanguage = () => language;
export const t = (key) =>
  words[key]?.[language === 'ru' ? 0 : 1] ||
  words.unavailable[language === 'ru' ? 0 : 1];
export const date = (value) =>
  new Intl.DateTimeFormat(language, { dateStyle: 'medium' }).format(value);
