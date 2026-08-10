# Обновление Node 3.0.0-3.1.1

Целевой образ Node `3.1.1` привязан к immutable multiarch digest в `compatibility.json`. Обновление меняет только прямое поле `image` сервиса Node в Compose и, для старого стека, исправляет target лога `/var/log/remnanode` на `/var/log/xray`.

Node 3.1.1 сохраняет Xray Core `v26.7.28`, `SECRET_KEY`, runtime-команды `rw-core`/`cli` и Unix-сокеты XHTTP. Перед переключением менеджер тестирует текущий Xray JSON новым core; Reality без явного `minClientVer` требует явного подтверждения риска.

Nginx-конфигурации CDN (Yandex/Beeline GET/POST), XHTTP stream separation, сертификаты, сайт маскировки и WARP не переписываются. Менеджер сохраняет их хеши до и после recreate Node и ждёт обнаруженные XHTTP Unix-сокеты.

Команды:

```bash
sudo rwm adopt --path /opt/remnanode --role node
sudo rwm diagnose
sudo rwm backup create --reason before-node-3.1.1
sudo rwm update node
```
