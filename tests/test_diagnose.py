from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remnawave_manager.api import REALITY_RECOVERY_NAME
from remnawave_manager.diagnose import (
    Check,
    _bootstrap_credentials_check,
    _certbot_renewal_checks,
    _firewall_transaction_checks,
    _legacy_log_check,
    _manager_storage_checks,
    _reality_credentials_check,
    _repair_regular_file,
    _runtime_dependencies,
    _unexpected_exposed_ports,
    repair_permissions,
    run_diagnostics,
)
from remnawave_manager.errors import ValidationError
from remnawave_manager.models import Component, Inventory, ManagedFile
from remnawave_manager.paths import RuntimePaths
from remnawave_manager.runner import Result, Runner, sha256_file
from remnawave_manager.state import StateStore


def _store(root: Path) -> StateStore:
    store = StateStore(RuntimePaths(root))
    store.save_inventory(
        Inventory(
            schema_version=1,
            role="panel",
            install_dir="/opt/remnawave",
            compose_file="/opt/remnawave/docker-compose.yml",
            env_file=None,
            webserver="nginx",
        )
    )
    return store


class DiagnoseTests(unittest.TestCase):
    def test_reality_recovery_is_reported_without_exposing_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(Path(temporary))
            recovery = store.paths.state / REALITY_RECOVERY_NAME
            secret = "node-secret-that-must-not-be-printed"
            recovery.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "profile_uuid": "11111111-1111-4111-8111-111111111111",
                        "inbound_uuid": "22222222-2222-4222-8222-222222222222",
                        "node_uuid": "33333333-3333-4333-8333-333333333333",
                        "host_uuid": "44444444-4444-4444-8444-444444444444",
                        "secret_key": secret,
                    }
                ),
                encoding="utf-8",
            )
            if os.name == "posix":
                recovery.chmod(0o600)

            check = _reality_credentials_check(store)

            self.assertIsNotNone(check)
            self.assertEqual(check.level, "warning")  # type: ignore[union-attr]
            self.assertNotIn(secret, check.detail)  # type: ignore[union-attr]

    def test_bootstrap_credentials_diagnostics_report_recovery_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install_dir = Path(temporary) / "panel"
            install_dir.mkdir()
            credentials = install_dir / ".bootstrap-credentials.json"
            credentials.write_text(
                json.dumps(
                    {
                        "имя_администратора": "rwm-admin",
                        "пароль_администратора": "generated-secret",
                    }
                ),
                encoding="utf-8",
            )
            os.chmod(credentials, 0o600)
            current = Inventory(
                schema_version=1,
                role="panel",
                install_dir=str(install_dir),
                compose_file=str(install_dir / "docker-compose.yml"),
                env_file=None,
                webserver="nginx",
            )

            check = _bootstrap_credentials_check(current)

            self.assertIsNotNone(check)
            assert check is not None
            self.assertEqual(check.level, "warning")
            self.assertIn(str(credentials), check.detail)
            self.assertNotIn("generated-secret", check.detail)

            credentials.unlink()
            self.assertIsNone(_bootstrap_credentials_check(current))

    def test_check_sanitizes_external_diagnostic_text(self) -> None:
        check = Check("error", "name\u202e", "detail\x1b[31m\rspoof\x00")

        self.assertEqual(check.name, "name")
        self.assertNotIn("\x1b", check.detail)
        self.assertNotIn("\r", check.detail)
        self.assertNotIn("\x00", check.detail)

    def test_firewall_transaction_diagnostics_accept_absent_and_empty_root(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(Path(temporary))
            transaction_root = store.paths.state / "firewall-transactions"

            absent = _firewall_transaction_checks(store)
            transaction_root.mkdir(mode=0o700)
            empty = _firewall_transaction_checks(store)

            self.assertEqual([check.level for check in absent], ["ok"])
            self.assertEqual([check.level for check in empty], ["ok"])

    def test_firewall_transaction_diagnostics_report_valid_orphan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(Path(temporary))
            transaction_root = store.paths.state / "firewall-transactions"
            transaction_root.mkdir(mode=0o700)
            transaction = transaction_root / f"ufw-{'a' * 32}"
            transaction.mkdir(mode=0o700)
            manifest = transaction / "manifest.json"
            manifest.write_text(
                json.dumps({"schema_version": 1, "active": True, "files": []}),
                encoding="utf-8",
            )
            os.chmod(manifest, 0o600)

            checks = _firewall_transaction_checks(store)

            self.assertEqual([check.level for check in checks], ["error"])
            self.assertIn("незавершённая UFW-транзакция", checks[0].detail)
            self.assertIn("manifest корректен", checks[0].detail)
            self.assertTrue(transaction.is_dir())

    def test_firewall_transaction_diagnostics_reject_unsafe_root_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(Path(temporary))
            transaction_root = store.paths.state / "firewall-transactions"
            transaction_root.write_text("not a directory", encoding="utf-8")

            checks = _firewall_transaction_checks(store)

            self.assertEqual([check.level for check in checks], ["error"])
            self.assertIn("ожидается обычный каталог", checks[0].detail)

    def test_firewall_transaction_diagnostics_reject_unsafe_entry_type(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(Path(temporary))
            transaction_root = store.paths.state / "firewall-transactions"
            transaction_root.mkdir(mode=0o700)
            entry = transaction_root / f"ufw-{'b' * 32}"
            entry.write_text("not a directory", encoding="utf-8")

            checks = _firewall_transaction_checks(store)

            self.assertEqual([check.level for check in checks], ["error"])
            self.assertIn("ожидается обычный каталог", checks[0].detail)

    @unittest.skipUnless(hasattr(os, "link"), "hard links are unavailable")
    def test_firewall_transaction_diagnostics_reject_hardlinked_manifest(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(Path(temporary))
            transaction_root = store.paths.state / "firewall-transactions"
            transaction_root.mkdir(mode=0o700)
            transaction = transaction_root / f"ufw-{'c' * 32}"
            transaction.mkdir(mode=0o700)
            manifest = transaction / "manifest.json"
            manifest.write_text('{"schema_version": 1}', encoding="utf-8")
            os.chmod(manifest, 0o600)
            try:
                os.link(manifest, transaction / "manifest-hardlink.json")
            except OSError as error:
                self.skipTest(f"hard links unavailable: {error}")

            checks = _firewall_transaction_checks(store)

            self.assertEqual([check.level for check in checks], ["error"])
            self.assertIn("hardlink", checks[0].detail)

    def test_firewall_transaction_diagnostics_reject_missing_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(Path(temporary))
            transaction_root = store.paths.state / "firewall-transactions"
            transaction_root.mkdir(mode=0o700)
            (transaction_root / f"ufw-{'d' * 32}").mkdir(mode=0o700)

            checks = _firewall_transaction_checks(store)

            self.assertEqual([check.level for check in checks], ["error"])
            self.assertIn("manifest небезопасен или повреждён", checks[0].detail)

    def test_manager_storage_checks_cover_directories_and_state_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(Path(temporary))

            checks = _manager_storage_checks(store)

            details = "\n".join(check.detail for check in checks)
            for path in (
                store.paths.etc,
                store.paths.state,
                store.paths.backups,
                store.paths.logs,
                store.paths.inventory,
                store.paths.settings,
                store.paths.secrets,
            ):
                self.assertIn(str(path), details)
            optional = [
                check
                for check in checks
                if str(store.paths.settings) in check.detail
                or str(store.paths.secrets) in check.detail
            ]
            self.assertTrue(optional)
            self.assertTrue(all(check.level == "ok" for check in optional))

    @unittest.skipUnless(hasattr(os, "link"), "hard links are unavailable")
    def test_manager_storage_checks_reject_hardlinked_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(Path(temporary))
            try:
                os.link(
                    store.paths.inventory,
                    store.paths.inventory.with_suffix(".hardlink"),
                )
            except OSError as error:
                self.skipTest(f"hard links unavailable: {error}")

            checks = _manager_storage_checks(store)

            inventory_check = next(
                check for check in checks if str(store.paths.inventory) in check.detail
            )
            self.assertEqual(inventory_check.level, "error")
            self.assertIn("hardlink", inventory_check.detail)

    @unittest.skipUnless(os.name == "posix", "POSIX modes are unavailable")
    def test_repair_permissions_can_recover_world_readable_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(Path(temporary))
            store.paths.inventory.chmod(0o644)

            changed = repair_permissions(store)

            self.assertIn(str(store.paths.inventory), changed)
            self.assertEqual(stat.S_IMODE(store.paths.inventory.stat().st_mode), 0o600)

    def test_runtime_dependency_list_covers_host_and_optional_warp_tools(self) -> None:
        current = Inventory(
            schema_version=1,
            role="node",
            install_dir="/opt/remnanode",
            compose_file="/opt/remnanode/docker-compose.yml",
            env_file=None,
            webserver="nginx",
            features={"warp": True},
        )

        dependencies = set(_runtime_dependencies(current))

        self.assertTrue(
            {
                "docker",
                "systemctl",
                "ss",
                "ip",
                "sshd",
                "ufw",
                "certbot",
                "sysctl",
                "modprobe",
                "wg",
                "wg-quick",
                "nft",
            }
            <= dependencies
        )

    def test_flags_every_public_port_of_sensitive_inventory_services(self) -> None:
        current = Inventory(
            schema_version=1,
            role="panel",
            install_dir="/opt/remnawave",
            compose_file="/opt/remnawave/docker-compose.yml",
            env_file=None,
            webserver="nginx",
            components={
                "panel": Component("panel", "panel-service"),
                "subscription": Component("subscription", "subscription-service"),
                "database": Component("database", "database-service"),
                "cache": Component("cache", "cache-service"),
                "nginx": Component("nginx", "nginx-service"),
            },
        )
        compose = {
            "services": {
                "panel-service": {
                    "ports": [
                        {"target": 4100, "published": "44100", "host_ip": "0.0.0.0"}
                    ]
                },
                "subscription-service": {
                    "ports": [{"target": 4200, "published": "44200"}]
                },
                "database-service": {
                    "ports": [{"target": 5432, "published": "45432", "host_ip": "::"}]
                },
                "cache-service": {
                    "ports": [
                        {"target": 6379, "published": None, "host_ip": "192.0.2.10"}
                    ]
                },
                "nginx-service": {
                    "ports": [{"target": 443, "published": "443", "host_ip": "0.0.0.0"}]
                },
            }
        }

        exposed = _unexpected_exposed_ports(compose, current)

        self.assertEqual(
            exposed,
            [
                "panel-service:44100 на 0.0.0.0",
                "subscription-service:44200 на 0.0.0.0",
                "database-service:45432 на ::",
                "cache-service:динамический->6379 на 192.0.2.10",
            ],
        )

    def test_accepts_ipv4_and_ipv6_loopback_bindings(self) -> None:
        current = Inventory(
            schema_version=1,
            role="panel",
            install_dir="/opt/remnawave",
            compose_file="/opt/remnawave/docker-compose.yml",
            env_file=None,
            webserver="nginx",
            components={"panel": Component("panel", "panel-service")},
        )
        compose = {
            "services": {
                "panel-service": {
                    "ports": [
                        {"target": 3000, "published": "3000", "host_ip": "127.0.0.1"},
                        {"target": 3001, "published": "3001", "host_ip": "127.42.0.9"},
                        {"target": 3002, "published": "3002", "host_ip": "::1"},
                        {"target": 3003, "published": "3003", "host_ip": "[::1]"},
                    ]
                }
            }
        }

        self.assertEqual(_unexpected_exposed_ports(compose, current), [])

    def test_flags_host_networking_for_sensitive_services(self) -> None:
        current = Inventory(
            schema_version=1,
            role="panel",
            install_dir="/opt/remnawave",
            compose_file="/opt/remnawave/docker-compose.yml",
            env_file=None,
            webserver="nginx",
            components={"database": Component("database", "database-service")},
        )
        compose = {"services": {"database-service": {"network_mode": "host"}}}

        self.assertEqual(
            _unexpected_exposed_ports(compose, current),
            ["database-service:network_mode=host"],
        )

    def test_diagnoses_and_repairs_every_private_managed_file_kind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files: dict[str, Path] = {}
            for kind in ("compose", "env", "nginx", "secret", "site"):
                path = root / f"managed-{kind}"
                path.write_text(kind, encoding="utf-8")
                os.chmod(path, 0o644)
                files[kind] = path
            store = StateStore(RuntimePaths(root))
            inventory = Inventory(
                schema_version=1,
                role="node",
                install_dir=str(root),
                compose_file=str(files["compose"]),
                env_file=str(files["env"]),
                webserver="nginx",
                managed_files=[
                    ManagedFile(str(path), sha256_file(path), kind)
                    for kind, path in files.items()
                ],
            )
            store.save_inventory(inventory)
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = Result(("command",), 0, "", "")

            with (
                mock.patch(
                    "remnawave_manager.diagnose.inspect_compose",
                    return_value={"services": {}},
                ),
                mock.patch("remnawave_manager.diagnose.test_nginx"),
                mock.patch("remnawave_manager.diagnose.check_node_runtime"),
            ):
                checks = run_diagnostics(runner, store)

            permission_errors = {
                kind
                for kind, path in files.items()
                if any(
                    check.name == "Права файлов"
                    and check.level == "error"
                    and check.detail.startswith(f"{path}:")
                    for check in checks
                )
            }
            self.assertEqual(
                permission_errors,
                {
                    "compose",
                    "env",
                    "nginx",
                    "secret",
                },
            )

            changed = set(repair_permissions(store))

            self.assertTrue(
                {str(files[kind]) for kind in ("compose", "env", "nginx", "secret")}
                <= changed
            )
            self.assertNotIn(str(files["site"]), changed)
            if os.name == "posix":
                for kind in ("compose", "env", "nginx", "secret"):
                    self.assertEqual(files[kind].stat().st_mode & 0o777, 0o600)
                self.assertEqual(files["site"].stat().st_mode & 0o777, 0o644)

    def test_reports_and_repairs_world_readable_legacy_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(root)
            legacy_log = root / "usr/local/remnawave_reverse/remnawave_reverse.log"
            legacy_log.parent.mkdir(parents=True)
            legacy_log.write_text("история установки\n", encoding="utf-8")
            os.chmod(legacy_log, 0o644)

            check = _legacy_log_check(store)
            changed = repair_permissions(store)

            self.assertIsNotNone(check)
            self.assertEqual(check.level, "error")  # type: ignore[union-attr]
            self.assertIn("group/world", check.detail)  # type: ignore[union-attr]
            self.assertIn(str(legacy_log), changed)
            if os.name == "posix":
                self.assertEqual(legacy_log.stat().st_mode & 0o777, 0o600)

    def test_ignores_absent_legacy_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(Path(temporary))

            self.assertIsNone(_legacy_log_check(store))

    def test_repair_permissions_wraps_filesystem_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = _store(root)
            os.chmod(store.paths.etc, 0o755)

            operation = "os.fchmod" if os.name == "posix" else "os.chmod"
            with (
                mock.patch(
                    f"remnawave_manager.diagnose.{operation}",
                    side_effect=OSError("read-only filesystem"),
                ),
                self.assertRaisesRegex(
                    ValidationError,
                    "Не удалось безопасно восстановить права файлов",
                ) as raised,
            ):
                repair_permissions(store)

            self.assertIsInstance(raised.exception.__cause__, OSError)

    @unittest.skipUnless(hasattr(os, "link"), "hard links are unavailable")
    def test_repair_permissions_refuses_hardlinked_managed_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            managed = root / "managed.env"
            managed.write_text("SECRET=value\n", encoding="utf-8")
            store = StateStore(RuntimePaths(root))
            store.save_inventory(
                Inventory(
                    schema_version=1,
                    role="node",
                    install_dir=str(root),
                    compose_file=str(root / "docker-compose.yml"),
                    env_file=str(managed),
                    webserver="nginx",
                    managed_files=[
                        ManagedFile(
                            str(managed),
                            sha256_file(managed),
                            "env",
                        )
                    ],
                )
            )
            try:
                os.link(managed, root / "managed-hardlink.env")
            except OSError as error:
                self.skipTest(f"hard links unavailable: {error}")

            with self.assertRaisesRegex(ValidationError, "небезопасный тип"):
                repair_permissions(store)

    def test_certbot_diagnostics_require_timer_and_standalone_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for phase in ("deploy", "pre", "post"):
                hook = root / phase / "remnawave-manager-nginx"
                hook.parent.mkdir(parents=True, exist_ok=True)
                hook.write_text(
                    "#!/bin/sh\n# Managed by remnawave-manager\n",
                    encoding="utf-8",
                )
                if os.name == "posix":
                    os.chmod(hook, 0o700)
            current = Inventory(
                schema_version=1,
                role="node",
                install_dir="/opt/remnanode",
                compose_file="/opt/remnanode/docker-compose.yml",
                env_file=None,
                webserver="nginx",
                features={"certbot_renewal": True, "certbot_standalone": True},
            )
            runner = mock.Mock(spec=Runner)
            runner.run.side_effect = (
                Result(("systemctl",), 0, "enabled\n", ""),
                Result(("systemctl",), 0, "active\n", ""),
            )

            checks = _certbot_renewal_checks(runner, current, hook_root=root)

            self.assertEqual([check.level for check in checks], ["ok", "ok", "ok"])

    @unittest.skipUnless(
        os.name == "posix", "POSIX ownership and modes are unavailable"
    )
    def test_certbot_diagnostics_reject_writable_or_hardlinked_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hooks: list[Path] = []
            for phase in ("deploy", "pre", "post"):
                hook = root / phase / "remnawave-manager-nginx"
                hook.parent.mkdir(parents=True, exist_ok=True)
                hook.write_text(
                    "#!/bin/sh\n# Managed by remnawave-manager\n",
                    encoding="utf-8",
                )
                hook.chmod(0o700)
                hooks.append(hook)
            hooks[0].chmod(0o720)
            os.link(hooks[1], hooks[1].with_name("hardlink"))
            current = Inventory(
                schema_version=1,
                role="node",
                install_dir="/opt/remnanode",
                compose_file="/opt/remnanode/docker-compose.yml",
                env_file=None,
                webserver="nginx",
                features={"certbot_renewal": True, "certbot_standalone": True},
            )
            runner = mock.Mock(spec=Runner)
            runner.run.side_effect = (
                Result(("systemctl",), 0, "enabled\n", ""),
                Result(("systemctl",), 0, "active\n", ""),
            )

            checks = _certbot_renewal_checks(runner, current, hook_root=root)

            self.assertEqual(checks[-1].level, "error")
            self.assertIn(str(hooks[0]), checks[-1].detail)
            self.assertIn(str(hooks[1]), checks[-1].detail)

    def test_certbot_diagnostics_reject_oversized_hook_without_unbounded_read(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hook = root / "deploy" / "remnawave-manager-nginx"
            hook.parent.mkdir(parents=True)
            hook.write_bytes(b"#" * (1024 * 1024 + 1))
            if os.name == "posix":
                hook.chmod(0o700)
            current = Inventory(
                schema_version=1,
                role="node",
                install_dir="/opt/remnanode",
                compose_file="/opt/remnanode/docker-compose.yml",
                env_file=None,
                webserver="nginx",
                features={"certbot_renewal": True},
            )
            runner = mock.Mock(spec=Runner)
            runner.run.side_effect = (
                Result(("systemctl",), 0, "enabled\n", ""),
                Result(("systemctl",), 0, "active\n", ""),
            )

            checks = _certbot_renewal_checks(runner, current, hook_root=root)

            self.assertEqual(checks[-1].level, "error")
            self.assertIn(str(hook), checks[-1].detail)

    @unittest.skipUnless(os.name == "posix", "O_NONBLOCK is a POSIX hardening")
    def test_permission_repair_opens_regular_files_nonblocking(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "managed.env"
            path.write_text("SECRET=value\n", encoding="utf-8")
            path.chmod(0o600)
            with mock.patch(
                "remnawave_manager.diagnose.os.open",
                wraps=os.open,
            ) as open_file:
                _repair_regular_file(path, 0o600, label="Managed-файл")

            flags = open_file.call_args.args[1]
            self.assertTrue(flags & os.O_NONBLOCK)

    def test_diagnostics_treats_compose_config_as_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = _store(Path(temporary))
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = Result(("command",), 0, "", "")
            with (
                mock.patch(
                    "remnawave_manager.diagnose.inspect_compose",
                    return_value={"services": {}},
                ),
                mock.patch("remnawave_manager.diagnose.test_nginx"),
            ):
                run_diagnostics(runner, store)

            compose_call = next(
                call
                for call in runner.run.call_args_list
                if "compose" in call.args[0] and "config" in call.args[0]
            )
            self.assertTrue(compose_call.kwargs["sensitive"])

    def test_diagnostics_report_disk_error_without_skipping_later_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            compose = root / "docker-compose.yml"
            compose.write_text("services: {}\n", encoding="utf-8")
            store = StateStore(RuntimePaths(root))
            store.save_inventory(
                Inventory(
                    schema_version=1,
                    role="node",
                    install_dir=str(root),
                    compose_file=str(compose),
                    env_file=None,
                    webserver="nginx",
                    managed_files=[
                        ManagedFile(str(compose), sha256_file(compose), "compose")
                    ],
                    components={"node": Component("node", "remnanode")},
                )
            )
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = Result(("command",), 0, "", "")

            with (
                mock.patch(
                    "remnawave_manager.diagnose.shutil.disk_usage",
                    side_effect=OSError("disk unavailable"),
                ),
                mock.patch(
                    "remnawave_manager.diagnose.inspect_compose",
                    return_value={"services": {}},
                ),
                mock.patch("remnawave_manager.diagnose.test_nginx"),
                mock.patch("remnawave_manager.diagnose.check_node_runtime"),
            ):
                checks = run_diagnostics(runner, store)

            self.assertTrue(
                any(
                    check.name == "Свободное место"
                    and check.level == "error"
                    and "disk unavailable" in check.detail
                    for check in checks
                )
            )
            self.assertTrue(
                any(check.name == "Runtime" and check.level == "ok" for check in checks)
            )


if __name__ == "__main__":
    unittest.main()
