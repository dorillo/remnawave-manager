from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from .backup import create_backup
from .errors import TransactionError, ValidationError
from .models import Inventory, ManagedFile
from .nginx import activate_nginx_config, nginx_is_running
from .runner import (
    Runner,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    read_stable_regular_file,
)
from .state import StateStore


@dataclass(frozen=True, slots=True)
class PanelAccess:
    url: str
    mode: str


@dataclass(frozen=True, slots=True)
class EmergencyAccess:
    enabled: bool
    url: str | None
    expires_at: str | None
    ssh_forward: str | None


_MANAGER_MAP = re.compile(
    r'map\s+\$cookie_(?P<name>rwm_[a-z0-9]{16,64})\s+\$panel_authorized\s*\{'
    r'(?:(?!\n\}).)*?"(?P<value>[A-Za-z0-9_-]{32,128})"\s+1;',
    re.DOTALL,
)
_MANAGER_GATE = re.compile(r"location\s+=\s+(?P<path>/_rwm/[A-Za-z0-9_-]{32,128})\s*\{")
_LEGACY_QUERY = re.compile(
    r'map\s+\$arg_(?P<name>[A-Za-z]{8,64})\s+\$auth_query\s*\{'
    r'(?:(?!\n\}).)*?"(?P<value>[A-Za-z]{8,128})"\s+1;',
    re.DOTALL,
)
_EMERGENCY_BEGIN = "# BEGIN REMNAWAVE-MANAGER EMERGENCY ACCESS"
_EMERGENCY_END = "# END REMNAWAVE-MANAGER EMERGENCY ACCESS"
_EMERGENCY_SERVICE = "remnawave-manager-emergency-close.service"
_EMERGENCY_TIMER = "remnawave-manager-emergency-close.timer"
_UNIT_MARKER = "X-Remnawave-Manager=true"
_MAX_MANAGER_UNIT_SIZE = 1024 * 1024
_MAX_NGINX_CONFIG_SIZE = 16 * 1024 * 1024

SystemdEnablement = Literal[
    "enabled",
    "enabled-runtime",
    "disabled",
    "masked",
    "masked-runtime",
    "static",
    "indirect",
    "not-found",
]


def _read_nginx_snapshot(path: Path) -> tuple[str, int]:
    snapshot = read_stable_regular_file(
        path,
        max_size=_MAX_NGINX_CONFIG_SIZE,
        label="Nginx-конфигурация",
    )
    if os.name == "posix" and (
        snapshot.uid != os.geteuid() or snapshot.mode & 0o022
    ):
        raise ValidationError(
            f"Nginx-конфигурация {path} имеет небезопасного владельца или права."
        )
    try:
        text = snapshot.data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValidationError(
            f"Nginx-конфигурация {path} не является корректным UTF-8."
        ) from error
    return text, snapshot.mode


def _read_utf8_exact(path: Path) -> str:
    # Decode bytes directly so rollback preserves CRLF configurations byte-for-byte.
    return _read_nginx_snapshot(path)[0]


def _verified_source_mode(path: Path, expected: str) -> int:
    current, mode = _read_nginx_snapshot(path)
    if current != expected:
        raise ValidationError(
            f"Файл {path} изменился после чтения; операция отменена без перезаписи."
        )
    return mode


def _rollback_step(
    errors: list[str], label: str, operation: Callable[[], object]
) -> bool:
    try:
        operation()
        return True
    except BaseException as error:  # noqa: BLE001 - continue independent compensation
        errors.append(f"{label}: {error}")
        return False


def panel_access(store: StateStore) -> PanelAccess:
    inventory = store.load_inventory()
    if inventory.role != "panel":
        raise ValidationError("Защитный URL применяется только на panel-сервере.")
    found: list[PanelAccess] = []
    for path in (Path(item) for item in inventory.nginx_files):
        if not path.is_file():
            continue
        text = _read_utf8_exact(path)
        manager = _MANAGER_MAP.search(text)
        gate = _MANAGER_GATE.search(text)
        if manager and gate:
            selected_domain = _first_server_name(text)
            cookie = f'{manager.group("name")}={manager.group("value")};'
            if cookie not in text:
                raise ValidationError("Manager cookie gate в nginx повреждён.")
            found.append(
                PanelAccess(
                    url=f'https://{selected_domain}{gate.group("path")}',
                    mode="manager-path",
                )
            )
            continue
        legacy = _LEGACY_QUERY.search(text)
        if legacy:
            selected_domain = _first_server_name(text)
            query = urllib.parse.urlencode(
                {legacy.group("name"): legacy.group("value")}
            )
            found.append(
                PanelAccess(
                    url=f"https://{selected_domain}/auth/login?{query}",
                    mode="legacy-query",
                )
            )
    if len(found) != 1:
        raise ValidationError("Не удалось однозначно определить защитный URL Panel в nginx.")
    return found[0]


def rotate_panel_access(runner: Runner, store: StateStore) -> PanelAccess:
    inventory = store.load_inventory()
    current = panel_access(store)
    if current.mode == "legacy-query":
        return _migrate_legacy_panel_access(runner, store, inventory, current)
    matches: list[tuple[Path, str, re.Match[str], re.Match[str]]] = []
    for path in (Path(item) for item in inventory.nginx_files):
        if not path.is_file():
            continue
        text = _read_utf8_exact(path)
        manager = _MANAGER_MAP.search(text)
        gate = _MANAGER_GATE.search(text)
        if manager and gate:
            matches.append((path, text, manager, gate))
    if len(matches) != 1:
        raise ValidationError("Не удалось однозначно определить manager cookie gate.")

    path, original, manager, gate = matches[0]
    old_name = manager.group("name")
    old_value = manager.group("value")
    old_path = gate.group("path")
    new_name = "rwm_" + secrets.token_hex(12)
    new_value = secrets.token_urlsafe(48)
    new_path = "/_rwm/" + secrets.token_urlsafe(36)
    updated = original.replace(f"$cookie_{old_name}", f"$cookie_{new_name}")
    updated = updated.replace(f'"{old_value}" 1;', f'"{new_value}" 1;')
    updated = updated.replace(
        f'{old_name}={old_value};',
        f'{new_name}={new_value};',
    )
    updated = updated.replace(f"location = {old_path} {{", f"location = {new_path} {{")
    if updated == original or any(
        value in updated for value in (f"$cookie_{old_name}", f"{old_name}={old_value};", old_path)
    ):
        raise ValidationError("Не удалось безопасно заменить все элементы cookie gate.")

    domain = urllib.parse.urlparse(current.url).hostname
    if not domain:
        raise ValidationError("В сохранённом URL Panel отсутствует домен.")
    result = PanelAccess(f"https://{domain}{new_path}", "manager-path")
    create_backup(runner, store, reason="pre-panel-access-rotate", retention=None)
    _apply_panel_access_update(
        runner,
        store,
        inventory,
        path,
        original,
        updated,
        result,
        operation="Ротация cookie gate",
    )
    return result


def _migrate_legacy_panel_access(
    runner: Runner,
    store: StateStore,
    inventory: Inventory,
    current: PanelAccess,
) -> PanelAccess:
    candidates: list[tuple[Path, str]] = []
    for path in (Path(item) for item in inventory.nginx_files):
        if not path.is_file():
            continue
        text = _read_utf8_exact(path)
        if _LEGACY_QUERY.search(text):
            candidates.append((path, text))
    if len(candidates) != 1:
        raise ValidationError("Не удалось однозначно определить legacy cookie gate.")
    path, original = candidates[0]
    domain = urllib.parse.urlparse(current.url).hostname
    if not domain:
        raise ValidationError("В legacy URL Panel отсутствует домен.")

    new_name = "rwm_" + secrets.token_hex(12)
    # Keep the legacy map key below nginx's default 64-byte hash bucket.
    new_value = secrets.token_urlsafe(32)
    new_path = "/_rwm/" + secrets.token_urlsafe(36)
    updated = original
    manager_map = (
        f"map $cookie_{new_name} $panel_authorized {{\n"
        "    default 0;\n"
        f'    "{new_value}" 1;\n'
        "}\n\n"
    )
    map_targets = ("auth_cookie", "auth_query", "authorized", "set_cookie_header")
    for index, target in enumerate(map_targets):
        pattern = re.compile(
            rf"(?ms)^\s*map\s+[^\n{{]+\s+\${target}\s*\{{[^{{}}]*\}}\s*"
        )
        replacement = manager_map if index == 0 else ""
        updated, count = pattern.subn(replacement, updated, count=1)
        if count != 1:
            raise ValidationError(
                f"Legacy nginx gate не содержит однозначный map ${target}; миграция остановлена."
            )
    updated, authorization_count = re.subn(
        r"\$authorized\b", "$panel_authorized", updated
    )
    if authorization_count < 1:
        raise ValidationError("В legacy nginx gate не найдено условие $authorized.")
    updated, header_count = re.subn(
        r"(?m)^\s*add_header\s+Set-Cookie\s+\$set_cookie_header\s*;\s*\n?",
        "",
        updated,
    )
    if header_count != 1:
        raise ValidationError("Legacy Set-Cookie header не найден однозначно.")

    server_name = re.search(
        rf"(?m)^\s*server_name\s+{re.escape(domain)}\s*;\s*$", updated
    )
    if server_name is None:
        raise ValidationError("Не найден server-блок домена Panel для миграции cookie.")
    next_server_name = re.search(r"(?m)^\s*server_name\s+", updated[server_name.end() :])
    boundary = (
        server_name.end() + next_server_name.start()
        if next_server_name is not None
        else len(updated)
    )
    location = re.search(
        r"(?m)^\s*location\s+/\s*\{",
        updated[server_name.end() : boundary],
    )
    if location is None:
        raise ValidationError("Не найден location / домена Panel для миграции cookie.")
    insert_at = server_name.end() + location.start()
    gate = (
        f"    location = {new_path} {{\n"
        "        access_log off;\n"
        '        add_header Cache-Control "no-store" always;\n'
        '        add_header Strict-Transport-Security "max-age=63072000; includeSubDomains" always;\n'
        "        add_header X-Content-Type-Options nosniff always;\n"
        "        add_header X-Frame-Options DENY always;\n"
        "        add_header Referrer-Policy no-referrer always;\n"
        '        add_header Permissions-Policy "camera=(), microphone=(), geolocation=()" always;\n'
        f'        add_header Set-Cookie "{new_name}={new_value}; Path=/; Max-Age=2592000; '
        'HttpOnly; Secure; SameSite=Strict" always;\n'
        "        return 302 /auth/login;\n"
        "    }\n\n"
    )
    updated = updated[:insert_at] + gate + updated[insert_at:]
    if any(
        token in updated
        for token in (
            "$auth_cookie",
            "$auth_query",
            "$authorized",
            "$set_cookie_header",
        )
    ) or _LEGACY_QUERY.search(updated):
        raise ValidationError("Не все элементы legacy cookie gate были удалены.")
    if not _MANAGER_MAP.search(updated) or not _MANAGER_GATE.search(updated):
        raise ValidationError("Новый manager cookie gate не прошёл внутреннюю проверку.")

    result = PanelAccess(f"https://{domain}{new_path}", "manager-path")
    create_backup(runner, store, reason="pre-panel-access-migrate", retention=None)
    _apply_panel_access_update(
        runner,
        store,
        inventory,
        path,
        original,
        updated,
        result,
        operation="Миграция legacy cookie gate",
    )
    return result


def _apply_panel_access_update(
    runner: Runner,
    store: StateStore,
    inventory: Inventory,
    path: Path,
    original: str,
    updated: str,
    result: PanelAccess,
    *,
    operation: str,
) -> None:
    inventory_snapshot = Inventory.from_dict(inventory.to_dict())
    inventory_file_snapshot = _snapshot_optional_file(store.paths.inventory)
    if inventory_file_snapshot is None:
        raise ValidationError("Файл инвентаризации исчез до начала операции.")
    if store.load_inventory().to_dict() != inventory_snapshot.to_dict():
        raise ValidationError(
            "Файл инвентаризации изменён внешним процессом; операция отменена."
        )
    _assert_optional_file_snapshot(
        store.paths.inventory,
        inventory_file_snapshot,
        label="Файл инвентаризации",
    )
    secrets_snapshot = _snapshot_optional_file(store.paths.secrets)
    data = _load_secrets(store)
    _assert_optional_file_snapshot(
        store.paths.secrets,
        secrets_snapshot,
        label="Файл секретов",
    )
    data["panel_access_url"] = result.url
    data["panel_cookie_mode"] = result.mode
    updated_inventory = _managed_nginx_inventory(inventory, path, updated)
    manager_inventory_snapshot = _json_snapshot(updated_inventory.to_dict())
    manager_secrets_snapshot = _json_snapshot(data)
    mode = _verified_source_mode(path, original)
    was_running = nginx_is_running(runner, inventory)
    if _verified_source_mode(path, original) != mode:
        raise ValidationError(
            f"Права nginx-конфигурации {path} изменились во время preflight."
        )
    nginx_attempted = False
    inventory_attempted = False
    secrets_attempted = False
    try:
        # The replace can succeed before a following directory fsync fails.
        nginx_attempted = True
        atomic_write_text(path, updated, mode=mode)
        _assert_nginx_snapshot(path, updated, mode)
        activate_nginx_config(runner, inventory, was_running=was_running)
        _assert_nginx_snapshot(path, updated, mode)
        _assert_optional_file_snapshot(
            store.paths.inventory,
            inventory_file_snapshot,
            label="Файл инвентаризации",
        )
        inventory_attempted = True
        store.save_inventory(updated_inventory)
        _assert_optional_file_snapshot(
            store.paths.inventory,
            manager_inventory_snapshot,
            label="Файл инвентаризации",
        )
        _assert_nginx_snapshot(path, updated, mode)
        _assert_optional_file_snapshot(
            store.paths.secrets,
            secrets_snapshot,
            label="Файл секретов",
        )
        secrets_attempted = True
        atomic_write_json(store.paths.secrets, data, mode=0o600)
        _assert_optional_file_snapshot(
            store.paths.secrets,
            manager_secrets_snapshot,
            label="Файл секретов",
        )
        _assert_nginx_snapshot(path, updated, mode)
    except BaseException as error:
        rollback_errors: list[str] = []
        nginx_restored = not nginx_attempted
        if nginx_attempted:
            nginx_restored = _rollback_step(
                rollback_errors,
                "nginx-файл",
                lambda: _restore_nginx_if_expected(
                    path,
                    original,
                    updated,
                    mode,
                ),
            )
        if nginx_attempted and nginx_restored:
            _rollback_step(
                rollback_errors,
                "nginx activation",
                lambda: activate_nginx_config(
                    runner,
                    inventory_snapshot,
                    was_running=was_running,
                ),
            )
        if inventory_attempted:
            _rollback_step(
                rollback_errors,
                "inventory",
                lambda: _restore_optional_file_if_expected(
                    store.paths.inventory,
                    inventory_file_snapshot,
                    manager_inventory_snapshot,
                    label="Файл инвентаризации",
                ),
            )
        if secrets_attempted:
            _rollback_step(
                rollback_errors,
                "secrets",
                lambda: _restore_optional_file_if_expected(
                    store.paths.secrets,
                    secrets_snapshot,
                    manager_secrets_snapshot,
                    label="Файл секретов",
                ),
            )
        if rollback_errors:
            raise TransactionError(
                f"{operation} завершилась ошибкой, rollback неполон: "
                + "; ".join(rollback_errors)
            ) from error
        raise TransactionError(f"{operation} отменена: {error}") from error


def _emergency_block(domain: str) -> str:
    if not re.fullmatch(r"[a-z0-9.-]+", domain):
        raise ValidationError("Некорректный домен Panel для аварийного proxy.")
    return f"""
{_EMERGENCY_BEGIN}
server {{
    listen 127.0.0.1:8443;
    server_name localhost;
    access_log off;

    location / {{
        proxy_http_version 1.1;
        proxy_pass http://127.0.0.1:3000;
        proxy_set_header Host {domain};
        proxy_set_header Origin https://{domain};
        proxy_set_header X-Real-IP 127.0.0.1;
        proxy_set_header X-Forwarded-For 127.0.0.1;
        proxy_set_header X-Forwarded-Proto https;
        proxy_set_header X-Forwarded-Host {domain};
        proxy_set_header X-Forwarded-Port 443;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_connect_timeout 5s;
        proxy_read_timeout 240s;
        proxy_send_timeout 240s;
        proxy_hide_header X-Powered-By;
    }}
}}
{_EMERGENCY_END}
"""


def _systemd_path(store: StateStore, path: Path) -> str:
    resolved = path.resolve()
    root = store.paths.root.resolve()
    if root != Path("/") and (resolved == root or root in resolved.parents):
        value = "/" + resolved.relative_to(root).as_posix().lstrip("/")
    else:
        value = resolved.as_posix()
    if (
        not value.startswith("/")
        or value == "/"
        or any(character.isspace() for character in value)
        or not re.fullmatch(r"/[A-Za-z0-9_./+\-]+", value)
    ):
        raise ValidationError(f"Путь {path} нельзя безопасно добавить в systemd unit.")
    return value


def _emergency_units(
    store: StateStore,
    expires_at: datetime,
    *,
    nginx_path: Path | None = None,
) -> tuple[Path, Path, str, str]:
    root = store.paths.root / "etc/systemd/system"
    service = root / _EMERGENCY_SERVICE
    timer = root / _EMERGENCY_TIMER
    writable = {
        "/var/backups/remnawave-manager",
        "/var/lib/remnawave-manager",
        "/var/log/remnawave-manager",
        "/etc/remnawave-manager",
        "/etc/systemd/system",
        "/run/remnawave-manager",
    }
    if nginx_path is not None:
        writable.add(_systemd_path(store, nginx_path.parent))
    write_directives = "\n".join(
        f"ReadWritePaths={path}" for path in sorted(writable)
    )
    service_text = f"""[Unit]
Description=Закрытие аварийного доступа Remnawave Panel
X-Remnawave-Manager=true
Wants=docker.service
After=docker.service
StartLimitIntervalSec=0

[Service]
Type=oneshot
ExecStart=/usr/local/bin/rwm security emergency-close
User=root
TimeoutStartSec=5min
Restart=on-failure
RestartSec=1min
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=read-only
ProtectSystem=strict
RuntimeDirectory=remnawave-manager
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes
{write_directives}
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
UMask=0077
"""
    calendar = expires_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    timer_text = f"""[Unit]
Description=Таймер аварийного доступа Remnawave Panel
X-Remnawave-Manager=true

[Timer]
OnCalendar={calendar}
Persistent=true
AccuracySec=1s
Unit={_EMERGENCY_SERVICE}

[Install]
WantedBy=timers.target
"""
    return service, timer, service_text, timer_text


def _read_manager_unit(path: Path) -> tuple[bytes, int] | None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValidationError(f"Не удалось проверить systemd unit {path}.") from error
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > _MAX_MANAGER_UNIT_SIZE
    ):
        raise ValidationError(
            f"Systemd unit {path} должен быть обычным файлом без symlink/hardlink."
        )
    if os.name == "posix" and (
        before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) & 0o022
    ):
        raise ValidationError(
            f"Systemd unit {path} имеет небезопасного владельца или права."
        )
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValidationError(f"Не удалось безопасно открыть systemd unit {path}.") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size > _MAX_MANAGER_UNIT_SIZE
        ):
            raise ValidationError(f"Systemd unit {path} был подменён при чтении.")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            payload = stream.read(_MAX_MANAGER_UNIT_SIZE + 1)
            after_open = os.fstat(stream.fileno())
        if len(payload) > _MAX_MANAGER_UNIT_SIZE:
            raise ValidationError(f"Systemd unit {path} слишком велик.")
        try:
            after_path = path.lstat()
        except OSError as error:
            raise ValidationError(
                f"Systemd unit {path} исчез или был подменён при чтении."
            ) from error
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(
            getattr(opened, field) != getattr(after_open, field)
            or getattr(before, field) != getattr(after_path, field)
            for field in stable_fields
        ):
            raise ValidationError(f"Systemd unit {path} изменился во время чтения.")
        try:
            text = payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as error:
            raise ValidationError(f"Systemd unit {path} не является UTF-8.") from error
        if _UNIT_MARKER not in text.splitlines():
            raise ValidationError(
                f"Unit {path} создан не менеджером; автоматическое изменение запрещено."
            )
        return payload, stat.S_IMODE(opened.st_mode)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _snapshot_units(
    paths: tuple[Path, Path],
) -> dict[Path, tuple[bytes, int] | None]:
    return {unit: _read_manager_unit(unit) for unit in paths}


def _restore_units_if_expected(
    snapshot: dict[Path, tuple[bytes, int] | None],
    manager_versions: dict[Path, tuple[bytes, int]],
    attempted: set[Path],
) -> None:
    current = {unit: _read_manager_unit(unit) for unit in snapshot}
    for unit, saved in snapshot.items():
        if _file_snapshots_equal(current[unit], saved):
            continue
        if unit not in attempted or not _file_snapshots_equal(
            current[unit], manager_versions.get(unit)
        ):
            raise TransactionError(
                f"Systemd unit {unit} изменён внешним процессом после записи "
                "менеджера; unit сохранён для ручного восстановления"
            )
    for unit, saved in snapshot.items():
        if _file_snapshots_equal(current[unit], saved):
            continue
        if saved is None:
            unit.unlink(missing_ok=True)
        else:
            atomic_write_text(unit, saved[0].decode("utf-8"), mode=saved[1])
    restored = {unit: _read_manager_unit(unit) for unit in snapshot}
    if any(
        not _file_snapshots_equal(restored[unit], saved)
        for unit, saved in snapshot.items()
    ):
        raise TransactionError(
            "Systemd units изменились во время rollback; требуется ручное восстановление."
        )


def _assert_unit_snapshot(
    path: Path,
    expected: tuple[bytes, int] | None,
) -> None:
    if not _file_snapshots_equal(_read_manager_unit(path), expected):
        raise ValidationError(
            f"Systemd unit {path} изменён внешним процессом; перезапись отменена."
        )


def _remove_units_if_unchanged(
    snapshot: dict[Path, tuple[bytes, int] | None],
) -> None:
    current = {unit: _read_manager_unit(unit) for unit in snapshot}
    if any(
        not _file_snapshots_equal(current[unit], saved)
        for unit, saved in snapshot.items()
    ):
        changed = next(
            unit
            for unit, saved in snapshot.items()
            if not _file_snapshots_equal(current[unit], saved)
        )
        raise TransactionError(
            f"Systemd unit {changed} изменён внешним процессом; автоматическое "
            "удаление отменено."
        )
    for unit, saved in snapshot.items():
        if saved is not None:
            unit.unlink(missing_ok=True)


def _unit_enablement(runner: Runner, unit: str) -> SystemdEnablement:
    enabled = runner.run(
        ["systemctl", "is-enabled", unit], check=False, timeout=30
    )
    enabled_text = enabled.stdout.strip().lower()
    supported = {
        "enabled",
        "enabled-runtime",
        "disabled",
        "masked",
        "masked-runtime",
        "static",
        "indirect",
        "not-found",
    }
    enabled_code_valid = (
        enabled_text in {"enabled", "enabled-runtime"}
        and enabled.returncode == 0
        or enabled_text not in {"enabled", "enabled-runtime", "not-found"}
        and enabled.returncode in {0, 1}
        or enabled_text == "not-found"
        and enabled.returncode in {1, 4}
    )
    if enabled_text not in supported or not enabled_code_valid:
        raise ValidationError(f"Не удалось определить состояние systemd unit {unit}.")
    return cast(SystemdEnablement, enabled_text)


def _unit_active(
    runner: Runner,
    unit: str,
    *,
    enablement: SystemdEnablement,
) -> bool:
    active = runner.run(
        ["systemctl", "is-active", unit], check=False, timeout=30
    )
    active_text = active.stdout.strip().lower()
    if active.returncode == 0 and active_text == "active":
        was_active = True
    elif (
        enablement == "not-found"
        and active.returncode in {3, 4}
        and active_text in {"inactive", "unknown"}
        or enablement != "not-found"
        and active.returncode == 3
        and active_text in {"inactive", "failed"}
    ):
        was_active = False
    else:
        raise ValidationError(f"Не удалось определить активность systemd unit {unit}.")
    return was_active


def _unit_runtime_state(
    runner: Runner, unit: str
) -> tuple[SystemdEnablement, bool]:
    enablement = _unit_enablement(runner, unit)
    return enablement, _unit_active(runner, unit, enablement=enablement)


def _restore_unit_runtime(
    runner: Runner,
    unit: str,
    state: tuple[SystemdEnablement, bool],
    *,
    unit_existed: bool,
) -> None:
    enablement, active = state
    errors: list[str] = []

    def run_step(label: str, arguments: list[str], *, check: bool = True) -> None:
        try:
            runner.run(arguments, check=check, timeout=120)
        except BaseException as error:  # noqa: BLE001 - continue independent compensation
            errors.append(f"{label}: {str(error) or type(error).__name__}")

    can_address_unit = unit_existed or active
    run_step(
        "runtime-unmask",
        ["systemctl", "unmask", "--runtime", unit],
        check=can_address_unit,
    )
    run_step(
        "persistent-unmask",
        ["systemctl", "unmask", unit],
        check=can_address_unit,
    )
    run_step(
        "active-state",
        ["systemctl", "start" if active else "stop", unit],
        check=can_address_unit,
    )
    if enablement == "enabled-runtime":
        run_step("remove-persistent-enable", ["systemctl", "disable", unit])
        run_step("runtime-enable", ["systemctl", "enable", "--runtime", unit])
    elif enablement == "enabled":
        run_step("enable", ["systemctl", "enable", unit])
    elif enablement == "not-found":
        # The restored snapshot contains no unit, so daemon-reload must make it absent.
        pass
    else:
        run_step(
            "disable",
            ["systemctl", "disable", unit],
            check=unit_existed,
        )
    if enablement == "masked-runtime":
        run_step("runtime-mask", ["systemctl", "mask", "--runtime", unit])
    elif enablement == "masked":
        run_step("mask", ["systemctl", "mask", unit])

    actual_enablement: SystemdEnablement | None = None
    actual_active: bool | None = None
    try:
        actual_enablement = _unit_enablement(runner, unit)
    except BaseException as error:  # noqa: BLE001 - verify activity independently
        errors.append(
            f"enablement verification: {str(error) or type(error).__name__}"
        )
    try:
        actual_active = _unit_active(runner, unit, enablement=enablement)
    except BaseException as error:  # noqa: BLE001 - report both verification failures
        errors.append(f"active verification: {str(error) or type(error).__name__}")
    if actual_enablement is not None and actual_enablement != enablement:
        errors.append(f"enablement {actual_enablement!r} вместо {enablement!r}")
    if actual_active is not None and actual_active != active:
        errors.append(f"active-state {actual_active!r} вместо {active!r}")
    if errors:
        raise TransactionError(
            f"Не удалось полностью восстановить systemd unit {unit}: "
            + "; ".join(errors)
        )


def _snapshot_optional_file(path: Path) -> tuple[bytes, int] | None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ValidationError(
            f"Не удалось проверить служебный файл менеджера: {path}"
        ) from error
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValidationError(f"Небезопасный служебный файл менеджера: {path}")
    snapshot = read_stable_regular_file(
        path,
        max_size=16 * 1024 * 1024,
        label="Служебный файл менеджера",
    )
    if os.name == "posix" and (
        snapshot.uid != os.geteuid() or snapshot.mode & 0o077
    ):
        raise ValidationError(
            f"Небезопасные владелец или права служебного файла менеджера: {path}"
        )
    return snapshot.data, snapshot.mode


def _restore_optional_file(path: Path, snapshot: tuple[bytes, int] | None) -> None:
    if path.exists() or path.is_symlink():
        _snapshot_optional_file(path)
    if snapshot is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write_bytes(path, snapshot[0], mode=snapshot[1])


def _json_snapshot(value: object, *, mode: int = 0o600) -> tuple[bytes, int]:
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n"
    except (TypeError, ValueError, RecursionError) as error:
        raise ValidationError("Не удалось подготовить снимок manager state.") from error
    return payload.encode("utf-8"), mode


def _file_snapshots_equal(
    left: tuple[bytes, int] | None,
    right: tuple[bytes, int] | None,
) -> bool:
    if left is None or right is None:
        return left is right
    return left[0] == right[0] and (os.name != "posix" or left[1] == right[1])


def _assert_optional_file_snapshot(
    path: Path,
    expected: tuple[bytes, int] | None,
    *,
    label: str,
) -> None:
    if not _file_snapshots_equal(_snapshot_optional_file(path), expected):
        raise ValidationError(
            f"{label} {path} изменён внешним процессом; перезапись отменена."
        )


def _restore_optional_file_if_expected(
    path: Path,
    original: tuple[bytes, int] | None,
    manager_version: tuple[bytes, int],
    *,
    label: str,
) -> None:
    current = _snapshot_optional_file(path)
    if _file_snapshots_equal(current, original):
        return
    if not _file_snapshots_equal(current, manager_version):
        raise TransactionError(
            f"{label} {path} изменён внешним процессом после записи менеджера; "
            "файл сохранён для ручного восстановления"
        )
    _restore_optional_file(path, original)
    _assert_optional_file_snapshot(path, original, label=label)


def _assert_nginx_snapshot(
    path: Path,
    expected: str,
    expected_mode: int,
) -> None:
    current, mode = _read_nginx_snapshot(path)
    if current != expected or mode != expected_mode:
        raise ValidationError(
            f"Nginx-конфигурация {path} изменена внешним процессом; "
            "перезапись отменена."
        )


def _restore_nginx_if_expected(
    path: Path,
    original: str,
    manager_version: str,
    mode: int,
) -> None:
    current, current_mode = _read_nginx_snapshot(path)
    if current == original and current_mode == mode:
        return
    if current != manager_version or current_mode != mode:
        raise TransactionError(
            f"Nginx-конфигурация {path} изменена внешним процессом после записи "
            "менеджера; файл сохранён для ручного восстановления"
        )
    atomic_write_text(path, original, mode=mode)
    _assert_nginx_snapshot(path, original, mode)


def _managed_nginx_inventory(
    inventory: Inventory,
    path: Path,
    expected: str,
) -> Inventory:
    updated = Inventory.from_dict(inventory.to_dict())
    digest = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    for item in updated.managed_files:
        if Path(item.path).resolve() == path.resolve():
            item.sha256 = digest
            break
    else:
        updated.managed_files.append(
            ManagedFile(path=str(path.resolve()), sha256=digest, kind="nginx")
        )
    return updated


def _emergency_matches(store: StateStore) -> list[tuple[Path, str]]:
    inventory = store.load_inventory()
    if inventory.role != "panel":
        raise ValidationError("Аварийный доступ применяется только на panel-сервере.")
    found: list[tuple[Path, str]] = []
    for path in (Path(item) for item in inventory.nginx_files):
        if not path.is_file():
            continue
        text = _read_utf8_exact(path)
        if _EMERGENCY_BEGIN in text or _EMERGENCY_END in text:
            if text.count(_EMERGENCY_BEGIN) != 1 or text.count(_EMERGENCY_END) != 1:
                raise ValidationError("Маркер аварийного доступа в nginx повреждён.")
            found.append((path, text))
    return found


def emergency_access_status(store: StateStore) -> EmergencyAccess:
    matches = _emergency_matches(store)
    data = _load_secrets(store)
    metadata = data.get("emergency_access")
    saved = metadata if isinstance(metadata, dict) else {}
    if not matches:
        return EmergencyAccess(False, None, None, None)
    if len(matches) != 1:
        raise ValidationError("Аварийный доступ найден более чем в одном nginx-файле.")
    return EmergencyAccess(
        True,
        "http://127.0.0.1:8443/auth/login",
        str(saved.get("expires_at")) if saved.get("expires_at") else None,
        "ssh -L 8443:127.0.0.1:8443 root@SERVER",
    )


def open_emergency_access(
    runner: Runner,
    store: StateStore,
    *,
    minutes: int = 30,
) -> EmergencyAccess:
    if not 5 <= minutes <= 120:
        raise ValidationError("Аварийный доступ можно открыть на срок от 5 до 120 минут.")
    if _emergency_matches(store):
        raise ValidationError("Аварийный доступ уже открыт; сначала закройте его.")
    inventory = store.load_inventory()
    current = panel_access(store)
    domain = urllib.parse.urlparse(current.url).hostname
    if not domain:
        raise ValidationError("Не удалось определить домен Panel.")
    candidates: list[tuple[Path, str]] = []
    for path in (Path(item) for item in inventory.nginx_files):
        if not path.is_file():
            continue
        text = _read_utf8_exact(path)
        if domain in text and ("127.0.0.1:3000" in text or "remnawave" in text.lower()):
            candidates.append((path, text))
    if len(candidates) != 1:
        raise ValidationError("Не удалось однозначно выбрать nginx-конфигурацию Panel.")
    path, original = candidates[0]
    updated = original.rstrip() + "\n" + _emergency_block(domain)
    expires = datetime.now(UTC) + timedelta(minutes=minutes)
    service, timer, service_text, timer_text = _emergency_units(
        store,
        expires,
        nginx_path=path,
    )
    units = (service, timer)
    unit_snapshot = _snapshot_units(units)
    manager_unit_versions = {
        service: (service_text.encode("utf-8"), 0o644),
        timer: (timer_text.encode("utf-8"), 0o644),
    }
    timer_state = _unit_runtime_state(runner, _EMERGENCY_TIMER)
    inventory_snapshot = Inventory.from_dict(inventory.to_dict())
    inventory_file_snapshot = _snapshot_optional_file(store.paths.inventory)
    if inventory_file_snapshot is None:
        raise ValidationError("Файл инвентаризации исчез до начала операции.")
    if store.load_inventory().to_dict() != inventory_snapshot.to_dict():
        raise ValidationError(
            "Файл инвентаризации изменён внешним процессом; операция отменена."
        )
    _assert_optional_file_snapshot(
        store.paths.inventory,
        inventory_file_snapshot,
        label="Файл инвентаризации",
    )
    secrets_snapshot = _snapshot_optional_file(store.paths.secrets)
    data = _load_secrets(store)
    _assert_optional_file_snapshot(
        store.paths.secrets,
        secrets_snapshot,
        label="Файл секретов",
    )
    data["emergency_access"] = {
        "expires_at": expires.replace(microsecond=0).isoformat(),
        "nginx_file": str(path.resolve()),
    }
    updated_inventory = _managed_nginx_inventory(inventory, path, updated)
    manager_inventory_snapshot = _json_snapshot(updated_inventory.to_dict())
    manager_secrets_snapshot = _json_snapshot(data)
    create_backup(runner, store, reason="pre-panel-emergency-open", retention=None)
    mode = _verified_source_mode(path, original)
    was_running = nginx_is_running(runner, inventory)
    unit_attempted: set[Path] = set()
    nginx_attempted = False
    inventory_attempted = False
    secrets_attempted = False
    try:
        service.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        _assert_unit_snapshot(service, unit_snapshot[service])
        unit_attempted.add(service)
        atomic_write_text(service, service_text, mode=0o644)
        _assert_unit_snapshot(service, manager_unit_versions[service])
        _assert_unit_snapshot(timer, unit_snapshot[timer])
        unit_attempted.add(timer)
        atomic_write_text(timer, timer_text, mode=0o644)
        _assert_unit_snapshot(service, manager_unit_versions[service])
        _assert_unit_snapshot(timer, manager_unit_versions[timer])
        runner.run(["systemctl", "daemon-reload"])
        runner.run(["systemctl", "enable", "--now", _EMERGENCY_TIMER])
        if _verified_source_mode(path, original) != mode:
            raise ValidationError(
                f"Права nginx-конфигурации {path} изменились во время preflight."
            )
        _assert_unit_snapshot(service, manager_unit_versions[service])
        _assert_unit_snapshot(timer, manager_unit_versions[timer])
        nginx_attempted = True
        atomic_write_text(path, updated, mode=mode)
        _assert_nginx_snapshot(path, updated, mode)
        activate_nginx_config(runner, inventory, was_running=was_running)
        _assert_nginx_snapshot(path, updated, mode)
        _assert_optional_file_snapshot(
            store.paths.inventory,
            inventory_file_snapshot,
            label="Файл инвентаризации",
        )
        inventory_attempted = True
        store.save_inventory(updated_inventory)
        _assert_optional_file_snapshot(
            store.paths.inventory,
            manager_inventory_snapshot,
            label="Файл инвентаризации",
        )
        _assert_nginx_snapshot(path, updated, mode)
        _assert_optional_file_snapshot(
            store.paths.secrets,
            secrets_snapshot,
            label="Файл секретов",
        )
        secrets_attempted = True
        atomic_write_json(store.paths.secrets, data, mode=0o600)
        _assert_optional_file_snapshot(
            store.paths.secrets,
            manager_secrets_snapshot,
            label="Файл секретов",
        )
        _assert_nginx_snapshot(path, updated, mode)
    except BaseException as error:
        rollback_errors: list[str] = []
        _rollback_step(
            rollback_errors,
            "остановка временного timer",
            lambda: runner.run(
                ["systemctl", "disable", "--now", _EMERGENCY_TIMER],
                check=False,
            ),
        )
        nginx_restored = not nginx_attempted
        if nginx_attempted:
            nginx_restored = _rollback_step(
                rollback_errors,
                "nginx-файл",
                lambda: _restore_nginx_if_expected(
                    path,
                    original,
                    updated,
                    mode,
                ),
            )
        if nginx_attempted and nginx_restored:
            _rollback_step(
                rollback_errors,
                "nginx activation",
                lambda: activate_nginx_config(
                    runner,
                    inventory_snapshot,
                    was_running=was_running,
                ),
            )
        if inventory_attempted:
            _rollback_step(
                rollback_errors,
                "inventory",
                lambda: _restore_optional_file_if_expected(
                    store.paths.inventory,
                    inventory_file_snapshot,
                    manager_inventory_snapshot,
                    label="Файл инвентаризации",
                ),
            )
        if secrets_attempted:
            _rollback_step(
                rollback_errors,
                "secrets",
                lambda: _restore_optional_file_if_expected(
                    store.paths.secrets,
                    secrets_snapshot,
                    manager_secrets_snapshot,
                    label="Файл секретов",
                ),
            )
        units_restored = _rollback_step(
            rollback_errors,
            "systemd units",
            lambda: _restore_units_if_expected(
                unit_snapshot,
                manager_unit_versions,
                unit_attempted,
            ),
        )
        if units_restored:
            daemon_reloaded = _rollback_step(
                rollback_errors,
                "systemd daemon-reload",
                lambda: runner.run(["systemctl", "daemon-reload"]),
            )
            if daemon_reloaded:
                _rollback_step(
                    rollback_errors,
                    "systemd runtime",
                    lambda: _restore_unit_runtime(
                        runner,
                        _EMERGENCY_TIMER,
                        timer_state,
                        unit_existed=unit_snapshot[timer] is not None,
                    ),
                )
        if rollback_errors:
            raise TransactionError(
                "Аварийный доступ не открыт, rollback неполон: "
                + "; ".join(rollback_errors)
            ) from error
        raise TransactionError(f"Аварийный доступ не открыт: {error}") from error
    return emergency_access_status(store)


def close_emergency_access(runner: Runner, store: StateStore) -> None:
    matches = _emergency_matches(store)
    if len(matches) > 1:
        raise ValidationError("Аварийный доступ найден более чем в одном nginx-файле.")
    inventory = store.load_inventory()
    inventory_snapshot = Inventory.from_dict(inventory.to_dict())
    changed: Path | None = None
    service, timer, _, _ = _emergency_units(store, datetime.now(UTC))
    cleanup_unit_snapshot = _snapshot_units((service, timer))
    if matches:
        path, original = matches[0]
        pattern = re.compile(
            r"\n?" + re.escape(_EMERGENCY_BEGIN) + r"\n.*?\n" + re.escape(_EMERGENCY_END) + r"\n?",
            re.DOTALL,
        )
        updated, count = pattern.subn("\n", original)
        if count != 1:
            raise ValidationError("Не удалось безопасно удалить аварийный nginx-блок.")
        mode = _verified_source_mode(path, original)
        was_running = nginx_is_running(runner, inventory)
        inventory_file_snapshot = _snapshot_optional_file(store.paths.inventory)
        if inventory_file_snapshot is None:
            raise ValidationError("Файл инвентаризации исчез до начала операции.")
        if store.load_inventory().to_dict() != inventory_snapshot.to_dict():
            raise ValidationError(
                "Файл инвентаризации изменён внешним процессом; операция отменена."
            )
        _assert_optional_file_snapshot(
            store.paths.inventory,
            inventory_file_snapshot,
            label="Файл инвентаризации",
        )
        closed = updated.rstrip() + "\n"
        updated_inventory = _managed_nginx_inventory(inventory, path, closed)
        manager_inventory_snapshot = _json_snapshot(updated_inventory.to_dict())
        nginx_attempted = False
        inventory_attempted = False
        try:
            if _verified_source_mode(path, original) != mode:
                raise ValidationError(
                    f"Права nginx-конфигурации {path} изменились во время preflight."
                )
            nginx_attempted = True
            atomic_write_text(path, closed, mode=mode)
            _assert_nginx_snapshot(path, closed, mode)
            activate_nginx_config(runner, inventory, was_running=was_running)
            _assert_nginx_snapshot(path, closed, mode)
            _assert_optional_file_snapshot(
                store.paths.inventory,
                inventory_file_snapshot,
                label="Файл инвентаризации",
            )
            inventory_attempted = True
            store.save_inventory(updated_inventory)
            _assert_optional_file_snapshot(
                store.paths.inventory,
                manager_inventory_snapshot,
                label="Файл инвентаризации",
            )
            _assert_nginx_snapshot(path, closed, mode)
        except BaseException as error:
            rollback_errors: list[str] = []
            nginx_restored = not nginx_attempted
            if nginx_attempted:
                nginx_restored = _rollback_step(
                    rollback_errors,
                    "nginx-файл",
                    lambda: _restore_nginx_if_expected(
                        path,
                        original,
                        closed,
                        mode,
                    ),
                )
            if inventory_attempted:
                _rollback_step(
                    rollback_errors,
                    "inventory",
                    lambda: _restore_optional_file_if_expected(
                        store.paths.inventory,
                        inventory_file_snapshot,
                        manager_inventory_snapshot,
                        label="Файл инвентаризации",
                    ),
                )
            if nginx_attempted and nginx_restored:
                _rollback_step(
                    rollback_errors,
                    "nginx activation",
                    lambda: activate_nginx_config(
                        runner,
                        inventory_snapshot,
                        was_running=was_running,
                    ),
                )
            if rollback_errors:
                raise TransactionError(
                    "Аварийный доступ не закрыт, rollback nginx/inventory неполон: "
                    + "; ".join(rollback_errors)
                    + f". Исходная ошибка: {error}"
                ) from error
            raise TransactionError(
                f"Аварийный доступ не закрыт из-за ошибки nginx: {error}"
            ) from error
        changed = path
    cleanup_errors: list[str] = []
    try:
        secrets_snapshot = _snapshot_optional_file(store.paths.secrets)
        data = _load_secrets(store)
        _assert_optional_file_snapshot(
            store.paths.secrets,
            secrets_snapshot,
            label="Файл секретов",
        )
        data.pop("emergency_access", None)
        manager_secrets_snapshot = _json_snapshot(data)
        _assert_optional_file_snapshot(
            store.paths.secrets,
            secrets_snapshot,
            label="Файл секретов",
        )
        atomic_write_json(store.paths.secrets, data, mode=0o600)
        _assert_optional_file_snapshot(
            store.paths.secrets,
            manager_secrets_snapshot,
            label="Файл секретов",
        )
    except BaseException as error:  # noqa: BLE001 - access is already closed; report cleanup interruption
        cleanup_errors.append(f"secrets: {error}")
    try:
        runner.run(["systemctl", "disable", "--now", _EMERGENCY_TIMER], check=False)
        enablement, active = _unit_runtime_state(runner, _EMERGENCY_TIMER)
        if active or enablement in {"enabled", "enabled-runtime", "masked", "masked-runtime"}:
            raise TransactionError("Таймер аварийного доступа остался enabled или active.")
        _remove_units_if_unchanged(cleanup_unit_snapshot)
        runner.run(["systemctl", "daemon-reload"])
    except BaseException as error:  # noqa: BLE001 - access is already closed; report cleanup interruption
        cleanup_errors.append(f"systemd: {error}")
    if cleanup_errors:
        suffix = f" (nginx: {changed})" if changed else ""
        raise TransactionError(
            "Аварийный nginx-доступ закрыт, но служебная очистка неполна"
            + suffix
            + ": "
            + "; ".join(cleanup_errors)
        )


def _load_secrets(store: StateStore) -> dict[str, object]:
    return store.load_secrets()


def _first_server_name(text: str) -> str:
    for match in re.finditer(r"(?m)^\s*server_name\s+([^;\s]+)", text):
        candidate = match.group(1).lower()
        if candidate != "_" and re.fullmatch(r"[a-z0-9.-]+", candidate):
            return candidate
    raise ValidationError("Не удалось определить домен Panel из nginx.")
