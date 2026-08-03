from __future__ import annotations

import argparse
import dataclasses
import getpass
import json
import os
import sys
import unicodedata
import warnings
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, TextIO

from . import __version__
from .adopt import adopt
from .api import (
    RemnawaveApi,
    complete_reality_credentials_handoff,
    configure_warp_routing,
    provision_reality_node,
    validate_reality_inputs,
    validate_warp_routing_inputs,
)
from .backup import create_backup, list_backups, restore_backup, verify_backup
from .backup_schedule import (
    backup_schedule_status,
    install_backup_schedule,
    remove_backup_schedule,
)
from .certificates import (
    CERTBOT_MANAGER_LOCK_HELD_ENV,
    CertificateSpec,
    assert_no_active_certbot_renewal,
    configure_adopted_certbot,
    issue_certificate,
)
from .compose import inspect_compose
from .diagnose import repair_permissions, run_diagnostics
from .disguise import DISGUISE_TEMPLATE_COUNT, apply_template, template_catalog
from .errors import ManagerError, TransactionError, ValidationError
from .firewall import configure_firewall
from .host import configure_host, host_status
from .install import (
    NodeInstallOptions,
    PanelInstallOptions,
    complete_panel_credentials_handoff,
    install_node,
    install_panel,
)
from .lifecycle import (
    component_logs,
    component_status,
    manage_component,
    panel_cli,
    validate_log_since,
)
from .maintenance import archive_stack
from .models import Inventory
from .nginx import reload_nginx, test_nginx
from .paths import RuntimePaths
from .registry import (
    REGISTRIES,
    registry_login,
    registry_logout,
    registry_status,
    select_registry,
    validate_registry_username,
)
from .runner import (
    Runner,
    exclusive_lock,
    require_root,
    require_ubuntu_2404,
    sanitize_external_text,
)
from .security import (
    close_emergency_access,
    emergency_access_status,
    open_emergency_access,
    panel_access,
    rotate_panel_access,
)
from .state import StateStore
from .update import update_node, update_panel_stack
from .warp import (
    adopt_warp,
    install_warp,
    rotate_warp,
    scan_warp,
    uninstall_warp,
    warp_action,
    warp_status,
    warp_watchdog,
)

InputFunction = Callable[[str], str]
SecretFunction = Callable[[str], str]
MAX_SECRET_LENGTH = 16 * 1024
MAX_TERMINAL_MESSAGE_LENGTH = 16 * 1024
DISGUISE_IDS = tuple(item["id"] for item in template_catalog())


class RussianArgumentParser(argparse.ArgumentParser):
    """ArgumentParser с русскими служебными сообщениями."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs["add_help"] = False
        super().__init__(*args, **kwargs)
        self.add_argument(
            "-h",
            "--help",
            action="help",
            help="Показать эту справку и выйти.",
        )

    def format_help(self) -> str:
        return (
            super()
            .format_help()
            .replace("usage: ", "использование: ")
            .replace("options:\n", "параметры:\n")
            .replace("positional arguments:\n", "позиционные аргументы:\n")
        )

    def format_usage(self) -> str:
        return super().format_usage().replace("usage: ", "использование: ")

    def error(self, message: str) -> None:
        translated = (
            message.replace(
                "the following arguments are required:",
                "не указаны обязательные аргументы:",
            )
            .replace("one of the arguments", "один из аргументов")
            .replace("is required", "обязателен")
            .replace("unrecognized arguments:", "неизвестные аргументы:")
            .replace("invalid choice:", "недопустимое значение:")
            .replace("invalid int value:", "некорректное целое число:")
            .replace("choose from", "допустимые значения:")
            .replace("expected one argument", "ожидалось одно значение")
            .replace("argument ", "аргумент ")
        )
        translated = " ".join(_terminal_safe_text(translated).split())
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: ошибка: {translated}\n")


@dataclass(slots=True)
class CliContext:
    runner: Runner
    store: StateStore
    paths: RuntimePaths
    stdout: TextIO
    stderr: TextIO
    input_fn: InputFunction
    secret_fn: SecretFunction
    json_output: bool = False

    def write(self, message: str = "") -> None:
        print(_terminal_safe_text(message), file=self.stdout)

    def error(self, message: str) -> None:
        print(_terminal_safe_text(message), file=self.stderr)

    def emit(self, value: Any) -> None:
        print(
            json.dumps(_jsonable(value), ensure_ascii=False, indent=2, sort_keys=True),
            file=self.stdout,
        )

    def interactive_ui(self) -> bool:
        """Return whether destructive screen redraws are safe for this output."""
        try:
            return bool(self.stdout.isatty()) and os.environ.get("TERM", "") != "dumb"
        except (AttributeError, OSError):
            return False

    def clear_screen(self) -> None:
        if not self.interactive_ui():
            return
        self.stdout.write("\033[2J\033[H")
        self.stdout.flush()

    def pause(self) -> None:
        if not self.interactive_ui():
            return
        try:
            self.input_fn("\nНажмите Enter, чтобы продолжить...")
        except (EOFError, KeyboardInterrupt):
            return

    def render_menu(
        self,
        title: str,
        choices: Sequence[str],
        *,
        allow_back: bool,
        zero_label: str,
    ) -> None:
        self.clear_screen()
        labels = [f"{index}. {label}" for index, label in enumerate(choices, 1)]
        if allow_back:
            labels.append(f"0. {zero_label}")
        width = max(
            52,
            min(
                96,
                max(
                    len(title) + 2,
                    len(f"Remnawave Manager {__version__}") + 2,
                    *(len(label) + 2 for label in labels),
                ),
            ),
        )
        border = "+" + "-" * width + "+"
        self.write(border)
        self.write(f"|{'Remnawave Manager ' + __version__:^{width}}|")
        self.write(f"|{title:^{width}}|")
        self.write(border)
        self.write()
        for label in labels[:-1] if allow_back else labels:
            self.write(f"  {label}")
        if allow_back:
            self.write()
            self.write(f"  {labels[-1]}")
        self.write()


def _terminal_safe_text(value: object) -> str:
    try:
        selected = str(value)
    except (RecursionError, UnicodeError):
        return ""
    selected = selected[-MAX_TERMINAL_MESSAGE_LENGTH:]
    return "".join(
        character
        if character in {"\n", "\t"}
        or unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
        else " "
        for character in selected
    )


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


def _add_certificate_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--certificate-method",
        choices=("existing", "http-01", "cloudflare", "gcore"),
        required=True,
        help="Способ получения TLS-сертификата.",
    )
    parser.add_argument("--email", help="Email для Let's Encrypt.")
    parser.add_argument(
        "--fullchain", metavar="ПУТЬ", help="Существующий fullchain.pem."
    )
    parser.add_argument(
        "--private-key", metavar="ПУТЬ", help="Существующий закрытый ключ."
    )


def _add_yes(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Подтвердить опасную операцию без интерактивного запроса.",
    )


def _log_tail(value: str) -> int:
    try:
        tail = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("--tail должен быть целым числом.") from error
    if not 1 <= tail <= 10_000:
        raise argparse.ArgumentTypeError("--tail должен быть от 1 до 10000.")
    return tail


def _log_since(value: str) -> str:
    try:
        return validate_log_since(value)
    except ValidationError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def build_parser() -> RussianArgumentParser:
    parser = RussianArgumentParser(
        prog="rwm",
        description="Безопасное управление Remnawave Panel, Subscription Page, Node и WARP.",
    )
    parser._optionals.title = "Параметры"
    parser.add_argument(
        "--json",
        action="store_true",
        help="Вывести результат в JSON; недоступно для menu, service logs и service panel-cli.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Показать версию программы и выйти.",
    )
    commands = parser.add_subparsers(dest="command", title="Команды", metavar="КОМАНДА")

    menu = commands.add_parser("menu", help="Открыть интерактивное меню.")
    menu.set_defaults(handler="menu")

    adoption = commands.add_parser(
        "adopt", help="Обнаружить установку и принять её под управление."
    )
    adoption.add_argument(
        "--path", type=Path, help="Каталог установки, например /opt/remnawave."
    )
    adoption.add_argument(
        "--role", choices=("panel", "node"), help="Ожидаемая роль сервера."
    )
    adoption.set_defaults(handler="adopt")

    inventory = commands.add_parser(
        "inventory", help="Показать сохранённую инвентаризацию."
    )
    inventory.set_defaults(handler="inventory")

    install = commands.add_parser(
        "install", help="Выполнить чистую установку компонента."
    )
    install_commands = install.add_subparsers(
        dest="install_target", required=True, metavar="ЦЕЛЬ"
    )
    panel = install_commands.add_parser(
        "panel", help="Установить Panel и Subscription Page."
    )
    panel.epilog = (
        "Пароль можно передать через RWM_ADMIN_PASSWORD; в argv секреты не принимаются."
    )
    panel.add_argument("--panel-domain", required=True, help="Домен Panel.")
    panel.add_argument(
        "--subscription-domain", required=True, help="Домен Subscription Page."
    )
    panel.add_argument(
        "--admin-username",
        help="Имя администратора; без параметра будет сгенерировано.",
    )
    panel.add_argument(
        "--ask-admin-password",
        action="store_true",
        help="Запросить пароль администратора скрыто; иначе он будет сгенерирован.",
    )
    panel.add_argument(
        "--api-token-days", type=int, default=365, help="Срок API-токена, дней."
    )
    panel.add_argument("--no-ufw", action="store_true", help="Не настраивать UFW.")
    panel.add_argument(
        "--ssh-port",
        action="append",
        type=int,
        help="Разрешённый SSH-порт; можно повторить.",
    )
    _add_certificate_arguments(panel)
    panel.set_defaults(handler="install-panel")

    node = install_commands.add_parser("node", help="Установить отдельную Node.")
    node.epilog = "SECRET_KEY читается из RWM_NODE_SECRET_KEY или запрашивается скрыто."
    node.add_argument("--domain", required=True, help="Домен Node.")
    node.add_argument(
        "--panel-ip", required=True, help="IPv4-адрес Panel для доступа к порту 2222."
    )
    source = node.add_mutually_exclusive_group(required=True)
    source.add_argument("--template", choices=DISGUISE_IDS)
    source.add_argument("--site-source", type=Path, metavar="КАТАЛОГ")
    node.add_argument("--no-ufw", action="store_true", help="Не настраивать UFW.")
    node.add_argument(
        "--ssh-port",
        action="append",
        type=int,
        help="Разрешённый SSH-порт; можно повторить.",
    )
    _add_certificate_arguments(node)
    node.set_defaults(handler="install-node")

    update = commands.add_parser(
        "update", help="Обновить компоненты согласно роли сервера."
    )
    update.add_argument(
        "--accept-reality-client-risk",
        action="store_true",
        help="Подтвердить требование Reality-клиентов не ниже 26.3.27.",
    )
    update.add_argument(
        "--accept-unknown-source",
        action="store_true",
        help="Разрешить обновление, если digest исходного релиза не удалось определить.",
    )
    _add_yes(update)
    update.set_defaults(handler="update")

    backup = commands.add_parser(
        "backup", help="Создать, проверить или восстановить локальный backup."
    )
    backup_commands = backup.add_subparsers(
        dest="backup_action", required=True, metavar="ДЕЙСТВИЕ"
    )
    backup_create = backup_commands.add_parser("create", help="Создать backup.")
    backup_create.add_argument(
        "--reason", default="manual", help="Краткая причина создания."
    )
    backup_create.add_argument(
        "--retention", type=int, help="Сколько backup этой роли оставить."
    )
    backup_create.set_defaults(handler="backup-create")
    backup_list = backup_commands.add_parser("list", help="Показать локальные backup.")
    backup_list.set_defaults(handler="backup-list")
    backup_verify = backup_commands.add_parser(
        "verify", help="Проверить целостность backup."
    )
    backup_verify.add_argument("path", type=Path, metavar="BACKUP")
    backup_verify.set_defaults(handler="backup-verify")
    backup_restore = backup_commands.add_parser("restore", help="Восстановить backup.")
    backup_restore.add_argument("path", type=Path, metavar="BACKUP")
    backup_restore.add_argument(
        "--without-database",
        action="store_true",
        help="Не восстанавливать PostgreSQL dump.",
    )
    _add_yes(backup_restore)
    backup_restore.set_defaults(handler="backup-restore")
    backup_schedule_state = backup_commands.add_parser(
        "schedule-status", help="Показать состояние расписания локальных backup."
    )
    backup_schedule_state.set_defaults(handler="backup-schedule-status")
    backup_schedule_enable = backup_commands.add_parser(
        "schedule-enable", help="Включить ежедневный или еженедельный backup."
    )
    backup_schedule_enable.add_argument(
        "--frequency",
        choices=("daily", "weekly"),
        required=True,
        help="Частота: daily или weekly (по воскресеньям).",
    )
    backup_schedule_enable.add_argument(
        "--time",
        dest="time_of_day",
        required=True,
        metavar="ЧЧ:ММ",
        help="Локальное время сервера в формате ЧЧ:ММ.",
    )
    backup_schedule_enable.add_argument(
        "--retention",
        type=int,
        required=True,
        metavar="N",
        help="Число хранимых локальных backup, от 1 до 1000.",
    )
    _add_yes(backup_schedule_enable)
    backup_schedule_enable.set_defaults(handler="backup-schedule-enable")
    backup_schedule_disable = backup_commands.add_parser(
        "schedule-disable", help="Отключить расписание backup."
    )
    _add_yes(backup_schedule_disable)
    backup_schedule_disable.set_defaults(handler="backup-schedule-disable")

    service = commands.add_parser(
        "service", help="Управлять контейнерами Panel, Node и nginx."
    )
    service_commands = service.add_subparsers(
        dest="service_action", required=True, metavar="ДЕЙСТВИЕ"
    )
    status = service_commands.add_parser(
        "status", help="Показать состояние компонентов."
    )
    status.set_defaults(handler="service-status")
    for action, description in (
        ("start", "Запустить компонент или весь стек (all)."),
        ("stop", "Остановить компонент или весь стек (all)."),
        ("restart", "Перезапустить компонент или весь стек (all)."),
    ):
        item = service_commands.add_parser(action, help=description)
        item.add_argument("component", metavar="КОМПОНЕНТ")
        _add_yes(item)
        item.set_defaults(handler=f"service-{action}")
    logs = service_commands.add_parser(
        "logs", help="Показать логи компонента или всего стека (all)."
    )
    logs.add_argument("component", metavar="КОМПОНЕНТ")
    logs.add_argument("--tail", type=_log_tail, default=100, metavar="N")
    logs.add_argument(
        "--follow", action="store_true", help="Продолжать вывод новых строк."
    )
    logs.add_argument(
        "--since", type=_log_since, metavar="ВРЕМЯ", help="Начало периода Docker logs."
    )
    logs.set_defaults(handler="service-logs")
    panel_command = service_commands.add_parser(
        "panel-cli", help="Открыть CLI внутри Panel."
    )
    panel_command.set_defaults(handler="service-panel-cli")

    registry = commands.add_parser(
        "registry", help="Управлять авторизацией Docker Registry."
    )
    registry_commands = registry.add_subparsers(
        dest="registry_action", required=True, metavar="ДЕЙСТВИЕ"
    )
    registry_state = registry_commands.add_parser(
        "status", help="Показать выбранный registry и авторизации."
    )
    registry_state.set_defaults(handler="registry-status")
    registry_select = registry_commands.add_parser(
        "select", help="Выбрать registry для загрузки образов."
    )
    registry_select.add_argument("registry", choices=tuple(REGISTRIES))
    registry_select.set_defaults(handler="registry-select")
    registry_signin = registry_commands.add_parser(
        "login", help="Войти в Docker Registry."
    )
    registry_signin.epilog = (
        "Пароль/token читается из RWM_REGISTRY_PASSWORD или запрашивается скрыто."
    )
    registry_signin.add_argument("--registry", choices=tuple(REGISTRIES), required=True)
    registry_signin.add_argument("--username", help="Имя пользователя registry.")
    registry_signin.add_argument(
        "--select", action="store_true", help="Сделать registry выбранным после входа."
    )
    registry_signin.set_defaults(handler="registry-login")
    registry_signout = registry_commands.add_parser(
        "logout", help="Удалить авторизацию Docker Registry."
    )
    registry_signout.add_argument(
        "--registry", choices=tuple(REGISTRIES), required=True
    )
    _add_yes(registry_signout)
    registry_signout.set_defaults(handler="registry-logout")

    warp = commands.add_parser("warp", help="Встроенное управление Cloudflare WARP.")
    warp_commands = warp.add_subparsers(
        dest="warp_action", required=True, metavar="ДЕЙСТВИЕ"
    )
    warp_scan = warp_commands.add_parser(
        "scan", help="Без изменений проверить существующий WARP."
    )
    warp_scan.set_defaults(handler="warp-scan")
    warp_state = warp_commands.add_parser(
        "status", help="Показать состояние управляемого WARP."
    )
    warp_state.set_defaults(handler="warp-status")
    warp_install = warp_commands.add_parser(
        "install", help="Установить WARP без сторонних скриптов."
    )
    warp_install.epilog = "WARP+ key читается только из WGCF_LICENSE_KEY или запрашивается скрыто с --plus."
    warp_install.add_argument(
        "--accept-tos", action="store_true", help="Принять Cloudflare Terms of Service."
    )
    warp_install.add_argument(
        "--plus",
        action="store_true",
        help="Запросить WARP+ key, если нет WGCF_LICENSE_KEY.",
    )
    warp_install.add_argument(
        "--wgcf-file", type=Path, help="Локальный проверяемый бинарный wgcf."
    )
    warp_install.set_defaults(handler="warp-install")
    warp_adopt = warp_commands.add_parser(
        "adopt", help="Проверить или принять существующий WARP."
    )
    warp_adopt.add_argument(
        "--takeover",
        action="store_true",
        help="Принять безопасную конфигурацию под управление.",
    )
    warp_adopt.add_argument(
        "--wgcf-file", type=Path, help="Локальный проверяемый бинарный wgcf."
    )
    _add_yes(warp_adopt)
    warp_adopt.set_defaults(handler="warp-adopt")
    for action, description in (
        ("start", "Запустить WARP."),
        ("stop", "Остановить WARP."),
        ("restart", "Перезапустить WARP."),
    ):
        item = warp_commands.add_parser(action, help=description)
        if action != "start":
            _add_yes(item)
        item.set_defaults(handler=f"warp-{action}")
    warp_rotate = warp_commands.add_parser(
        "rotate", help="Создать новый аккаунт и профиль WARP."
    )
    warp_rotate.add_argument(
        "--accept-tos", action="store_true", help="Принять Cloudflare Terms of Service."
    )
    warp_rotate.add_argument(
        "--plus",
        action="store_true",
        help="Запросить WARP+ key, если нет WGCF_LICENSE_KEY.",
    )
    _add_yes(warp_rotate)
    warp_rotate.set_defaults(handler="warp-rotate")
    warp_uninstall = warp_commands.add_parser(
        "uninstall", help="Удалить управляемые компоненты WARP."
    )
    warp_uninstall.add_argument(
        "--purge-credentials",
        action="store_true",
        help="Также удалить профиль и account.toml.",
    )
    _add_yes(warp_uninstall)
    warp_uninstall.set_defaults(handler="warp-uninstall")
    warp_watch = warp_commands.add_parser(
        "watchdog", help="Однократно выполнить health-check WARP."
    )
    warp_watch.set_defaults(handler="warp-watchdog")

    disguise = commands.add_parser(
        "disguise", help="Управлять выбираемыми сайтами-заглушками Node."
    )
    disguise_commands = disguise.add_subparsers(
        dest="disguise_action", required=True, metavar="ДЕЙСТВИЕ"
    )
    disguise_list = disguise_commands.add_parser(
        "list", help=f"Показать {DISGUISE_TEMPLATE_COUNT} встроенных шаблонов."
    )
    disguise_list.set_defaults(handler="disguise-list")
    disguise_apply = disguise_commands.add_parser(
        "apply", help="Заменить сайт-заглушку с backup и rollback."
    )
    disguise_apply.add_argument("template", choices=DISGUISE_IDS)
    _add_yes(disguise_apply)
    disguise_apply.set_defaults(handler="disguise-apply")

    api = commands.add_parser(
        "api", help="Безопасные операции через Remnawave Panel API."
    )
    api_commands = api.add_subparsers(
        dest="api_action", required=True, metavar="ДЕЙСТВИЕ"
    )
    reality = api_commands.add_parser(
        "reality", help="Создать Reality profile, Node и Host транзакционно."
    )
    reality.epilog = (
        "Admin API token читается из RWM_API_TOKEN или запрашивается скрыто."
    )
    reality.add_argument(
        "--base-url", default="http://127.0.0.1:3000", help="URL Panel API."
    )
    reality.add_argument("--profile-name", required=True)
    reality.add_argument("--inbound-tag", required=True)
    reality.add_argument("--node-name", required=True)
    reality.add_argument("--domain", required=True)
    _add_yes(reality)
    reality.set_defaults(handler="api-reality")
    routing = api_commands.add_parser(
        "warp-routing", help="Настроить manager-owned WARP routing в профиле."
    )
    routing.epilog = (
        "Admin API token читается из RWM_API_TOKEN или запрашивается скрыто."
    )
    routing.add_argument("action", choices=("apply", "remove"))
    routing.add_argument(
        "--base-url", default="http://127.0.0.1:3000", help="URL Panel API."
    )
    routing.add_argument("--profile-uuid", required=True)
    routing.add_argument(
        "--domain", action="append", default=[], help="Домен; можно повторить."
    )
    _add_yes(routing)
    routing.set_defaults(handler="api-warp-routing")

    diagnose = commands.add_parser(
        "diagnose", help="Проверить конфигурацию, runtime и безопасность."
    )
    diagnose.add_argument(
        "--repair-permissions",
        action="store_true",
        help="Исправить только известные небезопасные права.",
    )
    _add_yes(diagnose)
    diagnose.set_defaults(handler="diagnose")

    certificate = commands.add_parser(
        "certificate", help="Проверить или обновить сертификаты Certbot."
    )
    certificate_commands = certificate.add_subparsers(
        dest="certificate_action", required=True, metavar="ДЕЙСТВИЕ"
    )
    certificate_status = certificate_commands.add_parser(
        "status", help="Показать сертификаты Certbot."
    )
    certificate_status.set_defaults(handler="certificate-status")
    certificate_issue = certificate_commands.add_parser(
        "issue",
        help="Выпустить отдельный сертификат без изменения рабочего домена.",
    )
    certificate_issue.add_argument(
        "--domain", required=True, help="Домен нового сертификата."
    )
    certificate_issue.add_argument(
        "--method",
        choices=("http-01", "cloudflare", "gcore"),
        required=True,
        help="Способ подтверждения домена.",
    )
    certificate_issue.add_argument(
        "--email", required=True, help="Email для Let's Encrypt."
    )
    certificate_issue.add_argument(
        "--wildcard",
        action="store_true",
        help="Добавить *.DOMAIN; допустимо только для DNS-01.",
    )
    _add_yes(certificate_issue)
    certificate_issue.set_defaults(handler="certificate-issue")
    certificate_renew = certificate_commands.add_parser(
        "renew", help="Запустить безопасное продление Certbot."
    )
    certificate_renew.add_argument(
        "--dry-run", action="store_true", help="Тестовое продление Let's Encrypt."
    )
    _add_yes(certificate_renew)
    certificate_renew.set_defaults(handler="certificate-renew")
    certificate_repair = certificate_commands.add_parser(
        "repair-renewal",
        help="Проверить и восстановить безопасное автопродление Certbot.",
    )
    _add_yes(certificate_repair)
    certificate_repair.set_defaults(handler="certificate-repair-renewal")
    certificate_reload = certificate_commands.add_parser(
        "reload", help="Проверить и reload nginx."
    )
    certificate_reload.set_defaults(handler="certificate-reload")

    firewall = commands.add_parser(
        "firewall", help="Проверить или применить правила UFW."
    )
    firewall_commands = firewall.add_subparsers(
        dest="firewall_action", required=True, metavar="ДЕЙСТВИЕ"
    )
    firewall_status = firewall_commands.add_parser(
        "status", help="Показать подробный статус UFW."
    )
    firewall_status.set_defaults(handler="firewall-status")
    firewall_apply = firewall_commands.add_parser(
        "apply", help="Применить минимальные правила для роли."
    )
    firewall_apply.add_argument(
        "--role", choices=("auto", "panel", "node"), default="auto"
    )
    firewall_apply.add_argument("--panel-ip", help="IPv4 Panel; обязателен для Node.")
    firewall_apply.add_argument(
        "--ssh-port", action="append", type=int, help="SSH-порт; можно повторить."
    )
    _add_yes(firewall_apply)
    firewall_apply.set_defaults(handler="firewall-apply")

    system = commands.add_parser(
        "system", help="Проверить или настроить BBR и автоматические security updates."
    )
    system_commands = system.add_subparsers(
        dest="system_action", required=True, metavar="ДЕЙСТВИЕ"
    )
    system_status = system_commands.add_parser(
        "status", help="Показать состояние BBR, fq и unattended-upgrades."
    )
    system_status.set_defaults(handler="system-status")
    system_apply = system_commands.add_parser(
        "apply", help="Транзакционно включить BBR/fq и unattended-upgrades."
    )
    _add_yes(system_apply)
    system_apply.set_defaults(handler="system-apply")

    maintenance = commands.add_parser(
        "maintenance",
        help="Безопасное архивирование установки перед удалением или переустановкой.",
    )
    maintenance_commands = maintenance.add_subparsers(
        dest="maintenance_action", required=True, metavar="ДЕЙСТВИЕ"
    )
    archive_command = maintenance_commands.add_parser(
        "archive-stack",
        help="Создать backup, остановить стек и переместить его в восстанавливаемый архив.",
    )
    _add_yes(archive_command)
    archive_command.set_defaults(handler="maintenance-archive-stack")

    security = commands.add_parser(
        "security", help="Управлять защитным URL и cookie Panel."
    )
    security_commands = security.add_subparsers(
        dest="security_action", required=True, metavar="ДЕЙСТВИЕ"
    )
    access_show = security_commands.add_parser(
        "access", help="Показать защищённый URL Panel."
    )
    access_show.set_defaults(handler="security-access")
    access_rotate = security_commands.add_parser(
        "rotate-access",
        help="Сменить cookie/URL или мигрировать legacy query-cookie с rollback.",
    )
    _add_yes(access_rotate)
    access_rotate.set_defaults(handler="security-rotate-access")
    emergency_state = security_commands.add_parser(
        "emergency-status", help="Показать состояние временного аварийного доступа."
    )
    emergency_state.set_defaults(handler="security-emergency-status")
    emergency_open = security_commands.add_parser(
        "emergency-open",
        help="Временно открыть Panel только на loopback для SSH-туннеля.",
    )
    emergency_open.add_argument(
        "--minutes",
        type=int,
        default=30,
        metavar="N",
        help="Срок доступа от 5 до 120 минут (по умолчанию 30).",
    )
    _add_yes(emergency_open)
    emergency_open.set_defaults(handler="security-emergency-open")
    emergency_close = security_commands.add_parser(
        "emergency-close", help="Немедленно закрыть аварийный доступ."
    )
    emergency_close.set_defaults(handler="security-emergency-close")

    return parser


def _certificate_spec(args: argparse.Namespace, context: CliContext) -> CertificateSpec:
    method = args.certificate_method
    if method == "existing":
        if not args.fullchain or not args.private_key:
            raise ValidationError("Для existing укажите --fullchain и --private-key.")
        return CertificateSpec(
            method="existing",
            fullchain=Path(args.fullchain).expanduser(),
            private_key=Path(args.private_key).expanduser(),
        )
    return _letsencrypt_certificate_spec(method, args.email, context)


def _letsencrypt_certificate_spec(
    method: str, email: str | None, context: CliContext
) -> CertificateSpec:
    if not email:
        raise ValidationError("Для Let's Encrypt укажите --email.")
    cloudflare_token = None
    gcore_token = None
    if method == "cloudflare":
        cloudflare_token = _required_secret(
            context,
            "RWM_CLOUDFLARE_TOKEN",
            "Cloudflare API Token: ",
        )
    elif method == "gcore":
        gcore_token = _required_secret(
            context,
            "RWM_GCORE_TOKEN",
            "Gcore API Token: ",
        )
    return CertificateSpec(
        method=method,
        email=email,
        cloudflare_token=cloudflare_token,
        gcore_token=gcore_token,
    )


def _required_secret(context: CliContext, variable: str, prompt: str) -> str:
    if variable in os.environ:
        value = os.environ.pop(variable)
    else:
        value = context.secret_fn(prompt)
    return _validated_secret(value, variable=variable, required=True)


def _secure_getpass(prompt: str) -> str:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass(prompt)
    except getpass.GetPassWarning as error:
        raise ValidationError(
            "Защищённый терминал недоступен; задайте секрет через указанную переменную окружения."
        ) from error


def _optional_environment_secret(variable: str) -> str | None:
    if variable not in os.environ:
        return None
    return _validated_secret(
        os.environ.pop(variable), variable=variable, required=False
    )


def _validated_secret(value: object, *, variable: str, required: bool) -> str:
    if not isinstance(value, str) or not value:
        message = (
            f"Секрет не задан: используйте {variable} или скрытый интерактивный ввод."
            if required
            else f"Переменная {variable} задана пустой."
        )
        raise ValidationError(message)
    if len(value) > MAX_SECRET_LENGTH:
        raise ValidationError(
            f"{variable} превышает допустимый размер {MAX_SECRET_LENGTH} символов."
        )
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        for character in value
    ):
        raise ValidationError(f"{variable} содержит управляющие символы.")
    return value


def _prompt_twice(context: CliContext, label: str) -> str:
    first = context.secret_fn(label)
    second = context.secret_fn("Повторите значение: ")
    if first != second:
        raise ValidationError("Введённые секретные значения не совпадают.")
    return _validated_secret(first, variable="Секретное значение", required=False)


def _confirm(
    context: CliContext, message: str, *, assume_yes: bool, word: str = "ДА"
) -> None:
    if assume_yes:
        return
    if context.json_output:
        raise ValidationError(
            "В режиме --json для подтверждаемой операции обязательно укажите --yes."
        )
    safe_message = " ".join(_terminal_safe_text(message).split())
    answer = context.input_fn(
        f"{safe_message}\nДля продолжения введите {word}: "
    ).strip()
    if answer.casefold() != word.casefold():
        raise ValidationError("Операция отменена пользователем.")


def _template_source(template_id: str) -> Path:
    known = {item["id"] for item in template_catalog()}
    if template_id not in known:
        raise ValidationError(f"Неизвестный шаблон: {template_id}")
    source = Path(
        str(files("remnawave_manager").joinpath(f"data/disguises/{template_id}"))
    )
    if not source.is_dir():
        raise ValidationError(f"Каталог шаблона не найден: {template_id}")
    return source


def _show_inventory(context: CliContext, inventory: Inventory) -> None:
    if context.json_output:
        context.emit(inventory)
        return
    context.write(f"Роль: {inventory.role}")
    context.write(f"Каталог: {inventory.install_dir}")
    context.write(f"Compose: {inventory.compose_file}")
    context.write("Компоненты:")
    for name, component in sorted(inventory.components.items()):
        image = (
            component.running_image
            or component.configured_image
            or "образ не определён"
        )
        context.write(f"  {name}: {component.status or 'статус не определён'}; {image}")
    enabled = sorted(name for name, value in inventory.features.items() if value)
    context.write(
        "Особенности: " + (", ".join(enabled) if enabled else "не обнаружены")
    )


def _warp_license(context: CliContext, *, prompt: bool) -> str | None:
    value = _optional_environment_secret("WGCF_LICENSE_KEY")
    if value is None and prompt:
        value = _required_secret(context, "WGCF_LICENSE_KEY", "WARP+ license key: ")
    return value


def dispatch(args: argparse.Namespace, context: CliContext) -> int:
    handler = args.handler
    if handler == "adopt":
        inventory = adopt(
            context.runner,
            context.store,
            directory=args.path,
            requested_role=args.role,
        )
        _show_inventory(context, inventory)
        if not context.json_output:
            if inventory.features.get("certbot_legacy_renew_hook_removed"):
                context.write("Старый Certbot renew_hook заменён hooks менеджера.")
            if inventory.features.get("certbot_legacy_cron_removed"):
                context.write("Старое cron-задание Certbot заменено certbot.timer.")
        return 0
    if handler == "inventory":
        _show_inventory(context, context.store.load_inventory())
        return 0
    if handler == "install-panel":
        password = _optional_environment_secret("RWM_ADMIN_PASSWORD")
        if args.ask_admin_password:
            if password is not None:
                raise ValidationError(
                    "Одновременно заданы RWM_ADMIN_PASSWORD и --ask-admin-password; выберите один способ."
                )
            password = _prompt_twice(context, "Пароль администратора: ")
        result = install_panel(
            context.runner,
            context.store,
            PanelInstallOptions(
                panel_domain=args.panel_domain,
                subscription_domain=args.subscription_domain,
                certificate=_certificate_spec(args, context),
                admin_username=args.admin_username,
                admin_password=password,
                api_token_days=args.api_token_days,
                configure_ufw=not args.no_ufw,
                ssh_ports=tuple(args.ssh_port) if args.ssh_port else None,
            ),
        )
        try:
            if context.json_output:
                context.emit(result)
            else:
                context.write("Panel и Subscription Page установлены.")
                context.write(f"Защищённый URL входа: {result.access_url}")
                context.write(f"Имя администратора: {result.admin_username}")
                context.write(f"Пароль администратора: {result.admin_password}")
                context.write(
                    "Пароль показан один раз. Сохраните его в менеджере паролей."
                )
            context.stdout.flush()
        except (OSError, ValueError) as error:
            raise TransactionError(
                "Panel установлена, но вывод учётных данных не подтверждён; "
                "recovery-файл с паролем сохранён."
            ) from error
        complete_panel_credentials_handoff(result)
        return 0
    if handler == "install-node":
        source = args.site_source or _template_source(args.template)
        result = install_node(
            context.runner,
            context.store,
            NodeInstallOptions(
                domain=args.domain,
                panel_ip=args.panel_ip,
                secret_key=_required_secret(
                    context,
                    "RWM_NODE_SECRET_KEY",
                    "SECRET_KEY, созданный Panel API: ",
                ),
                certificate=_certificate_spec(args, context),
                site_source=Path(source),
                configure_ufw=not args.no_ufw,
                ssh_ports=tuple(args.ssh_port) if args.ssh_port else None,
            ),
        )
        if context.json_output:
            context.emit(result)
        else:
            context.write(
                f"Node для {result.domain} установлена и принята под управление."
            )
        return 0
    if handler == "update":
        inventory = context.store.load_inventory()
        warning = (
            "Будет создан проверенный backup, затем Panel и Subscription Page обновятся одной транзакцией. "
            "Миграции PostgreSQL откатываются только восстановлением dump."
            if inventory.role == "panel"
            else "Будет создан backup и протестирован текущий Xray-конфиг новым образом Node."
        )
        _confirm(context, warning, assume_yes=args.yes)
        result = (
            update_panel_stack(
                context.runner,
                context.store,
                accept_unknown_source=args.accept_unknown_source,
            )
            if inventory.role == "panel"
            else update_node(
                context.runner,
                context.store,
                accept_reality_client_risk=args.accept_reality_client_risk,
                accept_unknown_source=args.accept_unknown_source,
            )
        )
        if context.json_output:
            context.emit(result)
        else:
            context.write(f"Обновление завершено. Pre-update backup: {result.path}")
        return 0
    if handler == "backup-create":
        if (
            not args.reason
            or len(args.reason) > 100
            or any(
                unicodedata.category(character) in {"Cc", "Cf", "Cs"}
                for character in args.reason
            )
        ):
            raise ValidationError(
                "Причина backup должна содержать 1-100 печатных символов."
            )
        result = create_backup(
            context.runner,
            context.store,
            reason=args.reason,
            retention=args.retention,
        )
        context.emit(result) if context.json_output else context.write(
            f"Backup создан: {result.path}"
        )
        return 0
    if handler == "backup-list":
        backups = list_backups(context.store)
        if context.json_output:
            context.emit(backups)
        elif backups:
            for path in backups:
                context.write(str(path))
        else:
            context.write("Локальные backup не найдены.")
        return 0
    if handler == "backup-verify":
        manifest = verify_backup(args.path)
        if context.json_output:
            context.emit(manifest)
        else:
            context.write(f"Backup исправен: {args.path}")
            context.write(
                f"Создан: {manifest.get('created_at', 'неизвестно')}; "
                f"причина: {manifest.get('reason', 'неизвестно')}"
            )
        return 0
    if handler == "backup-restore":
        _confirm(
            context,
            f"Текущая конфигурация будет заменена содержимым {args.path}.",
            assume_yes=args.yes,
            word="ВОССТАНОВИТЬ",
        )
        restore_backup(
            context.runner,
            context.store,
            args.path,
            restore_database=not args.without_database,
        )
        context.emit(
            {
                "status": "restored",
                "backup": args.path,
                "database_restored": not args.without_database,
            }
        ) if context.json_output else context.write("Восстановление завершено.")
        return 0
    if handler == "backup-schedule-status":
        status = backup_schedule_status(context.runner, context.store)
        if context.json_output:
            context.emit(status)
        else:
            context.write(
                f"Расписание backup: {'включено' if status.enabled else 'выключено'}."
            )
            context.write(f"Таймер активен: {'да' if status.active else 'нет'}.")
            if status.frequency and status.time:
                frequency = (
                    "ежедневно"
                    if status.frequency == "daily"
                    else "еженедельно по воскресеньям"
                )
                context.write(
                    f"Периодичность: {frequency} в {status.time}; хранить: {status.retention}."
                )
            context.write(f"Следующий запуск: {status.next_run or 'не назначен'}.")
        return 0
    if handler == "backup-schedule-enable":
        _confirm(
            context,
            "Будет установлен и сразу включён systemd-таймер локальных backup.",
            assume_yes=args.yes,
        )
        status = install_backup_schedule(
            context.runner,
            context.store,
            frequency=args.frequency,
            time_of_day=args.time_of_day,
            retention=args.retention,
        )
        context.emit(status) if context.json_output else context.write(
            "Расписание backup включено."
        )
        return 0
    if handler == "backup-schedule-disable":
        _confirm(
            context,
            "Systemd-таймер backup будет отключён и удалён; существующие архивы сохранятся.",
            assume_yes=args.yes,
        )
        remove_backup_schedule(context.runner, context.store)
        context.emit(
            {"enabled": False, "archives_preserved": True}
        ) if context.json_output else context.write(
            "Расписание backup отключено; существующие архивы сохранены."
        )
        return 0
    if handler == "service-status":
        rows = component_status(context.runner, context.store.load_inventory())
        if context.json_output:
            context.emit(rows)
        else:
            for row in rows:
                health = f", health={row['health']}" if row["health"] else ""
                context.write(
                    f"{row['component']}: {row['status']} ({row['container']}{health})"
                )
        return 0
    if handler == "service-logs":
        component_logs(
            context.runner,
            context.store.load_inventory(),
            args.component,
            tail=args.tail,
            follow=args.follow,
            since=args.since,
        )
        return 0
    if handler == "service-panel-cli":
        panel_cli(context.runner, context.store.load_inventory())
        return 0
    if handler.startswith("service-"):
        action = handler.removeprefix("service-")
        if action in {"stop", "restart"}:
            _confirm(
                context,
                f"{'Весь стек' if args.component == 'all' else 'Компонент ' + args.component} "
                f"будет {('остановлен' if action == 'stop' else 'перезапущен')}.",
                assume_yes=args.yes,
            )
        manage_component(
            context.runner, context.store.load_inventory(), args.component, action
        )
        context.emit(
            {"component": args.component, "action": action, "status": "completed"}
        ) if context.json_output else context.write(
            f"Действие {action} для {args.component} выполнено."
        )
        return 0
    if handler == "registry-status":
        value = registry_status(context.store)
        if context.json_output:
            context.emit(value)
        else:
            context.write(f"Выбранный registry: {value['selected']}")
            hosts = value["authenticated_hosts"]
            context.write(
                "Авторизации: " + (", ".join(hosts) if hosts else "не обнаружены")
            )
        return 0
    if handler == "registry-select":
        select_registry(context.store, args.registry)
        context.emit(
            {"selected": args.registry}
        ) if context.json_output else context.write(f"Выбран registry: {args.registry}")
        return 0
    if handler == "registry-login":
        if context.json_output and not args.username:
            raise ValidationError(
                "В режиме --json для registry login обязательно укажите --username."
            )
        username = validate_registry_username(
            args.username
            if args.username is not None
            else context.input_fn(
                f"Имя пользователя для {REGISTRIES[args.registry]}: "
            ).strip()
        )
        password = _required_secret(
            context,
            "RWM_REGISTRY_PASSWORD",
            "Пароль или access token Docker Registry: ",
        )
        registry_login(
            context.runner,
            args.registry,
            username=username,
            password=password,
        )
        if args.select:
            select_registry(context.store, args.registry)
        context.emit(
            {
                "registry": args.registry,
                "selected": bool(args.select),
                "status": "authenticated",
            }
        ) if context.json_output else context.write(f"Вход в {args.registry} выполнен.")
        return 0
    if handler == "registry-logout":
        _confirm(
            context,
            f"Авторизация {args.registry} будет удалена из Docker config.",
            assume_yes=args.yes,
        )
        registry_logout(context.runner, args.registry)
        context.emit(
            {"registry": args.registry, "status": "logged_out"}
        ) if context.json_output else context.write(
            f"Выход из {args.registry} выполнен."
        )
        return 0
    if handler == "warp-scan":
        value = scan_warp(context.runner, context.paths)
        context.emit(value) if context.json_output else _show_warp_scan(context, value)
        return 0
    if handler == "warp-status":
        value = warp_status(context.runner, context.store)
        if context.json_output:
            context.emit(value)
        else:
            context.write(f"WARP: {'активен' if value['active'] else 'остановлен'}")
            context.write(
                f"Желаемое состояние: {'включён' if value['desired_enabled'] else 'выключен'}"
            )
            context.write(
                f"Cloudflare trace: {value['trace'] or value['error'] or 'нет данных'}"
            )
            context.write(
                f"Последнее рукопожатие, секунд назад: {value['last_handshake_seconds_ago']}"
            )
        return 0
    if handler == "warp-install":
        if not args.accept_tos:
            raise ValidationError("Для установки нужно явно указать --accept-tos.")
        value = install_warp(
            context.runner,
            context.store,
            accept_tos=args.accept_tos,
            license_key=_warp_license(context, prompt=args.plus),
            wgcf_file=args.wgcf_file,
        )
        context.emit(value) if context.json_output else context.write(
            "WARP установлен и проверен из контейнера Node."
        )
        return 0
    if handler == "warp-adopt":
        if args.takeover:
            _confirm(
                context,
                "Существующая WARP-конфигурация будет принята под управление после безопасного scan.",
                assume_yes=args.yes,
            )
        value = adopt_warp(
            context.runner,
            context.store,
            takeover=args.takeover,
            wgcf_file=args.wgcf_file,
        )
        context.emit(value) if context.json_output else _show_warp_scan(context, value)
        return 0
    if handler in {"warp-start", "warp-stop", "warp-restart"}:
        action = handler.removeprefix("warp-")
        if action in {"stop", "restart"}:
            _confirm(
                context,
                f"WARP будет {('остановлен' if action == 'stop' else 'перезапущен')}.",
                assume_yes=args.yes,
            )
        warp_action(context.runner, context.store, action)
        context.emit(
            {"action": action, "status": "completed"}
        ) if context.json_output else context.write(
            f"Действие {action} для WARP выполнено."
        )
        return 0
    if handler == "warp-rotate":
        if not args.accept_tos:
            raise ValidationError(
                "Для смены WARP account/profile нужно явно указать --accept-tos."
            )
        _confirm(
            context,
            "Текущий WARP account/profile будет заменён с предварительным backup.",
            assume_yes=args.yes,
        )
        rotate_warp(
            context.runner,
            context.store,
            accept_tos=args.accept_tos,
            license_key=_warp_license(context, prompt=args.plus),
        )
        context.emit({"status": "rotated"}) if context.json_output else context.write(
            "WARP account/profile заменён и проверен."
        )
        return 0
    if handler == "warp-uninstall":
        detail = (
            " вместе с credentials"
            if args.purge_credentials
            else " с сохранением credentials"
        )
        _confirm(
            context,
            f"Управляемый WARP будет удалён{detail}.",
            assume_yes=args.yes,
            word="УДАЛИТЬ",
        )
        uninstall_warp(
            context.runner,
            context.store,
            purge_credentials=args.purge_credentials,
        )
        context.emit(
            {
                "status": "uninstalled",
                "credentials_purged": bool(args.purge_credentials),
            }
        ) if context.json_output else context.write("WARP удалён.")
        return 0
    if handler == "warp-watchdog":
        value = warp_watchdog(context.runner, context.store)
        context.emit({"status": value}) if context.json_output else context.write(
            f"WARP health: {value}"
        )
        return 0
    if handler == "disguise-list":
        catalog = template_catalog()
        if context.json_output:
            context.emit(catalog)
        else:
            for item in catalog:
                context.write(f"{item['id']}: {item['name']} — {item['description']}")
        return 0
    if handler == "disguise-apply":
        _confirm(
            context,
            f"Сайт-заглушка будет заменён шаблоном {args.template}; сначала будет создан backup.",
            assume_yes=args.yes,
        )
        target = apply_template(context.runner, context.store, args.template)
        context.emit(
            {"target": target, "template": args.template}
        ) if context.json_output else context.write(
            f"Шаблон {args.template} установлен в {target}. Инвентаризация обновлена."
        )
        return 0
    if handler == "api-reality":
        api = RemnawaveApi(args.base_url)
        validate_reality_inputs(
            profile_name=args.profile_name,
            inbound_tag=args.inbound_tag,
            node_name=args.node_name,
            domain=args.domain,
        )
        _confirm(
            context,
            "В Panel будут созданы Config Profile, Node и Host, а новый inbound будет добавлен "
            "во все текущие Internal Squads. При ошибке изменения откатываются.",
            assume_yes=args.yes,
        )
        token = _required_secret(context, "RWM_API_TOKEN", "Admin API token Panel: ")
        value = provision_reality_node(
            api,
            token,
            profile_name=args.profile_name,
            inbound_tag=args.inbound_tag,
            node_name=args.node_name,
            domain=args.domain,
            store=context.store,
        )
        try:
            if context.json_output:
                context.emit(value)
            else:
                context.write(f"Config Profile UUID: {value.profile_uuid}")
                context.write(f"Inbound UUID: {value.inbound_uuid}")
                context.write(f"Node UUID: {value.node_uuid}")
                context.write(f"Host UUID: {value.host_uuid}")
                context.write(f"SECRET_KEY для установки Node: {value.secret_key}")
                context.write(
                    "SECRET_KEY показан один раз. Передайте его на Node через RWM_NODE_SECRET_KEY."
                )
            context.stdout.flush()
        except (OSError, ValueError) as error:
            raise TransactionError(
                "Reality provisioning завершён, но вывод SECRET_KEY не подтверждён; "
                "recovery-файл сохранён."
            ) from error
        complete_reality_credentials_handoff(context.store, value)
        return 0
    if handler == "api-warp-routing":
        if args.action == "apply" and not args.domain:
            raise ValidationError("Для apply укажите хотя бы один --domain.")
        api = RemnawaveApi(args.base_url)
        validate_warp_routing_inputs(
            args.profile_uuid,
            args.domain,
            remove=args.action == "remove",
        )
        _confirm(
            context,
            "Config Profile будет изменён через API после проверки параллельных изменений.",
            assume_yes=args.yes,
        )
        token = _required_secret(context, "RWM_API_TOKEN", "Admin API token Panel: ")
        configure_warp_routing(
            api,
            token,
            context.store,
            args.profile_uuid,
            args.domain,
            remove=args.action == "remove",
        )
        context.emit(
            {
                "action": args.action,
                "profile_uuid": args.profile_uuid,
                "domains": list(args.domain),
                "status": "updated",
            }
        ) if context.json_output else context.write("WARP routing обновлён.")
        return 0
    if handler == "diagnose":
        changed: list[str] = []
        if args.repair_permissions:
            _confirm(
                context,
                "Права известных каталогов менеджера и env-файлов будут ужесточены.",
                assume_yes=args.yes,
            )
            changed = repair_permissions(context.store)
        checks = run_diagnostics(context.runner, context.store)
        if context.json_output:
            context.emit({"changed_permissions": changed, "checks": checks})
        else:
            if changed:
                context.write("Исправлены права: " + ", ".join(changed))
            labels = {"ok": "OK", "warning": "ПРЕДУПРЕЖДЕНИЕ", "error": "ОШИБКА"}
            for check in checks:
                context.write(f"[{labels[check.level]}] {check.name}: {check.detail}")
        return 1 if any(check.level == "error" for check in checks) else 0
    if handler == "certificate-status":
        result = context.runner.run(
            ["certbot", "certificates"], check=False, timeout=120
        )
        output = sanitize_external_text(
            result.stdout, limit=16 * 1024
        ) or sanitize_external_text(result.stderr, limit=16 * 1024)
        if context.json_output:
            context.emit({"returncode": result.returncode, "output": output})
        else:
            context.write(output or "Certbot не вернул сведения о сертификатах.")
        return 0 if result.returncode == 0 else 1
    if handler == "certificate-issue":
        _confirm(
            context,
            "Certbot выпустит отдельный сертификат. Рабочий домен и конфигурация nginx не изменятся.",
            assume_yes=args.yes,
        )
        value = issue_certificate(
            context.runner,
            context.store.load_inventory(),
            args.domain,
            _letsencrypt_certificate_spec(args.method, args.email, context),
            wildcard=args.wildcard,
        )
        if context.json_output:
            context.emit(value)
        else:
            context.write(f"Сертификат выпущен: {value.certificate_name}")
            context.write(f"Домены: {', '.join(value.domains)}")
            context.write(f"Fullchain: {value.fullchain}")
            context.write(f"Закрытый ключ: {value.private_key}")
            context.write("Рабочая конфигурация Panel/Node не изменена.")
        return 0
    if handler == "certificate-repair-renewal":
        _confirm(
            context,
            "Менеджер проверит Certbot, заменит только узнаваемые legacy hooks/cron и включит certbot.timer.",
            assume_yes=args.yes,
        )
        inventory = context.store.load_inventory()
        compose_file = Path(inventory.compose_file)
        env_file = Path(inventory.env_file) if inventory.env_file else None
        compose = inspect_compose(context.runner, compose_file, env_file)
        plan = configure_adopted_certbot(
            context.runner,
            inventory,
            compose,
            store=context.store,
        )
        if context.json_output:
            context.emit(plan)
        elif not plan.detected:
            context.write("В управляемом стеке не обнаружены сертификаты Certbot.")
        else:
            context.write(
                "Автопродление настроено для: " + ", ".join(plan.certificate_names)
            )
            if plan.legacy_renew_hooks:
                context.write(
                    "Заменены legacy renew_hook: " + ", ".join(plan.legacy_renew_hooks)
                )
        return 0
    if handler == "certificate-renew":
        _confirm(
            context,
            "Certbot проверит продление; настроенные renewal hooks могут временно остановить nginx для HTTP-01.",
            assume_yes=args.yes,
        )
        command = ["certbot", "renew", "--non-interactive"]
        if args.dry_run:
            command.append("--dry-run")
        result = context.runner.run(
            command,
            check=False,
            timeout=1800,
            env={CERTBOT_MANAGER_LOCK_HELD_ENV: "1"},
        )
        output = sanitize_external_text(
            result.stdout, limit=16 * 1024
        ) or sanitize_external_text(result.stderr, limit=16 * 1024)
        if context.json_output:
            context.emit({"returncode": result.returncode, "output": output})
        else:
            context.write(output or "Certbot завершил работу без текстового отчёта.")
        return 0 if result.returncode == 0 else 1
    if handler == "certificate-reload":
        inventory = context.store.load_inventory()
        test_nginx(context.runner, inventory)
        reload_nginx(context.runner, inventory)
        context.emit({"status": "reloaded"}) if context.json_output else context.write(
            "Конфигурация nginx проверена, reload выполнен."
        )
        return 0
    if handler == "firewall-status":
        result = context.runner.run(
            ["ufw", "status", "verbose"], check=False, timeout=60
        )
        output = sanitize_external_text(
            result.stdout, limit=16 * 1024
        ) or sanitize_external_text(result.stderr, limit=16 * 1024)
        if context.json_output:
            context.emit({"returncode": result.returncode, "output": output})
        else:
            context.write(output or "UFW не вернул сведения о состоянии.")
        return 0 if result.returncode == 0 else 1
    if handler == "firewall-apply":
        role = args.role
        if role == "auto":
            role = context.store.load_inventory().role
        _confirm(
            context,
            f"UFW будет включён с минимальными правилами для роли {role}. Проверьте SSH-порты.",
            assume_yes=args.yes,
        )
        ports = configure_firewall(
            context.runner,
            role,
            panel_ip=args.panel_ip,
            ssh_ports=tuple(args.ssh_port) if args.ssh_port else None,
            transaction_root=context.store.paths.state / "firewall-transactions",
        )
        context.emit(
            {"role": role, "ssh_ports": ports}
        ) if context.json_output else context.write(
            "UFW настроен. Разрешённые SSH-порты: "
            + ", ".join(str(port) for port in ports)
        )
        return 0
    if handler == "system-status":
        status = host_status(context.runner, context.paths)
        if context.json_output:
            context.emit(status)
        else:
            context.write(
                "BBR: "
                + ("включён" if status.bbr_enabled else "выключен")
                + "; fq: "
                + ("включён" if status.fq_enabled else "выключен")
            )
            context.write(
                "Автоматические security updates: "
                + (
                    "настроены"
                    if status.unattended_configured
                    and status.apt_daily_timer_enabled
                    and status.apt_daily_timer_active
                    and status.apt_upgrade_timer_enabled
                    and status.apt_upgrade_timer_active
                    and status.unattended_service_enabled
                    and status.unattended_service_active
                    else "не настроены полностью"
                )
            )
        return 0
    if handler == "system-apply":
        _confirm(
            context,
            "Будут включены BBR/fq и системные таймеры unattended-upgrades без автоматической перезагрузки.",
            assume_yes=args.yes,
        )
        status = configure_host(context.runner, context.paths)
        context.emit(status) if context.json_output else context.write(
            "BBR/fq и автоматические security updates настроены и проверены."
        )
        return 0
    if handler == "maintenance-archive-stack":
        _confirm(
            context,
            "Будет создан backup, Compose-стек остановлен, а каталог перемещён в архив. "
            "Docker volumes, образы, UFW, сертификаты и WARP удаляться не будут.",
            assume_yes=args.yes,
        )
        archived = archive_stack(context.runner, context.store)
        if context.json_output:
            context.emit(archived)
        else:
            context.write(f"Стек {archived.role} архивирован.")
            context.write(f"Backup: {archived.backup}")
            context.write(f"Каталог: {archived.directory}")
            context.write(f"Inventory: {archived.inventory}")
            if archived.backup_schedule_disabled:
                context.write("Расписание backup отключено.")
        return 0
    if handler == "security-access":
        access = panel_access(context.store)
        if context.json_output:
            context.emit(access)
        else:
            context.write(f"Защищённый URL Panel: {access.url}")
            context.write(f"Режим: {access.mode}")
        return 0
    if handler == "security-rotate-access":
        _confirm(
            context,
            "Защитная cookie и URL будут заменены после backup; legacy query-cookie будет мигрирована.",
            assume_yes=args.yes,
        )
        access = rotate_panel_access(context.runner, context.store)
        context.emit(access) if context.json_output else context.write(
            f"Новый защищённый URL Panel: {access.url}"
        )
        return 0
    if handler == "security-emergency-status":
        status = emergency_access_status(context.store)
        if context.json_output:
            context.emit(status)
        elif not status.enabled:
            context.write("Аварийный доступ закрыт.")
        else:
            context.write(
                f"Аварийный доступ открыт до: {status.expires_at or 'неизвестно'}."
            )
            context.write(f"SSH-туннель: {status.ssh_forward}")
            context.write(f"После подключения откройте: {status.url}")
        return 0
    if handler == "security-emergency-open":
        _confirm(
            context,
            f"Panel будет доступна только через loopback-порт 8443 в течение {args.minutes} минут.",
            assume_yes=args.yes,
        )
        status = open_emergency_access(
            context.runner,
            context.store,
            minutes=args.minutes,
        )
        if context.json_output:
            context.emit(status)
        else:
            context.write(f"Аварийный доступ открыт до: {status.expires_at}.")
            context.write(f"Создайте SSH-туннель: {status.ssh_forward}")
            context.write(f"После подключения откройте: {status.url}")
        return 0
    if handler == "security-emergency-close":
        close_emergency_access(context.runner, context.store)
        context.emit({"enabled": False}) if context.json_output else context.write(
            "Аварийный доступ закрыт."
        )
        return 0
    raise ValidationError(f"Для команды не найден обработчик: {handler}")


def _show_warp_scan(context: CliContext, scan: Any) -> None:
    context.write(f"Конфигурация: {scan.config or 'не найдена'}")
    context.write(f"Account: {scan.account or 'не найден'}")
    context.write(f"Интерфейс warp: {'есть' if scan.interface_exists else 'нет'}")
    context.write(f"Сервис активен: {'да' if scan.unit_active else 'нет'}")
    context.write(f"Управляется менеджером: {'да' if scan.manager_state else 'нет'}")
    context.write(f"Безопасный takeover: {'да' if scan.safe_takeover else 'нет'}")
    if scan.conflicts:
        context.write("Конфликты: " + "; ".join(scan.conflicts))
    if scan.legacy_paths:
        context.write("Legacy-файлы: " + ", ".join(scan.legacy_paths))


def _is_mutating(args: argparse.Namespace) -> bool:
    handler = args.handler
    if handler in {
        "inventory",
        "backup-list",
        "backup-verify",
        "backup-schedule-status",
        "service-status",
        "service-logs",
        "registry-status",
        "warp-scan",
        "warp-status",
        "disguise-list",
        "certificate-status",
        "firewall-status",
        "system-status",
        "security-access",
        "security-emergency-status",
    }:
        return False
    return not (
        handler == "warp-adopt"
        and not args.takeover
        or handler == "diagnose"
        and not args.repair_permissions
    )


def _validate_json_mode(args: argparse.Namespace) -> None:
    if not getattr(args, "json", False):
        return
    handler = getattr(args, "handler", None)
    if handler in {None, "menu"}:
        raise ValidationError("Интерактивное меню недоступно в режиме --json.")
    if handler in {"service-logs", "service-panel-cli"}:
        raise ValidationError(
            "Режим --json недоступен для потоковых команд service logs и service panel-cli."
        )


def execute(args: argparse.Namespace, context: CliContext) -> int:
    if _is_mutating(args):
        require_root()
        require_ubuntu_2404()
        with exclusive_lock(context.paths.lock):
            assert_no_active_certbot_renewal()
            return dispatch(args, context)
    return dispatch(args, context)


def _choose(
    context: CliContext,
    title: str,
    choices: Sequence[str],
    *,
    allow_back: bool = True,
    zero_label: str = "Назад",
) -> int:
    context.render_menu(
        title,
        choices,
        allow_back=allow_back,
        zero_label=zero_label,
    )
    while True:
        raw = context.input_fn("Выберите пункт: ").strip()
        try:
            selected = int(raw)
        except ValueError:
            context.error("Введите номер пункта.")
            continue
        if allow_back and selected == 0:
            return 0
        if 1 <= selected <= len(choices):
            return selected
        context.error("Такого пункта нет.")


def _ask(
    context: CliContext,
    prompt: str,
    *,
    required: bool = True,
    default: str | None = None,
) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        value = context.input_fn(f"{prompt}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        if not required:
            return ""
        context.error("Значение не может быть пустым.")


def _yes_no(context: CliContext, prompt: str, *, default: bool = False) -> bool:
    suffix = " [Д/н]" if default else " [д/Н]"
    value = context.input_fn(prompt + suffix + ": ").strip().casefold()
    if not value:
        return default
    return value in {"д", "да", "y", "yes"}


def _ask_integer(
    context: CliContext,
    prompt: str,
    *,
    minimum: int,
    maximum: int,
    default: int,
) -> int:
    while True:
        raw = _ask(context, prompt, default=str(default))
        try:
            value = int(raw)
        except ValueError:
            context.error("Введите целое число.")
            continue
        if minimum <= value <= maximum:
            return value
        context.error(f"Допустимое значение: от {minimum} до {maximum}.")


def _interactive_certificate(context: CliContext) -> list[str]:
    selected = _choose(
        context,
        "TLS-сертификат:",
        (
            "Существующий сертификат",
            "Let's Encrypt HTTP-01",
            "Let's Encrypt Cloudflare DNS-01",
            "Let's Encrypt Gcore DNS-01",
        ),
        allow_back=False,
    )
    if selected == 1:
        return [
            "--certificate-method",
            "existing",
            "--fullchain",
            _ask(context, "Путь к fullchain.pem"),
            "--private-key",
            _ask(context, "Путь к закрытому ключу"),
        ]
    method = {2: "http-01", 3: "cloudflare", 4: "gcore"}[selected]
    return [
        "--certificate-method",
        method,
        "--email",
        _ask(context, "Email для Let's Encrypt"),
    ]


def _interactive_arguments(context: CliContext, section: int) -> list[str] | None:
    if section == 1:
        path = _ask(context, "Каталог установки", default="/opt/remnawave")
        role = _choose(
            context,
            "Ожидаемая роль:",
            ("Определить автоматически", "Panel", "Node"),
            allow_back=False,
        )
        result = ["adopt", "--path", path]
        if role in {2, 3}:
            result += ["--role", "panel" if role == 2 else "node"]
        return result
    if section == 2:
        target = _choose(
            context, "Чистая установка:", ("Panel + Subscription Page", "Node")
        )
        if target == 0:
            return None
        if target == 1:
            result = [
                "install",
                "panel",
                "--panel-domain",
                _ask(context, "Домен Panel"),
                "--subscription-domain",
                _ask(context, "Домен Subscription Page"),
            ]
            username = _ask(
                context, "Имя администратора (пусто = сгенерировать)", required=False
            )
            if username:
                result += ["--admin-username", username]
            if _yes_no(context, "Задать пароль вручную", default=False):
                result.append("--ask-admin-password")
            if not _yes_no(context, "Настроить UFW", default=True):
                result.append("--no-ufw")
            return result + _interactive_certificate(context)
        catalog = template_catalog()
        template = _choose(
            context,
            "Сайт-заглушка:",
            tuple(f"{item['name']}: {item['description']}" for item in catalog),
            allow_back=False,
        )
        result = [
            "install",
            "node",
            "--domain",
            _ask(context, "Домен Node"),
            "--panel-ip",
            _ask(context, "IPv4-адрес Panel"),
            "--template",
            catalog[template - 1]["id"],
        ]
        if not _yes_no(context, "Настроить UFW", default=True):
            result.append("--no-ufw")
        return result + _interactive_certificate(context)
    if section == 3:
        result = ["update"]
        if _yes_no(
            context, "Все Reality-клиенты имеют версию не ниже 26.3.27", default=False
        ):
            result.append("--accept-reality-client-risk")
        return result
    if section == 4:
        action = _choose(
            context,
            "Backup:",
            (
                "Создать",
                "Показать список",
                "Проверить",
                "Восстановить",
                "Статус расписания",
                "Включить расписание",
                "Отключить расписание",
            ),
        )
        if action == 0:
            return None
        if action == 1:
            return [
                "backup",
                "create",
                "--reason",
                _ask(context, "Причина", default="manual"),
            ]
        if action == 2:
            return ["backup", "list"]
        if action == 5:
            return ["backup", "schedule-status"]
        if action == 6:
            frequency = _choose(
                context,
                "Периодичность:",
                ("Ежедневно", "Еженедельно по воскресеньям"),
                allow_back=False,
            )
            return [
                "backup",
                "schedule-enable",
                "--frequency",
                "daily" if frequency == 1 else "weekly",
                "--time",
                _ask(context, "Локальное время сервера (ЧЧ:ММ)", default="03:00"),
                "--retention",
                str(
                    _ask_integer(
                        context,
                        "Количество хранимых backup",
                        minimum=1,
                        maximum=1000,
                        default=10,
                    )
                ),
            ]
        if action == 7:
            return ["backup", "schedule-disable"]
        path = _ask(context, "Путь к backup")
        if action == 3:
            return ["backup", "verify", path]
        result = ["backup", "restore", path]
        if _yes_no(context, "Не восстанавливать базу данных", default=False):
            result.append("--without-database")
        return result
    if section == 5:
        action = _choose(
            context,
            "Компоненты:",
            (
                "Статус",
                "Запустить",
                "Остановить",
                "Перезапустить",
                "Показать логи",
                "Открыть Panel CLI",
            ),
        )
        if action == 0:
            return None
        if action == 6:
            return ["service", "panel-cli"]
        names = ("status", "start", "stop", "restart", "logs")
        result = ["service", names[action - 1]]
        if action != 1:
            result.append(
                _ask(
                    context,
                    "Компонент (all, panel, subscription, node, nginx, database, cache)",
                )
            )
        if action == 5:
            result += [
                "--tail",
                str(
                    _ask_integer(
                        context,
                        "Число последних строк",
                        minimum=1,
                        maximum=10_000,
                        default=100,
                    )
                ),
            ]
            since = _ask(
                context, "Период --since (пусто = без ограничения)", required=False
            )
            if since:
                result += ["--since", since]
            if _yes_no(context, "Продолжать вывод новых строк", default=False):
                result.append("--follow")
        return result
    if section == 6:
        action = _choose(
            context, "Docker Registry:", ("Статус", "Выбрать", "Войти", "Выйти")
        )
        if action == 0:
            return None
        if action == 1:
            return ["registry", "status"]
        registry_choice = _choose(
            context,
            "Registry:",
            ("Docker Hub", "GitHub Container Registry"),
            allow_back=False,
        )
        registry = "docker-hub" if registry_choice == 1 else "ghcr"
        if action == 2:
            return ["registry", "select", registry]
        if action == 3:
            result = ["registry", "login", "--registry", registry]
            username = _ask(context, "Имя пользователя", required=False)
            if username:
                result += ["--username", username]
            if _yes_no(context, "Сделать registry выбранным", default=True):
                result.append("--select")
            return result
        return ["registry", "logout", "--registry", registry]
    if section == 7:
        labels = (
            "Проверить существующий WARP",
            "Статус",
            "Установить",
            "Принять существующий WARP",
            "Запустить",
            "Остановить",
            "Перезапустить",
            "Сменить account/profile",
            "Удалить",
        )
        action = _choose(context, "WARP:", labels)
        if action == 0:
            return None
        names = (
            "scan",
            "status",
            "install",
            "adopt",
            "start",
            "stop",
            "restart",
            "rotate",
            "uninstall",
        )
        result = ["warp", names[action - 1]]
        if action in {3, 8}:
            result.append("--accept-tos")
            if _yes_no(context, "Использовать WARP+ key", default=False):
                result.append("--plus")
        if action == 4 and _yes_no(
            context, "Выполнить takeover после scan", default=False
        ):
            result.append("--takeover")
        if action == 9 and _yes_no(context, "Удалить WARP credentials", default=False):
            result.append("--purge-credentials")
        return result
    if section == 8:
        action = _choose(
            context, "Сайты-заглушки:", ("Показать шаблоны", "Установить шаблон")
        )
        if action == 0:
            return None
        if action == 1:
            return ["disguise", "list"]
        catalog = template_catalog()
        selected = _choose(
            context,
            "Шаблон:",
            tuple(item["name"] for item in catalog),
            allow_back=False,
        )
        return ["disguise", "apply", catalog[selected - 1]["id"]]
    if section == 9:
        action = _choose(
            context,
            "Panel API:",
            ("Создать Reality Node", "Применить WARP routing", "Удалить WARP routing"),
        )
        if action == 0:
            return None
        base = _ask(context, "URL Panel API", default="http://127.0.0.1:3000")
        if action == 1:
            return [
                "api",
                "reality",
                "--base-url",
                base,
                "--profile-name",
                _ask(context, "Имя Config Profile"),
                "--inbound-tag",
                _ask(context, "Inbound tag"),
                "--node-name",
                _ask(context, "Имя Node"),
                "--domain",
                _ask(context, "Домен Node"),
            ]
        result = [
            "api",
            "warp-routing",
            "apply" if action == 2 else "remove",
            "--base-url",
            base,
            "--profile-uuid",
            _ask(context, "UUID Config Profile"),
        ]
        if action == 2:
            domains = _ask(context, "Домены через запятую")
            for domain in (item.strip() for item in domains.split(",")):
                if domain:
                    result += ["--domain", domain]
        return result
    if section == 10:
        result = ["diagnose"]
        if _yes_no(context, "Исправить известные небезопасные права", default=False):
            result.append("--repair-permissions")
        return result
    if section == 11:
        action = _choose(
            context,
            "TLS-сертификаты:",
            (
                "Статус Certbot",
                "Выпустить сертификат для нового домена",
                "Продлить",
                "Тестовое продление",
                "Восстановить автопродление",
                "Reload nginx",
            ),
        )
        if action == 0:
            return None
        if action == 1:
            return ["certificate", "status"]
        if action == 2:
            method_choice = _choose(
                context,
                "Способ подтверждения домена:",
                ("HTTP-01", "Cloudflare DNS-01", "Gcore DNS-01"),
                allow_back=False,
            )
            method = ("http-01", "cloudflare", "gcore")[method_choice - 1]
            result = [
                "certificate",
                "issue",
                "--domain",
                _ask(context, "Домен нового сертификата"),
                "--method",
                method,
                "--email",
                _ask(context, "Email для Let's Encrypt"),
            ]
            if method != "http-01" and _yes_no(
                context, "Добавить wildcard SAN для *.DOMAIN", default=True
            ):
                result.append("--wildcard")
            return result
        if action == 5:
            return ["certificate", "repair-renewal"]
        if action == 6:
            return ["certificate", "reload"]
        result = ["certificate", "renew"]
        if action == 4:
            result.append("--dry-run")
        return result
    if section == 12:
        action = _choose(context, "UFW:", ("Статус", "Применить правила"))
        if action == 0:
            return None
        if action == 1:
            return ["firewall", "status"]
        role = _choose(
            context,
            "Роль:",
            ("Определить из inventory", "Panel", "Node"),
            allow_back=False,
        )
        selected_role = ("auto", "panel", "node")[role - 1]
        effective_role = (
            context.store.load_inventory().role
            if selected_role == "auto"
            else selected_role
        )
        result = ["firewall", "apply", "--role", selected_role]
        if effective_role == "node":
            result += ["--panel-ip", _ask(context, "IPv4-адрес Panel")]
        return result
    if section == 13:
        action = _choose(
            context,
            "Защитный доступ Panel:",
            (
                "Показать URL",
                "Ротировать cookie и URL",
                "Статус аварийного доступа",
                "Открыть аварийный доступ через SSH-туннель",
                "Закрыть аварийный доступ",
            ),
        )
        if action == 0:
            return None
        if action == 1:
            return ["security", "access"]
        if action == 2:
            return ["security", "rotate-access"]
        if action == 3:
            return ["security", "emergency-status"]
        if action == 5:
            return ["security", "emergency-close"]
        minutes = _ask_integer(
            context,
            "Срок аварийного доступа, минут",
            minimum=5,
            maximum=120,
            default=30,
        )
        return ["security", "emergency-open", "--minutes", str(minutes)]
    if section == 14:
        return ["inventory"]
    if section == 15:
        action = _choose(
            context,
            "Настройка Ubuntu:",
            ("Показать состояние", "Включить BBR/fq и security updates"),
        )
        if action == 0:
            return None
        return ["system", "status" if action == 1 else "apply"]
    if section == 16:
        return ["maintenance", "archive-stack"]
    return None


def interactive_menu(parser: RussianArgumentParser, context: CliContext) -> int:
    sections = (
        "Обнаружить и принять установку",
        "Чистая установка",
        "Обновить компоненты",
        "Резервные копии и восстановление",
        "Запуск, остановка и статус компонентов",
        "Docker Registry",
        "Cloudflare WARP",
        "Сайты-заглушки Node",
        "Panel API: Reality и WARP routing",
        "Диагностика и права",
        "TLS-сертификаты",
        "Межсетевой экран UFW",
        "Защитный URL и cookie Panel",
        "Показать inventory",
        "BBR и автоматические security updates",
        "Архивировать стек перед удалением или переустановкой",
    )
    while True:
        selected = _choose(context, "Главное меню:", sections, zero_label="Выход")
        if selected == 0:
            context.write("Работа завершена.")
            return 0
        pause_after_result = False
        try:
            arguments = _interactive_arguments(context, selected)
            if arguments is None:
                continue
            pause_after_result = True
            args = parser.parse_args(arguments)
            args.json = False
            context.json_output = False
            return_code = execute(args, context)
            if return_code != 0:
                context.error(f"Операция завершилась с кодом {return_code}.")
        except ManagerError as error:
            pause_after_result = True
            context.error(f"Ошибка: {sanitize_external_text(str(error))}")
        except (EOFError, KeyboardInterrupt):
            context.error("Операция прервана пользователем.")
            return 130
        finally:
            if pause_after_result:
                context.pause()


def main(
    argv: Sequence[str] | None = None,
    *,
    runtime_paths: RuntimePaths | None = None,
    runner: Runner | None = None,
    input_fn: InputFunction | None = None,
    secret_fn: SecretFunction | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = runtime_paths or RuntimePaths()
    context = CliContext(
        runner=runner or Runner(),
        store=StateStore(paths),
        paths=paths,
        stdout=stdout or sys.stdout,
        stderr=stderr or sys.stderr,
        input_fn=input_fn or input,
        secret_fn=secret_fn or _secure_getpass,
        json_output=bool(args.json),
    )
    try:
        _validate_json_mode(args)
    except ManagerError as error:
        context.error(f"Ошибка: {sanitize_external_text(str(error))}")
        return 2
    if getattr(args, "handler", None) in {None, "menu"}:
        try:
            return interactive_menu(parser, context)
        except (EOFError, KeyboardInterrupt):
            context.error("Интерактивный режим завершён.")
            return 130
    try:
        return execute(args, context)
    except ManagerError as error:
        context.error(f"Ошибка: {sanitize_external_text(str(error))}")
        return 2
    except EOFError:
        context.error(
            "Ошибка: интерактивный ввод недоступен; задайте нужные параметры и секреты через окружение."
        )
        return 2
    except KeyboardInterrupt:
        context.error("Операция прервана пользователем.")
        return 130


__all__ = ["build_parser", "dispatch", "execute", "interactive_menu", "main"]
