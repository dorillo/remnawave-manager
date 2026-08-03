from __future__ import annotations

import ipaddress
import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .api import REALITY_RECOVERY_NAME
from .backup import _postgres_identity
from .certificates import CERTBOT_HOOK_VERSION_MARKER
from .compat import detect_component_version
from .compose import compose_command, inspect_compose
from .errors import TransactionError, ValidationError
from .health import (
    _missing_unix_sockets,
    check_node_runtime,
    check_panel_http,
    check_subscription_http,
)
from .integrity import configuration_drift
from .models import Inventory
from .nginx import test_nginx
from .runner import (
    Runner,
    command_exists,
    read_stable_regular_file,
    sanitize_external_text,
)
from .state import _MAX_STATE_FILE_SIZE, StateStore, _read_private_json

Level = Literal["ok", "warning", "error"]
_LEGACY_LOG = "usr/local/remnawave_reverse/remnawave_reverse.log"
_CERTBOT_HOOK_MARKER = "# Managed by remnawave-manager"
_PRIVATE_FILE_KINDS = frozenset({"compose", "env", "nginx", "secret"})
_SENSITIVE_COMPONENTS = frozenset({"panel", "subscription", "database", "cache"})
_FIREWALL_TRANSACTION_NAME = re.compile(r"ufw-[0-9a-f]{32}")
_MAX_FIREWALL_MANIFEST_SIZE = 64 * 1024
_BOOTSTRAP_CREDENTIALS_NAME = ".bootstrap-credentials.json"
_MAX_BOOTSTRAP_CREDENTIALS_SIZE = 64 * 1024
_MAX_REALITY_RECOVERY_SIZE = 64 * 1024
_MAX_CERTBOT_HOOK_SIZE = 1024 * 1024
_BASE_RUNTIME_DEPENDENCIES = (
    "docker",
    "curl",
    "openssl",
    "systemctl",
    "ss",
    "ip",
    "sshd",
    "ufw",
    "certbot",
    "sysctl",
    "modprobe",
)


@dataclass(slots=True)
class Check:
    level: Level
    name: str
    detail: str

    def __post_init__(self) -> None:
        self.name = sanitize_external_text(self.name, limit=256)
        self.detail = sanitize_external_text(self.detail, limit=4000)


def _manager_directory_check(path: Path) -> Check:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return Check("error", "Каталоги менеджера", f"{path}: отсутствует")
    except OSError as error:
        return Check(
            "error",
            "Каталоги менеджера",
            f"{path}: не удалось проверить: {error}",
        )
    if not stat.S_ISDIR(info.st_mode):
        return Check(
            "error",
            "Каталоги менеджера",
            f"{path}: ожидается обычный каталог, symlink недопустим",
        )
    mode = stat.S_IMODE(info.st_mode)
    if os.name == "posix" and info.st_uid != 0:
        return Check(
            "error",
            "Каталоги менеджера",
            f"{path}: владелец UID {info.st_uid}, ожидается root",
        )
    if mode != 0o700:
        return Check(
            "error",
            "Каталоги менеджера",
            f"{path}: права {mode:o}, ожидается 700",
        )
    return Check("ok", "Каталоги менеджера", f"{path}: root, {mode:o}")


def _manager_state_file_check(
    path: Path,
    *,
    required: bool,
) -> Check:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return Check(
            "error" if required else "ok",
            "State-файлы менеджера",
            f"{path}: {'отсутствует' if required else 'не создан (необязательный)'}",
        )
    except OSError as error:
        return Check(
            "error",
            "State-файлы менеджера",
            f"{path}: не удалось проверить: {error}",
        )
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return Check(
            "error",
            "State-файлы менеджера",
            f"{path}: ожидается обычный файл без symlink/hardlink",
        )
    mode = stat.S_IMODE(info.st_mode)
    if os.name == "posix" and info.st_uid != 0:
        return Check(
            "error",
            "State-файлы менеджера",
            f"{path}: владелец UID {info.st_uid}, ожидается root",
        )
    if mode != 0o600:
        return Check(
            "error",
            "State-файлы менеджера",
            f"{path}: права {mode:o}, ожидается 600",
        )
    if info.st_size > _MAX_STATE_FILE_SIZE:
        return Check(
            "error",
            "State-файлы менеджера",
            f"{path}: превышен лимит {_MAX_STATE_FILE_SIZE} байт",
        )
    return Check("ok", "State-файлы менеджера", f"{path}: root, {mode:o}")


def _manager_storage_checks(store: StateStore) -> list[Check]:
    checks = [
        _manager_directory_check(path)
        for path in (
            store.paths.etc,
            store.paths.state,
            store.paths.backups,
            store.paths.logs,
        )
    ]
    checks.extend(
        (
            _manager_state_file_check(store.paths.inventory, required=True),
            _manager_state_file_check(store.paths.settings, required=False),
            _manager_state_file_check(store.paths.secrets, required=False),
        )
    )
    return checks


def _bootstrap_credentials_check(inventory: Inventory) -> Check | None:
    if inventory.role != "panel":
        return None
    path = Path(inventory.install_dir) / _BOOTSTRAP_CREDENTIALS_NAME
    if not path.exists() and not path.is_symlink():
        return None
    try:
        payload = _read_private_json(
            path,
            label="временный файл учётных данных Panel",
            max_size=_MAX_BOOTSTRAP_CREDENTIALS_SIZE,
            required_mode=0o600,
        )
    except (OSError, ValidationError) as error:
        return Check(
            "error",
            "Временные учётные данные Panel",
            f"{path}: файл небезопасен или повреждён: {error}",
        )
    if (
        not isinstance(payload, dict)
        or set(payload) != {"имя_администратора", "пароль_администратора"}
        or not all(isinstance(value, str) and value for value in payload.values())
    ):
        return Check(
            "error",
            "Временные учётные данные Panel",
            f"{path}: неожиданный формат recovery-файла; проверьте его вручную",
        )
    return Check(
        "warning",
        "Временные учётные данные Panel",
        f"{path}: обнаружена recovery-копия пароля после прерванной выдачи; "
        "сохраните пароль в менеджере паролей и удалите файл вручную",
    )


def _reality_credentials_check(store: StateStore) -> Check | None:
    path = store.paths.state / REALITY_RECOVERY_NAME
    if not path.exists() and not path.is_symlink():
        return None
    try:
        payload = _read_private_json(
            path,
            label="recovery-файл Reality credentials",
            max_size=_MAX_REALITY_RECOVERY_SIZE,
            required_mode=0o600,
        )
    except (OSError, ValidationError) as error:
        return Check(
            "error",
            "Временные учётные данные Reality",
            f"{path}: файл небезопасен или повреждён: {error}",
        )
    uuid_fields = ("profile_uuid", "inbound_uuid", "node_uuid", "host_uuid")
    expected_keys = {"schema_version", *uuid_fields, "secret_key"}
    valid = (
        isinstance(payload, dict)
        and set(payload) == expected_keys
        and payload.get("schema_version") == 1
        and not isinstance(payload.get("schema_version"), bool)
        and all(
            isinstance(payload.get(field), str)
            and re.fullmatch(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                payload[field],
            )
            for field in uuid_fields
        )
        and isinstance(payload.get("secret_key"), str)
        and 1 <= len(payload["secret_key"]) <= 16_384
        and all(33 <= ord(character) <= 126 for character in payload["secret_key"])
    )
    if not valid:
        return Check(
            "error",
            "Временные учётные данные Reality",
            f"{path}: неожиданный формат recovery-файла; проверьте его вручную",
        )
    return Check(
        "warning",
        "Временные учётные данные Reality",
        f"{path}: SECRET_KEY не был подтверждён как выданный оператору; "
        "сохраните credentials и удалите файл вручную",
    )


def _firewall_transaction_checks(store: StateStore) -> list[Check]:
    root = store.paths.state / "firewall-transactions"
    try:
        info = root.lstat()
    except FileNotFoundError:
        return [
            Check(
                "ok",
                "UFW-транзакции",
                "каталог снимков не создан; незавершённых операций нет",
            )
        ]
    except OSError as error:
        return [
            Check(
                "error",
                "UFW-транзакции",
                f"не удалось проверить {root}: {error}",
            )
        ]
    if not stat.S_ISDIR(info.st_mode):
        return [
            Check(
                "error",
                "UFW-транзакции",
                f"{root}: ожидается обычный каталог, symlink недопустим",
            )
        ]
    mode = stat.S_IMODE(info.st_mode)
    if os.name == "posix" and info.st_uid != 0:
        return [
            Check(
                "error",
                "UFW-транзакции",
                f"{root}: владелец UID {info.st_uid}, ожидается root",
            )
        ]
    if os.name == "posix" and mode != 0o700:
        return [
            Check(
                "error",
                "UFW-транзакции",
                f"{root}: права {mode:o}, ожидается 700",
            )
        ]
    try:
        entries = sorted(root.iterdir(), key=lambda item: item.name)
    except OSError as error:
        return [
            Check(
                "error",
                "UFW-транзакции",
                f"не удалось прочитать {root}: {error}",
            )
        ]
    if not entries:
        return [
            Check(
                "ok",
                "UFW-транзакции",
                "незавершённых операций нет",
            )
        ]
    return [_firewall_transaction_entry_check(entry) for entry in entries]


def _firewall_transaction_entry_check(entry: Path) -> Check:
    prefix = f"обнаружена незавершённая UFW-транзакция {entry}"
    if _FIREWALL_TRANSACTION_NAME.fullmatch(entry.name) is None:
        return Check(
            "error",
            "UFW-транзакции",
            f"{prefix}: неожиданное имя; проверьте каталог вручную",
        )
    try:
        info = entry.lstat()
    except OSError as error:
        return Check(
            "error",
            "UFW-транзакции",
            f"{prefix}: не удалось проверить каталог: {error}",
        )
    if not stat.S_ISDIR(info.st_mode):
        return Check(
            "error",
            "UFW-транзакции",
            f"{prefix}: ожидается обычный каталог, symlink недопустим",
        )
    mode = stat.S_IMODE(info.st_mode)
    if os.name == "posix" and info.st_uid != 0:
        return Check(
            "error",
            "UFW-транзакции",
            f"{prefix}: владелец UID {info.st_uid}, ожидается root",
        )
    if os.name == "posix" and mode != 0o700:
        return Check(
            "error",
            "UFW-транзакции",
            f"{prefix}: права каталога {mode:o}, ожидается 700",
        )
    manifest = entry / "manifest.json"
    try:
        payload = _read_private_json(
            manifest,
            label="manifest UFW-транзакции",
            max_size=_MAX_FIREWALL_MANIFEST_SIZE,
            required_mode=0o600,
        )
    except (OSError, ValidationError) as error:
        return Check(
            "error",
            "UFW-транзакции",
            f"{prefix}: manifest небезопасен или повреждён: {error}",
        )
    if (
        not isinstance(payload, dict)
        or isinstance(payload.get("schema_version"), bool)
        or payload.get("schema_version") != 1
    ):
        return Check(
            "error",
            "UFW-транзакции",
            f"{prefix}: manifest должен быть JSON-объектом schema_version=1",
        )
    return Check(
        "error",
        "UFW-транзакции",
        f"{prefix}: manifest корректен; автоматическое восстановление не выполнялось",
    )


def _check_permissions(path: Path) -> Check:
    if path.is_symlink():
        return Check(
            "error",
            "Права файлов",
            f"{path}: символическая ссылка недопустима",
        )
    if not path.is_file():
        return Check(
            "error",
            "Права файлов",
            f"{path}: ожидается обычный файл с правами 600",
        )
    info = path.stat()
    if info.st_nlink != 1:
        return Check(
            "error",
            "Права файлов",
            f"{path}: hard link недопустим",
        )
    if os.name == "posix" and info.st_uid != 0:
        return Check(
            "error",
            "Права файлов",
            f"{path}: владелец UID {info.st_uid}, ожидается root",
        )
    mode = info.st_mode & 0o777
    if mode != 0o600:
        return Check("error", "Права файлов", f"{path}: {mode:o}, ожидается 600")
    return Check("ok", "Права файлов", f"{path}: {mode:o}")


def _legacy_log_check(store: StateStore) -> Check | None:
    path = store.paths.root / _LEGACY_LOG
    if path.is_symlink():
        return Check(
            "error",
            "Legacy log remnawave-reverse-proxy",
            f"{path}: обнаружена символьная ссылка; проверьте её вручную",
        )
    if not path.is_file():
        return None
    mode = path.stat().st_mode & 0o777
    if mode & 0o077:
        return Check(
            "error",
            "Legacy log remnawave-reverse-proxy",
            f"{path}: права {mode:o} допускают group/world access; выполните diagnose --repair-permissions",
        )
    return Check(
        "warning",
        "Legacy log remnawave-reverse-proxy",
        f"{path}: права {mode:o}; файл может содержать историю установки, проверьте необходимость хранения",
    )


def _runtime_dependencies(inventory: Inventory) -> tuple[str, ...]:
    dependencies = list(_BASE_RUNTIME_DEPENDENCIES)
    if inventory.features.get("warp"):
        dependencies.extend(("wg", "wg-quick", "nft"))
    return tuple(dependencies)


def run_diagnostics(runner: Runner, store: StateStore) -> list[Check]:
    checks = _manager_storage_checks(store)
    checks.extend(_firewall_transaction_checks(store))
    reality_credentials = _reality_credentials_check(store)
    if reality_credentials is not None:
        checks.append(reality_credentials)
    try:
        inventory = store.load_inventory()
    except Exception as error:  # noqa: BLE001 - return actionable storage checks
        checks.append(Check("error", "Инвентаризация", str(error)))
        return checks

    bootstrap_credentials = _bootstrap_credentials_check(inventory)
    if bootstrap_credentials is not None:
        checks.append(bootstrap_credentials)

    for path, loader in (
        (store.paths.settings, store.load_settings),
        (store.paths.secrets, store.load_secrets),
    ):
        if not path.exists() and not path.is_symlink():
            continue
        try:
            loader()
        except Exception as error:  # noqa: BLE001 - continue independent diagnostics
            checks.append(Check("error", "State-файлы менеджера", f"{path}: {error}"))
    try:
        legacy_log = _legacy_log_check(store)
        if legacy_log is not None:
            checks.append(legacy_log)
    except Exception as error:  # noqa: BLE001 - continue independent diagnostics
        checks.append(Check("error", "Legacy log remnawave-reverse-proxy", str(error)))
    for command in _runtime_dependencies(inventory):
        available = command_exists(command)
        checks.append(
            Check(
                "ok" if available else "error",
                "Зависимости",
                f"{command}: {'найден' if available else 'не найден'}",
            )
        )
    try:
        checks.extend(_certbot_renewal_checks(runner, inventory))
    except Exception as error:  # noqa: BLE001 - continue independent diagnostics
        checks.append(Check("error", "Certbot renewal", str(error)))
    compose_file = Path(inventory.compose_file)
    env_file = Path(inventory.env_file) if inventory.env_file else None
    try:
        result = runner.run(
            compose_command(compose_file, "config", "-q", env_file=env_file),
            cwd=compose_file.parent,
            check=False,
            sensitive=True,
        )
        checks.append(
            Check(
                "ok" if result.returncode == 0 else "error",
                "Docker Compose",
                "конфигурация валидна"
                if result.returncode == 0
                else "docker compose config завершился ошибкой",
            )
        )
    except Exception as error:  # noqa: BLE001 - continue independent diagnostics
        checks.append(Check("error", "Docker Compose", str(error)))
    try:
        drift = configuration_drift(inventory)
        checks.append(
            Check(
                "warning" if drift else "ok",
                "Инвентаризация",
                "; ".join(drift)
                if drift
                else "защищённые файлы соответствуют сохранённым хешам",
            )
        )
    except Exception as error:  # noqa: BLE001 - continue independent diagnostics
        checks.append(Check("error", "Инвентаризация", str(error)))
    transaction = store.paths.state / "active-transaction.json"
    if transaction.is_symlink() or (transaction.exists() and not transaction.is_file()):
        checks.append(Check("error", "Транзакция", "journal имеет небезопасный тип"))
    elif transaction.is_file():
        checks.append(
            Check(
                "error",
                "Транзакция",
                "обнаружен незавершённый journal; проверьте backup и состояние контейнеров",
            )
        )
    else:
        checks.append(Check("ok", "Транзакция", "незавершённых операций нет"))

    database_size: int | None = None
    if inventory.role == "panel" and "database" in inventory.components:
        try:
            database_size = _database_size_bytes(runner, inventory)
            checks.append(
                Check(
                    "ok",
                    "Размер PostgreSQL",
                    f"{database_size / (1024**3):.1f} GiB",
                )
            )
        except Exception as error:  # noqa: BLE001 - continue diagnostics
            checks.append(Check("warning", "Размер PostgreSQL", str(error)))
    try:
        usage = shutil.disk_usage(inventory.install_dir)
        free_gib = usage.free / (1024**3)
        required_bytes = max(
            2 * 1024**3,
            (database_size * 2 + 1024**3) if database_size is not None else 0,
        )
        required_gib = required_bytes / (1024**3)
        checks.append(
            Check(
                "ok" if usage.free >= required_bytes else "warning",
                "Свободное место",
                f"{free_gib:.1f} GiB; оценочный минимум {required_gib:.1f} GiB",
            )
        )
    except Exception as error:  # noqa: BLE001 - continue independent diagnostics
        checks.append(Check("error", "Свободное место", str(error)))
    for item in inventory.managed_files:
        if item.kind in _PRIVATE_FILE_KINDS:
            try:
                checks.append(_check_permissions(Path(item.path)))
            except Exception as error:  # noqa: BLE001 - report every file
                checks.append(Check("error", "Права файлов", f"{item.path}: {error}"))

    try:
        test_nginx(runner, inventory)
        checks.append(Check("ok", "Nginx", "nginx -t выполнен успешно"))
    except Exception as error:  # noqa: BLE001 - continue independent diagnostics
        checks.append(Check("error", "Nginx", str(error)))

    try:
        compose = inspect_compose(runner, compose_file, env_file)
        exposed = _unexpected_exposed_ports(compose, inventory)
        checks.append(
            Check(
                "error" if exposed else "ok",
                "Публикация портов",
                ", ".join(exposed)
                if exposed
                else "служебные порты не опубликованы на всех интерфейсах",
            )
        )
    except Exception as error:  # noqa: BLE001 - continue independent diagnostics
        checks.append(Check("error", "Публикация портов", str(error)))

    missing_sockets = _missing_unix_sockets(inventory.xhttp_sockets)
    checks.append(
        Check(
            "error" if missing_sockets else "ok",
            "XHTTP Unix sockets",
            ", ".join(missing_sockets)
            if missing_sockets
            else "все обнаруженные sockets существуют",
        )
    )
    try:
        if inventory.role == "panel":
            check_panel_http(runner, inventory.components["panel"])
            if "subscription" in inventory.components:
                subscription = inventory.components["subscription"]
                subscription_version = detect_component_version(
                    runner, "subscription", subscription
                )
                check_subscription_http(
                    runner,
                    subscription,
                    legacy=subscription_version == "7.2.6",
                )
        else:
            check_node_runtime(runner, inventory)
        checks.append(Check("ok", "Runtime", "компоненты прошли прикладную проверку"))
    except Exception as error:  # noqa: BLE001 - return the full diagnostic set
        checks.append(Check("error", "Runtime", str(error)))
    return checks


def _database_size_bytes(runner: Runner, inventory: Inventory) -> int:
    component = inventory.components["database"]
    container = component.container or component.service
    user, database = _postgres_identity(runner, container)
    result = runner.run(
        [
            "docker",
            "exec",
            container,
            "psql",
            "--username",
            user,
            "--dbname",
            database,
            "--tuples-only",
            "--no-align",
            "--set",
            "ON_ERROR_STOP=1",
            "--command",
            "SELECT pg_database_size(current_database());",
        ],
        check=False,
        timeout=60,
    )
    value = result.stdout.strip()
    if result.returncode != 0 or not value.isdigit() or int(value) <= 0:
        raise TransactionError("Не удалось определить размер рабочей PostgreSQL БД.")
    return int(value)


def _certbot_renewal_checks(
    runner: Runner,
    inventory: Inventory,
    *,
    hook_root: Path = Path("/etc/letsencrypt/renewal-hooks"),
) -> list[Check]:
    if not inventory.features.get("certbot_renewal"):
        return []
    checks: list[Check] = []
    for state in ("is-enabled", "is-active"):
        result = runner.run(
            ["systemctl", state, "certbot.timer"], check=False, timeout=30
        )
        label = "включён" if state == "is-enabled" else "активен"
        checks.append(
            Check(
                "ok" if result.returncode == 0 else "error",
                "Certbot timer",
                f"certbot.timer {label}"
                if result.returncode == 0
                else f"certbot.timer не {label}",
            )
        )
    all_hook_paths = [
        hook_root / phase / "remnawave-manager-nginx"
        for phase in ("deploy", "pre", "post")
    ]
    required = [all_hook_paths[0]]
    if inventory.features.get("certbot_standalone"):
        required.extend(
            (
                hook_root / "pre" / "remnawave-manager-nginx",
                hook_root / "post" / "remnawave-manager-nginx",
            )
        )
    invalid: list[str] = []
    for path in all_hook_paths:
        if path not in required:
            if path.exists() or path.is_symlink():
                invalid.append(str(path))
            continue
        try:
            snapshot = read_stable_regular_file(
                path,
                max_size=_MAX_CERTBOT_HOOK_SIZE,
                label="Certbot renewal hook",
            )
            text = snapshot.data.decode("utf-8", errors="strict")
        except (OSError, UnicodeError, ValidationError):
            invalid.append(str(path))
            continue
        mode = snapshot.mode
        unsafe_mode = mode & (
            stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX
        )
        if (
            (os.name == "posix" and snapshot.uid != 0)
            or (os.name == "posix" and mode & 0o111 == 0)
            or (os.name == "posix" and unsafe_mode)
            or _CERTBOT_HOOK_MARKER not in text.splitlines()
            or CERTBOT_HOOK_VERSION_MARKER not in text.splitlines()
        ):
            invalid.append(str(path))
    checks.append(
        Check(
            "error" if invalid else "ok",
            "Certbot renewal hooks",
            "устарели, повреждены, отсутствуют или лишние: "
            + ", ".join(invalid)
            + "; выполните sudo rwm certificate repair-renewal"
            if invalid
            else "manager hooks установлены",
        )
    )
    return checks


def _unexpected_exposed_ports(compose: dict, inventory: Inventory) -> list[str]:
    sensitive_services = {
        component.service
        for name, component in inventory.components.items()
        if name in _SENSITIVE_COMPONENTS
    }
    found: list[str] = []
    for service_name, service in compose.get("services", {}).items():
        if service_name not in sensitive_services or not isinstance(service, dict):
            continue
        if service.get("network_mode") == "host":
            found.append(f"{service_name}:network_mode=host")
        for port in service.get("ports", []) or []:
            if not isinstance(port, dict):
                continue
            published = port.get("published")
            host_ip = str(
                port.get("host_ip") or "0.0.0.0"  # noqa: S104, RUF100 - diagnostic sentinel for an unspecified bind
            ).strip()
            if _is_loopback_address(host_ip):
                continue
            label = str(published).strip() if published is not None else ""
            if not label:
                target = port.get("target")
                label = (
                    f"динамический->{target}" if target is not None else "динамический"
                )
            found.append(f"{service_name}:{label} на {host_ip}")
    return found


def _is_loopback_address(value: str) -> bool:
    selected = value.strip()
    if selected.startswith("[") and selected.endswith("]"):
        selected = selected[1:-1]
    try:
        return ipaddress.ip_address(selected).is_loopback
    except ValueError:
        return False


def _repair_directory(path: Path) -> bool:
    if os.name != "posix":
        info = path.stat()
        changed = info.st_mode & 0o777 != 0o700
        if changed:
            os.chmod(path, 0o700)
        return changed
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise ValidationError(f"Каталог {path} имеет небезопасный тип.")
        changed = False
        if stat.S_IMODE(info.st_mode) != 0o700:
            os.fchmod(descriptor, 0o700)
            changed = True
        if info.st_uid != 0:
            os.fchown(descriptor, 0, 0)
            changed = True
        return changed
    finally:
        os.close(descriptor)


def _repair_regular_file(path: Path, mode: int, *, label: str) -> bool:
    if os.name != "posix":
        if path.is_symlink() or not path.is_file():
            raise ValidationError(f"{label} {path} имеет небезопасный тип.")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValidationError(f"{label} {path} имеет небезопасный тип.")
        changed = stat.S_IMODE(info.st_mode) != mode
        if changed:
            os.chmod(path, mode)
        return changed
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValidationError(f"{label} {path} имеет небезопасный тип.")
        changed = False
        if stat.S_IMODE(info.st_mode) != mode:
            os.fchmod(descriptor, mode)
            changed = True
        if info.st_uid != 0:
            os.fchown(descriptor, 0, 0)
            changed = True
        return changed
    finally:
        os.close(descriptor)


def repair_permissions(store: StateStore) -> list[str]:
    try:
        changed: list[str] = []
        for directory in (
            store.paths.etc,
            store.paths.state,
            store.paths.backups,
            store.paths.logs,
        ):
            if directory.is_symlink() or (
                directory.exists() and not directory.is_dir()
            ):
                raise ValidationError(f"Каталог {directory} имеет небезопасный тип.")
            if not directory.exists():
                directory.mkdir(parents=True, mode=0o700)
                changed.append(str(directory))
            if _repair_directory(directory):
                changed.append(str(directory))
        if store.paths.inventory.is_symlink() or not store.paths.inventory.is_file():
            raise ValidationError(
                f"Файл инвентаризации {store.paths.inventory} имеет небезопасный тип."
            )
        if _repair_regular_file(
            store.paths.inventory,
            0o600,
            label="Файл инвентаризации",
        ):
            changed.append(str(store.paths.inventory))
        for path, label in (
            (store.paths.settings, "Файл настроек менеджера"),
            (store.paths.secrets, "Файл секретов менеджера"),
        ):
            if path.is_symlink():
                raise ValidationError(f"{label} {path} имеет небезопасный тип.")
            if path.exists() and _repair_regular_file(path, 0o600, label=label):
                changed.append(str(path))
        inventory = store.load_inventory()
        for item in inventory.managed_files:
            path = Path(item.path)
            if item.kind in _PRIVATE_FILE_KINDS and _repair_regular_file(
                path,
                0o600,
                label="Managed-файл",
            ):
                changed.append(str(path))
        legacy_log = store.paths.root / _LEGACY_LOG
        if legacy_log.is_symlink():
            raise ValidationError(f"Legacy log {legacy_log} имеет небезопасный тип.")
        if legacy_log.exists() and _repair_regular_file(
            legacy_log,
            0o600,
            label="Legacy log",
        ):
            changed.append(str(legacy_log))
        return changed
    except OSError as error:
        raise ValidationError(
            f"Не удалось безопасно восстановить права файлов: {error}"
        ) from error
