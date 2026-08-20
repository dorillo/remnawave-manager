# Обновление Node до 3.3.2

Целевой образ Node `3.3.2` привязан к immutable multiarch digest в `compatibility.json`. Обновление меняет только прямое поле `image` сервиса Node в Compose и, для старого стека, исправляет target лога `/var/log/remnanode` на `/var/log/xray`.

Перед обновлением Node обязательно обновите Panel как минимум до `3.3.0`, предпочтительно до текущей `3.3.2`. Node `3.3.2` принимает API только с производным SNI; менеджер остановит переход со старой Node без явного `--panel-3-3-ready`.

Node 3.3.2 сохраняет Xray Core `v26.7.28`, runtime-команды `rw-core`/`cli` и Unix-сокеты XHTTP. Версия строго проверяет CA, подпись node-сертификата, соответствие приватного ключа и срок действия внутри `SECRET_KEY`; менеджер выполняет тот же preflight новым образом до изменения Compose. Затем он тестирует текущий Xray JSON новым core; Reality без явного `minClientVer` требует явного подтверждения риска.

Nginx-конфигурации CDN (Yandex/Beeline GET/POST), XHTTP stream separation, сертификаты, сайт маскировки и WARP не переписываются. Менеджер сохраняет их хеши до и после recreate Node и ждёт обнаруженные XHTTP Unix-сокеты.

Команды:

```bash
sudo rwm adopt --path /opt/remnanode --role node
sudo rwm diagnose
sudo rwm backup create --reason before-node-3.3.2
sudo rwm update --panel-3-3-ready --yes
```
