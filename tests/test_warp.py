from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from remnawave_manager.errors import TransactionError, ValidationError
from remnawave_manager.models import Component, Inventory, ManagedFile
from remnawave_manager.paths import RuntimePaths
from remnawave_manager.runner import Result, atomic_write_bytes, sha256_file
from remnawave_manager.state import StateStore
from remnawave_manager.warp import (
    WarpPaths,
    WarpScan,
    _assert_node_contract,
    _cleanup_failed_registration,
    _disable_legacy_cron,
    _generate_account,
    _health_units,
    _install_units,
    _is_active,
    _is_enabled,
    _normalized_ipv4_default_routes,
    _RejectRedirect,
    _restore_snapshot,
    _restore_unit_state,
    _revoke_staged_account,
    adopt_warp,
    install_warp,
    rotate_warp,
    scan_warp,
    uninstall_warp,
    warp_action,
    warp_status,
    warp_watchdog,
)

WARP_PROFILE = """[Interface]
PrivateKey = AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=
Address = 172.16.0.2/32, 2606:4700:110:8765::2/128
MTU = 1280
Table = off

[Peer]
PublicKey = ICEiIyQlJicoKSorLC0uLzAxMjM0NTY3ODk6Ozw9Pj8=
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = engage.cloudflareclient.com:2408
PersistentKeepalive = 25
"""

WARP_ACCOUNT = (
    'device_id = "01234567-89ab-cdef-0123-456789abcdef"\n'
    'access_token = "old-secret-token"\n'
)


def _store(root: Path) -> StateStore:
    store = StateStore(RuntimePaths(root))
    store.save_inventory(
        Inventory(
            schema_version=1,
            role="node",
            install_dir="/opt/remnanode",
            compose_file="/opt/remnanode/docker-compose.yml",
            env_file="/opt/remnanode/.env",
            webserver="nginx",
            components={"node": Component("node", "remnanode", "remnanode")},
        )
    )
    return store


def _inactive_systemd(args, **_kwargs):  # type: ignore[no-untyped-def]
    command = tuple(args)
    if command[:2] == ("systemctl", "is-active"):
        return Result(command, 3, "inactive\n", "")
    if command[:2] == ("systemctl", "is-enabled"):
        return Result(command, 1, "disabled\n", "")
    return Result(command, 0, "", "")


def _private(path: Path, mode: int) -> None:
    os.chmod(path, mode)


class WarpLifecycleTests(unittest.TestCase):
    def test_invariants_ignore_only_exact_manager_owned_warp_route(self) -> None:
        routes = json.dumps(
            [
                {
                    "dst": "default",
                    "gateway": "192.0.2.1",
                    "dev": "ens3",
                },
                {"dst": "default", "dev": "warp", "metric": 42760},
                {"dst": "default", "dev": "warp", "metric": 123},
            ]
        )

        normalized = json.loads(_normalized_ipv4_default_routes(routes))

        self.assertEqual(
            normalized,
            [
                {
                    "dev": "ens3",
                    "dst": "default",
                    "gateway": "192.0.2.1",
                },
                {"dev": "warp", "dst": "default", "metric": 123},
            ],
        )

    def _write_action_state(
        self,
        store: StateStore,
        *,
        desired_enabled: bool,
    ) -> tuple[WarpPaths, bytes]:
        paths = WarpPaths(store.paths)
        paths.config.parent.mkdir(parents=True, exist_ok=True)
        paths.config.write_text(WARP_PROFILE, encoding="utf-8")
        _private(paths.config, 0o600)
        paths.data.mkdir(parents=True, exist_ok=True)
        _private(paths.data, 0o700)
        paths.state.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "backend": "native",
                    "desired_enabled": desired_enabled,
                    "consecutive_failures": 2,
                    "restart_timestamps": [],
                    "owned_files": {},
                }
            ),
            encoding="utf-8",
        )
        _private(paths.state, 0o600)
        return paths, paths.state.read_bytes()

    def test_systemd_query_errors_are_fail_closed(self) -> None:
        runner = mock.Mock()
        runner.run.return_value = Result(
            ("systemctl",),
            1,
            "",
            "Failed to connect to bus: Connection refused\n",
        )

        with self.assertRaisesRegex(TransactionError, "активность"):
            _is_active(runner, "wg-quick@warp.service")
        with self.assertRaisesRegex(TransactionError, "автозапуск"):
            _is_enabled(runner, "wg-quick@warp.service")

    def test_known_inactive_and_disabled_systemd_states_are_accepted(self) -> None:
        runner = mock.Mock()
        runner.run.side_effect = [
            Result(("systemctl",), 3, "inactive\n", ""),
            Result(("systemctl",), 1, "disabled\n", ""),
        ]

        self.assertFalse(_is_active(runner, "wg-quick@warp.service"))
        self.assertFalse(_is_enabled(runner, "wg-quick@warp.service"))

    def test_transitional_systemd_state_is_fail_closed(self) -> None:
        runner = mock.Mock()
        runner.run.return_value = Result(
            ("systemctl",),
            3,
            "activating\n",
            "",
        )

        with self.assertRaisesRegex(TransactionError, "активность"):
            _is_active(runner, "wg-quick@warp.service")

    def test_restore_unit_preserves_runtime_enablement_and_activity(self) -> None:
        runner = mock.Mock()
        state = {"enablement": "enabled", "active": False}

        def systemd(args, **_kwargs):  # type: ignore[no-untyped-def]
            command = tuple(args)
            if command[:2] == ("systemctl", "is-enabled"):
                enabled = state["enablement"]
                return Result(command, 0 if enabled.startswith("enabled") else 1, enabled + "\n", "")
            if command[:2] == ("systemctl", "is-active"):
                active = bool(state["active"])
                return Result(command, 0 if active else 3, "active\n" if active else "inactive\n", "")
            if command[:2] == ("systemctl", "disable"):
                state["enablement"] = "disabled"
            elif command[:3] == ("systemctl", "enable", "--runtime"):
                state["enablement"] = "enabled-runtime"
            elif command[:2] == ("systemctl", "start"):
                state["active"] = True
            return Result(command, 0, "", "")

        runner.run.side_effect = systemd

        _restore_unit_state(
            runner,
            "wg-quick@warp.service",
            active=True,
            enablement="enabled-runtime",
        )

        self.assertEqual(
            state,
            {"enablement": "enabled-runtime", "active": True},
        )

    def test_restore_unit_continues_after_keyboard_interrupt(self) -> None:
        runner = mock.Mock()
        state = {"enablement": "enabled", "active": False}

        def systemd(args, **_kwargs):  # type: ignore[no-untyped-def]
            command = tuple(args)
            if command[:2] == ("systemctl", "disable"):
                raise KeyboardInterrupt("interrupted disable")
            if command[:2] == ("systemctl", "start"):
                state["active"] = True
            if command[:2] == ("systemctl", "is-enabled"):
                return Result(command, 0, str(state["enablement"]) + "\n", "")
            if command[:2] == ("systemctl", "is-active"):
                return Result(command, 0, "active\n", "")
            return Result(command, 0, "", "")

        runner.run.side_effect = systemd

        with self.assertRaisesRegex(TransactionError, "interrupted disable"):
            _restore_unit_state(
                runner,
                "wg-quick@warp.service",
                active=True,
                enablement="disabled",
            )

        self.assertTrue(state["active"])
        self.assertIn(
            ["systemctl", "start", "wg-quick@warp.service"],
            [call.args[0] for call in runner.run.call_args_list],
        )

    def test_restore_snapshot_continues_after_keyboard_interrupt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            first.write_bytes(b"mutated-first")
            second.write_bytes(b"mutated-second")
            snapshot = {
                first: (b"original-first", 0o600),
                second: (b"original-second", 0o600),
            }

            def restore(path, payload, *, mode):  # type: ignore[no-untyped-def]
                if path == first:
                    raise KeyboardInterrupt("first restore interrupted")
                return atomic_write_bytes(path, payload, mode=mode)

            with (
                mock.patch(
                    "remnawave_manager.warp.atomic_write_bytes",
                    side_effect=restore,
                ),
                self.assertRaisesRegex(TransactionError, "first restore interrupted"),
            ):
                _restore_snapshot(snapshot)

            self.assertEqual(second.read_bytes(), b"original-second")

    def test_scan_rejects_enabled_official_warp_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(Path(temporary))
            runner = mock.Mock()

            def systemd(args, **_kwargs):  # type: ignore[no-untyped-def]
                command = tuple(args)
                if command[:2] == ("systemctl", "is-active"):
                    return Result(command, 3, "inactive\n", "")
                if command == ("systemctl", "is-enabled", "warp-svc.service"):
                    return Result(command, 0, "enabled\n", "")
                return Result(command, 1, "disabled\n", "")

            runner.run.side_effect = systemd

            scan = scan_warp(runner, store.paths)

            self.assertTrue(any("warp-svc" in item for item in scan.conflicts))
            self.assertFalse(scan.safe_takeover)

    def test_node_contract_rejects_explicit_net_raw_drop(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(Path(temporary))
            inventory = store.load_inventory()
            compose = {
                "services": {
                    "remnanode": {
                        "network_mode": "host",
                        "cap_drop": ["NET_RAW"],
                        "cap_add": [],
                    }
                }
            }

            with (
                mock.patch(
                    "remnawave_manager.warp.inspect_compose", return_value=compose
                ),
                self.assertRaisesRegex(ValidationError, "NET_RAW"),
            ):
                _assert_node_contract(mock.Mock(), inventory)

    def test_node_contract_rejects_managed_configuration_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(root)
            inventory = store.load_inventory()
            managed = root / "managed-compose.yml"
            managed.write_text("changed\n", encoding="utf-8")
            inventory.managed_files = [
                ManagedFile(str(managed), "0" * 64, "compose")
            ]

            with (
                mock.patch("remnawave_manager.warp.inspect_compose") as inspect,
                self.assertRaisesRegex(ValidationError, "повторите rwm adopt"),
            ):
                _assert_node_contract(mock.Mock(), inventory)

            inspect.assert_not_called()

    def test_node_contract_reports_missing_node_service_as_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            inventory = _store(Path(temporary)).load_inventory()

            with (
                mock.patch(
                    "remnawave_manager.warp.inspect_compose",
                    return_value={"services": {"other": {"network_mode": "host"}}},
                ),
                self.assertRaisesRegex(ValidationError, "не найден сервис Node"),
            ):
                _assert_node_contract(mock.Mock(), inventory)

    def test_watchdog_persists_rate_limit_before_restart(self) -> None:
        runner = mock.Mock()
        state = {
            "schema_version": 1,
            "backend": "native",
            "desired_enabled": True,
            "consecutive_failures": 2,
            "restart_timestamps": [],
            "owned_files": {},
        }

        with (
            mock.patch("remnawave_manager.warp._read_state", return_value=state),
            mock.patch("remnawave_manager.warp._is_active", return_value=False),
            mock.patch("remnawave_manager.warp._latest_handshake", return_value=0),
            mock.patch(
                "remnawave_manager.warp._save_state",
                side_effect=TransactionError("state disk full"),
            ),
            mock.patch("remnawave_manager.warp._systemctl") as systemctl,
            self.assertRaisesRegex(TransactionError, "state disk full"),
        ):
            warp_watchdog(runner, mock.Mock())

        systemctl.assert_not_called()
        self.assertEqual(state["last_health"], "restart_pending")

    def test_watchdog_fresh_handshake_breaks_failure_sequence(self) -> None:
        state = {
            "schema_version": 1,
            "backend": "native",
            "desired_enabled": True,
            "consecutive_failures": 2,
            "restart_timestamps": [],
            "owned_files": {},
        }

        with (
            mock.patch("remnawave_manager.warp._read_state", return_value=state),
            mock.patch("remnawave_manager.warp._is_active", return_value=True),
            mock.patch(
                "remnawave_manager.warp._trace_via_warp",
                side_effect=TransactionError("trace failed"),
            ),
            mock.patch("remnawave_manager.warp._latest_handshake", return_value=950),
            mock.patch("remnawave_manager.warp.time.time", return_value=1000),
            mock.patch("remnawave_manager.warp._save_state") as save_state,
        ):
            result = warp_watchdog(mock.Mock(), mock.Mock())

        self.assertEqual(result, "degraded")
        self.assertEqual(state["consecutive_failures"], 0)
        save_state.assert_called_once()

    def test_status_sanitizes_trace_error_for_terminal_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(Path(temporary))
            self._write_action_state(store, desired_enabled=True)

            with (
                mock.patch("remnawave_manager.warp._latest_handshake", return_value=0),
                mock.patch("remnawave_manager.warp._is_active", return_value=True),
                mock.patch(
                    "remnawave_manager.warp._trace_via_warp",
                    side_effect=TransactionError("\x1b[31mpoison\u202e"),
                ),
            ):
                status = warp_status(mock.Mock(), store)

            self.assertIn("poison", status["error"])
            self.assertNotIn("\x1b", status["error"])
            self.assertNotIn("\u202e", status["error"])

    def test_scan_and_install_reject_interface_without_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(root)
            interface = root / "sys/class/net/warp"
            interface.mkdir(parents=True)
            runner = mock.Mock()
            runner.run.side_effect = _inactive_systemd

            scan = scan_warp(runner, store.paths)

            self.assertTrue(scan.interface_exists)
            self.assertTrue(any("warp.conf" in conflict for conflict in scan.conflicts))
            self.assertFalse(scan.safe_takeover)
            with (
                mock.patch("remnawave_manager.warp._assert_node_contract"),
                mock.patch("remnawave_manager.warp.create_backup") as backup,
                self.assertRaisesRegex(ValidationError, "конфликт"),
            ):
                install_warp(runner, store, accept_tos=True)

            backup.assert_not_called()

    def test_scan_rejects_non_directory_wireguard_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(root)
            wireguard = root / "etc/wireguard"
            wireguard.parent.mkdir(parents=True, exist_ok=True)
            wireguard.write_text("not a directory\n", encoding="utf-8")
            runner = mock.Mock()
            runner.run.side_effect = _inactive_systemd

            scan = scan_warp(runner, store.paths)

            self.assertTrue(
                any("не является каталогом" in item for item in scan.conflicts)
            )
            self.assertFalse(scan.safe_takeover)

    def test_scan_accepts_known_warp_native_layout_for_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(root)
            paths = WarpPaths(store.paths)
            paths.config.parent.mkdir(parents=True)
            paths.config.write_text(WARP_PROFILE, encoding="utf-8")
            _private(paths.config, 0o644)
            account = root / "root/wgcf-account.toml"
            account.parent.mkdir(parents=True)
            account.write_text("device_id = 'legacy'\n", encoding="utf-8")
            _private(account, 0o644)
            cron = root / "etc/cron.d/warp-native"
            cron.parent.mkdir(parents=True)
            cron.write_text(
                "*/10 * * * * root /opt/warp-native/watchdog\n", encoding="utf-8"
            )
            _private(cron, 0o644)
            (root / "opt/warp-native").mkdir(parents=True)
            legacy_binary = root / "usr/local/bin/wgcf"
            legacy_binary.parent.mkdir(parents=True)
            legacy_binary.write_bytes(b"legacy binary")
            runner = mock.Mock()

            def systemd(args, **_kwargs):  # type: ignore[no-untyped-def]
                command = tuple(args)
                if command == ("systemctl", "is-active", "wg-quick@warp.service"):
                    return Result(command, 0, "active\n", "")
                if command == ("systemctl", "is-enabled", "wg-quick@warp.service"):
                    return Result(command, 0, "enabled\n", "")
                if command[:2] == ("systemctl", "is-active"):
                    return Result(command, 3, "inactive\n", "")
                return Result(command, 1, "disabled\n", "")

            runner.run.side_effect = systemd

            scan = scan_warp(runner, store.paths)

            self.assertEqual(scan.config, str(paths.config))
            self.assertEqual(scan.account, str(account))
            self.assertTrue(scan.safe_takeover)
            self.assertIn(str(cron), scan.legacy_paths)
            self.assertIn(str(legacy_binary), scan.legacy_paths)

    def test_managed_scan_prefers_managed_account_and_lists_legacy_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(root)
            paths, _ = self._write_action_state(store, desired_enabled=True)
            paths.account.write_text(WARP_ACCOUNT, encoding="utf-8")
            _private(paths.account, 0o600)
            legacy_account = root / "root/wgcf-account.toml"
            legacy_account.parent.mkdir(parents=True)
            legacy_account.write_text(WARP_ACCOUNT, encoding="utf-8")
            _private(legacy_account, 0o600)
            runner = mock.Mock()
            runner.run.side_effect = _inactive_systemd

            scan = scan_warp(runner, store.paths)

            self.assertTrue(scan.manager_state)
            self.assertFalse(scan.safe_takeover)
            self.assertEqual(scan.account, str(paths.account))
            self.assertIn(str(legacy_account), scan.legacy_paths)
            self.assertFalse(any("несколько WARP account" in item for item in scan.conflicts))

    def test_managed_scan_rejects_missing_managed_account(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(root)
            _paths, _ = self._write_action_state(store, desired_enabled=True)
            legacy_account = root / "root/wgcf-account.toml"
            legacy_account.parent.mkdir(parents=True)
            legacy_account.write_text(WARP_ACCOUNT, encoding="utf-8")
            _private(legacy_account, 0o600)
            runner = mock.Mock()
            runner.run.side_effect = _inactive_systemd

            scan = scan_warp(runner, store.paths)

            self.assertTrue(scan.manager_state)
            self.assertIsNone(scan.account)
            self.assertTrue(
                any("account-файл отсутствует" in item for item in scan.conflicts)
            )

    def test_scan_and_rotate_reject_pending_registration_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(root)
            paths, _ = self._write_action_state(store, desired_enabled=False)
            pending = paths.data / (".rotate-" + "a" * 32)
            pending.mkdir(mode=0o700)
            (pending / "account.toml").write_text(
                "device_id = 'pending'\n",
                encoding="utf-8",
            )
            runner = mock.Mock()
            runner.run.side_effect = _inactive_systemd

            scan = scan_warp(runner, store.paths)

            self.assertTrue(any("незавершённой" in item for item in scan.conflicts))
            with (
                mock.patch("remnawave_manager.warp._assert_node_contract"),
                mock.patch("remnawave_manager.warp.create_backup") as backup,
                self.assertRaisesRegex(ValidationError, "незавершённой"),
            ):
                rotate_warp(runner, store, accept_tos=True)

            backup.assert_not_called()

    def test_scan_rejects_orphan_manager_health_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(root)
            paths = WarpPaths(store.paths)
            paths.health_service.parent.mkdir(parents=True)
            paths.health_service.write_text(
                "[Unit]\nX-Remnawave-Manager=true\n",
                encoding="utf-8",
            )
            runner = mock.Mock()
            runner.run.side_effect = _inactive_systemd

            scan = scan_warp(runner, store.paths)

            self.assertTrue(any("health units" in item for item in scan.conflicts))
            self.assertFalse(scan.safe_takeover)

    def test_install_late_failure_removes_new_binary_and_notice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(root)
            paths = WarpPaths(store.paths)
            binary = paths.bin_dir / "wgcf-2.2.32"
            notice = binary.with_name(binary.name + ".LICENSE.txt")
            runner = mock.Mock()
            runner.run.side_effect = _inactive_systemd

            def install_binary(target: Path, *, local_file=None):  # type: ignore[no-untyped-def]
                self.assertIsNone(local_file)
                target.mkdir(parents=True, exist_ok=True)
                binary.write_bytes(b"verified binary")
                notice.write_text("license\n", encoding="utf-8")
                return binary

            with (
                mock.patch("remnawave_manager.warp._assert_node_contract"),
                mock.patch(
                    "remnawave_manager.warp.wgcf_contract",
                    return_value={"version": "2.2.32"},
                ),
                mock.patch(
                    "remnawave_manager.warp.install_wgcf",
                    side_effect=install_binary,
                ),
                mock.patch("remnawave_manager.warp.create_backup"),
                mock.patch(
                    "remnawave_manager.warp._invariants",
                    return_value={"stable": "yes"},
                ),
                mock.patch(
                    "remnawave_manager.warp._generate_account",
                    side_effect=TransactionError("registration failed"),
                ),
                self.assertRaisesRegex(TransactionError, "registration failed"),
            ):
                install_warp(runner, store, accept_tos=True)

            self.assertFalse(binary.exists())
            self.assertFalse(notice.exists())
            self.assertFalse(paths.state.exists())
            self.assertFalse(paths.data.exists())

    def test_start_failure_restores_units_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(Path(temporary))
            paths, state_before = self._write_action_state(
                store,
                desired_enabled=False,
            )
            runner = mock.Mock()

            with (
                mock.patch("remnawave_manager.warp._assert_node_contract"),
                mock.patch("remnawave_manager.warp._is_active", return_value=False),
                mock.patch(
                    "remnawave_manager.warp._unit_enablement",
                    return_value="disabled",
                ),
                mock.patch(
                    "remnawave_manager.warp._invariants",
                    return_value={"route4": "unchanged"},
                ),
                mock.patch(
                    "remnawave_manager.warp._trace_via_warp",
                    side_effect=TransactionError("trace failed"),
                ),
                mock.patch("remnawave_manager.warp._systemctl") as systemctl,
                self.assertRaisesRegex(
                    TransactionError, "прежнее состояние восстановлено"
                ),
            ):
                from remnawave_manager.warp import warp_action

                warp_action(runner, store, "start")

            self.assertEqual(paths.state.read_bytes(), state_before)
            self.assertIn(
                mock.call(
                    runner,
                    "disable",
                    "--now",
                    "remnawave-warp-health.timer",
                ),
                systemctl.call_args_list,
            )
            self.assertIn(
                mock.call(
                    runner,
                    "stop",
                    "wg-quick@warp.service",
                ),
                systemctl.call_args_list,
            )
            self.assertIn(
                mock.call(runner, "disable", "wg-quick@warp.service"),
                systemctl.call_args_list,
            )

    def test_stop_disables_warp_autostart_and_watchdog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(Path(temporary))
            paths, _ = self._write_action_state(store, desired_enabled=True)
            runner = mock.Mock()

            with (
                mock.patch("remnawave_manager.warp._assert_node_contract"),
                mock.patch("remnawave_manager.warp._is_active", return_value=True),
                mock.patch(
                    "remnawave_manager.warp._unit_enablement",
                    return_value="enabled",
                ),
                mock.patch(
                    "remnawave_manager.warp._invariants",
                    return_value={"route4": "unchanged"},
                ),
                mock.patch("remnawave_manager.warp._systemctl") as systemctl,
            ):
                warp_action(runner, store, "stop")

            self.assertIn(
                mock.call(
                    runner,
                    "disable",
                    "--now",
                    "wg-quick@warp.service",
                ),
                systemctl.call_args_list,
            )
            self.assertIn(
                mock.call(
                    runner,
                    "disable",
                    "--now",
                    "remnawave-warp-health.timer",
                ),
                systemctl.call_args_list,
            )
            state = json.loads(paths.state.read_text(encoding="utf-8"))
            self.assertFalse(state["desired_enabled"])

    def test_actions_reject_state_left_by_standard_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(Path(temporary))
            paths, _ = self._write_action_state(store, desired_enabled=False)
            state = json.loads(paths.state.read_text(encoding="utf-8"))
            state["uninstalled_at"] = "2026-08-03T00:00:00Z"
            paths.state.write_text(json.dumps(state), encoding="utf-8")
            _private(paths.state, 0o600)
            runner = mock.Mock()

            with (
                mock.patch("remnawave_manager.warp._assert_node_contract"),
                self.assertRaisesRegex(ValidationError, "был удалён"),
            ):
                warp_action(runner, store, "start")

            runner.run.assert_not_called()

    def test_restart_success_sets_desired_state_and_enables_watchdog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(Path(temporary))
            paths, _ = self._write_action_state(store, desired_enabled=False)
            runner = mock.Mock()

            with (
                mock.patch("remnawave_manager.warp._assert_node_contract"),
                mock.patch("remnawave_manager.warp._is_active", return_value=False),
                mock.patch(
                    "remnawave_manager.warp._unit_enablement",
                    return_value="disabled",
                ),
                mock.patch(
                    "remnawave_manager.warp._invariants",
                    return_value={"route4": "unchanged"},
                ),
                mock.patch("remnawave_manager.warp._trace_via_warp", return_value="on"),
                mock.patch("remnawave_manager.warp._verify_container_visibility"),
                mock.patch("remnawave_manager.warp._systemctl") as systemctl,
            ):
                from remnawave_manager.warp import warp_action

                warp_action(runner, store, "restart")

            state = json.loads(paths.state.read_text(encoding="utf-8"))
            self.assertTrue(state["desired_enabled"])
            self.assertEqual(state["consecutive_failures"], 0)
            self.assertIn(
                mock.call(runner, "restart", "wg-quick@warp.service"),
                systemctl.call_args_list,
            )
            self.assertIn(
                mock.call(runner, "enable", "wg-quick@warp.service"),
                systemctl.call_args_list,
            )
            self.assertIn(
                mock.call(
                    runner,
                    "enable",
                    "--now",
                    "remnawave-warp-health.timer",
                ),
                systemctl.call_args_list,
            )

    def test_warp_plus_key_is_passed_only_through_wgcf_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            staging = Path(temporary)
            binary = staging / "wgcf"
            runner = mock.Mock()

            def run(args, **_kwargs):  # type: ignore[no-untyped-def]
                values = list(args)
                if "register" in values:
                    (staging / "account.toml").write_text(
                        "device_id = 'test'\n",
                        encoding="utf-8",
                    )
                if "generate" in values:
                    (staging / "profile.conf").write_text(
                        WARP_PROFILE,
                        encoding="utf-8",
                    )
                return Result(tuple(values), 0, "", "")

            runner.run.side_effect = run
            license_key = "warp-plus-key-that-must-stay-secret"

            with mock.patch.dict(
                os.environ,
                {
                    "REMNAWAVE_API_TOKEN": "unrelated-secret",
                    "WGCF_LICENSE_KEY": "inherited-license-that-must-not-win",
                },
                clear=False,
            ):
                account, profile = _generate_account(
                    runner,
                    binary,
                    staging,
                    license_key=license_key,
                )

            self.assertEqual(account, staging / "account.toml")
            self.assertEqual(profile.addresses[0], "172.16.0.2/32")
            commands = [call.args[0] for call in runner.run.call_args_list]
            self.assertNotIn(
                license_key,
                " ".join(value for command in commands for value in command),
            )
            update = next(
                call for call in runner.run.call_args_list if "update" in call.args[0]
            )
            self.assertEqual(update.kwargs["env"]["WGCF_LICENSE_KEY"], license_key)
            self.assertTrue(update.kwargs["sensitive"])
            for call in runner.run.call_args_list:
                self.assertNotIn("REMNAWAVE_API_TOKEN", call.kwargs["env"])
                if "update" not in call.args[0]:
                    self.assertNotIn("WGCF_LICENSE_KEY", call.kwargs["env"])

    def test_health_service_has_private_shared_runtime_lock_directory(self) -> None:
        service, _timer = _health_units()

        self.assertIn("RuntimeDirectory=remnawave-manager", service)
        self.assertIn("RuntimeDirectoryMode=0700", service)
        self.assertIn("RuntimeDirectoryPreserve=yes", service)
        self.assertIn("/run/remnawave-manager", service)
        self.assertNotIn("/run/lock", service)

    def test_failed_registration_revokes_new_cloudflare_device(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            account = Path(temporary) / "account.toml"
            device_id = "01234567-89ab-cdef-0123-456789abcdef"
            account.write_text(
                f'device_id = "{device_id}"\naccess_token = "secret-token"\n',
                encoding="utf-8",
            )
            opener = mock.Mock()

            class Response:
                status = 204

                def __enter__(self):  # type: ignore[no-untyped-def]
                    return self

                def __exit__(self, *_args):  # type: ignore[no-untyped-def]
                    return False

            opener.open.return_value = Response()

            with mock.patch(
                "remnawave_manager.warp.urllib.request.build_opener",
                return_value=opener,
            ) as build_opener:
                _revoke_staged_account(account)

            handlers = build_opener.call_args.args
            self.assertEqual(len(handlers), 2)
            self.assertIsInstance(handlers[0], urllib.request.ProxyHandler)
            self.assertEqual(handlers[0].proxies, {})
            self.assertIsInstance(handlers[1], _RejectRedirect)
            request = opener.open.call_args.args[0]
            self.assertEqual(request.get_method(), "DELETE")
            self.assertTrue(request.full_url.endswith(f"/reg/{device_id}"))
            self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")

    def test_failed_registration_closes_cloudflare_http_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            account = Path(temporary) / "account.toml"
            account.write_text(
                'device_id = "01234567-89ab-cdef-0123-456789abcdef"\n'
                'access_token = "secret-token"\n',
                encoding="utf-8",
            )
            error = urllib.error.HTTPError(
                "https://api.cloudflareclient.com/device",
                403,
                "Forbidden",
                {},
                io.BytesIO(b"forbidden"),
            )
            opener = mock.Mock()
            opener.open.side_effect = error

            with (
                mock.patch(
                    "remnawave_manager.warp.urllib.request.build_opener",
                    return_value=opener,
                ),
                mock.patch.object(error, "close", wraps=error.close) as close,
                self.assertRaisesRegex(TransactionError, "HTTP 403"),
            ):
                _revoke_staged_account(account)

            close.assert_called_once_with()

    def test_failed_registration_preserves_credentials_when_revoke_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            staging = parent / ".staging-test"
            staging.mkdir()
            account = staging / "account.toml"
            account.write_text("credentials\n", encoding="utf-8")

            with (
                mock.patch(
                    "remnawave_manager.warp._revoke_staged_account",
                    side_effect=TransactionError("Cloudflare unavailable"),
                ),
                self.assertRaisesRegex(TransactionError, "Cloudflare unavailable"),
            ):
                _cleanup_failed_registration(staging, parent)

            self.assertTrue(account.is_file())

    def test_watchdog_unit_uses_installed_rwm_entrypoint(self) -> None:
        service, timer = _health_units()

        self.assertIn("ExecStart=/usr/local/bin/rwm warp watchdog", service)
        self.assertIn("OnUnitActiveSec=5min", timer)
        self.assertIn("X-Remnawave-Manager=true", service)
        self.assertIn("X-Remnawave-Manager=true", timer)
        self.assertNotIn("curl", service)

    def test_install_units_refuses_foreign_service_without_overwriting_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = WarpPaths(RuntimePaths(Path(temporary)))
            paths.health_service.parent.mkdir(parents=True)
            original = "[Service]\nExecStart=/usr/local/bin/foreign-watchdog\n"
            paths.health_service.write_text(original, encoding="utf-8")

            with self.assertRaisesRegex(ValidationError, "создан не менеджером"):
                _install_units(mock.Mock(), paths)

            self.assertEqual(paths.health_service.read_text(encoding="utf-8"), original)
            self.assertFalse(paths.health_timer.exists())

    def test_install_units_refuses_foreign_vendor_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            paths = WarpPaths(RuntimePaths(Path(temporary)))
            foreign = (
                paths.runtime.root
                / "usr/lib/systemd/system"
                / paths.health_service.name
            )
            foreign.parent.mkdir(parents=True)
            foreign.write_text(
                "[Service]\nExecStart=/usr/bin/false\n", encoding="utf-8"
            )

            with self.assertRaisesRegex(ValidationError, "вне управляемого пути"):
                _install_units(mock.Mock(), paths)

            self.assertFalse(paths.health_service.exists())

    def test_legacy_cron_collision_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cron = root / "warp-native"
            target = root / "warp-native.disabled-by-remnawave-manager"
            cron.write_text("active\n", encoding="utf-8")
            target.write_text("keep\n", encoding="utf-8")

            with self.assertRaisesRegex(ValidationError, "уже существует"):
                _disable_legacy_cron(cron, target)

            self.assertEqual(cron.read_text(encoding="utf-8"), "active\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "keep\n")

    def test_legacy_cron_is_moved_without_clobbering(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cron = root / "warp-native"
            target = root / "warp-native.disabled-by-remnawave-manager"
            cron.write_text("active\n", encoding="utf-8")

            moved = _disable_legacy_cron(cron, target)

            self.assertEqual(moved, {str(cron): str(target)})
            self.assertFalse(cron.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "active\n")

    def test_takeover_failure_restores_files_services_and_legacy_cron(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(root)
            paths = WarpPaths(store.paths)
            paths.config.parent.mkdir(parents=True)
            paths.config.write_text(WARP_PROFILE, encoding="utf-8")
            _private(paths.config, 0o600)
            legacy_account = root / "root/wgcf-account.toml"
            legacy_account.parent.mkdir(parents=True)
            legacy_account.write_text("legacy account\n", encoding="utf-8")
            cron, cron_target = (
                root / "etc/cron.d/warp-native",
                root / "etc/cron.d/warp-native.disabled-by-remnawave-manager",
            )
            cron.parent.mkdir(parents=True)
            cron.write_text("legacy cron\n", encoding="utf-8")
            scan = WarpScan(
                config=str(paths.config),
                account=str(legacy_account),
                interface_exists=True,
                unit_active=False,
                manager_state=False,
                safe_takeover=True,
            )
            runner = mock.Mock()
            runner.run.side_effect = _inactive_systemd

            def install_binary(target: Path, *, local_file=None):  # type: ignore[no-untyped-def]
                self.assertIsNone(local_file)
                self.assertFalse(cron.exists())
                self.assertTrue(cron_target.is_file())
                binary = target / "wgcf-2.2.32"
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_bytes(b"new binary")
                binary.with_name(binary.name + ".LICENSE.txt").write_text(
                    "license\n", encoding="utf-8"
                )
                return binary

            with (
                mock.patch("remnawave_manager.warp._assert_node_contract"),
                mock.patch("remnawave_manager.warp.scan_warp", return_value=scan),
                mock.patch(
                    "remnawave_manager.warp.wgcf_contract",
                    return_value={"version": "2.2.32"},
                ),
                mock.patch(
                    "remnawave_manager.warp.install_wgcf",
                    side_effect=install_binary,
                ),
                mock.patch(
                    "remnawave_manager.warp._invariants",
                    return_value={"stable": "yes"},
                ),
                mock.patch("remnawave_manager.warp._trace_via_warp", return_value="on"),
                mock.patch("remnawave_manager.warp._verify_container_visibility"),
                mock.patch("remnawave_manager.warp.create_backup"),
                mock.patch(
                    "remnawave_manager.warp._save_state",
                    side_effect=RuntimeError("state write failed"),
                ),
                self.assertRaisesRegex(RuntimeError, "state write failed"),
            ):
                adopt_warp(runner, store, takeover=True)

            self.assertEqual(paths.config.read_text(encoding="utf-8"), WARP_PROFILE)
            self.assertFalse(paths.account.exists())
            self.assertFalse(paths.state.exists())
            self.assertFalse(paths.health_service.exists())
            self.assertFalse(paths.health_timer.exists())
            self.assertFalse((paths.bin_dir / "wgcf-2.2.32").exists())
            self.assertFalse((paths.bin_dir / "wgcf-2.2.32.LICENSE.txt").exists())
            self.assertEqual(cron.read_text(encoding="utf-8"), "legacy cron\n")
            self.assertFalse(cron_target.exists())

            commands = [call.args[0] for call in runner.run.call_args_list]
            self.assertIn(["systemctl", "stop", "wg-quick@warp.service"], commands)
            self.assertIn(["systemctl", "disable", "wg-quick@warp.service"], commands)

    def test_install_refuses_existing_account_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(root)
            paths = WarpPaths(store.paths)
            paths.account.parent.mkdir(parents=True)
            paths.account.write_text("foreign account\n", encoding="utf-8")
            runner = mock.Mock()
            runner.run.side_effect = _inactive_systemd

            with (
                mock.patch("remnawave_manager.warp._assert_node_contract"),
                mock.patch("remnawave_manager.warp.create_backup") as backup,
                self.assertRaisesRegex(ValidationError, "уже настроен"),
            ):
                install_warp(runner, store, accept_tos=True)

            self.assertEqual(scan_warp(runner, store.paths).account, str(paths.account))
            backup.assert_not_called()
            self.assertFalse(
                any(
                    call.args[0][:2] == ["apt-get", "update"]
                    for call in runner.run.call_args_list
                )
            )

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks are unavailable")
    def test_uninstall_refuses_broken_symlink_instead_of_owned_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(root)
            paths = WarpPaths(store.paths)
            paths.data.mkdir(parents=True)
            _private(paths.data, 0o700)
            paths.health_service.parent.mkdir(parents=True)
            try:
                paths.health_service.symlink_to(root / "missing-unit")
            except OSError as error:
                self.skipTest(f"symbolic links unavailable: {error}")
            paths.state.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "desired_enabled": False,
                        "owned_files": {str(paths.health_service): "0" * 64},
                    }
                ),
                encoding="utf-8",
            )
            _private(paths.state, 0o600)

            with (
                mock.patch("remnawave_manager.warp.create_backup"),
                self.assertRaisesRegex(ValidationError, "символической ссылкой"),
            ):
                uninstall_warp(mock.Mock(), store)

            self.assertTrue(paths.health_service.is_symlink())

    def test_rotate_restores_stopped_desired_state_after_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(root)
            paths = WarpPaths(store.paths)
            paths.data.mkdir(parents=True)
            _private(paths.data, 0o700)
            paths.config.parent.mkdir(parents=True)
            paths.config.write_text(WARP_PROFILE, encoding="utf-8")
            _private(paths.config, 0o600)
            paths.account.write_text(WARP_ACCOUNT, encoding="utf-8")
            paths.health_timer.parent.mkdir(parents=True, exist_ok=True)
            paths.health_timer.write_text(
                "X-Remnawave-Manager=true\n",
                encoding="utf-8",
            )
            paths.bin_dir.mkdir(parents=True)
            previous_binary = paths.bin_dir / "wgcf-2.2.31"
            previous_binary.write_bytes(b"old wgcf")
            binary = paths.bin_dir / "wgcf-2.2.32"
            binary.write_bytes(b"wgcf")
            paths.state.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "backend": "native",
                        "desired_enabled": False,
                        "consecutive_failures": 0,
                        "restart_timestamps": [],
                        "owned_files": {
                            str(previous_binary): sha256_file(previous_binary)
                        },
                        "wgcf_version": "2.2.31",
                    }
                ),
                encoding="utf-8",
            )
            _private(paths.state, 0o600)
            runner = mock.Mock()
            profile = mock.Mock()
            profile.render.return_value = WARP_PROFILE

            def generate(_runner, _binary, staging, *, license_key):  # type: ignore[no-untyped-def]
                self.assertIsNone(license_key)
                account = staging / "account.toml"
                account.write_text("new account\n", encoding="utf-8")
                return account, profile

            revoked_payloads: list[str] = []

            def revoke_previous(account: Path) -> None:
                revoked_payloads.append(account.read_text(encoding="utf-8"))
                self.assertEqual(account.name, "previous-account.toml")
                self.assertEqual(paths.account.read_text(encoding="utf-8"), "new account\n")
                saved = json.loads(paths.state.read_text(encoding="utf-8"))
                self.assertEqual(saved["wgcf_version"], "2.2.32")

            with (
                mock.patch("remnawave_manager.warp._assert_node_contract"),
                mock.patch(
                    "remnawave_manager.warp._is_active",
                    side_effect=lambda _runner, unit: unit.startswith("remnawave-warp"),
                ),
                mock.patch(
                    "remnawave_manager.warp._unit_enablement",
                    side_effect=lambda _runner, unit: (
                        "enabled" if unit.startswith("wg-quick") else "disabled"
                    ),
                ),
                mock.patch(
                    "remnawave_manager.warp.wgcf_contract",
                    return_value={"version": "2.2.32"},
                ),
                mock.patch(
                    "remnawave_manager.warp.install_wgcf",
                    return_value=binary,
                ),
                mock.patch(
                    "remnawave_manager.warp._invariants", return_value={"stable": "yes"}
                ),
                mock.patch("remnawave_manager.warp._trace_via_warp", return_value="on"),
                mock.patch("remnawave_manager.warp._verify_container_visibility"),
                mock.patch(
                    "remnawave_manager.warp._generate_account", side_effect=generate
                ),
                mock.patch(
                    "remnawave_manager.warp._revoke_staged_account",
                    side_effect=revoke_previous,
                ),
                mock.patch("remnawave_manager.warp.create_backup"),
            ):
                rotate_warp(runner, store, accept_tos=True)

            self.assertEqual(revoked_payloads, [WARP_ACCOUNT])

            commands = [call.args[0] for call in runner.run.call_args_list]
            self.assertIn(["systemctl", "start", "wg-quick@warp.service"], commands)
            self.assertIn(["systemctl", "stop", "wg-quick@warp.service"], commands)
            self.assertIn(["systemctl", "enable", "wg-quick@warp.service"], commands)
            self.assertIn(
                ["systemctl", "disable", "remnawave-warp-health.timer"], commands
            )
            self.assertIn(
                ["systemctl", "start", "remnawave-warp-health.timer"], commands
            )
            saved = json.loads(paths.state.read_text(encoding="utf-8"))
            self.assertFalse(saved["desired_enabled"])
            self.assertEqual(saved["wgcf_version"], "2.2.32")
            self.assertFalse(previous_binary.exists())

    def test_rotate_keeps_new_profile_after_post_commit_revoke_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(Path(temporary))
            paths = WarpPaths(store.paths)
            paths.data.mkdir(parents=True)
            _private(paths.data, 0o700)
            paths.config.parent.mkdir(parents=True)
            paths.config.write_text(WARP_PROFILE, encoding="utf-8")
            _private(paths.config, 0o600)
            paths.account.write_text(WARP_ACCOUNT, encoding="utf-8")
            _private(paths.account, 0o600)
            paths.health_timer.parent.mkdir(parents=True, exist_ok=True)
            paths.health_timer.write_text(
                "X-Remnawave-Manager=true\n",
                encoding="utf-8",
            )
            paths.bin_dir.mkdir(parents=True)
            binary = paths.bin_dir / "wgcf-2.2.32"
            binary.write_bytes(b"wgcf")
            paths.state.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "backend": "native",
                        "desired_enabled": True,
                        "consecutive_failures": 0,
                        "restart_timestamps": [],
                        "owned_files": {},
                        "wgcf_version": "2.2.32",
                    }
                ),
                encoding="utf-8",
            )
            _private(paths.state, 0o600)
            profile = mock.Mock()
            new_profile = WARP_PROFILE.replace("172.16.0.2/32", "172.16.0.3/32")
            profile.render.return_value = new_profile

            def generate(_runner, _binary, staging, *, license_key):  # type: ignore[no-untyped-def]
                self.assertIsNone(license_key)
                account = staging / "account.toml"
                account.write_text("new account\n", encoding="utf-8")
                return account, profile

            with (
                mock.patch("remnawave_manager.warp._assert_node_contract"),
                mock.patch("remnawave_manager.warp._is_active", return_value=True),
                mock.patch(
                    "remnawave_manager.warp._unit_enablement",
                    return_value="enabled",
                ),
                mock.patch(
                    "remnawave_manager.warp.wgcf_contract",
                    return_value={"version": "2.2.32"},
                ),
                mock.patch(
                    "remnawave_manager.warp.install_wgcf",
                    return_value=binary,
                ),
                mock.patch(
                    "remnawave_manager.warp._generate_account",
                    side_effect=generate,
                ),
                mock.patch(
                    "remnawave_manager.warp._invariants",
                    return_value={"stable": "yes"},
                ),
                mock.patch("remnawave_manager.warp._trace_via_warp", return_value="on"),
                mock.patch("remnawave_manager.warp._verify_container_visibility"),
                mock.patch("remnawave_manager.warp.create_backup"),
                mock.patch(
                    "remnawave_manager.warp._revoke_staged_account",
                    side_effect=TransactionError("Cloudflare unavailable"),
                ),
                mock.patch("remnawave_manager.warp._restore_snapshot") as restore,
                self.assertRaisesRegex(TransactionError, "оставлена активной"),
            ):
                rotate_warp(mock.Mock(), store, accept_tos=True)

            restore.assert_not_called()
            self.assertEqual(paths.config.read_text(encoding="utf-8"), new_profile)
            self.assertEqual(paths.account.read_text(encoding="utf-8"), "new account\n")
            saved = json.loads(paths.state.read_text(encoding="utf-8"))
            self.assertIn("rotated_at", saved)
            staging = list(paths.data.glob(".rotate-*"))
            self.assertEqual(len(staging), 1)
            self.assertEqual(
                (staging[0] / "previous-account.toml").read_text(encoding="utf-8"),
                WARP_ACCOUNT,
            )
            self.assertEqual(
                (staging[0] / "account.toml").read_text(encoding="utf-8"),
                "new account\n",
            )

    def test_rotate_failure_restores_files_binary_and_all_unit_states(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(root)
            paths = WarpPaths(store.paths)
            paths.data.mkdir(parents=True, mode=0o700)
            _private(paths.data, 0o700)
            paths.config.parent.mkdir(parents=True)
            paths.config.write_text(WARP_PROFILE, encoding="utf-8")
            _private(paths.config, 0o600)
            paths.account.write_text(WARP_ACCOUNT, encoding="utf-8")
            _private(paths.account, 0o600)
            paths.health_timer.parent.mkdir(parents=True, exist_ok=True)
            paths.health_timer.write_text(
                "X-Remnawave-Manager=true\n",
                encoding="utf-8",
            )
            state = {
                "schema_version": 1,
                "backend": "native",
                "desired_enabled": True,
                "consecutive_failures": 0,
                "restart_timestamps": [],
                "owned_files": {
                    str(paths.config): sha256_file(paths.config),
                    str(paths.account): sha256_file(paths.account),
                },
            }
            paths.state.write_text(json.dumps(state), encoding="utf-8")
            _private(paths.state, 0o600)
            original = {
                paths.config: paths.config.read_bytes(),
                paths.account: paths.account.read_bytes(),
                paths.state: paths.state.read_bytes(),
            }
            binary = paths.bin_dir / "wgcf-2.2.32"
            notice = binary.with_name(binary.name + ".LICENSE.txt")
            runner = mock.Mock()
            profile = mock.Mock()
            profile.render.return_value = WARP_PROFILE.replace(
                "172.16.0.2/32",
                "172.16.0.3/32",
            )

            def install_binary(target: Path):  # type: ignore[no-untyped-def]
                target.mkdir(parents=True, exist_ok=True)
                binary.write_bytes(b"new verified binary")
                notice.write_text("license\n", encoding="utf-8")
                return binary

            def generate(_runner, _binary, staging, *, license_key):  # type: ignore[no-untyped-def]
                self.assertIsNone(license_key)
                account = staging / "account.toml"
                account.write_text("new account\n", encoding="utf-8")
                return account, profile

            with (
                mock.patch("remnawave_manager.warp._assert_node_contract"),
                mock.patch(
                    "remnawave_manager.warp._is_active",
                    side_effect=lambda _runner, unit: unit.startswith("wg-quick"),
                ),
                mock.patch(
                    "remnawave_manager.warp._unit_enablement",
                    side_effect=lambda _runner, unit: (
                        "enabled"
                        if unit.startswith("remnawave-warp")
                        else "disabled"
                    ),
                ),
                mock.patch(
                    "remnawave_manager.warp.wgcf_contract",
                    return_value={"version": "2.2.32"},
                ),
                mock.patch(
                    "remnawave_manager.warp.install_wgcf",
                    side_effect=install_binary,
                ),
                mock.patch(
                    "remnawave_manager.warp._generate_account",
                    side_effect=generate,
                ),
                mock.patch(
                    "remnawave_manager.warp._invariants",
                    return_value={"stable": "yes"},
                ),
                mock.patch(
                    "remnawave_manager.warp._trace_via_warp",
                    side_effect=TransactionError("new profile failed"),
                ),
                mock.patch("remnawave_manager.warp._revoke_staged_account"),
                mock.patch("remnawave_manager.warp.create_backup"),
                self.assertRaisesRegex(TransactionError, "new profile failed"),
            ):
                rotate_warp(runner, store, accept_tos=True)

            for path, payload in original.items():
                self.assertEqual(path.read_bytes(), payload)
            self.assertFalse(binary.exists())
            self.assertFalse(notice.exists())
            self.assertFalse(any(paths.data.glob(".rotate-*")))
            commands = [call.args[0] for call in runner.run.call_args_list]
            self.assertIn(["systemctl", "disable", "wg-quick@warp.service"], commands)
            self.assertIn(["systemctl", "start", "wg-quick@warp.service"], commands)
            self.assertIn(
                ["systemctl", "enable", "remnawave-warp-health.timer"],
                commands,
            )
            self.assertIn(
                ["systemctl", "stop", "remnawave-warp-health.timer"],
                commands,
            )

    def test_uninstall_never_deletes_unrecognized_file_from_owned_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(root)
            paths = WarpPaths(store.paths)
            paths.data.mkdir(parents=True, mode=0o700)
            _private(paths.data, 0o700)
            paths.bin_dir.mkdir(parents=True)
            unrelated = paths.bin_dir / "operator-data"
            unrelated.write_text("keep\n", encoding="utf-8")
            paths.state.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "backend": "native",
                        "desired_enabled": False,
                        "owned_files": {
                            str(unrelated): sha256_file(unrelated),
                        },
                    }
                ),
                encoding="utf-8",
            )
            _private(paths.state, 0o600)
            runner = mock.Mock()
            runner.run.side_effect = _inactive_systemd

            with mock.patch("remnawave_manager.warp.create_backup"):
                uninstall_warp(runner, store)

            self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep\n")

    def test_full_purge_removes_state_so_clean_install_is_possible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(root)
            paths = WarpPaths(store.paths)
            files = [
                paths.config,
                paths.account,
                paths.health_service,
                paths.health_timer,
                paths.bin_dir / "wgcf-2.2.32",
            ]
            for path in files:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(path.name + "\n", encoding="utf-8")
            paths.data.mkdir(parents=True, exist_ok=True)
            _private(paths.data, 0o700)
            paths.state.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "desired_enabled": False,
                        "wgcf_version": "2.2.32",
                        "owned_files": {str(path): sha256_file(path) for path in files},
                    }
                ),
                encoding="utf-8",
            )
            _private(paths.state, 0o600)

            runner = mock.Mock()
            runner.run.side_effect = _inactive_systemd
            with mock.patch("remnawave_manager.warp.create_backup"):
                uninstall_warp(runner, store, purge_credentials=True)

            self.assertFalse(paths.state.exists())
            self.assertFalse(paths.config.exists())
            self.assertFalse(paths.account.exists())
            self.assertFalse((paths.bin_dir / "wgcf-2.2.32").exists())

    def test_uninstall_checks_owned_files_before_stopping_warp(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(root)
            paths = WarpPaths(store.paths)
            paths.data.mkdir(parents=True)
            _private(paths.data, 0o700)
            paths.health_service.parent.mkdir(parents=True)
            paths.health_service.write_text("operator change\n", encoding="utf-8")
            paths.state.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "desired_enabled": True,
                        "owned_files": {str(paths.health_service): "0" * 64},
                    }
                ),
                encoding="utf-8",
            )
            _private(paths.state, 0o600)
            runner = mock.Mock()

            with (
                mock.patch("remnawave_manager.warp.create_backup") as backup,
                self.assertRaisesRegex(ValidationError, "изменён после установки"),
            ):
                uninstall_warp(runner, store)

            runner.run.assert_not_called()
            backup.assert_not_called()
            self.assertEqual(
                paths.health_service.read_text(encoding="utf-8"),
                "operator change\n",
            )

    def test_uninstall_failure_restores_files_and_exact_unit_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(root)
            paths = WarpPaths(store.paths)
            files = [paths.health_service, paths.health_timer]
            for path in files:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(path.name + "\n", encoding="utf-8")
            paths.data.mkdir(parents=True, exist_ok=True)
            _private(paths.data, 0o700)
            paths.state.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "desired_enabled": True,
                        "owned_files": {str(path): sha256_file(path) for path in files},
                    }
                ),
                encoding="utf-8",
            )
            _private(paths.state, 0o600)
            original_state = paths.state.read_bytes()
            runner = mock.Mock()

            with (
                mock.patch("remnawave_manager.warp.create_backup"),
                mock.patch(
                    "remnawave_manager.warp._is_active",
                    side_effect=lambda _runner, unit: unit.startswith("wg-quick"),
                ),
                mock.patch(
                    "remnawave_manager.warp._unit_enablement",
                    side_effect=lambda _runner, unit: (
                        "enabled"
                        if unit.startswith("remnawave-warp")
                        else "disabled"
                    ),
                ),
                mock.patch(
                    "remnawave_manager.warp._save_state",
                    side_effect=RuntimeError("state write failed"),
                ),
                self.assertRaisesRegex(
                    TransactionError, "прежнее состояние восстановлено"
                ),
            ):
                uninstall_warp(runner, store)

            self.assertTrue(all(path.is_file() for path in files))
            self.assertEqual(paths.state.read_bytes(), original_state)
            commands = [call.args[0] for call in runner.run.call_args_list]
            self.assertIn(
                ["systemctl", "start", "wg-quick@warp.service"],
                commands,
            )
            self.assertIn(
                ["systemctl", "disable", "wg-quick@warp.service"],
                commands,
            )
            self.assertIn(
                ["systemctl", "enable", "remnawave-warp-health.timer"],
                commands,
            )
            self.assertIn(
                ["systemctl", "stop", "remnawave-warp-health.timer"],
                commands,
            )


if __name__ == "__main__":
    unittest.main()
