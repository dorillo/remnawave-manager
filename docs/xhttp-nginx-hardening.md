# XHTTP через nginx: уникальный путь и снижение шаблонности

Инструкция относится к отдельной Node, установленной `remnawave-manager`, и к
Remnawave Node `3.0.0`. В этом образе используется Xray Core `v26.7.28`.

Путь XHTTP не является паролем и не делает соединение невидимым. Он только
отделяет XHTTP от обычного сайта и уменьшает вероятность обнаружения простым
сканированием известных путей. На вид трафика также влияют TLS-отпечаток
клиента, HTTP-версия, режим XHTTP, домен, обычный сайт на `/` и профиль
нагрузки. Поэтому цель этой настройки: убрать массовый шаблон `/xhttppath/`,
не выдавая это за полноценную защиту от анализа трафика.

## Правила для пути

- Создавайте отдельный путь для каждой Node.
- Используйте не менее 18 случайных байт.
- Не включайте в путь слова `xhttp`, `vless`, `vpn`, `proxy` или имя Node.
- Начальный и конечный `/` должны совпадать в Xray, nginx и Host Remnawave.
- Не публикуйте рабочий путь в issue, скриншотах и общих чатах.

Получить случайный идентификатор на сервере:

```console
openssl rand -hex 18
```

Например, для обычного сайта можно использовать один из форматов:

```text
/assets/СЛУЧАЙНЫЙ_HEX/
/api/v1/events/СЛУЧАЙНЫЙ_HEX/
```

Выберите формат, который не конфликтует с реальными URL сайта. Дальше в
примерах значение обозначено как `XHTTP_PATH`. Не вставляйте это слово в
рабочую конфигурацию.

## Резервная копия

Перед изменением установленной Node:

```console
sudo rwm diagnose
sudo rwm backup create --reason before-xhttp-change
```

Изменения Config Profile в Panel не входят в локальную резервную копию Node.
Сохраните его текущий JSON отдельно средствами Panel.

## XHTTP inbound

Добавьте объект в массив `inbounds` Config Profile. Для каждой Node задайте
уникальные `tag`, `listen` и `path`:

```json
{
  "tag": "NODE_XHTTP",
  "listen": "/dev/shm/xhttp-node.socket,0666",
  "protocol": "vless",
  "settings": {
    "clients": [],
    "fallbacks": [],
    "decryption": "none"
  },
  "sniffing": {
    "enabled": true,
    "destOverride": [
      "http",
      "tls",
      "quic"
    ]
  },
  "streamSettings": {
    "network": "xhttp",
    "xhttpSettings": {
      "mode": "auto",
      "path": "XHTTP_PATH",
      "extra": {
        "noSSEHeader": true
      }
    }
  }
}
```

`clients: []` оставьте пустым: разрешённых пользователей добавляет Remnawave.
Параметры padding, packet size и XMUX здесь намеренно не дублируются. Для
Xray `v26.7.28` их отсутствующие значения нормализуются самим ядром.

## Почему Docker-шаг из исходного гайда не нужен

`remnawave-manager` уже добавляет общий bind mount `/dev/shm:/dev/shm:rw` в
сервисы `remnanode` и `remnawave-nginx`. Через него nginx и Xray видят один и
тот же Unix socket. Проверьте сгенерированный Compose:

```console
cd /opt/remnanode
sudo docker compose config | grep -n /dev/shm
```

В выводе `/dev/shm` должен присутствовать у обоих сервисов. Не добавляйте
второй mount и не заменяйте Compose фрагментом из стороннего гайда. Для
маршрута XHTTP достаточно изменить Config Profile и `nginx.conf`.

## Маршрут nginx

В `/opt/remnanode/nginx.conf` найдите `server` нужного домена с директивой
`listen unix:/dev/shm/nginx.sock ssl proxy_protocol;`. Перед существующим
`location /` добавьте блок, заменив `XHTTP_PATH` и путь сокета на значения из
inbound:

```nginx
location ^~ XHTTP_PATH {
    client_max_body_size 2m;
    client_body_timeout 5m;

    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $proxy_protocol_addr;
    proxy_set_header X-Forwarded-For $proxy_protocol_addr;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header Connection "";

    proxy_buffering off;
    proxy_request_buffering off;
    proxy_read_timeout 315s;
    proxy_send_timeout 5m;
    proxy_pass http://unix:/dev/shm/xhttp-node.socket;
}
```

`^~` не даёт общему regex для статических файлов перехватить XHTTP URL.
Ограничение `2m` достаточно для стандартного `scMaxEachPostBytes` в
`1 000 000` байт и безопаснее неограниченного `client_max_body_size 0`.

Проверьте Compose и nginx до reload:

```console
cd /opt/remnanode
sudo docker compose config -q
sudo docker compose exec -T remnawave-nginx nginx -t
sudo docker compose exec -T remnawave-nginx nginx -s reload
```

Если Config Profile ещё не применён, отсутствие нового Unix socket ожидаемо.
Nginx принимает такую конфигурацию, но XHTTP заработает только после запуска
inbound с тем же сокетом.

## Host в Remnawave

Создайте Host для нового inbound и проверьте:

| Поле | Значение |
| --- | --- |
| Address | домен этой Node |
| Port | `443` |
| SNI | тот же домен |
| Security | `TLS` |
| Fingerprint | `chrome` |
| ALPN | `h2,http/1.1` |
| Path | тот же `XHTTP_PATH` |
| Mode | `auto` |
| Inbound | новый `NODE_XHTTP` |

Для обычной Node без отдельного download-домена поле **xHTTP Extra** можно
оставить пустым или указать пустой JSON-объект:

```json
{}
```

В Xray `v26.7.28` значения из распространённого примера
`xPaddingBytes: "100-1000"`, `scMaxEachPostBytes: 1000000`,
`scMinPostsIntervalMs: 30` и `scStreamUpServerSecs: "20-80"` уже являются
дефолтами. `noGRPCHeader: false` тоже является дефолтом и в схеме
`auto + TLS` не влияет на packet-up запросы. Их явное повторение не улучшает
маскировку.

Единственное существенное отличие полного примера из публичного гайда:
`xmux.maxConcurrency: "16-32"` вместо стандартных трёх соединений. Если эта
настройка нужна после измерения нагрузки, достаточно минимального блока:

```json
{
  "xmux": {
    "maxConcurrency": "16-32",
    "hMaxRequestTimes": "600-900",
    "hMaxReusableSecs": "1800-3000"
  }
}
```

Он эквивалентен прежнему XMUX-поведению: пропущенные нулевые поля остаются
нулевыми. На ненагруженной Node сначала используйте `{}` и меняйте XMUX только
по результатам теста задержки и стабильности.

Не добавляйте `downloadSettings` с `another.domain` из примера. Этот блок
нужен только при реально настроенном отдельном download-домене, сертификате,
DNS и маршруте nginx. Значение-заглушка ломает соединение.

## Проверка

1. Разрешите новый inbound сначала только тестовому Internal Squad.
2. Обновите подписку на тестовом клиенте и убедитесь, что в ней указан новый
   путь.
3. Подключитесь через XHTTP и проверьте загрузку и отдачу, а не только ping.
4. Убедитесь, что `https://ДОМЕН/` по-прежнему открывает обычный сайт.
5. Просмотрите ошибки Node и nginx.

```console
sudo rwm service status
sudo docker compose -f /opt/remnanode/docker-compose.yml logs \
  --tail=100 remnanode remnawave-nginx
```

Обычный `curl` к непубличному пути не воспроизводит XHTTP-сессию и сам по себе
не является проверкой транспорта.

После успешного внешнего теста обновите доверенную базовую линию менеджера:

```console
sudo rwm --json adopt --path /opt/remnanode --role node
sudo rwm --json inventory
```

Текущая версия manager распознаёт и nginx listener socket, и Xray socket из
прямого `proxy_pass http://unix:/...socket;`. Для этой схемы в inventory должны
появиться оба пути. Наличие Xray socket можно дополнительно проверить:

```console
sudo test -S /dev/shm/xhttp-node.socket && echo "XHTTP socket: OK"
```

## Ротация на работающей Node

Не меняйте старый путь одновременно во всех местах, если клиенты уже получили
его в подписках. Сохранённая на устройстве конфигурация не обновится сама и
сразу перестанет подключаться.

Для ротации без резкого отключения:

1. Создайте второй inbound с новым `tag`, сокетом и путём, не удаляя старый.
2. Добавьте второй `location` nginx и выполните `nginx -t` и reload.
3. Создайте второй Host и включите его только тестовому Squad.
4. После теста выдайте новый Host нужным пользователям, оставив старый на
   переходный период.
5. Попросите клиентов обновить подписку и дождитесь выбранного срока ротации.
6. Отключите старый Host, затем удалите старый inbound и `location`.
7. Повторите `rwm adopt` и проверьте inventory.

На нескольких Node повторяйте процедуру по одной: путь, tag и socket каждой
Node должны быть уникальными.

## Что остаётся заметным

Случайный path защищает только от поиска массового литерала `/xhttppath/`.
Он не скрывает IP-адрес, SNI, сертификат, TLS ClientHello, объём и длительность
соединений и особенности XHTTP. Реальный статический сайт на корне, корректный
сертификат, отдельный домен Node и отсутствие лишних открытых портов важнее,
чем подбор «красивого» URL.

Для сайта-заглушки менеджера доступны команды:

```console
sudo rwm disguise list
sudo rwm disguise apply 03-morrow-coffee
```

Не размещайте Panel, Subscription Page и Node на одном публичном домене.

## Источники значений

- [Dockerfile Remnawave Node 3.0.0](https://github.com/remnawave/node/blob/3.0.0/Dockerfile)
  фиксирует Xray Core `v26.7.28`.
- [Разбор XHTTP-конфигурации Xray v26.7.28](https://github.com/XTLS/Xray-core/blob/v26.7.28/infra/conf/transport_method.go)
  задаёт XMUX defaults и правила поля `extra`.
- [Нормализация XHTTP-параметров](https://github.com/XTLS/Xray-core/blob/v26.7.28/transport/internet/splithttp/config.go)
  задаёт стандартные размеры POST и интервалы.
- [Нормализация padding](https://github.com/XTLS/Xray-core/blob/v26.7.28/transport/internet/splithttp/xpadding.go)
  задаёт диапазон `100-1000`.

Версия ядра на сервере не гарантирует ту же версию в клиентском приложении.
Перед массовой выдачей нового Host проверьте каждый поддерживаемый тип клиента.
