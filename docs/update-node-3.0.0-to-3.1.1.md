# Обновление Node до 3.3.2

Целевой образ Node `3.3.2` привязан к immutable multiarch digest в `compatibility.json`. Обновление меняет только прямое поле `image` сервиса Node в Compose и, для старого стека, исправляет target лога `/var/log/remnanode` на `/var/log/xray`.

Перед обновлением Node обязательно обновите Panel как минимум до `3.3.0`, предпочтительно до текущей `3.3.2`. Node `3.3.2` принимает API только с производным SNI; менеджер остановит переход со старой Node без явного `--panel-3-3-ready`.

Node 3.3.2 сохраняет Xray Core `v26.7.28`, runtime-команды `rw-core`/`cli` и Unix-сокеты XHTTP. Версия строго проверяет CA, подпись node-сертификата, соответствие приватного ключа и срок действия внутри `SECRET_KEY`; менеджер выполняет тот же preflight новым образом до изменения Compose. Затем он тестирует текущий Xray JSON новым core; Reality без явного `minClientVer` требует явного подтверждения риска.

Если старый `SECRET_KEY` принят предыдущей Node, но отклонён 3.3.2, интерактивный `rwm update` попросит новый ключ, выданный обновлённой Panel через `/api/keygen` (или скопированный из настроек Node). Ключ сначала проходит preflight образом 3.3.2, затем менеджер транзакционно заменяет его в `.env` либо в прямом `environment:` сервиса Node и обновляет Compose. Ввод принимает чистый ключ, строку `SECRET_KEY=…`/`SECRET_KEY: …` из Compose и JSON-ответ `/api/keygen`; перед сохранением оболочка удаляется. Для неинтерактивного запуска передайте новый ключ только через окружение: `RWM_NODE_SECRET_KEY='…' sudo --preserve-env=RWM_NODE_SECRET_KEY rwm update ...`; после запуска удалите переменную.

Если вставленный ключ снова не проходит именно проверку payload, менеджер предложит получить его напрямую с Panel: укажите URL Panel (`https://panel.example.com` или `https://panel.example.com/api`) и admin API token с правом `keygen:get`. Ключ не выводится и не передаётся в argv. Если Panel закрыта cookie-gate, перед запуском менеджера передайте cookie именно ему: `RWM_PANEL_COOKIES_JSON='{"rwm_access":"…"}' sudo --preserve-env=RWM_PANEL_COOKIES_JSON rwm update ...`. Это не `REMNAWAVE_COOKIES_JSON` VPN-сайта: она не попадает в процесс менеджера. Это исключает искажение при копировании; если и ответ API не проходит preflight, Panel фактически не выдаёт совместимый ключ и обновление безопасно остановится до изменения Node.

Nginx-конфигурации CDN (Yandex/Beeline GET/POST), XHTTP stream separation, сертификаты, сайт маскировки и WARP не переписываются. Менеджер сохраняет их хеши до и после recreate Node и ждёт обнаруженные XHTTP Unix-сокеты.

Команды:

```bash
sudo rwm adopt --path /opt/remnanode --role node
sudo rwm diagnose
sudo rwm backup create --reason before-node-3.3.2
sudo rwm update --panel-3-3-ready --yes
```
