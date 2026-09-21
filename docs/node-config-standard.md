# Настройки Node и inventory в 0.1.22

Состав транспортов выбирает администратор. Обновление менеджера не добавляет
CDN, XHTTP или WARP и не требует менять IP, TLS, пути, порты или сокеты.
Новые установки Node уже включают gzip для сайта; обновление существующей
Node сохраняет её nginx. Одинаковая версия Node не означает одинаковые nginx.

## Явный провайдер

Внутри каждого XHTTP `location`, рядом с `proxy_pass`, добавьте один комментарий:

```nginx
# remnawave-manager: transport=xhttp provider=yandex
```

Допустимые значения: `none` (прямой XHTTP без CDN), `yandex`, `beeline-get`,
`beeline-post`. Для exact и prefix locations одного транспорта нужен маркер
в обоих блоках. На одной ноде допустимы несколько разных провайдеров.
Upstream переименовывать не требуется. Маркер не отправляется клиенту или CDN.

Менеджер принимает маркер только внутри location с единственным `proxy_pass`
на локальный TCP backend или Unix socket. Не размещайте его над upstream,
в начале файла или в location картинок/API заглушки. Некорректный или оставшийся
без маршрута маркер останавливает сканирование с объяснением ошибки.

Старые характерные имена upstream продолжают распознаваться. Общий upstream
`xray_xhttp` означает XHTTP с неизвестным провайдером, пока не добавлен маркер.
Сам сокет fallback nginx не доказывает наличие XHTTP. Ссылки на CDN в ресурсах
сайта также не определяют провайдера VPN. Маркер отражает явно заданную
конфигурацию; реальную работу CDN проверяйте клиентом.

## Сжатие

Для сайта в HTTP-контексте (в начале `conf.d/default.conf` или внутри `http`)
используйте следующий блок, если эквивалентные директивы ещё не настроены:

```nginx
server_tokens off;
gzip on;
gzip_vary on;
gzip_proxied any;
gzip_min_length 1024;
gzip_comp_level 6;
gzip_types application/javascript application/json application/manifest+json application/wasm application/xml font/eot font/opentype font/otf font/ttf image/svg+xml text/css text/javascript text/plain text/xml;
```

Не дублируйте существующие директивы. В каждом VPN location оставьте `gzip off;`,
`proxy_buffering off;` и `proxy_request_buffering off;`. Сохраните индивидуальные
таймауты, лимиты, заголовки, path и backend из соответствующего гайда.
Отсутствие gzip не ломает VPN; это оптимизация загрузки сайта.

## Проверка и сохранение

Перед правками создайте backup менеджером и запишите выданный путь:

```console
sudo rwm backup create --reason before-node-config-change
sudo rwm backup verify /var/backups/remnawave-manager/ИМЯ_BACKUP.tar.gz
```

После правок проверьте конфигурацию на хосте и внутри контейнера. При bind mount
отдельного файла редактор с атомарным сохранением может заменить inode, оставив
контейнер на старом конфиге: одного `nginx -t` внутри него тогда недостаточно.
Сравните `sha256sum` файла хоста и смонтированного файла. Если они различаются,
сначала проверьте новый файл в изолированном контейнере с теми же mounts; затем
пересоздайте только nginx в согласованное окно. Не перезапускайте Node ради
комментария в nginx. Штатная смена сайта менеджером учитывает замену inode.

Если контейнер видит актуальный файл:

```console
cd /opt/remnanode
sudo docker compose config -q
sudo docker compose exec -T remnawave-nginx nginx -t
sudo docker compose exec -T remnawave-nginx nginx -s reload
sudo rwm inventory --refresh
```

Проверьте сайт, загрузку и отдачу VPN через каждый используемый транспорт.
После проверки осознанно сохраните контрольную точку:

```console
sudo rwm adopt --path /opt/remnanode --role node
```

Пункт меню «Показать inventory» теперь проверяет текущие nginx, контейнеры и
WARP. CLI `rwm inventory` сохраняет прежнюю семантику снимка; `--refresh`
читает текущее состояние без записи inventory, принятия drift и изменения
Certbot. Состояние сертификатов и прочие сведения остаются из сохранённого
inventory; для их проверки используйте `rwm diagnose`.

Флаг `gzip` означает найденную активную директиву в управляемых файлах, а не
проверку сжатия каждого ответа. Учитывайте наследование и внешние include;
для проверки ответа используйте `Accept-Encoding: gzip` и `Content-Encoding`.
Смена сайта сохраняет маркеры и VPN locations; обновление Node сохраняет nginx
и WARP и проверяет контрольные суммы защищённых файлов.
