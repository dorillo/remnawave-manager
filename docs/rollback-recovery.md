# Rollback и аварийное восстановление

Remnawave Manager использует локальные проверяемые backup и transaction journal. Этот механизм рассчитан на возврат конфигурации текущего сервера после неудачного update, но не заменяет snapshot диска или внешнюю копию.

## Где находятся данные

| Путь | Назначение |
| --- | --- |
| `/var/backups/remnawave-manager` | локальные архивы `.tar.gz` |
| `/var/lib/remnawave-manager/inventory.json` | текущая инвентаризация и хеши managed files |
| `/var/lib/remnawave-manager/active-transaction.json` | journal незавершённого update или архивирования стека |
| `/var/lib/remnawave-manager/api-backups` | JSON-копии Config Profile перед WARP routing PATCH |

Архивы никуда не отправляются. При потере диска локальный backup будет потерян вместе с установкой.

## Состав backup

Каждый архив содержит:

- `manifest.json` со схемой, ролью, временем, причиной и inventory;
- зарегистрированные Compose, env, nginx и site-файлы;
- SHA-256 и режим доступа каждого файла;
- для Panel: custom-format PostgreSQL dump;
- для Node: дополнительные копии обнаруженных WARP-файлов и units.

PostgreSQL dump проверяется через `pg_restore --list` ещё до упаковки. После создания весь tar проверяется: разрешены только обычные зарегистрированные файлы, запрещены абсолютные пути, `..`, дубли и незаявленные элементы, для каждого payload сверяется SHA-256. Также ограничены число элементов, абсолютный объём распакованных данных и отношение распакованного размера к сжатому.

`verify` принимает только обычный single-link файл, открывает его без перехода по symlink и проверяет неизменность inode, размера, временных меток и числа ссылок на протяжении чтения. Перед проверкой и извлечением restore создаёт такую же проверяемую приватную копию сжатого архива в `/var/backups/remnawave-manager/.restore-source-*`; подмена исходного файла во время копирования останавливает операцию.

Перед остановкой рабочего стека restore полностью извлекает приватную копию архива в staging,
проверяет checksum каждого payload, `docker compose config` и `pg_restore --list`. После
остановки сохраняются исходные файлы и фактически запущенные сервисы. Старая PostgreSQL database
остаётся под временным именем до успешных health-check. При ошибке возвращаются database, файлы,
inventory и исходное состояние сервисов. Если автоматическая компенсация неполна, приватный
каталог `.restore-*` сохраняется вместе с `rollback.json` и точным соответствием снимков путям.

Состояние Nginx сохраняется отдельно. Работающий Compose-сервис Nginx после атомарной
замены bind-mounted файла пересоздаётся и проверяется уже на новом inode. Изначально
остановленный Nginx не запускается: менеджер выполняет `docker compose create
--force-recreate`, затем проверяет конфигурацию одноразовым `docker compose run --rm
--no-deps ... nginx -t`. Тот же исходный running/stopped-снимок используется при
rollback, поэтому падение контейнера на отклонённой конфигурации не превращает
работавший до операции Nginx в остановленный.

Дополнительные WARP-файлы находятся в архиве для ручного recovery и аудита, но помечены как supplemental. Обычный `rwm backup restore` не разворачивает их автоматически поверх системы. WARP install/adopt/rotate имеют собственные snapshots и rollback.

## Создание и проверка

```bash
sudo rwm backup create --reason manual
sudo rwm backup list
sudo rwm backup verify /var/backups/remnawave-manager/ИМЯ_BACKUP.tar.gz
```

Полный manifest можно посмотреть в JSON без распаковки:

```bash
sudo rwm --json backup verify /var/backups/remnawave-manager/ИМЯ_BACKUP.tar.gz
```

Для удаления старых архивов той же роли после успешного создания нового:

```bash
sudo rwm backup create --reason scheduled --retention 10
```

`backup list` и retention рассматривают только обычные single-link `.tar.gz`; symlink, hardlink и специальные файлы игнорируются. Перед удалением retention повторно проверяет inode и метаданные, перемещает выбранный архив в случайный quarantine и удаляет его только после повторной проверки. Подмена останавливает retention, не удаляя подменённый файл. `--retention` принимает от 1 до 1000. Dry-run отсутствует.

Для restore на filesystem каталога backup сначала должно оставаться не меньше размера сжатого архива плюс `256 MiB`; затем staging распаковки отдельно проверяет место по заявленному объёму содержимого. Фактический пиковый расход выше размера исходного `.tar.gz`, поэтому не начинайте восстановление на почти заполненном диске.

## Расписание локальных backup

Менеджер может установить ежедневный или еженедельный systemd timer:

```bash
sudo rwm backup schedule-enable --frequency daily --time 03:30 --retention 10
sudo rwm backup schedule-status
```

Время интерпретируется в локальной временной зоне сервера. Для `weekly` запуск назначается на воскресенье. Таймер сохраняет пропущенный запуск после перезагрузки и использует случайную задержку до 10 минут. Изменение расписания разрешено только для units с ownership-маркером менеджера.

Отключение не удаляет уже созданные архивы:

```bash
sudo rwm backup schedule-disable
```

## Автоматический rollback update

### Panel

Перед update Panel stack все целевые образы загружаются и проверяются без остановки сервисов. Затем менеджер записывает journal, сохраняет фактический running/stopped-набор, останавливает Panel и Subscription Page и только при закрытом write-path создаёт backup с PostgreSQL. Путь backup появляется в journal лишь после полной проверки архива.

Если создание backup не удалось, managed-файлы и БД ещё не изменялись. Менеджер не запускает restore из неполного архива, а возвращает исходный набор сервисов. Если migration, health-check Panel, Subscription Page или nginx завершается позже, менеджер:

1. Останавливает прикладные сервисы.
2. Возвращает managed files из pre-update backup, включая старый Compose с PostgreSQL 18.3.
3. Пересоздаёт контейнер на образе PostgreSQL из сохранённого Compose и восстанавливает dump.
4. Проверяет Compose.
5. Запускает сервисы и ждёт health-check.
6. Проверяет Panel, Subscription Page и nginx.

Восстановление БД сначала выполняется в новой staging database. Рабочая БД переименовывается только после успешного `pg_restore`, затем staging получает рабочее имя. Если переключение имён прерывается, код пытается вернуть прежнее имя старой БД.

При проверенной миграции `2.8.1 → 3.2.0` рабочий update сначала переводит database image с PostgreSQL 18.3 на 18.4 и проверяет его health до старта Panel. Rollback сначала возвращает Compose и PostgreSQL 18.3, а уже затем загружает dump; восстановление dump через несовместимый оставшийся image не выполняется. После успешного update и после rollback менеджер возвращает точный running/stopped-набор, зафиксированный перед окном обслуживания.

### Node

Перед update Node также создаётся backup. При ошибке возвращаются старый Compose image и managed config, после чего проверяются runtime, nginx и сохранённые XHTTP sockets. PostgreSQL для Node отсутствует и не восстанавливается.

Node update не изменяет WARP-конфигурацию. Поэтому автоматический rollback не накатывает supplemental WARP-файлы.

### Другие операции

Ротация защитного URL Panel и замена заглушки создают backup и имеют локальный rollback изменяемых файлов. Чистая установка возвращает созданный Certbot lineage, hooks, DNS credential, состояние timer и исходную конфигурацию UFW при поздней ошибке. Установка, takeover, ротация и uninstall WARP используют собственный оперативный snapshot: ошибка той же команды uninstall возвращает файлы, units и исходное runtime-состояние. Созданный перед uninstall общий backup содержит WARP только как supplemental-данные, поэтому обычный более поздний `rwm backup restore` не разворачивает их автоматически; перед `--purge-credentials` всё равно сохраните учётные данные отдельно от сервера.

## Проверка после сообщения об автоматическом rollback

Если CLI сообщил, что предыдущая версия восстановлена:

```bash
sudo rwm service status
sudo rwm diagnose
sudo rwm backup list
```

Проверьте указанный в сообщении архив:

```bash
sudo rwm backup verify /var/backups/remnawave-manager/ИМЯ_BACKUP.tar.gz
```

Для Panel дополнительно проверьте вход и реальную страницу подписки. Для Node проверьте обычный Reality, XHTTP, CDN и WARP с внешнего клиента.

Не повторяйте update, пока не найдена причина первого сбоя.

## Ручное восстановление Panel

Полный rollback Panel 3 на старую версию требует БД. Используйте pre-update архив, созданный до первого старта новой Panel:

```bash
sudo rwm backup verify /var/backups/remnawave-manager/ИМЯ_PRE_UPDATE_BACKUP.tar.gz
sudo rwm backup restore /var/backups/remnawave-manager/ИМЯ_PRE_UPDATE_BACKUP.tar.gz
```

CLI потребует ввести слово `ВОССТАНОВИТЬ`. Для автоматизации существует `--yes`, но он только отключает подтверждение.

Не добавляйте `--without-database` при полном откате Panel после миграции 3.x. Возврат только старого image/env при уже мигрированной БД не является корректным rollback.

После восстановления:

```bash
sudo rwm service status
sudo rwm diagnose
sudo rwm inventory
sudo rwm security access
```

## Временный доступ к Panel через SSH

Если сама Panel исправна, но защищённый URL или cookie недоступны, полное восстановление backup не требуется. Откройте на 5-120 минут proxy, слушающий только loopback:

```bash
sudo rwm security emergency-open --minutes 30
sudo rwm security emergency-status
```

CLI покажет SSH-команду с пробросом локального порта `8443` и URL для браузера. Доступ автоматически закроется systemd timer. Служба закрытия запускается после Docker и повторяется при временной ошибке, поэтому пропущенный из-за перезагрузки срок не оставляет доступ открытым без повторной попытки. Закрыть его сразу можно командой:

```bash
sudo rwm security emergency-close
```

Открытие создаёт backup и меняет только однозначно найденный nginx-файл Panel. Конфигурация проходит `nginx -t`; ошибка открытия или закрытия возвращает исходный файл. Не открывайте внутренний порт Panel в UFW или Docker Compose для решения этой задачи.

## Ручное восстановление Node

```bash
sudo rwm backup verify /var/backups/remnawave-manager/ИМЯ_PRE_UPDATE_BACKUP.tar.gz
sudo rwm backup restore \
  /var/backups/remnawave-manager/ИМЯ_PRE_UPDATE_BACKUP.tar.gz \
  --without-database
```

Для Node флаг явно показывает, что PostgreSQL не участвует. После restore менеджер ждёт сохранённые sockets и проверяет runtime/nginx.

Если WARP повреждён отдельно, сначала выполните безопасный scan:

```bash
sudo rwm warp scan
```

Не извлекайте WARP credentials из tar поверх `/etc/wireguard` автоматически. Используйте [WARP adoption](warp.md) или ручное восстановление после проверки конкретных файлов.

## Ограничения restore

Restore намеренно отказывается работать, если архив похож на backup другого сервера. Должны совпадать:

- роль;
- install directory;
- пути Compose и env;
- набор и идентификаторы сервисов/контейнеров;
- пути nginx и site roots;
- разрешённые inventory paths;
- PostgreSQL user/database для Panel.

Restore не удаляет произвольные лишние файлы, а атомарно заменяет зарегистрированные файлы из manifest.

Текущий inventory должен читаться. Если `inventory.json` утрачен, а Compose ещё валиден, сначала заново примите ту же установку:

```bash
sudo rwm adopt --path /opt/remnawave --role panel
```

или:

```bash
sudo rwm adopt --path /opt/remnanode --role node
```

После этого снова выполните verify и restore. Если Compose/env повреждены настолько, что adoption невозможен, CLI не будет небезопасно извлекать tar в root filesystem. Сохраните архив неизменным и восстанавливайте конфигурацию вручную только после анализа manifest.

## Незавершённый journal

`rwm diagnose` сообщает об `active-transaction.json`. Это означает, что update или `maintenance archive-stack` не дошли до `committed` либо rollback не смог завершиться.

```bash
sudo rwm diagnose
sudo cat /var/lib/remnawave-manager/active-transaction.json
```

Сначала остановите любые новые update/install/adopt и сохраните отдельную копию journal. Не редактируйте его и не подставляйте другой backup. Поле `operation` определяет процедуру; универсального безопасного `restore` для всех операций нет.

### `panel-update`

Journal содержит исходный `running_services`. В фазах `stopping-applications` и `creating-backup` ключ `backup` может отсутствовать: проверенный транзакционный архив ещё не привязан, а постоянная конфигурация и БД ещё не изменялись. Не выбирайте другой архив. Верните только исходное состояние сервисов после проверки Compose.

Если точный `backup` записан, сначала выполните `backup verify` именно для этого пути. Для фазы после начала миграции используйте полный Panel restore с БД, затем проверьте Panel, Subscription Page, nginx и исходный running/stopped-набор.

### `node-update`

Путь `backup` записывается при создании journal. До появления `running_services` Node ещё не пересоздавалась. Начиная с `recreating-node` проверяйте и восстанавливайте только указанный архив с `--without-database`, затем проверяйте XHTTP sockets, nginx, Reality и WARP-интерфейсы.

### `stack-archive`

До отключения backup timer и `docker compose down` journal фиксирует:

- `archive_targets`: точные пары `original`/`archive` для install directory, inventory и, если он существовал, secrets;
- `created_services`: все существовавшие контейнеры Compose-проекта;
- `running_services`: подмножество фактически запущенных контейнеров;
- точный проверенный `backup`.

Менеджер намеренно не выполняет автоматический rename после аварийного завершения. Для каждой пары из `archive_targets` отдельно проверьте тип и существование обоих точных путей. `original` существует, а `archive` отсутствует — перемещение не произошло. `original` отсутствует, а `archive` существует — произошло. Если существуют оба пути либо не существует ни одного, состояние неоднозначно: ничего не перемещайте и сохраните диск для ручного анализа.

Запрещено выбирать каталог по «самому новому» имени `.removed-*`, использовать wildcard, принимать архивный путь через `rwm adopt` или запускать restore из случайного backup. Суффикс времени не является идентификатором восстановления; единственный источник соответствия — конкретный `active-transaction.json`.

Если принято решение откатить незавершённое архивирование, сначала сделайте побайтовые копии journal и всех существующих сторон пар. Остановите контейнеры этого Compose-проекта, затем вручную верните только те `archive`, чьи точные `original` отсутствуют: install directory, inventory и указанную в journal secrets. Не допускайте перезаписи уже существующего original. После возврата проверьте Compose; пересоздайте только `created_services`, запустите только `running_services` и восстановите расписание backup по сохранённым settings, если оно было отключено.

Если `phase` равна `committed`, все exact archive-пути существуют, original-пути отсутствуют и это было желаемым результатом, стек уже архивирован; rename обратно не нужен. Если у `stack-archive` нет `archive_targets`, не угадывайте: в текущей версии destructive phase ещё не могла начаться, а journal старой/неизвестной версии требует отдельного анализа.

После выбранной процедуры повторите `service status` и `diagnose`. Удалять journal вручную допустимо только после проверки точного конечного состояния и сохранения его копии; само удаление journal ничего не восстанавливает.

## Если restore завершился ошибкой

Не запускайте несколько restore параллельно. Все изменяющие команды используют общий lock, но ручные Docker-команды могут обойти его.

Сохраните:

- исходный backup;
- `active-transaction.json`;
- вывод `rwm service status`;
- вывод `rwm diagnose`;
- текущие Compose/env/nginx без публикации секретов.

Не публикуйте `.env`, PostgreSQL dump, WARP account/private key, Registry token, API token или полный защищённый URL Panel.

## Dry-run

У `backup restore` и `update` нет dry-run. `backup verify` является безопасной проверкой архива без изменения системы. `certificate renew --dry-run` относится только к Certbot и не проверяет rollback Remnawave.

## После восстановления

Обязательный минимум:

```bash
sudo rwm service status
sudo rwm diagnose
sudo rwm inventory
```

Затем проведите прикладной тест извне. Локальный health-check не подтверждает весь маршрут через DNS, CDN и клиентский провайдер.
