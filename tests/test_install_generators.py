import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remnawave_manager.certificates import (
    CertificateMaterial,
    CertificateSpec,
    build_certbot_command,
    configure_adopted_certbot,
    discover_certbot_renewal,
    install_renewal_hooks,
    obtain_certificate,
)
from remnawave_manager.errors import TransactionError, ValidationError
from remnawave_manager.firewall import FirewallPlan, build_firewall_commands
from remnawave_manager.install import (
    NGINX_IMAGE,
    POSTGRES_IMAGE,
    VALKEY_IMAGE,
    NodeInstallOptions,
    PanelEnvironment,
    PanelInstallOptions,
    _add_managed_files,
    _admin_credentials,
    _archive_incomplete_install,
    _certificate_secret_paths,
    _ensure_container_names_available,
    _finish_install_attempt,
    _install_compose_command,
    _preflight,
    _require_local_docker,
    _rollback_failed_install_state,
    _rollback_failed_install_state_safely,
    _rollback_install_resources,
    _start_install_attempt,
    complete_panel_credentials_handoff,
    install_node,
    install_panel,
    render_node_compose,
    render_node_env,
    render_node_nginx,
    render_panel_compose,
    render_panel_env,
    render_panel_nginx,
    render_subscription_env,
)
from remnawave_manager.models import Component, Inventory
from remnawave_manager.paths import RuntimePaths
from remnawave_manager.runner import Result, Runner
from remnawave_manager.state import StateStore


def certificate() -> CertificateMaterial:
    return CertificateMaterial(
        host_root=Path("/etc/letsencrypt"),
        container_root="/etc/letsencrypt",
        fullchain="/etc/letsencrypt/live/panel.example.com/fullchain.pem",
        private_key="/etc/letsencrypt/live/panel.example.com/privkey.pem",
        managed_by_certbot=True,
    )


class InstallGeneratorTests(unittest.TestCase):
    def test_node_install_requires_panel_3_3_confirmation_before_preflight(self) -> None:
        with (
            mock.patch("remnawave_manager.install._preflight") as preflight,
            self.assertRaisesRegex(ValidationError, "--panel-3-3-ready"),
        ):
            install_node(
                mock.Mock(spec=Runner),
                mock.Mock(spec=StateStore),
                NodeInstallOptions(
                    domain="node.example.com",
                    panel_ip="203.0.113.10",
                    secret_key="node.secret_123",
                    certificate=CertificateSpec(
                        method="http-01", email="admin@example.com"
                    ),
                    site_source=Path("/not-used"),
                ),
            )

        preflight.assert_not_called()

    def test_panel_install_rejects_ghcr_before_pulling_any_image(self) -> None:
        store = mock.Mock()
        store.load_settings.return_value = {"registry": "ghcr"}
        runner = mock.Mock(spec=Runner)

        with (
            mock.patch("remnawave_manager.install._preflight"),
            mock.patch("remnawave_manager.install._ensure_container_names_available"),
            mock.patch("remnawave_manager.install._pull_component_image") as pull,
            self.assertRaisesRegex(ValidationError, "registry select docker-hub"),
        ):
            install_panel(
                runner,
                store,
                PanelInstallOptions(
                    panel_domain="panel.example.com",
                    subscription_domain="subscription.example.com",
                    certificate=CertificateSpec(
                        method="existing",
                        fullchain=Path("/etc/ssl/fullchain.pem"),
                        private_key=Path("/etc/ssl/privkey.pem"),
                    ),
                    configure_ufw=False,
                ),
            )

        pull.assert_not_called()

    def test_clean_nginx_templates_use_full_remnawave_compression_contract(self) -> None:
        panel = render_panel_nginx(
            panel_domain="panel.example.com",
            subscription_domain="subscription.example.com",
            certificate=certificate(),
            cookie_name="rwm_0123456789abcdef",
            cookie_value="a" * 48,
            gate_path="/_rwm/" + "b" * 48,
        )
        node = render_node_nginx(
            domain="node.example.com",
            certificate=certificate(),
        )

        for rendered in (panel, node):
            self.assertIn("gzip_comp_level 6;", rendered)
            self.assertIn("application/manifest+json", rendered)
            self.assertIn("application/wasm", rendered)
            self.assertIn("font/ttf", rendered)
        self.assertEqual(panel.count("proxy_read_timeout 240s;"), 2)
        self.assertEqual(panel.count("proxy_send_timeout 240s;"), 2)

    def test_install_resource_rollback_restores_ufw_and_certificate_after_stop(self) -> None:
        firewall = mock.Mock()
        certificate_material = mock.Mock(spec=CertificateMaterial)
        runner = mock.Mock(spec=Runner)
        runner.run.side_effect = [
            Result(("docker", "ps"), 0, "", ""),
            Result(("docker", "ps"), 0, "", ""),
        ]

        detail = _rollback_install_resources(
            runner,
            firewall,
            certificate_material,
            ("remnanode", "remnawave-nginx"),
        )

        self.assertIsNone(detail)
        firewall.rollback.assert_called_once_with()
        certificate_material.rollback.assert_called_once_with(runner)
        for call in runner.run.call_args_list:
            self.assertIn("--all", call.args[0])

    def test_install_resource_rollback_retains_tls_and_ufw_while_container_runs(self) -> None:
        firewall = mock.Mock()
        firewall.artifact_path = Path(
            "/var/lib/remnawave-manager/firewall-transactions/ufw-test"
        )
        certificate_material = mock.Mock(spec=CertificateMaterial)
        runner = mock.Mock(spec=Runner)
        runner.run.return_value = Result(("docker", "ps"), 0, "container-id\n", "")

        detail = _rollback_install_resources(
            runner,
            firewall,
            certificate_material,
            ("remnanode",),
        )

        self.assertIn("TLS lineage и credentials сохранены", detail or "")
        self.assertIn("UFW не ослаблялся", detail or "")
        firewall.rollback.assert_not_called()
        certificate_material.rollback.assert_not_called()

    def test_install_resource_rollback_retains_ufw_when_docker_check_fails(self) -> None:
        firewall = mock.Mock()
        firewall.artifact_path = Path(
            "/var/lib/remnawave-manager/firewall-transactions/ufw-test"
        )
        certificate_material = mock.Mock(spec=CertificateMaterial)
        runner = mock.Mock(spec=Runner)
        runner.run.return_value = Result(("docker", "ps"), 1, "", "daemon unavailable")

        detail = _rollback_install_resources(
            runner,
            firewall,
            certificate_material,
            ("remnanode",),
        )

        self.assertIn("не удалось проверить", detail or "")
        self.assertIn("UFW не ослаблялся", detail or "")
        firewall.rollback.assert_not_called()
        certificate_material.rollback.assert_not_called()

    def test_install_resource_rollback_survives_docker_probe_exception(self) -> None:
        firewall = mock.Mock()
        firewall.artifact_path = Path(
            "/var/lib/remnawave-manager/firewall-transactions/ufw-test"
        )
        certificate_material = mock.Mock(spec=CertificateMaterial)
        runner = mock.Mock(spec=Runner)
        runner.run.side_effect = TimeoutError("docker probe timed out")

        detail = _rollback_install_resources(
            runner,
            firewall,
            certificate_material,
            ("remnanode",),
        )

        self.assertIn("TimeoutError", detail or "")
        firewall.rollback.assert_not_called()
        certificate_material.rollback.assert_not_called()

    def test_install_resource_rollback_skips_docker_probe_without_external_changes(self) -> None:
        runner = mock.Mock(spec=Runner)

        detail = _rollback_install_resources(
            runner,
            None,
            None,
            ("remnanode",),
        )

        self.assertIsNone(detail)
        runner.run.assert_not_called()

    def test_certificate_credentials_are_registered_as_managed_secrets(self) -> None:
        directory = Path("/opt/remnanode")
        material = CertificateMaterial(
            host_root=directory / "certificates",
            container_root="/etc/nginx/ssl",
            fullchain="/etc/nginx/ssl/fullchain.pem",
            private_key="/etc/nginx/ssl/privkey.pem",
            managed_by_certbot=False,
            credentials_file=Path(
                "/etc/remnawave-manager/certbot/gcore-node.example.com.ini"
            ),
        )

        self.assertEqual(
            _certificate_secret_paths(
                directory,
                CertificateSpec(method="gcore", email="admin@example.com"),
                material,
            ),
            [Path("/etc/remnawave-manager/certbot/gcore-node.example.com.ini")],
        )
        cloudflare_material = CertificateMaterial(
            host_root=directory / "certificates",
            container_root="/etc/nginx/ssl",
            fullchain="/etc/nginx/ssl/fullchain.pem",
            private_key="/etc/nginx/ssl/privkey.pem",
            managed_by_certbot=True,
            credentials_file=Path(
                "/etc/remnawave-manager/certbot/cloudflare-node.example.com.ini"
            ),
        )
        self.assertEqual(
            _certificate_secret_paths(
                directory,
                CertificateSpec(method="cloudflare", email="admin@example.com"),
                cloudflare_material,
            ),
            [Path("/etc/remnawave-manager/certbot/cloudflare-node.example.com.ini")],
        )
        self.assertEqual(
            _certificate_secret_paths(
                directory,
                CertificateSpec(
                    method="existing",
                    fullchain=Path("source-fullchain.pem"),
                    private_key=Path("source-privkey.pem"),
                ),
                material,
            ),
            [
                directory / "certificates/fullchain.pem",
                directory / "certificates/privkey.pem",
            ],
        )

    def test_incomplete_install_is_archived_without_deleting_volumes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "remnanode"
            _start_install_attempt(directory, "node")
            (directory / ".env").write_text(
                "SECRET_KEY=value\n", encoding="utf-8"
            )
            (directory / "docker-compose.yml").write_text(
                "services: {}\n", encoding="utf-8"
            )
            runner = mock.Mock()

            archived = _archive_incomplete_install(runner, directory, "node")

            self.assertIsNotNone(archived)
            self.assertFalse(directory.exists())
            self.assertTrue(archived.is_dir())  # type: ignore[union-attr]
            command = runner.run.call_args.args[0]
            self.assertIn("down", command)
            self.assertIn("--remove-orphans", command)
            self.assertEqual(command[command.index("--timeout") + 1], "60")
            self.assertNotIn("--volumes", command)
            self.assertEqual(
                command[command.index("--project-name") + 1], "remnanode"
            )

    def test_foreign_nonempty_directory_is_never_archived(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "remnawave"
            directory.mkdir()
            (directory / "foreign.conf").write_text("owner=admin\n", encoding="utf-8")
            runner = mock.Mock()

            archived = _archive_incomplete_install(runner, directory, "panel")

            self.assertIsNone(archived)
            self.assertTrue(directory.is_dir())
            runner.run.assert_not_called()

    def test_completed_install_removes_only_valid_current_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "remnanode"
            _start_install_attempt(directory, "node")

            _finish_install_attempt(directory, "node")

            self.assertFalse((directory / ".rwm-install-state.json").exists())

    def test_install_marker_is_created_without_overwriting_an_existing_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "remnanode"
            _start_install_attempt(directory, "node")
            marker = directory / ".rwm-install-state.json"
            original = marker.read_bytes()

            with self.assertRaisesRegex(ValidationError, "Маркер установки уже существует"):
                _start_install_attempt(directory, "panel")

            self.assertEqual(marker.read_bytes(), original)

    def test_install_marker_hardlink_is_rejected_without_removal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "remnanode"
            _start_install_attempt(directory, "node")
            marker = directory / ".rwm-install-state.json"
            duplicate = directory / "marker-hardlink.json"
            try:
                os.link(marker, duplicate)
            except OSError as error:
                self.skipTest(f"hard links unavailable: {error}")

            with self.assertRaisesRegex(ValidationError, "hardlink"):
                _finish_install_attempt(directory, "node")

            self.assertTrue(marker.is_file())
            self.assertTrue(duplicate.is_file())

    def test_container_name_preflight_fails_closed_and_matches_exact_names(self) -> None:
        runner = mock.Mock(spec=Runner)
        runner.run.return_value = Result(
            ("docker", "ps"),
            0,
            "remnawave\nremnawave-sidecar\n",
            "",
        )

        with self.assertRaisesRegex(ValidationError, "remnawave"):
            _ensure_container_names_available(
                runner,
                ("remnawave", "remnawave-nginx"),
            )

        command = runner.run.call_args.args[0]
        self.assertEqual(command[:3], ["docker", "ps", "--all"])
        self.assertIn("--format", command)

        runner.run.side_effect = RuntimeError("docker daemon unavailable")
        with self.assertRaisesRegex(RuntimeError, "daemon unavailable"):
            _ensure_container_names_available(runner, ("remnanode",))

    def test_install_compose_commands_pin_the_project_name(self) -> None:
        compose = Path("/opt/remnawave/docker-compose.yml")
        env = Path("/opt/remnawave/.env")

        command = _install_compose_command(
            compose,
            "remnawave",
            "config",
            "-q",
            env_file=env,
        )

        self.assertEqual(
            command[:4],
            ["docker", "compose", "--project-name", "remnawave"],
        )
        self.assertEqual(command[4:6], ["--env-file", str(env)])
        with self.assertRaisesRegex(ValidationError, "Compose-проекта"):
            _install_compose_command(compose, "from-environment", "down")

    def test_failed_install_state_rollback_removes_only_matching_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(RuntimePaths(root))
            install_dir = root / "opt" / "remnawave"
            inventory = Inventory(
                schema_version=1,
                role="panel",
                install_dir=str(install_dir),
                compose_file=str(install_dir / "docker-compose.yml"),
                env_file=str(install_dir / ".env"),
                webserver="nginx",
            )
            secrets_payload = {"panel_access_url": "https://panel.example/_rwm/test"}
            store.save_inventory(inventory)
            store.paths.secrets.write_text(
                json.dumps(secrets_payload), encoding="utf-8"
            )
            store.paths.secrets.chmod(0o600)

            detail = _rollback_failed_install_state(
                store,
                install_dir,
                "panel",
                secrets_payload=secrets_payload,
            )

            self.assertIsNone(detail)
            self.assertFalse(store.paths.inventory.exists())
            self.assertFalse(store.paths.secrets.exists())

    def test_failed_install_state_rollback_preserves_foreign_or_corrupt_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(RuntimePaths(root))
            store.initialize()
            install_dir = root / "opt" / "remnawave"
            foreign = Inventory(
                schema_version=1,
                role="node",
                install_dir=str(root / "opt" / "remnanode"),
                compose_file=str(root / "opt" / "remnanode" / "docker-compose.yml"),
                env_file=str(root / "opt" / "remnanode" / ".env"),
                webserver="nginx",
            )
            secrets_payload = {"panel_access_url": "https://panel.example/_rwm/test"}
            store.save_inventory(foreign)
            store.paths.secrets.write_text(
                json.dumps(secrets_payload), encoding="utf-8"
            )
            store.paths.secrets.chmod(0o600)

            detail = _rollback_failed_install_state(
                store,
                install_dir,
                "panel",
                secrets_payload=secrets_payload,
            )

            self.assertIn("не соответствует текущей попытке", detail or "")
            self.assertTrue(store.paths.inventory.is_file())
            self.assertTrue(store.paths.secrets.is_file())

            store.paths.inventory.write_text("{broken", encoding="utf-8")
            detail = _rollback_failed_install_state(
                store,
                install_dir,
                "panel",
                secrets_payload=secrets_payload,
            )
            self.assertIn("не удалось проверить созданный inventory", detail or "")
            self.assertEqual(store.paths.inventory.read_text(encoding="utf-8"), "{broken")
            self.assertTrue(store.paths.secrets.is_file())

    def test_failed_install_state_wrapper_retains_resources_on_unexpected_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(RuntimePaths(root))
            install_dir = root / "opt" / "remnawave"

            with mock.patch(
                "remnawave_manager.install._rollback_failed_install_state",
                side_effect=RuntimeError("state storage unavailable"),
            ):
                detail = _rollback_failed_install_state_safely(
                    store,
                    install_dir,
                    "panel",
                )

            self.assertIn("RuntimeError", detail or "")
            self.assertIn("state storage unavailable", detail or "")

    def test_failed_install_state_keeps_secrets_when_inventory_cannot_be_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(RuntimePaths(root))
            install_dir = root / "opt" / "remnawave"
            inventory = Inventory(
                schema_version=1,
                role="panel",
                install_dir=str(install_dir),
                compose_file=str(install_dir / "docker-compose.yml"),
                env_file=str(install_dir / ".env"),
                webserver="nginx",
            )
            secrets_payload = {
                "panel_access_url": "https://panel.example/_rwm/test"
            }
            store.save_inventory(inventory)
            store.paths.secrets.write_text(
                json.dumps(secrets_payload),
                encoding="utf-8",
            )
            store.paths.secrets.chmod(0o600)
            original_unlink = Path.unlink

            def selective_unlink(path, missing_ok=False):  # type: ignore[no-untyped-def]
                if path == store.paths.inventory:
                    raise PermissionError("immutable inventory")
                return original_unlink(path, missing_ok=missing_ok)

            with mock.patch.object(Path, "unlink", new=selective_unlink):
                detail = _rollback_failed_install_state(
                    store,
                    install_dir,
                    "panel",
                    secrets_payload=secrets_payload,
                )

            self.assertIn("immutable inventory", detail or "")
            self.assertTrue(store.paths.inventory.is_file())
            self.assertTrue(store.paths.secrets.is_file())

    def test_preflight_rejects_stale_secrets_without_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(RuntimePaths(root))
            store.initialize()
            store.paths.secrets.write_text("{}\n", encoding="utf-8")
            directory = root / "opt" / "remnawave"
            runner = mock.Mock(spec=Runner)

            with (
                mock.patch("remnawave_manager.install.require_root"),
                mock.patch("remnawave_manager.install.require_ubuntu_2404"),
                self.assertRaisesRegex(ValidationError, "secrets.json без активного inventory"),
            ):
                _preflight(
                    runner,
                    store,
                    directory,
                    expected=directory,
                    role="panel",
                )

            runner.run.assert_not_called()

    def test_preflight_rejects_active_transaction_before_touching_install_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(RuntimePaths(root / "runtime"))
            store.initialize()
            journal = store.paths.state / "active-transaction.json"
            journal.write_text(
                '{"operation":"panel-update","phase":"migrating"}\n',
                encoding="utf-8",
            )
            directory = root / "opt" / "remnawave"
            _start_install_attempt(directory, "panel")
            original_marker = (directory / ".rwm-install-state.json").read_bytes()
            runner = mock.Mock(spec=Runner)

            with (
                mock.patch("remnawave_manager.install.require_root"),
                mock.patch("remnawave_manager.install.require_ubuntu_2404"),
                self.assertRaisesRegex(ValidationError, "незавершённая транзакция"),
            ):
                _preflight(
                    runner,
                    store,
                    directory,
                    expected=directory,
                    role="panel",
                )

            self.assertEqual(
                (directory / ".rwm-install-state.json").read_bytes(),
                original_marker,
            )
            self.assertTrue(journal.is_file())
            runner.run.assert_not_called()

    def test_preflight_does_not_archive_an_incomplete_install_before_dependency_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            directory = root / "opt" / "remnanode"
            _start_install_attempt(directory, "node")
            (directory / ".env").write_text("SECRET_KEY=value\n", encoding="utf-8")
            (directory / "docker-compose.yml").write_text(
                "services: {}\n",
                encoding="utf-8",
            )
            store = StateStore(RuntimePaths(root / "runtime"))
            runner = mock.Mock(spec=Runner)

            with (
                mock.patch("remnawave_manager.install.require_root"),
                mock.patch("remnawave_manager.install.require_ubuntu_2404"),
                mock.patch(
                    "remnawave_manager.install.command_exists",
                    return_value=False,
                ),
                self.assertRaisesRegex(ValidationError, "Команда docker не найдена"),
            ):
                _preflight(
                    runner,
                    store,
                    directory,
                    expected=directory,
                    role="node",
                )

            self.assertTrue(directory.is_dir())
            self.assertTrue((directory / ".rwm-install-state.json").is_file())
            runner.run.assert_not_called()

    def test_clean_install_rejects_remote_docker_routing(self) -> None:
        runner = mock.Mock(spec=Runner)

        with (
            mock.patch.dict(
                os.environ,
                {"DOCKER_HOST": "tcp://remote.example:2376"},
                clear=False,
            ),
            self.assertRaisesRegex(ValidationError, "удалённый DOCKER_HOST"),
        ):
            _require_local_docker(runner)

        runner.run.assert_not_called()

        runner.run.return_value = Result(
            ("docker", "context", "inspect"),
            0,
            "ssh://remote.example\n",
            "",
        )
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(ValidationError, "не использует локальный"),
        ):
            _require_local_docker(runner)

    def test_clean_install_accepts_local_rootful_docker_context(self) -> None:
        runner = mock.Mock(spec=Runner)
        runner.run.return_value = Result(
            ("docker", "context", "inspect"),
            0,
            "unix:///var/run/docker.sock\n",
            "",
        )

        with mock.patch.dict(os.environ, {}, clear=True):
            _require_local_docker(runner)

        runner.run.assert_called_once_with(
            [
                "docker",
                "context",
                "inspect",
                "--format",
                "{{.Endpoints.docker.Host}}",
            ],
            timeout=30,
        )

    def test_explicit_empty_admin_credentials_are_not_replaced_by_random_values(self) -> None:
        with self.assertRaisesRegex(ValidationError, "Имя администратора"):
            _admin_credentials("", "valid-password-123")
        with self.assertRaisesRegex(ValidationError, "Пароль"):
            _admin_credentials("admin_user", "")

    def test_base_images_use_verified_multiarch_manifest_digests(self) -> None:
        self.assertEqual(
            POSTGRES_IMAGE,
            "postgres:18.4@sha256:"
            "a02db8cac496f15b094798a38254f14d6e00741f709360e5e00bb6668ea31636",
        )
        self.assertEqual(
            VALKEY_IMAGE,
            "valkey/valkey:9.0.3-alpine@sha256:"
            "e1095c6c76ee982cb2d1e07edbb7fb2a53606630a1d810d5a47c9f646b708bf5",
        )
        self.assertEqual(
            NGINX_IMAGE,
            "nginx:1.28.0-alpine@sha256:"
            "30f1c0d78e0ad60901648be663a710bdadf19e4c10ac6782c235200619158284",
        )

    def test_panel_env_uses_v3_secret_and_exact_database_contract(self) -> None:
        rendered = render_panel_env(
            PanelEnvironment(
                panel_domain="panel.example.com",
                subscription_domain="sub.example.com",
                app_secret="a" * 128,
                postgres_password="b" * 64,
                metrics_password="c" * 128,
                webhook_secret="d" * 64,
            )
        )

        self.assertIn("APP_SECRET=" + "a" * 128, rendered)
        self.assertNotIn("JWT_AUTH_SECRET", rendered)
        self.assertNotIn("JWT_API_TOKENS_SECRET", rendered)
        self.assertIn("postgresql://remnawave:" + "b" * 64, rendered)
        self.assertIn("@remnawave-db:5432/remnawave", rendered)
        self.assertIn("SUB_PUBLIC_DOMAIN=sub.example.com", rendered)
        self.assertNotIn("REMNAWAVE_API_TOKEN", rendered)

    def test_subscription_token_is_isolated_in_its_own_env(self) -> None:
        rendered = render_subscription_env("token.part_1-part_2")

        self.assertIn("REMNAWAVE_API_TOKEN=token.part_1-part_2", rendered)
        self.assertNotIn("APP_SECRET", rendered)
        self.assertNotIn("DATABASE_URL", rendered)

    def test_panel_compose_is_version_pinned_and_has_no_node(self) -> None:
        rendered = render_panel_compose(
            panel_image="remnawave/backend:3.2.0@sha256:" + "a" * 64,
            subscription_image="remnawave/subscription-page:8.0.0@sha256:" + "b" * 64,
            certificate=certificate(),
        )

        self.assertNotIn("latest", rendered)
        self.assertIn("postgres:18.4", rendered)
        self.assertIn("valkey/valkey:9.0.3-alpine", rendered)
        self.assertIn("nginx:1.28.0-alpine", rendered)
        self.assertGreaterEqual(rendered.count("@sha256:"), 5)
        self.assertIn('"127.0.0.1:3000:3000"', rendered)
        self.assertIn('"127.0.0.1:3010:3010"', rendered)
        self.assertIn("source: ./data/postgres", rendered)
        self.assertEqual(rendered.count("- valkey-socket:/var/run/valkey"), 2)
        self.assertIn("name: valkey-socket", rendered)
        self.assertNotIn("source: ./run/valkey", rendered)
        self.assertIn("network_mode: host", rendered)
        self.assertNotIn("remnanode", rendered)
        self.assertNotIn("restart: always", rendered)
        self.assertEqual(rendered.count("restart: unless-stopped"), 5)
        self.assertNotIn(":/etc/letsencrypt:ro", rendered)
        self.assertIn(
            ":/etc/letsencrypt/live/panel.example.com:ro", rendered
        )
        self.assertIn(
            ":/etc/letsencrypt/archive/panel.example.com:ro", rendered
        )

    def test_panel_cookie_gate_has_no_query_secret_and_disables_logging(self) -> None:
        rendered = render_panel_nginx(
            panel_domain="panel.example.com",
            subscription_domain="sub.example.com",
            certificate=certificate(),
            cookie_name="rwm_" + "a" * 16,
            cookie_value="b" * 48,
            gate_path="/_rwm/" + "c" * 48,
        )

        self.assertIn("gzip on;", rendered)
        self.assertIn("map_hash_bucket_size 128;", rendered)
        self.assertNotIn("map $arg_", rendered)
        self.assertIn("access_log off;", rendered)
        self.assertIn("SameSite=Strict", rendered)
        self.assertIn("map $uri $rwm_auth_key", rendered)
        self.assertIn("limit_req_zone $rwm_auth_key", rendered)
        self.assertIn("~^/api/auth/(?:login|register)$", rendered)
        self.assertIn("return 302 /auth/login;", rendered)
        self.assertNotIn("return 302 /auth/login?", rendered)
        self.assertIn("$cookie_rwm_" + "a" * 16, rendered)
        self.assertNotIn("$proxy_add_x_forwarded_for", rendered)
        self.assertEqual(rendered.count("proxy_set_header X-Forwarded-For $remote_addr;"), 2)
        self.assertIn("proxy_connect_timeout 5s;", rendered)
        self.assertIn("proxy_read_timeout 240s;", rendered)
        subscription = rendered.split("server_name sub.example.com;", 1)[1].split(
            "server {", 1
        )[0]
        self.assertIn("access_log off;", subscription)
        gate = rendered.split("location = /_rwm/", 1)[1].split("location /", 1)[0]
        self.assertIn("add_header Referrer-Policy no-referrer always;", gate)
        self.assertIn("add_header Strict-Transport-Security", gate)

    def test_panel_http_redirect_rejects_unknown_host(self) -> None:
        rendered = render_panel_nginx(
            panel_domain="panel.example.com",
            subscription_domain="sub.example.com",
            certificate=certificate(),
            cookie_name="rwm_" + "a" * 16,
            cookie_value="b" * 48,
            gate_path="/_rwm/" + "c" * 48,
        )

        default_http = rendered.split(
            "server {\n    listen 80 default_server;", 1
        )[1].split("}\n", 1)[0]
        redirect_http = rendered.split(
            "server {\n    listen 80;", 1
        )[1].split("}\n", 1)[0]
        self.assertIn("server_name _;", default_http)
        self.assertIn("return 444;", default_http)
        self.assertNotIn("$host", default_http)
        self.assertIn("server_name panel.example.com sub.example.com;", redirect_http)
        self.assertIn("return 308 https://$host$request_uri;", redirect_http)

    def test_node_generators_keep_secret_out_of_compose_and_use_unix_socket(self) -> None:
        secret = "node.secret_123"
        env = render_node_env(secret)
        compose = render_node_compose(
            node_image="remnawave/node:3.0.0@sha256:" + "c" * 64,
            certificate=certificate(),
        )
        nginx = render_node_nginx(domain="node.example.com", certificate=certificate())

        self.assertIn(secret, env)
        self.assertNotIn(secret, compose)
        self.assertNotIn("latest", compose)
        self.assertIn("remnawave/node:3.0.0", compose)
        self.assertIn("source: ./site", compose)
        self.assertIn("/dev/shm:/dev/shm:rw", compose)
        self.assertIn("target: /var/log/xray", compose)
        self.assertNotIn("target: /var/log/remnanode", compose)
        self.assertIn("cap_add:\n      - NET_ADMIN", compose)
        self.assertNotIn("restart: always", compose)
        self.assertEqual(compose.count("restart: unless-stopped"), 2)
        self.assertNotIn(":/etc/letsencrypt:ro", compose)
        for target_root in (
            "/etc/letsencrypt",
            "/var/lib/remnawave/configs/xray/ssl",
        ):
            self.assertIn(
                f":{target_root}/live/panel.example.com:ro", compose
            )
            self.assertIn(
                f":{target_root}/archive/panel.example.com:ro", compose
            )
        self.assertIn("listen unix:/dev/shm/nginx.sock ssl proxy_protocol;", nginx)
        self.assertIn("root /var/www/html;", nginx)
        self.assertIn("Content-Security-Policy", nginx)
        self.assertIn("location ~ (^|/)\\.", nginx)

    def test_node_http_redirect_rejects_unknown_host_and_allows_site_scripts(self) -> None:
        rendered = render_node_nginx(
            domain="node.example.com",
            certificate=certificate(),
        )

        default_http = rendered.split(
            "server {\n    listen 80 default_server;", 1
        )[1].split("}\n", 1)[0]
        redirect_http = rendered.split(
            "server {\n    listen 80;", 1
        )[1].split("}\n", 1)[0]
        self.assertIn("server_name _;", default_http)
        self.assertIn("return 444;", default_http)
        self.assertNotIn("$host", default_http)
        self.assertIn("server_name node.example.com;", redirect_http)
        self.assertIn("return 308 https://$host$request_uri;", redirect_http)
        self.assertIn("script-src 'self'", rendered)
        self.assertIn("connect-src 'none'", rendered)
        self.assertNotIn("script-src 'none'", rendered)

    def test_http01_certbot_command_is_noninteractive_and_has_all_domains(self) -> None:
        command = build_certbot_command(
            ["panel.example.com", "sub.example.com"],
            CertificateSpec(method="http-01", email="admin@example.com"),
        )

        self.assertEqual(command[:3], ["certbot", "certonly", "--non-interactive"])
        self.assertIn("--standalone", command)
        self.assertEqual(command.count("--domain"), 2)
        self.assertIn("panel.example.com", command)
        self.assertIn("sub.example.com", command)

    def test_cloudflare_certbot_command_uses_credentials_file_not_token(self) -> None:
        command = build_certbot_command(
            ["node.example.com"],
            CertificateSpec(
                method="cloudflare",
                email="admin@example.com",
                cloudflare_token="secret-token-that-must-not-appear",
            ),
            credentials_file=Path("/opt/remnanode/.cloudflare.ini"),
        )

        self.assertIn("--dns-cloudflare", command)
        self.assertIn(str(Path("/opt/remnanode/.cloudflare.ini")), command)
        self.assertNotIn("secret-token-that-must-not-appear", command)

    def test_certbot_hooks_refuse_foreign_file_without_partial_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            foreign = root / "pre" / "remnawave-manager-nginx"
            foreign.parent.mkdir(parents=True)
            foreign.write_text("#!/bin/sh\nforeign-command\n", encoding="utf-8")

            with self.assertRaisesRegex(ValidationError, "создан не менеджером"):
                install_renewal_hooks(
                    nginx_container="remnawave-nginx",
                    stop_for_standalone=False,
                    hook_root=root,
                )

            self.assertEqual(
                foreign.read_text(encoding="utf-8"), "#!/bin/sh\nforeign-command\n"
            )
            self.assertFalse((root / "deploy" / "remnawave-manager-nginx").exists())

    def test_certificate_obtain_checks_hook_ownership_before_certbot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            foreign = root / "deploy" / "remnawave-manager-nginx"
            foreign.parent.mkdir(parents=True)
            foreign.write_text("#!/bin/sh\nforeign-command\n", encoding="utf-8")
            runner = mock.Mock()

            with (
                mock.patch(
                    "remnawave_manager.certificates._DEFAULT_HOOK_ROOT",
                    root,
                ),
                self.assertRaisesRegex(ValidationError, "создан не менеджером"),
            ):
                obtain_certificate(
                    runner,
                    ["panel.example.com"],
                    CertificateSpec(method="http-01", email="admin@example.com"),
                    install_dir=root / "install",
                )

            runner.run.assert_not_called()

    def test_certbot_hooks_can_disable_manager_owned_standalone_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install_renewal_hooks(
                nginx_container="remnawave-nginx",
                stop_for_standalone=True,
                hook_root=root,
            )

            deploy = root / "deploy" / "remnawave-manager-nginx"
            pre = root / "pre" / "remnawave-manager-nginx"
            post = root / "post" / "remnawave-manager-nginx"
            self.assertTrue(deploy.is_file())
            self.assertTrue(pre.is_file())
            self.assertTrue(post.is_file())

            install_renewal_hooks(
                nginx_container="remnawave-nginx",
                stop_for_standalone=False,
                hook_root=root,
            )

            self.assertTrue(deploy.is_file())
            self.assertFalse(pre.exists())
            self.assertFalse(post.exists())

    def test_adoption_discovers_standalone_certbot_and_installs_transactional_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "letsencrypt"
            renewal = root / "renewal"
            renewal.mkdir(parents=True)
            (renewal / "node.example.com.conf").write_text(
                "version = 2.11.0\n"
                "archive_dir = /etc/letsencrypt/archive/node.example.com\n"
                "[renewalparams]\n"
                "authenticator = standalone\n"
                "renew_hook = sh -c 'cd /opt/remnanode && "
                "docker compose up -d remnawave-nginx'\n",
                encoding="utf-8",
            )
            nginx = Path(temporary) / "nginx.conf"
            nginx.write_text(
                "ssl_certificate /etc/letsencrypt/live/node.example.com/fullchain.pem;\n",
                encoding="utf-8",
            )
            compose = {
                "services": {
                    "remnawave-nginx": {
                        "volumes": [
                            {
                                "type": "bind",
                                "source": str(root),
                                "target": "/etc/letsencrypt",
                            }
                        ]
                    }
                }
            }
            inventory = Inventory(
                schema_version=1,
                role="node",
                install_dir="/opt/remnanode",
                compose_file="/opt/remnanode/docker-compose.yml",
                env_file="/opt/remnanode/.env",
                webserver="nginx",
                nginx_files=[str(nginx)],
                components={
                    "nginx": Component(
                        "nginx", "remnawave-nginx", container="remnawave-nginx"
                    )
                },
            )
            runner = mock.Mock(spec=Runner)

            plan = discover_certbot_renewal(
                compose, [nginx], letsencrypt_root=root
            )
            with mock.patch(
                "remnawave_manager.certificates._certbot_timer_state",
                return_value=("disabled", False),
            ):
                configured = configure_adopted_certbot(
                    runner, inventory, compose, letsencrypt_root=root
                )

            self.assertEqual(plan.certificate_names, ("node.example.com",))
            self.assertTrue(plan.uses_standalone)
            self.assertEqual(plan.legacy_renew_hooks, ("node.example.com",))
            self.assertEqual(configured, plan)
            renewed = (renewal / "node.example.com.conf").read_text(encoding="utf-8")
            self.assertNotIn("renew_hook", renewed)
            self.assertIn("authenticator = standalone", renewed)
            for phase in ("deploy", "pre", "post"):
                hook = root / "renewal-hooks" / phase / "remnawave-manager-nginx"
                self.assertTrue(hook.is_file())
                if os.name == "posix":
                    self.assertEqual(hook.stat().st_mode & 0o777, 0o700)
            runner.run.assert_any_call(
                ["systemctl", "enable", "--now", "certbot.timer"], timeout=120
            )
            self.assertTrue(inventory.features["certbot_renewal"])
            self.assertTrue(inventory.features["certbot_standalone"])
            self.assertTrue(
                inventory.features["certbot_legacy_renew_hook_removed"]
            )

    def test_adopted_certbot_restores_hooks_when_timer_enable_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "letsencrypt"
            renewal = root / "renewal"
            renewal.mkdir(parents=True)
            (renewal / "panel.example.com.conf").write_text(
                "version = 2.11.0\n"
                "[renewalparams]\n"
                "authenticator = dns-cloudflare\n",
                encoding="utf-8",
            )
            compose = {
                "services": {
                    "remnawave-nginx": {
                        "volumes": [
                            {
                                "type": "bind",
                                "source": str(root / "live" / "panel.example.com"),
                                "target": "/etc/nginx/ssl/panel.example.com",
                            }
                        ]
                    }
                }
            }
            inventory = Inventory(
                schema_version=1,
                role="panel",
                install_dir="/opt/remnawave",
                compose_file="/opt/remnawave/docker-compose.yml",
                env_file="/opt/remnawave/.env",
                webserver="nginx",
                components={"nginx": Component("nginx", "remnawave-nginx")},
            )
            runner = mock.Mock(spec=Runner)
            runner.run.side_effect = RuntimeError("systemd failure")

            with (
                mock.patch(
                    "remnawave_manager.certificates._certbot_timer_state",
                    return_value=("disabled", False),
                ),
                self.assertRaisesRegex(TransactionError, "rollback неполон"),
            ):
                configure_adopted_certbot(
                    runner, inventory, compose, letsencrypt_root=root
                )

            for phase in ("deploy", "pre", "post"):
                self.assertFalse(
                    (root / "renewal-hooks" / phase / "remnawave-manager-nginx").exists()
                )
            self.assertNotIn("certbot_renewal", inventory.features)

    def test_adopted_certbot_restores_crontab_when_timer_enable_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "letsencrypt"
            renewal = root / "renewal"
            renewal.mkdir(parents=True)
            (renewal / "panel.example.com.conf").write_text(
                "version = 2.11.0\n"
                "[renewalparams]\n"
                "authenticator = dns-cloudflare\n",
                encoding="utf-8",
            )
            compose = {
                "services": {
                    "remnawave-nginx": {
                        "volumes": [
                            {
                                "type": "bind",
                                "source": str(root),
                                "target": "/etc/letsencrypt",
                            }
                        ]
                    }
                }
            }
            inventory = Inventory(
                schema_version=1,
                role="panel",
                install_dir="/opt/remnawave",
                compose_file="/opt/remnawave/docker-compose.yml",
                env_file="/opt/remnawave/.env",
                webserver="nginx",
                components={"nginx": Component("nginx", "remnawave-nginx")},
            )
            original = (
                "15 2 * * * /usr/local/bin/custom-backup\n"
                "0 5 * * 0 /usr/bin/certbot renew --quiet "
                ">> /usr/local/remnawave_reverse/cron_jobs.log 2>&1\n"
            )
            runner = mock.Mock(spec=Runner)

            def run(args, **_kwargs):  # type: ignore[no-untyped-def]
                command = list(args)
                if command == ["crontab", "-u", "root", "-l"]:
                    return Result(tuple(command), 0, original, "")
                if command == [
                    "systemctl",
                    "enable",
                    "--now",
                    "certbot.timer",
                ]:
                    raise RuntimeError("systemd failure")
                if command == ["systemctl", "is-enabled", "certbot.timer"]:
                    return Result(tuple(command), 1, "disabled\n", "")
                if command == ["systemctl", "is-active", "certbot.timer"]:
                    return Result(tuple(command), 3, "inactive\n", "")
                return Result(tuple(command), 0, "", "")

            runner.run.side_effect = run
            with (
                mock.patch(
                    "remnawave_manager.certificates._certbot_timer_state",
                    return_value=("disabled", False),
                ),
                mock.patch(
                    "remnawave_manager.certificates.command_exists",
                    return_value=True,
                ),
                self.assertRaisesRegex(RuntimeError, "systemd failure"),
            ):
                configure_adopted_certbot(
                    runner,
                    inventory,
                    compose,
                    letsencrypt_root=root,
                )

            writes = [
                call.kwargs["input_text"]
                for call in runner.run.call_args_list
                if call.args[0] == ["crontab", "-u", "root", "-"]
            ]
            self.assertEqual(len(writes), 2)
            self.assertNotIn("remnawave_reverse/cron_jobs.log", writes[0])
            self.assertEqual(writes[1], original)
            self.assertNotIn("certbot_renewal", inventory.features)

    def test_adopted_certbot_rolls_back_when_inventory_commit_fails_after_replace(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            letsencrypt = root / "letsencrypt"
            renewal = letsencrypt / "renewal"
            renewal.mkdir(parents=True)
            (renewal / "panel.example.com.conf").write_text(
                "version = 2.11.0\n"
                "[renewalparams]\n"
                "authenticator = dns-cloudflare\n",
                encoding="utf-8",
            )
            compose = {
                "services": {
                    "remnawave-nginx": {
                        "volumes": [
                            {
                                "type": "bind",
                                "source": str(letsencrypt),
                                "target": "/etc/letsencrypt",
                            }
                        ]
                    }
                }
            }
            current = Inventory(
                schema_version=1,
                role="panel",
                install_dir="/opt/remnawave-old",
                compose_file="/opt/remnawave-old/docker-compose.yml",
                env_file=None,
                webserver="nginx",
            )
            inventory = Inventory(
                schema_version=1,
                role="panel",
                install_dir="/opt/remnawave",
                compose_file="/opt/remnawave/docker-compose.yml",
                env_file=None,
                webserver="nginx",
                components={"nginx": Component("nginx", "remnawave-nginx")},
            )
            store = StateStore(RuntimePaths(root / "state-root"))
            store.save_inventory(current)
            original_save = store.save_inventory

            def fail_after_replace(value):  # type: ignore[no-untyped-def]
                original_save(value)
                raise OSError("inventory directory fsync failed")

            runner = mock.Mock(spec=Runner)
            with (
                mock.patch(
                    "remnawave_manager.certificates._certbot_timer_state",
                    return_value=("disabled", False),
                ),
                mock.patch(
                    "remnawave_manager.certificates._certbot_timer_enablement",
                    return_value="disabled",
                ),
                mock.patch(
                    "remnawave_manager.certificates._certbot_timer_active",
                    return_value=False,
                ),
                mock.patch(
                    "remnawave_manager.certificates.command_exists",
                    return_value=False,
                ),
                mock.patch.object(
                    store,
                    "save_inventory",
                    side_effect=fail_after_replace,
                ),
                self.assertRaisesRegex(OSError, "inventory directory fsync failed"),
            ):
                configure_adopted_certbot(
                    runner,
                    inventory,
                    compose,
                    store=store,
                    letsencrypt_root=letsencrypt,
                )

            self.assertEqual(store.load_inventory().to_dict(), current.to_dict())
            self.assertEqual(inventory.features, {})
            for phase in ("deploy", "pre", "post"):
                self.assertFalse(
                    (
                        letsencrypt
                        / "renewal-hooks"
                        / phase
                        / "remnawave-manager-nginx"
                    ).exists()
                )

    def test_only_known_reverse_proxy_certbot_cron_is_removed(self) -> None:
        from remnawave_manager.certificates import _migrate_legacy_certbot_cron
        from remnawave_manager.runner import Result

        original = (
            "15 2 * * * /usr/local/bin/custom-backup >> /var/log/backup.log 2>&1\n"
            "0 5 * * 0 /usr/bin/certbot renew --quiet "
            ">> /usr/local/remnawave_reverse/cron_jobs.log 2>&1\n"
            "30 4 * * * /usr/bin/certbot renew --quiet >> /var/log/custom-certbot.log 2>&1\n"
        )
        runner = mock.Mock(spec=Runner)
        runner.run.return_value = Result(
            ("crontab", "-u", "root", "-l"), 0, original, ""
        )

        with mock.patch("remnawave_manager.certificates.command_exists", return_value=True):
            previous, changed = _migrate_legacy_certbot_cron(runner)

        self.assertTrue(changed)
        self.assertEqual(previous, original)
        replacement = runner.run.call_args_list[1].kwargs["input_text"]
        self.assertIn("custom-backup", replacement)
        self.assertIn("custom-certbot.log", replacement)
        self.assertNotIn("remnawave_reverse/cron_jobs.log", replacement)

    def test_node_firewall_restricts_api_to_panel_and_preserves_custom_ssh(self) -> None:
        commands = build_firewall_commands("node", [22022], panel_ip="203.0.113.10")

        self.assertIn(
            ["ufw", "allow", "22022/tcp", "comment", "remnawave-manager:ssh"],
            commands,
        )
        node_rule = next(
            command
            for command in commands
            if "remnawave-manager:panel-api" in command
        )
        self.assertEqual(
            node_rule,
            [
                "ufw",
                "insert",
                "1",
                "allow",
                "from",
                "203.0.113.10",
                "to",
                "any",
                "port",
                "2222",
                "proto",
                "tcp",
                "comment",
                "remnawave-manager:panel-api",
            ],
        )
        deny_rule = next(
            command
            for command in commands
            if "remnawave-manager:node-api-deny" in command
        )
        self.assertEqual(deny_rule[0:4], ["ufw", "insert", "2", "deny"])
        self.assertIn("2222", deny_rule)

    def test_firewall_rejects_missing_panel_ip(self) -> None:
        with self.assertRaises(ValidationError):
            build_firewall_commands("node", [22])

    def test_compose_generator_rejects_latest(self) -> None:
        with self.assertRaises(ValidationError):
            render_node_compose(node_image="remnawave/node:latest", certificate=certificate())

        with self.assertRaises(ValidationError):
            render_node_compose(node_image="remnawave/node:3.0.0", certificate=certificate())

    def test_missing_expected_managed_file_aborts_inventory_publication(self) -> None:
        inventory = Inventory(
            schema_version=1,
            role="node",
            install_dir="/opt/remnanode",
            compose_file="/opt/remnanode/docker-compose.yml",
            env_file="/opt/remnanode/.env",
            webserver="nginx",
        )
        store = mock.Mock(spec=StateStore)

        with self.assertRaisesRegex(TransactionError, "managed-файл отсутствует"):
            _add_managed_files(
                store,
                inventory,
                [Path("/definitely/missing/rwm-secret")],
                kind="secret",
            )

        store.save_inventory.assert_not_called()

    def test_compose_down_exception_does_not_skip_external_resource_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            site = root / "template"
            site.mkdir()
            (site / "index.html").write_text("placeholder", encoding="utf-8")
            install_dir = root / "node"
            store = StateStore(RuntimePaths(root / "runtime"))
            firewall_transaction = mock.Mock()
            certificate_transaction = mock.Mock()
            material = CertificateMaterial(
                host_root=root / "letsencrypt",
                container_root="/etc/letsencrypt",
                fullchain="/etc/letsencrypt/live/node.example.com/fullchain.pem",
                private_key="/etc/letsencrypt/live/node.example.com/privkey.pem",
                managed_by_certbot=True,
                transaction=certificate_transaction,
            )
            runner = mock.Mock(spec=Runner)

            def run(args, **_kwargs):  # type: ignore[no-untyped-def]
                command = tuple(args)
                if args[:2] == ["docker", "compose"] and "down" in args:
                    raise TimeoutError("compose cleanup timed out")
                return Result(command, 0, "", "")

            runner.run.side_effect = run

            with (
                mock.patch("remnawave_manager.install._preflight"),
                mock.patch("remnawave_manager.install._ensure_container_names_available"),
                mock.patch(
                    "remnawave_manager.install._pull_component_image",
                    return_value="remnawave/node:3.0.0@sha256:" + "c" * 64,
                ),
                mock.patch("remnawave_manager.install._pull_base_images"),
                mock.patch(
                    "remnawave_manager.install.plan_firewall",
                    return_value=FirewallPlan("node", (22,), ()),
                ),
                mock.patch(
                    "remnawave_manager.install.apply_firewall_transactional",
                    return_value=firewall_transaction,
                ),
                mock.patch(
                    "remnawave_manager.install.obtain_certificate",
                    return_value=material,
                ),
                mock.patch(
                    "remnawave_manager.install.wait_container",
                    side_effect=RuntimeError("health failed"),
                ),
                self.assertRaisesRegex(
                    TransactionError,
                    "docker compose down: TimeoutError",
                ),
            ):
                install_node(
                    runner,
                    store,
                    NodeInstallOptions(
                        domain="node.example.com",
                        panel_ip="203.0.113.10",
                        secret_key="node.secret_123",
                        certificate=CertificateSpec(
                            method="http-01", email="admin@example.com"
                        ),
                        site_source=site,
                        panel_3_3_ready=True,
                        install_dir=install_dir,
                    ),
                )

            firewall_transaction.rollback.assert_called_once_with()
            certificate_transaction.rollback.assert_called_once_with(runner)
            blocker_checks = [
                call
                for call in runner.run.call_args_list
                if call.args[0][:3] == ["docker", "ps", "--all"]
            ]
            self.assertEqual(len(blocker_checks), 2)

    def test_post_commit_bootstrap_cleanup_failure_keeps_panel_managed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install_dir = root / "panel"
            store = StateStore(RuntimePaths(root / "runtime"))
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = Result(("docker", "compose"), 0, "", "")
            firewall_transaction = mock.Mock()
            material = CertificateMaterial(
                host_root=root / "letsencrypt",
                container_root="/etc/letsencrypt",
                fullchain="/etc/letsencrypt/live/panel.example.com/fullchain.pem",
                private_key="/etc/letsencrypt/live/panel.example.com/privkey.pem",
                managed_by_certbot=True,
            )
            adopted = Inventory(
                schema_version=1,
                role="panel",
                install_dir=str(install_dir),
                compose_file=str(install_dir / "docker-compose.yml"),
                env_file=str(install_dir / ".env"),
                webserver="nginx",
            )
            api = mock.Mock()
            api.register_or_login.return_value = "admin-token"
            api.create_subscription_token.return_value = "subscription-token"
            original_unlink = Path.unlink

            def selective_unlink(path, missing_ok=False):  # type: ignore[no-untyped-def]
                if path.name == ".bootstrap-credentials.json":
                    raise PermissionError("immutable bootstrap file")
                return original_unlink(path, missing_ok=missing_ok)

            with (
                mock.patch("remnawave_manager.install._preflight"),
                mock.patch("remnawave_manager.install._ensure_container_names_available"),
                mock.patch(
                    "remnawave_manager.install._pull_component_image",
                    side_effect=[
                        "remnawave/backend:3.2.0@sha256:" + "a" * 64,
                        "remnawave/subscription-page:8.0.0@sha256:" + "b" * 64,
                    ],
                ),
                mock.patch("remnawave_manager.install._pull_base_images"),
                mock.patch(
                    "remnawave_manager.install.plan_firewall",
                    return_value=FirewallPlan("panel", (22,), ()),
                ),
                mock.patch(
                    "remnawave_manager.install.apply_firewall_transactional",
                    return_value=firewall_transaction,
                ),
                mock.patch(
                    "remnawave_manager.install.obtain_certificate",
                    return_value=material,
                ),
                mock.patch("remnawave_manager.install.wait_container"),
                mock.patch("remnawave_manager.install._wait_api_ready"),
                mock.patch("remnawave_manager.install.check_panel_http"),
                mock.patch("remnawave_manager.install.check_subscription_http"),
                mock.patch("remnawave_manager.install.check_subscription_api_scopes"),
                mock.patch(
                    "remnawave_manager.install.adopt",
                    return_value=adopted,
                ),
            ):
                result = install_panel(
                    runner,
                    store,
                    PanelInstallOptions(
                        panel_domain="panel.example.com",
                        subscription_domain="sub.example.com",
                        certificate=CertificateSpec(
                            method="http-01", email="admin@example.com"
                        ),
                        install_dir=install_dir,
                    ),
                    api_factory=lambda _url: api,
                )

            self.assertTrue(store.paths.inventory.is_file())
            self.assertTrue(store.paths.secrets.is_file())
            self.assertTrue((install_dir / ".bootstrap-credentials.json").is_file())
            self.assertFalse((install_dir / ".rwm-install-state.json").exists())
            firewall_transaction.commit.assert_called_once_with()
            firewall_transaction.rollback.assert_not_called()
            with (
                mock.patch.object(Path, "unlink", new=selective_unlink),
                self.assertRaisesRegex(TransactionError, "не удалось удалить"),
            ):
                complete_panel_credentials_handoff(result)
            self.assertTrue((install_dir / ".bootstrap-credentials.json").is_file())

            complete_panel_credentials_handoff(result)
            self.assertFalse((install_dir / ".bootstrap-credentials.json").exists())
            down_commands = [
                call.args[0]
                for call in runner.run.call_args_list
                if call.args[0][:2] == ["docker", "compose"]
                and "down" in call.args[0]
            ]
            self.assertEqual(down_commands, [])

    def test_node_install_checks_runtime_before_adoption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            site = root / "template"
            site.mkdir()
            (site / "index.html").write_text("placeholder", encoding="utf-8")
            install_dir = root / "node"
            material = CertificateMaterial(
                host_root=root / "certificates",
                container_root="/etc/letsencrypt",
                fullchain="/etc/letsencrypt/live/node.example.com/fullchain.pem",
                private_key="/etc/letsencrypt/live/node.example.com/privkey.pem",
                managed_by_certbot=True,
            )
            adopted = Inventory(
                schema_version=1,
                role="node",
                install_dir=str(install_dir),
                compose_file=str(install_dir / "docker-compose.yml"),
                env_file=str(install_dir / ".env"),
                webserver="nginx",
                components={
                    "node": Component(name="node", service="remnanode", container="remnanode")
                },
            )
            store = mock.Mock()
            store.load_settings.return_value = {"registry": "docker-hub"}
            store.paths.state = root / "state"
            events: list[str] = []

            class EventRunner(Runner):
                def run(self, args, **kwargs):  # type: ignore[no-untyped-def]
                    if args[:2] == ["docker", "compose"] and "up" in args:
                        events.append("compose-up")
                    return super().run(args, **kwargs)

            def runtime_check(_runner, candidate):  # type: ignore[no-untyped-def]
                self.assertEqual(candidate.components["node"].container, "remnanode")
                events.append("runtime")

            def adopt_after_runtime(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                events.append("adopt")
                return adopted

            firewall_transaction = mock.Mock()
            firewall_transaction.commit.side_effect = lambda: events.append("commit")

            def apply_firewall_before_containers(*_args, **kwargs):  # type: ignore[no-untyped-def]
                self.assertEqual(kwargs["transaction_root"], root / "state" / "firewall-transactions")
                events.append("firewall")
                return firewall_transaction

            def obtain_after_firewall(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                events.append("certificate")
                return material

            with (
                mock.patch("remnawave_manager.install._preflight"),
                mock.patch("remnawave_manager.install._ensure_container_names_available"),
                mock.patch(
                    "remnawave_manager.install._pull_component_image",
                    return_value="remnawave/node:3.0.0@sha256:" + "c" * 64,
                ),
                mock.patch("remnawave_manager.install._pull_base_images"),
                mock.patch(
                    "remnawave_manager.install.plan_firewall",
                    return_value=FirewallPlan("node", (22,), ()),
                ),
                mock.patch(
                    "remnawave_manager.install.apply_firewall_transactional",
                    side_effect=apply_firewall_before_containers,
                ),
                mock.patch(
                    "remnawave_manager.install.obtain_certificate",
                    side_effect=obtain_after_firewall,
                ),
                mock.patch("remnawave_manager.install.wait_container"),
                mock.patch("remnawave_manager.install.wait_for_paths"),
                mock.patch(
                    "remnawave_manager.install.wait_node_runtime",
                    side_effect=runtime_check,
                ),
                mock.patch(
                    "remnawave_manager.install.adopt",
                    side_effect=adopt_after_runtime,
                ),
            ):
                result = install_node(
                    EventRunner(dry_run=True),
                    store,
                    NodeInstallOptions(
                        domain="node.example.com",
                        panel_ip="203.0.113.10",
                        secret_key="node.secret_123",
                        certificate=CertificateSpec(
                            method="http-01", email="admin@example.com"
                        ),
                        site_source=site,
                        panel_3_3_ready=True,
                        install_dir=install_dir,
                    ),
                )

            self.assertEqual(
                events,
                [
                    "firewall",
                    "certificate",
                    "compose-up",
                    "compose-up",
                    "runtime",
                    "adopt",
                    "commit",
                ],
            )
            self.assertEqual(result.inventory, adopted)

    def test_state_conflict_prevents_external_resource_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            site = root / "template"
            site.mkdir()
            (site / "index.html").write_text("placeholder", encoding="utf-8")
            install_dir = root / "node"
            store = mock.Mock()
            store.load_settings.return_value = {"registry": "docker-hub"}
            store.paths.state = root / "state"
            firewall_transaction = mock.Mock()
            rollback_resources = mock.Mock()
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = Result(("docker", "compose"), 0, "", "")

            with (
                mock.patch("remnawave_manager.install._preflight"),
                mock.patch("remnawave_manager.install._ensure_container_names_available"),
                mock.patch(
                    "remnawave_manager.install._pull_component_image",
                    return_value="remnawave/node:3.0.0@sha256:" + "c" * 64,
                ),
                mock.patch("remnawave_manager.install._pull_base_images"),
                mock.patch(
                    "remnawave_manager.install.plan_firewall",
                    return_value=FirewallPlan("node", (22,), ()),
                ),
                mock.patch(
                    "remnawave_manager.install.apply_firewall_transactional",
                    return_value=firewall_transaction,
                ),
                mock.patch(
                    "remnawave_manager.install.obtain_certificate",
                    return_value=certificate(),
                ),
                mock.patch(
                    "remnawave_manager.install.wait_container",
                    side_effect=RuntimeError("health failed"),
                ),
                mock.patch(
                    "remnawave_manager.install._rollback_failed_install_state",
                    return_value="inventory conflict",
                ),
                mock.patch(
                    "remnawave_manager.install._rollback_install_resources",
                    rollback_resources,
                ),
                self.assertRaisesRegex(TransactionError, "manager state не удалось вернуть"),
            ):
                install_node(
                    runner,
                    store,
                    NodeInstallOptions(
                        domain="node.example.com",
                        panel_ip="203.0.113.10",
                        secret_key="node.secret_123",
                        certificate=CertificateSpec(
                            method="http-01", email="admin@example.com"
                        ),
                        site_source=site,
                        panel_3_3_ready=True,
                        install_dir=install_dir,
                    ),
                )

            rollback_resources.assert_not_called()
            firewall_transaction.rollback.assert_not_called()
            down_commands = [
                call.args[0]
                for call in runner.run.call_args_list
                if call.args[0][:2] == ["docker", "compose"]
                and "down" in call.args[0]
            ]
            self.assertEqual(len(down_commands), 1)
            self.assertNotIn("--volumes", down_commands[0])


if __name__ == "__main__":
    unittest.main()
