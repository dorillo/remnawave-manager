from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remnawave_manager.backup import (
    _MAX_ARCHIVE_MEMBERS,
    _apply_retention,
    _archive_name_for,
    _copy_file_atomic,
    _extract_member,
    _PreparedRestoreFile,
    _restore_database,
    _restore_prepared_originals,
    _run_best_effort,
    create_backup,
    delete_backups,
    list_backups,
    restore_backup,
    verify_backup,
)
from remnawave_manager.errors import CommandError, TransactionError, ValidationError
from remnawave_manager.models import Component, Inventory, ManagedFile
from remnawave_manager.paths import RuntimePaths
from remnawave_manager.runner import Result, sha256_file


def inventory(root: Path, *, role: str = "node") -> Inventory:
    compose = root / "docker-compose.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    component = (
        Component("database", "remnawave-db", "remnawave-db")
        if role == "panel"
        else Component("node", "remnanode", "remnanode")
    )
    return Inventory(
        schema_version=1,
        role=role,  # type: ignore[arg-type]
        install_dir=str(root),
        compose_file=str(compose),
        env_file=None,
        webserver=None,
        components={component.name: component},
        managed_files=[ManagedFile(str(compose), sha256_file(compose), "compose")],
    )


def make_backup(
    path: Path,
    saved: Inventory,
    files: list[tuple[Path, bytes]],
    *,
    extra: tuple[str, bytes] | None = None,
    database_dump: bytes | None = None,
) -> None:
    entries = [
        {
            "source": str(source),
            "archive_path": _archive_name_for(source),
            "sha256": hashlib.sha256(payload).hexdigest(),
            "mode": 0o600,
            "restore": True,
        }
        for source, payload in files
    ]
    database = None
    if database_dump is not None:
        database = {
            "archive_path": "database/panel.dump",
            "sha256": hashlib.sha256(database_dump).hexdigest(),
            "container": "remnawave-db",
            "user": "rw",
            "database": "remnawave",
            "format": "postgres-custom",
        }
    manifest = {
        "schema_version": 1,
        "inventory": saved.to_dict(),
        "files": entries,
        "database": database,
    }
    manifest_payload = json.dumps(manifest).encode("utf-8")
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in [("manifest.json", manifest_payload), *[(item["archive_path"], data) for item, (_, data) in zip(entries, files)]]:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        if database is not None and database_dump is not None:
            info = tarfile.TarInfo(database["archive_path"])
            info.size = len(database_dump)
            archive.addfile(info, io.BytesIO(database_dump))
        if extra is not None:
            info = tarfile.TarInfo(extra[0])
            info.size = len(extra[1])
            archive.addfile(info, io.BytesIO(extra[1]))


class NeverRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, args, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(tuple(args))
        raise AssertionError("runner must not be called before backup path validation")


class Store:
    def __init__(self, current: Inventory) -> None:
        self.current = current
        self.paths = RuntimePaths(Path(current.install_dir))

    def initialize(self) -> None:
        for path in (
            self.paths.etc,
            self.paths.state,
            self.paths.backups,
            self.paths.logs,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def load_inventory(self) -> Inventory:
        return self.current

    def save_inventory(self, value: Inventory) -> None:
        self.current = value


class DatabaseRunner:
    def __init__(self, *, fail_restore: bool = False) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.compose_snapshots: list[str] = []
        self.fail_restore = fail_restore

    def run(self, args, *, check=True, **kwargs):  # type: ignore[no-untyped-def]
        command = tuple(args)
        self.calls.append(command)
        if command[:2] == ("docker", "compose") and "--force-recreate" in command:
            compose_path = Path(command[command.index("-f") + 1])
            self.compose_snapshots.append(compose_path.read_text(encoding="utf-8"))
        if command[:2] == ("docker", "inspect"):
            return Result(command, 0, json.dumps(["POSTGRES_USER=rw", "POSTGRES_DB=remnawave"]), "")
        is_restore = "pg_restore" in command and "--dbname" in command
        if self.fail_restore and is_restore:
            result = Result(command, 1, "", "restore failed")
            if check:
                raise CommandError("restore failed")
            return result
        return Result(command, 0, "", "")


class CleanupFailureRunner(DatabaseRunner):
    def run(self, args, *, check=True, **kwargs):  # type: ignore[no-untyped-def]
        command = tuple(args)
        if not check and ("dropdb" in command or command[-3:-1] == ("rm", "-f")):
            raise CommandError("cleanup failed")
        return super().run(args, check=check, **kwargs)


class FailComposeConfigRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def run(self, args, **kwargs):  # type: ignore[no-untyped-def]
        command = tuple(args)
        self.calls.append(command)
        if command[:2] == ("docker", "compose") and "config" in command:
            raise CommandError("compose config failed")
        return Result(command, 0, "", "")


class FailFirstStartRunner:
    def __init__(self, service: str) -> None:
        self.service = service
        self.calls: list[tuple[str, ...]] = []
        self.starts = 0

    def run(self, args, **kwargs):  # type: ignore[no-untyped-def]
        command = tuple(args)
        self.calls.append(command)
        if command[:2] == ("docker", "compose") and "ps" in command:
            return Result(command, 0, self.service + "\n", "")
        if command[:2] == ("docker", "compose") and "up" in command:
            self.starts += 1
            if self.starts == 1:
                raise CommandError("service start failed")
        return Result(command, 0, "", "")


class FailEveryStartRunner(FailFirstStartRunner):
    def run(self, args, **kwargs):  # type: ignore[no-untyped-def]
        command = tuple(args)
        if command[:2] == ("docker", "compose") and "up" in command:
            self.calls.append(command)
            self.starts += 1
            raise CommandError("service start failed")
        return super().run(args, **kwargs)


class IntegratedDatabaseRunner(DatabaseRunner):
    def run(self, args, *, check=True, **kwargs):  # type: ignore[no-untyped-def]
        command = tuple(args)
        if command[:2] == ("docker", "compose") and "ps" in command:
            self.calls.append(command)
            return Result(command, 0, "remnawave-db\nremnawave\n", "")
        return super().run(args, check=check, **kwargs)


class NginxRestoreRunner:
    def __init__(
        self,
        config: Path,
        running_services: set[str],
    ) -> None:
        self.config = config
        self.running_services = running_services
        self.calls: list[tuple[str, ...]] = []
        self.nginx_recreate_payloads: list[bytes] = []

    def run(self, args, **_kwargs):  # type: ignore[no-untyped-def]
        command = tuple(args)
        self.calls.append(command)
        if command[:2] == ("docker", "compose") and "ps" in command:
            stdout = "\n".join(sorted(self.running_services))
            return Result(command, 0, stdout + ("\n" if stdout else ""), "")
        if (
            command[:2] == ("docker", "compose")
            and "up" in command
            and "--force-recreate" in command
            and command[-1] == "proxy"
        ):
            self.nginx_recreate_payloads.append(self.config.read_bytes())
        return Result(command, 0, "", "")


def nginx_inventory(root: Path) -> tuple[Inventory, Path]:
    current = inventory(root)
    config = root / "nginx.conf"
    config.write_text("server { return 204; }\n", encoding="utf-8")
    current.webserver = "nginx"
    current.components["nginx"] = Component("nginx", "proxy", None)
    current.nginx_files = [str(config)]
    current.managed_files.append(
        ManagedFile(str(config), sha256_file(config), "nginx")
    )
    return current, config


class BackupCreationTests(unittest.TestCase):
    def test_staging_cleanup_interrupt_does_not_mask_creation_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = inventory(root)
            store = Store(current)

            with (
                mock.patch(
                    "remnawave_manager.backup._archive_name_for",
                    side_effect=RuntimeError("primary creation error"),
                ),
                mock.patch(
                    "remnawave_manager.backup.shutil.rmtree",
                    side_effect=KeyboardInterrupt,
                ),
                self.assertLogs("remnawave_manager.backup", level="WARNING"),
                self.assertRaisesRegex(RuntimeError, "primary creation error"),
            ):
                create_backup(DatabaseRunner(), store)  # type: ignore[arg-type]

    def test_managed_source_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = inventory(root)
            compose = Path(current.compose_file)
            target = root / "actual-compose.yml"
            target.write_bytes(compose.read_bytes())
            compose.unlink()
            try:
                compose.symlink_to(target)
            except OSError as error:
                if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                    self.skipTest(f"symbolic links unavailable: {error}")
                raise
            store = Store(current)
            runner = NeverRunner()

            with self.assertRaisesRegex(ValidationError, "символической ссылкой"):
                create_backup(runner, store)  # type: ignore[arg-type]

            self.assertEqual(runner.calls, [])
            self.assertFalse(store.paths.backups.exists())

    def test_missing_managed_file_aborts_before_runner_or_archive_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = inventory(root)
            Path(current.compose_file).unlink()
            store = Store(current)
            runner = NeverRunner()

            with self.assertRaisesRegex(ValidationError, "managed-файл отсутствует"):
                create_backup(runner, store)  # type: ignore[arg-type]

            self.assertEqual(runner.calls, [])
            self.assertFalse(store.paths.backups.exists())

    def test_changed_managed_file_aborts_before_runner_or_archive_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = inventory(root)
            Path(current.compose_file).write_text("services:\n  changed: {}\n", encoding="utf-8")
            store = Store(current)
            runner = NeverRunner()

            with self.assertRaisesRegex(ValidationError, "managed-файл изменён"):
                create_backup(runner, store)  # type: ignore[arg-type]

            self.assertEqual(runner.calls, [])
            self.assertFalse(store.paths.backups.exists())


class BackupSecurityTests(unittest.TestCase):
    def test_delete_removes_only_selected_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(inventory(root))
            store.initialize()
            first = store.paths.backups / "20260803-node-first.tar.gz"
            second = store.paths.backups / "20260803-node-second.tar.gz"
            third = store.paths.backups / "20260803-node-third.tar.gz"
            for path in (first, second, third):
                path.write_bytes(path.name.encode("ascii"))

            removed = delete_backups(store, [first, third])  # type: ignore[arg-type]

            self.assertEqual(removed, [first, third])
            self.assertFalse(first.exists())
            self.assertTrue(second.is_file())
            self.assertFalse(third.exists())

    def test_delete_accepts_backup_name_and_rejects_outside_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(inventory(root))
            store.initialize()
            archive = store.paths.backups / "20260803-node-backup.tar.gz"
            archive.write_bytes(b"backup")

            with self.assertRaisesRegex(ValidationError, "только из каталога"):
                delete_backups(store, [root / "outside.tar.gz"])  # type: ignore[arg-type]

            self.assertEqual(
                delete_backups(store, [Path(archive.name)]),  # type: ignore[arg-type]
                [archive],
            )

    def test_delete_rejects_hardlinked_and_duplicate_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(inventory(root))
            store.initialize()
            outside = root / "outside.tar.gz"
            outside.write_bytes(b"must survive")
            linked = store.paths.backups / "20260803-node-linked.tar.gz"
            os.link(outside, linked)

            with self.assertRaisesRegex(ValidationError, "single-link"):
                delete_backups(store, [linked])  # type: ignore[arg-type]
            with self.assertRaisesRegex(ValidationError, "повторно"):
                delete_backups(store, [linked, Path(linked.name)])  # type: ignore[arg-type]

            self.assertEqual(outside.read_bytes(), b"must survive")
            self.assertTrue(linked.exists())

    def test_delete_is_blocked_by_active_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(inventory(root))
            store.initialize()
            archive = store.paths.backups / "20260803-node-backup.tar.gz"
            archive.write_bytes(b"backup")
            (store.paths.state / "active-transaction.json").write_text(
                "{}", encoding="utf-8"
            )

            with self.assertRaisesRegex(ValidationError, "незавершённая транзакция"):
                delete_backups(store, [archive])  # type: ignore[arg-type]

            self.assertTrue(archive.is_file())

    def test_delete_quarantines_path_replaced_during_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = Store(inventory(root))
            store.initialize()
            archive = store.paths.backups / "20260803-node-backup.tar.gz"
            archive.write_bytes(b"original")
            replacement = root / "replacement.tar.gz"
            replacement.write_bytes(b"replacement must not be deleted")
            real_rename = os.rename

            def replace_before_rename(
                source: str | os.PathLike[str],
                destination: str | os.PathLike[str],
                **kwargs: object,
            ) -> None:
                os.replace(replacement, archive)
                real_rename(source, destination, **kwargs)  # type: ignore[arg-type]

            with (
                mock.patch(
                    "remnawave_manager.backup.os.rename",
                    side_effect=replace_before_rename,
                ),
                self.assertRaisesRegex(TransactionError, "подменён"),
            ):
                delete_backups(store, [archive])  # type: ignore[arg-type]

            quarantined = list(store.paths.backups.glob(".delete-*.tmp"))
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(
                quarantined[0].read_bytes(), b"replacement must not be deleted"
            )

    def test_verify_rejects_symlink_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved = inventory(root)
            archive = root / "backup.tar.gz"
            make_backup(archive, saved, [(Path(saved.compose_file), b"services: {}\n")])
            link = root / "linked.tar.gz"
            try:
                link.symlink_to(archive)
            except OSError as error:
                if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                    self.skipTest(f"symbolic links unavailable: {error}")
                raise

            with self.assertRaisesRegex(ValidationError, "symlink"):
                verify_backup(link)

    def test_verify_rejects_hardlinked_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved = inventory(root)
            archive = root / "backup.tar.gz"
            make_backup(archive, saved, [(Path(saved.compose_file), b"services: {}\n")])
            link = root / "linked.tar.gz"
            os.link(archive, link)

            with self.assertRaisesRegex(ValidationError, "hardlink"):
                verify_backup(link)

    def test_list_ignores_hardlinked_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved = inventory(root)
            store = Store(saved)
            store.initialize()
            ordinary = store.paths.backups / "ordinary-node-a.tar.gz"
            ordinary.write_bytes(b"ordinary")
            outside = root / "outside.tar.gz"
            outside.write_bytes(b"outside")
            linked = store.paths.backups / "linked-node-b.tar.gz"
            os.link(outside, linked)

            self.assertEqual(list_backups(store), [ordinary])  # type: ignore[arg-type]

    def test_list_ignores_symlink_archives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved = inventory(root)
            store = Store(saved)
            store.initialize()
            ordinary = store.paths.backups / "ordinary-node-a.tar.gz"
            ordinary.write_bytes(b"ordinary")
            outside = root / "outside.tar.gz"
            outside.write_bytes(b"outside")
            linked = store.paths.backups / "linked-node-b.tar.gz"
            try:
                linked.symlink_to(outside)
            except OSError as error:
                if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                    self.skipTest(f"symbolic links unavailable: {error}")
                raise

            self.assertEqual(list_backups(store), [ordinary])  # type: ignore[arg-type]

    def test_retention_never_deletes_hardlinked_archive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "backups"
            directory.mkdir()
            newest = directory / "20260803-node-new.tar.gz"
            oldest = directory / "20260801-node-old.tar.gz"
            newest.write_bytes(b"new")
            oldest.write_bytes(b"old")
            os.utime(oldest, (1, 1))
            os.utime(newest, (3, 3))
            outside = root / "outside.tar.gz"
            outside.write_bytes(b"must survive")
            linked = directory / "20260802-node-linked.tar.gz"
            os.link(outside, linked)
            os.utime(linked, (2, 2))

            _apply_retention(directory, "node", 1)

            self.assertTrue(newest.is_file())
            self.assertFalse(oldest.exists())
            self.assertTrue(linked.is_file())
            self.assertEqual(outside.read_bytes(), b"must survive")

    def test_retention_quarantines_path_replaced_during_delete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "backups"
            directory.mkdir()
            newest = directory / "20260803-node-new.tar.gz"
            oldest = directory / "20260801-node-old.tar.gz"
            newest.write_bytes(b"new")
            oldest.write_bytes(b"old")
            os.utime(oldest, (1, 1))
            os.utime(newest, (3, 3))
            replacement = root / "replacement.tar.gz"
            replacement.write_bytes(b"replacement must not be deleted")
            real_rename = os.rename

            def replace_before_rename(
                source: str | os.PathLike[str],
                destination: str | os.PathLike[str],
                **kwargs: object,
            ) -> None:
                os.replace(replacement, oldest)
                real_rename(source, destination, **kwargs)  # type: ignore[arg-type]

            with (
                mock.patch(
                    "remnawave_manager.backup.os.rename",
                    side_effect=replace_before_rename,
                ),
                self.assertRaisesRegex(TransactionError, "подменён"),
            ):
                _apply_retention(directory, "node", 1)

            quarantined = list(directory.glob(".retention-*.tmp"))
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(quarantined[0].read_bytes(), b"replacement must not be deleted")
            self.assertTrue(newest.is_file())

    def test_verify_limits_total_declared_content_size(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved = inventory(root)
            archive = root / "backup.tar.gz"
            make_backup(archive, saved, [(Path(saved.compose_file), b"services: {}\n")])

            with (
                mock.patch("remnawave_manager.backup._MAX_ARCHIVE_CONTENT_SIZE", 1),
                self.assertRaisesRegex(ValidationError, "Суммарный размер"),
            ):
                verify_backup(archive)

    def test_restore_uses_private_snapshot_after_source_is_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved = inventory(root)
            compose = Path(saved.compose_file)
            payload = b"services:\n  remnanode:\n    image: remnanode:2.8.0\n"
            compose.write_bytes(payload)
            saved.managed_files[0].sha256 = hashlib.sha256(payload).hexdigest()
            archive = root / "backup.tar.gz"
            make_backup(archive, saved, [(compose, payload)])
            original_verify = verify_backup
            snapshots: list[Path] = []

            def replace_source(snapshot: Path) -> dict[str, object]:
                snapshots.append(snapshot)
                archive.write_bytes(b"replaced after private copy")
                return original_verify(snapshot)

            with (
                mock.patch(
                    "remnawave_manager.backup.verify_backup",
                    side_effect=replace_source,
                ),
                mock.patch("remnawave_manager.backup.test_nginx"),
                mock.patch("remnawave_manager.backup.reload_nginx"),
            ):
                restore_backup(DatabaseRunner(), Store(saved), archive)  # type: ignore[arg-type]

            self.assertEqual(len(snapshots), 1)
            self.assertNotEqual(snapshots[0], archive)
            self.assertFalse(snapshots[0].exists())
            self.assertEqual(compose.read_bytes(), payload)
            self.assertEqual(archive.read_bytes(), b"replaced after private copy")

    def test_private_snapshot_cleanup_interrupt_does_not_mask_restore_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved = inventory(root)
            archive = root / "backup.tar.gz"
            archive.write_bytes(b"private snapshot source")

            with (
                mock.patch(
                    "remnawave_manager.backup._restore_backup_snapshot",
                    side_effect=RuntimeError("primary restore error"),
                ),
                mock.patch(
                    "remnawave_manager.backup.shutil.rmtree",
                    side_effect=KeyboardInterrupt,
                ),
                self.assertLogs("remnawave_manager.backup", level="WARNING"),
                self.assertRaisesRegex(RuntimeError, "primary restore error"),
            ):
                restore_backup(DatabaseRunner(), Store(saved), archive)  # type: ignore[arg-type]

    def test_private_snapshot_cleanup_base_exception_after_success_is_reported(self) -> None:
        class CleanupInterrupted(BaseException):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved = inventory(root)
            archive = root / "backup.tar.gz"
            archive.write_bytes(b"private snapshot source")

            with (
                mock.patch("remnawave_manager.backup._restore_backup_snapshot"),
                mock.patch(
                    "remnawave_manager.backup.shutil.rmtree",
                    side_effect=CleanupInterrupted("cleanup interrupted"),
                ),
                self.assertRaisesRegex(
                    TransactionError,
                    "приватную копию backup удалить не удалось",
                ),
            ):
                restore_backup(DatabaseRunner(), Store(saved), archive)  # type: ignore[arg-type]

    def test_verify_stops_when_archive_member_limit_is_exceeded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            archive_path = Path(temporary) / "backup.tar.gz"
            with tarfile.open(archive_path, "w:gz") as archive:
                for index in range(_MAX_ARCHIVE_MEMBERS + 1):
                    info = tarfile.TarInfo(f"empty-{index}")
                    info.size = 0
                    archive.addfile(info, io.BytesIO())

            with self.assertRaisesRegex(ValidationError, "слишком много элементов"):
                verify_backup(archive_path)

    def test_verify_rejects_non_string_archive_path_as_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved = inventory(root)
            archive = root / "backup.tar.gz"
            manifest = {
                "schema_version": 1,
                "inventory": saved.to_dict(),
                "files": [
                    {
                        "source": saved.compose_file,
                        "archive_path": ["not", "a", "path"],
                        "sha256": "a" * 64,
                        "mode": 0o600,
                        "restore": True,
                    }
                ],
                "database": None,
            }
            payload = json.dumps(manifest).encode("utf-8")
            with tarfile.open(archive, "w:gz") as tar:
                info = tarfile.TarInfo("manifest.json")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))

            with self.assertRaises(ValidationError):
                verify_backup(archive)

    def test_verify_rejects_unregistered_tar_member(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved = inventory(root)
            archive = root / "backup.tar.gz"
            make_backup(archive, saved, [], extra=("unregistered.txt", b"x"))

            with self.assertRaises(ValidationError):
                verify_backup(archive)

    def test_restore_rejects_path_added_only_by_saved_inventory_before_stopping_services(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = inventory(root)
            outside = root.parent / "outside-rwm-test"
            saved = Inventory.from_dict(current.to_dict())
            payload = b"malicious"
            saved.managed_files.append(
                ManagedFile(str(outside), hashlib.sha256(payload).hexdigest(), "config")
            )
            archive = root / "backup.tar.gz"
            make_backup(archive, saved, [(outside, payload)])
            runner = NeverRunner()

            with self.assertRaises(ValidationError):
                restore_backup(runner, Store(current), archive)  # type: ignore[arg-type]
            self.assertEqual(runner.calls, [])

    def test_extract_refuses_to_consume_reserved_free_space(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_path = root / "backup.tar.gz"
            payload = b"database dump"
            with tarfile.open(archive_path, "w:gz") as output:
                info = tarfile.TarInfo("database/panel.dump")
                info.size = len(payload)
                output.addfile(info, io.BytesIO(payload))

            target = root / "restore" / "panel.dump"
            with (
                tarfile.open(archive_path, "r:gz") as archive,
                mock.patch(
                    "remnawave_manager.backup.shutil.disk_usage",
                    return_value=mock.Mock(free=len(payload) - 1),
                ),
                mock.patch("remnawave_manager.backup._MIN_FREE_SPACE_AFTER_EXTRACT", 0),
                self.assertRaisesRegex(ValidationError, "Недостаточно свободного места"),
            ):
                _extract_member(
                    archive,
                    "database/panel.dump",
                    target,
                    checksum=hashlib.sha256(payload).hexdigest(),
                    limit=len(payload),
                )

            self.assertFalse(target.exists())


class RestoreTransactionTests(unittest.TestCase):
    def test_running_nginx_is_recreated_for_restore_and_again_for_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved, config = nginx_inventory(root)
            compose = Path(saved.compose_file)
            saved_compose = b"services:\n  remnanode:\n    image: remnanode:2.8.0\n  proxy:\n    image: nginx:old\n"
            saved_config = b"server { return 202; }\n"
            compose.write_bytes(saved_compose)
            config.write_bytes(saved_config)
            saved.managed_files[0].sha256 = hashlib.sha256(saved_compose).hexdigest()
            saved.managed_files[1].sha256 = hashlib.sha256(saved_config).hexdigest()
            archive = root / "backup.tar.gz"
            make_backup(
                archive,
                saved,
                [(compose, saved_compose), (config, saved_config)],
            )

            current = Inventory.from_dict(saved.to_dict())
            current_compose = b"services:\n  remnanode:\n    image: remnanode:3.0.0\n  proxy:\n    image: nginx:new\n"
            current_config = b"server { return 204; }\n"
            compose.write_bytes(current_compose)
            config.write_bytes(current_config)
            current.managed_files[0].sha256 = hashlib.sha256(current_compose).hexdigest()
            current.managed_files[1].sha256 = hashlib.sha256(current_config).hexdigest()
            runner = NginxRestoreRunner(config, {"remnanode", "proxy"})

            with (
                mock.patch("remnawave_manager.backup.wait_container"),
                mock.patch(
                    "remnawave_manager.backup.check_node_runtime",
                    side_effect=[TransactionError("runtime failed"), None],
                ),
                mock.patch("remnawave_manager.backup.wait_for_paths"),
                self.assertRaisesRegex(TransactionError, "runtime failed"),
            ):
                restore_backup(runner, Store(current), archive)  # type: ignore[arg-type]

            self.assertEqual(runner.nginx_recreate_payloads, [saved_config, current_config])
            self.assertEqual(compose.read_bytes(), current_compose)
            self.assertEqual(config.read_bytes(), current_config)
            nginx_starts = [
                command
                for command in runner.calls
                if command[:2] == ("docker", "compose")
                and "up" in command
                and command[-1] == "proxy"
            ]
            self.assertEqual(len(nginx_starts), 2)

    def test_stopped_nginx_restore_recreates_and_validates_without_starting_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved, config = nginx_inventory(root)
            compose = Path(saved.compose_file)
            saved_compose = b"services:\n  remnanode:\n    image: remnanode:2.8.0\n  proxy:\n    image: nginx:old\n"
            saved_config = b"server { return 202; }\n"
            compose.write_bytes(saved_compose)
            config.write_bytes(saved_config)
            saved.managed_files[0].sha256 = hashlib.sha256(saved_compose).hexdigest()
            saved.managed_files[1].sha256 = hashlib.sha256(saved_config).hexdigest()
            archive = root / "backup.tar.gz"
            make_backup(
                archive,
                saved,
                [(compose, saved_compose), (config, saved_config)],
            )

            current = Inventory.from_dict(saved.to_dict())
            current_compose = b"services:\n  remnanode:\n    image: remnanode:3.0.0\n  proxy:\n    image: nginx:new\n"
            current_config = b"server { return 204; }\n"
            compose.write_bytes(current_compose)
            config.write_bytes(current_config)
            current.managed_files[0].sha256 = hashlib.sha256(current_compose).hexdigest()
            current.managed_files[1].sha256 = hashlib.sha256(current_config).hexdigest()
            runner = NginxRestoreRunner(config, {"remnanode"})

            with (
                mock.patch("remnawave_manager.backup.wait_container"),
                mock.patch("remnawave_manager.backup.check_node_runtime"),
                mock.patch("remnawave_manager.backup.wait_for_paths"),
            ):
                restore_backup(runner, Store(current), archive)  # type: ignore[arg-type]

            proxy_commands = [
                command
                for command in runner.calls
                if command[:2] == ("docker", "compose") and command[-1] == "proxy"
            ]
            self.assertEqual(len([command for command in proxy_commands if "create" in command]), 1)
            self.assertEqual(len([command for command in runner.calls if "run" in command and "proxy" in command]), 1)
            self.assertFalse(any("up" in command for command in proxy_commands))
            self.assertFalse(any("stop" in command for command in proxy_commands))
            self.assertEqual(config.read_bytes(), saved_config)

    def test_restore_rejects_symlink_target_without_touching_link_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved = inventory(root)
            compose = Path(saved.compose_file)
            link_destination = root / "outside.yml"
            original_payload = b"must remain unchanged\n"
            restored_payload = b"services:\n  restored: {}\n"
            link_destination.write_bytes(original_payload)
            compose.unlink()
            try:
                compose.symlink_to(link_destination)
            except OSError as error:
                if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                    self.skipTest(f"symbolic links unavailable: {error}")
                raise
            saved.managed_files[0].sha256 = hashlib.sha256(restored_payload).hexdigest()
            archive = root / "backup.tar.gz"
            make_backup(archive, saved, [(compose, restored_payload)])
            runner = NeverRunner()

            with self.assertRaisesRegex(ValidationError, "символической ссылкой"):
                restore_backup(runner, Store(saved), archive)  # type: ignore[arg-type]

            self.assertEqual(link_destination.read_bytes(), original_payload)
            self.assertTrue(compose.is_symlink())
            self.assertEqual(runner.calls, [])

    def test_internal_restore_keeps_matching_recovery_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved = inventory(root)
            compose = Path(saved.compose_file)
            archive = root / "backup.tar.gz"
            make_backup(archive, saved, [(compose, compose.read_bytes())])
            store = Store(saved)
            store.initialize()
            journal = store.paths.state / "active-transaction.json"
            journal.write_text(
                json.dumps(
                    {
                        "transaction_id": "a" * 32,
                        "operation": "node-update",
                        "backup": str(archive.resolve()),
                        "phase": "rolling-back",
                    }
                ),
                encoding="utf-8",
            )

            with (
                mock.patch("remnawave_manager.backup.test_nginx"),
                mock.patch("remnawave_manager.backup.reload_nginx"),
            ):
                restore_backup(
                    DatabaseRunner(),
                    store,  # type: ignore[arg-type]
                    archive,
                    clear_recovery_journal=False,
                )

            self.assertTrue(journal.is_file())

    def test_successful_matching_recovery_clears_only_its_stale_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved = inventory(root)
            compose = Path(saved.compose_file)
            payload = compose.read_bytes()
            archive = root / "backup.tar.gz"
            make_backup(archive, saved, [(compose, payload)])
            store = Store(saved)
            store.initialize()
            journal = store.paths.state / "active-transaction.json"
            journal.write_text(
                json.dumps(
                    {
                        "operation": "node-update",
                        "backup": str(archive.resolve()),
                        "phase": "rolling-back",
                    }
                ),
                encoding="utf-8",
            )

            with (
                mock.patch("remnawave_manager.backup.test_nginx"),
                mock.patch("remnawave_manager.backup.reload_nginx"),
            ):
                restore_backup(DatabaseRunner(), store, archive)  # type: ignore[arg-type]

            self.assertFalse(journal.exists())

            journal.write_text(
                json.dumps(
                    {
                        "transaction_id": "a" * 32,
                        "operation": "node-update",
                        "backup": str(root / "different.tar.gz"),
                        "phase": "rolling-back",
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch("remnawave_manager.backup.test_nginx"),
                mock.patch("remnawave_manager.backup.reload_nginx"),
            ):
                restore_backup(DatabaseRunner(), store, archive)  # type: ignore[arg-type]

            self.assertTrue(journal.is_file())

    def test_panel_restore_uses_legacy_health_for_saved_subscription_726(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved = inventory(root, role="panel")
            panel = Component("panel", "remnawave", "remnawave")
            subscription = Component(
                "subscription",
                "remnawave-subscription-page",
                "remnawave-subscription-page",
            )
            saved.components.update(
                {"panel": panel, "subscription": subscription}
            )
            compose = Path(saved.compose_file)
            archive = root / "backup.tar.gz"
            make_backup(archive, saved, [(compose, compose.read_bytes())])
            running = {panel.service, subscription.service}

            with (
                mock.patch(
                    "remnawave_manager.backup._running_component_services",
                    return_value=running,
                ),
                mock.patch("remnawave_manager.backup.wait_container"),
                mock.patch("remnawave_manager.backup.check_panel_http"),
                mock.patch(
                    "remnawave_manager.backup.detect_component_version",
                    return_value="7.2.6",
                ) as detect_version,
                mock.patch(
                    "remnawave_manager.backup.check_subscription_http"
                ) as subscription_health,
                mock.patch("remnawave_manager.backup.test_nginx"),
                mock.patch("remnawave_manager.backup.reload_nginx"),
            ):
                restore_backup(DatabaseRunner(), Store(saved), archive)  # type: ignore[arg-type]

            detect_version.assert_called_once_with(
                mock.ANY, "subscription", subscription
            )
            subscription_health.assert_called_once_with(
                mock.ANY, subscription, legacy=True
            )

    def test_compose_candidate_is_validated_before_stop_or_file_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved = inventory(root)
            compose = Path(saved.compose_file)
            saved_payload = b"services:\n  remnanode:\n    image: remnanode:2.8.0\n"
            saved.managed_files[0].sha256 = hashlib.sha256(saved_payload).hexdigest()
            archive = root / "backup.tar.gz"
            make_backup(archive, saved, [(compose, saved_payload)])

            current = Inventory.from_dict(saved.to_dict())
            current_payload = b"services:\n  remnanode:\n    image: remnanode:3.0.0\n"
            compose.write_bytes(current_payload)
            current.managed_files[0].sha256 = hashlib.sha256(current_payload).hexdigest()
            runner = FailComposeConfigRunner()

            with self.assertRaisesRegex(CommandError, "compose config failed"):
                restore_backup(runner, Store(current), archive)  # type: ignore[arg-type]

            self.assertEqual(compose.read_bytes(), current_payload)
            self.assertFalse(any("stop" in command for command in runner.calls))

    def test_failed_start_restores_original_files_and_running_services(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved = inventory(root)
            compose = Path(saved.compose_file)
            saved_payload = b"services:\n  remnanode:\n    image: remnanode:2.8.0\n"
            saved.managed_files[0].sha256 = hashlib.sha256(saved_payload).hexdigest()
            archive = root / "backup.tar.gz"
            make_backup(archive, saved, [(compose, saved_payload)])

            current = Inventory.from_dict(saved.to_dict())
            current_payload = b"services:\n  remnanode:\n    image: remnanode:3.0.0\n"
            compose.write_bytes(current_payload)
            current.managed_files[0].sha256 = hashlib.sha256(current_payload).hexdigest()
            runner = FailFirstStartRunner("remnanode")
            store = Store(current)

            with (
                mock.patch("remnawave_manager.backup.wait_container"),
                mock.patch("remnawave_manager.backup.check_node_runtime"),
                mock.patch("remnawave_manager.backup.wait_for_paths"),
                mock.patch("remnawave_manager.backup.test_nginx"),
                mock.patch("remnawave_manager.backup.reload_nginx"),
                self.assertRaisesRegex(TransactionError, "исходные файлы, БД и состояние сервисов восстановлены"),
            ):
                restore_backup(runner, store, archive)  # type: ignore[arg-type]

            self.assertEqual(compose.read_bytes(), current_payload)
            self.assertEqual(runner.starts, 2)
            self.assertFalse(any(store.paths.backups.glob(".restore-*")))

    def test_post_commit_cleanup_interrupt_never_starts_rollback(self) -> None:
        class CleanupInterrupted(BaseException):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved = inventory(root)
            compose = Path(saved.compose_file)
            saved_payload = b"services:\n  remnanode:\n    image: remnanode:2.8.0\n"
            saved.managed_files[0].sha256 = hashlib.sha256(saved_payload).hexdigest()
            archive = root / "backup.tar.gz"
            make_backup(archive, saved, [(compose, saved_payload)])

            current = Inventory.from_dict(saved.to_dict())
            current_payload = b"services:\n  remnanode:\n    image: remnanode:3.0.0\n"
            compose.write_bytes(current_payload)
            current.managed_files[0].sha256 = hashlib.sha256(current_payload).hexdigest()
            store = Store(current)
            real_rmtree = shutil.rmtree

            def interrupt_transaction_cleanup(path, *args, **kwargs):  # type: ignore[no-untyped-def]
                candidate = Path(path)
                if candidate.name.startswith(".restore-") and not candidate.name.startswith(
                    ".restore-source-"
                ):
                    raise CleanupInterrupted("transaction cleanup interrupted")
                return real_rmtree(path, *args, **kwargs)

            with (
                mock.patch("remnawave_manager.backup.wait_container"),
                mock.patch("remnawave_manager.backup.check_node_runtime"),
                mock.patch("remnawave_manager.backup.wait_for_paths"),
                mock.patch("remnawave_manager.backup.test_nginx"),
                mock.patch("remnawave_manager.backup.reload_nginx"),
                mock.patch(
                    "remnawave_manager.backup.shutil.rmtree",
                    side_effect=interrupt_transaction_cleanup,
                ),
                mock.patch(
                    "remnawave_manager.backup._restore_prepared_originals"
                ) as rollback_files,
                self.assertRaisesRegex(TransactionError, "post-commit cleanup"),
            ):
                restore_backup(DatabaseRunner(), store, archive)  # type: ignore[arg-type]

            rollback_files.assert_not_called()
            self.assertEqual(compose.read_bytes(), saved_payload)
            self.assertEqual(len(list(store.paths.backups.glob(".restore-*"))), 1)

    def test_health_failure_after_database_swap_restores_previous_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved = inventory(root, role="panel")
            saved.components["panel"] = Component("panel", "remnawave", "remnawave")
            compose = Path(saved.compose_file)
            saved_payload = (
                b"services:\n"
                b"  remnawave-db:\n    image: postgres:18.3\n"
                b"  remnawave:\n    image: remnawave:2.8.1\n"
            )
            saved.managed_files[0].sha256 = hashlib.sha256(saved_payload).hexdigest()
            dump = b"PGDMP" + b"x" * 2048
            archive = root / "backup.tar.gz"
            make_backup(archive, saved, [(compose, saved_payload)], database_dump=dump)

            current = Inventory.from_dict(saved.to_dict())
            current_payload = (
                b"services:\n"
                b"  remnawave-db:\n    image: postgres:18.4\n"
                b"  remnawave:\n    image: remnawave:3.1.0\n"
            )
            compose.write_bytes(current_payload)
            current.managed_files[0].sha256 = hashlib.sha256(current_payload).hexdigest()
            runner = IntegratedDatabaseRunner()
            store = Store(current)

            with (
                mock.patch("remnawave_manager.backup.wait_container"),
                mock.patch(
                    "remnawave_manager.backup.check_panel_http",
                    side_effect=[TransactionError("panel health failed"), None],
                ),
                mock.patch("remnawave_manager.backup.test_nginx"),
                mock.patch("remnawave_manager.backup.reload_nginx"),
                self.assertRaisesRegex(TransactionError, "исходные файлы, БД и состояние сервисов восстановлены"),
            ):
                restore_backup(runner, store, archive)  # type: ignore[arg-type]

            statements = [command[-1] for command in runner.calls if "psql" in command]
            self.assertTrue(any('ALTER DATABASE "remnawave" RENAME TO "rwm_previous_' in item for item in statements))
            self.assertTrue(any('ALTER DATABASE "remnawave" RENAME TO "rwm_failed_' in item for item in statements))
            self.assertTrue(
                any('ALTER DATABASE "rwm_previous_' in item and 'RENAME TO "remnawave"' in item for item in statements)
            )
            self.assertEqual(compose.read_bytes(), current_payload)
            self.assertFalse(any(store.paths.backups.glob(".restore-*")))

    def test_incomplete_service_rollback_keeps_mapped_original_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved = inventory(root)
            compose = Path(saved.compose_file)
            saved_payload = b"services:\n  remnanode:\n    image: remnanode:2.8.0\n"
            saved.managed_files[0].sha256 = hashlib.sha256(saved_payload).hexdigest()
            archive = root / "backup.tar.gz"
            make_backup(archive, saved, [(compose, saved_payload)])

            current = Inventory.from_dict(saved.to_dict())
            current_payload = b"services:\n  remnanode:\n    image: remnanode:3.0.0\n"
            compose.write_bytes(current_payload)
            current.managed_files[0].sha256 = hashlib.sha256(current_payload).hexdigest()
            runner = FailEveryStartRunner("remnanode")
            store = Store(current)

            with (
                mock.patch("remnawave_manager.backup.wait_container"),
                mock.patch("remnawave_manager.backup.check_node_runtime"),
                mock.patch("remnawave_manager.backup.wait_for_paths"),
                mock.patch("remnawave_manager.backup.test_nginx") as test_nginx,
                mock.patch("remnawave_manager.backup.reload_nginx"),
                self.assertRaisesRegex(TransactionError, "Файлы безопасного rollback"),
            ):
                restore_backup(runner, store, archive)  # type: ignore[arg-type]

            transactions = list(store.paths.backups.glob(".restore-*"))
            self.assertEqual(len(transactions), 1)
            rollback = json.loads((transactions[0] / "rollback.json").read_text(encoding="utf-8"))
            self.assertEqual(rollback["files"][0]["target"], str(compose))
            original = Path(rollback["files"][0]["original"])
            self.assertEqual(original.read_bytes(), current_payload)
            self.assertEqual(compose.read_bytes(), current_payload)
            test_nginx.assert_called_once()

    def test_original_file_rollback_continues_after_base_exception(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_target = root / "first.conf"
            second_target = root / "second.conf"
            first_original = root / "original-first"
            second_original = root / "original-second"
            first_target.write_bytes(b"new-first")
            second_target.write_bytes(b"new-second")
            first_original.write_bytes(b"old-first")
            second_original.write_bytes(b"old-second")
            real_copy = _copy_file_atomic

            def interrupt_first(source: Path, target: Path, *, mode: int) -> None:
                if target == first_target:
                    raise KeyboardInterrupt
                real_copy(source, target, mode=mode)

            with (
                mock.patch(
                    "remnawave_manager.backup._copy_file_atomic",
                    side_effect=interrupt_first,
                ),
                self.assertRaisesRegex(TransactionError, "first.conf"),
            ):
                _restore_prepared_originals(
                    [
                        _PreparedRestoreFile(
                            first_target,
                            0o600,
                            root / "prepared-first",
                            first_original,
                            0o600,
                        ),
                        _PreparedRestoreFile(
                            second_target,
                            0o600,
                            root / "prepared-second",
                            second_original,
                            0o600,
                        ),
                    ]
                )

            self.assertEqual(first_target.read_bytes(), b"new-first")
            self.assertEqual(second_target.read_bytes(), b"old-second")


class DatabaseRestoreTests(unittest.TestCase):
    def test_best_effort_cleanup_survives_all_base_exceptions(self) -> None:
        class CleanupInterrupted(BaseException):
            pass

        runner = mock.Mock()
        command = ("docker", "exec", "database", "cleanup")
        runner.run.side_effect = [
            KeyboardInterrupt(),
            CleanupInterrupted("cleanup interrupted"),
            Result(command, 0, "", ""),
        ]

        with self.assertLogs("remnawave_manager.backup", level="WARNING"):
            first = _run_best_effort(runner, list(command), label="first cleanup")
            second = _run_best_effort(runner, list(command), label="second cleanup")
        third = _run_best_effort(runner, list(command), label="third cleanup")

        self.assertFalse(first)
        self.assertFalse(second)
        self.assertTrue(third)
        self.assertEqual(runner.run.call_count, 3)

    def test_restore_uses_separate_size_limit_for_database_dump(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved = inventory(root, role="panel")
            compose = Path(saved.compose_file)
            dump = b"PGDMP" + b"x" * 2048
            archive = root / "backup.tar.gz"
            make_backup(
                archive,
                saved,
                [(compose, compose.read_bytes())],
                database_dump=dump,
            )

            with (
                mock.patch("remnawave_manager.backup._MAX_CONFIG_FILE_SIZE", 1024),
                mock.patch("remnawave_manager.backup._MAX_DATABASE_DUMP_SIZE", 4096),
                mock.patch("remnawave_manager.backup.wait_container"),
                mock.patch("remnawave_manager.backup.test_nginx"),
                mock.patch("remnawave_manager.backup.reload_nginx"),
            ):
                restore_backup(DatabaseRunner(), Store(saved), archive)  # type: ignore[arg-type]

    def test_cleanup_failure_does_not_mask_database_restore_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dump = root / "panel.dump"
            dump.write_bytes(b"PGDMP" + b"x" * 2048)
            runner = CleanupFailureRunner(fail_restore=True)

            with (
                self.assertLogs("remnawave_manager.backup", level="WARNING") as logs,
                self.assertRaisesRegex(CommandError, "restore failed"),
            ):
                _restore_database(
                    runner,
                    inventory(root, role="panel"),
                    {"format": "postgres-custom", "user": "rw", "database": "remnawave"},
                    dump,
                )

            self.assertTrue(any("cleanup failed" in line for line in logs.output))

    def test_restore_recreates_saved_postgres_image_before_loading_dump(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            saved = inventory(root, role="panel")
            compose = Path(saved.compose_file)
            legacy_compose = (
                b"services:\n"
                b"  remnawave-db:\n"
                b"    image: postgres:18.3\n"
            )
            saved.managed_files[0].sha256 = hashlib.sha256(legacy_compose).hexdigest()
            archive = root / "backup.tar.gz"
            dump = b"PGDMP" + b"x" * 2048
            make_backup(
                archive,
                saved,
                [(compose, legacy_compose)],
                database_dump=dump,
            )
            compose.write_text(
                "services:\n"
                "  remnawave-db:\n"
                "    image: postgres:18.4@sha256:" + "a" * 64 + "\n",
                encoding="utf-8",
            )
            runner = DatabaseRunner()

            with (
                mock.patch("remnawave_manager.backup.wait_container") as wait,
                mock.patch("remnawave_manager.backup.check_panel_http"),
                mock.patch("remnawave_manager.backup.test_nginx"),
                mock.patch("remnawave_manager.backup.reload_nginx"),
            ):
                restore_backup(runner, Store(saved), archive)

            recreate_index = next(
                index
                for index, command in enumerate(runner.calls)
                if command[:2] == ("docker", "compose")
                and "--force-recreate" in command
                and command[-1] == "remnawave-db"
            )
            restore_index = next(
                index
                for index, command in enumerate(runner.calls)
                if "pg_restore" in command and "--dbname" in command
            )
            self.assertLess(recreate_index, restore_index)
            self.assertEqual(runner.compose_snapshots, [legacy_compose.decode("utf-8")])
            self.assertEqual(compose.read_bytes(), legacy_compose)
            wait.assert_called_once()
            self.assertEqual(wait.call_args.args[1].name, "database")
            self.assertTrue(wait.call_args.kwargs["require_health"])

    def test_restore_builds_staging_database_before_switching_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dump = root / "panel.dump"
            dump.write_bytes(b"PGDMP" + b"x" * 2048)
            runner = DatabaseRunner()

            _restore_database(
                runner,
                inventory(root, role="panel"),
                {"format": "postgres-custom", "user": "rw", "database": "remnawave"},
                dump,
            )

            createdb = next(command for command in runner.calls if "createdb" in command)
            staging = createdb[-1]
            restore_index = next(
                index
                for index, command in enumerate(runner.calls)
                if "pg_restore" in command and "--dbname" in command
            )
            rename_old_index = next(
                index
                for index, command in enumerate(runner.calls)
                if "psql" in command and "RENAME TO \"rwm_previous_" in command[-1]
            )
            rename_new_index = next(
                index
                for index, command in enumerate(runner.calls)
                if "psql" in command and f'ALTER DATABASE "{staging}"' in command[-1]
            )
            self.assertLess(restore_index, rename_old_index)
            self.assertLess(rename_old_index, rename_new_index)
            self.assertFalse(
                any("dropdb" in command and command[-1] == "remnawave" for command in runner.calls)
            )

    def test_failed_staging_restore_never_renames_or_drops_working_database(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dump = root / "panel.dump"
            dump.write_bytes(b"PGDMP" + b"x" * 2048)
            runner = DatabaseRunner(fail_restore=True)

            with self.assertRaises(CommandError):
                _restore_database(
                    runner,
                    inventory(root, role="panel"),
                    {"format": "postgres-custom", "user": "rw", "database": "remnawave"},
                    dump,
                )

            self.assertFalse(any("RENAME TO" in command[-1] for command in runner.calls))
            self.assertFalse(
                any("dropdb" in command and command[-1] == "remnawave" for command in runner.calls)
            )


if __name__ == "__main__":
    unittest.main()
