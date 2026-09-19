import { storage } from '../shared/storage.js';

const words = {
  general: ['Все видео', 'All videos'],
  movies: ['Фильмы', 'Movies'],
  series: ['Сериалы', 'Series'],
  music: ['Музыка', 'Music'],
  entertainment: ['Развлечения', 'Entertainment'],
  kids: ['Мультфильмы', 'Animation'],
  sport: ['Спорт', 'Sport'],
  news: ['Новости', 'News'],
  forYou: ['Для вас', 'For you'],
  recommended: ['Рекомендации', 'Recommended'],
  views: ['просмотров', 'views'],
  likeVideo: ['Нравится', 'Like'],
  comments: ['Комментарии', 'Comments'],
  writeComment: ['Напишите комментарий…', 'Write a comment…'],
  writeReply: ['Напишите ответ…', 'Write a reply…'],
  editComment: ['Измените комментарий…', 'Edit comment…'],
  sendComment: ['Отправить', 'Post comment'],
  reply: ['Ответить', 'Reply'],
  edit: ['Редактировать', 'Edit'],
  likeComment: ['Нравится комментарий', 'Like comment'],
  loadingComments: ['Загружаем комментарии…', 'Loading comments…'],
  commentsUnavailable: ['Не удалось загрузить комментарии. Сохранённые комментарии показаны, если доступны.', 'Could not load comments. Saved comments are shown when available.'],
  commentsRetry: ['Повторить загрузку комментариев', 'Retry loading comments'],
  loadReplies: ['Загрузить ответы', 'Load replies'],
  firstComment: ['Добавьте первый комментарий.', 'Write the first comment.'],
  moreComments: ['Показать ещё комментарии', 'Show more comments'],
  pinned: ['Закреплено', 'Pinned'],
  share: ['Поделиться', 'Share'],
  copied: ['Ссылка скопирована', 'Link copied'],
  home: ['Главная', 'Home'],
  catalog: ['Каталог', 'Explore'],
  subscriptions: ['Подписки', 'Subscriptions'],
  history: ['История', 'History'],
  likes: ['Понравившиеся', 'Liked videos'],
  later: ['Смотреть позже', 'Watch later'],
  profile: ['Профиль', 'Profile'],
  login: ['Войти', 'Sign in'],
  logout: ['Выйти', 'Sign out'],
  register: ['Создать профиль', 'Create profile'],
  email: ['Электронная почта', 'Email'],
  password: ['Пароль', 'Password'],
  name: ['Имя', 'Name'],
  save: ['Сохранить', 'Save'],
  cancel: ['Отмена', 'Cancel'],
  close: ['Закрыть', 'Close'],
  search: ['Поиск видео и каналов', 'Search videos and channels'],
  all: ['Все темы', 'All topics'],
  science: ['Наука', 'Science'],
  travel: ['Путешествия', 'Travel'],
  culture: ['Культура', 'Culture'],
  education: ['Лекции', 'Lectures'],
  city: ['Города', 'Cities'],
  welcome: [
    'Истории, которым стоит уделить время.',
    'Stories worth your time.',
  ],
  intro: [
    'Открывайте мир через документальные фильмы, разговоры и путешествия.',
    'Discover the world through documentaries, conversations and journeys.',
  ],
  watch: ['Смотреть', 'Watch'],
  resume: ['Продолжить просмотр', 'Continue watching'],
  recent: ['Видеотека', 'Video library'],
  more: ['Показать ещё', 'Show more'],
  moreVideos: ['Загрузить ещё видео', 'Load more videos'],
  empty: ['Здесь пока нет видео', 'No videos here yet'],
  emptyHint: [
    'Измените фильтры или добавьте видео из каталога.',
    'Try other filters or save a video from the catalog.',
  ],
  loading: ['Загрузка…', 'Loading…'],
  retry: ['Повторить', 'Retry'],
  unavailable: [
    'Не удалось загрузить каталог. Повторите попытку.',
    'Unable to load the catalog. Please retry.',
  ],
  playerError: [
    'Плеер не ответил или видео недоступно. Попробуйте снова.',
    'The player did not respond or the video is unavailable. Please retry.',
  ],
  follow: ['Подписаться', 'Subscribe'],
  following: ['Вы подписаны', 'Subscribed'],
  delete: ['Удалить', 'Delete'],
  confirm: ['Подтвердить', 'Confirm'],
  confirmDelete: ['Удалить выбранные данные?', 'Delete the selected data?'],
  confirmLogout: ['Выйти из профиля?', 'Sign out of your profile?'],
  saved: ['Сохранено', 'Saved'],
  storageError: [
    'Не удалось сохранить изменения. Попробуйте снова.',
    'Changes could not be saved. Please try again.',
  ],
  authError: ['Проверьте почту и пароль.', 'Check your email and password.'],
  duplicate: [
    'Профиль с этой почтой уже существует.',
    'A profile with this email already exists.',
  ],
  invalid: [
    'Заполните имя и почту. Пароль должен содержать от 8 до 128 символов.',
    'Enter your name and email. Password must contain 8–128 characters.',
  ],
  needLogin: [
    'Войдите, чтобы собрать свою видеотеку.',
    'Sign in to build your video library.',
  ],
  avatar: ['Фото профиля', 'Profile photo'],
  bio: ['О себе', 'About you'],
  avatarError: [
    'Выберите корректное изображение.',
    'Choose a valid image.',
  ],
  yourChannel: ['Ваш канал', 'Your channel'],
  editProfile: ['Редактировать профиль', 'Edit profile'],
  uploadVideo: ['Загрузить видео', 'Upload video'],
  selectVideo: ['Видеофайл', 'Video file'],
  videoDescription: ['Описание (необязательно)', 'Description (optional)'],
  publish: ['Опубликовать', 'Publish'],
  uploading: ['Сохраняем видео…', 'Saving video…'],
  uploaded: ['Видео опубликовано', 'Video published'],
  uploadHint: [
    'MP4, WebM и другие поддерживаемые форматы.',
    'MP4, WebM and other supported formats.',
  ],
  videoError: [
    'Выберите корректное видео и укажите название.',
    'Choose a valid video and enter a title.',
  ],
  videoStorageError: [
    'Не удалось сохранить видео. Проверьте свободное место и попробуйте снова.',
    'The video could not be saved. Check free space and try again.',
  ],
  deleteVideo: ['Удалить видео', 'Delete video'],
  confirmVideoDelete: [
    'Удалить это видео без возможности восстановления?',
    'Delete this video permanently?',
  ],
  noUploadedVideos: ['На канале пока нет видео', 'No videos on this channel yet'],
  noUploadedHint: [
    'Загрузите первое видео — оно появится здесь.',
    'Upload your first video and it will appear here.',
  ],
  profileVideos: ['Видео канала', 'Channel videos'],
  sort: ['Сортировка', 'Sort'],
  newest: ['Сначала новые', 'Newest first'],
  title: ['По названию', 'By title'],
  duration: ['Длительность', 'Duration'],
  any: ['Любая', 'Any'],
  short: ['До 20 минут', 'Under 20 minutes'],
  long: ['От 20 минут', '20 minutes or longer'],
  language: ['Язык видео', 'Video language'],
  ru: ['Русский', 'Russian'],
  en: ['Английский', 'English'],
  interfaceLanguage: ['Язык интерфейса', 'Interface language'],
  results: ['Найдено', 'Results'],
  channel: ['Канал', 'Channel'],
  channels: ['Каналы', 'Channels'],
  allChannels: ['Все каналы', 'All channels'],
  videos: ['Видео', 'Videos'],
  related: ['Ещё по теме', 'More to explore'],
  description: ['Описание', 'Description'],
  noDescription: ['Автор не добавил описание.', 'No description provided.'],
  videoTitle: ['Название видео', 'Video title'],
  clearHistory: ['Очистить историю', 'Clear history'],
  menu: ['Меню', 'Menu'],
  skip: ['К содержимому', 'Skip to content'],
  back: ['В каталог', 'Back to catalog'],
  notFound: ['Видео не найдено', 'Video not found'],
  count: ['материалов', 'videos'],
  userComments: ['Комментарии пользователя', 'User comments'],
  userNotFound: ['Профиль пользователя не найден', 'User profile not found'],
  justNow: ['только что', 'just now'],
};
let language = storage.get(
  'aster:language',
  navigator.language?.startsWith('ru') ? 'ru' : 'en',
);
if (!['ru', 'en'].includes(language)) language = 'en';
export const locale = () => language;
export const t = (key) => words[key]?.[language === 'ru' ? 0 : 1] || key;
export function setLanguage(value) {
  language = value === 'ru' ? 'ru' : 'en';
  document.documentElement.lang = language;
  return storage.set('aster:language', language);
}
export function date(value) {
  return relativeDate(value);
}
export function relativeDate(value) {
  const parsed = new Date(value);
  if (!Number.isFinite(parsed.getTime())) return '';
  const seconds = (parsed.getTime() - Date.now()) / 1000;
  const absolute = Math.abs(seconds);
  if (absolute < 45) return t('justNow');
  const [amount, unit] =
    absolute < 3600
      ? [seconds / 60, 'minute']
      : absolute < 86400
        ? [seconds / 3600, 'hour']
        : absolute < 2592000
          ? [seconds / 86400, 'day']
          : absolute < 31536000
            ? [seconds / 2592000, 'month']
            : [seconds / 31536000, 'year'];
  return new Intl.RelativeTimeFormat(language, { numeric: 'auto' }).format(
    Math.round(amount),
    unit,
  );
}
export function duration(value) {
  const seconds = Math.max(0, Math.floor(Number(value) || 0));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}
document.documentElement.lang = language;
