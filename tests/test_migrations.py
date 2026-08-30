from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remnawave_manager.adopt import adopt
from remnawave_manager.backup import BackupResult
from remnawave_manager.compat import component_target, require_supported_source
from remnawave_manager.envfile import EnvDocument
from remnawave_manager.errors import TransactionError, ValidationError
from remnawave_manager.journal import TransactionJournal
from remnawave_manager.models import Component, Inventory
from remnawave_manager.paths import RuntimePaths
from remnawave_manager.runner import Result
from remnawave_manager.state import StateStore
from remnawave_manager.update import (
    _dump_node_config,
    _node_secret,
    _reality_without_min_version,
    _reconcile_running_services,
    _require_private_permissions,
    _settings,
    update_node,
    update_panel_stack,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"

PANEL_2_8_1_DIGEST = (
    "sha256:361f9bb0b183d4fcefea2f1f7163db490e2aa1ec3b4bdde016a9ab9229ce956b"
)
SUBSCRIPTION_7_2_6_DIGEST = (
    "sha256:da5ee26ec70ecd81e57303993e8bfb74c8e52f2fa74644b84aad53324cde2e8c"
)
NODE_2_8_0_DIGEST = (
    "sha256:03f14935751b4ab565181e2b1766ccd1a9ac349d6839acd3ee49014e543fa232"
)
POSTGRES_18_3_DIGEST = (
    "sha256:7e32e9833a6fb1c92c32552794cb6ed569d51b445a54907d35fc112ef39684db"
)
LEGACY_POSTGRES_IMAGE_ID = "sha256:" + "1" * 64
LEGACY_PANEL_IMAGE_ID = "sha256:" + "2" * 64
LEGACY_VALKEY_IMAGE_ID = "sha256:" + "3" * 64
LEGACY_SUBSCRIPTION_IMAGE_ID = "sha256:" + "4" * 64
LEGACY_NGINX_IMAGE_ID = "sha256:" + "5" * 64
LEGACY_NODE_IMAGE_ID = "sha256:" + "6" * 64
NODE_SECRET_PAYLOAD = (
    "eyJjYUNlcnRQZW0iOiJmaXh0dXJlLWNhIiwiand0UHVibGljS2V5IjoiZml4dHVyZS1qd3Qi"
    "LCJub2RlQ2VydFBlbSI6ImZpeHR1cmUtY2VydCIsIm5vZGVLZXlQZW0iOiJmaXh0dXJlLWtleSJ9"
)


def target_image(component: str, registry: str = "docker-hub") -> str:
    target = component_target(component, registry)
    return f"{target['image']}@{target['digest']}"


TARGET_IMAGES = {
    "panel": target_image("panel"),
    "subscription": target_image("subscription"),
    "database": target_image("database"),
    "node": target_image("node"),
}


class LegacyRunner:
    def __init__(self, install_dir: Path, fixture_name: str) -> None:
        self.install_dir = install_dir
        self.fixture_name = fixture_name
        self.calls: list[tuple[str, ...]] = []
        self.inspect_data = self._inspect_data()

    def _inspect_data(self) -> dict[str, dict[str, object]]:
        def running(image: str, image_id: str) -> dict[str, object]:
            return {
                "Config": {"Image": image},
                "Image": image_id,
                "State": {"Running": True, "Status": "running"},
            }

        if self.fixture_name == "legacy_panel_2_8_1":
            return {
                "remnawave-db": running("postgres:18.3", LEGACY_POSTGRES_IMAGE_ID),
                "remnawave": running("remnawave/backend:2", LEGACY_PANEL_IMAGE_ID),
                "remnawave-redis": running(
                    "valkey/valkey:9.0.3-alpine", LEGACY_VALKEY_IMAGE_ID
                ),
                "remnawave-subscription-page": running(
                    "remnawave/subscription-page:latest",
                    LEGACY_SUBSCRIPTION_IMAGE_ID,
                ),
                "remnawave-nginx": running("nginx:1.28", LEGACY_NGINX_IMAGE_ID),
            }
        return {
            "remnawave-nginx": running("nginx:1.28", LEGACY_NGINX_IMAGE_ID),
            "remnanode": running("remnawave/node:latest", LEGACY_NODE_IMAGE_ID),
        }

    def _compose_config(self) -> dict[str, object]:
        template = json.loads(
            (self.install_dir / "compose-config.json").read_text(encoding="utf-8")
        )

        def replace(value):  # type: ignore[no-untyped-def]
            if isinstance(value, str):
                return value.replace("__ROOT__", self.install_dir.as_posix())
            if isinstance(value, list):
                return [replace(item) for item in value]
            if isinstance(value, dict):
                return {key: replace(item) for key, item in value.items()}
            return value

        return replace(template)

    def run(self, args, **kwargs):  # type: ignore[no-untyped-def]
        command = tuple(args)
        self.calls.append(command)
        if command[:2] == ("docker", "compose") and "--format" in command:
            return Result(command, 0, json.dumps(self._compose_config()), "")
        if command[:3] == ("docker", "inspect", "--format"):
            container = command[-1]
            return Result(command, 0, json.dumps(self.inspect_data[container]), "")
        if command[:3] == ("docker", "image", "inspect"):
            digests = {
                LEGACY_POSTGRES_IMAGE_ID: [
                    f"postgres@{POSTGRES_18_3_DIGEST}"
                ],
                LEGACY_PANEL_IMAGE_ID: [f"remnawave/backend@{PANEL_2_8_1_DIGEST}"],
                LEGACY_SUBSCRIPTION_IMAGE_ID: [
                    f"remnawave/subscription-page@{SUBSCRIPTION_7_2_6_DIGEST}"
                ],
                LEGACY_NODE_IMAGE_ID: [f"remnawave/node@{NODE_2_8_0_DIGEST}"],
            }.get(command[-1], [])
            return Result(command, 0, json.dumps(digests), "")
        if command == ("ip", "-j", "link", "show"):
            links = [{"ifname": "lo"}]
            if self.fixture_name == "legacy_node_2_8_0":
                links.append({"ifname": "warp"})
            return Result(command, 0, json.dumps(links), "")
        if command[:4] == ("docker", "exec", "remnanode", "cli"):
            payload = (self.install_dir / "xray.json").read_text(encoding="utf-8")
            return Result(command, 0, payload, "")
        if command[:2] == ("docker", "run") and "RWM_NODE_SECRET_OK" in command[-1]:
            return Result(command, 0, "RWM_NODE_SECRET_OK\n", "")
        return Result(command, 0, "", "")


def copy_fixture(temporary: str, name: str) -> Path:
    install_dir = Path(temporary) / "install"
    shutil.copytree(FIXTURES / name, install_dir)
    return install_dir


def adopt_fixture(temporary: str, name: str):  # type: ignore[no-untyped-def]
    install_dir = copy_fixture(temporary, name)
    runner = LegacyRunner(install_dir, name)
    store = StateStore(RuntimePaths(Path(temporary) / "runtime"))
    role = "panel" if name == "legacy_panel_2_8_1" else "node"
    inventory = adopt(runner, store, directory=install_dir, requested_role=role)
    # Successful update fixtures model the documented permission-repair step.
    if os.name == "posix":
        for item in inventory.managed_files:
            if item.kind in {"compose", "env", "nginx", "secret"}:
                Path(item.path).chmod(0o600)
    return install_dir, runner, store, inventory


def managed_payloads(inventory) -> dict[Path, bytes]:  # type: ignore[no-untyped-def]
    return {Path(item.path): Path(item.path).read_bytes() for item in inventory.managed_files}


def restore_payloads(payloads: dict[Path, bytes]) -> None:
    for path, payload in payloads.items():
        path.write_bytes(payload)


def fake_pull(_runner, component: str, registry: str) -> str:  # type: ignore[no-untyped-def]
    if component == "database" and registry != "docker-hub":
        raise AssertionError("PostgreSQL must be pulled from its explicit Docker Hub contract")
    return TARGET_IMAGES[component]


class LegacyAdoptionTests(unittest.TestCase):
    def test_panel_2_8_1_and_subscription_7_2_6_are_identified_with_postgres_18_3(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, runner, _, inventory = adopt_fixture(temporary, "legacy_panel_2_8_1")

            self.assertEqual(inventory.role, "panel")
            self.assertEqual(set(inventory.components), {"panel", "subscription", "database", "cache", "nginx"})
            self.assertEqual(
                require_supported_source(runner, "panel", inventory.components["panel"]),
                "2.8.1",
            )
            self.assertEqual(
                require_supported_source(
                    runner, "subscription", inventory.components["subscription"]
                ),
                "7.2.6",
            )
            self.assertEqual(
                require_supported_source(runner, "database", inventory.components["database"]),
                "18.3",
            )

    def test_node_adoption_records_warp_xhttp_yandex_and_all_nginx_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install_dir, runner, _, inventory = adopt_fixture(
                temporary, "legacy_node_2_8_0"
            )

            self.assertEqual(
                require_supported_source(runner, "node", inventory.components["node"]),
                "2.8.0",
            )
            self.assertEqual(inventory.warp_interfaces, ["warp"])
            self.assertEqual(
                inventory.xhttp_sockets,
                ["/dev/shm/nginx.sock", "/dev/shm/xray-xhttp.sock"],
            )
            self.assertTrue(inventory.features["warp"])
            self.assertTrue(inventory.features["xhttp_stream_separation"])
            self.assertTrue(inventory.features["yandex_cdn"])
            self.assertEqual(
                set(map(Path, inventory.nginx_files)),
                {
                    (install_dir / "nginx/nginx.conf").resolve(),
                    (install_dir / "nginx/conf.d/xhttp-yandex.conf").resolve(),
                },
            )


class LegacyPanelMigrationTests(unittest.TestCase):
    def test_update_preflight_rejects_world_readable_private_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, _, _, inventory = adopt_fixture(temporary, "legacy_panel_2_8_1")
            env_path = Path(inventory.env_file or "")
            env_path.chmod(0o644)

            with self.assertRaisesRegex(
                ValidationError,
                r"diagnose --repair-permissions",
            ):
                _require_private_permissions(
                    inventory,
                    expected_uid=env_path.stat().st_uid,
                )

            self.assertIn(env_path, {Path(item.path) for item in inventory.managed_files})

    def test_update_settings_are_strictly_validated(self) -> None:
        store = mock.Mock(spec=StateStore)

        store.load_settings.return_value = {}
        self.assertEqual(_settings(store), ("docker-hub", 10))

        for registry in (None, False, 7, "", "quay"):
            with self.subTest(registry=registry):
                store.load_settings.return_value = {
                    "registry": registry,
                    "backup_retention": 10,
                }
                with self.assertRaisesRegex(ValidationError, "Docker Registry"):
                    _settings(store)

        for retention in (None, False, 0, 1001, "10", [], 1.5):
            with self.subTest(retention=retention):
                store.load_settings.return_value = {
                    "registry": "docker-hub",
                    "backup_retention": retention,
                }
                with self.assertRaisesRegex(ValidationError, "от 1 до 1000"):
                    _settings(store)

    def test_panel_update_rejects_ghcr_before_pulling_any_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, runner, store, _ = adopt_fixture(temporary, "legacy_panel_2_8_1")
            store.save_settings({"registry": "ghcr", "backup_retention": 10})

            with (
                mock.patch("remnawave_manager.update.pull_verified") as pull,
                mock.patch("remnawave_manager.update.create_backup") as backup,
                self.assertRaisesRegex(ValidationError, "registry select docker-hub"),
            ):
                update_panel_stack(runner, store)

            pull.assert_not_called()
            backup.assert_not_called()
            self.assertFalse((store.paths.state / "active-transaction.json").exists())

    def test_invalid_v3_environment_aborts_before_backup_or_image_pull(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install_dir, runner, store, _ = adopt_fixture(
                temporary, "legacy_panel_2_8_1"
            )
            env_path = install_dir / ".env"
            env_path.write_text(
                env_path.read_text(encoding="utf-8").replace(
                    "METRICS_PASS=legacy-metrics-password\n",
                    "",
                ),
                encoding="utf-8",
            )
            adopt(
                runner,
                store,
                directory=install_dir,
                requested_role="panel",
            )

            with (
                mock.patch("remnawave_manager.update.create_backup") as backup,
                mock.patch("remnawave_manager.update.pull_verified") as pull,
                self.assertRaisesRegex(ValidationError, "METRICS_PASS"),
            ):
                update_panel_stack(runner, store)

            backup.assert_not_called()
            pull.assert_not_called()

    def test_stale_transaction_blocks_update_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, runner, store, _ = adopt_fixture(temporary, "legacy_panel_2_8_1")
            journal = store.paths.state / "active-transaction.json"
            journal.write_text(
                '{"operation":"panel-update","phase":"starting-panel"}\n',
                encoding="utf-8",
            )

            with (
                mock.patch("remnawave_manager.update.create_backup") as backup,
                self.assertRaisesRegex(ValidationError, "незавершённая транзакция"),
            ):
                update_panel_stack(runner, store)

            backup.assert_not_called()
            self.assertIn("starting-panel", journal.read_text(encoding="utf-8"))

    def test_drift_during_image_preflight_aborts_before_first_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install_dir, runner, store, _ = adopt_fixture(
                temporary, "legacy_panel_2_8_1"
            )
            compose_before = (install_dir / "docker-compose.yml").read_bytes()
            env_before = (install_dir / ".env").read_bytes()
            nginx = install_dir / "nginx.conf"
            external_change = nginx.read_text(encoding="utf-8") + "\n# operator change\n"
            backup = BackupResult(Path(temporary) / "pre-panel.tar.gz", {})

            def pull_with_drift(_runner, component, registry):  # type: ignore[no-untyped-def]
                image = fake_pull(_runner, component, registry)
                if component == "database":
                    nginx.write_text(external_change, encoding="utf-8")
                return image

            with (
                mock.patch("remnawave_manager.update.create_backup", return_value=backup),
                mock.patch(
                    "remnawave_manager.update.pull_verified",
                    side_effect=pull_with_drift,
                ),
                mock.patch("remnawave_manager.update.restore_backup") as rollback,
                self.assertRaisesRegex(ValidationError, "Конфигурация изменилась"),
            ):
                update_panel_stack(runner, store)

            rollback.assert_not_called()
            self.assertEqual((install_dir / "docker-compose.yml").read_bytes(), compose_before)
            self.assertEqual((install_dir / ".env").read_bytes(), env_before)
            self.assertEqual(nginx.read_text(encoding="utf-8"), external_change)
            self.assertFalse((store.paths.state / "active-transaction.json").exists())

    def test_drift_during_database_dump_is_not_overwritten_or_restored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install_dir, runner, store, _ = adopt_fixture(
                temporary, "legacy_panel_2_8_1"
            )
            nginx = install_dir / "nginx.conf"
            external_change = nginx.read_text(encoding="utf-8") + "\n# changed during dump\n"
            backup = BackupResult(Path(temporary) / "pre-panel.tar.gz", {})

            def backup_with_drift(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                nginx.write_text(external_change, encoding="utf-8")
                return backup

            with (
                mock.patch(
                    "remnawave_manager.update.create_backup",
                    side_effect=backup_with_drift,
                ),
                mock.patch("remnawave_manager.update.pull_verified", side_effect=fake_pull),
                mock.patch("remnawave_manager.update.validate_rendered_compose"),
                mock.patch("remnawave_manager.update.ensure_gzip") as migrate_nginx,
                mock.patch("remnawave_manager.update.restore_backup") as restore,
                self.assertRaisesRegex(TransactionError, "Конфигурация изменилась"),
            ):
                update_panel_stack(runner, store)

            migrate_nginx.assert_not_called()
            restore.assert_not_called()
            self.assertEqual(nginx.read_text(encoding="utf-8"), external_change)
            self.assertFalse((store.paths.state / "active-transaction.json").exists())

    def test_database_pull_failure_does_not_touch_or_restore_the_legacy_stack(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, runner, store, inventory = adopt_fixture(
                temporary, "legacy_panel_2_8_1"
            )
            originals = managed_payloads(inventory)
            backup = BackupResult(Path(temporary) / "pre-panel.tar.gz", {})
            calls_before_update = len(runner.calls)

            def fail_database_pull(_runner, component, registry):  # type: ignore[no-untyped-def]
                if component == "database":
                    raise TransactionError("Docker Hub is unavailable")
                return fake_pull(_runner, component, registry)

            with (
                mock.patch("remnawave_manager.update.create_backup", return_value=backup),
                mock.patch(
                    "remnawave_manager.update.pull_verified",
                    side_effect=fail_database_pull,
                ),
                mock.patch("remnawave_manager.update.restore_backup") as rollback,
                self.assertRaises(TransactionError),
            ):
                update_panel_stack(runner, store)

            rollback.assert_not_called()
            self.assertEqual(managed_payloads(inventory), originals)
            update_calls = runner.calls[calls_before_update:]
            self.assertFalse(
                any(
                    command[:2] == ("docker", "compose")
                    and ("stop" in command or "up" in command)
                    for command in update_calls
                )
            )
            self.assertFalse((store.paths.state / "active-transaction.json").exists())

    def test_update_changes_only_version_contracts_and_migrates_the_v3_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install_dir, runner, store, inventory = adopt_fixture(
                temporary, "legacy_panel_2_8_1"
            )
            original_compose = (install_dir / "docker-compose.yml").read_text(encoding="utf-8")
            backup = BackupResult(Path(temporary) / "pre-panel.tar.gz", {})
            calls_before_update = len(runner.calls)

            with (
                mock.patch("remnawave_manager.update.create_backup", return_value=backup),
                mock.patch("remnawave_manager.update.pull_verified", side_effect=fake_pull) as pull,
                mock.patch("remnawave_manager.update.validate_rendered_compose"),
                mock.patch("remnawave_manager.update.wait_container") as wait,
                mock.patch("remnawave_manager.update.wait_panel_http"),
                mock.patch("remnawave_manager.update.check_subscription_http"),
                mock.patch("remnawave_manager.update.check_subscription_api_scopes"),
                mock.patch("remnawave_manager.update.test_nginx"),
                mock.patch("remnawave_manager.update.adopt"),
            ):
                result = update_panel_stack(runner, store)

            self.assertEqual(result, backup)
            pull.assert_any_call(runner, "database", "docker-hub")
            updated_compose = (install_dir / "docker-compose.yml").read_text(encoding="utf-8")
            restored_scalars = (
                updated_compose.replace(TARGET_IMAGES["panel"], "remnawave/backend:2")
                .replace(
                    TARGET_IMAGES["subscription"], "remnawave/subscription-page:latest"
                )
                .replace(TARGET_IMAGES["database"], "postgres:18.3")
            )
            self.assertEqual(restored_scalars, original_compose)
            updated_nginx = (install_dir / "nginx.conf").read_text(encoding="utf-8")
            self.assertIn("application/javascript", updated_nginx)
            self.assertIn("application/wasm", updated_nginx)
            self.assertEqual(updated_nginx.count("access_log off;"), 1)

            env = EnvDocument.load(install_dir / ".env")
            self.assertEqual(env.raw_value("APP_SECRET"), "'legacy-auth-secret-with-quotes'")
            self.assertFalse(env.has("JWT_AUTH_SECRET"))
            self.assertFalse(env.has("JWT_API_TOKENS_SECRET"))
            self.assertFalse(env.has("SWAGGER_PATH"))
            self.assertFalse(env.has("SCALAR_PATH"))
            self.assertFalse(env.has("IS_DOCS_ENABLED"))
            self.assertEqual(env.effective_value("CUSTOM_OPERATOR_SETTING"), "keep-this-value")

            update_calls = runner.calls[calls_before_update:]
            database_up = next(
                index
                for index, command in enumerate(update_calls)
                if "up" in command and command[-1] == inventory.components["database"].service
            )
            panel_up = next(
                index
                for index, command in enumerate(update_calls)
                if "up" in command and command[-1] == inventory.components["panel"].service
            )
            self.assertLess(database_up, panel_up)
            self.assertIn("--force-recreate", update_calls[database_up])
            self.assertEqual(wait.call_args_list[0].args[1].name, "database")
            self.assertTrue(wait.call_args_list[0].kwargs["require_health"])

    def test_panel_backup_is_created_only_after_write_path_is_quiesced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, runner, store, inventory = adopt_fixture(
                temporary, "legacy_panel_2_8_1"
            )
            backup = BackupResult(Path(temporary) / "pre-panel.tar.gz", {})
            panel_service = inventory.components["panel"].service
            subscription_service = inventory.components["subscription"].service
            running = {component.service for component in inventory.components.values()}
            quiesced = running - {panel_service, subscription_service}
            journal_path = store.paths.state / "active-transaction.json"

            def create_after_stop(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                stop_calls = [
                    command
                    for command in runner.calls
                    if command[:2] == ("docker", "compose") and "stop" in command
                ]
                self.assertEqual(len(stop_calls), 1)
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                self.assertEqual(journal["phase"], "creating-backup")
                self.assertNotIn("backup", journal)
                self.assertEqual(journal["running_services"], sorted(running))
                return backup

            def inspect_attached_backup(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                self.assertEqual(journal["backup"], str(backup.path))

            with (
                mock.patch(
                    "remnawave_manager.update._running_component_services",
                    side_effect=[running, quiesced, quiesced, running, running],
                ),
                mock.patch(
                    "remnawave_manager.update.create_backup",
                    side_effect=create_after_stop,
                ),
                mock.patch("remnawave_manager.update.pull_verified", side_effect=fake_pull),
                mock.patch("remnawave_manager.update.validate_rendered_compose"),
                mock.patch(
                    "remnawave_manager.update.ensure_gzip",
                    side_effect=inspect_attached_backup,
                ),
                mock.patch("remnawave_manager.update.wait_container"),
                mock.patch("remnawave_manager.update.wait_panel_http"),
                mock.patch("remnawave_manager.update.check_subscription_http"),
                mock.patch("remnawave_manager.update.check_subscription_api_scopes"),
                mock.patch("remnawave_manager.update.test_nginx"),
                mock.patch("remnawave_manager.update.adopt"),
            ):
                self.assertEqual(update_panel_stack(runner, store), backup)

            self.assertFalse(journal_path.exists())

    def test_backup_failure_restores_exact_service_state_without_database_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, runner, store, inventory = adopt_fixture(
                temporary, "legacy_panel_2_8_1"
            )
            panel_service = inventory.components["panel"].service
            subscription_service = inventory.components["subscription"].service
            database_service = inventory.components["database"].service
            nginx_service = inventory.components["nginx"].service
            # Subscription and cache were intentionally stopped before update.
            running = {panel_service, database_service, nginx_service}
            quiesced = {database_service, nginx_service}
            journal_path = store.paths.state / "active-transaction.json"

            def fail_backup(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                self.assertEqual(journal["phase"], "creating-backup")
                self.assertNotIn("backup", journal)
                self.assertEqual(journal["running_services"], sorted(running))
                raise TransactionError("pg_dump failed")

            with (
                mock.patch(
                    "remnawave_manager.update._running_component_services",
                    side_effect=[running, quiesced, quiesced, running],
                ),
                mock.patch(
                    "remnawave_manager.update.create_backup",
                    side_effect=fail_backup,
                ),
                mock.patch("remnawave_manager.update.pull_verified", side_effect=fake_pull),
                mock.patch("remnawave_manager.update.validate_rendered_compose"),
                mock.patch("remnawave_manager.update.wait_container"),
                mock.patch("remnawave_manager.update.wait_panel_http"),
                mock.patch("remnawave_manager.update.restore_backup") as restore,
                self.assertRaisesRegex(TransactionError, "состояние сервисов восстановлено"),
            ):
                update_panel_stack(runner, store)

            restore.assert_not_called()
            self.assertFalse(journal_path.exists())
            starts = [
                command[-1]
                for command in runner.calls
                if command[:2] == ("docker", "compose") and "up" in command
            ]
            self.assertEqual(starts, [panel_service])
            self.assertNotIn(subscription_service, starts)

    def test_reopened_write_path_rejects_backup_before_configuration_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, runner, store, inventory = adopt_fixture(
                temporary, "legacy_panel_2_8_1"
            )
            backup = BackupResult(Path(temporary) / "pre-panel.tar.gz", {})
            running = {component.service for component in inventory.components.values()}
            applications = {
                inventory.components["panel"].service,
                inventory.components["subscription"].service,
            }
            quiesced = running - applications

            with (
                mock.patch(
                    "remnawave_manager.update._running_component_services",
                    side_effect=[running, quiesced, running, running, running],
                ),
                mock.patch(
                    "remnawave_manager.update.create_backup",
                    return_value=backup,
                ),
                mock.patch("remnawave_manager.update.pull_verified", side_effect=fake_pull),
                mock.patch("remnawave_manager.update.validate_rendered_compose"),
                mock.patch("remnawave_manager.update.ensure_gzip") as migrate_nginx,
                mock.patch("remnawave_manager.update.restore_backup") as restore,
                self.assertRaisesRegex(TransactionError, "снова запустился"),
            ):
                update_panel_stack(runner, store)

            migrate_nginx.assert_not_called()
            restore.assert_not_called()
            self.assertFalse((store.paths.state / "active-transaction.json").exists())

    def test_failed_panel_health_check_restores_every_legacy_file_and_database_dump(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install_dir, runner, store, inventory = adopt_fixture(
                temporary, "legacy_panel_2_8_1"
            )
            originals = managed_payloads(inventory)
            backup = BackupResult(Path(temporary) / "pre-panel.tar.gz", {})

            def restore(  # type: ignore[no-untyped-def]
                _runner,
                _store,
                _path,
                *,
                restore_database=True,
                clear_recovery_journal=True,
            ):
                self.assertTrue(restore_database)
                self.assertFalse(clear_recovery_journal)
                self.assertIn(TARGET_IMAGES["database"], (install_dir / "docker-compose.yml").read_text(encoding="utf-8"))
                restore_payloads(originals)

            with (
                mock.patch("remnawave_manager.update.create_backup", return_value=backup),
                mock.patch("remnawave_manager.update.pull_verified", side_effect=fake_pull),
                mock.patch("remnawave_manager.update.validate_rendered_compose"),
                mock.patch("remnawave_manager.update.wait_container"),
                mock.patch(
                    "remnawave_manager.update.wait_panel_http",
                    side_effect=TransactionError("panel health failure"),
                ),
                mock.patch("remnawave_manager.update.restore_backup", side_effect=restore) as rollback,
                self.assertRaises(TransactionError),
            ):
                update_panel_stack(runner, store)

            rollback.assert_called_once_with(
                runner,
                store,
                backup.path,
                restore_database=True,
                clear_recovery_journal=False,
            )
            self.assertEqual(managed_payloads(inventory), originals)


class LegacyNodeMigrationTests(unittest.TestCase):
    def test_node_secret_prefers_protected_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_path = Path(temporary) / ".env"
            env_path.write_text("SECRET_KEY='env-secret'\n", encoding="utf-8")
            inventory = Inventory(
                schema_version=1,
                role="node",
                install_dir=temporary,
                compose_file=str(Path(temporary) / "docker-compose.yml"),
                env_file=str(env_path),
                webserver="nginx",
                components={"node": Component("node", "remnanode", "remnanode")},
            )
            runner = mock.Mock()

            self.assertEqual(_node_secret(runner, inventory), "env-secret")
            runner.run.assert_not_called()

    def test_node_secret_falls_back_to_single_runtime_value(self) -> None:
        inventory = Inventory(
            schema_version=1,
            role="node",
            install_dir="/opt/remnanode",
            compose_file="/opt/remnanode/docker-compose.yml",
            env_file=None,
            webserver="nginx",
            components={"node": Component("node", "remnanode", "remnanode")},
        )
        runner = mock.Mock()
        runner.run.return_value = Result(
            ("docker", "inspect"),
            0,
            json.dumps(["NODE_PORT=2222", "SECRET_KEY=runtime-secret"]),
            "",
        )

        self.assertEqual(_node_secret(runner, inventory), "runtime-secret")
        self.assertTrue(runner.run.call_args.kwargs["sensitive"])

    def test_node_secret_rejects_malformed_duplicate_or_missing_runtime_values(
        self,
    ) -> None:
        inventory = Inventory(
            schema_version=1,
            role="node",
            install_dir="/opt/remnanode",
            compose_file="/opt/remnanode/docker-compose.yml",
            env_file=None,
            webserver="nginx",
            components={"node": Component("node", "remnanode", "remnanode")},
        )
        outputs = (
            "not-json",
            json.dumps(["SECRET_KEY=one", "SECRET_KEY=two"]),
            json.dumps(["NODE_PORT=2222"]),
            json.dumps(["SECRET_KEY="]),
        )
        for output in outputs:
            with self.subTest(output=output):
                runner = mock.Mock()
                runner.run.return_value = Result(
                    ("docker", "inspect"), 0, output, ""
                )
                with self.assertRaisesRegex(ValidationError, "SECRET_KEY"):
                    _node_secret(runner, inventory)

    def test_node_3_4_update_requires_panel_to_be_updated_first(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, runner, store, _ = adopt_fixture(temporary, "legacy_node_2_8_0")
            with (
                mock.patch("remnawave_manager.update.create_backup") as backup,
                mock.patch("remnawave_manager.update.pull_verified") as pull,
                self.assertRaisesRegex(ValidationError, "--panel-3-4-ready"),
            ):
                update_node(runner, store)

            backup.assert_not_called()
            pull.assert_not_called()

    def test_secret_preflight_failure_stops_before_compose_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _install_dir, runner, store, inventory = adopt_fixture(
                temporary, "legacy_node_2_8_0"
            )
            compose_path = Path(inventory.compose_file)
            original_compose = compose_path.read_bytes()
            backup = BackupResult(Path(temporary) / "pre-node.tar.gz", {})
            calls_before_update = len(runner.calls)

            with (
                mock.patch(
                    "remnawave_manager.update.create_backup", return_value=backup
                ),
                mock.patch(
                    "remnawave_manager.update.pull_verified", side_effect=fake_pull
                ),
                mock.patch(
                    "remnawave_manager.update.validate_node_secret",
                    side_effect=ValidationError("invalid SECRET_KEY"),
                ) as validate_secret,
                mock.patch("remnawave_manager.update._dump_node_config") as dump,
                mock.patch("remnawave_manager.update.restore_backup") as restore,
                self.assertRaisesRegex(ValidationError, "SECRET_KEY"),
            ):
                update_node(runner, store, panel_3_4_ready=True)

            validate_secret.assert_called_once_with(
                runner, TARGET_IMAGES["node"], NODE_SECRET_PAYLOAD
            )
            dump.assert_not_called()
            restore.assert_not_called()
            self.assertEqual(compose_path.read_bytes(), original_compose)
            update_calls = runner.calls[calls_before_update:]
            self.assertFalse(
                any(
                    command[:2] == ("docker", "compose") and "up" in command
                    for command in update_calls
                )
            )
            self.assertFalse((store.paths.state / "active-transaction.json").exists())

    def test_replacement_secret_is_checked_before_env_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install_dir, runner, store, inventory = adopt_fixture(
                temporary, "legacy_node_2_8_0"
            )
            env_path = install_dir / ".env"
            original_env = env_path.read_bytes()
            backup = BackupResult(Path(temporary) / "pre-node.tar.gz", {})

            with (
                mock.patch(
                    "remnawave_manager.update.create_backup", return_value=backup
                ),
                mock.patch(
                    "remnawave_manager.update.pull_verified", side_effect=fake_pull
                ),
                mock.patch(
                    "remnawave_manager.update.validate_node_secret",
                    side_effect=ValidationError("replacement SECRET_KEY rejected"),
                ) as validate_secret,
                self.assertRaisesRegex(ValidationError, "replacement SECRET_KEY"),
            ):
                update_node(
                    runner,
                    store,
                    panel_3_4_ready=True,
                    replacement_secret="replacement-node-secret",
                )

            validate_secret.assert_called_once_with(
                runner, TARGET_IMAGES["node"], "replacement-node-secret"
            )
            self.assertEqual(env_path.read_bytes(), original_env)
            self.assertFalse((store.paths.state / "active-transaction.json").exists())

    def test_replacement_secret_is_persisted_in_env_without_extra_quotes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install_dir, runner, store, _inventory = adopt_fixture(
                temporary, "legacy_node_2_8_0"
            )
            env_path = install_dir / ".env"
            backup = BackupResult(Path(temporary) / "pre-node.tar.gz", {})

            with (
                mock.patch("remnawave_manager.update.create_backup", return_value=backup),
                mock.patch("remnawave_manager.update.pull_verified", side_effect=fake_pull),
                mock.patch("remnawave_manager.update.validate_node_secret"),
                mock.patch("remnawave_manager.update.validate_rendered_compose"),
                mock.patch("remnawave_manager.update.wait_container"),
                mock.patch("remnawave_manager.update.wait_node_runtime"),
                mock.patch("remnawave_manager.update.wait_for_paths"),
                mock.patch("remnawave_manager.update.adopt"),
                mock.patch(
                    "remnawave_manager.update._existing_warp_interfaces",
                    return_value=set(),
                ),
            ):
                result = update_node(
                    runner,
                    store,
                    panel_3_4_ready=True,
                    replacement_secret="SECRET_KEY=replacement-node-secret",
                )

            self.assertEqual(result, backup)
            self.assertEqual(
                EnvDocument.load(env_path).effective_value("SECRET_KEY"),
                "replacement-node-secret",
            )
            self.assertIn(
                "SECRET_KEY=replacement-node-secret\n",
                env_path.read_text(encoding="utf-8"),
            )

    def test_failed_update_restores_env_after_secret_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _install_dir, runner, store, inventory = adopt_fixture(
                temporary, "legacy_node_2_8_0"
            )
            originals = managed_payloads(inventory)
            backup = BackupResult(Path(temporary) / "pre-node.tar.gz", {})

            def restore(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                restore_payloads(originals)

            with (
                mock.patch("remnawave_manager.update.create_backup", return_value=backup),
                mock.patch("remnawave_manager.update.pull_verified", side_effect=fake_pull),
                mock.patch("remnawave_manager.update.validate_node_secret"),
                mock.patch("remnawave_manager.update.validate_rendered_compose"),
                mock.patch("remnawave_manager.update.wait_container"),
                mock.patch(
                    "remnawave_manager.update.wait_node_runtime",
                    side_effect=TransactionError("node runtime failure"),
                ),
                mock.patch(
                    "remnawave_manager.update.restore_backup",
                    side_effect=restore,
                ) as rollback,
                mock.patch(
                    "remnawave_manager.update._existing_warp_interfaces",
                    return_value=set(),
                ),
                self.assertRaisesRegex(TransactionError, "предыдущая конфигурация восстановлена"),
            ):
                update_node(
                    runner,
                    store,
                    panel_3_4_ready=True,
                    replacement_secret="replacement-node-secret",
                )

            rollback.assert_called_once()
            self.assertEqual(managed_payloads(inventory), originals)

    def test_replacement_secret_updates_direct_compose_environment_without_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install_dir = copy_fixture(temporary, "legacy_node_2_8_0")
            compose_path = install_dir / "docker-compose.yml"
            compose_path.write_text(
                compose_path.read_text(encoding="utf-8").replace(
                    "    env_file: .env\n",
                    "    environment:\n"
                    "      - NODE_PORT=2222\n"
                    "      - SECRET_KEY=legacy.node.secret.must.stay.opaque\n",
                ),
                encoding="utf-8",
            )
            (install_dir / ".env").unlink()
            runner = LegacyRunner(install_dir, "legacy_node_2_8_0")
            store = StateStore(RuntimePaths(Path(temporary) / "runtime"))
            inventory = adopt(runner, store, directory=install_dir, requested_role="node")
            if os.name == "posix":
                for item in inventory.managed_files:
                    if item.kind in {"compose", "env", "nginx", "secret"}:
                        Path(item.path).chmod(0o600)
            backup = BackupResult(Path(temporary) / "pre-node.tar.gz", {})

            with (
                mock.patch("remnawave_manager.update.create_backup", return_value=backup),
                mock.patch("remnawave_manager.update.pull_verified", side_effect=fake_pull),
                mock.patch(
                    "remnawave_manager.update._node_secret",
                    return_value="legacy.node.secret.must.stay.opaque",
                ),
                mock.patch("remnawave_manager.update.validate_node_secret") as validate_secret,
                mock.patch("remnawave_manager.update.validate_rendered_compose"),
                mock.patch("remnawave_manager.update.wait_container"),
                mock.patch("remnawave_manager.update.wait_node_runtime"),
                mock.patch("remnawave_manager.update.wait_for_paths"),
                mock.patch("remnawave_manager.update.adopt"),
                mock.patch(
                    "remnawave_manager.update._existing_warp_interfaces",
                    return_value=set(),
                ),
            ):
                result = update_node(
                    runner,
                    store,
                    panel_3_4_ready=True,
                    replacement_secret="SECRET_KEY=replacement-node-secret",
                )

            self.assertEqual(result, backup)
            validate_secret.assert_called_once_with(
                runner, TARGET_IMAGES["node"], "replacement-node-secret"
            )
            self.assertIn(
                "      - SECRET_KEY=replacement-node-secret\n",
                compose_path.read_text(encoding="utf-8"),
            )

    def test_operator_edit_after_manager_write_blocks_automatic_restore(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, runner, store, inventory = adopt_fixture(
                temporary, "legacy_node_2_8_0"
            )
            compose_path = Path(inventory.compose_file)
            backup = BackupResult(Path(temporary) / "pre-node.tar.gz", {})
            operator_edit: str | None = None

            def edit_then_fail(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                nonlocal operator_edit
                operator_edit = (
                    compose_path.read_text(encoding="utf-8")
                    + "# operator edit after manager write\n"
                )
                compose_path.write_text(operator_edit, encoding="utf-8")
                raise TransactionError("node health failed")

            with (
                mock.patch("remnawave_manager.update.create_backup", return_value=backup),
                mock.patch("remnawave_manager.update.pull_verified", side_effect=fake_pull),
                mock.patch("remnawave_manager.update._validate_xray_image"),
                mock.patch("remnawave_manager.update.validate_rendered_compose"),
                mock.patch(
                    "remnawave_manager.update.wait_container",
                    side_effect=edit_then_fail,
                ),
                mock.patch("remnawave_manager.update.restore_backup") as restore,
                self.assertRaisesRegex(TransactionError, "внешнего изменения managed-файлов"),
            ):
                update_node(runner, store, panel_3_4_ready=True)

            restore.assert_not_called()
            self.assertIsNotNone(operator_edit)
            self.assertEqual(compose_path.read_text(encoding="utf-8"), operator_edit)
            self.assertTrue((store.paths.state / "active-transaction.json").exists())

    def test_operator_compose_edit_before_write_is_not_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, runner, store, inventory = adopt_fixture(
                temporary, "legacy_node_2_8_0"
            )
            compose_path = Path(inventory.compose_file)
            operator_edit = compose_path.read_text(encoding="utf-8") + "# operator edit\n"
            backup = BackupResult(Path(temporary) / "pre-node.tar.gz", {})

            def edit_during_validation(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                compose_path.write_text(operator_edit, encoding="utf-8")

            with (
                mock.patch("remnawave_manager.update.create_backup", return_value=backup),
                mock.patch("remnawave_manager.update.pull_verified", side_effect=fake_pull),
                mock.patch("remnawave_manager.update._validate_xray_image"),
                mock.patch(
                    "remnawave_manager.update.validate_rendered_compose",
                    side_effect=edit_during_validation,
                ),
                mock.patch("remnawave_manager.update.restore_backup") as restore,
                self.assertRaisesRegex(ValidationError, "изменился после загрузки"),
            ):
                update_node(runner, store, panel_3_4_ready=True)

            restore.assert_not_called()
            self.assertEqual(compose_path.read_text(encoding="utf-8"), operator_edit)
            self.assertFalse((store.paths.state / "active-transaction.json").exists())

    def test_node_preflight_cleanup_failure_stops_before_compose_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _install_dir, runner, store, inventory = adopt_fixture(
                temporary, "legacy_node_2_8_0"
            )
            compose_path = Path(inventory.compose_file)
            original_compose = compose_path.read_bytes()
            backup = BackupResult(Path(temporary) / "pre-node.tar.gz", {})
            original_unlink = Path.unlink

            def reject_xray_cleanup(path: Path, missing_ok: bool = False) -> None:
                if path.name.startswith("rwm-xray-"):
                    raise OSError("cleanup denied")
                original_unlink(path, missing_ok=missing_ok)

            with (
                mock.patch("remnawave_manager.update.create_backup", return_value=backup),
                mock.patch("remnawave_manager.update.pull_verified", side_effect=fake_pull),
                mock.patch("remnawave_manager.update._validate_xray_image"),
                mock.patch.object(Path, "unlink", new=reject_xray_cleanup),
                self.assertRaisesRegex(TransactionError, "временный Xray-конфиг"),
            ):
                update_node(runner, store, panel_3_4_ready=True)

            self.assertEqual(compose_path.read_bytes(), original_compose)
            self.assertFalse((store.paths.state / "active-transaction.json").exists())

    def test_blank_or_null_reality_minimum_version_requires_explicit_acceptance(self) -> None:
        config = {
            "inbounds": [
                {
                    "tag": "blank",
                    "streamSettings": {
                        "security": "reality",
                        "realitySettings": {"minClientVer": "  "},
                    },
                },
                {
                    "tag": "null",
                    "streamSettings": {
                        "security": "reality",
                        "realitySettings": {"minClientVer": None},
                    },
                },
                {
                    "tag": "explicit",
                    "streamSettings": {
                        "security": "reality",
                        "realitySettings": {"minClientVer": "0.0.0"},
                    },
                },
            ]
        }

        self.assertEqual(_reality_without_min_version(config), ["blank", "null"])

    def test_node_config_dump_must_be_a_json_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install_dir, runner, _, inventory = adopt_fixture(
                temporary, "legacy_node_2_8_0"
            )
            (install_dir / "xray.json").write_text("[]", encoding="utf-8")

            with self.assertRaisesRegex(ValidationError, "не является объектом"):
                _dump_node_config(runner, inventory)

    def test_node_config_dump_is_size_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install_dir, runner, _, inventory = adopt_fixture(
                temporary, "legacy_node_2_8_0"
            )
            (install_dir / "xray.json").write_text(
                '{"inbounds": []}',
                encoding="utf-8",
            )

            with (
                mock.patch("remnawave_manager.update._MAX_NODE_CONFIG_SIZE", 4),
                self.assertRaisesRegex(ValidationError, "превышает"),
            ):
                _dump_node_config(runner, inventory)

    def test_partial_node_config_dump_is_removed_on_fsync_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, runner, _, inventory = adopt_fixture(
                temporary, "legacy_node_2_8_0"
            )
            real_mkstemp = tempfile.mkstemp

            def local_mkstemp(*args, **kwargs):  # type: ignore[no-untyped-def]
                kwargs["dir"] = temporary
                return real_mkstemp(*args, **kwargs)

            with (
                mock.patch(
                    "remnawave_manager.update.tempfile.mkstemp",
                    side_effect=local_mkstemp,
                ),
                mock.patch(
                    "remnawave_manager.update.os.fsync",
                    side_effect=OSError("fsync failed"),
                ),
                self.assertRaisesRegex(OSError, "fsync failed"),
            ):
                _dump_node_config(runner, inventory)

            self.assertEqual(list(Path(temporary).glob("rwm-xray-*.json")), [])

    def test_node_update_preserves_warp_xhttp_yandex_and_all_opaque_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install_dir, runner, store, inventory = adopt_fixture(
                temporary, "legacy_node_2_8_0"
            )
            original_compose = (install_dir / "docker-compose.yml").read_text(encoding="utf-8")
            opaque_before = {
                path: payload
                for path, payload in managed_payloads(inventory).items()
                if path != Path(inventory.compose_file)
            }
            backup = BackupResult(Path(temporary) / "pre-node.tar.gz", {})
            calls_before_update = len(runner.calls)

            with (
                mock.patch("remnawave_manager.update.create_backup", return_value=backup),
                mock.patch("remnawave_manager.update.pull_verified", side_effect=fake_pull),
                mock.patch("remnawave_manager.update.validate_rendered_compose"),
                mock.patch("remnawave_manager.update.wait_container"),
                mock.patch("remnawave_manager.update.wait_node_runtime"),
                mock.patch("remnawave_manager.update.wait_for_paths") as wait_paths,
                mock.patch("remnawave_manager.update.adopt"),
                mock.patch(
                    "remnawave_manager.update._existing_paths",
                    return_value=list(inventory.xhttp_sockets),
                ),
                mock.patch(
                    "remnawave_manager.update._existing_warp_interfaces",
                    side_effect=[{"warp"}, {"warp"}],
                ) as warp_interfaces,
            ):
                result = update_node(runner, store, panel_3_4_ready=True)

            self.assertEqual(result, backup)
            updated_compose = (install_dir / "docker-compose.yml").read_text(encoding="utf-8")
            self.assertEqual(
                updated_compose.replace(TARGET_IMAGES["node"], "remnawave/node:latest"),
                original_compose,
            )
            self.assertEqual(
                {path: path.read_bytes() for path in opaque_before}, opaque_before
            )
            wait_paths.assert_called_once_with(
                ["/dev/shm/nginx.sock", "/dev/shm/xray-xhttp.sock"]
            )
            self.assertEqual(warp_interfaces.call_count, 2)

            update_calls = runner.calls[calls_before_update:]
            recreate = [
                command
                for command in update_calls
                if command[:2] == ("docker", "compose") and "up" in command
            ]
            self.assertEqual(len(recreate), 1)
            self.assertEqual(recreate[0][-1], inventory.components["node"].service)
            secret_preflight = next(
                command
                for command in update_calls
                if command[:2] == ("docker", "run") and "node" in command
            )
            self.assertIn(TARGET_IMAGES["node"], secret_preflight)
            self.assertIn("--read-only", secret_preflight)
            self.assertEqual(
                secret_preflight[secret_preflight.index("--network") + 1], "none"
            )
            preflight = next(
                command
                for command in update_calls
                if command[:2] == ("docker", "run") and "rw-core" in command
            )
            self.assertIn(TARGET_IMAGES["node"], preflight)
            self.assertIn("rw-core", preflight)
            self.assertIn("--read-only", preflight)
            self.assertIn("--network", preflight)
            self.assertEqual(preflight[preflight.index("--network") + 1], "none")
            self.assertIn("--cap-drop", preflight)
            self.assertEqual(preflight[preflight.index("--cap-drop") + 1], "ALL")
            self.assertIn("--volumes-from", preflight)
            self.assertEqual(
                preflight[preflight.index("--volumes-from") + 1],
                "remnanode:ro",
            )
            config_mount = preflight[preflight.index("--volume") + 1]
            self.assertTrue(config_mount.endswith(":/tmp/config.json:ro"))

    def test_failed_node_runtime_check_restores_compose_and_opaque_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            install_dir, runner, store, inventory = adopt_fixture(
                temporary, "legacy_node_2_8_0"
            )
            originals = managed_payloads(inventory)
            backup = BackupResult(Path(temporary) / "pre-node.tar.gz", {})

            def restore(  # type: ignore[no-untyped-def]
                _runner,
                _store,
                _path,
                *,
                restore_database=True,
                clear_recovery_journal=True,
            ):
                self.assertFalse(restore_database)
                self.assertFalse(clear_recovery_journal)
                journal = json.loads(
                    (store.paths.state / "active-transaction.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertIn("running_services", journal)
                self.assertIn(TARGET_IMAGES["node"], (install_dir / "docker-compose.yml").read_text(encoding="utf-8"))
                restore_payloads(originals)

            with (
                mock.patch("remnawave_manager.update.create_backup", return_value=backup),
                mock.patch("remnawave_manager.update.pull_verified", side_effect=fake_pull),
                mock.patch("remnawave_manager.update.validate_rendered_compose"),
                mock.patch("remnawave_manager.update.wait_container"),
                mock.patch(
                    "remnawave_manager.update.wait_node_runtime",
                    side_effect=TransactionError("node runtime failure"),
                ),
                mock.patch("remnawave_manager.update.restore_backup", side_effect=restore) as rollback,
                mock.patch(
                    "remnawave_manager.update._existing_paths",
                    return_value=list(inventory.xhttp_sockets),
                ),
                mock.patch(
                    "remnawave_manager.update._existing_warp_interfaces",
                    return_value={"warp"},
                ),
                self.assertRaises(TransactionError),
            ):
                update_node(runner, store, panel_3_4_ready=True)

            rollback.assert_called_once_with(
                runner,
                store,
                backup.path,
                restore_database=False,
                clear_recovery_journal=False,
            )
            self.assertEqual(managed_payloads(inventory), originals)

    def test_journal_phase_failure_does_not_skip_node_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, runner, store, inventory = adopt_fixture(
                temporary, "legacy_node_2_8_0"
            )
            originals = managed_payloads(inventory)
            backup = BackupResult(Path(temporary) / "pre-node.tar.gz", {})
            original_phase = TransactionJournal.phase

            def fail_rollback_phase(journal, value):  # type: ignore[no-untyped-def]
                if value == "rolling-back":
                    raise OSError("journal filesystem unavailable")
                return original_phase(journal, value)

            def restore(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                restore_payloads(originals)

            with (
                mock.patch("remnawave_manager.update.create_backup", return_value=backup),
                mock.patch("remnawave_manager.update.pull_verified", side_effect=fake_pull),
                mock.patch("remnawave_manager.update.validate_rendered_compose"),
                mock.patch("remnawave_manager.update.wait_container"),
                mock.patch(
                    "remnawave_manager.update.wait_node_runtime",
                    side_effect=TransactionError("node runtime failure"),
                ),
                mock.patch(
                    "remnawave_manager.update.TransactionJournal.phase",
                    autospec=True,
                    side_effect=fail_rollback_phase,
                ),
                mock.patch(
                    "remnawave_manager.update.restore_backup",
                    side_effect=restore,
                ) as rollback,
                mock.patch(
                    "remnawave_manager.update._existing_paths",
                    return_value=list(inventory.xhttp_sockets),
                ),
                mock.patch(
                    "remnawave_manager.update._existing_warp_interfaces",
                    return_value={"warp"},
                ),
                self.assertRaisesRegex(TransactionError, "обновление journal"),
            ):
                update_node(runner, store, panel_3_4_ready=True)

            rollback.assert_called_once_with(
                runner,
                store,
                backup.path,
                restore_database=False,
                clear_recovery_journal=False,
            )
            self.assertEqual(managed_payloads(inventory), originals)
            self.assertTrue((store.paths.state / "active-transaction.json").exists())


class RollbackServiceStateTests(unittest.TestCase):
    def test_reconcile_restarts_services_stopped_by_failed_update_in_dependency_order(self) -> None:
        current = Inventory(
            schema_version=1,
            role="panel",
            install_dir="/opt/remnawave",
            compose_file="/opt/remnawave/docker-compose.yml",
            env_file="/opt/remnawave/.env",
            webserver="nginx",
            components={
                "database": Component("database", "db"),
                "cache": Component("cache", "cache"),
                "panel": Component("panel", "panel"),
                "subscription": Component("subscription", "subscription"),
                "nginx": Component("nginx", "nginx"),
            },
        )
        running = {"db", "cache", "nginx"}
        expected = {"db", "cache", "panel", "subscription", "nginx"}
        commands: list[tuple[str, ...]] = []

        def run(args, **kwargs):  # type: ignore[no-untyped-def]
            command = tuple(args)
            commands.append(command)
            if "ps" in command:
                return Result(command, 0, "\n".join(sorted(running)) + "\n", "")
            if "up" in command:
                running.add(command[-1])
            elif "stop" in command:
                running.difference_update(command[command.index("stop") + 1 :])
            return Result(command, 0, "", "")

        runner = mock.Mock()
        runner.run.side_effect = run
        with (
            mock.patch("remnawave_manager.update.wait_container"),
            mock.patch("remnawave_manager.update.wait_panel_http"),
            mock.patch(
                "remnawave_manager.update.check_subscription_http"
            ) as subscription_health,
        ):
            _reconcile_running_services(
                runner, current, expected, legacy_subscription=True
            )

        starts = [command[-1] for command in commands if "up" in command]
        self.assertEqual(starts, ["panel", "subscription"])
        self.assertEqual(running, expected)
        subscription_health.assert_called_once_with(
            runner, current.components["subscription"], legacy=True
        )


if __name__ == "__main__":
    unittest.main()
