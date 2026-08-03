from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remnawave_manager.backup import BackupResult
from remnawave_manager.errors import TransactionError, ValidationError
from remnawave_manager.maintenance import _restore_schedule_runtime, archive_stack
from remnawave_manager.models import Inventory, ManagedFile
from remnawave_manager.paths import RuntimePaths
from remnawave_manager.runner import Result, Runner, sha256_file
from remnawave_manager.state import StateStore


class MaintenanceTests(unittest.TestCase):
    def test_schedule_runtime_restore_preserves_exact_enablement(self) -> None:
        state = ("enabled-runtime", False)
        runner = mock.Mock(spec=Runner)

        with mock.patch(
            "remnawave_manager.maintenance.restore_backup_schedule_runtime"
        ) as restore:
            _restore_schedule_runtime(runner, state)

        restore.assert_called_once_with(runner, state)

    def test_archive_records_recovery_metadata_before_schedule_disable_and_down(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "opt/remnanode"
            install.mkdir(parents=True)
            compose = install / "docker-compose.yml"
            compose.write_text("services: {}\n", encoding="utf-8")
            store = StateStore(RuntimePaths(root))
            store.save_inventory(
                Inventory(
                    schema_version=1,
                    role="node",
                    install_dir=str(install),
                    compose_file=str(compose),
                    env_file=None,
                    webserver="nginx",
                    managed_files=[
                        ManagedFile(str(compose), sha256_file(compose), "compose")
                    ],
                )
            )
            store.save_settings(
                {
                    "registry": "docker-hub",
                    "backup_retention": 10,
                    "backup_schedule": {
                        "frequency": "daily",
                        "time": "03:15",
                        "retention": 7,
                    },
                }
            )
            backup_path = store.paths.backups / "verified-node.tar.gz"
            backup_path.write_bytes(b"archive")
            created = {"remnanode", "stopped-worker"}
            running = {"remnanode"}
            snapshots_at_mutation: list[dict[str, object]] = []
            runner = mock.Mock(spec=Runner)

            def assert_journal_metadata() -> None:
                payload = json.loads(
                    (store.paths.state / "active-transaction.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(
                    payload["created_services"],
                    ["remnanode", "stopped-worker"],
                )
                self.assertEqual(payload["running_services"], ["remnanode"])
                self.assertEqual(
                    payload["archive_targets"]["install_directory"]["original"],
                    str(install),
                )
                self.assertEqual(
                    payload["archive_targets"]["inventory"]["original"],
                    str(store.paths.inventory),
                )
                snapshots_at_mutation.append(payload)

            def run(args, **_kwargs):  # type: ignore[no-untyped-def]
                if "ps" in args:
                    selected = created if "--all" in args else running
                    output = "\n".join(sorted(selected))
                    return Result(tuple(args), 0, output + ("\n" if output else ""), "")
                if "down" in args:
                    assert_journal_metadata()
                    created.clear()
                    running.clear()
                return Result(tuple(args), 0, "", "")

            def disable_schedule(_runner: Runner, _store: StateStore) -> None:
                assert_journal_metadata()

            runner.run.side_effect = run
            with (
                mock.patch(
                    "remnawave_manager.maintenance.create_backup",
                    return_value=BackupResult(backup_path, {}),
                ),
                mock.patch(
                    "remnawave_manager.maintenance.backup_schedule_runtime_state",
                    return_value=("enabled", True),
                ),
                mock.patch(
                    "remnawave_manager.maintenance.remove_backup_schedule",
                    side_effect=disable_schedule,
                ),
            ):
                archived = archive_stack(runner, store)

            self.assertEqual(len(snapshots_at_mutation), 2)
            targets = snapshots_at_mutation[0]["archive_targets"]
            self.assertIsInstance(targets, dict)
            self.assertEqual(
                targets["install_directory"]["archive"],  # type: ignore[index]
                str(archived.directory),
            )
            self.assertEqual(
                targets["inventory"]["archive"],  # type: ignore[index]
                str(archived.inventory),
            )

    def test_secondary_base_exception_is_reported_without_aborting_rollback_handler(self) -> None:
        class RollbackInterrupted(BaseException):
            pass

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "opt/remnanode"
            install.mkdir(parents=True)
            compose = install / "docker-compose.yml"
            compose.write_text("services: {}\n", encoding="utf-8")
            store = StateStore(RuntimePaths(root))
            store.save_inventory(
                Inventory(
                    schema_version=1,
                    role="node",
                    install_dir=str(install),
                    compose_file=str(compose),
                    env_file=None,
                    webserver="nginx",
                    managed_files=[
                        ManagedFile(str(compose), sha256_file(compose), "compose")
                    ],
                )
            )
            backup_path = root / "backup.tar.gz"
            backup_path.write_bytes(b"archive")
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = Result(("docker", "compose"), 0, "", "")
            original_replace = __import__("os").replace
            original_rename = Path.rename

            def fail_inventory_move(source: Path, destination: Path) -> None:
                if Path(source) == store.paths.inventory:
                    raise OSError("inventory move failed")
                original_replace(source, destination)

            def interrupt_directory_rollback(source: Path, destination: Path) -> Path:
                if ".removed-" in source.name:
                    raise RollbackInterrupted("rollback interrupted")
                return original_rename(source, destination)

            with (
                mock.patch(
                    "remnawave_manager.maintenance.create_backup",
                    return_value=BackupResult(backup_path, {}),
                ),
                mock.patch(
                    "remnawave_manager.maintenance.os.replace",
                    side_effect=fail_inventory_move,
                ),
                mock.patch(
                    "remnawave_manager.maintenance.Path.rename",
                    side_effect=interrupt_directory_rollback,
                    autospec=True,
                ),
                self.assertRaisesRegex(TransactionError, "rollback interrupted"),
            ):
                archive_stack(runner, store)

    def test_archive_stack_keeps_recoverable_directory_and_removes_active_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "opt/remnanode"
            install.mkdir(parents=True)
            compose = install / "docker-compose.yml"
            env = install / ".env"
            compose.write_text("services: {}\n", encoding="utf-8")
            env.write_text("SECRET_KEY=value\n", encoding="utf-8")
            store = StateStore(RuntimePaths(root))
            store.save_inventory(
                Inventory(
                    schema_version=1,
                    role="node",
                    install_dir=str(install),
                    compose_file=str(compose),
                    env_file=str(env),
                    webserver="nginx",
                    managed_files=[
                        ManagedFile(str(compose), sha256_file(compose), "compose"),
                        ManagedFile(str(env), sha256_file(env), "env"),
                    ],
                )
            )
            store.initialize()
            store.paths.secrets.write_text("{}\n", encoding="utf-8")
            store.paths.secrets.chmod(0o600)
            backup_path = store.paths.backups / "verified-node.tar.gz"
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            backup_path.write_bytes(b"archive")
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = Result(("docker", "compose"), 0, "", "")

            with mock.patch(
                "remnawave_manager.maintenance.create_backup",
                return_value=BackupResult(backup_path, {}),
            ):
                archived = archive_stack(runner, store)

            self.assertFalse(install.exists())
            self.assertTrue(archived.directory.is_dir())
            self.assertTrue((archived.directory / ".env").is_file())
            self.assertFalse(store.paths.inventory.exists())
            self.assertTrue(archived.inventory.is_file())
            self.assertIsNotNone(archived.secrets)
            self.assertTrue(archived.secrets.is_file())  # type: ignore[union-attr]
            commands = [call.args[0] for call in runner.run.call_args_list]
            self.assertTrue(any("down" in command for command in commands))
            self.assertTrue(all("--volumes" not in command for command in commands))
            self.assertFalse(
                (store.paths.state / "active-transaction.json").exists()
            )

    def test_inventory_archive_failure_restores_directory_and_restarts_stack(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "opt/remnawave"
            install.mkdir(parents=True)
            compose = install / "docker-compose.yml"
            compose.write_text("services: {}\n", encoding="utf-8")
            store = StateStore(RuntimePaths(root))
            store.save_inventory(
                Inventory(
                    schema_version=1,
                    role="panel",
                    install_dir=str(install),
                    compose_file=str(compose),
                    env_file=None,
                    webserver="nginx",
                    managed_files=[
                        ManagedFile(str(compose), sha256_file(compose), "compose")
                    ],
                )
            )
            backup_path = root / "backup.tar.gz"
            backup_path.write_bytes(b"archive")
            runner = mock.Mock(spec=Runner)
            created = {"remnawave", "custom-sidecar", "stopped-worker"}
            running = {"remnawave", "custom-sidecar"}

            def run(args, **_kwargs):  # type: ignore[no-untyped-def]
                if "ps" in args:
                    selected = created if "--all" in args else running
                    return Result(
                        tuple(args),
                        0,
                        "\n".join(sorted(selected)) + ("\n" if selected else ""),
                        "",
                    )
                if "down" in args:
                    created.clear()
                    running.clear()
                elif "up" in args and "--no-start" in args:
                    pull = args.index("never")
                    created.update(args[pull + 1 :])
                elif "start" in args:
                    start = args.index("start")
                    running.update(args[start + 1 :])
                elif "stop" in args:
                    stop = args.index("stop")
                    running.difference_update(args[stop + 1 :])
                return Result(tuple(args), 0, "", "")

            runner.run.side_effect = run

            original_replace = __import__("os").replace

            def fail_inventory_move(source: Path, destination: Path) -> None:
                if Path(source) == store.paths.inventory:
                    raise OSError("inventory move failed")
                original_replace(source, destination)

            with (
                mock.patch(
                    "remnawave_manager.maintenance.create_backup",
                    return_value=BackupResult(backup_path, {}),
                ),
                mock.patch(
                    "remnawave_manager.maintenance.os.replace",
                    side_effect=fail_inventory_move,
                ),
                self.assertRaisesRegex(TransactionError, "inventory move failed"),
            ):
                archive_stack(runner, store)

            self.assertTrue(install.is_dir())
            self.assertTrue(store.paths.inventory.is_file())
            commands = [call.args[0] for call in runner.run.call_args_list]
            self.assertTrue(any("down" in command for command in commands))
            self.assertTrue(any("up" in command for command in commands))
            self.assertNotIn("--remove-orphans", next(command for command in commands if "down" in command))
            self.assertEqual(running, {"remnawave", "custom-sidecar"})
            self.assertEqual(created, {"remnawave", "custom-sidecar", "stopped-worker"})
            self.assertFalse(
                (store.paths.state / "active-transaction.json").exists()
            )
            recreate_call = next(
                call
                for call in runner.run.call_args_list
                if "--no-start" in call.args[0]
            )
            self.assertNotEqual(recreate_call.kwargs.get("check"), False)

    def test_archive_failure_keeps_previously_stopped_services_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "opt/remnanode"
            install.mkdir(parents=True)
            compose = install / "docker-compose.yml"
            compose.write_text("services: {}\n", encoding="utf-8")
            store = StateStore(RuntimePaths(root))
            store.save_inventory(
                Inventory(
                    schema_version=1,
                    role="node",
                    install_dir=str(install),
                    compose_file=str(compose),
                    env_file=None,
                    webserver="nginx",
                    managed_files=[
                        ManagedFile(str(compose), sha256_file(compose), "compose")
                    ],
                )
            )
            backup_path = root / "backup.tar.gz"
            backup_path.write_bytes(b"archive")
            created = {"nginx", "node"}
            running = {"nginx"}
            runner = mock.Mock(spec=Runner)

            def run(args, **_kwargs):  # type: ignore[no-untyped-def]
                if "ps" in args:
                    selected = created if "--all" in args else running
                    output = "\n".join(sorted(selected))
                    return Result(tuple(args), 0, output + ("\n" if output else ""), "")
                if "down" in args:
                    created.clear()
                    running.clear()
                elif "up" in args and "--no-start" in args:
                    created.update(args[args.index("never") + 1 :])
                elif "start" in args:
                    running.update(args[args.index("start") + 1 :])
                return Result(tuple(args), 0, "", "")

            runner.run.side_effect = run
            original_replace = __import__("os").replace

            def fail_inventory_move(source: Path, destination: Path) -> None:
                if Path(source) == store.paths.inventory:
                    raise OSError("inventory move failed")
                original_replace(source, destination)

            with (
                mock.patch(
                    "remnawave_manager.maintenance.create_backup",
                    return_value=BackupResult(backup_path, {}),
                ),
                mock.patch(
                    "remnawave_manager.maintenance.os.replace",
                    side_effect=fail_inventory_move,
                ),
                self.assertRaises(TransactionError),
            ):
                archive_stack(runner, store)

            self.assertEqual(running, {"nginx"})
            self.assertEqual(created, {"nginx", "node"})
            start_command = next(
                call.args[0]
                for call in runner.run.call_args_list
                if "start" in call.args[0]
            )
            self.assertEqual(start_command[-1], "nginx")

    def test_archive_refuses_non_file_secrets_before_docker_or_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "opt/remnanode"
            install.mkdir(parents=True)
            compose = install / "docker-compose.yml"
            compose.write_text("services: {}\n", encoding="utf-8")
            store = StateStore(RuntimePaths(root))
            store.save_inventory(
                Inventory(
                    schema_version=1,
                    role="node",
                    install_dir=str(install),
                    compose_file=str(compose),
                    env_file=None,
                    webserver="nginx",
                    managed_files=[
                        ManagedFile(str(compose), sha256_file(compose), "compose")
                    ],
                )
            )
            store.paths.secrets.mkdir(parents=True)
            runner = mock.Mock(spec=Runner)

            with (
                mock.patch(
                    "remnawave_manager.maintenance.create_backup"
                ) as create_backup,
                self.assertRaisesRegex(ValidationError, "secrets"),
            ):
                archive_stack(runner, store)

            create_backup.assert_not_called()
            runner.run.assert_not_called()

    @unittest.skipUnless(hasattr(os, "link"), "hard links are unavailable")
    def test_archive_refuses_hardlinked_secrets_before_docker_or_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "opt/remnanode"
            install.mkdir(parents=True)
            compose = install / "docker-compose.yml"
            compose.write_text("services: {}\n", encoding="utf-8")
            store = StateStore(RuntimePaths(root))
            store.save_inventory(
                Inventory(
                    schema_version=1,
                    role="node",
                    install_dir=str(install),
                    compose_file=str(compose),
                    env_file=None,
                    webserver="nginx",
                    managed_files=[
                        ManagedFile(str(compose), sha256_file(compose), "compose")
                    ],
                )
            )
            store.initialize()
            store.paths.secrets.write_text("{}\n", encoding="utf-8")
            store.paths.secrets.chmod(0o600)
            try:
                os.link(store.paths.secrets, root / "secrets-hardlink.json")
            except OSError as error:
                self.skipTest(f"hard links unavailable: {error}")
            runner = mock.Mock(spec=Runner)

            with (
                mock.patch(
                    "remnawave_manager.maintenance.create_backup"
                ) as create_backup,
                self.assertRaisesRegex(ValidationError, "hardlink"),
            ):
                archive_stack(runner, store)

            create_backup.assert_not_called()
            runner.run.assert_not_called()

    def test_archive_refuses_managed_file_change_during_backup_before_down(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "opt/remnanode"
            install.mkdir(parents=True)
            compose = install / "docker-compose.yml"
            compose.write_text("services: {}\n", encoding="utf-8")
            store = StateStore(RuntimePaths(root))
            store.save_inventory(
                Inventory(
                    schema_version=1,
                    role="node",
                    install_dir=str(install),
                    compose_file=str(compose),
                    env_file=None,
                    webserver="nginx",
                    managed_files=[
                        ManagedFile(str(compose), sha256_file(compose), "compose")
                    ],
                )
            )
            backup_path = root / "backup.tar.gz"
            backup_path.write_bytes(b"archive")
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = Result(("docker", "compose"), 0, "", "")

            def mutate_after_backup(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                compose.write_text("services:\n  changed: {}\n", encoding="utf-8")
                return BackupResult(backup_path, {})

            with (
                mock.patch(
                    "remnawave_manager.maintenance.create_backup",
                    side_effect=mutate_after_backup,
                ),
                self.assertRaisesRegex(ValidationError, "изменились"),
            ):
                archive_stack(runner, store)

            commands = [call.args[0] for call in runner.run.call_args_list]
            self.assertFalse(any("down" in command for command in commands))
            self.assertFalse(
                (store.paths.state / "active-transaction.json").exists()
            )


if __name__ == "__main__":
    unittest.main()
