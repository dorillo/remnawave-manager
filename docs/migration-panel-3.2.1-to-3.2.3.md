# Обновление Panel 3.2.1-3.2.3

Менеджер проверяет и обновляет Panel до `3.2.3`, Subscription Page до `8.0.0` и PostgreSQL до `18.4` по digest из `compatibility.json`.

В Panel 3.2.3 добавлена штатная Prisma-миграция `node_ips`. Она запускается entrypoint Panel после восстановления базы; вручную менять схему или `.env` не нужно. Перед окном обслуживания менеджер создаёт backup базы и проверяет итоговые health/API scopes.

Конфигурация CDN, Subscription Page, XHTTP stream separation, Reality и WARP не переписывается: Compose меняет только прямые `image` выбранных сервисов, а managed nginx/env/site-файлы защищены drift-проверкой и хешами. После запуска менеджер возвращает исходный набор running/stopped-сервисов.

Перед production-обновлением выполните:

```bash
sudo rwm adopt --path /opt/remnawave --role panel
sudo rwm diagnose
sudo rwm backup create --reason before-panel-3.2.3
sudo rwm update panel
```

Если исходный образ не доказан по digest или конфигурация изменилась после adoption, операция останавливается до любых изменений. Не используйте `--accept-unknown-source`, если источник нельзя независимо проверить.
