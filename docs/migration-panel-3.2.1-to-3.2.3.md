# Обновление Panel до 3.3.2

Менеджер проверяет и обновляет Panel до `3.3.2`, Subscription Page до `8.0.0` и PostgreSQL до `18.4` по digest из `compatibility.json`.

В Panel 3.3.0 добавлены штатные Prisma-миграции host mapper, node integrations и shared lists. Версии 3.3.1-3.3.2 исправляют Torrent Blocker, сортировку Node Plugins и ссылки UI, не добавляя env или миграций БД. Миграции запускаются entrypoint Panel; вручную менять схему или `.env` не нужно. Перед окном обслуживания менеджер создаёт backup базы и проверяет итоговые health/API scopes.

Конфигурация CDN, Subscription Page, XHTTP stream separation, Reality и WARP не переписывается: Compose меняет только прямые `image` выбранных сервисов, а managed nginx/env/site-файлы защищены drift-проверкой и хешами. После запуска менеджер возвращает исходный набор running/stopped-сервисов.

Перед production-обновлением выполните:

```bash
sudo rwm adopt --path /opt/remnawave --role panel
sudo rwm diagnose
sudo rwm backup create --reason before-panel-3.3.2
sudo rwm update --yes
```

Если исходный образ не доказан по digest или конфигурация изменилась после adoption, операция останавливается до любых изменений. Не используйте `--accept-unknown-source`, если источник нельзя независимо проверить.
