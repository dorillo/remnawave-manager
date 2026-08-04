# Docker Registry на российских серверах

На серверах с российскими IP Docker Hub или GHCR могут требовать авторизацию, ограничивать анонимные загрузки или быть недоступными из-за сетевой политики провайдера. Remnawave Manager поддерживает вход в Registry без передачи пароля в аргументах процесса.

## Поддерживаемые Registry

CLI принимает только два идентификатора:

| Идентификатор | Docker host | Доступные проверенные компоненты |
| --- | --- | --- |
| `docker-hub` | `docker.io` | Panel 3.2.1, Subscription Page 8.0.0, Node 3.0.0 |
| `ghcr` | `ghcr.io` | Panel 3.2.1, Node 3.0.0 |

Для Panel-сервера выбирайте `docker-hub`. Panel обновляется вместе с Subscription Page, а для Subscription Page 8.0.0 в текущем compatibility manifest нет проверенного GHCR-образа.

Для отдельной Node можно выбрать `docker-hub` или `ghcr`.

Выбор `ghcr` относится только к образу Remnawave Node или Panel. Чистая установка отдельной Node дополнительно загружает официальный nginx с Docker Hub; Panel stack также использует оттуда Subscription Page, PostgreSQL, Valkey и nginx. Поэтому GHCR может полностью обойти Docker Hub при обновлении уже установленной Node, но не при чистой установке. Если российский адрес требует авторизацию Docker Hub, войдите и в `docker-hub`, даже когда для самой Node выбран `ghcr`.

Пользовательские зеркала и произвольные Registry CLI сейчас не поддерживает. Не подменяйте image вручную перед update: это нарушит проверку исходной версии и digest.

## Интерактивный вход

Docker Hub:

```bash
sudo rwm registry login \
  --registry docker-hub \
  --username ВАШЕ_ИМЯ \
  --select
```

GHCR для образа отдельной Node:

```bash
sudo rwm registry login \
  --registry ghcr \
  --username ВАШЕ_ИМЯ_GITHUB \
  --select
```

CLI запросит пароль или access token через защищённый Python `getpass`. Затем он выполнит эквивалент `docker login HOST --username USER --password-stdin`: секрет передаётся Docker через stdin и отсутствует в argv. Если защищённый терминал недоступен, менеджер не переходит к обычному отображаемому вводу.

`--select` одновременно делает этот Registry источником будущих install/update. Без него выполняется только login.

Для чистой установки Node с выбранным GHCR сначала отдельно авторизуйте Docker Hub без `--select`, затем авторизуйте и выберите GHCR:

```bash
sudo rwm registry login --registry docker-hub --username ВАШЕ_ИМЯ
sudo rwm registry login --registry ghcr --username ВАШЕ_ИМЯ_GITHUB --select
```

Выбрать уже авторизованный Registry можно отдельно:

```bash
sudo rwm registry select docker-hub
sudo rwm registry select ghcr
```

## Вход через переменную окружения

Для автоматизации поддерживается `RWM_REGISTRY_PASSWORD`. Не размещайте token в shell-скрипте, unit-файле или истории команд.

В интерактивной root-сессии:

```bash
sudo -i
read -rsp 'Docker Registry token: ' RWM_REGISTRY_PASSWORD
printf '\n'
export RWM_REGISTRY_PASSWORD
rwm registry login --registry docker-hub --username ВАШЕ_ИМЯ --select
unset RWM_REGISTRY_PASSWORD
exit
```

Если переменная уже безопасно задана в текущем окружении:

```bash
sudo --preserve-env=RWM_REGISTRY_PASSWORD \
  rwm registry login --registry docker-hub --username ВАШЕ_ИМЯ --select
unset RWM_REGISTRY_PASSWORD
```

Не используйте несуществующие параметры `--password` или `--token`: CLI намеренно их не предоставляет.

Переменная окружения не попадает в argv, но её могут читать привилегированные процессы. На обычном интерактивном сервере скрытый prompt предпочтительнее.

## Проверка состояния

```bash
sudo rwm registry status
```

Команда показывает:

- выбранный менеджером Registry;
- hosts, для которых Docker config текущего пользователя содержит авторизацию.

Менеджер и Docker обычно запускаются через `sudo`, поэтому значимым является Docker config пользователя root. Login, status и update нужно выполнять в одном и том же контексте root. Авторизация обычного пользователя не гарантирует, что её увидит `sudo rwm`.

Для JSON:

```bash
sudo rwm --json registry status
```

## Выход

```bash
sudo rwm registry logout --registry docker-hub
sudo rwm registry logout --registry ghcr
```

Logout требует подтверждения и вызывает `docker logout` для соответствующего host. Он не меняет выбранный Registry в настройках менеджера. При необходимости после logout выполните `rwm registry select` для другого источника.

## Как выполняется pull

Образы Remnawave для install/update выбираются из compatibility manifest. PostgreSQL при update
также описан в manifest. Базовые образы чистой установки PostgreSQL, Valkey и nginx закреплены
отдельными константами с полным `sha256` digest; они всегда загружаются с Docker Hub. После pull
Remnawave-образов менеджер читает локальные `RepoDigests` и сравнивает digest с проверенным
manifest. Несовпадение останавливает операцию.

Registry login не отключает digest-проверку и не разрешает `latest`.

## Что login решает, а что нет

Login может решить:

- требование обязательной авторизации;
- ограничение анонимных pull;
- доступ к пакету, для которого вашей учётной записи выданы права.

Login не решает:

- DNS-блокировку Registry;
- TCP timeout или блокировку TLS;
- запрет маршрута у хостинг-провайдера;
- отсутствие компонента в выбранном Registry;
- неверный digest в compatibility manifest.

Если после успешного login pull получает timeout, проверьте DNS, исходящий TCP/443, системное время, прокси Docker daemon и правила провайдера. Менеджер не меняет сетевые настройки Docker и не устанавливает обход блокировок Registry.

## Типовые ошибки

### `unauthorized` или `denied`

Повторите login под root и проверьте права access token:

```bash
sudo rwm registry login --registry docker-hub --username ВАШЕ_ИМЯ
sudo rwm registry status
```

Для GHCR token должен позволять вашей учётной записи читать нужный package. Точный набор прав зависит от политики GitHub-аккаунта.

### Panel с выбранным `ghcr`

Переключитесь на Docker Hub до install/update Panel stack:

```bash
sudo rwm registry select docker-hub
sudo rwm registry status
```

### Исходная версия не определена

Это не ошибка login. Менеджер не смог сопоставить текущий image с поддерживаемым digest/tag. Не применяйте `--accept-unknown-source`, пока вручную не проверите происхождение запущенного образа.

### Docker хранит credentials в config.json

Менеджер передаёт секрет безопасно через stdin, но дальнейшее хранение выполняет Docker. Без настроенного credential helper Docker может сохранить auth-данные в `/root/.docker/config.json` в кодированном, но не шифрованном виде. Ограничьте права на root home и используйте отдельный token с минимальными правами.

## Ссылки

- [официальная справка `docker login`](https://docs.docker.com/reference/cli/docker/login/)
- [матрица совместимости проекта](../src/remnawave_manager/data/compatibility.json)
- [миграция Panel](migration-panel-2.8.1-to-3.2.1.md)
- [обновление Node](update-node-2.8.0-to-3.0.0.md)
