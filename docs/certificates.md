# Управление TLS-сертификатами

Менеджер поддерживает четыре метода при чистой установке Panel или Node:

- `existing`: проверить и скопировать существующие `fullchain.pem` и `privkey.pem`;
- `http-01`: выпустить сертификат конкретных FQDN через standalone Certbot;
- `cloudflare`: выпустить сертификат через Cloudflare DNS-01;
- `gcore`: выпустить сертификат через Gcore DNS-01.

Cloudflare token читается из `RWM_CLOUDFLARE_TOKEN`, Gcore token — из
`RWM_GCORE_TOKEN`. В argv токены не принимаются. Credential-файлы создаются с правами `0600`
в стабильном каталоге `/etc/remnawave-manager/certbot`, поэтому архивирование каталога стека
не ломает renewal-конфигурацию.

Чистая установка выдаёт сертификат транзакционно. Если дальнейший запуск контейнеров,
регистрация Panel, health-check или настройка UFW завершается ошибкой, менеджер удаляет только
lineage, созданный этой попыткой, и возвращает manager-owned hooks, DNS credential и исходное
состояние `certbot.timer`. Уже существующий lineage не перезаписывается и не удаляется: повторное
использование разрешено только при точном совпадении метода challenge и полного набора SAN.
Для DNS-01 renewal-конфигурация дополнительно должна ссылаться на ожидаемый стабильный credential.
Смена Cloudflare/Gcore/HTTP-01 поверх lineage с тем же именем останавливается до изменения файлов.

Корневой `install.sh` ставит Cloudflare plugin из Ubuntu Universe. Официальный Gcore plugin
отсутствует в Ubuntu 24.04, поэтому установщик загружает только wheel
`certbot-dns-gcore==0.1.8`, проверяет закреплённый SHA-256, устанавливает его без зависимостей
в системный Python и убеждается, что plugin виден команде `certbot plugins`. Благодаря этому
стандартный `certbot.timer` использует тот же plugin при автопродлении.

## Выпуск для нового домена

Как и старый `remnawave-reverse-proxy`, команда `issue` создаёт отдельный Certbot lineage,
но не меняет рабочую конфигурацию Panel или Node:

```bash
sudo rwm certificate issue \
  --domain new.example.com \
  --method http-01 \
  --email admin@example.com
```

Для DNS-01 можно добавить wildcard SAN. Передавайте literal-домен: для `example.com` будут
выпущены `example.com` и `*.example.com`; менеджер не пытается самостоятельно вычислять
registrable domain.

```bash
export RWM_GCORE_TOKEN='API_TOKEN'
sudo --preserve-env=RWM_GCORE_TOKEN rwm certificate issue \
  --domain example.com \
  --method gcore \
  --wildcard \
  --email admin@example.com
unset RWM_GCORE_TOKEN
```

Wildcard через HTTP-01 отклоняется. Существующий lineage с именем, совпадающим с
нормализованным `--domain`, команда `issue` не перезаписывает; для него используйте
`certificate renew`.

При HTTP-01 менеджер определяет состояние nginx и останавливает только активный системный
service или контейнер. После Certbot nginx возвращается в исходное состояние. При ошибке
восстанавливаются manager-owned hooks и DNS credentials, а созданный незавершённый lineage
удаляется с проверкой результата.

## Что не меняется

Выпуск сертификата не меняет:

- домены в `.env` и Docker Compose;
- `server_name` и пути сертификатов в nginx;
- Hosts, Reality `serverNames` и другие объекты Remnawave API;
- DNS-записи и CDN-конфигурацию.

Это намеренная граница безопасности. Смена рабочего домена затрагивает несколько независимых
контрактов и выполняется отдельно после проверки конкретной топологии.

## Продление

```bash
sudo rwm certificate status
sudo rwm certificate renew --dry-run
sudo rwm certificate renew
sudo rwm certificate repair-renewal
sudo rwm certificate reload
```

Adoption и `repair-renewal` заменяют только узнаваемые hooks старого скрипта и не трогают
произвольные пользовательские hooks. Для standalone renewal manager-owned pre/post hooks
останавливают nginx только если он был активен. Deploy hook сначала выполняет `nginx -t`,
затем reload.

Hooks Certbot и изменяющие команды `rwm` используют общий
`/run/remnawave-manager/manager.lock`. Hook ожидает lock не более 120 секунд; если в это время
идёт длительное обновление или восстановление, Certbot завершит попытку ошибкой и повторит её
по следующему расписанию. Во время standalone renewal создаётся PID-marker
`/run/remnawave-manager-certbot-nginx-PID`, поэтому менеджер не начнёт другую изменяющую
операцию между остановкой и возвратом nginx.

Если Certbot или сервер аварийно завершился после pre-hook, marker намеренно остаётся, а новые
изменяющие команды блокируются. Проверьте PID из имени marker, `systemctl status certbot.timer`
и состояние nginx. Если процесса уже нет, при необходимости запустите nginx, проверьте
`nginx -t`, затем удалите только проверенный stale marker. Не удаляйте marker живого процесса:
post-hook использует его, чтобы восстановить исходное состояние nginx.

Локальный stack backup не является полной копией `/etc/letsencrypt`. Для восстановления после
потери диска отдельно храните защищённую копию `/etc/letsencrypt` и
`/etc/remnawave-manager/certbot`; не переносите credential-файлы с правами шире `0600`.
