from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import ssl
import stat
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .backup import create_backup
from .compose import inspect_compose
from .errors import TransactionError, ValidationError
from .integrity import configuration_drift
from .models import Inventory
from .paths import RuntimePaths
from .runner import (
    Runner,
    atomic_copy,
    atomic_write_bytes,
    atomic_write_json,
    atomic_write_text,
    ensure_within,
    sanitize_external_text,
    sha256_file,
)
from .state import StateStore, utc_now
from .warp_config import WARP_IPV4_ROUTE_METRIC, WarpProfile, load_warp_profile
from .warp_download import install_wgcf, wgcf_contract, wgcf_notice_path

_UNIT_MARKER = "X-Remnawave-Manager=true"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_WGCF_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){2}(?:[-+][0-9A-Za-z.-]+)?")
_CLOUDFLARE_DEVICE_RE = re.compile(r"[0-9A-Fa-f-]{16,64}")
_CLOUDFLARE_API = "https://api.cloudflareclient.com/v0a1922"
_PENDING_STAGING_RE = re.compile(r"\.(?:staging|rotate)-[0-9a-f]{32}")
_ENABLED_UNIT_STATES = {
    "alias",
    "enabled",
    "enabled-runtime",
    "linked",
    "linked-runtime",
}
_DISABLED_UNIT_STATES = {
    "disabled",
    "generated",
    "indirect",
    "masked",
    "masked-runtime",
    "not-found",
    "static",
    "transient",
}
_EXTERNAL_ENVIRONMENT_KEYS = {
    "ALL_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "NO_PROXY",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "all_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
}


class _SystemdQueryError(TransactionError):
    pass


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise TransactionError(
            "Cloudflare API попытался перенаправить запрос отзыва WARP device."
        )


@dataclass(slots=True)
class WarpScan:
    config: str | None
    account: str | None
    interface_exists: bool
    unit_active: bool
    manager_state: bool
    legacy_paths: list[str] = field(default_factory=list)
    other_wireguard_configs: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    safe_takeover: bool = False


@dataclass(frozen=True, slots=True)
class WarpPaths:
    runtime: RuntimePaths

    @property
    def config(self) -> Path:
        return self.runtime.root / "etc/wireguard/warp.conf"

    @property
    def data(self) -> Path:
        return self.runtime.state / "warp"

    @property
    def account(self) -> Path:
        return self.data / "account.toml"

    @property
    def state(self) -> Path:
        return self.data / "state.json"

    @property
    def bin_dir(self) -> Path:
        return self.runtime.root / "usr/lib/remnawave-manager/bin"

    @property
    def health_service(self) -> Path:
        return self.runtime.root / "etc/systemd/system/remnawave-warp-health.service"

    @property
    def health_timer(self) -> Path:
        return self.runtime.root / "etc/systemd/system/remnawave-warp-health.timer"


def _pending_staging(paths: WarpPaths) -> list[Path]:
    if not paths.data.is_dir() or paths.data.is_symlink():
        return []
    try:
        return sorted(
            (
                item
                for item in paths.data.iterdir()
                if _PENDING_STAGING_RE.fullmatch(item.name) is not None
            ),
            key=str,
        )
    except OSError as error:
        raise ValidationError(
            f"Не удалось проверить незавершённые WARP-операции в {paths.data}."
        ) from error


def _systemctl(runner: Runner, *arguments: str, check: bool = True) -> None:
    runner.run(["systemctl", *arguments], check=check)


def _systemctl_query(runner: Runner, action: str, unit: str) -> tuple[int, str, str]:
    environment = dict(os.environ)
    environment.update({"LANG": "C", "LC_ALL": "C"})
    result = runner.run(
        ["systemctl", action, unit],
        check=False,
        timeout=30,
        env=environment,
    )
    return result.returncode, result.stdout.strip().lower(), result.stderr.strip().lower()


def _is_active(runner: Runner, unit: str) -> bool:
    returncode, value, _ = _systemctl_query(runner, "is-active", unit)
    if returncode == 0 and value in {"active", "reloading", "refreshing"}:
        return True
    if returncode in {3, 4} and value in {
        "inactive",
        "failed",
        "unknown",
    }:
        return False
    raise _SystemdQueryError(
        f"Не удалось однозначно определить активность systemd unit {unit}."
    )


def _unit_enablement(runner: Runner, unit: str) -> str:
    returncode, value, detail = _systemctl_query(runner, "is-enabled", unit)
    if returncode == 0 and value in _ENABLED_UNIT_STATES:
        return value
    if value in _DISABLED_UNIT_STATES:
        return value
    if returncode != 0 and (
        "no such file or directory" in detail or "not found" in detail
    ):
        return "not-found"
    raise _SystemdQueryError(
        f"Не удалось однозначно определить автозапуск systemd unit {unit}."
    )


def _is_enabled(runner: Runner, unit: str) -> bool:
    return _unit_enablement(runner, unit) in _ENABLED_UNIT_STATES


def _restorable_unit_enablement(runner: Runner, unit: str) -> str:
    enablement = _unit_enablement(runner, unit)
    if enablement in {"linked", "linked-runtime", "alias"}:
        raise ValidationError(
            f"Systemd unit {unit} имеет состояние {enablement!r}; безопасная "
            "WARP-транзакция не может восстановить неизвестный link target."
        )
    return enablement


def scan_warp(runner: Runner, runtime: RuntimePaths) -> WarpScan:
    paths = WarpPaths(runtime)
    wireguard_dir = runtime.root / "etc/wireguard"
    conflicts: list[str] = []
    manager_state = False
    if paths.state.is_symlink() or (paths.state.exists() and not paths.state.is_file()):
        conflicts.append(f"Состояние WARP имеет небезопасный тип: {paths.state}")
    elif paths.state.is_file():
        manager_state = True
    if wireguard_dir.is_symlink():
        conflicts.append(f"Каталог WireGuard является символической ссылкой: {wireguard_dir}")
        configs: list[Path] = []
    elif wireguard_dir.exists() and not wireguard_dir.is_dir():
        conflicts.append(f"Путь WireGuard не является каталогом: {wireguard_dir}")
        configs = []
    else:
        configs = (
            sorted(wireguard_dir.glob("*.conf"), key=str)
            if wireguard_dir.is_dir()
            else []
        )
        if wireguard_dir.is_dir() and os.name == "posix":
            metadata = wireguard_dir.stat()
            if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
                conflicts.append(
                    f"Каталог WireGuard {wireguard_dir} должен принадлежать root "
                    "и не быть доступен для записи группе или остальным."
                )
    if paths.data.is_symlink() or (paths.data.exists() and not paths.data.is_dir()):
        conflicts.append(f"Каталог состояния WARP имеет небезопасный тип: {paths.data}")
    elif paths.data.is_dir() and os.name == "posix":
        metadata = paths.data.stat()
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            conflicts.append(
                f"Каталог состояния WARP {paths.data} должен принадлежать root и иметь права 0700."
            )
    pending = _pending_staging(paths)
    if pending:
        conflicts.append(
            "Найдены credentials незавершённой WARP-операции; требуется ручной recovery: "
            + ", ".join(str(path) for path in pending)
        )
    config: Path | None = None
    if paths.config.is_symlink() or (paths.config.exists() and not paths.config.is_file()):
        conflicts.append(f"WARP-конфиг имеет небезопасный тип: {paths.config}")
    elif paths.config.is_file():
        config = paths.config
    account_candidates = [
        paths.account,
        runtime.root / "root/wgcf-account.toml",
        runtime.root / "opt/warp-native/wgcf-account.toml",
    ]
    accounts: list[Path] = []
    for candidate in account_candidates:
        if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
            conflicts.append(f"WARP account имеет небезопасный тип: {candidate}")
        elif candidate.is_file():
            try:
                _read_regular_file(candidate, require_owner=True)
            except ValidationError as error:
                conflicts.append(str(error))
            else:
                accounts.append(candidate)
    legacy_accounts: list[Path] = []
    if manager_state and paths.account in accounts:
        account = paths.account
        legacy_accounts = [item for item in accounts if item != paths.account]
    elif manager_state:
        account = None
        conflicts.append(
            f"WARP управляется менеджером, но account-файл отсутствует: {paths.account}"
        )
    elif len(accounts) > 1:
        conflicts.append(
            "Найдено несколько WARP account файлов; автоматический takeover неоднозначен: "
            + ", ".join(str(path) for path in accounts)
        )
        account = accounts[0]
    else:
        account = accounts[0] if accounts else None
    legacy_candidates = [
        runtime.root / "opt/warp-native",
        runtime.root / "etc/cron.d/warp-native",
        runtime.root / "usr/local/bin/wgcf",
        runtime.root / "usr/local/bin/warp",
    ]
    legacy = [
        str(path)
        for path in [*legacy_candidates, *legacy_accounts]
        if path.exists() or path.is_symlink()
    ]
    official_active = _is_active(runner, "warp-svc.service")
    official_enabled = _is_enabled(runner, "warp-svc.service")
    unit_active = _is_active(runner, "wg-quick@warp.service")
    unit_enabled = _is_enabled(runner, "wg-quick@warp.service")
    health_active = _is_active(runner, "remnawave-warp-health.timer")
    health_enabled = _is_enabled(runner, "remnawave-warp-health.timer")
    if official_active or official_enabled:
        conflicts.append(
            "Официальный warp-svc активен или включён в автозапуск; "
            "одновременное управление запрещено."
        )
    if config is not None:
        try:
            load_warp_profile(config)
        except ValidationError as error:
            conflicts.append(str(error))
    interface_exists = (runtime.root / "sys/class/net/warp").exists()
    if config is None and (interface_exists or unit_active or unit_enabled):
        conflicts.append(
            "Интерфейс warp или wg-quick@warp.service активен/включён, "
            "но безопасный /etc/wireguard/warp.conf не найден."
        )
    health_paths_present = any(
        path.exists() or path.is_symlink()
        for path in (paths.health_service, paths.health_timer)
    )
    if not manager_state and (health_paths_present or health_active or health_enabled):
        conflicts.append(
            "Найдены WARP health units без manager state; требуется ручной recovery."
        )
    return WarpScan(
        config=str(config) if config else None,
        account=str(account) if account else None,
        interface_exists=interface_exists,
        unit_active=unit_active,
        manager_state=manager_state,
        legacy_paths=legacy,
        other_wireguard_configs=[str(path) for path in configs if path.name != "warp.conf"],
        conflicts=conflicts,
        safe_takeover=config is not None and not manager_state and not conflicts,
    )


def _assert_node_contract(runner: Runner, inventory: Inventory) -> None:
    if inventory.role != "node" or "node" not in inventory.components:
        raise ValidationError("WARP можно устанавливать только на отдельном node-сервере.")
    drift = configuration_drift(inventory)
    if drift:
        raise ValidationError(
            "Конфигурация Node изменилась после adoption; повторите rwm adopt перед WARP: "
            + "; ".join(drift[:16])
        )
    compose_file = Path(inventory.compose_file)
    env_file = Path(inventory.env_file) if inventory.env_file else None
    compose = inspect_compose(runner, compose_file, env_file)
    services = compose.get("services")
    service_name = inventory.components["node"].service
    if not isinstance(services, dict) or not isinstance(services.get(service_name), dict):
        raise ValidationError(
            f"В текущем Compose не найден сервис Node {service_name}; повторите rwm adopt."
        )
    service = services[service_name]
    if service.get("network_mode") != "host":
        raise ValidationError("Для WARP Node должна использовать network_mode: host.")
    capabilities = {str(value).upper() for value in service.get("cap_add", []) or []}
    dropped = {str(value).upper() for value in service.get("cap_drop", []) or []}
    if dropped & {"ALL", "NET_RAW"} and "NET_RAW" not in capabilities:
        raise ValidationError("Для WARP Node должна сохранять capability NET_RAW.")


def _normalized_json(result: str) -> str:
    try:
        value = json.loads(result)
    except json.JSONDecodeError:
        return result.strip()

    def clean(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: clean(current) for key, current in sorted(item.items()) if key not in {"packets", "bytes"}}
        if isinstance(item, list):
            return [clean(current) for current in item]
        return item

    return json.dumps(clean(value), sort_keys=True, separators=(",", ":"))


def _normalized_ipv4_default_routes(result: str) -> str:
    try:
        routes = json.loads(result)
    except json.JSONDecodeError:
        return _normalized_json(result)
    if not isinstance(routes, list):
        return _normalized_json(result)
    filtered = [
        route
        for route in routes
        if not (
            isinstance(route, dict)
            and route.get("dst") == "default"
            and route.get("dev") == "warp"
            and route.get("metric") == WARP_IPV4_ROUTE_METRIC
            and "gateway" not in route
        )
    ]
    return _normalized_json(json.dumps(filtered))


def _invariants(runner: Runner, runtime: RuntimePaths) -> dict[str, str]:
    commands = {
        "route4": ["ip", "-j", "route", "show", "default"],
        "route6": ["ip", "-j", "-6", "route", "show", "default"],
        "rule4": ["ip", "-j", "rule", "show"],
        "rule6": ["ip", "-j", "-6", "rule", "show"],
        "nft": ["nft", "-j", "list", "ruleset"],
    }
    values: dict[str, str] = {}
    for key, command in commands.items():
        result = runner.run(command, check=False)
        if result.returncode != 0:
            raise TransactionError(
                f"Не удалось получить системный invariant {key}; WARP-операция остановлена."
            )
        values[key] = (
            _normalized_ipv4_default_routes(result.stdout)
            if key == "route4"
            else _normalized_json(result.stdout)
        )
    for name in ("etc/resolv.conf", "etc/ufw/user.rules", "etc/ufw/user6.rules"):
        path = runtime.root / name
        values[name] = _invariant_file(path, allow_symlink=name == "etc/resolv.conf")
    return values


def _invariant_file(path: Path, *, allow_symlink: bool) -> str:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return "<отсутствует>"
    except OSError as error:
        raise TransactionError(f"Не удалось проверить invariant {path}: {error}") from error
    if stat.S_ISLNK(metadata.st_mode):
        if not allow_symlink:
            raise ValidationError(f"Символическая ссылка недопустима для invariant {path}.")
        try:
            target = os.readlink(path)
            resolved = path.resolve(strict=True)
            payload, _ = _read_regular_file(resolved, max_size=16 * 1024 * 1024)
            digest = hashlib.sha256(payload).hexdigest()
            if os.readlink(path) != target:
                raise TransactionError(f"Символическая ссылка {path} изменилась во время проверки.")
        except (OSError, RuntimeError) as error:
            raise TransactionError(f"Не удалось безопасно прочитать invariant {path}.") from error
        return f"symlink:{target}:{digest}"
    if not stat.S_ISREG(metadata.st_mode):
        raise ValidationError(f"Invariant {path} не является обычным файлом.")
    payload, _ = _read_regular_file(path, max_size=16 * 1024 * 1024)
    return hashlib.sha256(payload).hexdigest()


def _assert_invariants(before: dict[str, str], after: dict[str, str]) -> None:
    changed = [key for key, value in before.items() if after.get(key) != value]
    if changed:
        raise TransactionError(
            "WARP попытался изменить системные маршруты/rules/resolver/firewall: " + ", ".join(changed)
        )


def _health_units() -> tuple[str, str]:
    service = """[Unit]
Description=Проверка Cloudflare WARP для Remnawave Manager
After=network-online.target wg-quick@warp.service
Wants=network-online.target
X-Remnawave-Manager=true

[Service]
Type=oneshot
ExecStart=/usr/local/bin/rwm warp watchdog
User=root
UMask=0077
TimeoutStartSec=3min
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectSystem=strict
RuntimeDirectory=remnawave-manager
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes
ReadWritePaths=/var/lib/remnawave-manager/warp /run/remnawave-manager
ProtectClock=true
ProtectHostname=true
ProtectKernelLogs=true
ProtectKernelModules=true
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
LockPersonality=true
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW
AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW
"""
    timer = """[Unit]
Description=Периодическая проверка Cloudflare WARP
X-Remnawave-Manager=true

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
RandomizedDelaySec=30s
Persistent=true

[Install]
WantedBy=timers.target
"""
    return service, timer


def _assert_unit_ownership(paths: WarpPaths) -> None:
    for path in (paths.health_service, paths.health_timer):
        for directory in (
            "run/systemd/system",
            "usr/local/lib/systemd/system",
            "usr/lib/systemd/system",
            "lib/systemd/system",
        ):
            candidate = paths.runtime.root / directory / path.name
            if candidate.exists() or candidate.is_symlink():
                raise ValidationError(
                    f"Systemd unit {path.name} уже существует вне управляемого пути: "
                    f"{candidate}"
                )
        for directory in (
            "etc/systemd/system",
            "run/systemd/system",
            "usr/local/lib/systemd/system",
            "usr/lib/systemd/system",
            "lib/systemd/system",
        ):
            drop_in = paths.runtime.root / directory / f"{path.name}.d"
            if drop_in.exists() or drop_in.is_symlink():
                raise ValidationError(
                    f"Для systemd unit {path.name} найден чужой drop-in: {drop_in}"
                )
        if path.is_symlink():
            raise ValidationError(f"Unit {path} является символьной ссылкой; изменение запрещено.")
        if path.exists() and not path.is_file():
            raise ValidationError(f"Путь unit {path} не является обычным файлом.")
        if path.is_file():
            payload, _ = _read_regular_file(
                path,
                max_size=64 * 1024,
                require_owner=True,
            )
            if _UNIT_MARKER not in {
                line.strip() for line in payload.decode("utf-8", "replace").splitlines()
            }:
                raise ValidationError(
                    f"Unit {path} создан не менеджером; автоматическая перезапись запрещена."
                )


def _install_units(runner: Runner, paths: WarpPaths) -> None:
    _assert_unit_ownership(paths)
    service, timer = _health_units()
    atomic_write_text(paths.health_service, service, mode=0o644)
    atomic_write_text(paths.health_timer, timer, mode=0o644)
    _systemctl(runner, "daemon-reload")


def _legacy_cron_paths(runtime: RuntimePaths) -> tuple[Path, Path]:
    cron = runtime.root / "etc/cron.d/warp-native"
    return cron, cron.with_name("warp-native.disabled-by-remnawave-manager")


def _assert_legacy_cron_move(cron: Path, target: Path) -> None:
    try:
        metadata = cron.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise ValidationError(f"Не удалось проверить legacy cron {cron}: {error}") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ValidationError(f"Legacy cron {cron} не является обычным отдельным файлом.")
    if os.name == "posix" and (
        metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022
    ):
        raise ValidationError(
            f"Legacy cron {cron} имеет небезопасные владельца или права."
        )
    if target.exists() or target.is_symlink():
        raise ValidationError(
            f"Файл {target} уже существует; отключение legacy cron остановлено."
        )


def _disable_legacy_cron(cron: Path, target: Path) -> dict[str, str]:
    _assert_legacy_cron_move(cron, target)
    try:
        before = cron.lstat()
    except FileNotFoundError:
        return {}
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValidationError(f"Legacy cron {cron} был подменён до отключения.")
    try:
        os.link(cron, target, follow_symlinks=False)
    except FileExistsError as error:
        raise ValidationError(
            f"Файл {target} появился параллельно; legacy cron не изменён."
        ) from error
    except OSError as error:
        raise TransactionError(f"Не удалось безопасно отключить legacy cron {cron}: {error}") from error
    try:
        source = cron.lstat()
        linked = target.lstat()
        if (
            not stat.S_ISREG(source.st_mode)
            or not stat.S_ISREG(linked.st_mode)
            or (source.st_dev, source.st_ino) != (before.st_dev, before.st_ino)
            or (linked.st_dev, linked.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise TransactionError(
                f"Legacy cron {cron} был подменён во время отключения."
            )
        cron.unlink()
    except OSError as error:
        try:
            linked = target.lstat()
            if (linked.st_dev, linked.st_ino) == (before.st_dev, before.st_ino):
                target.unlink()
        except OSError:
            pass
        raise TransactionError(f"Не удалось удалить активный legacy cron {cron}: {error}") from error
    except TransactionError:
        try:
            linked = target.lstat()
            if (linked.st_dev, linked.st_ino) == (before.st_dev, before.st_ino):
                target.unlink()
        except OSError:
            pass
        raise
    return {str(cron): str(target)}


def _restore_legacy_cron(cron: Path, target: Path) -> None:
    if cron.exists() or cron.is_symlink():
        raise TransactionError(
            f"Нельзя восстановить legacy cron: путь {cron} уже занят."
        )
    if target.is_symlink() or not target.is_file():
        raise TransactionError(
            f"Нельзя восстановить legacy cron: резервный файл {target} недоступен."
        )
    before = target.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise TransactionError(
            f"Нельзя восстановить legacy cron: резервный файл {target} небезопасен."
        )
    try:
        os.link(target, cron, follow_symlinks=False)
    except FileExistsError as error:
        raise TransactionError(
            f"Путь {cron} появился параллельно; legacy cron не восстановлен."
        ) from error
    except OSError as error:
        raise TransactionError(
            f"Не удалось восстановить legacy cron {cron}: {error}"
        ) from error
    try:
        source = cron.lstat()
        linked = target.lstat()
        if (
            not stat.S_ISREG(source.st_mode)
            or not stat.S_ISREG(linked.st_mode)
            or (source.st_dev, source.st_ino) != (before.st_dev, before.st_ino)
            or (linked.st_dev, linked.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise TransactionError(
                f"Legacy cron {target} был подменён во время восстановления."
            )
        target.unlink()
    except OSError as error:
        try:
            source = cron.lstat()
            if (source.st_dev, source.st_ino) == (before.st_dev, before.st_ino):
                cron.unlink()
        except OSError:
            pass
        raise TransactionError(
            f"Legacy cron восстановлен, но не удалось удалить {target}: {error}"
        ) from error
    except TransactionError:
        try:
            source = cron.lstat()
            if (source.st_dev, source.st_ino) == (before.st_dev, before.st_ino):
                cron.unlink()
        except OSError:
            pass
        raise


def _trace_via_warp(timeout: float = 8.0) -> str:
    request = (
        b"GET /cdn-cgi/trace HTTP/1.1\r\n"
        b"Host: www.cloudflare.com\r\n"
        b"User-Agent: remnawave-manager/0.1\r\n"
        b"Connection: close\r\n\r\n"
    )
    context = ssl.create_default_context()
    errors: list[str] = []
    try:
        addresses = socket.getaddrinfo(
            "www.cloudflare.com",
            443,
            family=socket.AF_INET,
            type=socket.SOCK_STREAM,
        )
    except OSError as error:
        raise TransactionError(
            f"Не удалось разрешить IPv4-адрес www.cloudflare.com: {error}"
        ) from error
    for family, socktype, proto, _, address in addresses:
        raw = socket.socket(family, socktype, proto)
        raw.settimeout(timeout)
        try:
            if not hasattr(socket, "SO_BINDTODEVICE"):
                raise ValidationError("Ядро/Python не поддерживает SO_BINDTODEVICE.")
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"warp\0")
            raw.connect(address)
            with context.wrap_socket(raw, server_hostname="www.cloudflare.com") as tls:
                tls.sendall(request)
                chunks: list[bytes] = []
                total = 0
                limit = 128 * 1024
                while total <= limit:
                    block = tls.recv(min(16384, limit - total + 1))
                    if not block:
                        break
                    chunks.append(block)
                    total += len(block)
                if total > limit:
                    raise TransactionError("Cloudflare trace превысил допустимый размер.")
            payload = b"".join(chunks).decode("utf-8", "replace")
            if " 200 " not in payload.split("\r\n", 1)[0]:
                raise TransactionError("Cloudflare trace вернул не-200 ответ.")
            for line in payload.splitlines():
                if line.startswith("warp="):
                    value = line.split("=", 1)[1].strip().lower()
                    if value in {"on", "plus"}:
                        return value
                    raise TransactionError(f"Cloudflare trace сообщил warp={value}.")
            raise TransactionError("В Cloudflare trace отсутствует поле warp.")
        except ValidationError:
            raise
        except (OSError, ssl.SSLError, TransactionError) as error:
            errors.append(str(error))
        finally:
            raw.close()
    raise TransactionError("TLS-проверка через интерфейс warp не удалась: " + "; ".join(errors[-3:]))


def _latest_handshake(runner: Runner) -> int:
    result = runner.run(["wg", "show", "warp", "latest-handshakes"], check=False)
    if result.returncode != 0:
        return 0
    timestamps: list[int] = []
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[1].isdigit():
            timestamps.append(int(fields[1]))
    return max(timestamps, default=0)


def _verify_container_visibility(runner: Runner, inventory: Inventory) -> None:
    node = inventory.components["node"]
    container = node.container or node.service
    result = runner.run(
        ["docker", "exec", container, "test", "-d", "/sys/class/net/warp"],
        check=False,
    )
    if result.returncode != 0:
        raise TransactionError("Контейнер Node не видит интерфейс warp.")


def _read_regular_file(
    path: Path,
    *,
    max_size: int = 1024 * 1024,
    require_owner: bool = False,
) -> tuple[bytes, int]:
    try:
        before = path.lstat()
    except OSError as error:
        raise ValidationError(f"Не удалось проверить WARP-файл {path}: {error}") from error
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValidationError(f"Небезопасный тип WARP-файла: {path}")
    if before.st_size < 0 or before.st_size > max_size:
        raise ValidationError(f"WARP-файл {path} превышает {max_size} байт.")
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValidationError(f"Не удалось безопасно открыть WARP-файл {path}: {error}") from error
    try:
        current = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ValidationError(f"WARP-файл {path} был подменён во время проверки.")
        if (
            require_owner
            and os.name == "posix"
            and (current.st_uid != os.geteuid() or current.st_mode & 0o022)
        ):
            raise ValidationError(
                f"WARP-файл {path} имеет небезопасные владельца или права."
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(max_size + 1)
        after = os.fstat(descriptor)
    except ValidationError:
        raise
    except OSError as error:
        raise ValidationError(f"Не удалось безопасно прочитать WARP-файл {path}.") from error
    finally:
        os.close(descriptor)
    if (
        len(payload) != current.st_size
        or len(payload) > max_size
        or (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_nlink,
        )
        != (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
            current.st_ctime_ns,
            current.st_nlink,
        )
    ):
        raise ValidationError(f"WARP-файл {path} изменился во время чтения.")
    return payload, current.st_mode & 0o777


def _assert_private_data_directory(paths: WarpPaths, *, create: bool) -> None:
    if create:
        try:
            paths.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as error:
            raise ValidationError(f"Не удалось создать каталог состояния WARP {paths.data}.") from error
    try:
        metadata = paths.data.lstat()
    except OSError as error:
        raise ValidationError(f"Каталог состояния WARP недоступен: {paths.data}") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValidationError(f"Каталог состояния WARP имеет небезопасный тип: {paths.data}")
    if os.name == "posix":
        if metadata.st_uid != os.geteuid():
            raise ValidationError(f"Каталог состояния WARP {paths.data} принадлежит другому пользователю.")
        if metadata.st_mode & 0o077:
            raise ValidationError(f"Каталог состояния WARP {paths.data} должен иметь права 0700.")


def _read_state(paths: WarpPaths) -> dict[str, Any]:
    _assert_private_data_directory(paths, create=False)
    if not paths.state.is_file():
        raise ValidationError("WARP не принят под управление: state.json отсутствует.")
    try:
        payload, mode = _read_regular_file(paths.state, require_owner=True)
        if os.name == "posix" and mode & 0o077:
            raise ValidationError("Состояние WARP state.json должно иметь права 0600.")
        state = json.loads(payload.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValidationError("Состояние WARP повреждено.") from error
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise ValidationError("Версия состояния WARP не поддерживается.")
    if state.get("backend", "native") != "native":
        raise ValidationError("Состояние WARP содержит неподдерживаемый backend.")
    if not isinstance(state.get("desired_enabled"), bool):
        raise ValidationError("Состояние WARP содержит некорректный desired_enabled.")
    failures = state.get("consecutive_failures", 0)
    if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
        raise ValidationError("Состояние WARP содержит некорректный счётчик ошибок.")
    restarts = state.get("restart_timestamps", [])
    if (
        not isinstance(restarts, list)
        or len(restarts) > 1024
        or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in restarts)
    ):
        raise ValidationError("Состояние WARP содержит некорректную историю restart.")
    owned = state.get("owned_files", {})
    if (
        not isinstance(owned, dict)
        or len(owned) > 64
        or any(
            not isinstance(path, str)
            or not path
            or "\x00" in path
            or not Path(path).is_absolute()
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
            for path, digest in owned.items()
        )
    ):
        raise ValidationError("Состояние WARP содержит некорректный список owned_files.")
    version = state.get("wgcf_version")
    if version is not None and (
        not isinstance(version, str) or _WGCF_VERSION_RE.fullmatch(version) is None
    ):
        raise ValidationError("Состояние WARP содержит некорректную версию wgcf.")
    return state


def _save_state(paths: WarpPaths, state: dict[str, Any]) -> None:
    if paths.state.is_symlink() or (paths.state.exists() and not paths.state.is_file()):
        raise ValidationError(f"Состояние WARP имеет небезопасный тип: {paths.state}")
    _assert_private_data_directory(paths, create=True)
    state["updated_at"] = utc_now()
    atomic_write_json(paths.state, state, mode=0o600)


def _assert_warp_installed(state: dict[str, Any]) -> None:
    if "uninstalled_at" in state:
        raise ValidationError(
            "WARP был удалён. Выполните uninstall --purge-credentials перед новой установкой."
        )


def _owned(paths: WarpPaths, binary: Path) -> dict[str, str]:
    candidates = [
        paths.config,
        paths.account,
        binary,
        wgcf_notice_path(binary),
        paths.health_service,
        paths.health_timer,
    ]
    return {str(path): sha256_file(path) for path in candidates if path.is_file()}


def _owned_wgcf_artifacts(
    paths: WarpPaths,
    state: dict[str, Any],
) -> dict[Path, str]:
    version = state.get("wgcf_version")
    if not isinstance(version, str) or _WGCF_VERSION_RE.fullmatch(version) is None:
        return {}
    allowed = {
        paths.bin_dir / f"wgcf-{version}",
        paths.bin_dir / f"wgcf-{version}.LICENSE.txt",
    }
    return {
        Path(path): digest
        for path, digest in state.get("owned_files", {}).items()
        if Path(path) in allowed
    }


def _assert_safe_manager_bin_directory(paths: WarpPaths) -> None:
    try:
        metadata = paths.bin_dir.lstat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise ValidationError(
            f"Не удалось проверить каталог wgcf {paths.bin_dir}: {error}"
        ) from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValidationError(f"Каталог wgcf имеет небезопасный тип: {paths.bin_dir}")
    if os.name == "posix" and (
        metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022
    ):
        raise ValidationError(
            f"Каталог wgcf {paths.bin_dir} имеет небезопасные владельца или права."
        )


def _owned_file_exists_unchanged(path: Path, expected: str) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    payload, _ = _read_regular_file(
        path,
        max_size=64 * 1024 * 1024,
        require_owner=True,
    )
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValidationError(
            f"Файл {path} изменён после установки; автоматическое удаление запрещено."
        )
    return True


def _snapshot(paths: list[Path]) -> dict[Path, tuple[bytes, int] | None]:
    snapshot: dict[Path, tuple[bytes, int] | None] = {}
    for path in paths:
        if not path.exists() and not path.is_symlink():
            snapshot[path] = None
            continue
        snapshot[path] = _read_regular_file(path, max_size=64 * 1024 * 1024)
    return snapshot


def _restore_snapshot(snapshot: dict[Path, tuple[bytes, int] | None]) -> None:
    errors: list[str] = []
    for path, saved in snapshot.items():
        try:
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise TransactionError(
                    f"WARP-файл {path} был подменён; rollback остановлен."
                )
            if saved is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write_bytes(path, saved[0], mode=saved[1])
        except BaseException as error:  # noqa: BLE001 - restore independent files
            errors.append(f"{path}: {error}")
    if errors:
        raise TransactionError("Не удалось восстановить WARP-файлы: " + "; ".join(errors))


def _restore_unit_state(
    runner: Runner,
    unit: str,
    *,
    active: bool,
    enablement: str,
    present: bool = True,
) -> None:
    if not present:
        return
    if enablement not in _ENABLED_UNIT_STATES | _DISABLED_UNIT_STATES:
        raise TransactionError(
            f"Нельзя восстановить неизвестное enabled-состояние unit {unit}: "
            f"{enablement!r}."
        )
    if enablement in {"linked", "linked-runtime", "alias"}:
        raise TransactionError(
            f"Автоматическое восстановление состояния {enablement!r} для unit {unit} "
            "невозможно без исходного link target."
        )

    errors: list[str] = []

    def restore_step(label: str, *arguments: str) -> None:
        try:
            _systemctl(runner, *arguments, unit)
        except BaseException as error:  # noqa: BLE001 - compensation must continue
            errors.append(f"{label}: {error}")

    # Remove both persistent and runtime masks before restoring links/activity.
    # A unit can remain active while masked, therefore masks are applied last.
    restore_step("снятие runtime mask", "unmask", "--runtime")
    restore_step("снятие persistent mask", "unmask")
    if enablement == "enabled-runtime":
        restore_step("удаление persistent enablement", "disable")
        restore_step("восстановление runtime enablement", "enable", "--runtime")
    elif enablement == "enabled":
        restore_step("восстановление persistent enablement", "enable")
    elif enablement not in {"masked", "masked-runtime"}:
        restore_step("отключение автозапуска", "disable")

    restore_step(
        "восстановление активности",
        "start" if active else "stop",
    )
    if enablement == "masked-runtime":
        restore_step("восстановление runtime mask", "mask", "--runtime")
    elif enablement == "masked":
        restore_step("восстановление persistent mask", "mask")

    current_enablement: str | None = None
    current_active: bool | None = None
    try:
        current_enablement = _unit_enablement(runner, unit)
    except BaseException as error:  # noqa: BLE001 - verify both dimensions
        errors.append(f"проверка enabled-состояния: {error}")
    try:
        current_active = _is_active(runner, unit)
    except BaseException as error:  # noqa: BLE001 - verify both dimensions
        errors.append(f"проверка активности: {error}")
    if current_enablement is not None and current_enablement != enablement:
        errors.append(
            f"enabled-состояние {current_enablement!r} вместо {enablement!r}"
        )
    if current_active is not None and current_active != active:
        errors.append(
            f"активность {current_active!r} вместо {active!r}"
        )
    if errors:
        raise TransactionError(
            f"Не удалось точно восстановить systemd unit {unit}: " + "; ".join(errors)
        )


def _rollback_step(errors: list[str], label: str, operation: Any) -> None:
    try:
        operation()
    except BaseException as error:  # noqa: BLE001 - rollback must survive interrupts
        errors.append(f"{label}: {error}")


def _cleanup_staging(path: Path, parent: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    ensure_within(path, parent)
    if path.is_symlink() or not path.is_dir():
        raise TransactionError(f"Временный WARP-каталог подменён: {path}")
    shutil.rmtree(path)
    if path.exists() or path.is_symlink():
        raise TransactionError(f"Не удалось полностью удалить временный WARP-каталог {path}.")


def _account_revoke_credentials(account: Path) -> tuple[str, str]:
    try:
        payload, _ = _read_regular_file(account, require_owner=True)
        data = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeError, tomllib.TOMLDecodeError, ValidationError) as error:
        raise TransactionError(
            f"Не удалось прочитать credentials WARP device; файл сохранён: {account}"
        ) from error
    device_id = data.get("device_id")
    access_token = data.get("access_token")
    if (
        not isinstance(device_id, str)
        or _CLOUDFLARE_DEVICE_RE.fullmatch(device_id) is None
        or not isinstance(access_token, str)
        or not access_token
        or len(access_token) > 4096
        or any(
            ord(character) < 0x20 or ord(character) > 0x7E
            for character in access_token
        )
    ):
        raise TransactionError(
            f"Credentials WARP device имеют неожиданный формат; файл сохранён: {account}"
        )
    return device_id, access_token


def _revoke_staged_account(account: Path) -> None:
    device_id, access_token = _account_revoke_credentials(account)
    quoted_device = urllib.parse.quote(device_id, safe="")
    request = urllib.request.Request(  # noqa: S310, RUF100 - fixed HTTPS API origin and quoted device ID
        f"{_CLOUDFLARE_API}/reg/{quoted_device}/account/reg/{quoted_device}",
        headers={
            "Authorization": f"Bearer {access_token}",
            "CF-Client-Version": "a-6.3-1922",
            "User-Agent": "okhttp/3.12.1",
        },
        method="DELETE",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _RejectRedirect(),
    )
    try:
        with opener.open(request, timeout=30) as response:
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
        with suppress(OSError):
            error.close()
        if status in {404, 410}:
            return
        raise TransactionError(
            f"Cloudflare отклонил отзыв WARP device (HTTP {status}); "
            f"credentials сохранены: {account}"
        ) from error
    except TransactionError as error:
        raise TransactionError(f"{error} Credentials сохранены: {account}") from error
    except (OSError, urllib.error.URLError) as error:
        raise TransactionError(
            f"Не удалось отозвать WARP device; credentials сохранены: {account}"
        ) from error
    if not 200 <= status < 300:
        raise TransactionError(
            f"Cloudflare вернул HTTP {status} при отзыве WARP device; "
            f"credentials сохранены: {account}"
        )


def _cleanup_failed_registration(staging: Path, parent: Path) -> None:
    if not staging.exists() and not staging.is_symlink():
        return
    ensure_within(staging, parent)
    if staging.is_symlink() or not staging.is_dir():
        raise TransactionError(
            f"Временный WARP-каталог подменён; credentials сохранены: {staging}"
        )
    account = staging / "account.toml"
    if account.exists() or account.is_symlink():
        _revoke_staged_account(account)
    _cleanup_staging(staging, parent)


def _chmod_private_regular(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValidationError(f"WARP-файл {path} не является обычным отдельным файлом.")
        descriptor = os.open(path, flags)
    except ValidationError:
        raise
    except OSError as error:
        raise ValidationError(f"Не удалось безопасно открыть WARP-файл {path}: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (metadata.st_dev, metadata.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ValidationError(f"WARP-файл {path} не является обычным отдельным файлом.")
        if os.name == "posix" and metadata.st_uid != os.geteuid():
            raise ValidationError(f"WARP-файл {path} принадлежит другому пользователю.")
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        else:
            os.chmod(path, 0o600)
    finally:
        os.close(descriptor)


def _external_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key in _EXTERNAL_ENVIRONMENT_KEYS
    }


def _generate_account(
    runner: Runner,
    binary: Path,
    staging: Path,
    *,
    license_key: str | None,
) -> tuple[Path, WarpProfile]:
    account = staging / "account.toml"
    profile = staging / "profile.conf"
    base_environment = _external_environment()
    runner.run(
        [str(binary), "--config", str(account), "register", "--accept-tos"],
        cwd=staging,
        env=base_environment,
        sensitive=True,
        timeout=120,
    )
    if license_key:
        # Viper in wgcf maps its license_key setting to WGCF_LICENSE_KEY.
        environment = dict(base_environment)
        environment["WGCF_LICENSE_KEY"] = license_key
        runner.run(
            [str(binary), "--config", str(account), "update"],
            cwd=staging,
            env=environment,
            sensitive=True,
            timeout=120,
        )
    runner.run(
        [str(binary), "--config", str(account), "generate", "--profile", str(profile)],
        cwd=staging,
        env=base_environment,
        sensitive=True,
        timeout=120,
    )
    if not account.is_file() or not profile.is_file():
        raise TransactionError("wgcf не создал account/profile.")
    _chmod_private_regular(account)
    _chmod_private_regular(profile)
    return account, load_warp_profile(profile, generated_profile=True)


def install_warp(
    runner: Runner,
    store: StateStore,
    *,
    accept_tos: bool,
    license_key: str | None = None,
    wgcf_file: Path | None = None,
) -> dict[str, Any]:
    if not accept_tos:
        raise ValidationError("Для регистрации нужно явно принять Cloudflare Terms of Service.")
    inventory = store.load_inventory()
    _assert_node_contract(runner, inventory)
    paths = WarpPaths(store.paths)
    _assert_unit_ownership(paths)
    scan = scan_warp(runner, store.paths)
    if (
        scan.config
        or scan.account
        or scan.manager_state
        or scan.legacy_paths
        or scan.conflicts
    ):
        raise ValidationError(
            "WARP уже настроен, найдена legacy-автоматизация или обнаружен конфликт. "
            "Для существующей установки используйте warp adopt."
        )
    contract = wgcf_contract()
    binary_path = paths.bin_dir / f"wgcf-{contract['version']}"
    notice_path = wgcf_notice_path(binary_path)
    snapshot = _snapshot(
        [
            paths.config,
            paths.account,
            paths.state,
            paths.health_service,
            paths.health_timer,
            binary_path,
            notice_path,
        ]
    )
    wg_active = scan.unit_active
    wg_enablement = _restorable_unit_enablement(runner, "wg-quick@warp.service")
    health_active = _is_active(runner, "remnawave-warp-health.timer")
    health_enablement = _restorable_unit_enablement(
        runner, "remnawave-warp-health.timer"
    )
    health_present = snapshot[paths.health_timer] is not None
    data_existed = paths.data.is_dir()
    create_backup(runner, store, reason="pre-warp-install", retention=None)
    apt_environment = _external_environment()
    apt_environment.update(
        {
            "APT_LISTCHANGES_FRONTEND": "none",
            "DEBIAN_FRONTEND": "noninteractive",
        }
    )
    runner.run(
        ["apt-get", "update"],
        env=apt_environment,
        timeout=1200,
    )
    runner.run(
        [
            "apt-get",
            "install",
            "-y",
            "--no-install-recommends",
            "iproute2",
            "nftables",
            "wireguard-tools",
        ],
        env=apt_environment,
        timeout=1200,
    )
    staging = paths.data / f".staging-{uuid.uuid4().hex}"
    before = _invariants(runner, store.paths)
    wg_mutated = False
    health_mutated = False
    try:
        binary = install_wgcf(paths.bin_dir, local_file=wgcf_file)
        if binary != binary_path:
            raise TransactionError("Установщик wgcf вернул неожиданный путь binary.")
        _assert_private_data_directory(paths, create=True)
        staging.mkdir(mode=0o700)
        account, profile = _generate_account(
            runner, binary, staging, license_key=license_key
        )
        paths.config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_text(paths.config, profile.render(), mode=0o600)
        atomic_copy(account, paths.account, mode=0o600)
        _install_units(runner, paths)
        wg_mutated = True
        _systemctl(runner, "enable", "--now", "wg-quick@warp.service")
        mode = _trace_via_warp()
        _verify_container_visibility(runner, inventory)
        after = _invariants(runner, store.paths)
        _assert_invariants(before, after)
        health_mutated = True
        _systemctl(runner, "enable", "--now", "remnawave-warp-health.timer")
        state = {
            "schema_version": 1,
            "backend": "native",
            "desired_enabled": True,
            "installed_at": utc_now(),
            "tos_accepted_at": utc_now(),
            "account_type": mode,
            "consecutive_failures": 0,
            "restart_timestamps": [],
            "owned_files": _owned(paths, binary),
            "wgcf_version": contract["version"],
        }
        _save_state(paths, state)
        _cleanup_staging(staging, paths.data)
        return state
    except BaseException as error:
        rollback_errors: list[str] = []
        if health_mutated:
            _rollback_step(
                rollback_errors,
                "остановка health timer",
                lambda: _systemctl(
                    runner,
                    "disable",
                    "--now",
                    "remnawave-warp-health.timer",
                ),
            )
        if wg_mutated:
            _rollback_step(
                rollback_errors,
                "остановка WARP",
                lambda: _systemctl(runner, "stop", "wg-quick@warp.service"),
            )
        _rollback_step(
            rollback_errors,
            "восстановление файлов",
            lambda: _restore_snapshot(snapshot),
        )
        _rollback_step(
            rollback_errors,
            "systemd daemon-reload",
            lambda: _systemctl(runner, "daemon-reload"),
        )
        if wg_mutated or wg_active or wg_enablement in _ENABLED_UNIT_STATES:
            _rollback_step(
                rollback_errors,
                "восстановление WARP service",
                lambda: _restore_unit_state(
                    runner,
                    "wg-quick@warp.service",
                    active=wg_active,
                    enablement=wg_enablement,
                ),
            )
        _rollback_step(
            rollback_errors,
            "восстановление health timer",
            lambda: _restore_unit_state(
                runner,
                "remnawave-warp-health.timer",
                active=health_active,
                enablement=health_enablement,
                present=health_present,
            ),
        )
        _rollback_step(
            rollback_errors,
            "отзыв временного WARP device и удаление credentials",
            lambda: _cleanup_failed_registration(staging, paths.data),
        )
        if not data_existed and paths.data.is_dir():
            _rollback_step(
                rollback_errors,
                "удаление пустого WARP state каталога",
                paths.data.rmdir,
            )
        _rollback_step(
            rollback_errors,
            "системные invariants",
            lambda: _assert_invariants(before, _invariants(runner, store.paths)),
        )
        if rollback_errors:
            raise TransactionError(
                "Установка WARP не завершена, rollback неполон: "
                + "; ".join(rollback_errors)
                + f". Исходная ошибка: {error}"
            ) from error
        raise


def adopt_warp(
    runner: Runner,
    store: StateStore,
    *,
    takeover: bool,
    wgcf_file: Path | None = None,
) -> WarpScan:
    inventory = store.load_inventory()
    _assert_node_contract(runner, inventory)
    scan = scan_warp(runner, store.paths)
    if not takeover:
        return scan
    if not scan.safe_takeover or not scan.config:
        raise ValidationError("Существующая WARP-конфигурация не прошла безопасный takeover scan.")
    paths = WarpPaths(store.paths)
    _assert_unit_ownership(paths)
    cron, cron_target = _legacy_cron_paths(store.paths)
    _assert_legacy_cron_move(cron, cron_target)
    profile = load_warp_profile(Path(scan.config))
    contract = wgcf_contract()
    binary_path = paths.bin_dir / f"wgcf-{contract['version']}"
    notice_path = wgcf_notice_path(binary_path)
    snapshots = _snapshot(
        [
            paths.config,
            paths.account,
            paths.state,
            paths.health_service,
            paths.health_timer,
            binary_path,
            notice_path,
        ]
    )
    wg_active = scan.unit_active
    wg_enablement = _restorable_unit_enablement(runner, "wg-quick@warp.service")
    health_active = _is_active(runner, "remnawave-warp-health.timer")
    health_enablement = _restorable_unit_enablement(
        runner, "remnawave-warp-health.timer"
    )
    health_present = snapshots[paths.health_timer] is not None
    before = _invariants(runner, store.paths)
    create_backup(runner, store, reason="pre-warp-takeover", retention=None)
    disabled_legacy: dict[str, str] = {}
    wg_mutated = False
    health_mutated = False
    try:
        disabled_legacy = _disable_legacy_cron(cron, cron_target)
        binary = install_wgcf(paths.bin_dir, local_file=wgcf_file)
        if binary != binary_path:
            raise TransactionError("Установщик wgcf вернул неожиданный путь бинарного файла.")
        _assert_private_data_directory(paths, create=True)
        if scan.account and Path(scan.account) != paths.account:
            account_payload, _ = _read_regular_file(Path(scan.account))
            atomic_write_bytes(paths.account, account_payload, mode=0o600)
        atomic_write_text(paths.config, profile.render(), mode=0o600)
        _install_units(runner, paths)
        wg_mutated = True
        _systemctl(runner, "enable", "--now", "wg-quick@warp.service")
        mode = _trace_via_warp()
        _verify_container_visibility(runner, inventory)
        _assert_invariants(before, _invariants(runner, store.paths))
        health_mutated = True
        _systemctl(runner, "enable", "--now", "remnawave-warp-health.timer")

        state = {
            "schema_version": 1,
            "backend": "native",
            "desired_enabled": True,
            "adopted_at": utc_now(),
            "account_type": mode,
            "consecutive_failures": 0,
            "restart_timestamps": [],
            "owned_files": _owned(paths, binary),
            "disabled_legacy": disabled_legacy,
            "wgcf_version": contract["version"],
            "profile": {
                "addresses": list(profile.addresses),
                "endpoint": profile.endpoint,
            },
        }
        _save_state(paths, state)
        return scan_warp(runner, store.paths)
    except BaseException as error:
        rollback_errors: list[str] = []

        def rollback(label: str, operation: Any) -> None:
            try:
                operation()
            except BaseException as rollback_error:  # noqa: BLE001 - collect rollback failures
                rollback_errors.append(f"{label}: {rollback_error}")

        if health_mutated:
            rollback(
                "остановка health timer",
                lambda: _systemctl(
                    runner,
                    "disable",
                    "--now",
                    "remnawave-warp-health.timer",
                ),
            )
        if wg_mutated:
            rollback(
                "остановка временного WARP",
                lambda: _systemctl(runner, "stop", "wg-quick@warp.service"),
            )
        if disabled_legacy:
            rollback(
                "восстановление legacy cron",
                lambda: _restore_legacy_cron(cron, cron_target),
            )
        rollback("восстановление файлов", lambda: _restore_snapshot(snapshots))
        rollback(
            "systemd daemon-reload",
            lambda: _systemctl(runner, "daemon-reload"),
        )
        rollback(
            "восстановление состояния WARP service",
            lambda: _restore_unit_state(
                runner,
                "wg-quick@warp.service",
                active=wg_active,
                enablement=wg_enablement,
            ),
        )
        rollback(
            "восстановление состояния health timer",
            lambda: _restore_unit_state(
                runner,
                "remnawave-warp-health.timer",
                active=health_active,
                enablement=health_enablement,
                present=health_present,
            ),
        )
        rollback(
            "системные invariants",
            lambda: _assert_invariants(before, _invariants(runner, store.paths)),
        )
        if rollback_errors:
            raise TransactionError(
                "WARP takeover не завершён, автоматический откат неполон: "
                + "; ".join(rollback_errors)
                + f". Исходная ошибка: {error}"
            ) from error
        raise


def warp_action(runner: Runner, store: StateStore, action: Literal["start", "stop", "restart"]) -> None:
    if action not in {"start", "stop", "restart"}:
        raise ValidationError(f"Неизвестное WARP-действие: {action}")
    inventory = store.load_inventory()
    _assert_node_contract(runner, inventory)
    paths = WarpPaths(store.paths)
    state = _read_state(paths)
    _assert_warp_installed(state)
    _assert_unit_ownership(paths)
    load_warp_profile(paths.config)
    state_snapshot = _snapshot([paths.state])
    wg_active = _is_active(runner, "wg-quick@warp.service")
    wg_enablement = _restorable_unit_enablement(runner, "wg-quick@warp.service")
    health_active = _is_active(runner, "remnawave-warp-health.timer")
    health_enablement = _restorable_unit_enablement(
        runner, "remnawave-warp-health.timer"
    )
    before = _invariants(runner, store.paths)
    try:
        if action == "stop":
            _systemctl(
                runner,
                "disable",
                "--now",
                "remnawave-warp-health.timer",
            )
            _systemctl(runner, "disable", "--now", "wg-quick@warp.service")
            state["desired_enabled"] = False
        else:
            if action == "restart":
                _systemctl(runner, "enable", "wg-quick@warp.service")
                _systemctl(runner, "restart", "wg-quick@warp.service")
            else:
                _systemctl(runner, "enable", "--now", "wg-quick@warp.service")
            _trace_via_warp()
            _verify_container_visibility(runner, inventory)
            _systemctl(
                runner,
                "enable",
                "--now",
                "remnawave-warp-health.timer",
            )
            state["desired_enabled"] = True
        _assert_invariants(before, _invariants(runner, store.paths))
        state["consecutive_failures"] = 0
        _save_state(paths, state)
    except BaseException as error:
        rollback_errors: list[str] = []

        def rollback(label: str, operation: Any) -> None:
            try:
                operation()
            except BaseException as rollback_error:  # noqa: BLE001 - collect rollback failures
                rollback_errors.append(f"{label}: {rollback_error}")

        rollback(
            "остановка health timer",
            lambda: _systemctl(
                runner,
                "disable",
                "--now",
                "remnawave-warp-health.timer",
            ),
        )
        rollback(
            "остановка WARP",
            lambda: _systemctl(
                runner,
                "stop",
                "wg-quick@warp.service",
            ),
        )
        rollback(
            "восстановление WARP service",
            lambda: _restore_unit_state(
                runner,
                "wg-quick@warp.service",
                active=wg_active,
                enablement=wg_enablement,
            ),
        )
        rollback(
            "восстановление health timer",
            lambda: _restore_unit_state(
                runner,
                "remnawave-warp-health.timer",
                active=health_active,
                enablement=health_enablement,
            ),
        )
        rollback("восстановление state.json", lambda: _restore_snapshot(state_snapshot))
        try:
            _assert_invariants(before, _invariants(runner, store.paths))
        except BaseException as rollback_error:  # noqa: BLE001 - collect rollback failures
            rollback_errors.append(f"системные invariants: {rollback_error}")
        if rollback_errors:
            raise TransactionError(
                f"Действие WARP {action} завершилось ошибкой, а rollback выполнен "
                "не полностью: "
                + "; ".join(rollback_errors)
                + f". Исходная ошибка: {error}"
            ) from error
        raise TransactionError(
            f"Действие WARP {action} не выполнено; прежнее состояние восстановлено: {error}"
        ) from error


def warp_status(runner: Runner, store: StateStore) -> dict[str, Any]:
    paths = WarpPaths(store.paths)
    state = _read_state(paths)
    handshake = _latest_handshake(runner)
    trace: str | None = None
    error: str | None = None
    active = _is_active(runner, "wg-quick@warp.service")
    if active:
        try:
            trace = _trace_via_warp()
        except (TransactionError, ValidationError) as current:
            error = sanitize_external_text(str(current), limit=1000)
    return {
        "active": active,
        "desired_enabled": bool(state.get("desired_enabled")),
        "trace": trace,
        "last_handshake_seconds_ago": max(0, int(time.time()) - handshake) if handshake else None,
        "consecutive_failures": int(state.get("consecutive_failures", 0)),
        "error": error,
    }


def warp_watchdog(runner: Runner, store: StateStore) -> str:
    paths = WarpPaths(store.paths)
    state = _read_state(paths)
    if not state.get("desired_enabled"):
        return "disabled"
    now = int(time.time())
    try:
        if not _is_active(runner, "wg-quick@warp.service"):
            raise TransactionError("wg-quick@warp не активен")
        _trace_via_warp()
        state["consecutive_failures"] = 0
        state["last_health"] = "healthy"
        _save_state(paths, state)
        return "healthy"
    except _SystemdQueryError:
        raise
    except TransactionError as error:
        handshake = _latest_handshake(runner)
        handshake_age = now - handshake
        if handshake and 0 <= handshake_age < 300:
            state["consecutive_failures"] = 0
            state["last_health"] = "degraded"
            state["last_error"] = sanitize_external_text(str(error), limit=1000)
            _save_state(paths, state)
            return "degraded"
        failures = int(state.get("consecutive_failures", 0)) + 1
        state["consecutive_failures"] = failures
        state["last_health"] = "failed"
        state["last_error"] = sanitize_external_text(str(error), limit=1000)
        restarts = [
            int(item)
            for item in state.get("restart_timestamps", [])
            if 0 <= now - int(item) < 3600
        ]
        last_restart = max(restarts, default=0)
        if failures >= 3 and now - last_restart >= 600 and len(restarts) < 3:
            restarts.append(now)
            state["restart_timestamps"] = restarts
            state["last_health"] = "restart_pending"
            _save_state(paths, state)
            _systemctl(runner, "restart", "wg-quick@warp.service")
            state["consecutive_failures"] = 0
            state["last_health"] = "restarted"
        _save_state(paths, state)
        return str(state["last_health"])


def rotate_warp(
    runner: Runner,
    store: StateStore,
    *,
    accept_tos: bool,
    license_key: str | None = None,
) -> None:
    if not accept_tos:
        raise ValidationError("Для новой регистрации нужно явно принять Cloudflare Terms of Service.")
    inventory = store.load_inventory()
    _assert_node_contract(runner, inventory)
    paths = WarpPaths(store.paths)
    state = _read_state(paths)
    _assert_warp_installed(state)
    _assert_unit_ownership(paths)
    pending = _pending_staging(paths)
    if pending:
        raise ValidationError(
            "Ротация WARP запрещена до recovery незавершённой операции: "
            + ", ".join(str(path) for path in pending)
        )
    load_warp_profile(paths.config)
    contract = wgcf_contract()
    binary_path = paths.bin_dir / f"wgcf-{contract['version']}"
    notice_path = wgcf_notice_path(binary_path)
    previous_artifacts = _owned_wgcf_artifacts(paths, state)
    stale_artifacts = {
        path: digest
        for path, digest in previous_artifacts.items()
        if path not in {binary_path, notice_path}
    }
    _assert_safe_manager_bin_directory(paths)
    for path, digest in stale_artifacts.items():
        _owned_file_exists_unchanged(path, digest)
    snapshot = _snapshot(
        [
            paths.config,
            paths.account,
            paths.state,
            binary_path,
            notice_path,
            *stale_artifacts,
        ]
    )
    wg_active = _is_active(runner, "wg-quick@warp.service")
    wg_enablement = _restorable_unit_enablement(runner, "wg-quick@warp.service")
    health_active = _is_active(runner, "remnawave-warp-health.timer")
    health_enablement = _restorable_unit_enablement(
        runner, "remnawave-warp-health.timer"
    )
    health_present = paths.health_timer.is_file()
    desired_before = bool(state.get("desired_enabled"))
    create_backup(runner, store, reason="pre-warp-rotate", retention=None)
    staging = paths.data / f".rotate-{uuid.uuid4().hex}"
    before = _invariants(runner, store.paths)
    wg_mutated = False
    unit_restore_started = False
    committed = False
    previous_account: Path | None = None
    try:
        binary = install_wgcf(paths.bin_dir)
        if binary != binary_path:
            raise TransactionError("Установщик wgcf вернул неожиданный путь binary.")
        staging.mkdir(mode=0o700)
        saved_account = snapshot[paths.account]
        if saved_account is not None:
            previous_account = staging / "previous-account.toml"
            atomic_write_bytes(previous_account, saved_account[0], mode=0o600)
            # Validate cleanup credentials before registering another device.
            _account_revoke_credentials(previous_account)
        account, profile = _generate_account(runner, binary, staging, license_key=license_key)
        wg_mutated = True
        _systemctl(runner, "stop", "wg-quick@warp.service")
        atomic_write_text(paths.config, profile.render(), mode=0o600)
        atomic_copy(account, paths.account, mode=0o600)
        _systemctl(runner, "start", "wg-quick@warp.service")
        mode = _trace_via_warp()
        _verify_container_visibility(runner, inventory)
        _assert_invariants(before, _invariants(runner, store.paths))
        state["account_type"] = mode
        state["rotated_at"] = utc_now()
        state["wgcf_version"] = contract["version"]
        state["consecutive_failures"] = 0
        state["desired_enabled"] = desired_before
        unit_restore_started = True
        _restore_unit_state(
            runner,
            "wg-quick@warp.service",
            active=wg_active,
            enablement=wg_enablement,
        )
        _restore_unit_state(
            runner,
            "remnawave-warp-health.timer",
            active=health_active,
            enablement=health_enablement,
            present=health_present,
        )
        for path in stale_artifacts:
            path.unlink(missing_ok=True)
        state["owned_files"] = _owned(paths, binary)
        _save_state(paths, state)
        committed = True
        if previous_account is not None:
            _revoke_staged_account(previous_account)
        _cleanup_staging(staging, paths.data)
    except BaseException as error:
        if committed:
            raise TransactionError(
                "Новая WARP-конфигурация применена и оставлена активной, но отзыв "
                "прежнего Cloudflare device или очистка временных credentials не "
                f"завершены. Автоматический rollback после commit запрещён; "
                f"выполните ручной recovery каталога {staging}. Ошибка: {error}"
            ) from error
        rollback_errors: list[str] = []
        if wg_mutated or unit_restore_started:
            _rollback_step(
                rollback_errors,
                "остановка временного WARP",
                lambda: _systemctl(runner, "stop", "wg-quick@warp.service"),
            )
        _rollback_step(
            rollback_errors,
            "восстановление WARP-файлов",
            lambda: _restore_snapshot(snapshot),
        )
        if wg_mutated or unit_restore_started:
            _rollback_step(
                rollback_errors,
                "восстановление WARP service",
                lambda: _restore_unit_state(
                    runner,
                    "wg-quick@warp.service",
                    active=wg_active,
                    enablement=wg_enablement,
                ),
            )
            _rollback_step(
                rollback_errors,
                "восстановление health timer",
                lambda: _restore_unit_state(
                    runner,
                    "remnawave-warp-health.timer",
                    active=health_active,
                    enablement=health_enablement,
                    present=health_present,
                ),
            )
        _rollback_step(
            rollback_errors,
            "отзыв временного WARP device и удаление credentials",
            lambda: _cleanup_failed_registration(staging, paths.data),
        )
        _rollback_step(
            rollback_errors,
            "системные invariants",
            lambda: _assert_invariants(before, _invariants(runner, store.paths)),
        )
        if rollback_errors:
            raise TransactionError(
                "Ротация WARP не завершена, rollback неполон: "
                + "; ".join(rollback_errors)
                + f". Исходная ошибка: {error}"
            ) from error
        raise


def uninstall_warp(
    runner: Runner,
    store: StateStore,
    *,
    purge_credentials: bool = False,
) -> None:
    paths = WarpPaths(store.paths)
    state = _read_state(paths)
    owned = {Path(path): digest for path, digest in state.get("owned_files", {}).items()}
    _assert_safe_manager_bin_directory(paths)
    removable = [paths.health_service, paths.health_timer]
    removable.extend(_owned_wgcf_artifacts(paths, state))
    if purge_credentials:
        removable.extend([paths.config, paths.account])
    removable = list(dict.fromkeys(removable))
    for path in removable:
        if path.is_symlink():
            raise ValidationError(f"Файл {path} заменён символической ссылкой; удаление запрещено.")
        expected = owned.get(path)
        if path.exists() and (
            not expected or not _owned_file_exists_unchanged(path, expected)
        ):
            raise ValidationError(
                f"Файл {path} изменён после установки; автоматическое удаление запрещено."
            )

    snapshot_paths = [*removable, paths.state]
    snapshots = _snapshot(list(dict.fromkeys(snapshot_paths)))
    wg_active = _is_active(runner, "wg-quick@warp.service")
    wg_enablement = _restorable_unit_enablement(runner, "wg-quick@warp.service")
    health_active = _is_active(runner, "remnawave-warp-health.timer")
    health_enablement = _restorable_unit_enablement(
        runner, "remnawave-warp-health.timer"
    )
    health_present = snapshots.get(paths.health_timer) is not None
    create_backup(runner, store, reason="pre-warp-uninstall", retention=None)

    try:
        if health_active:
            _systemctl(runner, "stop", "remnawave-warp-health.timer")
        if health_enablement in _ENABLED_UNIT_STATES:
            _systemctl(runner, "disable", "remnawave-warp-health.timer")
        if wg_active:
            _systemctl(runner, "stop", "wg-quick@warp.service")
        if wg_enablement in _ENABLED_UNIT_STATES:
            _systemctl(runner, "disable", "wg-quick@warp.service")
        for path in removable:
            path.unlink(missing_ok=True)
        _systemctl(runner, "daemon-reload")
        if purge_credentials:
            paths.state.unlink(missing_ok=True)
            return
        state["desired_enabled"] = False
        state["uninstalled_at"] = utc_now()
        state["owned_files"] = {
            str(path): digest for path, digest in owned.items() if path.exists()
        }
        _save_state(paths, state)
    except BaseException as error:
        rollback_errors: list[str] = []

        def rollback(label: str, operation: Any) -> None:
            try:
                operation()
            except BaseException as rollback_error:  # noqa: BLE001 - collect rollback failures
                rollback_errors.append(f"{label}: {rollback_error}")

        rollback("восстановление файлов", lambda: _restore_snapshot(snapshots))
        rollback(
            "systemd daemon-reload",
            lambda: _systemctl(runner, "daemon-reload"),
        )
        rollback(
            "восстановление WARP service",
            lambda: _restore_unit_state(
                runner,
                "wg-quick@warp.service",
                active=wg_active,
                enablement=wg_enablement,
            ),
        )
        rollback(
            "восстановление health timer",
            lambda: _restore_unit_state(
                runner,
                "remnawave-warp-health.timer",
                active=health_active,
                enablement=health_enablement,
                present=health_present,
            ),
        )
        if rollback_errors:
            raise TransactionError(
                "Удаление WARP завершилось ошибкой, rollback неполон: "
                + "; ".join(rollback_errors)
                + f". Исходная ошибка: {error}"
            ) from error
        raise TransactionError(
            f"Удаление WARP не выполнено; прежнее состояние восстановлено: {error}"
        ) from error
