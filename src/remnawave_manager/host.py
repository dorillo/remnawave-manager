from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .errors import ManagerError, TransactionError, ValidationError
from .paths import RuntimePaths
from .runner import (
    Runner,
    atomic_write_text,
    read_stable_regular_file,
)

_SYSCTL_MARKER = "# Managed by remnawave-manager"
_APT_MARKER = "// Managed by remnawave-manager"
_SYSCTL_CONFIG = (
    f"{_SYSCTL_MARKER}\n"
    "net.core.default_qdisc = fq\n"
    "net.ipv4.tcp_congestion_control = bbr\n"
)
_APT_CONFIG = (
    f"{_APT_MARKER}\n"
    'APT::Periodic::Update-Package-Lists "1";\n'
    'APT::Periodic::Unattended-Upgrade "1";\n'
    'Unattended-Upgrade::Automatic-Reboot "false";\n'
)
_APT_TIMER_UNITS = ("apt-daily.timer", "apt-daily-upgrade.timer")
_UNATTENDED_UNIT = "unattended-upgrades.service"
_MAX_HOST_CONFIG_SIZE = 1024 * 1024
_APT_ENVIRONMENT_KEYS = {
    "ALL_PROXY",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "NO_PROXY",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "all_proxy",
    "http_proxy",
    "https_proxy",
    "no_proxy",
}


@dataclass(frozen=True, slots=True)
class HostStatus:
    bbr_available: bool
    bbr_enabled: bool
    fq_enabled: bool
    unattended_configured: bool
    apt_daily_timer_enabled: bool
    apt_daily_timer_active: bool
    apt_upgrade_timer_enabled: bool
    apt_upgrade_timer_active: bool
    unattended_service_enabled: bool
    unattended_service_active: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class OperatingSystemUpdate:
    reboot_required: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _managed_paths(runtime: RuntimePaths) -> tuple[Path, Path]:
    return (
        runtime.root / "etc/sysctl.d/90-remnawave-manager-bbr.conf",
        runtime.root
        / "etc/apt/apt.conf.d/52remnawave-manager-unattended-upgrades",
    )


def _assert_owned(path: Path, marker: str) -> None:
    if not path.exists() and not path.is_symlink():
        return
    _owned_config_snapshot(path, marker)


def _owned_config_snapshot(path: Path, marker: str) -> tuple[str, int]:
    snapshot = read_stable_regular_file(
        path,
        max_size=_MAX_HOST_CONFIG_SIZE,
        label="Системный файл",
    )
    if os.name == "posix" and (
        snapshot.uid != os.geteuid() or snapshot.mode & 0o022
    ):
        raise ValidationError(
            f"Системный файл {path} должен принадлежать root и не быть доступным "
            "для записи группе/прочим."
        )
    try:
        content = snapshot.data.decode("utf-8")
    except UnicodeError as error:
        raise ValidationError(
            f"Системный файл {path} не является корректным UTF-8; "
            "автоматическая перезапись запрещена."
        ) from error
    if not content.splitlines() or content.splitlines()[0] != marker:
        raise ValidationError(
            f"Системный файл {path} создан не менеджером; "
            "автоматическая перезапись запрещена."
        )
    return content, snapshot.mode


def _matches_managed_config(path: Path, expected: str) -> bool:
    try:
        content, _ = _owned_config_snapshot(path, expected.splitlines()[0])
        return content.replace("\r\n", "\n").replace("\r", "\n") == expected
    except ValidationError:
        return False


def _sysctl(runner: Runner, name: str) -> str:
    result = runner.run(["sysctl", "-n", name], check=False, timeout=30)
    return result.stdout.strip() if result.returncode == 0 else ""


def _unit_state(runner: Runner, action: str, unit: str) -> bool:
    return (
        runner.run(
            ["systemctl", action, "--quiet", unit],
            check=False,
            timeout=30,
        ).returncode
        == 0
    )


def _required_sysctl(runner: Runner, name: str) -> str:
    result = runner.run(["sysctl", "-n", name], check=False, timeout=30)
    value = result.stdout.strip()
    if result.returncode != 0 or not value:
        raise ValidationError(f"Не удалось определить исходное значение sysctl {name}.")
    return value


def _required_unit_enablement(runner: Runner, unit: str) -> str:
    enabled = runner.run(
        ["systemctl", "is-enabled", unit],
        check=False,
        timeout=30,
    )
    enabled_state = enabled.stdout.strip()
    enabled_code_valid = (
        enabled_state in {"enabled", "enabled-runtime"}
        and enabled.returncode == 0
        or enabled_state not in {"enabled", "enabled-runtime"}
        and enabled.returncode in {0, 1}
    )
    if (
        not enabled_code_valid
        or enabled_state
        not in {
            "enabled",
            "enabled-runtime",
            "disabled",
            "masked",
            "masked-runtime",
            "static",
            "indirect",
        }
    ):
        raise ValidationError(
            f"Не удалось определить исходное состояние systemd unit {unit}."
        )
    return enabled_state


def _required_unit_activity(runner: Runner, unit: str) -> str:
    active = runner.run(
        ["systemctl", "is-active", unit],
        check=False,
        timeout=30,
    )
    active_state = active.stdout.strip()
    if active.returncode not in {0, 3} or active_state not in {"active", "inactive"}:
        raise ValidationError(
            f"Не удалось определить исходное состояние systemd unit {unit}."
        )
    return active_state


def _required_unit_state(runner: Runner, unit: str) -> dict[str, str]:
    return {
        "enabled": _required_unit_enablement(runner, unit),
        "active": _required_unit_activity(runner, unit),
    }


def _restore_unit_state(
    runner: Runner,
    unit: str,
    state: dict[str, str],
) -> None:
    enabled_state = state["enabled"]
    errors: list[str] = []

    def run_step(label: str, command: list[str]) -> None:
        try:
            runner.run(command, timeout=120)
        except BaseException as error:  # noqa: BLE001 - continue independent compensation
            errors.append(f"{label}: {str(error) or type(error).__name__}")

    # Clear both mask locations first. This is required to restore an active
    # unit that was masked before the transaction; the mask is reapplied last.
    run_step("runtime unmask", ["systemctl", "unmask", "--runtime", unit])
    run_step("persistent unmask", ["systemctl", "unmask", unit])
    run_step(
        "active-state",
        [
            "systemctl",
            "start" if state["active"] == "active" else "stop",
            unit,
        ],
    )
    if enabled_state == "enabled-runtime":
        run_step(
            "persistent enablement cleanup",
            ["systemctl", "disable", unit],
        )
        run_step(
            "runtime enablement",
            ["systemctl", "enable", "--runtime", unit],
        )
    elif enabled_state == "enabled":
        run_step("enablement", ["systemctl", "enable", unit])
    else:
        run_step("enablement cleanup", ["systemctl", "disable", unit])
    if enabled_state == "masked-runtime":
        run_step("runtime mask", ["systemctl", "mask", "--runtime", unit])
    elif enabled_state == "masked":
        run_step("mask", ["systemctl", "mask", unit])

    restored_enabled: str | None = None
    restored_active: str | None = None
    try:
        restored_enabled = _required_unit_enablement(runner, unit)
    except BaseException as error:  # noqa: BLE001 - verify active independently
        errors.append(
            f"enablement verification: {str(error) or type(error).__name__}"
        )
    try:
        restored_active = _required_unit_activity(runner, unit)
    except BaseException as error:  # noqa: BLE001 - report both verification failures
        errors.append(f"active verification: {str(error) or type(error).__name__}")
    if restored_enabled is not None and restored_enabled != enabled_state:
        errors.append(
            f"enablement {restored_enabled!r} вместо {enabled_state!r}"
        )
    if restored_active is not None and restored_active != state["active"]:
        errors.append(
            f"active-state {restored_active!r} вместо {state['active']!r}"
        )
    if errors:
        raise TransactionError(
            f"Не удалось точно восстановить исходное состояние systemd unit {unit}: "
            + "; ".join(errors)
        )


def host_status(runner: Runner, runtime: RuntimePaths) -> HostStatus:
    _sysctl_path, apt_path = _managed_paths(runtime)
    available = _sysctl(runner, "net.ipv4.tcp_available_congestion_control").split()
    return HostStatus(
        bbr_available="bbr" in available,
        bbr_enabled=_sysctl(runner, "net.ipv4.tcp_congestion_control") == "bbr",
        fq_enabled=_sysctl(runner, "net.core.default_qdisc") == "fq",
        unattended_configured=_matches_managed_config(apt_path, _APT_CONFIG),
        apt_daily_timer_enabled=_unit_state(
            runner, "is-enabled", "apt-daily.timer"
        ),
        apt_daily_timer_active=_unit_state(
            runner, "is-active", "apt-daily.timer"
        ),
        apt_upgrade_timer_enabled=_unit_state(
            runner, "is-enabled", "apt-daily-upgrade.timer"
        ),
        apt_upgrade_timer_active=_unit_state(
            runner, "is-active", "apt-daily-upgrade.timer"
        ),
        unattended_service_enabled=_unit_state(
            runner, "is-enabled", _UNATTENDED_UNIT
        ),
        unattended_service_active=_unit_state(
            runner, "is-active", _UNATTENDED_UNIT
        ),
    )


def update_operating_system(
    runner: Runner,
    runtime: RuntimePaths,
) -> OperatingSystemUpdate:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in _APT_ENVIRONMENT_KEYS
    }
    environment.update(
        {
            "APT_LISTCHANGES_FRONTEND": "none",
            "DEBIAN_FRONTEND": "noninteractive",
            "NEEDRESTART_MODE": "a",
        }
    )
    lock_options = ["-o", "DPkg::Lock::Timeout=600"]
    runner.run(
        ["apt-get", *lock_options, "update"],
        env=environment,
        timeout=1800,
    )
    runner.run(
        [
            "apt-get",
            *lock_options,
            "-o",
            "Dpkg::Options::=--force-confdef",
            "-o",
            "Dpkg::Options::=--force-confold",
            "-y",
            "full-upgrade",
        ],
        env=environment,
        timeout=7200,
    )
    reboot_marker = runtime.root / "var/run/reboot-required"
    return OperatingSystemUpdate(reboot_required=reboot_marker.is_file())


def configure_host(runner: Runner, runtime: RuntimePaths) -> HostStatus:
    sysctl_path, apt_path = _managed_paths(runtime)
    _assert_owned(sysctl_path, _SYSCTL_MARKER)
    _assert_owned(apt_path, _APT_MARKER)

    previous_sysctl = {
        "net.ipv4.tcp_congestion_control": _required_sysctl(
            runner, "net.ipv4.tcp_congestion_control"
        ),
        "net.core.default_qdisc": _required_sysctl(
            runner, "net.core.default_qdisc"
        ),
    }
    timer_states = {
        unit: _required_unit_state(runner, unit)
        for unit in _APT_TIMER_UNITS
    }
    unattended_state = _required_unit_state(runner, _UNATTENDED_UNIT)

    runner.run(["modprobe", "tcp_bbr"], check=False, timeout=30)
    available = _required_sysctl(
        runner, "net.ipv4.tcp_available_congestion_control"
    ).split()
    if "bbr" not in available:
        raise ValidationError(
            "Ядро не сообщает поддержку BBR; системная оптимизация не применена."
        )

    snapshots = {
        path: _owned_config_snapshot(
            path,
            _SYSCTL_MARKER if path == sysctl_path else _APT_MARKER,
        )
        if path.is_file()
        else None
        for path in (sysctl_path, apt_path)
    }
    try:
        atomic_write_text(sysctl_path, _SYSCTL_CONFIG, mode=0o644)
        atomic_write_text(apt_path, _APT_CONFIG, mode=0o644)
        runner.run(["sysctl", "--load", str(sysctl_path)], timeout=120)
        if (
            _sysctl(runner, "net.ipv4.tcp_congestion_control") != "bbr"
            or _sysctl(runner, "net.core.default_qdisc") != "fq"
        ):
            raise TransactionError("Настройки BBR/fq записаны, но ядро их не применило.")
        runner.run(
            ["systemctl", "enable", "--now", *_APT_TIMER_UNITS],
            timeout=120,
        )
        runner.run(
            ["systemctl", "enable", "--now", _UNATTENDED_UNIT],
            timeout=120,
        )
        status = host_status(runner, runtime)
        if not (
            status.bbr_enabled
            and status.fq_enabled
            and status.unattended_configured
            and status.apt_daily_timer_enabled
            and status.apt_daily_timer_active
            and status.apt_upgrade_timer_enabled
            and status.apt_upgrade_timer_active
            and status.unattended_service_enabled
            and status.unattended_service_active
        ):
            raise TransactionError(
                "Настройка хоста завершилась не полностью; "
                "проверьте rwm system status и systemctl."
            )
        return status
    except BaseException as error:
        rollback_errors: list[str] = []
        for path, snapshot in snapshots.items():
            try:
                if snapshot is None:
                    path.unlink(missing_ok=True)
                else:
                    payload, mode = snapshot
                    atomic_write_text(path, payload, mode=mode)
            except BaseException as rollback_error:  # noqa: BLE001 - continue independent compensation
                rollback_errors.append(f"восстановление {path}: {rollback_error}")
        for name, value in previous_sysctl.items():
            try:
                runner.run(
                    ["sysctl", "-w", f"{name}={value}"],
                    timeout=30,
                )
            except BaseException as rollback_error:  # noqa: BLE001 - continue independent compensation
                rollback_errors.append(f"восстановление sysctl {name}: {rollback_error}")
        for unit, state in timer_states.items():
            try:
                _restore_unit_state(runner, unit, state)
            except BaseException as rollback_error:  # noqa: BLE001 - continue independent compensation
                rollback_errors.append(
                    f"восстановление {unit}: {rollback_error}"
                )
        try:
            _restore_unit_state(runner, _UNATTENDED_UNIT, unattended_state)
        except BaseException as rollback_error:  # noqa: BLE001 - continue independent compensation
            rollback_errors.append(
                f"восстановление {_UNATTENDED_UNIT}: {rollback_error}"
            )
        if rollback_errors:
            raise TransactionError(
                "Настройка хоста завершилась ошибкой, rollback неполон: "
                + "; ".join(rollback_errors)
                + f". Исходная ошибка: {error}"
            ) from error
        if isinstance(error, ManagerError):
            raise
        raise TransactionError(
            f"Настройка хоста не выполнена; исходное состояние восстановлено: {error}"
        ) from error
