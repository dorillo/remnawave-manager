# Миграция Panel 2.8.1 на 3.2.1

Эта инструкция относится к следующей проверенной связке:

- Remnawave Panel 2.8.1;
- Subscription Page 7.2.6;
- PostgreSQL 18.3;
- Panel и Subscription Page находятся на одном сервере Ubuntu 24.04;
- nginx уже настроен;
- целевые версии: Panel 3.2.1, Subscription Page 8.0.0 и PostgreSQL 18.4.

Обновление Panel и Subscription Page выполняется одной транзакцией. Устанавливать или добавлять Node для этого не нужно. Чистая установка Panel через менеджер также не создаёт обязательную Node.

## Что изменяет менеджер

В проверенном сценарии `rwm update`:

1. Повторно инспектирует фактически существующие контейнеры и останавливается, если image reference или immutable image ID изменились после adoption.
2. Проверяет, что live-digest Panel, Subscription Page и PostgreSQL относится к поддерживаемой исходной версии, а managed-файлы не изменились.
3. Проверяет будущие env и Compose в памяти, затем до окна простоя загружает образы Panel 3.2.1, Subscription Page 8.0.0 и PostgreSQL 18.4 и сверяет их digest.
4. Записывает transaction journal и снимок фактически запущенных Compose-сервисов.
5. Останавливает Panel и Subscription Page и повторно убеждается, что write-path закрыт.
6. Создаёт локальный backup конфигурации и custom-format dump PostgreSQL, проверяет dump через `pg_restore --list`, а готовый архив по manifest и SHA-256. Только после этого путь архива добавляется в journal.
7. Мигрирует env-контракт Panel 3 и образы в Compose.
8. В однозначно распознанных proxy-блоках nginx включает полный gzip-контракт, меняет legacy timeout `60s` на `240s`, исключает подделку `X-Forwarded-For` и отключает access-log Subscription Page, способный содержать идентификаторы ссылок. Произвольные server-блоки, XHTTP и Яндекс-конфигурация не переписываются.
9. Сохраняет изменённые env и Compose и проверяет `docker compose config -q`.
10. Пересоздаёт PostgreSQL с образом 18.4 и ждёт обязательный health-check до запуска Panel.
11. Запускает Panel, ждёт health-check и проверяет локальный HTTP endpoint.
12. Запускает Subscription Page, ждёт health-check, её endpoint и шесть обязательных API scopes.
13. Проверяет уже загруженную конфигурацию nginx, возвращает исходный running/stopped-набор сервисов и заново строит inventory.

Если любой обязательный шаг завершается ошибкой, менеджер пытается восстановить конфигурацию и PostgreSQL из pre-update backup.
Если не удалось создать сам backup, конфигурация и БД ещё не изменялись: менеджер не вызывает restore и только возвращает исходное состояние сервисов.

При первом запуске Panel 3.2.1 штатная миграция scopes удаляет только записи API-токенов с некорректным UUID. Перед обновлением проверьте, что токен, используемый внешним сайтом, создан штатно и соответствует UUID-формату; сам секрет токена при этом не меняется.

## Критичный APP_SECRET

Panel 3 использует `APP_SECRET` вместо `JWT_AUTH_SECRET`. Менеджер переносит эффективное значение байт-в-байт:

```text
JWT_AUTH_SECRET -> APP_SECRET
```

Новый случайный `APP_SECRET` при миграции не создаётся. Замена секрета нарушила бы существующую авторизацию и пароль администратора.

Если `APP_SECRET` уже существует и не пуст, сохраняется его эффективное значение, а `JWT_AUTH_SECRET` удаляется. Если оба допустимых секрета отсутствуют или пусты, update останавливается до изменения файлов.

Удаляются устаревшие переменные:

- `JWT_API_TOKENS_SECRET`;
- `SWAGGER_PATH`;
- `SCALAR_PATH`;
- `IS_DOCS_ENABLED`.

Не редактируйте эти переменные непосредственно перед update без отдельной проверенной копии `.env`.

## Необратимость миграции БД

Миграции PostgreSQL, которые выполняет новая Panel при старте, нельзя корректно отменить одним возвратом старого Docker image. Полный rollback Panel требует одновременно:

- вернуть старые Compose и env;
- вернуть образ PostgreSQL 18.3, пересоздать контейнер и восстановить pre-update dump;
- запустить старые Panel и Subscription Page вместе.

Именно поэтому автоматический update останавливает прикладной write-path и создаёт dump до первого изменения конфигурации или БД. Это исключает потерю записей, появившихся между dump и остановкой Panel. Подробнее: [rollback и аварийное восстановление](rollback-recovery.md).

## 1. Установите менеджер

Из локальной копии репозитория:

```bash
chmod +x install.sh
sudo ./install.sh
```

Команда должна завершиться сообщением о доступности `sudo rwm`.

Повторный запуск установщика собирает отдельный versioned venv, проверяет его и атомарно переключает ожидаемую ссылку `/usr/local/bin/rwm`; предыдущее рабочее окружение сохраняется. При коллизии с чужим непустым каталогом или файлом установщик остановится; не обходите эту проверку ручным созданием маркера.

## 2. При необходимости войдите в Docker Registry

На сервере с ограничениями Docker Hub сначала выполните вход через менеджер:

```bash
sudo rwm registry login --registry docker-hub --username ВАШЕ_ИМЯ --select
sudo rwm registry status
```

Пароль или access token запрашивается скрыто и передаётся Docker через stdin. Не добавляйте его в командную строку. Для Panel выбирайте `docker-hub`: в текущем compatibility manifest образ Subscription Page 8.0.0 проверен только для Docker Hub.

Подробности: [Docker Registry на российских серверах](registry-russia.md).

## 3. Выполните adoption

```bash
sudo rwm adopt --path /opt/remnawave --role panel
sudo rwm inventory
sudo rwm diagnose
sudo rwm service status
```

Если Compose монтирует сертификаты из `/etc/letsencrypt`, adoption проверит их renewal-конфигурации, установит безопасные hooks для контейнера nginx и включит `certbot.timer`. Узнаваемые legacy `renew_hook` и cron старого скрипта будут заменены; пользовательские задания не затрагиваются. Для повторной проверки используйте `sudo rwm certificate repair-renewal`.

До продолжения inventory должен содержать как минимум `panel`, `subscription` и `database`, а database должна определяться как PostgreSQL 18.3 или 18.4. Ошибки диагностики нужно разобрать, а не обходить `--yes`.

Если сохранился лог предыдущего `remnawave-reverse-proxy`, диагностика отдельно покажет `/usr/local/remnawave_reverse/remnawave_reverse.log`. Команда `sudo rwm diagnose --repair-permissions` ограничит его права до `0600`, но решение об удалении после проверки содержимого остаётся за администратором.

Старый установщик также мог оставить `.env`, Compose и nginx с правами `0644`.
Если диагностика сообщает такую ошибку, до update выполните:

```bash
sudo rwm diagnose --repair-permissions
sudo rwm diagnose
```

Update не исправляет эти права скрытно и останавливается до загрузки образов, backup или
остановки сервисов, пока приватные managed-файлы не принадлежат root с режимом `0600`.

Adoption сохраняет текущие файлы без переписывания. Существующая cookie-защита nginx также остаётся без изменений. Её распознанный URL можно проверить:

```bash
sudo rwm security access
```

Update не конвертирует legacy gate скрытно. После проверки текущего URL его можно явно мигрировать на более строгий `manager-path`:

```bash
sudo rwm security rotate-access
```

Команда создаёт backup, удаляет только однозначно распознанные legacy map/header-блоки, проверяет nginx и при ошибке возвращает исходный файл. После успеха сохраните выведенный URL: старый query-URL перестанет работать.

## 4. Проверьте исходную версию

Менеджер пытается доказать версию по digest запущенного или настроенного образа, затем по поддерживаемому tag. Для Panel 2.8.1 и Subscription Page 7.2.6 ручное разрешение неизвестного источника не требуется.

Перед доказательством версии менеджер повторяет live `docker inspect`. Если контейнер был пересоздан под тем же mutable tag после adoption, update остановится и потребует повторный `rwm adopt`; сохранённый старый image ID не используется как доказательство.

Если версия не определяется, update остановится. Флаг:

```bash
sudo rwm update --accept-unknown-source
```

не выполняет дополнительную проверку совместимости. Применяйте его только если вы самостоятельно сопоставили фактически запущенные digest и конфигурацию с поддерживаемым релизом. Это не средство исправления проблем сети или Registry.

Panel stack использует проверенный Subscription Page image только из Docker Hub. Если выбран `ghcr`, update останавливается до загрузки образов; выполните `sudo rwm registry select docker-hub` и при необходимости сначала войдите в Docker Hub.

## 5. Создайте контрольный backup

Update всё равно создаст собственный pre-update backup. Отдельная контрольная копия полезна для проверки процесса до окна обслуживания:

```bash
sudo rwm backup create --reason before-panel-3.2.1
sudo rwm backup list
```

Скопируйте путь последнего архива и проверьте его:

```bash
sudo rwm backup verify /var/backups/remnawave-manager/ИМЯ_BACKUP.tar.gz
```

Архив остаётся на этом же сервере. Менеджер никуда его не отправляет.

## 6. Dry-run и окно обслуживания

У update нет dry-run. Команды `rwm update --dry-run` не существует. `--yes` только пропускает текстовое подтверждение и сразу начинает реальное изменение.

Перед окном обслуживания доступны только безопасные проверки:

```bash
sudo rwm diagnose
sudo rwm service status
sudo rwm backup verify /var/backups/remnawave-manager/ИМЯ_BACKUP.tar.gz
sudo rwm certificate renew --dry-run
```

Последняя команда тестирует Certbot, а не обновление Remnawave. Renewal hooks HTTP-01 могут кратковременно останавливать nginx даже во время тестового продления.

## 7. Запустите обновление

```bash
sudo rwm update
```

Прочитайте предупреждение и подтвердите операцию. Для автоматизированного запуска существует `--yes`, но он не рекомендуется для первой миграции.

Не прерывайте процесс после начала миграции базы. Journal активной транзакции записывается в:

```text
/var/lib/remnawave-manager/active-transaction.json
```

## 8. Проверка после обновления

```bash
sudo rwm service status
sudo rwm diagnose
sudo rwm inventory
sudo rwm security access
sudo rwm certificate status
```

Дополнительно проверьте извне:

- вход администратора через защищённый URL;
- загрузку Panel;
- выдачу страницы подписки по реальной пользовательской ссылке;
- подключение одной тестовой конфигурации;
- отсутствие публикации служебных портов Panel и PostgreSQL наружу.

Не удаляйте pre-update backup сразу после первой успешной проверки.

Если после обновления утрачен защищённый URL или cookie, не публикуйте внутренний порт Panel. Откройте ограниченный по времени loopback-доступ и используйте выведенный SSH-туннель:

```bash
sudo rwm security emergency-open --minutes 30
sudo rwm security emergency-status
sudo rwm security emergency-close
```

## Если update остановился

Если сообщение говорит, что предыдущая версия восстановлена, сначала выполните:

```bash
sudo rwm service status
sudo rwm diagnose
sudo rwm backup list
```

Если автоматический rollback тоже завершился ошибкой, не запускайте update повторно. Проверьте указанный в ошибке backup и перейдите к [аварийному восстановлению](rollback-recovery.md).

## Что update намеренно не делает

- не устанавливает Node;
- не добавляет Node в Panel;
- не переносит Panel или БД на другой сервер;
- не меняет произвольно существующую cookie-защиту;
- не меняет правила UFW принятой установки;
- не запускает немедленное обновление сертификата (дальнейшее автопродление выполняет `certbot.timer`);
- не отправляет backup за пределы сервера;
- не понижает требования безопасности для прохождения миграции.

## Ссылки

- [официальная документация Remnawave](https://docs.rw/)
- [описание изменений Remnawave 3.0.0](https://f.docs.rw/releases/v300)
- [rollback и recovery](rollback-recovery.md)
