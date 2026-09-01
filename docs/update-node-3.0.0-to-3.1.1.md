# Обновление Node до 3.4.1

Целевой образ Node `3.4.1` привязан к immutable multiarch digest в `compatibility.json`. Обновление меняет только прямое поле `image` сервиса Node в Compose и, для старого стека, исправляет target лога `/var/log/remnanode` на `/var/log/xray`.

Перед обновлением Node обязательно обновите и проверьте Panel `3.4.3`. Node и Panel должны использовать проверенную актуальную связку; менеджер остановит переход со старой Node без явного `--panel-3-4-ready`.

Node 3.4.1 сохраняет ENV-контракт, Xray Core `v26.7.28`, runtime-команды `rw-core`/`cli` и Unix-сокеты XHTTP версии 3.4.0. Версия обновляет Node Plugins/Zod и исправляет сброс старых соединений при замене VLESS UUID. Необязательные `NFTABLES_LOGGING`, `NFTABLES_ACCEPT_REPLY_TRAFFIC` и `SNI_VERIFICATION` остаются совместимыми; старый env валиден, а чистая установка задаёт upstream-compatible defaults явно. Менеджер проверяет `SECRET_KEY` и текущий Xray JSON новым образом до изменения Compose; Reality без явного `minClientVer` требует явного подтверждения риска.

Если старый `SECRET_KEY` принят предыдущей Node, но отклонён 3.4.1, интерактивный `rwm update` попросит новый ключ, выданный обновлённой Panel через `/api/keygen` (или скопированный из настроек Node). Ключ сначала проходит preflight образом 3.4.1, затем менеджер транзакционно заменяет его в `.env` либо в прямом `environment:` сервиса Node и обновляет Compose. Ввод принимает чистый ключ, строку `SECRET_KEY=…`/`SECRET_KEY: …` из Compose и JSON-ответ `/api/keygen`; перед сохранением оболочка удаляется. Для неинтерактивного запуска передайте новый ключ только через окружение: `RWM_NODE_SECRET_KEY='…' sudo --preserve-env=RWM_NODE_SECRET_KEY rwm update ...`; после запуска удалите переменную.

Если вставленный ключ снова не проходит именно проверку payload, менеджер предложит получить его напрямую с Panel: укажите URL Panel (`https://panel.example.com` или `https://panel.example.com/api`), admin API token с правом `keygen:get` и cookie (`rwm_access=…` или JSON-объект `{"rwm_access":"…"}`). Ввод cookie скрыт; если cookie-gate не используется, оставьте его пустым. Ключ не выводится и не передаётся в argv. Это исключает искажение при копировании; если и ответ API не проходит preflight, Panel фактически не выдаёт совместимый ключ и обновление безопасно остановится до изменения Node.

Nginx-конфигурации CDN (Yandex/Beeline GET/POST), XHTTP stream separation, сертификаты, сайт маскировки и WARP не переписываются. Менеджер сохраняет их хеши до и после recreate Node и ждёт обнаруженные XHTTP Unix-сокеты.

Команды:

```bash
sudo rwm adopt --path /opt/remnanode --role node
sudo rwm diagnose
sudo rwm backup create --reason before-node-3.4.1
sudo rwm update --panel-3-4-ready --yes
```
