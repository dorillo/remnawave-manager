from __future__ import annotations

import os
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .backup import BackupResult, create_backup
from .backup_schedule import (
    TimerState,
    backup_schedule_runtime_state,
    install_backup_schedule,
    remove_backup_schedule,
    render_backup_units,
    restore_backup_schedule_runtime,
)
from .compose import compose_command
from .errors import TransactionError, ValidationError
from .integrity import configuration_drift
from .journal import TransactionJournal
from .runner import Runner, ensure_within
from .state import StateStore, utc_now


@dataclass(frozen=True, slots=True)
class ArchivedStack:
    role: str
    backup: Path
    directory: Path
    inventory: Path
    secrets: Path | None
    backup_schedule_disabled: bool


@dataclass(frozen=True, slots=True)
class _ServiceSnapshot:
    created: frozenset[str]
    running: frozenset[str]


def _service_names(
    runner: Runner,
    compose_file: Path,
    env_file: Path | None,
    *,
    all_containers: bool,
) -> set[str]:
    arguments = ["ps", "--services"]
    if all_containers:
        arguments.append("--all")
    else:
        arguments.extend(("--status", "running"))
    result = runner.run(
        compose_command(
            compose_file,
            *arguments,
            env_file=env_file,
        ),
        cwd=compose_file.parent,
    )
    services = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", item) for item in services):
        raise TransactionError("Docker Compose вернул некорректное имя сервиса.")
    return services


def _service_snapshot(
    runner: Runner,
    compose_file: Path,
    env_file: Path | None,
) -> _ServiceSnapshot:
    created_before = _service_names(
        runner, compose_file, env_file, all_containers=True
    )
    running = _service_names(
        runner, compose_file, env_file, all_containers=False
    )
    created_after = _service_names(
        runner, compose_file, env_file, all_containers=True
    )
    if created_before != created_after or not running <= created_after:
        raise TransactionError(
            "Состояние Compose-сервисов изменяется параллельно; операция остановлена."
        )
    return _ServiceSnapshot(
        created=frozenset(created_after),
        running=frozenset(running),
    )


def _restore_service_snapshot(
    runner: Runner,
    compose_file: Path,
    env_file: Path | None,
    expected: _ServiceSnapshot,
) -> None:
    current = _service_snapshot(runner, compose_file, env_file)
    missing = set(expected.created - current.created)
    if missing:
        runner.run(
            compose_command(
                compose_file,
                "up",
                "--no-start",
                "--no-deps",
                "--pull",
                "never",
                *sorted(missing),
                env_file=env_file,
            ),
            cwd=compose_file.parent,
        )
    current = _service_snapshot(runner, compose_file, env_file)
    to_stop = set((current.running & expected.created) - expected.running)
    if to_stop:
        runner.run(
            compose_command(
                compose_file,
                "stop",
                *sorted(to_stop),
                env_file=env_file,
            ),
            cwd=compose_file.parent,
        )
    current = _service_snapshot(runner, compose_file, env_file)
    to_start = set(expected.running - current.running)
    if to_start:
        runner.run(
            compose_command(
                compose_file,
                "start",
                *sorted(to_start),
                env_file=env_file,
            ),
            cwd=compose_file.parent,
        )
    final = _service_snapshot(runner, compose_file, env_file)
    if final != expected:
        raise TransactionError(
            "Не удалось вернуть исходное состояние Compose-сервисов: "
            f"ожидалось created={sorted(expected.created)}, "
            f"running={sorted(expected.running)}; "
            f"получено created={sorted(final.created)}, running={sorted(final.running)}."
        )


def _restore_schedule_runtime(runner: Runner, state: TimerState) -> None:
    restore_backup_schedule_runtime(runner, state)


def _sync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _rename_and_sync(source: Path, destination: Path) -> None:
    source.rename(destination)
    _sync_directory(destination.parent)


def _replace_and_sync(source: Path, destination: Path) -> None:
    os.replace(source, destination)
    _sync_directory(destination.parent)


def archive_stack(runner: Runner, store: StateStore) -> ArchivedStack:
    if store.paths.inventory.is_symlink() or not store.paths.inventory.is_file():
        raise ValidationError(
            "Inventory отсутствует или имеет небезопасный тип; архивирование запрещено."
        )
    if store.paths.secrets.is_symlink() or (
        store.paths.secrets.exists() and not store.paths.secrets.is_file()
    ):
        raise ValidationError(
            "Файл secrets имеет небезопасный тип; архивирование запрещено."
        )
    secrets_present = store.paths.secrets.exists()
    secrets_snapshot = store.load_secrets() if secrets_present else None
    inventory = store.load_inventory()
    expected = store.paths.root / (
        "opt/remnawave" if inventory.role == "panel" else "opt/remnanode"
    )
    directory = Path(inventory.install_dir)
    if directory.is_symlink() or not directory.is_dir():
        raise ValidationError(f"Каталог установки имеет небезопасный тип: {directory}")
    if directory.resolve() != expected.resolve():
        raise ValidationError(
            "Автоматическое архивирование разрешено только для стандартного каталога "
            f"{expected}. Нестандартную установку удалите вручную после backup."
        )
    compose_file = Path(inventory.compose_file)
    ensure_within(compose_file, directory)
    if not compose_file.is_file() or compose_file.is_symlink():
        raise ValidationError("Compose-файл отсутствует или имеет небезопасный тип.")
    TransactionJournal.ensure_available(store)
    drift = configuration_drift(inventory)
    if drift:
        raise ValidationError(
            "Перед архивированием зафиксируйте текущую конфигурацию через rwm adopt: "
            + "; ".join(drift)
        )

    env_file = Path(inventory.env_file) if inventory.env_file else None
    runner.run(
        compose_command(compose_file, "config", "-q", env_file=env_file),
        cwd=directory,
    )
    backup: BackupResult = create_backup(
        runner, store, reason="pre-stack-archive", retention=None
    )

    if store.load_inventory() != inventory or configuration_drift(inventory):
        raise ValidationError(
            "Inventory или managed-файлы изменились во время создания backup; "
            "архивирование остановлено. Повторите adopt после проверки изменений."
        )
    if store.paths.secrets.exists() != secrets_present:
        raise ValidationError(
            "Файл secrets появился или исчез во время создания backup; "
            "архивирование остановлено."
        )
    if secrets_present and store.load_secrets() != secrets_snapshot:
        raise ValidationError(
            "Файл secrets изменился во время создания backup; архивирование остановлено."
        )

    settings = store.load_settings()
    raw_schedule = settings.get("backup_schedule")
    schedule = raw_schedule if isinstance(raw_schedule, dict) else None
    schedule_runtime: TimerState | None = None
    if schedule is not None:
        frequency = schedule.get("frequency")
        time_of_day = schedule.get("time")
        retention = schedule.get("retention")
        if (
            frequency not in {"daily", "weekly"}
            or not isinstance(time_of_day, str)
            or not isinstance(retention, int)
        ):
            raise ValidationError(
                "Настройки расписания backup повреждены; сначала исправьте или отключите расписание."
            )
        render_backup_units(
            frequency=frequency,
            time_of_day=time_of_day,
            retention=retention,
        )
        schedule_runtime = backup_schedule_runtime_state(runner)
        if schedule_runtime[0] == "not-found":
            raise ValidationError(
                "Расписание backup записано в manager settings, но systemd timer "
                "отсутствует; сначала переустановите или отключите расписание."
            )

    services_before = _service_snapshot(runner, compose_file, env_file)
    stamp = utc_now().replace(":", "").replace("+00:00", "Z").replace("-", "")
    suffix = f"{stamp}-{uuid.uuid4().hex[:8]}"
    archived_directory = directory.with_name(f"{directory.name}.removed-{suffix}")
    archived_inventory = store.paths.state / f"inventory.removed-{suffix}.json"
    archived_secrets = store.paths.etc / f"secrets.removed-{suffix}.json"
    for target in (archived_directory, archived_inventory, archived_secrets):
        if target.exists() or target.is_symlink():
            raise ValidationError(f"Архивный путь уже занят: {target}")

    schedule_disabled = False
    directory_moved = False
    inventory_moved = False
    secrets_moved = False
    journal = TransactionJournal(store, "stack-archive", backup.path)
    try:
        journal.set_archive_metadata(
            install_directory=(directory, archived_directory),
            inventory=(store.paths.inventory, archived_inventory),
            secrets=(store.paths.secrets, archived_secrets)
            if secrets_present
            else None,
            created_services=services_before.created,
            running_services=services_before.running,
        )
        if schedule is not None:
            journal.phase("disabling-backup-schedule")
            remove_backup_schedule(runner, store)
            schedule_disabled = True
        journal.phase("stopping-stack")
        runner.run(
            compose_command(
                compose_file,
                "down",
                env_file=env_file,
            ),
            cwd=directory,
        )
        remaining = _service_snapshot(runner, compose_file, env_file)
        if remaining.created or remaining.running:
            raise TransactionError(
                "После docker compose down остались контейнеры Compose-проекта: "
                + ", ".join(sorted(remaining.created | remaining.running))
            )
        journal.phase("moving-install-directory")
        directory.rename(archived_directory)
        directory_moved = True
        _sync_directory(archived_directory.parent)
        journal.phase("moving-manager-state")
        os.replace(store.paths.inventory, archived_inventory)
        inventory_moved = True
        _sync_directory(archived_inventory.parent)
        if secrets_present:
            if store.paths.secrets.is_symlink() or not store.paths.secrets.is_file():
                raise TransactionError(
                    "Файл secrets изменил тип или исчез после preflight; "
                    "архивирование остановлено."
                )
            os.replace(store.paths.secrets, archived_secrets)
            secrets_moved = True
            _sync_directory(archived_secrets.parent)
        elif store.paths.secrets.exists() or store.paths.secrets.is_symlink():
            raise TransactionError(
                "Файл secrets появился после preflight и не был перемещён; "
                "архивирование остановлено."
            )
        journal.phase("committed")
        journal.complete()
    except BaseException as error:
        rollback_errors: list[str] = []

        try:
            journal.phase("rolling-back")
        except BaseException as rollback_error:  # noqa: BLE001 - continue data rollback
            rollback_errors.append(f"journal: {rollback_error}")

        def rollback(label: str, action: Callable[[], object]) -> None:
            try:
                action()
            except BaseException as rollback_error:  # noqa: BLE001 - continue rollback
                rollback_errors.append(f"{label}: {rollback_error}")

        if secrets_moved:
            rollback(
                "возврат secrets",
                lambda: _replace_and_sync(archived_secrets, store.paths.secrets),
            )
        if inventory_moved:
            rollback(
                "возврат inventory",
                lambda: _replace_and_sync(archived_inventory, store.paths.inventory),
            )
        if directory_moved:
            rollback(
                "возврат каталога",
                lambda: _rename_and_sync(archived_directory, directory),
            )
        if directory.exists():
            rollback(
                "возврат состояния Compose",
                lambda: _restore_service_snapshot(
                    runner,
                    compose_file,
                    env_file,
                    services_before,
                ),
            )
        if schedule_disabled and schedule is not None:
            rollback(
                "возврат расписания backup",
                lambda: install_backup_schedule(
                    runner,
                    store,
                    frequency=schedule["frequency"],
                    time_of_day=schedule["time"],
                    retention=schedule["retention"],
                ),
            )
            if schedule_runtime is not None:
                rollback(
                    "возврат runtime-состояния backup timer",
                    lambda: _restore_schedule_runtime(runner, schedule_runtime),
                )
        if not rollback_errors:
            rollback("очистка journal", journal.complete)
        detail = f" Архив сохранён: {backup.path}."
        if rollback_errors:
            detail += " Ошибки rollback: " + "; ".join(rollback_errors)
        raise TransactionError(f"Стек не архивирован: {error}.{detail}") from error

    return ArchivedStack(
        role=inventory.role,
        backup=backup.path,
        directory=archived_directory,
        inventory=archived_inventory,
        secrets=archived_secrets if secrets_moved else None,
        backup_schedule_disabled=schedule_disabled,
    )
