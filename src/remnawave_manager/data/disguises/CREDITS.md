# Источники фотографий

Фотографии сохранены локально и используются по [Unsplash License](https://unsplash.com/license). Эти фотографии не загружаются с Unsplash во время работы. Источники динамического контента описаны отдельно.

- [Crew](https://unsplash.com/photos/wy9X4c0rX4M), photo id `1497366811353-6870744d04b2`: `01-northline/hero.jpg`.

## Свод

`05-field-notes/purify.es.js` — DOMPurify 3.4.15, Cure53 и участники проекта.
Исходный код: https://github.com/cure53/DOMPurify. Полная лицензия включена
в `05-field-notes/DOMPurify-LICENSE.md` (Apache-2.0 OR MPL-2.0).
Оформление и `05-field-notes/favicon.svg` созданы для проекта.
Энциклопедические материалы загружаются из источника и не включены в пакет;
состояние интеграции описано в `docs/svod.md`.

## Loop

`06-loop-archive` — собственный интерфейс проекта; оформление и favicon созданы
для Loop. GIF, стикеры и клипы загружаются с https://gifs.ru/ через nginx
и не входят в дистрибутив. Права на материалы остаются у их правообладателей.
Метаданные содержат автора, если он указан источником.
Используются анонимные маршруты сайта, не коммерческий GIFS API.
Технические ограничения и проверки описаны в `docs/loop.md`.

Плеер Loop использует локальные копии библиотек под MIT License:
- `06-loop-archive/loop-gif-reader.js` — декодер из [omggif](https://github.com/deanm/omggif), Dean McNamee; адаптирован к ES modules, кодировщик исключён.
- `06-loop-archive/loop-webm-duration.js` — [fix-webm-duration](https://github.com/yusitnikov/fix-webm-duration) 1.0.6; адаптирован к ES modules.
Полные лицензии сохранены в начале соответствующих файлов.

## Fokus

`07-fokus-news` — собственное оформление и favicon проекта. Новости, изображения
и внешние комментарии загружаются с https://ria.ru/ через ограниченный nginx-прокси
и не включаются в дистрибутив. Права остаются у правообладателей. Пользовательские действия сохраняются локально.
