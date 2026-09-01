# Обновление Panel до 3.4.3

Менеджер проверяет и обновляет Panel до `3.4.3`, Subscription Page до `8.0.0` и PostgreSQL до `18.4` по digest из `compatibility.json`.

В Panel 3.4.0-3.4.1 добавлены штатные Prisma-миграции режимов привязки Host к Internal Squads и тегов сущностей. Новые параметры генерации short UUID имеют безопасные upstream defaults, поэтому существующий `.env` остаётся валидным. Panel 3.4.2 исправляет конкурентную регистрацию HWID, а 3.4.3 закрывает обход аутентификации backend-tools; новых миграций и обязательных ENV после 3.4.1 нет. Миграции запускаются entrypoint Panel; вручную менять схему или `.env` не нужно. Перед окном обслуживания менеджер создаёт backup базы, а при ошибке восстанавливает dump и прежние образы. После запуска проверяются Panel, Subscription Page, обязательные API scopes и nginx.

Конфигурация CDN, Subscription Page, XHTTP stream separation, Reality и WARP не переписывается: Compose меняет только прямые `image` выбранных сервисов, а managed nginx/env/site-файлы защищены drift-проверкой и хешами. После запуска менеджер возвращает исходный набор running/stopped-сервисов.

Перед production-обновлением выполните:

```bash
sudo rwm adopt --path /opt/remnawave --role panel
sudo rwm diagnose
sudo rwm backup create --reason before-panel-3.4.3
sudo rwm update --yes
```

Если исходный образ не доказан по digest или конфигурация изменилась после adoption, операция останавливается до любых изменений. Не используйте `--accept-unknown-source`, если источник нельзя независимо проверить.
