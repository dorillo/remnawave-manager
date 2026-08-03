# Remnawave Manager

Локальный русскоязычный менеджер для безопасной установки и обслуживания Remnawave на Ubuntu 24.04 LTS. Он управляет Docker Compose, nginx, локальными резервными копиями, сертификатами, UFW и встроенной конфигурацией Cloudflare WARP без запуска сторонних установочных shell-скриптов.

Менеджер запускается отдельно на каждом сервере. Он не подключается к Node по SSH и не является центральной системой управления парком серверов.

## Проверенная совместимость

Актуальная матрица находится в `src/remnawave_manager/data/compatibility.json` и привязана к digest образов.

Менеджер никогда не следует плавающему тегу `latest` и не повышает версии только потому, что upstream опубликовал новый релиз. Новая версия становится поддерживаемой после отдельной проверки release notes, миграций, API-контрактов и digest образов, затем явно добавляется в compatibility manifest. Перед production-обновлением новая связка дополнительно проверяется на отдельной Ubuntu 24.04 с копией реальных данных.

Для Panel 3.2.0 проверены официальный release note, полный diff с 3.1.0, отсутствие изменений `.env`, Compose и Prisma, а также совпадение multiarch manifest digest в Docker Hub и GHCR. Релиз добавляет read-only endpoint `GET /api/system/configuration` и исправляет валидацию Config Profile; отдельная миграция конфигурации или базы не требуется.

| Компонент | Проверенная исходная версия | Целевая версия |
| --- | --- | --- |
| Remnawave Panel | 2.8.1, 3.0.0, 3.1.0, 3.2.0 | 3.2.0 |
| Subscription Page | 7.2.6, 8.0.0 | 8.0.0 |
| Remnawave Node | 2.8.0, 3.0.0 | 3.0.0 |
| PostgreSQL | 18.3, 18.4 | 18.4 |
| `wgcf` | не применяется | 2.2.32 с фиксированным SHA-256 |

Поддерживаются только Linux-серверы с Ubuntu 24.04 LTS на архитектуре `amd64` или `arm64`. Для Panel и Node предполагается nginx. Совмещённая установка Panel и Node на одном сервере намеренно не поддерживается.

Рекомендуемая топология:

- один сервер: Panel и Subscription Page;
- отдельный сервер на каждую Node;
- nginx на Panel и Node;
- XHTTP Unix sockets, stream separation, Reality, WARP и конфигурация Яндекс CDN сохраняются при обновлении Node, если они обнаружены во время adoption.

Чистая установка Panel не создаёт Node и не требует её добавления.

## Установка менеджера

Запустите на сервере от пользователя с `sudo`:

```bash
curl -fsSL https://raw.githubusercontent.com/dorillo/remnawave-manager/main/install.sh | sudo bash -s -- install
```

Команда загружает полный репозиторий во временный каталог, запускает штатный установщик и удаляет временные файлы после завершения. Для установки из заранее проверенной локальной копии репозитория используйте:

```bash
chmod +x install.sh
sudo ./install.sh install
```

Установщик:

- проверяет Linux, Ubuntu 24.04, архитектуру `amd64`/`arm64` и права root до изменения системы;
- идемпотентно включает официальный компонент APT `Universe`, затем устанавливает Docker Compose, Certbot с DNS-плагином Cloudflare, UFW, unattended-upgrades и системные зависимости;
- устанавливает официальный Certbot DNS-плагин Gcore `0.1.8` из PyPI без зависимостей только после проверки закреплённого SHA-256; плагин ставится в системный Python, чтобы стандартный `certbot.timer` видел его при автопродлении;
- проверяет и запускает стандартный системный rootful `docker.service` через `/run/docker.sock`;
- собирает новый versioned virtual environment в `/opt/remnawave-manager/runtime`, проверяет импорт и entrypoint, затем атомарно переключает ссылку `active`;
- устанавливает текущую локальную версию проекта без сторонних Python-зависимостей;
- создаёт команду `/usr/local/bin/rwm`;
- пытается включить BBR/fq и автоматические security updates без автоматической перезагрузки;
- при обновлении уже принятой установки проверяет и перегенерирует manager-owned Certbot hooks. Ошибка этой дополнительной операции не откатывает рабочую версию менеджера и выводится с командой ручного восстановления.

Каталоги `/opt/remnawave-manager` и `/etc/remnawave-manager` должны принадлежать `root:root`, не могут быть доступны для записи группе/остальным и помечаются single-link файлами
`.managed-by-remnawave-manager` с фиксированными правами; конфигурационный каталог имеет права `0700`. Повторный запуск
обновляет установленную локальную копию под отдельным lock и сохраняет ссылку `previous` на предыдущее рабочее окружение. Если непустой каталог не имеет правильного маркера или
`/usr/local/bin/rwm` занят не принадлежащим менеджеру файлом, установщик останавливается и ничего
там не перезаписывает. Не создавайте маркер вручную для чужого каталога.

Если ядро не поддерживает BBR или systemd units обновлений недоступны, установка самого менеджера всё равно завершается и выводит предупреждение. После исправления причины повторите `sudo rwm system apply`.

Установщик не использует rootless Docker, Docker из snap, Podman-совместимый сокет или удалённый `DOCKER_HOST`. Если команда `docker` уже существует, но Compose v2 отсутствует, автоматически поддерживаются пакеты Ubuntu `docker.io` и официальный `docker-ce`; неизвестная установка Docker не изменяется. При ошибке запуска выводится состояние `docker.service`, а существующие данные Docker не удаляются.

Интерактивное меню:

```bash
sudo rwm
```

В интерактивном терминале главное и вложенные меню очищают предыдущий экран перед отрисовкой. После выполнения операции результат остаётся видимым до нажатия Enter. При перенаправлении вывода и в `--json` управляющие последовательности не используются.

Справка по любой операции:

```bash
rwm --help
rwm update --help
rwm warp --help
```

Альтернативный запуск через созданный virtual environment:

```bash
sudo /opt/remnawave-manager/runtime/active/bin/python -m remnawave_manager
```

## Принятие существующей установки

Перед первым обновлением менеджер должен построить inventory и сохранить хеши защищаемых файлов.

На сервере Panel:

```bash
sudo rwm adopt --path /opt/remnawave --role panel
sudo rwm inventory
sudo rwm diagnose
```

На каждой Node:

```bash
sudo rwm adopt --path /opt/remnanode --role node
sudo rwm inventory
sudo rwm diagnose
```

Adoption не переписывает Compose, env или nginx, но записывает inventory в `/var/lib/remnawave-manager/inventory.json`. Если стек использует сертификаты из `/etc/letsencrypt`, adoption также проверяет renewal-конфигурации, устанавливает принадлежащие менеджеру Certbot hooks и включает `certbot.timer`. Узнаваемые `renew_hook` и cron-задание старого `remnawave-reverse-proxy` удаляются транзакционно; произвольные пользовательские hooks и cron-задания не изменяются. Конфликт с чужим файлом на пути manager hook останавливает adoption.

Если после adoption вы осознанно изменили конфигурацию, сначала проверьте изменения и повторите `rwm adopt` для фиксации новой контрольной точки. Без этого update остановится при обнаружении drift. Автопродление можно повторно проверить и восстановить отдельно:

```bash
sudo rwm certificate repair-renewal
sudo rwm certificate renew --dry-run
```

Совмещённая Panel+Node установка будет отклонена. Сначала роли нужно разнести по разным серверам.

## Чистая установка Panel

Пример с Let's Encrypt HTTP-01:

```bash
sudo rwm install panel \
  --panel-domain panel.example.com \
  --subscription-domain sub.example.com \
  --certificate-method http-01 \
  --email admin@example.com
```

Менеджер установит Panel 3.2.0 и Subscription Page 8.0.0 на одном сервере, настроит nginx, UFW, TLS и защитный URL с cookie. Node при этом не устанавливается.

Имя и стойкий пароль администратора генерируются автоматически и показываются один раз. Можно указать `--admin-username` и запросить собственный пароль через `--ask-admin-password`. Передача пароля значением аргумента командной строки не поддерживается.

Для существующего сертификата, Cloudflare DNS-01 или Gcore DNS-01 смотрите:

```bash
rwm install panel --help
```

Выпуск отдельного сертификата для будущего домена, продление и renewal hooks описаны в
[управлении TLS-сертификатами](docs/certificates.md). Команда выпуска не меняет рабочий домен.

## Чистая установка Node

Сначала получите `SECRET_KEY` Node в Panel. Команда API создаёт Config Profile, Node и Host, добавляет созданный inbound во все существующие Internal Squads, а при ошибке возвращает состав squads и удаляет только созданные объекты:

```bash
sudo rwm api reality \
  --profile-name Moscow \
  --inbound-tag reality_msk \
  --node-name Moscow-1 \
  --domain node.example.com
```

Admin API token вводится скрыто или читается из `RWM_API_TOKEN`. Полученный `SECRET_KEY` показывается один раз.

На сервере Node выберите один из десяти встроенных шаблонов:

```bash
export RWM_NODE_SECRET_KEY='полученный-secret-key'
sudo --preserve-env=RWM_NODE_SECRET_KEY rwm install node \
  --domain node.example.com \
  --panel-ip 192.0.2.10 \
  --template 01-northline \
  --certificate-method http-01 \
  --email admin@example.com
unset RWM_NODE_SECRET_KEY
```

Вместо `--template` можно указать подготовленный локальный каталог через `--site-source`. На Linux каталог, его содержимое и все родители должны принадлежать root и не допускать group/world write; это защищает root-копирование от подмены дерева. Случайного выбора заглушки нет.

Если чистая установка прервалась после создания рабочего каталога, в нём остаётся строгий ownership-маркер попытки. Следующий запуск той же команды сначала выполняет `docker compose down` без удаления volumes и перемещает только подтверждённый менеджером каталог в `/opt/remnawave.incomplete-ДАТА-ID` или `/opt/remnanode.incomplete-ДАТА-ID`. Чужой непустой каталог без корректного маркера никогда не перемещается автоматически. Сертификат Let's Encrypt, DNS credential, renewal hooks, `certbot.timer` и UFW участвуют в отдельных rollback-транзакциях; при неполном rollback CLI сохраняет приватный снимок и не сообщает об успешной установке.

## Обновление

После adoption команда сама определяет роль сервера:

```bash
sudo rwm update
```

До окна обслуживания загружаются и проверяются все целевые образы. Затем Panel и Subscription Page останавливаются, после чего при закрытом write-path создаётся и проверяется локальный pre-update backup. Panel, Subscription Page и PostgreSQL обновляются одной транзакцией; проверенный переход базы для исходной установки выполняется с PostgreSQL 18.3 на 18.4. Ошибка создания backup не запускает restore неполного архива и возвращает исходное состояние сервисов. Для Node сначала выгружается текущий Xray JSON и проверяется новым образом в изолированном контейнере с теми же необходимыми runtime mounts.

У `rwm update` нет dry-run. Не используйте несуществующие варианты вроде `rwm update --dry-run`. Безопасная предварительная последовательность:

```bash
sudo rwm diagnose
sudo rwm backup create --reason before-update
sudo rwm backup list
sudo rwm backup verify /var/backups/remnawave-manager/ИМЯ_BACKUP.tar.gz
```

Подробные инструкции:

- [миграция Panel 2.8.1 и Subscription Page 7.2.6](docs/migration-panel-2.8.1-to-3.2.0.md);
- [обновление Node 2.8.0](docs/update-node-2.8.0-to-3.0.0.md);
- [сохранение XHTTP, stream separation и Яндекс CDN](docs/xhttp-yandex-preservation.md);
- [rollback и аварийное восстановление](docs/rollback-recovery.md).

## Резервные копии

Все backup сохраняются только локально в `/var/backups/remnawave-manager` с правами доступа менеджера. Автоматической отправки в облако, Telegram, S3 или на другой сервер нет. Verify/restore принимают только неизменяемый во время чтения обычный single-link архив и не переходят по symlink; list/retention игнорируют symlink, hardlink и специальные файлы.

```bash
sudo rwm backup create --reason manual
sudo rwm backup list
sudo rwm backup verify /var/backups/remnawave-manager/ИМЯ_BACKUP.tar.gz
sudo rwm backup restore /var/backups/remnawave-manager/ИМЯ_BACKUP.tar.gz
```

Panel backup содержит проверенный custom-format PostgreSQL dump. Для ручного backup можно применить retention после успешного создания нового архива:

```bash
sudo rwm backup create --reason scheduled --retention 10
```

`--retention` удаляет только повторно проверенные более старые локальные архивы той же роли и не является dry-run. Restore сначала создаёт приватную стабильную копию сжатого архива, поэтому до распаковки требуется как минимум размер `.tar.gz` плюс `256 MiB` свободного места, а staging содержимого может потребовать больше.

Ежедневное или еженедельное расписание создаётся как принадлежащий менеджеру systemd timer. Время задаётся в локальной временной зоне сервера, еженедельный запуск выполняется по воскресеньям:

```bash
sudo rwm backup schedule-enable --frequency daily --time 03:30 --retention 10
sudo rwm backup schedule-status
sudo rwm backup schedule-disable
```

Таймер является persistent: пропущенный запуск выполняется после следующей загрузки сервера. Для распределения нагрузки systemd может задержать старт не более чем на 10 минут. `schedule-disable` удаляет только units расписания, существующие архивы сохраняются. Чужие units с теми же именами менеджер не перезаписывает.

## Управление сервисами

Названия доступных компонентов берутся из inventory. Обычно на Panel это `panel`, `subscription`, `database` и `cache`, а на Node — `node` и, если он описан в Compose, `nginx`.

```bash
sudo rwm service status
sudo rwm service restart all
sudo rwm service logs all --tail 200 --since 30m
sudo rwm service restart panel
sudo rwm service restart node
sudo rwm service logs node --tail 200 --since 30m
sudo rwm service logs panel --follow
sudo rwm service panel-cli
```

Значение `all` управляет всем Compose-стеком текущего сервера. Для `start all`
запускается `docker compose up -d` со всеми зависимостями; `stop all` и
`restart all` применяются ко всем сервисам проекта. `logs all` объединяет их
логи с префиксами Compose. Индивидуальный `start` по-прежнему не запускает
зависимости выбранного компонента. После `start` и `restart` команда ждёт состояние
контейнера и выполняет подходящую прикладную проверку: HTTP Panel/Subscription Page,
runtime Xray Node, `nginx -t` и сохранённые XHTTP-сокеты. Ошибка проверки не выводится
как успешное завершение.

`service panel-cli` открывает штатную интерактивную команду `cli` внутри контейнера Panel и доступен только на panel-сервере. Он выполняется от root под общим lock, поскольку команды внутри Panel могут изменять состояние.

## Основные команды

| Назначение | Команда |
| --- | --- |
| Показать inventory | `sudo rwm inventory` |
| Диагностика | `sudo rwm diagnose` |
| Исправить известные права | `sudo rwm diagnose --repair-permissions` |
| Статус контейнеров | `sudo rwm service status` |
| Перезапустить весь стек | `sudo rwm service restart all` |
| Логи всего стека | `sudo rwm service logs all --tail 200` |
| Перезапустить компонент | `sudo rwm service restart node` |
| Логи компонента | `sudo rwm service logs node --tail 200` |
| CLI внутри Panel | `sudo rwm service panel-cli` |
| Статус расписания backup | `sudo rwm backup schedule-status` |
| Registry | `sudo rwm registry status` |
| Сертификаты | `sudo rwm certificate status` |
| Выпустить сертификат нового домена | `sudo rwm certificate issue --help` |
| Восстановить автопродление | `sudo rwm certificate repair-renewal` |
| Тест Certbot | `sudo rwm certificate renew --dry-run` |
| Reload nginx | `sudo rwm certificate reload` |
| UFW | `sudo rwm firewall status` |
| BBR и security updates | `sudo rwm system status` |
| Применить настройку Ubuntu | `sudo rwm system apply` |
| Заглушки | `sudo rwm disguise list` |
| Защищённый URL Panel | `sudo rwm security access` |
| Ротация cookie и URL | `sudo rwm security rotate-access` |
| Статус аварийного доступа | `sudo rwm security emergency-status` |
| WARP scan | `sudo rwm warp scan` |
| WARP status | `sudo rwm warp status` |
| Архивировать стек | `sudo rwm maintenance archive-stack` |

На принятой установке `security rotate-access` распознаёт старую query-cookie, удаляет только известные legacy map/header-блоки nginx и переводит доступ на `manager-path`. Перед изменением создаётся backup; конфигурация проходит `nginx -t`, а при ошибке возвращается исходный файл. Старый URL после успешной миграции перестаёт работать.

Для JSON поместите глобальный параметр перед командой:

```bash
sudo rwm --json inventory
sudo rwm --json diagnose
```

В режиме `--json` stdout содержит ровно один JSON-документ. Потоковые интерактивные команды
`service logs`, `service panel-cli` и `menu` этот режим отклоняют. Подтверждаемые операции
с `--json` требуют явного `--yes`, чтобы prompt не смешивался с машинно-читаемым выводом.
`--yes` только отключает запрос подтверждения и не меняет проверки безопасности.

Доступные ID заглушек: `01-northline`, `02-aster-observatory`, `03-morrow-coffee`, `04-signal-works`, `05-field-notes`, `06-loop-archive`, `07-fokus-news`, `08-vector-docs`, `09-pulse-monitor`, `10-dev-circle`. Шаблоны полностью статические, хранят все рабочие ассеты локально и не выполняют внешних запросов. Замена выполняется с backup и проверкой конфигурации на новом bind mount. Работающий nginx-контейнер точечно пересоздаётся; остановленный пересоздаётся без запуска и проверяется одноразовым контейнером:

```bash
sudo rwm disguise apply 03-morrow-coffee
```

Перед заменой все файлы текущего сайта должны совпадать с inventory. Неучтённый или
изменённый файл блокирует операцию до повторного осознанного `rwm adopt`, чтобы backup
не оказался неполным и пользовательские материалы не были удалены молча. На Linux
корень сайта и его не-sticky родители также не должны быть доступны для записи группе
или другим пользователям.

## Секреты

Секреты не принимаются значениями argv. Используется скрытый prompt или переменная окружения:

| Переменная | Назначение |
| --- | --- |
| `RWM_ADMIN_PASSWORD` | собственный пароль при чистой установке Panel |
| `RWM_NODE_SECRET_KEY` | `SECRET_KEY` чистой установки Node |
| `RWM_REGISTRY_PASSWORD` | пароль или access token Docker Registry |
| `RWM_CLOUDFLARE_TOKEN` | Cloudflare API Token для DNS-01 |
| `RWM_GCORE_TOKEN` | Gcore API Token для DNS-01 |
| `RWM_API_TOKEN` | Admin API token Remnawave |
| `WGCF_LICENSE_KEY` | WARP+ key |

Если секрет передаётся через окружение вместе с `sudo`, разрешайте только нужную переменную через `--preserve-env=ИМЯ` и сразу выполняйте `unset`. При отсутствии защищённого терминала CLI не переходит к вводу с отображением секрета.

Для серверов с ограниченным доступом к registry смотрите [Docker Registry на российских серверах](docs/registry-russia.md).

## WARP

WARP реализован внутри менеджера. Сторонний установочный shell-скрипт не скачивается. Используется зафиксированный бинарный `wgcf`, проверяемый по SHA-256, kernel WireGuard и собственный systemd health-check.

```bash
sudo rwm warp scan
sudo rwm warp install --accept-tos
sudo rwm warp status
```

Для существующей конфигурации сначала выполните scan, затем отдельный takeover. Полное описание: [docs/warp.md](docs/warp.md).

## Аварийный доступ к Panel

Если защищённый URL или cookie утрачены, Panel можно временно открыть только на loopback-порту сервера и подключиться к нему через SSH-туннель:

```bash
sudo rwm security emergency-open --minutes 30
sudo rwm security emergency-status
```

Команда выведет точную строку `ssh -L` и локальный URL. Порт `8443` не публикуется наружу; nginx слушает только `127.0.0.1`. Доступ автоматически закрывает systemd timer, а немедленное закрытие выполняется так:

```bash
sudo rwm security emergency-close
```

Перед открытием создаётся backup, nginx проверяется до reload, а при ошибке конфигурация откатывается. Аварийный доступ не заменяет обычную cookie-защиту и должен использоваться только для восстановления доступа через доверенное SSH-соединение.

## Настройка Ubuntu

Состояние BBR/fq и автоматических security updates проверяется без изменений:

```bash
sudo rwm system status
```

Корневой `install.sh` выполняет это применение автоматически. Команда ниже нужна для повторной настройки или ручного восстановления: она создаёт принадлежащие менеджеру файлы `/etc/sysctl.d/90-remnawave-manager-bbr.conf` и `/etc/apt/apt.conf.d/52remnawave-manager-unattended-upgrades`, включает `apt-daily.timer` и `apt-daily-upgrade.timer`, но не разрешает автоматическую перезагрузку сервера:

```bash
sudo rwm system apply
```

Чужие файлы на этих путях не перезаписываются. При ошибке возвращаются исходные файлы, параметры ядра и состояния systemd units.

## Удаление и переустановка стека

Вместо необратимого удаления каталогов, Docker volumes и всех образов используется восстанавливаемое архивирование:

```bash
sudo rwm maintenance archive-stack
```

Команда проверяет drift и Compose, создаёт локальный backup, записывает в transaction journal точные исходные/архивные пути и created/running-снимок, отключает расписание backup, останавливает только текущий Compose-проект и перемещает стандартный каталог установки вместе с inventory в файлы с суффиксом `.removed-ДАТА-ID`. Сертификаты, UFW, Docker images/volumes и WARP не удаляются. После этого можно выполнить новую `rwm install panel` или `rwm install node`; до ручного удаления архивного каталога старые данные остаются доступными для восстановления. При SIGKILL или перезагрузке во время команды используйте только [процедуру для `stack-archive`](docs/rollback-recovery.md#stack-archive); выбирать архив по timestamp запрещено.

Сам менеджер обновляется повторным запуском той же команды с `curl` либо `sudo ./install.sh install` из новой проверенной локальной копии репозитория. Установщик собирает и проверяет отдельное Python-окружение и атомарно переключает `/usr/local/bin/rwm` только после успешной проверки. Вариант с `curl` загружает код из ветки `main`; для воспроизводимой установки с предварительным аудитом используйте локальную копию конкретного коммита.

## Безопасность и границы

- Все изменяющие команды выполняются от root под общим lock.
- Внешние команды запускаются списком аргументов без shell-конкатенации.
- Образы компонентов привязаны к проверенным digest.
- Compose, env, nginx, secret, state и backup создаются с ограниченными правами; диагностика
  требует `0600` для приватных managed-файлов legacy-установок и не меняет публичные файлы сайта.
- Диагностика отдельно обнаруживает legacy-лог `/usr/local/remnawave_reverse/remnawave_reverse.log`, который может содержать историю установки. `diagnose --repair-permissions` ограничивает его права до `0600`, но не удаляет файл.
- Update отказывается работать при необъяснённом drift или неподтверждённой исходной версии.
- `--accept-unknown-source` не проверяет совместимость. Используйте его только после собственной проверки исходного образа.
- Автоматический rollback не заменяет внешний backup всего сервера. Потеря диска уничтожит и локальные архивы.
- Менеджер не настраивает управление IPv6 и не устанавливает кастомные расширения legiz.

## Пути

| Путь | Содержимое |
| --- | --- |
| `/etc/remnawave-manager` | настройки и секретные метаданные менеджера |
| `/var/lib/remnawave-manager` | inventory, journal транзакции, WARP state, API backup профилей |
| `/var/backups/remnawave-manager` | локальные `.tar.gz` backup |
| `/var/log/remnawave-manager` | каталог логов менеджера |
| `/run/remnawave-manager/manager.lock` | lock изменяющих операций в root-owned каталоге `0700` |
| `/opt/remnawave` | стандартный каталог Panel |
| `/opt/remnanode` | стандартный каталог Node |

## Документация

- [официальная документация Remnawave](https://docs.rw/)
- [изменения Remnawave 3.0.0](https://f.docs.rw/releases/v300)
- [Docker Registry на российских серверах](docs/registry-russia.md)
- [WARP](docs/warp.md)
- [rollback и recovery](docs/rollback-recovery.md)

## Лицензия и сторонние материалы

Код проекта распространяется по [MIT License](LICENSE). Фотографии встроенных заглушек имеют отдельную [Unsplash License](https://unsplash.com/license); авторы и исходные ссылки перечислены в `src/remnawave_manager/data/disguises/CREDITS.md`.

`wgcf` является неофициальным клиентом Cloudflare WARP и загружается из upstream-релизов [ViRb3/wgcf](https://github.com/ViRb3/wgcf) во время операции WARP. Он не является частью исходного кода менеджера. Panel, Backend, Node и Subscription Page являются внешним ПО Remnawave под AGPL-3.0 и также не бандлятся в этот репозиторий. Полные уведомления: [NOTICE](NOTICE).

Remnawave Manager не аффилирован с Remnawave, Cloudflare, Яндекс или Unsplash. Названия и товарные знаки принадлежат соответствующим правообладателям.
