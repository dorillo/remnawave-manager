# Обновление Node 2.8.0 на 3.0.0

Инструкция предназначена для отдельного Node-сервера Ubuntu 24.04 с nginx. Совмещённая Panel+Node установка менеджером не поддерживается.

Целевой образ Node 3.0.0 привязан к digest в compatibility manifest. Обновление меняет только прямое поле `image` сервиса Node в Compose. Комментарии, окончания строк и YAML anchors сохраняются.

## Что проверяется до переключения

`rwm update` выполняет следующие проверки:

1. Inventory имеет роль `node` и содержит компонент `node`.
2. Все защищаемые файлы совпадают с хешами adoption.
3. Текущий образ относится к поддерживаемой версии 2.8.0 или 3.0.0.
4. Создан и проверен локальный backup.
5. Новый образ загружен и его digest совпал с manifest.
6. Текущий runtime Xray JSON успешно получен командой Node `cli --dump-config-raw`.
7. Reality inbounds проверены на наличие `minClientVer`.
8. Этот JSON проходит `rw-core run -test` из нового образа в изолированном контейнере.

Тестовый контейнер запускается без сети, с read-only root filesystem, без capabilities и с `no-new-privileges`.

## Reality и минимальная версия клиента

Новый core Node 3.0.0 использует значение `26.3.27` по умолчанию, если в Reality inbound отсутствует явный `minClientVer`.

Если такие inbounds найдены, update остановится. Сначала обновите клиенты и убедитесь, что они поддерживают требуемую версию. После этого повторите:

```bash
sudo rwm update --accept-reality-client-risk
```

Флаг только подтверждает осознанный риск. Он не меняет Xray JSON, не добавляет `minClientVer` и не устанавливает небезопасное `0.0.0`.

Если во всех Reality inbounds уже есть явное значение, флаг не нужен.

## Сохранение nginx, XHTTP, CDN и WARP

До пересоздания Node менеджер фиксирует:

- хеши всех managed files, кроме самого Compose;
- существующие XHTTP Unix sockets из inventory;
- обнаруженные WARP-интерфейсы.

После запуска Node 3.0.0 он:

- проверяет runtime Node;
- ждёт возврата ранее существовавших sockets;
- сравнивает защищаемые файлы с исходными хешами;
- проверяет, что WARP-интерфейсы не исчезли.

nginx-сервис не переключается на другой image и не пересоздаётся этой операцией. Конфигурации Яндекс CDN и Beeline CDN GET/POST не переписываются. Подробная процедура контроля: [сохранение XHTTP и CDN](xhttp-yandex-preservation.md).

## 1. Установите менеджер и войдите в Registry

```bash
chmod +x install.sh
sudo ./install.sh
```

Повторный запуск установщика собирает отдельный versioned venv внутри `/opt/remnawave-manager`, проверяет его и атомарно переключает ожидаемую ссылку `/usr/local/bin/rwm`; предыдущее рабочее окружение сохраняется. Если путь занят чужой установкой, скрипт остановится без перезаписи; не создавайте маркер вручную для обхода проверки.

При необходимости:

```bash
sudo rwm registry login --registry docker-hub --username ВАШЕ_ИМЯ --select
sudo rwm registry status
```

Для отдельной Node также доступен проверенный образ `ghcr`, если он доступнее из вашей сети:

```bash
sudo rwm registry login --registry ghcr --username ВАШЕ_ИМЯ --select
sudo rwm registry status
```

Пароль или token передаётся Docker через stdin, а не argv. Подробности: [registry-russia.md](registry-russia.md).

## 2. Выполните adoption

```bash
sudo rwm adopt --path /opt/remnanode --role node
sudo rwm inventory
sudo rwm diagnose
sudo rwm service status
```

Если Compose монтирует сертификаты из `/etc/letsencrypt`, adoption проверит renewal-конфигурации, установит hooks для nginx и включит `certbot.timer`. Узнаваемые legacy hooks/cron старого скрипта заменяются транзакционно, а пользовательские задания остаются без изменений. Повторная команда ремонта: `sudo rwm certificate repair-renewal`.

Для JSON-вывода особенностей inventory:

```bash
sudo rwm --json inventory
```

До обновления проверьте, что в inventory отражены используемые sockets, WARP и признаки XHTTP/Яндекс CDN/Beeline CDN. Если нужные nginx-файлы не попали в managed files, update не сможет доказать их сохранность. Исправьте bind mounts или расположение конфигурации и повторите adoption.

Старый установщик мог оставить `.env`, Compose и nginx с правами `0644`. Если
`diagnose` сообщает такую ошибку, выполните `sudo rwm diagnose --repair-permissions`,
затем повторите `sudo rwm diagnose`. Update останавливается до загрузки образов,
backup или остановки сервисов, пока приватные managed-файлы не имеют владельца
root и режим `0600`.

## 3. Проверьте WARP отдельно

Если Node использует WARP:

```bash
sudo rwm warp scan
```

Для уже принятой менеджером конфигурации:

```bash
sudo rwm warp status
```

Legacy WARP не нужно переустанавливать непосредственно перед Node update. Сначала изучите [процедуру WARP adoption](warp.md).

## 4. Создайте и проверьте backup

```bash
sudo rwm backup create --reason before-node-3.0.0
sudo rwm backup list
sudo rwm backup verify /var/backups/remnawave-manager/ИМЯ_BACKUP.tar.gz
```

Update создаст ещё один pre-update backup автоматически. Архивы остаются локально.

Node backup содержит managed config и копии дополнительных WARP-файлов. Команда обычного restore автоматически возвращает managed config, но не разворачивает дополнительные WARP-файлы поверх системы. WARP-операции имеют собственные проверки и rollback.

## 5. Dry-run

Для Node update нет dry-run. Команды `rwm update --dry-run` не существует. Проверка нового core встроена в реальный update, но выполняется до переключения Compose image. Если Xray JSON несовместим, образ Node не будет изменён.

Используйте перед окном обслуживания:

```bash
sudo rwm diagnose
sudo rwm service status
sudo rwm backup verify /var/backups/remnawave-manager/ИМЯ_BACKUP.tar.gz
```

## 6. Запустите update

Для конфигурации без проблемного Reality:

```bash
sudo rwm update
```

После подтверждённого обновления всех Reality-клиентов, если менеджер сообщил об отсутствующем `minClientVer`:

```bash
sudo rwm update --accept-reality-client-risk
```

Если digest исходной версии не определяется, update остановится. `--accept-unknown-source` допустим только после самостоятельной проверки фактически запущенного образа:

```bash
sudo rwm update --accept-unknown-source
```

Не используйте этот флаг для обхода configuration drift или ошибки Xray test.

## 7. Проверка после update

```bash
sudo rwm service status
sudo rwm diagnose
sudo rwm inventory
```

Для WARP:

```bash
sudo rwm warp status
```

Обязательно проведите внешние прикладные тесты:

- обычный TCP Reality;
- XHTTP TLS через nginx;
- каждый вариант stream separation;
- подключение через Яндекс CDN;
- домены, которые должны идти через WARP;
- сайт-заглушка на прямом HTTPS-запросе.

Менеджер проверяет runtime, файлы, sockets и интерфейс, но не может имитировать реальный путь клиента через внешний CDN.

## Автоматический rollback

Если после переключения не проходит runtime, не возвращается socket, меняется защищаемый файл или исчезает WARP-интерфейс, менеджер восстанавливает pre-update Node config и старый Compose image из backup без восстановления PostgreSQL.

Если автоматический rollback не завершился:

```bash
sudo rwm backup list
sudo rwm backup verify /var/backups/remnawave-manager/ИМЯ_BACKUP.tar.gz
sudo rwm service status
sudo rwm diagnose
```

Затем следуйте [инструкции recovery](rollback-recovery.md). Не запускайте update повторно до выяснения причины.

## Что update не меняет

- nginx-конфигурацию;
- сайт-заглушку;
- XHTTP и stream separation настройки;
- конфигурацию Яндекс CDN;
- WARP profile, account, systemd units, маршруты и DNS;
- Panel API Config Profile;
- правила UFW;
- TLS-сертификаты.

Изменения этих элементов выполняются отдельными командами и не входят в Node update.
