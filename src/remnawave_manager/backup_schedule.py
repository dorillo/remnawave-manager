from __future__ import annotations

import os
import re
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from .errors import TransactionError, ValidationError
from .runner import Runner, atomic_write_bytes, atomic_write_text
from .state import StateStore

Frequency = Literal["daily", "weekly"]
TimerEnablement = Literal[
    "enabled",
    "enabled-runtime",
    "disabled",
    "masked",
    "masked-runtime",
    "static",
    "indirect",
    "not-found",
]
TimerState = tuple[TimerEnablement, bool]
SERVICE_NAME = "remnawave-manager-backup.service"
TIMER_NAME = "remnawave-manager-backup.timer"
_MARKER = "X-Remnawave-Manager=true"
_MAX_SNAPSHOT_FILE_SIZE = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class BackupSchedule:
    enabled: bool
    active: bool
    frequency: str | None
    time: str | None
    retention: int | None
    next_run: str | None


def _unit_paths(store: StateStore) -> tuple[Path, Path]:
    root = store.paths.root / "etc/systemd/system"
    return root / SERVICE_NAME, root / TIMER_NAME


def _validate_time(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"([01][0-9]|2[0-3]):([0-5][0-9])", value)
    if not match:
        raise ValidationError("Время backup должно иметь формат ЧЧ:ММ от 00:00 до 23:59.")
    return int(match.group(1)), int(match.group(2))


def render_backup_units(
    *,
    frequency: Frequency,
    time_of_day: str,
    retention: int,
) -> tuple[str, str]:
    if frequency not in {"daily", "weekly"}:
        raise ValidationError("Частота backup должна быть daily или weekly.")
    hour, minute = _validate_time(time_of_day)
    if not 1 <= retention <= 1000:
        raise ValidationError("Число хранимых backup должно быть от 1 до 1000.")
    calendar = (
        f"*-*-* {hour:02d}:{minute:02d}:00"
        if frequency == "daily"
        else f"Sun *-*-* {hour:02d}:{minute:02d}:00"
    )
    service = f"""[Unit]
Description=Локальный backup Remnawave Manager
Requires=docker.service
After=docker.service
{_MARKER}

[Service]
Type=oneshot
ExecStart=/usr/local/bin/rwm backup create --reason scheduled --retention {retention}
TimeoutStartSec=infinity
User=root
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=read-only
ProtectSystem=strict
RuntimeDirectory=remnawave-manager
RuntimeDirectoryMode=0700
RuntimeDirectoryPreserve=yes
ReadWritePaths=/etc/remnawave-manager /var/backups/remnawave-manager /var/lib/remnawave-manager /var/log/remnawave-manager /run/remnawave-manager /tmp
ProtectKernelTunables=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
UMask=0077
"""
    timer = f"""[Unit]
Description=Расписание локального backup Remnawave Manager
{_MARKER}

[Timer]
OnCalendar={calendar}
Persistent=true
AccuracySec=1min
RandomizedDelaySec=10min
Unit={SERVICE_NAME}

[Install]
WantedBy=timers.target
"""
    return service, timer


def _read_snapshot_file(path: Path) -> tuple[bytes, int]:
    try:
        before = path.lstat()
    except OSError as error:
        raise ValidationError(
            f"Не удалось проверить файл расписания backup {path}."
        ) from error
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValidationError(
            f"Файл расписания backup должен быть обычным файлом без hardlink: {path}"
        )
    if before.st_size > _MAX_SNAPSHOT_FILE_SIZE:
        raise ValidationError(f"Файл расписания backup слишком велик: {path}")
    if os.name == "posix":
        if before.st_uid != os.geteuid():
            raise ValidationError(
                f"Файл расписания backup принадлежит другому пользователю: {path}"
            )
        if stat.S_IMODE(before.st_mode) & 0o022:
            raise ValidationError(
                f"Файл расписания backup доступен для записи группе/прочим: {path}"
            )

    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValidationError(
            f"Не удалось безопасно открыть файл расписания backup {path}."
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size > _MAX_SNAPSHOT_FILE_SIZE
        ):
            raise ValidationError(
                f"Файл расписания backup был подменён или имеет небезопасный тип: {path}"
            )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            payload = stream.read(_MAX_SNAPSHOT_FILE_SIZE + 1)
            after_open = os.fstat(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(payload) > _MAX_SNAPSHOT_FILE_SIZE:
        raise ValidationError(f"Файл расписания backup слишком велик: {path}")
    try:
        after_path = path.lstat()
    except OSError as error:
        raise ValidationError(
            f"Файл расписания backup исчез во время чтения: {path}"
        ) from error
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
        "st_nlink",
    )
    if (
        len(payload) != opened.st_size
        or any(
            getattr(opened, field) != getattr(after_open, field)
            or getattr(before, field) != getattr(after_path, field)
            for field in stable_fields
        )
    ):
        raise ValidationError(
            f"Файл расписания backup изменился во время чтения: {path}"
        )
    return payload, stat.S_IMODE(opened.st_mode)


def _snapshot(paths: tuple[Path, ...]) -> dict[Path, tuple[bytes, int] | None]:
    snapshots: dict[Path, tuple[bytes, int] | None] = {}
    for path in paths:
        try:
            path.lstat()
        except FileNotFoundError:
            snapshots[path] = None
            continue
        except OSError as error:
            raise ValidationError(
                f"Не удалось проверить путь при снимке расписания backup: {path}"
            ) from error
        snapshots[path] = _read_snapshot_file(path)
    return snapshots


def _has_marker(path: Path) -> bool:
    payload, _mode = _read_snapshot_file(path)
    return _MARKER in payload.decode("utf-8", errors="replace").splitlines()


def _assert_manager_owned(paths: Sequence[Path]) -> None:
    for path in paths:
        if path.is_symlink():
            raise ValidationError(f"Unit {path} является символьной ссылкой; изменение запрещено.")
        if path.exists() and not path.is_file():
            raise ValidationError(f"Путь unit {path} не является обычным файлом.")
        if path.is_file() and not _has_marker(path):
            raise ValidationError(
                f"Unit {path} создан не менеджером; автоматическая перезапись запрещена."
            )


def _restore(snapshot: dict[Path, tuple[bytes, int] | None]) -> None:
    errors: list[str] = []
    for path, saved in snapshot.items():
        try:
            if path.name in {SERVICE_NAME, TIMER_NAME}:
                _assert_manager_owned((path,))
            if saved is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                atomic_write_bytes(path, saved[0], mode=saved[1])
        except BaseException as error:  # noqa: BLE001 - continue independent rollback
            errors.append(f"{path}: {str(error) or type(error).__name__}")
    if errors:
        raise TransactionError(
            "Не удалось полностью восстановить файлы расписания backup: "
            + "; ".join(errors)
        )


def _timer_enablement(runner: Runner) -> TimerEnablement:
    enabled = runner.run(
        ["systemctl", "is-enabled", TIMER_NAME], check=False, timeout=30
    )
    enablement = enabled.stdout.strip().lower()
    supported_enablement = {
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
        enablement in {"enabled", "enabled-runtime"}
        and enabled.returncode == 0
        or enablement not in {"enabled", "enabled-runtime", "not-found"}
        and enabled.returncode in {0, 1}
        or enablement == "not-found"
        and enabled.returncode in {1, 4}
    )
    if enablement not in supported_enablement or not enabled_code_valid:
        raise ValidationError("Не удалось определить исходное состояние backup timer.")
    return cast(TimerEnablement, enablement)


def _timer_active(runner: Runner, *, enablement: TimerEnablement) -> bool:
    active = runner.run(
        ["systemctl", "is-active", TIMER_NAME], check=False, timeout=30
    )
    active_text = active.stdout.strip().lower()
    if active.returncode == 0 and active_text == "active":
        is_active = True
    elif (
        active.returncode == 3
        and active_text in {"inactive", "failed"}
        or enablement == "not-found"
        and active.returncode in {3, 4}
        and active_text in {"inactive", "unknown"}
    ):
        is_active = False
    else:
        raise ValidationError("Не удалось определить исходное состояние backup timer.")
    return is_active


def _timer_state(runner: Runner) -> TimerState:
    enablement = _timer_enablement(runner)
    return enablement, _timer_active(runner, enablement=enablement)


def _restore_timer_state(
    runner: Runner,
    *,
    enablement: TimerEnablement,
    active: bool,
    unit_existed: bool,
) -> None:
    errors: list[str] = []

    def run_step(label: str, args: list[str], *, check: bool = True) -> None:
        try:
            runner.run(args, check=check, timeout=120)
        except BaseException as error:  # noqa: BLE001 - continue independent rollback
            errors.append(f"{label}: {str(error) or type(error).__name__}")

    can_address_unit = unit_existed or active
    run_step(
        "runtime unmask",
        ["systemctl", "unmask", "--runtime", TIMER_NAME],
        check=can_address_unit,
    )
    run_step(
        "persistent unmask",
        ["systemctl", "unmask", TIMER_NAME],
        check=can_address_unit,
    )
    # Restore activity before reapplying a possible mask.
    run_step(
        "active",
        ["systemctl", "start" if active else "stop", TIMER_NAME],
        check=can_address_unit,
    )
    if enablement == "enabled-runtime":
        run_step("удаление persistent enablement", ["systemctl", "disable", TIMER_NAME])
        run_step(
            "runtime enablement",
            ["systemctl", "enable", "--runtime", TIMER_NAME],
        )
    elif enablement == "enabled":
        run_step("enablement", ["systemctl", "enable", TIMER_NAME])
    elif enablement in {"masked", "masked-runtime"}:
        run_step("удаление enablement", ["systemctl", "disable", TIMER_NAME])
        command = ["systemctl", "mask"]
        if enablement == "masked-runtime":
            command.append("--runtime")
        command.append(TIMER_NAME)
        run_step("mask", command)
    else:
        run_step(
            "disablement",
            ["systemctl", "disable", TIMER_NAME],
            check=unit_existed or enablement != "not-found",
        )

    actual_enablement: TimerEnablement | None = None
    actual_active: bool | None = None
    try:
        actual_enablement = _timer_enablement(runner)
    except BaseException as error:  # noqa: BLE001 - verify activity independently
        errors.append(
            f"enablement verification: {str(error) or type(error).__name__}"
        )
    try:
        actual_active = _timer_active(runner, enablement=enablement)
    except BaseException as error:  # noqa: BLE001 - preserve both verification failures
        errors.append(f"active verification: {str(error) or type(error).__name__}")
    if actual_enablement is not None and actual_enablement != enablement:
        errors.append(
            f"enablement verification: {actual_enablement!r} вместо {enablement!r}"
        )
    if actual_active is not None and actual_active != active:
        errors.append(
            f"active verification: {actual_active!r} вместо {active!r}"
        )
    if errors:
        raise TransactionError(
            "Backup timer не вернулся в исходное состояние: " + "; ".join(errors)
        )


def restore_backup_schedule_runtime(
    runner: Runner,
    state: TimerState,
) -> None:
    _restore_timer_state(
        runner,
        enablement=state[0],
        active=state[1],
        unit_existed=True,
    )


def backup_schedule_runtime_state(runner: Runner) -> TimerState:
    return _timer_state(runner)


def _rollback_schedule(
    runner: Runner,
    snapshot: dict[Path, tuple[bytes, int] | None],
    timer_state: TimerState,
    timer_path: Path,
) -> list[str]:
    errors: list[str] = []
    try:
        _restore(snapshot)
    except BaseException as error:  # noqa: BLE001 - continue independent rollback
        errors.append(f"файлы: {error}")
    try:
        runner.run(["systemctl", "daemon-reload"])
    except BaseException as error:  # noqa: BLE001 - continue independent rollback
        errors.append(f"daemon-reload: {str(error) or type(error).__name__}")
    try:
        _restore_timer_state(
            runner,
            enablement=timer_state[0],
            active=timer_state[1],
            unit_existed=snapshot[timer_path] is not None,
        )
    except BaseException as error:  # noqa: BLE001 - continue independent rollback
        errors.append(f"runtime timer: {error}")
    return errors


def install_backup_schedule(
    runner: Runner,
    store: StateStore,
    *,
    frequency: Frequency,
    time_of_day: str,
    retention: int,
) -> BackupSchedule:
    store.load_inventory()
    store.initialize()
    service_text, timer_text = render_backup_units(
        frequency=frequency,
        time_of_day=time_of_day,
        retention=retention,
    )
    paths = _unit_paths(store)
    _assert_manager_owned(paths)
    snapshot = _snapshot((*paths, store.paths.settings))
    timer_state = _timer_state(runner)
    settings = store.load_settings()
    settings["backup_retention"] = retention
    settings["backup_schedule"] = {
        "frequency": frequency,
        "time": time_of_day,
        "retention": retention,
    }
    try:
        paths[0].parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        atomic_write_text(paths[0], service_text, mode=0o644)
        atomic_write_text(paths[1], timer_text, mode=0o644)
        runner.run(["systemctl", "daemon-reload"])
        runner.run(["systemctl", "enable", "--now", TIMER_NAME])
        runner.run(["systemctl", "restart", TIMER_NAME])
        if _timer_state(runner) != ("enabled", True):
            raise TransactionError("Backup timer не перешёл в enabled/active-состояние.")
        store.save_settings(settings)
    except BaseException as error:
        rollback_errors = _rollback_schedule(runner, snapshot, timer_state, paths[1])
        if rollback_errors:
            raise TransactionError(
                "Настройка расписания backup завершилась ошибкой, rollback неполон: "
                + "; ".join(rollback_errors)
            ) from error
        raise
    return backup_schedule_status(runner, store)


def remove_backup_schedule(runner: Runner, store: StateStore) -> None:
    paths = _unit_paths(store)
    _assert_manager_owned(paths)
    snapshot = _snapshot((*paths, store.paths.settings))
    timer_state = _timer_state(runner)
    settings = store.load_settings()
    settings.pop("backup_schedule", None)
    try:
        runner.run(["systemctl", "disable", "--now", TIMER_NAME], check=False)
        if _timer_state(runner) != ("disabled", False):
            raise TransactionError("Backup timer остался enabled или active.")
        for path in paths:
            path.unlink(missing_ok=True)
        runner.run(["systemctl", "daemon-reload"])
        store.save_settings(settings)
    except BaseException as error:
        rollback_errors = _rollback_schedule(runner, snapshot, timer_state, paths[1])
        if rollback_errors:
            raise TransactionError(
                "Удаление расписания backup завершилось ошибкой, rollback неполон: "
                + "; ".join(rollback_errors)
            ) from error
        raise


def backup_schedule_status(runner: Runner, store: StateStore) -> BackupSchedule:
    settings = store.load_settings().get("backup_schedule")
    configured = settings if isinstance(settings, dict) else {}
    enablement, active = _timer_state(runner)
    next_result = runner.run(
        ["systemctl", "show", TIMER_NAME, "--property=NextElapseUSecRealtime", "--value"],
        check=False,
    )
    next_run = next_result.stdout.strip() if next_result.returncode == 0 else ""
    retention = configured.get("retention")
    return BackupSchedule(
        enabled=enablement in {"enabled", "enabled-runtime"},
        active=active,
        frequency=str(configured["frequency"]) if configured.get("frequency") else None,
        time=str(configured["time"]) if configured.get("time") else None,
        retention=int(retention) if isinstance(retention, int) else None,
        next_run=next_run or None,
    )
