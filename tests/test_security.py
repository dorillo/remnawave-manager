from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remnawave_manager.certificates import CertificateMaterial
from remnawave_manager.errors import TransactionError, ValidationError
from remnawave_manager.install import render_panel_nginx
from remnawave_manager.models import Component, Inventory, ManagedFile
from remnawave_manager.paths import RuntimePaths
from remnawave_manager.runner import Result, atomic_write_text, sha256_file
from remnawave_manager.security import (
    _restore_unit_runtime,
    _snapshot_units,
    close_emergency_access,
    emergency_access_status,
    open_emergency_access,
    panel_access,
    rotate_panel_access,
)
from remnawave_manager.state import StateStore


def certificate(root: Path) -> CertificateMaterial:
    return CertificateMaterial(
        host_root=root,
        container_root="/etc/nginx/ssl",
        fullchain="/etc/nginx/ssl/fullchain.pem",
        private_key="/etc/nginx/ssl/privkey.pem",
        managed_by_certbot=False,
    )


def save_inventory(store: StateStore, root: Path, nginx: Path) -> Inventory:
    compose = root / "docker-compose.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    value = Inventory(
        schema_version=1,
        role="panel",
        install_dir=str(root),
        compose_file=str(compose),
        env_file=None,
        webserver="nginx",
        nginx_files=[str(nginx)],
        components={"nginx": Component("nginx", "remnawave-nginx", "remnawave-nginx")},
        managed_files=[ManagedFile(str(nginx), sha256_file(nginx), "nginx")],
    )
    store.save_inventory(value)
    return value


class EmergencySystemdRunner:
    def __init__(self, *, enablement: str = "disabled", active: bool = False) -> None:
        self.enablement = enablement
        self.active = active
        self.calls: list[tuple[str, ...]] = []

    def run(self, args, **_kwargs):  # type: ignore[no-untyped-def]
        command = tuple(args)
        self.calls.append(command)
        if command[:2] == ("systemctl", "is-enabled"):
            code = 0 if self.enablement in {"enabled", "enabled-runtime"} else 1
            if self.enablement == "not-found":
                code = 4
            return Result(command, code, self.enablement + "\n", "")
        if command[:2] == ("systemctl", "is-active"):
            status = "active" if self.active else (
                "unknown" if self.enablement == "not-found" else "inactive"
            )
            return Result(command, 0 if self.active else (4 if status == "unknown" else 3), status + "\n", "")
        if command[:2] == ("systemctl", "unmask"):
            self.enablement = "disabled"
        elif command[:3] == ("systemctl", "enable", "--now"):
            self.enablement = "enabled"
            self.active = True
        elif command[:3] == ("systemctl", "disable", "--now"):
            self.enablement = "disabled"
            self.active = False
        elif command[:3] == ("systemctl", "enable", "--runtime"):
            self.enablement = "enabled-runtime"
        elif command[:3] == ("systemctl", "mask", "--runtime"):
            self.enablement = "masked-runtime"
        elif command[:2] == ("systemctl", "enable"):
            self.enablement = "enabled"
        elif command[:2] == ("systemctl", "disable"):
            self.enablement = "disabled"
        elif command[:2] == ("systemctl", "mask"):
            self.enablement = "masked"
        elif command[:2] == ("systemctl", "start"):
            self.active = True
        elif command[:2] == ("systemctl", "stop"):
            self.active = False
        return Result(command, 0, "", "")


class PanelSecurityTests(unittest.TestCase):
    def test_emergency_unit_snapshot_rejects_hardlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside.service"
            outside.write_text(
                "[Unit]\nX-Remnawave-Manager=true\n", encoding="utf-8"
            )
            service = root / "managed.service"
            timer = root / "missing.timer"
            os.link(outside, service)

            with self.assertRaisesRegex(ValidationError, "hardlink"):
                _snapshot_units((service, timer))

            self.assertEqual(
                outside.read_text(encoding="utf-8"),
                "[Unit]\nX-Remnawave-Manager=true\n",
            )

    def test_emergency_timer_restores_runtime_enablement_exactly(self) -> None:
        runner = EmergencySystemdRunner(enablement="enabled", active=True)

        _restore_unit_runtime(
            runner,  # type: ignore[arg-type]
            "remnawave-manager-emergency-close.timer",
            ("enabled-runtime", False),
            unit_existed=True,
        )

        self.assertEqual((runner.enablement, runner.active), ("enabled-runtime", False))

    def test_emergency_timer_restores_masked_active_state_exactly(self) -> None:
        runner = EmergencySystemdRunner(enablement="masked", active=False)

        _restore_unit_runtime(
            runner,  # type: ignore[arg-type]
            "remnawave-manager-emergency-close.timer",
            ("masked", True),
            unit_existed=True,
        )

        self.assertEqual((runner.enablement, runner.active), ("masked", True))
        self.assertLess(
            runner.calls.index(
                ("systemctl", "start", "remnawave-manager-emergency-close.timer")
            ),
            runner.calls.index(
                ("systemctl", "mask", "remnawave-manager-emergency-close.timer")
            ),
        )

    def test_emergency_timer_rollback_continues_after_interrupt(self) -> None:
        runner = EmergencySystemdRunner(enablement="enabled", active=True)
        original_run = runner.run
        interrupted = False

        def interrupt_once(args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal interrupted
            if tuple(args)[:2] == ("systemctl", "stop") and not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            return original_run(args, **kwargs)

        runner.run = interrupt_once  # type: ignore[method-assign]
        with self.assertRaisesRegex(TransactionError, "active-state"):
            _restore_unit_runtime(
                runner,  # type: ignore[arg-type]
                "remnawave-manager-emergency-close.timer",
                ("enabled-runtime", False),
                unit_existed=True,
            )

        self.assertEqual(runner.enablement, "enabled-runtime")

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks are unavailable")
    def test_panel_access_refuses_symlinked_nginx_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nginx = root / "nginx.conf"
            nginx.write_text(
                render_panel_nginx(
                    panel_domain="panel.example.com",
                    subscription_domain="sub.example.com",
                    certificate=certificate(root),
                    cookie_name="rwm_" + "n" * 24,
                    cookie_value="v" * 64,
                    gate_path="/_rwm/" + "p" * 48,
                ),
                encoding="utf-8",
            )
            store = StateStore(RuntimePaths(root))
            save_inventory(store, root, nginx)
            outside = root / "outside.conf"
            nginx.replace(outside)
            try:
                nginx.symlink_to(outside)
            except OSError as error:
                self.skipTest(f"symbolic links unavailable: {error}")

            with self.assertRaisesRegex(ValidationError, "обычным файлом"):
                panel_access(store)

    def test_panel_access_refuses_hardlinked_nginx_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nginx = root / "nginx.conf"
            nginx.write_text(
                render_panel_nginx(
                    panel_domain="panel.example.com",
                    subscription_domain="sub.example.com",
                    certificate=certificate(root),
                    cookie_name="rwm_" + "n" * 24,
                    cookie_value="v" * 64,
                    gate_path="/_rwm/" + "p" * 48,
                ),
                encoding="utf-8",
            )
            store = StateStore(RuntimePaths(root))
            save_inventory(store, root, nginx)
            linked = root / "nginx-linked.conf"
            try:
                linked.hardlink_to(nginx)
            except OSError as error:
                self.skipTest(f"hard links unavailable: {error}")

            with self.assertRaisesRegex(ValidationError, "hardlink"):
                panel_access(store)

    def test_panel_access_rotation_preserves_edit_during_nginx_state_probe(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nginx = root / "nginx.conf"
            nginx.write_text(
                render_panel_nginx(
                    panel_domain="panel.example.com",
                    subscription_domain="sub.example.com",
                    certificate=certificate(root),
                    cookie_name="rwm_" + "n" * 24,
                    cookie_value="v" * 64,
                    gate_path="/_rwm/" + "p" * 48,
                ),
                encoding="utf-8",
            )
            store = StateStore(RuntimePaths(root))
            save_inventory(store, root, nginx)
            operator_edit = "# operator edit\n" + nginx.read_text(encoding="utf-8")

            def edit_config(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                nginx.write_text(operator_edit, encoding="utf-8")
                return True

            with (
                mock.patch("remnawave_manager.security.create_backup"),
                mock.patch(
                    "remnawave_manager.security.nginx_is_running",
                    side_effect=edit_config,
                ),
                mock.patch(
                    "remnawave_manager.security.activate_nginx_config"
                ) as activate,
                self.assertRaisesRegex(ValidationError, "изменился после чтения"),
            ):
                rotate_panel_access(mock.Mock(), store)

            activate.assert_not_called()
            self.assertEqual(nginx.read_text(encoding="utf-8"), operator_edit)

    def test_reads_manager_path_without_persisted_secret_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nginx = root / "nginx.conf"
            nginx.write_text(
                render_panel_nginx(
                    panel_domain="panel.example.com",
                    subscription_domain="sub.example.com",
                    certificate=certificate(root),
                    cookie_name="rwm_" + "n" * 24,
                    cookie_value="v" * 64,
                    gate_path="/_rwm/" + "p" * 48,
                ),
                encoding="utf-8",
            )
            store = StateStore(RuntimePaths(root))
            save_inventory(store, root, nginx)

            access = panel_access(store)

            self.assertEqual(access.mode, "manager-path")
            self.assertEqual(access.url, "https://panel.example.com/_rwm/" + "p" * 48)

    def test_ignores_unrelated_files_from_directory_nginx_mount(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            global_nginx = root / "nginx/nginx.conf"
            panel_nginx = root / "nginx/conf.d/panel.conf"
            panel_nginx.parent.mkdir(parents=True)
            global_nginx.write_text(
                "events {}\nhttp { include /etc/nginx/conf.d/*.conf; }\n",
                encoding="utf-8",
            )
            panel_nginx.write_text(
                render_panel_nginx(
                    panel_domain="panel.example.com",
                    subscription_domain="sub.example.com",
                    certificate=certificate(root),
                    cookie_name="rwm_" + "n" * 24,
                    cookie_value="v" * 64,
                    gate_path="/_rwm/" + "p" * 48,
                ),
                encoding="utf-8",
            )
            store = StateStore(RuntimePaths(root))
            inventory = save_inventory(store, root, panel_nginx)
            inventory.nginx_files.insert(0, str(global_nginx))
            store.save_inventory(inventory)

            access = panel_access(store)

            self.assertEqual(
                access.url,
                "https://panel.example.com/_rwm/" + "p" * 48,
            )

    def test_reads_legacy_reverse_proxy_query_cookie(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nginx = root / "nginx.conf"
            nginx.write_text(
                """map $arg_AbCdEfGh $auth_query {
    default 0;
    "QrStUvWx" 1;
}
server {
    server_name panel.example.com;
}
""",
                encoding="utf-8",
            )
            store = StateStore(RuntimePaths(root))
            save_inventory(store, root, nginx)

            access = panel_access(store)

            self.assertEqual(access.mode, "legacy-query")
            self.assertEqual(
                access.url,
                "https://panel.example.com/auth/login?AbCdEfGh=QrStUvWx",
            )

    def test_rotates_legacy_query_cookie_to_manager_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nginx = root / "nginx.conf"
            nginx.write_text(
                """map $http_upgrade $connection_upgrade {
    default upgrade;
    "" close;
}
map $http_cookie $auth_cookie {
    default 0;
    "~*AbCdEfGh=QrStUvWx" 1;
}
map $arg_AbCdEfGh $auth_query {
    default 0;
    "QrStUvWx" 1;
}
map $auth_cookie$auth_query $authorized {
    "~1" 1;
    default 0;
}
map $arg_AbCdEfGh $set_cookie_header {
    "QrStUvWx" "AbCdEfGh=QrStUvWx; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=31536000";
    default "";
}
server {
    server_name panel.example.com;
    location / {
        if ($authorized = 0) { return 418; }
        proxy_pass http://127.0.0.1:3000;
    }
    add_header Set-Cookie $set_cookie_header;
}
""",
                encoding="utf-8",
            )
            store = StateStore(RuntimePaths(root))
            save_inventory(store, root, nginx)

            with (
                mock.patch("remnawave_manager.security.create_backup"),
                mock.patch("remnawave_manager.security.activate_nginx_config"),
                mock.patch(
                    "remnawave_manager.security.nginx_is_running",
                    return_value=True,
                ),
                mock.patch("remnawave_manager.security.secrets.token_hex", return_value="a" * 24),
                mock.patch(
                    "remnawave_manager.security.secrets.token_urlsafe",
                    side_effect=["V" * 64, "P" * 48],
                ),
            ):
                access = rotate_panel_access(mock.Mock(), store)

            self.assertEqual(access.mode, "manager-path")
            self.assertEqual(access.url, "https://panel.example.com/_rwm/" + "P" * 48)
            rendered = nginx.read_text(encoding="utf-8")
            self.assertIn("$cookie_rwm_" + "a" * 24, rendered)
            self.assertNotIn("$auth_query", rendered)
            self.assertNotIn("$authorized", rendered)
            self.assertNotIn("$set_cookie_header", rendered)
            gate = rendered.split("location = /_rwm/", 1)[1].split("location /", 1)[0]
            self.assertIn("add_header Referrer-Policy no-referrer always;", gate)

    def test_rotation_updates_nginx_inventory_and_stored_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nginx = root / "nginx.conf"
            nginx.write_text(
                render_panel_nginx(
                    panel_domain="panel.example.com",
                    subscription_domain="sub.example.com",
                    certificate=certificate(root),
                    cookie_name="rwm_" + "n" * 24,
                    cookie_value="v" * 64,
                    gate_path="/_rwm/" + "p" * 48,
                ),
                encoding="utf-8",
            )
            store = StateStore(RuntimePaths(root))
            save_inventory(store, root, nginx)
            store.initialize()
            atomic_write_text(
                store.paths.secrets,
                json.dumps(
                    {
                        "panel_access_url": "https://panel.example.com/_rwm/"
                        + "p" * 48
                    }
                ),
                mode=0o600,
            )

            with (
                mock.patch("remnawave_manager.security.create_backup"),
                mock.patch("remnawave_manager.security.activate_nginx_config"),
                mock.patch(
                    "remnawave_manager.security.nginx_is_running",
                    return_value=True,
                ),
                mock.patch("remnawave_manager.security.secrets.token_hex", return_value="a" * 24),
                mock.patch(
                    "remnawave_manager.security.secrets.token_urlsafe",
                    side_effect=["V" * 64, "P" * 48],
                ),
            ):
                access = rotate_panel_access(mock.Mock(), store)

            rendered = nginx.read_text(encoding="utf-8")
            self.assertIn("$cookie_rwm_" + "a" * 24, rendered)
            self.assertIn("rwm_" + "a" * 24 + "=" + "V" * 64, rendered)
            self.assertEqual(access.url, "https://panel.example.com/_rwm/" + "P" * 48)
            inventory = store.load_inventory()
            self.assertEqual(inventory.managed_files[0].sha256, sha256_file(nginx))
            saved = json.loads(store.paths.secrets.read_text(encoding="utf-8"))
            self.assertEqual(saved["panel_access_url"], access.url)

    def test_rotation_rolls_back_inventory_and_secrets_when_secret_write_fails(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nginx = root / "nginx.conf"
            nginx.write_text(
                render_panel_nginx(
                    panel_domain="panel.example.com",
                    subscription_domain="sub.example.com",
                    certificate=certificate(root),
                    cookie_name="rwm_" + "n" * 24,
                    cookie_value="v" * 64,
                    gate_path="/_rwm/" + "p" * 48,
                ),
                encoding="utf-8",
            )
            original_nginx = nginx.read_bytes()
            store = StateStore(RuntimePaths(root))
            original_inventory = save_inventory(store, root, nginx).to_dict()
            original_secrets = {
                "panel_access_url": "https://panel.example.com/_rwm/" + "p" * 48,
                "panel_cookie_mode": "manager-path",
            }
            atomic_write_text(
                store.paths.secrets,
                json.dumps(original_secrets),
                mode=0o600,
            )

            with (
                mock.patch("remnawave_manager.security.create_backup"),
                mock.patch("remnawave_manager.security.activate_nginx_config"),
                mock.patch(
                    "remnawave_manager.security.nginx_is_running",
                    return_value=True,
                ),
                mock.patch("remnawave_manager.security.secrets.token_hex", return_value="a" * 24),
                mock.patch(
                    "remnawave_manager.security.secrets.token_urlsafe",
                    side_effect=["V" * 64, "P" * 48],
                ),
                mock.patch(
                    "remnawave_manager.security.atomic_write_json",
                    side_effect=OSError("secrets fsync failed"),
                ),
                self.assertRaises(TransactionError),
            ):
                rotate_panel_access(mock.Mock(), store)

            self.assertEqual(nginx.read_bytes(), original_nginx)
            self.assertEqual(store.load_inventory().to_dict(), original_inventory)
            self.assertEqual(
                json.loads(store.paths.secrets.read_text(encoding="utf-8")),
                original_secrets,
            )

    def test_rotation_does_not_replace_service_with_rejected_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nginx = root / "nginx.conf"
            nginx.write_text(
                render_panel_nginx(
                    panel_domain="panel.example.com",
                    subscription_domain="sub.example.com",
                    certificate=certificate(root),
                    cookie_name="rwm_" + "n" * 24,
                    cookie_value="v" * 64,
                    gate_path="/_rwm/" + "p" * 48,
                ),
                encoding="utf-8",
            )
            original = nginx.read_bytes()
            store = StateStore(RuntimePaths(root))
            save_inventory(store, root, nginx)
            runner = mock.Mock()
            runner.run.side_effect = [
                Result((), 0, "remnawave-nginx\n", ""),
                Result((), 1, "", "new config rejected"),
                Result((), 0, "", ""),
                Result((), 0, "", ""),
                Result((), 0, "", ""),
            ]

            with (
                mock.patch("remnawave_manager.security.create_backup"),
                mock.patch(
                    "remnawave_manager.security.secrets.token_hex",
                    return_value="a" * 24,
                ),
                mock.patch(
                    "remnawave_manager.security.secrets.token_urlsafe",
                    side_effect=["V" * 64, "P" * 48],
                ),
                self.assertRaisesRegex(TransactionError, "отменена"),
            ):
                rotate_panel_access(runner, store)

            self.assertEqual(nginx.read_bytes(), original)
            recreate_calls = [
                call
                for call in runner.run.call_args_list
                if "--force-recreate" in call.args[0]
            ]
            self.assertEqual(len(recreate_calls), 1)
            state_checks = [
                call
                for call in runner.run.call_args_list
                if "ps" in call.args[0]
            ]
            self.assertEqual(len(state_checks), 1)

    def test_rotation_rolls_back_write_that_failed_after_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nginx = root / "nginx.conf"
            nginx.write_text(
                render_panel_nginx(
                    panel_domain="panel.example.com",
                    subscription_domain="sub.example.com",
                    certificate=certificate(root),
                    cookie_name="rwm_" + "n" * 24,
                    cookie_value="v" * 64,
                    gate_path="/_rwm/" + "p" * 48,
                ),
                encoding="utf-8",
            )
            original = nginx.read_bytes()
            store = StateStore(RuntimePaths(root))
            save_inventory(store, root, nginx)
            calls = 0

            def fail_after_replace(path, text, *, mode):  # type: ignore[no-untyped-def]
                nonlocal calls
                calls += 1
                atomic_write_text(path, text, mode=mode)
                if calls == 1:
                    raise OSError("directory fsync failed")

            with (
                mock.patch("remnawave_manager.security.create_backup"),
                mock.patch("remnawave_manager.security.activate_nginx_config"),
                mock.patch(
                    "remnawave_manager.security.nginx_is_running",
                    return_value=True,
                ),
                mock.patch(
                    "remnawave_manager.security.atomic_write_text",
                    side_effect=fail_after_replace,
                ),
                self.assertRaises(TransactionError),
            ):
                rotate_panel_access(mock.Mock(), store)

            self.assertEqual(nginx.read_bytes(), original)

    def test_rotation_preserves_operator_edit_after_manager_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nginx = root / "nginx.conf"
            nginx.write_text(
                render_panel_nginx(
                    panel_domain="panel.example.com",
                    subscription_domain="sub.example.com",
                    certificate=certificate(root),
                    cookie_name="rwm_" + "n" * 24,
                    cookie_value="v" * 64,
                    gate_path="/_rwm/" + "p" * 48,
                ),
                encoding="utf-8",
            )
            store = StateStore(RuntimePaths(root))
            original_inventory = save_inventory(store, root, nginx).to_dict()
            operator_version: bytes | None = None

            def reject_after_operator_edit(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                nonlocal operator_version
                operator_version = nginx.read_bytes() + b"# operator edit\n"
                nginx.write_bytes(operator_version)
                raise RuntimeError("nginx config rejected")

            activate = mock.Mock(side_effect=reject_after_operator_edit)
            with (
                mock.patch("remnawave_manager.security.create_backup"),
                mock.patch(
                    "remnawave_manager.security.nginx_is_running",
                    return_value=True,
                ),
                mock.patch(
                    "remnawave_manager.security.activate_nginx_config",
                    activate,
                ),
                self.assertRaisesRegex(TransactionError, "rollback неполон"),
            ):
                rotate_panel_access(mock.Mock(), store)

            self.assertIsNotNone(operator_version)
            self.assertEqual(nginx.read_bytes(), operator_version)
            self.assertEqual(store.load_inventory().to_dict(), original_inventory)
            self.assertEqual(activate.call_count, 1)

    def test_rotation_preserves_edit_when_write_fails_before_replace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nginx = root / "nginx.conf"
            nginx.write_text(
                render_panel_nginx(
                    panel_domain="panel.example.com",
                    subscription_domain="sub.example.com",
                    certificate=certificate(root),
                    cookie_name="rwm_" + "n" * 24,
                    cookie_value="v" * 64,
                    gate_path="/_rwm/" + "p" * 48,
                ),
                encoding="utf-8",
            )
            store = StateStore(RuntimePaths(root))
            save_inventory(store, root, nginx)
            operator_version = nginx.read_bytes() + b"# operator edit\n"

            def fail_before_replace(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                nginx.write_bytes(operator_version)
                raise OSError("temporary file fsync failed")

            with (
                mock.patch("remnawave_manager.security.create_backup"),
                mock.patch(
                    "remnawave_manager.security.nginx_is_running",
                    return_value=True,
                ),
                mock.patch(
                    "remnawave_manager.security.atomic_write_text",
                    side_effect=fail_before_replace,
                ),
                mock.patch(
                    "remnawave_manager.security.activate_nginx_config"
                ) as activate,
                self.assertRaisesRegex(TransactionError, "rollback неполон"),
            ):
                rotate_panel_access(mock.Mock(), store)

            activate.assert_not_called()
            self.assertEqual(nginx.read_bytes(), operator_version)

    def test_emergency_access_is_loopback_only_and_removed_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nginx = root / "opt/remnawave/nginx.conf"
            nginx.parent.mkdir(parents=True)
            nginx.write_text(
                render_panel_nginx(
                    panel_domain="panel.example.com",
                    subscription_domain="sub.example.com",
                    certificate=certificate(root),
                    cookie_name="rwm_" + "n" * 24,
                    cookie_value="v" * 64,
                    gate_path="/_rwm/" + "p" * 48,
                ),
                encoding="utf-8",
            )
            store = StateStore(RuntimePaths(root))
            save_inventory(store, root, nginx)
            runner = mock.Mock()

            with (
                mock.patch("remnawave_manager.security.create_backup"),
                mock.patch("remnawave_manager.security.activate_nginx_config"),
                mock.patch(
                    "remnawave_manager.security.nginx_is_running",
                    return_value=True,
                ),
                mock.patch(
                    "remnawave_manager.security._unit_runtime_state",
                    return_value=(False, False),
                ),
            ):
                opened = open_emergency_access(runner, store, minutes=15)

                rendered = nginx.read_text(encoding="utf-8")
                self.assertTrue(opened.enabled)
                self.assertIn("listen 127.0.0.1:8443;", rendered)
                self.assertNotIn("listen 8443;", rendered)
                self.assertIn("proxy_set_header Origin https://panel.example.com;", rendered)
                self.assertIn("proxy_connect_timeout 5s;", rendered)
                self.assertIn("proxy_read_timeout 240s;", rendered)
                self.assertIn("proxy_send_timeout 240s;", rendered)
                self.assertTrue((root / "etc/systemd/system/remnawave-manager-emergency-close.timer").is_file())
                service = root / "etc/systemd/system/remnawave-manager-emergency-close.service"
                self.assertIn(
                    "ReadWritePaths=/opt/remnawave",
                    service.read_text(encoding="utf-8"),
                )
                service_text = service.read_text(encoding="utf-8")
                self.assertIn(
                    "ReadWritePaths=/var/backups/remnawave-manager",
                    service_text,
                )
                self.assertIn(
                    "ReadWritePaths=/var/log/remnawave-manager",
                    service_text,
                )
                self.assertIn(
                    "ReadWritePaths=/run/remnawave-manager",
                    service_text,
                )
                self.assertIn("RuntimeDirectory=remnawave-manager", service_text)
                self.assertIn("RuntimeDirectoryMode=0700", service_text)
                self.assertIn("RuntimeDirectoryPreserve=yes", service_text)
                self.assertNotIn("ReadWritePaths=/run/lock", service_text)
                self.assertIn("Wants=docker.service", service_text)
                self.assertIn("After=docker.service", service_text)
                self.assertIn("TimeoutStartSec=5min", service_text)
                self.assertIn("Restart=on-failure", service_text)
                self.assertIn("RestartSec=1min", service_text)
                self.assertTrue(emergency_access_status(store).enabled)

                close_emergency_access(runner, store)

            closed = nginx.read_text(encoding="utf-8")
            self.assertNotIn("REMNAWAVE-MANAGER EMERGENCY ACCESS", closed)
            self.assertFalse(emergency_access_status(store).enabled)
            self.assertFalse(
                (root / "etc/systemd/system/remnawave-manager-emergency-close.timer").exists()
            )
            inventory = store.load_inventory()
            self.assertEqual(inventory.managed_files[0].sha256, sha256_file(nginx))

    def test_emergency_close_restores_nginx_when_validation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nginx = root / "etc/nginx/sites-enabled/panel.conf"
            nginx.parent.mkdir(parents=True)
            nginx.write_text(
                render_panel_nginx(
                    panel_domain="panel.example.com",
                    subscription_domain="sub.example.com",
                    certificate=certificate(root),
                    cookie_name="rwm_" + "n" * 24,
                    cookie_value="v" * 64,
                    gate_path="/_rwm/" + "p" * 48,
                ),
                encoding="utf-8",
            )
            store = StateStore(RuntimePaths(root))
            save_inventory(store, root, nginx)
            runner = mock.Mock()

            with (
                mock.patch("remnawave_manager.security.create_backup"),
                mock.patch("remnawave_manager.security.activate_nginx_config"),
                mock.patch(
                    "remnawave_manager.security.nginx_is_running",
                    return_value=True,
                ),
                mock.patch(
                    "remnawave_manager.security._unit_runtime_state",
                    return_value=(False, False),
                ),
            ):
                open_emergency_access(runner, store, minutes=15)

            opened = nginx.read_text(encoding="utf-8")
            with (
                mock.patch(
                    "remnawave_manager.security.activate_nginx_config",
                    side_effect=RuntimeError("nginx config rejected"),
                ),
                mock.patch(
                    "remnawave_manager.security.nginx_is_running",
                    return_value=True,
                ),
                self.assertRaises(TransactionError),
            ):
                close_emergency_access(runner, store)

            self.assertEqual(nginx.read_text(encoding="utf-8"), opened)
            self.assertTrue(emergency_access_status(store).enabled)

    def test_emergency_close_preserves_operator_edit_after_manager_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nginx = root / "etc/nginx/sites-enabled/panel.conf"
            nginx.parent.mkdir(parents=True)
            nginx.write_text(
                render_panel_nginx(
                    panel_domain="panel.example.com",
                    subscription_domain="sub.example.com",
                    certificate=certificate(root),
                    cookie_name="rwm_" + "n" * 24,
                    cookie_value="v" * 64,
                    gate_path="/_rwm/" + "p" * 48,
                ),
                encoding="utf-8",
            )
            store = StateStore(RuntimePaths(root))
            save_inventory(store, root, nginx)
            runner = mock.Mock()

            with (
                mock.patch("remnawave_manager.security.create_backup"),
                mock.patch("remnawave_manager.security.activate_nginx_config"),
                mock.patch(
                    "remnawave_manager.security.nginx_is_running",
                    return_value=True,
                ),
                mock.patch(
                    "remnawave_manager.security._unit_runtime_state",
                    return_value=(False, False),
                ),
            ):
                open_emergency_access(runner, store, minutes=15)

            operator_version: bytes | None = None

            def reject_after_operator_edit(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                nonlocal operator_version
                operator_version = nginx.read_bytes() + b"# operator edit\n"
                nginx.write_bytes(operator_version)
                raise RuntimeError("nginx config rejected")

            activate = mock.Mock(side_effect=reject_after_operator_edit)
            with (
                mock.patch(
                    "remnawave_manager.security.activate_nginx_config",
                    activate,
                ),
                mock.patch(
                    "remnawave_manager.security.nginx_is_running",
                    return_value=True,
                ),
                self.assertRaisesRegex(
                    TransactionError,
                    "rollback nginx/inventory неполон",
                ),
            ):
                close_emergency_access(runner, store)

            self.assertIsNotNone(operator_version)
            self.assertEqual(nginx.read_bytes(), operator_version)
            self.assertEqual(activate.call_count, 1)

    def test_emergency_open_preserves_unit_changed_before_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nginx = root / "opt/remnawave/nginx.conf"
            nginx.parent.mkdir(parents=True)
            nginx.write_text(
                render_panel_nginx(
                    panel_domain="panel.example.com",
                    subscription_domain="sub.example.com",
                    certificate=certificate(root),
                    cookie_name="rwm_" + "n" * 24,
                    cookie_value="v" * 64,
                    gate_path="/_rwm/" + "p" * 48,
                ),
                encoding="utf-8",
            )
            original_nginx = nginx.read_bytes()
            store = StateStore(RuntimePaths(root))
            original_inventory = save_inventory(store, root, nginx).to_dict()
            service = (
                root
                / "etc/systemd/system/remnawave-manager-emergency-close.service"
            )
            operator_unit = b"[Unit]\nX-Remnawave-Manager=true\n# operator edit\n"
            runner = EmergencySystemdRunner(enablement="disabled", active=False)

            def fail_after_unit_edit(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                service.write_bytes(operator_unit)
                raise OSError("secrets fsync failed")

            with (
                mock.patch("remnawave_manager.security.create_backup"),
                mock.patch("remnawave_manager.security.activate_nginx_config"),
                mock.patch(
                    "remnawave_manager.security.nginx_is_running",
                    return_value=True,
                ),
                mock.patch(
                    "remnawave_manager.security.atomic_write_json",
                    side_effect=fail_after_unit_edit,
                ),
                self.assertRaisesRegex(TransactionError, "rollback неполон"),
            ):
                open_emergency_access(runner, store, minutes=15)  # type: ignore[arg-type]

            self.assertEqual(service.read_bytes(), operator_unit)
            self.assertEqual(nginx.read_bytes(), original_nginx)
            self.assertEqual(store.load_inventory().to_dict(), original_inventory)
            self.assertEqual((runner.enablement, runner.active), ("disabled", False))

    def test_emergency_open_failure_restores_exact_timer_state_and_units(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nginx = root / "opt/remnawave/nginx.conf"
            nginx.parent.mkdir(parents=True)
            nginx.write_text(
                render_panel_nginx(
                    panel_domain="panel.example.com",
                    subscription_domain="sub.example.com",
                    certificate=certificate(root),
                    cookie_name="rwm_" + "n" * 24,
                    cookie_value="v" * 64,
                    gate_path="/_rwm/" + "p" * 48,
                ),
                encoding="utf-8",
            )
            original_nginx = nginx.read_text(encoding="utf-8")
            store = StateStore(RuntimePaths(root))
            save_inventory(store, root, nginx)
            unit_root = root / "etc/systemd/system"
            unit_root.mkdir(parents=True)
            service = unit_root / "remnawave-manager-emergency-close.service"
            timer = unit_root / "remnawave-manager-emergency-close.timer"
            old_service = b"[Unit]\nX-Remnawave-Manager=true\nold-service\n"
            old_timer = b"[Unit]\nX-Remnawave-Manager=true\nold-timer\n"
            service.write_bytes(old_service)
            timer.write_bytes(old_timer)
            runner = EmergencySystemdRunner(enablement="disabled", active=True)

            with (
                mock.patch("remnawave_manager.security.create_backup"),
                mock.patch("remnawave_manager.security.activate_nginx_config"),
                mock.patch(
                    "remnawave_manager.security.nginx_is_running",
                    return_value=True,
                ),
                mock.patch(
                    "remnawave_manager.security.atomic_write_json",
                    side_effect=OSError("state write failed"),
                ),
                self.assertRaisesRegex(TransactionError, "не открыт"),
            ):
                open_emergency_access(runner, store, minutes=15)  # type: ignore[arg-type]

            self.assertEqual(nginx.read_text(encoding="utf-8"), original_nginx)
            self.assertEqual(service.read_bytes(), old_service)
            self.assertEqual(timer.read_bytes(), old_timer)
            self.assertEqual((runner.enablement, runner.active), ("disabled", True))


if __name__ == "__main__":
    unittest.main()
