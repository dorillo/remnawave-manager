from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remnawave_manager.backup_schedule import (
    SERVICE_NAME,
    TIMER_NAME,
    _restore,
    _restore_timer_state,
    _rollback_schedule,
    _snapshot,
    install_backup_schedule,
    remove_backup_schedule,
    render_backup_units,
)
from remnawave_manager.errors import TransactionError, ValidationError
from remnawave_manager.models import Inventory
from remnawave_manager.paths import RuntimePaths
from remnawave_manager.runner import Result
from remnawave_manager.state import StateStore


class ScheduleRunner:
    def __init__(
        self,
        *,
        enabled: bool = False,
        active: bool = False,
        fail_restart: bool = False,
        enablement: str | None = None,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.enablement = enablement or ("enabled" if enabled else "disabled")
        self.active = active
        self.fail_restart = fail_restart

    @property
    def enabled(self) -> bool:
        return self.enablement in {"enabled", "enabled-runtime"}

    def run(self, args, **kwargs):  # type: ignore[no-untyped-def]
        command = tuple(args)
        self.calls.append(command)
        if command[:2] == ("systemctl", "is-enabled"):
            returncode = (
                0
                if self.enablement in {"enabled", "enabled-runtime"}
                else 4
                if self.enablement == "not-found"
                else 1
            )
            return Result(command, returncode, self.enablement + "\n", "")
        if command[:2] == ("systemctl", "is-active"):
            active_state = "active" if self.active else "inactive"
            returncode = 0 if self.active else 3
            return Result(command, returncode, active_state + "\n", "")
        if command[:2] == ("systemctl", "unmask"):
            self.enablement = "disabled"
        elif command[:3] == ("systemctl", "enable", "--now"):
            self.enablement = "enabled"
            self.active = True
        elif command[:3] == ("systemctl", "disable", "--now"):
            self.enablement = "disabled"
            self.active = False
        elif command[:2] == ("systemctl", "enable"):
            self.enablement = (
                "enabled-runtime" if "--runtime" in command else "enabled"
            )
        elif command[:2] == ("systemctl", "disable"):
            self.enablement = "disabled"
        elif command[:2] == ("systemctl", "mask"):
            self.enablement = (
                "masked-runtime" if "--runtime" in command else "masked"
            )
        elif command[:2] == ("systemctl", "start"):
            self.active = True
        elif command[:2] == ("systemctl", "stop"):
            self.active = False
        if command[:2] == ("systemctl", "restart") and self.fail_restart:
            self.fail_restart = False
            raise RuntimeError("restart failed")
        if "NextElapseUSecRealtime" in command:
            return Result(command, 0, "Sun 2026-08-09 03:15:00 UTC\n", "")
        return Result(command, 0, "", "")


class BackupScheduleTests(unittest.TestCase):
    def test_masked_active_timer_is_unmasked_started_and_remasked(self) -> None:
        runner = ScheduleRunner(enablement="masked-runtime", active=False)

        _restore_timer_state(
            runner,  # type: ignore[arg-type]
            enablement="masked-runtime",
            active=True,
            unit_existed=True,
        )

        self.assertEqual(
            (runner.enablement, runner.active),
            ("masked-runtime", True),
        )
        self.assertLess(
            runner.calls.index(("systemctl", "start", TIMER_NAME)),
            runner.calls.index(("systemctl", "mask", "--runtime", TIMER_NAME)),
        )

    def test_timer_verifies_activity_after_enablement_query_interrupt(self) -> None:
        runner = ScheduleRunner(enablement="disabled", active=False)
        original_run = runner.run
        verification_started = False

        def interrupt_enablement(args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal verification_started
            command = tuple(args)
            if command[:2] == ("systemctl", "is-enabled"):
                verification_started = True
                raise KeyboardInterrupt
            return original_run(args, **kwargs)

        runner.run = interrupt_enablement  # type: ignore[method-assign]

        with self.assertRaisesRegex(TransactionError, "enablement verification"):
            _restore_timer_state(
                runner,  # type: ignore[arg-type]
                enablement="disabled",
                active=False,
                unit_existed=True,
            )

        self.assertTrue(verification_started)
        self.assertIn(("systemctl", "is-active", TIMER_NAME), runner.calls)

    def test_rollback_continues_after_second_keyboard_interrupt(self) -> None:
        runner = ScheduleRunner()
        timer = Path("/etc/systemd/system") / TIMER_NAME
        snapshot = {timer: None}

        with (
            mock.patch(
                "remnawave_manager.backup_schedule._restore",
                side_effect=KeyboardInterrupt,
            ),
            mock.patch(
                "remnawave_manager.backup_schedule._restore_timer_state"
            ) as restore_timer,
        ):
            errors = _rollback_schedule(
                runner,  # type: ignore[arg-type]
                snapshot,
                ("disabled", False),
                timer,
            )

        self.assertEqual(len(errors), 1)
        self.assertIn("файлы", errors[0])
        restore_timer.assert_called_once_with(
            runner,
            enablement="disabled",
            active=False,
            unit_existed=False,
        )

    def test_rollback_attempts_timer_restore_after_daemon_reload_interrupt(self) -> None:
        runner = ScheduleRunner(enabled=True, active=True)
        timer = Path("/etc/systemd/system") / TIMER_NAME
        snapshot = {timer: (b"timer", 0o644)}
        original_run = runner.run

        def interrupted_reload(args, **kwargs):  # type: ignore[no-untyped-def]
            if tuple(args) == ("systemctl", "daemon-reload"):
                raise KeyboardInterrupt
            return original_run(args, **kwargs)

        with (
            mock.patch("remnawave_manager.backup_schedule._restore"),
            mock.patch.object(runner, "run", side_effect=interrupted_reload),
        ):
            errors = _rollback_schedule(
                runner,  # type: ignore[arg-type]
                snapshot,
                ("disabled", False),
                timer,
            )

        self.assertEqual(len(errors), 1)
        self.assertIn("daemon-reload", errors[0])
        self.assertEqual((runner.enablement, runner.active), ("disabled", False))
        self.assertIn(("systemctl", "stop", TIMER_NAME), runner.calls)
        self.assertIn(("systemctl", "disable", TIMER_NAME), runner.calls)

    def test_file_restore_continues_after_independent_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "settings.json"
            second = root / "other.json"
            first.write_bytes(b"changed-first")
            second.write_bytes(b"changed-second")

            def fail_first(path: Path, data: bytes, *, mode: int) -> None:
                if path == first:
                    raise KeyboardInterrupt
                path.write_bytes(data)
                path.chmod(mode)

            with mock.patch(
                "remnawave_manager.backup_schedule.atomic_write_bytes",
                side_effect=fail_first,
            ), self.assertRaises(TransactionError) as raised:
                _restore(
                    {
                        first: (b"original-first", 0o600),
                        second: (b"original-second", 0o600),
                    }
                )

            self.assertIn(str(first), str(raised.exception))
            self.assertEqual(first.read_bytes(), b"changed-first")
            self.assertEqual(second.read_bytes(), b"original-second")

    def test_snapshot_rejects_hardlinked_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original = root / "original.timer"
            hardlink = root / TIMER_NAME
            original.write_text(
                "[Unit]\nX-Remnawave-Manager=true\n", encoding="utf-8"
            )
            hardlink.hardlink_to(original)

            with self.assertRaisesRegex(ValidationError, "hardlink"):
                _snapshot((hardlink,))

    def test_renders_fixed_weekly_calendar_and_restricted_service(self) -> None:
        service, timer = render_backup_units(
            frequency="weekly",
            time_of_day="03:15",
            retention=10,
        )

        self.assertIn("OnCalendar=Sun *-*-* 03:15:00", timer)
        self.assertIn("RandomizedDelaySec=10min", timer)
        self.assertIn("--retention 10", service)
        self.assertIn("TimeoutStartSec=infinity", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("ReadWritePaths=/etc/remnawave-manager ", service)
        self.assertIn("RuntimeDirectory=remnawave-manager", service)
        self.assertIn("RuntimeDirectoryMode=0700", service)
        self.assertIn("RuntimeDirectoryPreserve=yes", service)
        self.assertIn("/run/remnawave-manager", service)
        self.assertNotIn("/run/lock", service)
        self.assertNotIn("curl", service)

    def test_rejects_unvalidated_time(self) -> None:
        for value in ("3:15", "24:00", "03:15; reboot", "ночь"):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                render_backup_units(frequency="daily", time_of_day=value, retention=10)

    def test_install_and_remove_only_manager_owned_units(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(RuntimePaths(root))
            store.save_inventory(
                Inventory(
                    schema_version=1,
                    role="node",
                    install_dir="/opt/remnanode",
                    compose_file="/opt/remnanode/docker-compose.yml",
                    env_file=None,
                    webserver="nginx",
                )
            )
            runner = ScheduleRunner()

            status = install_backup_schedule(
                runner,
                store,
                frequency="daily",
                time_of_day="02:30",
                retention=7,
            )

            unit_root = root / "etc/systemd/system"
            self.assertTrue((unit_root / SERVICE_NAME).is_file())
            self.assertTrue((unit_root / TIMER_NAME).is_file())
            self.assertTrue(status.enabled)
            remove_backup_schedule(runner, store)
            self.assertFalse((unit_root / SERVICE_NAME).exists())
            self.assertFalse((unit_root / TIMER_NAME).exists())

    def test_remove_refuses_foreign_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(RuntimePaths(root))
            unit_root = root / "etc/systemd/system"
            unit_root.mkdir(parents=True)
            (unit_root / TIMER_NAME).write_text("[Timer]\nOnBootSec=1\n", encoding="utf-8")

            with self.assertRaises(ValidationError):
                remove_backup_schedule(ScheduleRunner(), store)  # type: ignore[arg-type]

    def test_install_refuses_to_overwrite_foreign_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(RuntimePaths(root))
            store.save_inventory(
                Inventory(
                    schema_version=1,
                    role="node",
                    install_dir="/opt/remnanode",
                    compose_file="/opt/remnanode/docker-compose.yml",
                    env_file=None,
                    webserver="nginx",
                )
            )
            unit_root = root / "etc/systemd/system"
            unit_root.mkdir(parents=True)
            foreign = unit_root / SERVICE_NAME
            original = "[Service]\nExecStart=/usr/local/bin/foreign-backup\n"
            foreign.write_text(original, encoding="utf-8")

            with self.assertRaises(ValidationError):
                install_backup_schedule(
                    ScheduleRunner(),  # type: ignore[arg-type]
                    store,
                    frequency="daily",
                    time_of_day="02:30",
                    retention=7,
                )

            self.assertEqual(foreign.read_text(encoding="utf-8"), original)

    def test_failed_install_restores_disabled_but_active_timer_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(RuntimePaths(root))
            store.save_inventory(
                Inventory(
                    schema_version=1,
                    role="node",
                    install_dir="/opt/remnanode",
                    compose_file="/opt/remnanode/docker-compose.yml",
                    env_file=None,
                    webserver="nginx",
                )
            )
            unit_root = root / "etc/systemd/system"
            unit_root.mkdir(parents=True)
            service = unit_root / SERVICE_NAME
            timer = unit_root / TIMER_NAME
            old_service = b"[Unit]\nX-Remnawave-Manager=true\nold-service\n"
            old_timer = b"[Unit]\nX-Remnawave-Manager=true\nold-timer\n"
            service.write_bytes(old_service)
            timer.write_bytes(old_timer)
            runner = ScheduleRunner(enabled=False, active=True, fail_restart=True)

            with self.assertRaisesRegex(RuntimeError, "restart failed"):
                install_backup_schedule(
                    runner,  # type: ignore[arg-type]
                    store,
                    frequency="daily",
                    time_of_day="02:30",
                    retention=7,
                )

            self.assertEqual(service.read_bytes(), old_service)
            self.assertEqual(timer.read_bytes(), old_timer)
            self.assertEqual((runner.enablement, runner.active), ("disabled", True))

    def test_failed_install_restores_runtime_enablement_exactly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(RuntimePaths(root))
            store.save_inventory(
                Inventory(
                    schema_version=1,
                    role="node",
                    install_dir="/opt/remnanode",
                    compose_file="/opt/remnanode/docker-compose.yml",
                    env_file=None,
                    webserver="nginx",
                )
            )
            unit_root = root / "etc/systemd/system"
            unit_root.mkdir(parents=True)
            service = unit_root / SERVICE_NAME
            timer = unit_root / TIMER_NAME
            old_service = b"[Unit]\nX-Remnawave-Manager=true\nold-service\n"
            old_timer = b"[Unit]\nX-Remnawave-Manager=true\nold-timer\n"
            service.write_bytes(old_service)
            timer.write_bytes(old_timer)
            runner = ScheduleRunner(
                active=False,
                fail_restart=True,
                enablement="enabled-runtime",
            )

            with self.assertRaisesRegex(RuntimeError, "restart failed"):
                install_backup_schedule(
                    runner,  # type: ignore[arg-type]
                    store,
                    frequency="daily",
                    time_of_day="02:30",
                    retention=7,
                )

            self.assertEqual(service.read_bytes(), old_service)
            self.assertEqual(timer.read_bytes(), old_timer)
            self.assertEqual(
                (runner.enablement, runner.active),
                ("enabled-runtime", False),
            )


if __name__ == "__main__":
    unittest.main()
