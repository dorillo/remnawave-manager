from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import tarfile
import tempfile
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

from . import __version__
from .compat import detect_component_version
from .compose import compose_command, validate_rendered_compose
from .errors import TransactionError, ValidationError
from .health import (
    check_node_runtime,
    check_subscription_http,
    wait_container,
    wait_for_paths,
    wait_panel_http,
)
from .journal import TransactionJournal
from .models import Inventory
from .nginx import (
    activate_nginx_config,
    prepare_nginx_config,
    reload_nginx,
    test_nginx,
)
from .runner import Runner, atomic_write_bytes, ensure_within, sha256_file
from .state import StateStore, utc_now

_MAX_MANIFEST_SIZE = 16 * 1024 * 1024
_MAX_CONFIG_FILE_SIZE = 512 * 1024 * 1024
_MAX_DATABASE_DUMP_SIZE = 8 * 1024 * 1024 * 1024 * 1024
_MAX_ARCHIVE_CONTENT_SIZE = _MAX_DATABASE_DUMP_SIZE + 64 * 1024 * 1024 * 1024
_MAX_BACKUP_ARCHIVE_FILE_SIZE = _MAX_ARCHIVE_CONTENT_SIZE
_MIN_ARCHIVE_CONTENT_ALLOWANCE = 1024 * 1024 * 1024
_MAX_ARCHIVE_EXPANSION_RATIO = 1000
_MAX_ARCHIVE_MEMBERS = 100_000
_COPY_BLOCK_SIZE = 1024 * 1024
_MIN_FREE_SPACE_AFTER_EXTRACT = 256 * 1024 * 1024
_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class BackupResult:
    path: Path
    manifest: dict[str, Any]


@dataclass(slots=True)
class _PreparedRestoreFile:
    target: Path
    mode: int
    prepared: Path
    original: Path | None
    original_mode: int | None


@dataclass(slots=True)
class _DatabaseSwap:
    container: str
    user: str
    database: str
    previous_database: str


@dataclass(frozen=True, slots=True)
class _RecoveryJournal:
    path: Path
    device: int
    inode: int


@dataclass(frozen=True, slots=True)
class _ArchiveEntry:
    path: Path
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int


def _matching_recovery_journal(
    store: StateStore, backup_path: Path
) -> _RecoveryJournal | None:
    return _matching_recovery_journal_path(
        store.paths.state / "active-transaction.json", backup_path
    )


def _matching_recovery_journal_path(
    path: Path, backup_path: Path
) -> _RecoveryJournal | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_MANIFEST_SIZE:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("operation") not in {
            "panel-update",
            "node-update",
        }:
            return None
        saved_backup = data.get("backup")
        if not isinstance(saved_backup, str) or not Path(saved_backup).is_absolute():
            return None
        if Path(saved_backup).resolve() != backup_path.resolve():
            return None
        return _RecoveryJournal(path, info.st_dev, info.st_ino)
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        return None


def _clear_matching_recovery_journal(
    journal: _RecoveryJournal | None,
    backup_path: Path,
) -> None:
    if journal is None:
        return
    current = _matching_recovery_journal_path(journal.path, backup_path)
    if current is None or (current.device, current.inode) != (
        journal.device,
        journal.inode,
    ):
        return
    try:
        journal.path.unlink()
        if os.name == "posix":
            descriptor = os.open(journal.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    except OSError:
        # Restore уже committed; ошибка housekeeping не должна запускать rollback
        # после удаления проверенной предыдущей БД.
        return
def _archive_name_for(source: Path) -> str:
    absolute = source.resolve()
    return "files/rootfs/" + absolute.as_posix().lstrip("/")


def _postgres_identity(runner: Runner, container: str) -> tuple[str, str]:
    result = runner.run(
        ["docker", "inspect", "--format", "{{json .Config.Env}}", container],
        sensitive=True,
    )
    try:
        entries = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValidationError("Не удалось прочитать параметры PostgreSQL-контейнера.") from error
    if not isinstance(entries, list) or any(
        not isinstance(entry, str) for entry in entries
    ):
        raise ValidationError("Docker вернул некорректные параметры PostgreSQL-контейнера.")
    values: dict[str, str] = {}
    for entry in entries:
        if "=" in entry:
            key, value = entry.split("=", 1)
            values[key] = value
    user = values.get("POSTGRES_USER", "postgres")
    database = values.get("POSTGRES_DB", "postgres")
    valid = re.compile(r"[A-Za-z0-9_.-]{1,63}")
    if not valid.fullmatch(user) or not valid.fullmatch(database):
        raise ValidationError("Некорректные POSTGRES_USER/POSTGRES_DB.")
    return user, database


def _run_best_effort(
    runner: Runner,
    args: list[str],
    *,
    label: str,
    timeout: int = 120,
    sensitive: bool = True,
) -> bool:
    """Run non-critical cleanup without replacing the operation's real error."""
    try:
        result = runner.run(
            args,
            check=False,
            timeout=timeout,
            sensitive=sensitive,
        )
    except BaseException as error:  # noqa: BLE001 - cleanup must survive interrupts and preserve the primary error
        _LOGGER.warning("%s: %s", label, error)
        return False
    if result.returncode != 0:
        _LOGGER.warning("%s: команда завершилась с кодом %s", label, result.returncode)
        return False
    return True


def _create_database_dump(
    runner: Runner,
    inventory: Inventory,
    destination: Path,
) -> dict[str, str] | None:
    database = inventory.components.get("database")
    if inventory.role != "panel" or database is None:
        return None
    container = database.container or database.service
    user, db_name = _postgres_identity(runner, container)
    remote = f"/tmp/rwm-{uuid.uuid4().hex}.dump"  # noqa: S108, RUF100 - private path inside the database container
    try:
        runner.run(
            [
                "docker",
                "exec",
                container,
                "pg_dump",
                "--username",
                user,
                "--dbname",
                db_name,
                "--format=custom",
                "--compress=6",
                f"--file={remote}",
            ],
            timeout=1800,
            sensitive=True,
        )
        runner.run(
            ["docker", "exec", container, "pg_restore", "--list", remote],
            timeout=300,
            sensitive=True,
        )
        runner.run(["docker", "cp", f"{container}:{remote}", str(destination)], timeout=600)
        os.chmod(destination, 0o600)
    finally:
        _run_best_effort(
            runner,
            ["docker", "exec", container, "rm", "-f", remote],
            label="Не удалось удалить временный PostgreSQL dump после backup",
        )
    if not destination.is_file() or destination.stat().st_size < 1024:
        destination.unlink(missing_ok=True)
        raise TransactionError("Получен пустой или подозрительно маленький дамп PostgreSQL.")
    return {
        "archive_path": "database/panel.dump",
        "sha256": sha256_file(destination),
        "container": container,
        "user": user,
        "database": db_name,
        "format": "postgres-custom",
    }


def _supplemental_paths(inventory: Inventory) -> list[Path]:
    candidates: list[Path] = []
    if inventory.role == "node":
        candidates.extend(
            [
                Path("/etc/wireguard"),
                Path("/var/lib/remnawave-manager/warp"),
                Path("/usr/lib/remnawave-manager/bin"),
                Path("/etc/systemd/system/remnawave-warp.service"),
                Path("/etc/systemd/system/remnawave-warp-watchdog.service"),
                Path("/etc/systemd/system/remnawave-warp-watchdog.timer"),
                Path("/etc/systemd/system/remnawave-warp-health.service"),
                Path("/etc/systemd/system/remnawave-warp-health.timer"),
            ]
        )
    return [path for path in candidates if path.exists()]


def _iter_regular_files(root: Path) -> Iterable[Path]:
    if root.is_symlink():
        return
    if root.is_file():
        yield root
        return
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*"), key=str):
        if path.is_file() and not path.is_symlink():
            yield path


def _copy_regular_nofollow(
    source: Path,
    target: Path,
    *,
    limit: int | None = None,
    minimum_free_after: int = 0,
) -> tuple[str, int]:
    try:
        before = source.lstat()
    except OSError as error:
        raise ValidationError(f"Не удалось проверить файл backup {source}: {error}") from error
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValidationError(
            f"Источник backup должен быть обычным файлом без hardlink: {source}"
        )
    if limit is not None and before.st_size > limit:
        raise ValidationError(f"Источник backup превышает допустимый размер: {source}")
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source, flags)
    except OSError as error:
        raise ValidationError(
            f"Не удалось безопасно открыть источник backup {source}: {error}"
        ) from error
    opened = os.fstat(descriptor)
    if (
        not stat.S_ISREG(opened.st_mode)
        or opened.st_nlink != 1
        or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
    ):
        os.close(descriptor)
        raise ValidationError(f"Источник backup был подменён: {source}")
    if limit is not None and opened.st_size > limit:
        os.close(descriptor)
        raise ValidationError(f"Источник backup превышает допустимый размер: {source}")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if minimum_free_after:
        available = shutil.disk_usage(target.parent).free
        required = opened.st_size + minimum_free_after
        if available < required:
            os.close(descriptor)
            raise ValidationError(
                f"Недостаточно свободного места для безопасной копии backup: "
                f"требуется не менее {required} байт, доступно {available}."
            )
    digest = hashlib.sha256()
    copied = 0
    input_stream = os.fdopen(descriptor, "rb")
    descriptor = -1
    try:
        with input_stream, target.open("xb") as output_stream:
            for block in iter(lambda: input_stream.read(_COPY_BLOCK_SIZE), b""):
                copied += len(block)
                if copied > opened.st_size or (limit is not None and copied > limit):
                    raise TransactionError(
                        f"Источник backup вырос во время копирования: {source}"
                    )
                digest.update(block)
                output_stream.write(block)
            output_stream.flush()
            os.fsync(output_stream.fileno())
            after_open = os.fstat(input_stream.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        after_path = source.lstat()
    except OSError as error:
        target.unlink(missing_ok=True)
        raise ValidationError(f"Источник backup исчез во время копирования: {source}") from error
    stable_fields = (
        "st_dev",
        "st_ino",
        "st_size",
        "st_mtime_ns",
        "st_ctime_ns",
        "st_nlink",
    )
    if any(
        getattr(opened, field) != getattr(after_open, field)
        or getattr(before, field) != getattr(after_path, field)
        for field in stable_fields
    ) or copied != opened.st_size:
        target.unlink(missing_ok=True)
        raise TransactionError(f"Источник backup изменился во время копирования: {source}")
    os.chmod(target, 0o600)
    return digest.hexdigest(), stat.S_IMODE(opened.st_mode)


@contextmanager
def _open_regular_backup(path: Path) -> Iterator[tuple[BinaryIO, int]]:
    try:
        before = path.lstat()
    except FileNotFoundError as error:
        raise ValidationError(f"Backup не найден: {path}") from error
    except OSError as error:
        raise ValidationError(f"Не удалось проверить backup {path}: {error}") from error
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValidationError(
            f"Backup должен быть обычным файлом без symlink и hardlink: {path}"
        )
    if before.st_size > _MAX_BACKUP_ARCHIVE_FILE_SIZE:
        raise ValidationError(f"Backup превышает допустимый размер: {path}")

    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValidationError(f"Не удалось безопасно открыть backup {path}: {error}") from error

    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ValidationError(f"Backup был подменён или имеет небезопасный тип: {path}")
        if opened.st_size > _MAX_BACKUP_ARCHIVE_FILE_SIZE:
            raise ValidationError(f"Backup превышает допустимый размер: {path}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            try:
                yield stream, opened.st_size
            finally:
                after_open = os.fstat(stream.fileno())
                try:
                    after_path = path.lstat()
                except OSError as error:
                    raise ValidationError(
                        f"Backup исчез или был подменён во время чтения: {path}"
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
                    not stat.S_ISREG(after_open.st_mode)
                    or not stat.S_ISREG(after_path.st_mode)
                    or any(
                        getattr(opened, field) != getattr(after_open, field)
                        or getattr(before, field) != getattr(after_path, field)
                        for field in stable_fields
                    )
                ):
                    raise ValidationError(f"Backup изменился во время чтения: {path}")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


@contextmanager
def _open_backup_archive(path: Path) -> Iterator[tuple[tarfile.TarFile, int]]:
    with (
        _open_regular_backup(path) as (stream, compressed_size),
        tarfile.open(fileobj=stream, mode="r:gz") as archive,
    ):
        yield archive, compressed_size


def _validated_managed_sources(inventory: Inventory) -> list[tuple[Path, str]]:
    sources: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for item in inventory.managed_files:
        path = Path(item.path)
        if not path.is_absolute():
            raise ValidationError(f"Managed-файл содержит относительный путь: {path}.")
        if path.is_symlink():
            raise ValidationError(f"Managed-файл является символьной ссылкой: {path}.")
        resolved = path.resolve()
        if resolved in seen:
            raise ValidationError(f"Managed-файл повторяется в инвентаризации: {resolved}.")
        seen.add(resolved)
        if not re.fullmatch(r"[0-9a-f]{64}", item.sha256):
            raise ValidationError(f"Некорректная контрольная сумма managed-файла {path}.")
        if not path.is_file():
            raise ValidationError(
                f"Нельзя создать полный backup: managed-файл отсутствует: {path}. "
                f"Восстановите файл или повторите adoption командой rwm adopt --path {inventory.install_dir}."
            )
        try:
            actual = sha256_file(path)
        except OSError as error:
            raise ValidationError(f"Не удалось прочитать managed-файл {path}: {error}") from error
        if actual != item.sha256:
            raise ValidationError(
                f"Нельзя создать backup: managed-файл изменён после инвентаризации: {path}. "
                f"Проверьте изменения и повторите adoption командой rwm adopt --path {inventory.install_dir}."
            )
        sources.append((path, item.sha256))
    return sources


def create_backup(
    runner: Runner,
    store: StateStore,
    *,
    reason: str = "manual",
    retention: int | None = None,
) -> BackupResult:
    inventory = store.load_inventory()
    managed_sources = _validated_managed_sources(inventory)
    store.initialize()
    stamp = utc_now().replace(":", "").replace("+00:00", "Z").replace("-", "")
    backup_id = f"{stamp}-{inventory.role}-{uuid.uuid4().hex[:8]}"
    staging = store.paths.backups / f".{backup_id}.staging"
    ensure_within(staging, store.paths.backups)
    staging.mkdir(mode=0o700)
    operation_error: BaseException | None = None
    try:
        database_path = staging / "database" / "panel.dump"
        database_path.parent.mkdir(parents=True, mode=0o700)
        database = _create_database_dump(runner, inventory, database_path)

        sources: list[tuple[Path, bool, str | None]] = [
            (path, True, checksum) for path, checksum in managed_sources
        ]
        for root in _supplemental_paths(inventory):
            for path in _iter_regular_files(root):
                sources.append((path, False, None))

        seen: set[Path] = set()
        file_manifest: list[dict[str, Any]] = []
        for source, restore, expected_checksum in sources:
            resolved = source.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            archive_path = _archive_name_for(source)
            target = staging / PurePosixPath(archive_path)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            checksum, source_mode = _copy_regular_nofollow(source, target)
            if expected_checksum is not None and checksum != expected_checksum:
                raise TransactionError(
                    f"Managed-файл изменился во время создания backup: {source}. Backup отменён."
                )
            file_manifest.append(
                {
                    "source": str(source),
                    "archive_path": archive_path,
                    "sha256": checksum,
                    "mode": source_mode,
                    "restore": restore,
                }
            )

        manifest: dict[str, Any] = {
            "schema_version": 1,
            "backup_id": backup_id,
            "created_at": utc_now(),
            "reason": reason,
            "manager_version": __version__,
            "inventory": inventory.to_dict(),
            "files": file_manifest,
            "database": database,
        }
        manifest_data = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        atomic_write_bytes(staging / "manifest.json", manifest_data, mode=0o600)

        temporary_archive = store.paths.backups / f".{backup_id}.tar.gz.tmp"
        final_archive = store.paths.backups / f"{backup_id}.tar.gz"
        try:
            with tarfile.open(temporary_archive, "w:gz", compresslevel=6) as archive:
                archive.add(staging / "manifest.json", arcname="manifest.json", recursive=False)
                for item in file_manifest:
                    archive.add(
                        staging / PurePosixPath(item["archive_path"]),
                        arcname=item["archive_path"],
                        recursive=False,
                    )
                if database:
                    archive.add(database_path, arcname=database["archive_path"], recursive=False)
            os.chmod(temporary_archive, 0o600)
            flags = os.O_RDONLY
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(temporary_archive, flags)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise ValidationError(
                        "Временный backup имеет небезопасный тип перед публикацией."
                    )
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary_archive, final_archive)
            if os.name == "posix":
                descriptor = os.open(store.paths.backups, os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            verify_backup(final_archive)
        except BaseException as error:
            cleanup_errors: list[str] = []
            for artifact in (temporary_archive, final_archive):
                try:
                    artifact.unlink(missing_ok=True)
                except BaseException as cleanup_error:  # noqa: BLE001 - continue independent cleanup
                    cleanup_errors.append(
                        f"{artifact}: {cleanup_error or type(cleanup_error).__name__}"
                    )
            if cleanup_errors:
                raise TransactionError(
                    "Создание backup завершилось ошибкой, очистка неполна: "
                    + "; ".join(cleanup_errors)
                ) from error
            raise
    except BaseException as error:
        operation_error = error
        raise
    finally:
        try:
            try:
                staging_info = staging.lstat()
            except FileNotFoundError:
                staging_info = None
            if staging_info is not None:
                if not stat.S_ISDIR(staging_info.st_mode):
                    raise TransactionError(
                        f"Временный каталог backup был подменён: {staging}"
                    )
                ensure_within(staging, store.paths.backups)
                shutil.rmtree(staging)
        except BaseException as cleanup_error:
            if operation_error is None:
                raise TransactionError(
                    "Backup создан, но временные файлы удалить не удалось: "
                    f"{staging}"
                ) from cleanup_error
            _LOGGER.warning(
                "Не удалось удалить временные файлы после ошибки создания backup %s: %s",
                staging,
                cleanup_error or type(cleanup_error).__name__,
            )

    _apply_retention(store.paths.backups, inventory.role, retention)
    return BackupResult(final_archive, manifest)


def _safe_member(name: str) -> bool:
    path = PurePosixPath(name)
    return bool(name) and not path.is_absolute() and ".." not in path.parts


def _member(archive: tarfile.TarFile, name: str) -> tarfile.TarInfo:
    try:
        member = archive.getmember(name)
    except KeyError as error:
        raise ValidationError(f"В backup отсутствует {name}.") from error
    if not member.isfile() or not _safe_member(member.name):
        raise ValidationError(f"Недопустимый элемент backup: {name}")
    return member


def _read_member(
    archive: tarfile.TarFile,
    name: str,
    *,
    limit: int = _MAX_CONFIG_FILE_SIZE,
) -> bytes:
    member = _member(archive, name)
    if member.size < 0 or member.size > limit:
        raise ValidationError(f"Элемент backup {name} превышает допустимый размер.")
    stream = archive.extractfile(member)
    if stream is None:
        raise ValidationError(f"Не удалось прочитать {name} из backup.")
    with stream:
        payload = stream.read(limit + 1)
    if len(payload) != member.size or len(payload) > limit:
        raise ValidationError(f"Размер элемента backup {name} не совпадает с manifest tar.")
    return payload


def _hash_member(archive: tarfile.TarFile, name: str) -> str:
    member = _member(archive, name)
    stream = archive.extractfile(member)
    if stream is None:
        raise ValidationError(f"Не удалось прочитать {name} из backup.")
    digest = hashlib.sha256()
    size = 0
    with stream:
        for block in iter(lambda: stream.read(_COPY_BLOCK_SIZE), b""):
            size += len(block)
            digest.update(block)
    if size != member.size:
        raise ValidationError(f"Размер элемента backup {name} не совпадает с manifest tar.")
    return digest.hexdigest()


def verify_backup(path: Path) -> dict[str, Any]:
    try:
        with _open_backup_archive(path) as (archive, compressed_size):
            members: list[tarfile.TarInfo] = []
            content_size = 0
            content_limit = min(
                _MAX_ARCHIVE_CONTENT_SIZE,
                max(
                    _MIN_ARCHIVE_CONTENT_ALLOWANCE,
                    compressed_size * _MAX_ARCHIVE_EXPANSION_RATIO,
                ),
            )
            for member in archive:
                if len(members) >= _MAX_ARCHIVE_MEMBERS:
                    raise ValidationError("Backup содержит слишком много элементов.")
                if member.size < 0:
                    raise ValidationError(
                        f"Backup содержит элемент с некорректным размером: {member.name}"
                    )
                content_size += member.size
                if content_size > content_limit:
                    raise ValidationError(
                        "Суммарный размер содержимого backup превышает допустимый предел."
                    )
                members.append(member)
            names: set[str] = set()
            for member in members:
                if (
                    not _safe_member(member.name)
                    or not member.isfile()
                    or member.name in names
                ):
                    raise ValidationError(f"Backup содержит небезопасный элемент: {member.name}")
                names.add(member.name)
            try:
                manifest = json.loads(
                    _read_member(archive, "manifest.json", limit=_MAX_MANIFEST_SIZE)
                )
            except (json.JSONDecodeError, UnicodeError, RecursionError) as error:
                raise ValidationError("manifest.json в backup повреждён.") from error
            if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
                raise ValidationError("Версия backup не поддерживается.")
            raw_files = manifest.get("files", [])
            if not isinstance(raw_files, list):
                raise ValidationError("Список files в manifest повреждён.")
            entries: list[dict[str, Any]] = []
            for item in raw_files:
                if not isinstance(item, dict):
                    raise ValidationError("Элемент files в manifest повреждён.")
                entries.append(item)
            database = manifest.get("database")
            if database is not None:
                if not isinstance(database, dict):
                    raise ValidationError("Раздел database в manifest повреждён.")
                entries.append(database)

            expected_names = {"manifest.json"}
            config_paths = {
                archive_path
                for item in raw_files
                if isinstance(item, dict)
                and isinstance((archive_path := item.get("archive_path")), str)
            }
            database_path = (
                database.get("archive_path") if isinstance(database, dict) else None
            )
            for item in entries:
                archive_path = item.get("archive_path")
                checksum = item.get("sha256")
                if (
                    not isinstance(archive_path, str)
                    or not _safe_member(archive_path)
                    or not isinstance(checksum, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", checksum)
                    or archive_path in expected_names
                ):
                    raise ValidationError("Manifest backup содержит некорректный путь или checksum.")
                expected_names.add(archive_path)
                member_size = _member(archive, archive_path).size
                if archive_path in config_paths and member_size > _MAX_CONFIG_FILE_SIZE:
                    raise ValidationError(
                        f"Файл {archive_path} превышает допустимый размер backup."
                    )
                if (
                    archive_path == database_path
                    and member_size > _MAX_DATABASE_DUMP_SIZE
                ):
                    raise ValidationError(
                        "PostgreSQL dump превышает допустимый размер backup."
                    )
                actual = _hash_member(archive, archive_path)
                if actual != checksum:
                    raise ValidationError(f"Неверная контрольная сумма {archive_path}.")
            if names != expected_names:
                raise ValidationError("Backup содержит элементы, не зарегистрированные в manifest.")
            return manifest
    except (tarfile.TarError, OSError) as error:
        raise ValidationError(f"Не удалось прочитать backup {path}: {error}") from error


def list_backups(store: StateStore) -> list[Path]:
    return [entry.path for entry in _regular_backup_entries(store.paths.backups, missing_ok=True)]


def delete_backups(store: StateStore, paths: Iterable[Path]) -> list[Path]:
    requested = tuple(Path(path) for path in paths)
    if not requested:
        raise ValidationError("Укажите хотя бы один backup для удаления.")

    TransactionJournal.ensure_available(store)
    directory = store.paths.backups
    names: list[str] = []
    seen: set[str] = set()
    for path in requested:
        if path.is_absolute():
            if path.parent != directory:
                raise ValidationError(
                    f"Разрешено удалять backup только из каталога {directory}: {path}"
                )
        elif path.parent != Path(".") or not path.name:
            raise ValidationError(
                f"Укажите имя backup или путь непосредственно из каталога {directory}: {path}"
            )
        name = path.name
        if not name.endswith(".tar.gz"):
            raise ValidationError(f"Некорректное имя backup: {path}")
        if name in seen:
            raise ValidationError(f"Backup выбран повторно: {directory / name}")
        seen.add(name)
        names.append(name)

    with _open_backup_directory(directory, missing_ok=False) as (descriptor, _exists):
        entries = {
            entry.path.name: entry
            for entry in _scan_regular_backup_entries(directory, descriptor, role=None)
        }
        selected: list[_ArchiveEntry] = []
        for name in names:
            entry = entries.get(name)
            if entry is None:
                raise ValidationError(
                    f"Backup не найден или не является обычным single-link архивом: "
                    f"{directory / name}"
                )
            selected.append(entry)

        removed: list[Path] = []
        for entry in selected:
            _delete_archive_entry(
                directory,
                descriptor,
                entry,
                operation="ручным удалением",
                quarantine_prefix="delete",
                missing_ok=False,
            )
            removed.append(entry.path)
        if removed and descriptor is not None:
            os.fsync(descriptor)
        return removed


@contextmanager
def _open_backup_directory(
    directory: Path,
    *,
    missing_ok: bool,
) -> Iterator[tuple[int | None, bool]]:
    try:
        before = directory.lstat()
    except FileNotFoundError:
        if missing_ok:
            yield None, False
            return
        raise ValidationError(f"Каталог backup не найден: {directory}") from None
    except OSError as error:
        raise ValidationError(f"Не удалось проверить каталог backup {directory}: {error}") from error
    if not stat.S_ISDIR(before.st_mode):
        raise ValidationError(f"Каталог backup имеет небезопасный тип: {directory}")

    if os.name != "posix":
        yield None, True
        return

    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(directory, flags)
    except OSError as error:
        raise ValidationError(
            f"Не удалось безопасно открыть каталог backup {directory}: {error}"
        ) from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ValidationError(f"Каталог backup был подменён: {directory}")
        yield descriptor, True
    finally:
        os.close(descriptor)


def _scan_regular_backup_entries(
    directory: Path,
    descriptor: int | None,
    *,
    role: str | None,
) -> list[_ArchiveEntry]:
    scan_source: int | Path = descriptor if descriptor is not None else directory
    entries: list[_ArchiveEntry] = []
    try:
        with os.scandir(scan_source) as iterator:
            for candidate in iterator:
                name = candidate.name
                if not name.endswith(".tar.gz") or (role is not None and f"-{role}-" not in name):
                    continue
                path = directory / name
                try:
                    info = (
                        os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                        if descriptor is not None
                        else path.lstat()
                    )
                except FileNotFoundError:
                    continue
                except OSError as error:
                    raise ValidationError(
                        f"Не удалось проверить элемент каталога backup {path}: {error}"
                    ) from error
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    continue
                entries.append(
                    _ArchiveEntry(
                        path,
                        info.st_dev,
                        info.st_ino,
                        info.st_size,
                        info.st_mtime_ns,
                        info.st_ctime_ns,
                    )
                )
    except FileNotFoundError as error:
        raise ValidationError(f"Каталог backup исчез во время чтения: {directory}") from error
    except OSError as error:
        raise ValidationError(f"Не удалось прочитать каталог backup {directory}: {error}") from error
    return sorted(entries, key=lambda item: (item.mtime_ns, item.path.name), reverse=True)


def _regular_backup_entries(
    directory: Path,
    *,
    role: str | None = None,
    missing_ok: bool = False,
) -> list[_ArchiveEntry]:
    with _open_backup_directory(directory, missing_ok=missing_ok) as (descriptor, exists):
        if not exists:
            return []
        return _scan_regular_backup_entries(directory, descriptor, role=role)


def _delete_archive_entry(
    directory: Path,
    descriptor: int | None,
    entry: _ArchiveEntry,
    *,
    operation: str,
    quarantine_prefix: str,
    missing_ok: bool,
) -> bool:
    try:
        current = (
            os.stat(entry.path.name, dir_fd=descriptor, follow_symlinks=False)
            if descriptor is not None
            else entry.path.lstat()
        )
    except FileNotFoundError:
        if missing_ok:
            return False
        raise TransactionError(
            f"Backup исчез перед {operation}; удаление остановлено: {entry.path}"
        ) from None
    except OSError as error:
        raise TransactionError(
            f"Не удалось повторно проверить backup перед {operation}: "
            f"{entry.path}: {error}"
        ) from error
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or (current.st_dev, current.st_ino) != (entry.device, entry.inode)
        or current.st_size != entry.size
        or current.st_mtime_ns != entry.mtime_ns
        or current.st_ctime_ns != entry.ctime_ns
    ):
        raise TransactionError(
            f"Backup изменился перед {operation}; удаление отменено: {entry.path}"
        )

    quarantine_name = f".{quarantine_prefix}-{uuid.uuid4().hex}.tmp"
    quarantine_path = directory / quarantine_name
    try:
        if descriptor is None:
            os.rename(entry.path, quarantine_path)
        else:
            os.rename(
                entry.path.name,
                quarantine_name,
                src_dir_fd=descriptor,
                dst_dir_fd=descriptor,
            )
    except OSError as error:
        raise TransactionError(
            f"Не удалось изолировать backup перед удалением {entry.path}: {error}"
        ) from error
    try:
        quarantined = (
            os.stat(quarantine_name, dir_fd=descriptor, follow_symlinks=False)
            if descriptor is not None
            else quarantine_path.lstat()
        )
    except OSError as error:
        raise TransactionError(
            f"Не удалось проверить изолированный backup {quarantine_path}; "
            "автоматическое удаление отменено."
        ) from error
    if (
        not stat.S_ISREG(quarantined.st_mode)
        or quarantined.st_nlink != 1
        or (quarantined.st_dev, quarantined.st_ino) != (entry.device, entry.inode)
        or quarantined.st_size != entry.size
        or quarantined.st_mtime_ns != entry.mtime_ns
    ):
        raise TransactionError(
            f"Backup был подменён во время {operation}; подозрительный файл сохранён: "
            f"{quarantine_path}"
        )
    try:
        if descriptor is None:
            quarantine_path.unlink()
        else:
            os.unlink(quarantine_name, dir_fd=descriptor)
    except OSError as error:
        raise TransactionError(
            f"Не удалось удалить изолированный backup {quarantine_path}: {error}"
        ) from error
    return True


def _apply_retention(directory: Path, role: str, retention: int | None) -> None:
    if retention is None:
        return
    if retention < 1 or retention > 1000:
        raise ValidationError("Число хранимых backup должно быть от 1 до 1000.")
    with _open_backup_directory(directory, missing_ok=False) as (descriptor, _exists):
        archives = _scan_regular_backup_entries(directory, descriptor, role=role)
        removed = False
        for entry in archives[retention:]:
            removed = (
                _delete_archive_entry(
                    directory,
                    descriptor,
                    entry,
                    operation="retention",
                    quarantine_prefix="retention",
                    missing_ok=True,
                )
                or removed
            )
        if removed and descriptor is not None:
            os.fsync(descriptor)


def _extract_member(
    archive: tarfile.TarFile,
    name: str,
    target: Path,
    *,
    checksum: str,
    limit: int = _MAX_CONFIG_FILE_SIZE,
) -> None:
    member = _member(archive, name)
    if member.size < 0 or member.size > limit:
        raise ValidationError(f"Файл {name} превышает допустимый размер восстановления.")
    stream = archive.extractfile(member)
    if stream is None:
        raise ValidationError(f"Не удалось прочитать {name} из backup.")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    available = shutil.disk_usage(target.parent).free
    required = member.size + _MIN_FREE_SPACE_AFTER_EXTRACT
    if available < required:
        raise ValidationError(
            f"Недостаточно свободного места для восстановления {name}: "
            f"требуется не менее {required} байт, доступно {available}."
        )
    digest = hashlib.sha256()
    size = 0
    try:
        with stream, target.open("xb") as output:
            for block in iter(lambda: stream.read(_COPY_BLOCK_SIZE), b""):
                size += len(block)
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    if size != member.size or digest.hexdigest() != checksum:
        target.unlink(missing_ok=True)
        raise ValidationError(f"Файл {name} повреждён во время восстановления.")
    os.chmod(target, 0o600)


def _copy_file_atomic(source: Path, target: Path, *, mode: int) -> None:
    if not 0 <= mode <= 0o777:
        raise ValidationError(f"Некорректные права файла {target}.")
    if target.is_symlink():
        raise ValidationError(
            f"Путь восстановления является символьной ссылкой: {target}."
        )
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.restore-", dir=target.parent)
    temporary_path = Path(temporary)
    try:
        with source.open("rb") as input_stream, os.fdopen(descriptor, "wb") as output_stream:
            shutil.copyfileobj(input_stream, output_stream, length=_COPY_BLOCK_SIZE)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, target)
        if os.name == "posix":
            directory_fd = os.open(target.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)


def _restore_prepared_originals(items: Iterable[_PreparedRestoreFile]) -> None:
    errors: list[str] = []
    for item in items:
        try:
            if item.original is None:
                item.target.unlink(missing_ok=True)
            else:
                if item.original_mode is None:
                    raise TransactionError(
                        f"Не сохранены исходные права файла {item.target}."
                    )
                _copy_file_atomic(
                    item.original,
                    item.target,
                    mode=item.original_mode,
                )
        except BaseException as error:  # noqa: BLE001 - continue independent rollback
            errors.append(f"{item.target}: {error or type(error).__name__}")
    if errors:
        raise TransactionError(
            "Не удалось полностью вернуть исходные файлы: " + "; ".join(errors)
        )


def _running_component_services(
    runner: Runner,
    inventory: Inventory,
    compose_file: Path,
    env_file: Path | None,
) -> set[str]:
    result = runner.run(
        compose_command(
            compose_file,
            "ps",
            "--services",
            "--status",
            "running",
            env_file=env_file,
        ),
        cwd=compose_file.parent,
    )
    known = {component.service for component in inventory.components.values()}
    return {line.strip() for line in result.stdout.splitlines() if line.strip() in known}


def _validate_database_dump(
    runner: Runner,
    inventory: Inventory,
    database: dict[str, Any],
    dump_path: Path,
) -> None:
    component = inventory.components.get("database")
    if component is None:
        raise ValidationError("В текущей инвентаризации нет PostgreSQL-контейнера.")
    container = component.container or component.service
    user, db_name = _postgres_identity(runner, container)
    if user != database.get("user") or db_name != database.get("database"):
        raise ValidationError("POSTGRES_USER/POSTGRES_DB отличаются от сохранённого backup.")
    remote = f"/tmp/rwm-validate-{uuid.uuid4().hex}.dump"  # noqa: S108, RUF100 - private path inside the database container
    try:
        runner.run(["docker", "cp", str(dump_path), f"{container}:{remote}"], timeout=600)
        runner.run(
            ["docker", "exec", container, "pg_restore", "--list", remote],
            timeout=300,
            sensitive=True,
        )
    finally:
        _run_best_effort(
            runner,
            ["docker", "exec", container, "rm", "-f", remote],
            label="Не удалось удалить временный PostgreSQL dump после проверки",
        )


def _same_path(left: str | None, right: str | None) -> bool:
    if left is None or right is None:
        return left is right
    return Path(left).resolve() == Path(right).resolve()


def restore_backup(
    runner: Runner,
    store: StateStore,
    backup_path: Path,
    *,
    restore_database: bool = True,
    clear_recovery_journal: bool = True,
) -> None:
    # Keep one immutable, private copy for both manifest validation and extraction.
    # The caller-supplied path may live in a directory writable by another user.
    store.load_inventory()
    store.initialize()
    snapshot_dir = store.paths.backups / f".restore-source-{uuid.uuid4().hex}"
    ensure_within(snapshot_dir, store.paths.backups)
    snapshot_dir.mkdir(mode=0o700)
    snapshot_path = snapshot_dir / "source.tar.gz"
    operation_error: BaseException | None = None
    try:
        _copy_regular_nofollow(
            backup_path,
            snapshot_path,
            limit=_MAX_BACKUP_ARCHIVE_FILE_SIZE,
            minimum_free_after=_MIN_FREE_SPACE_AFTER_EXTRACT,
        )
        _restore_backup_snapshot(
            runner,
            store,
            snapshot_path,
            recovery_backup_path=backup_path,
            restore_database=restore_database,
            clear_recovery_journal=clear_recovery_journal,
        )
    except BaseException as error:
        operation_error = error
        raise
    finally:
        try:
            try:
                snapshot_info = snapshot_dir.lstat()
            except FileNotFoundError:
                snapshot_info = None
            if snapshot_info is not None:
                if not stat.S_ISDIR(snapshot_info.st_mode):
                    raise TransactionError(
                        f"Приватный каталог restore был подменён: {snapshot_dir}"
                    )
                ensure_within(snapshot_dir, store.paths.backups)
                shutil.rmtree(snapshot_dir)
        except BaseException as cleanup_error:
            if operation_error is None:
                raise TransactionError(
                    f"Restore завершён, но приватную копию backup удалить не удалось: "
                    f"{snapshot_dir}"
                ) from cleanup_error
            _LOGGER.warning(
                "Не удалось удалить приватную копию backup после ошибки restore %s: %s",
                snapshot_dir,
                cleanup_error,
            )


def _restore_backup_snapshot(
    runner: Runner,
    store: StateStore,
    backup_path: Path,
    *,
    recovery_backup_path: Path,
    restore_database: bool,
    clear_recovery_journal: bool,
) -> None:
    current = store.load_inventory()
    manifest = verify_backup(backup_path)
    recovery_journal = (
        _matching_recovery_journal(store, recovery_backup_path)
        if clear_recovery_journal
        else None
    )
    try:
        saved = Inventory.from_dict(manifest["inventory"])
    except (KeyError, TypeError, ValueError) as error:
        raise ValidationError("Inventory в backup повреждён.") from error
    if saved.role != current.role:
        raise ValidationError("Роль backup не совпадает с ролью этого сервера.")
    if (
        not _same_path(saved.install_dir, current.install_dir)
        or not _same_path(saved.compose_file, current.compose_file)
        or not _same_path(saved.env_file, current.env_file)
    ):
        raise ValidationError("Backup создан для другого каталога установки или compose/env.")
    if set(saved.components) != set(current.components):
        raise ValidationError("Набор компонентов backup не совпадает с текущей установкой.")
    for name, component in saved.components.items():
        active = current.components[name]
        if component.service != active.service or component.container != active.container:
            raise ValidationError(f"Идентификатор сервиса {name} в backup не совпадает.")

    current_nginx = {Path(path).resolve() for path in current.nginx_files}
    saved_nginx = {Path(path).resolve() for path in saved.nginx_files}
    site_roots = {Path(path).resolve() for path in current.site_dirs}
    saved_site_roots = {Path(path).resolve() for path in saved.site_dirs}
    if current_nginx != saved_nginx or site_roots != saved_site_roots:
        raise ValidationError("Пути nginx/site в backup не совпадают с текущей установкой.")
    allowed = {Path(item.path).resolve() for item in current.managed_files}
    saved_managed: dict[Path, Any] = {}
    for managed in saved.managed_files:
        resolved = Path(managed.path).resolve()
        if resolved in saved_managed:
            raise ValidationError(f"Inventory backup содержит повторяющийся managed-путь {managed.path}.")
        if not re.fullmatch(r"[0-9a-f]{64}", managed.sha256):
            raise ValidationError(f"Inventory backup содержит некорректный checksum {managed.path}.")
        if resolved not in allowed and not any(root in resolved.parents for root in site_roots):
            raise ValidationError(f"Inventory backup содержит незарегистрированный путь {managed.path}.")
        saved_managed[resolved] = managed

    restore_entries: list[tuple[dict[str, Any], Path, int]] = []
    restore_targets: set[Path] = set()
    for item in manifest.get("files", []):
        restore = item.get("restore")
        if not isinstance(restore, bool):
            raise ValidationError("Флаг restore в manifest должен быть boolean.")
        if not restore:
            continue
        source = item.get("source")
        if not isinstance(source, str) or not Path(source).is_absolute():
            raise ValidationError("Backup содержит некорректный путь восстановления.")
        target = Path(source)
        resolved = target.resolve()
        inside_site = any(root in resolved.parents for root in site_roots)
        if resolved not in allowed and not inside_site:
            raise ValidationError(f"Backup пытается восстановить незарегистрированный путь {target}.")
        managed = saved_managed.get(resolved)
        if managed is None:
            raise ValidationError(f"Backup пытается восстановить путь вне saved inventory: {target}.")
        if item.get("sha256") != managed.sha256:
            raise ValidationError(f"Checksum managed-файла {target} не совпадает с saved inventory.")
        if resolved in restore_targets:
            raise ValidationError(f"Backup содержит повторяющийся путь восстановления {target}.")
        restore_targets.add(resolved)
        expected_archive_path = _archive_name_for(target)
        if item.get("archive_path") != expected_archive_path:
            raise ValidationError(f"Путь {target} не соответствует archive_path в manifest.")
        try:
            mode = int(item.get("mode", 0o600))
        except (TypeError, ValueError) as error:
            raise ValidationError(f"Некорректные права файла {target} в manifest.") from error
        if not 0 <= mode <= 0o777:
            raise ValidationError(f"Некорректные права файла {target} в manifest.")
        restore_entries.append((item, target, mode))
    if restore_targets != set(saved_managed):
        missing = sorted(str(path) for path in set(saved_managed) - restore_targets)
        raise ValidationError(
            "Backup не содержит полный набор managed-файлов saved inventory"
            + (": " + ", ".join(missing) if missing else ".")
        )

    database = manifest.get("database")
    if restore_database and database and (
        database.get("archive_path") != "database/panel.dump"
        or database.get("format") != "postgres-custom"
        or not isinstance(database.get("user"), str)
        or not isinstance(database.get("database"), str)
    ):
        raise ValidationError("Метаданные PostgreSQL dump в manifest повреждены.")

    store.initialize()
    compose_file = Path(current.compose_file)
    env_file = Path(current.env_file) if current.env_file else None
    transaction_dir = store.paths.backups / f".restore-{uuid.uuid4().hex}"
    ensure_within(transaction_dir, store.paths.backups)
    transaction_dir.mkdir(mode=0o700)
    prepared_files: list[_PreparedRestoreFile] = []
    dump_path: Path | None = None
    database_swap: _DatabaseSwap | None = None
    running_services: set[str] = set()
    mutation_started = False
    inventory_saved = False
    transaction_committed = False
    rollback_data: dict[str, Any] = {
        "backup": str(recovery_backup_path),
        "files": [],
        "database": None,
    }

    def remove_transaction_dir() -> None:
        try:
            info = transaction_dir.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(info.st_mode):
            raise TransactionError(
                f"Каталог restore-транзакции был подменён: {transaction_dir}"
            )
        ensure_within(transaction_dir, store.paths.backups)
        shutil.rmtree(transaction_dir)

    def service_list(names: Iterable[str]) -> list[str]:
        selected = set(names)
        order = ("database", "panel", "subscription", "node", "nginx")
        ordered = [
            current.components[name].service
            for name in order
            if name in current.components and current.components[name].service in selected
        ]
        extras = sorted(selected - set(ordered))
        return [*ordered, *extras]

    def verify_running(inventory: Inventory, expected: set[str]) -> None:
        for name in ("panel", "subscription", "node", "nginx"):
            component = inventory.components.get(name)
            if component is None or component.service not in expected:
                continue
            wait_container(
                runner,
                component,
                timeout=600 if name == "panel" else 300,
                require_health=name == "panel",
            )
        panel = inventory.components.get("panel")
        if panel is not None and panel.service in expected:
            wait_panel_http(runner, panel)
        subscription = inventory.components.get("subscription")
        if subscription is not None and subscription.service in expected:
            subscription_version = detect_component_version(
                runner, "subscription", subscription
            )
            check_subscription_http(
                runner,
                subscription,
                legacy=subscription_version == "7.2.6",
            )
        node = inventory.components.get("node")
        if node is not None and node.service in expected:
            check_node_runtime(runner, inventory)
            wait_for_paths(inventory.xhttp_sockets)
        nginx = inventory.components.get("nginx")
        if nginx is None or nginx.service in expected:
            test_nginx(runner, inventory)
            if nginx is None:
                reload_nginx(runner, inventory)

    try:
        with _open_backup_archive(backup_path) as (archive, _compressed_size):
            for index, (item, target, mode) in enumerate(restore_entries):
                prepared = transaction_dir / "prepared" / f"{index:06d}"
                _extract_member(
                    archive,
                    item["archive_path"],
                    prepared,
                    checksum=item["sha256"],
                )
                original: Path | None = None
                original_mode: int | None = None
                if target.is_symlink():
                    raise ValidationError(
                        f"Путь восстановления является символьной ссылкой: {target}."
                    )
                if target.exists():
                    if not target.is_file():
                        raise ValidationError(f"Путь восстановления не является обычным файлом: {target}.")
                    original = transaction_dir / "original" / f"{index:06d}"
                    original.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    original_mode = target.stat().st_mode & 0o777
                    _copy_regular_nofollow(target, original)
                prepared_files.append(
                    _PreparedRestoreFile(target, mode, prepared, original, original_mode)
                )

            if restore_database and database:
                dump_path = transaction_dir / "database" / "panel.dump"
                _extract_member(
                    archive,
                    database["archive_path"],
                    dump_path,
                    checksum=database["sha256"],
                    limit=_MAX_DATABASE_DUMP_SIZE,
                )

        rollback_data["files"] = [
            {
                "target": str(item.target),
                "original": str(item.original) if item.original else None,
                "original_mode": item.original_mode,
            }
            for item in prepared_files
        ]
        atomic_write_bytes(
            transaction_dir / "rollback.json",
            json.dumps(rollback_data, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
            mode=0o600,
        )

        prepared_by_target = {item.target.resolve(): item.prepared for item in prepared_files}
        prepared_compose = prepared_by_target.get(compose_file.resolve())
        if prepared_compose is None:
            raise ValidationError("Backup не содержит managed compose-файл.")
        try:
            compose_payload = prepared_compose.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            raise ValidationError("Compose-файл в backup не является корректным UTF-8.") from error
        prepared_env = prepared_by_target.get(env_file.resolve()) if env_file is not None else None
        if env_file is not None and prepared_env is None:
            raise ValidationError("Backup не содержит managed env-файл.")
        # Compose проверяется на полностью подготовленных файлах до остановки сервисов.
        validate_rendered_compose(runner, compose_file, compose_payload, prepared_env)
        if dump_path is not None and database is not None:
            _validate_database_dump(runner, current, database, dump_path)

        running_services = _running_component_services(runner, current, compose_file, env_file)
        stop_order = ("subscription", "panel", "node", "nginx")
        services_to_stop = [
            current.components[name].service
            for name in stop_order
            if name in current.components and current.components[name].service in running_services
        ]
        mutation_started = True
        if services_to_stop:
            runner.run(
                compose_command(compose_file, "stop", *services_to_stop, env_file=env_file),
                cwd=compose_file.parent,
            )

        for item in prepared_files:
            _copy_file_atomic(item.prepared, item.target, mode=item.mode)
        runner.run(
            compose_command(compose_file, "config", "-q", env_file=env_file),
            cwd=compose_file.parent,
        )
        if dump_path is not None:
            database_component = current.components.get("database")
            if database_component is None:
                raise ValidationError(
                    "В текущей инвентаризации нет PostgreSQL-контейнера."
                )
            runner.run(
                compose_command(
                    compose_file,
                    "up",
                    "-d",
                    "--no-deps",
                    "--force-recreate",
                    database_component.service,
                    env_file=env_file,
                ),
                cwd=compose_file.parent,
            )
            wait_container(
                runner,
                database_component,
                timeout=300,
                require_health=True,
            )
            database_swap = _restore_database(
                runner,
                current,
                database,
                dump_path,
                preserve_previous=True,
            )
            if database_swap is not None:
                rollback_data["database"] = {
                    "container": database_swap.container,
                    "database": database_swap.database,
                    "previous_database": database_swap.previous_database,
                }
                atomic_write_bytes(
                    transaction_dir / "rollback.json",
                    json.dumps(rollback_data, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
                    mode=0o600,
                )

        nginx_component = saved.components.get("nginx")
        nginx_service = nginx_component.service if nginx_component is not None else None
        services_to_start = [
            service for service in services_to_stop if service != nginx_service
        ]
        if services_to_start:
            runner.run(
                compose_command(
                    compose_file,
                    "up",
                    "-d",
                    "--no-deps",
                    *services_to_start,
                    env_file=env_file,
                ),
                cwd=compose_file.parent,
            )
        if nginx_component is not None:
            if nginx_component.service in running_services:
                activate_nginx_config(runner, saved, was_running=True)
            else:
                prepare_nginx_config(runner, saved)
        database_component = current.components.get("database")
        if (
            dump_path is not None
            and database_component is not None
            and database_component.service not in running_services
        ):
            runner.run(
                compose_command(compose_file, "stop", database_component.service, env_file=env_file),
                cwd=compose_file.parent,
            )

        verify_running(saved, running_services)
        store.save_inventory(saved)
        inventory_saved = True
        if database_swap is not None:
            _commit_database(runner, database_swap)
        transaction_committed = True
        remove_transaction_dir()
        _clear_matching_recovery_journal(recovery_journal, recovery_backup_path)
    except BaseException as error:
        if transaction_committed:
            raise TransactionError(
                "Restore применён и проверен, но post-commit cleanup не завершён; "
                f"рабочая конфигурация не откатывалась: {error}. "
                f"Проверьте служебный путь {transaction_dir}"
            ) from error
        if not mutation_started:
            try:
                remove_transaction_dir()
            except BaseException as cleanup_error:  # noqa: BLE001 - preserve the primary restore error
                raise TransactionError(
                    "Restore остановлен до изменения системы, но временные файлы "
                    f"удалить не удалось: {transaction_dir}: "
                    f"{cleanup_error or type(cleanup_error).__name__}"
                ) from error
            raise

        rollback_errors: list[str] = []

        def rollback(label: str, operation: Any) -> None:
            try:
                operation()
            except BaseException as rollback_error:  # noqa: BLE001
                rollback_errors.append(f"{label}: {rollback_error}")

        if database_swap is not None:
            rollback("возврат исходной PostgreSQL БД", lambda: _rollback_database(runner, database_swap))

        rollback(
            "возврат исходных файлов",
            lambda: _restore_prepared_originals(prepared_files),
        )
        if inventory_saved:
            rollback("возврат текущей инвентаризации", lambda: store.save_inventory(current))
        rollback(
            "проверка исходного Compose",
            lambda: runner.run(
                compose_command(compose_file, "config", "-q", env_file=env_file),
                cwd=compose_file.parent,
            ),
        )

        database_component = current.components.get("database")
        if (
            dump_path is not None
            and database_component is not None
            and database_component.service not in running_services
        ):
            rollback(
                "остановка исходно неактивной PostgreSQL БД",
                lambda: runner.run(
                    compose_command(
                        compose_file,
                        "stop",
                        database_component.service,
                        env_file=env_file,
                    ),
                    cwd=compose_file.parent,
                ),
            )
        original_running = service_list(running_services)
        nginx_component = current.components.get("nginx")
        nginx_service = nginx_component.service if nginx_component is not None else None
        services_to_start = [
            service for service in original_running if service != nginx_service
        ]
        if services_to_start:
            rollback(
                "запуск исходно активных сервисов",
                lambda: runner.run(
                    compose_command(
                        compose_file,
                        "up",
                        "-d",
                        "--no-deps",
                        *services_to_start,
                        env_file=env_file,
                    ),
                    cwd=compose_file.parent,
                ),
            )
        if nginx_component is not None:
            if nginx_component.service in running_services:
                rollback(
                    "возврат исходной конфигурации nginx",
                    lambda: activate_nginx_config(runner, current, was_running=True),
                )
            else:
                rollback(
                    "проверка исходной конфигурации nginx",
                    lambda: prepare_nginx_config(runner, current),
                )
        rollback(
            "проверка исходного состояния сервисов",
            lambda: verify_running(current, running_services),
        )
        if not rollback_errors:
            try:
                remove_transaction_dir()
            except BaseException as cleanup_error:  # noqa: BLE001 - retain rollback artifacts and primary error
                rollback_errors.append(
                    "удаление файлов завершённой restore-транзакции: "
                    f"{cleanup_error or type(cleanup_error).__name__}"
                )
        if rollback_errors:
            detail = "; ".join(rollback_errors)
            raise TransactionError(
                f"Restore не завершён, автоматический rollback неполон: {detail}. "
                f"Исходная ошибка: {error}. Файлы безопасного rollback: {transaction_dir}"
            ) from error
        raise TransactionError(
            f"Restore не завершён; исходные файлы, БД и состояние сервисов восстановлены: {error}"
        ) from error


def _restore_database(
    runner: Runner,
    inventory: Inventory,
    database: dict[str, Any],
    dump_path: Path,
    *,
    preserve_previous: bool = False,
) -> _DatabaseSwap | None:
    component = inventory.components.get("database")
    if component is None:
        raise ValidationError("В текущей инвентаризации нет PostgreSQL-контейнера.")
    container = component.container or component.service
    user, db_name = _postgres_identity(runner, container)
    if (
        database.get("format") != "postgres-custom"
        or user != database.get("user")
        or db_name != database.get("database")
    ):
        raise ValidationError("POSTGRES_USER/POSTGRES_DB отличаются от сохранённого backup.")
    if not dump_path.is_file():
        raise ValidationError("Временный PostgreSQL dump не найден.")
    remote = f"/tmp/rwm-restore-{uuid.uuid4().hex}.dump"  # noqa: S108, RUF100 - private path inside the database container
    suffix = uuid.uuid4().hex[:16]
    staging_db = f"rwm_restore_{suffix}"
    previous_db = f"rwm_previous_{suffix}"
    current_renamed = False
    staging_renamed = False

    def psql(statement: str, *, check: bool = True) -> bool:
        command = [
            "docker",
            "exec",
            container,
            "psql",
            "--username",
            user,
            "--dbname",
            "template1",
            "--set",
            "ON_ERROR_STOP=1",
            "--command",
            statement,
        ]
        if not check:
            return _run_best_effort(
                runner,
                command,
                label="Не удалось выполнить аварийную команду PostgreSQL",
                timeout=300,
            )
        result = runner.run(
            command,
            timeout=300,
            sensitive=True,
        )
        return result.returncode == 0

    try:
        runner.run(["docker", "cp", str(dump_path), f"{container}:{remote}"], timeout=600)
        runner.run(["docker", "exec", container, "pg_restore", "--list", remote], sensitive=True)
        runner.run(
            [
                "docker",
                "exec",
                container,
                "createdb",
                "--username",
                user,
                "--maintenance-db=template1",
                staging_db,
            ],
            timeout=300,
            sensitive=True,
        )
        runner.run(
            [
                "docker",
                "exec",
                container,
                "pg_restore",
                "--username",
                user,
                "--dbname",
                staging_db,
                "--exit-on-error",
                "--no-owner",
                remote,
            ],
            timeout=1800,
            sensitive=True,
        )
        psql(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "  # noqa: S608, RUF100 - db_name is restricted by _postgres_identity
            f"WHERE datname = '{db_name}' AND pid <> pg_backend_pid();"
        )
        psql(f'ALTER DATABASE "{db_name}" RENAME TO "{previous_db}";')
        current_renamed = True
        psql(f'ALTER DATABASE "{staging_db}" RENAME TO "{db_name}";')
        staging_renamed = True
        swap = _DatabaseSwap(container, user, db_name, previous_db)
        if preserve_previous:
            return swap
        _commit_database(runner, swap)
        return None
    except BaseException as error:
        if current_renamed and staging_renamed:
            swap = _DatabaseSwap(container, user, db_name, previous_db)
            try:
                _rollback_database(runner, swap)
            except BaseException as rollback_error:  # noqa: BLE001
                raise TransactionError(
                    "Не удалось откатить PostgreSQL после ошибки завершения переключения. "
                    f"Проверенная старая БД сохранена под именем {previous_db}: {rollback_error}"
                ) from error
        if current_renamed and not staging_renamed and not psql(
            f'ALTER DATABASE "{previous_db}" RENAME TO "{db_name}";',
            check=False,
        ):
            raise TransactionError(
                "Не удалось вернуть имя рабочей БД после ошибки переключения. "
                f"Проверенная старая БД сохранена под именем {previous_db}."
            )
        raise
    finally:
        if not staging_renamed:
            _run_best_effort(
                runner,
                [
                    "docker",
                    "exec",
                    container,
                    "dropdb",
                    "--username",
                    user,
                    "--if-exists",
                    "--force",
                    "--maintenance-db=template1",
                    staging_db,
                ],
                label="Не удалось удалить временную PostgreSQL БД",
                timeout=300,
            )
        _run_best_effort(
            runner,
            ["docker", "exec", container, "rm", "-f", remote],
            label="Не удалось удалить временный PostgreSQL dump после restore",
        )


def _database_psql(
    runner: Runner,
    swap: _DatabaseSwap,
    statement: str,
    *,
    check: bool = True,
) -> bool:
    command = [
        "docker",
        "exec",
        swap.container,
        "psql",
        "--username",
        swap.user,
        "--dbname",
        "template1",
        "--set",
        "ON_ERROR_STOP=1",
        "--command",
        statement,
    ]
    if not check:
        return _run_best_effort(
            runner,
            command,
            label="Не удалось выполнить аварийную команду PostgreSQL",
            timeout=300,
        )
    result = runner.run(
        command,
        timeout=300,
        sensitive=True,
    )
    return result.returncode == 0


def _drop_database(
    runner: Runner,
    swap: _DatabaseSwap,
    database: str,
    *,
    check: bool = True,
) -> None:
    command = [
        "docker",
        "exec",
        swap.container,
        "dropdb",
        "--username",
        swap.user,
        "--if-exists",
        "--force",
        "--maintenance-db=template1",
        database,
    ]
    if not check:
        _run_best_effort(
            runner,
            command,
            label=f"Не удалось удалить временную PostgreSQL БД {database}",
            timeout=300,
        )
        return
    runner.run(
        command,
        timeout=300,
        sensitive=True,
    )


def _commit_database(runner: Runner, swap: _DatabaseSwap) -> None:
    _drop_database(runner, swap, swap.previous_database)


def _rollback_database(runner: Runner, swap: _DatabaseSwap) -> None:
    failed_database = f"rwm_failed_{uuid.uuid4().hex[:16]}"
    _database_psql(
        runner,
        swap,
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "  # noqa: S608, RUF100 - database is validated before the swap is created
        f"WHERE datname = '{swap.database}' AND pid <> pg_backend_pid();",
    )
    _database_psql(
        runner,
        swap,
        f'ALTER DATABASE "{swap.database}" RENAME TO "{failed_database}";',
    )
    if not _database_psql(
        runner,
        swap,
        f'ALTER DATABASE "{swap.previous_database}" RENAME TO "{swap.database}";',
        check=False,
    ):
        _database_psql(
            runner,
            swap,
            f'ALTER DATABASE "{failed_database}" RENAME TO "{swap.database}";',
            check=False,
        )
        raise TransactionError(
            "Не удалось вернуть исходную PostgreSQL БД. "
            f"Она сохранена под именем {swap.previous_database}."
        )
    _drop_database(runner, swap, failed_database, check=False)
