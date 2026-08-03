from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remnawave_manager.errors import TransactionError, ValidationError
from remnawave_manager.models import Component, Inventory
from remnawave_manager.nginx import (
    activate_nginx_config,
    ensure_gzip,
    prepare_nginx_config,
    reload_nginx,
)
from remnawave_manager.nginx import (
    test_nginx as validate_nginx,
)
from remnawave_manager.runner import Result, Runner


def _inventory(config: Path) -> Inventory:
    return Inventory(
        schema_version=1,
        role="panel",
        install_dir=str(config.parent),
        compose_file=str(config.parent / "docker-compose.yml"),
        env_file=None,
        webserver="nginx",
        nginx_files=[str(config)],
    )


def _inventory_many(*configs: Path) -> Inventory:
    inventory = _inventory(configs[0])
    inventory.nginx_files = [str(config) for config in configs]
    return inventory


def _container_inventory(config: Path) -> Inventory:
    inventory = _inventory(config)
    inventory.env_file = str(config.parent / ".env")
    inventory.components = {
        "nginx": Component("nginx", "proxy", container=None),
    }
    return inventory


class NginxMigrationTests(unittest.TestCase):
    def test_legacy_panel_and_subscription_proxy_contract_is_hardened(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "nginx.conf"
            unrelated = (
                "server {\n"
                "    server_name xhttp.example.test;\n"
                "    location /xhttp {\n"
                "        proxy_pass http://xhttp_backend;\n"
                "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
                "        proxy_read_timeout 60s;\n"
                "    }\n"
                "}\n"
            )
            config.write_text(
                "upstream remnawave { server 127.0.0.1:3000; }\n"
                "upstream json { server 127.0.0.1:3010; }\n"
                "server {\n"
                "    server_name panel.example.test;\n"
                "    location / {\n"
                "        proxy_pass http://remnawave;\n"
                "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
                "        proxy_read_timeout 60s;\n"
                "        proxy_send_timeout 60s;\n"
                "    }\n"
                "}\n"
                "server {\n"
                "    server_name sub.example.test;\n"
                "    location / {\n"
                "        proxy_pass http://json;\n"
                "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
                "        proxy_read_timeout 60s;\n"
                "        proxy_send_timeout 60s;\n"
                "    }\n"
                "}\n"
                + unrelated,
                encoding="utf-8",
            )
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = Result((), 0, "", "")

            self.assertEqual(ensure_gzip(runner, _inventory(config)), config)

            rendered = config.read_text(encoding="utf-8")
            self.assertEqual(rendered.count("proxy_read_timeout 240s;"), 2)
            self.assertEqual(rendered.count("proxy_send_timeout 240s;"), 2)
            self.assertEqual(rendered.count("X-Forwarded-For $remote_addr;"), 2)
            self.assertEqual(rendered.count("access_log off;"), 1)
            self.assertIn(unrelated, rendered)

    def test_proxy_protocol_panel_uses_authenticated_source_address(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "nginx.conf"
            config.write_text(
                "server {\n"
                "    listen unix:/dev/shm/nginx.sock ssl proxy_protocol;\n"
                "    location / {\n"
                "        proxy_pass http://remnawave;\n"
                "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
                "    }\n"
                "}\n",
                encoding="utf-8",
            )
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = Result((), 0, "", "")

            ensure_gzip(runner, _inventory(config))

            rendered = config.read_text(encoding="utf-8")
            self.assertIn("X-Forwarded-For $proxy_protocol_addr;", rendered)
            self.assertNotIn("$proxy_add_x_forwarded_for", rendered)

    def test_separate_subscription_file_is_migrated_without_overriding_custom_log(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            panel = root / "panel.conf"
            subscription = root / "subscription.conf"
            panel.write_text(
                "server { location / { proxy_pass http://127.0.0.1:3000; "
                "proxy_read_timeout 60s; } }\n",
                encoding="utf-8",
            )
            subscription.write_text(
                "server { access_log /var/log/nginx/subscription.audit; "
                "location / { proxy_pass http://127.0.0.1:3010; "
                "proxy_send_timeout 60s; } }\n",
                encoding="utf-8",
            )
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = Result((), 0, "", "")

            self.assertEqual(
                ensure_gzip(runner, _inventory_many(panel, subscription)),
                panel,
            )

            self.assertIn("proxy_read_timeout 240s", panel.read_text(encoding="utf-8"))
            rendered_subscription = subscription.read_text(encoding="utf-8")
            self.assertIn("proxy_send_timeout 240s", rendered_subscription)
            self.assertIn(
                "access_log /var/log/nginx/subscription.audit;",
                rendered_subscription,
            )
            self.assertNotIn("access_log off;", rendered_subscription)
            self.assertEqual(runner.run.call_count, 3)

    def test_gzip_in_unrelated_discovered_file_does_not_skip_panel_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unrelated = root / "00-unrelated.conf"
            panel = root / "panel.conf"
            unrelated.write_text("gzip on;\nserver { server_name unrelated.test; }\n", encoding="utf-8")
            panel.write_text(
                "server { proxy_pass http://127.0.0.1:3000; }\n",
                encoding="utf-8",
            )
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = Result((), 0, "", "")

            changed = ensure_gzip(runner, _inventory_many(unrelated, panel))

            self.assertEqual(changed, panel)
            self.assertIn("gzip on;", panel.read_text(encoding="utf-8"))

    def test_gzip_inside_unrelated_server_in_same_file_is_not_global(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "nginx.conf"
            config.write_text(
                "events {}\n"
                "http {\n"
                "    server { server_name unrelated.test; gzip on; }\n"
                "    server { proxy_pass http://127.0.0.1:3000; }\n"
                "}\n",
                encoding="utf-8",
            )
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = Result((), 0, "", "")

            changed = ensure_gzip(runner, _inventory(config))

            self.assertEqual(changed, config)
            self.assertEqual(config.read_text(encoding="utf-8").count("gzip on;"), 2)

    def test_existing_http_level_gzip_gets_required_types(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "nginx.conf"
            original = (
                "events {}\n"
                "http { gzip on; server { proxy_pass http://127.0.0.1:3000; } }\n"
            )
            config.write_text(original, encoding="utf-8")
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = Result((), 0, "", "")

            self.assertEqual(ensure_gzip(runner, _inventory(config)), config)
            rendered = config.read_text(encoding="utf-8")
            self.assertIn("gzip_types", rendered)
            self.assertIn("application/json", rendered)
            self.assertEqual(rendered.count("gzip on;"), 1)

    def test_partial_http_level_gzip_types_are_extended_without_duplicate_directive(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "nginx.conf"
            config.write_text(
                "events {}\n"
                "http { gzip on; gzip_types text/plain application/json;\n"
                "server { proxy_pass http://127.0.0.1:3000; } }\n",
                encoding="utf-8",
            )
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = Result((), 0, "", "")

            ensure_gzip(runner, _inventory(config))

            rendered = config.read_text(encoding="utf-8")
            self.assertEqual(rendered.count("gzip_types"), 1)
            self.assertIn("application/javascript", rendered)
            self.assertIn("text/plain application/json;", rendered)

    def test_complete_http_level_gzip_configuration_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "nginx.conf"
            types = (
                "application/javascript application/json application/manifest+json "
                "application/wasm application/xml font/eot font/opentype font/otf "
                "font/ttf image/svg+xml text/css text/javascript text/plain text/xml"
            )
            original = f"events {{}}\nhttp {{ gzip on; gzip_types {types}; }}\n"
            config.write_text(original, encoding="utf-8")
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = Result((), 0, "", "")

            self.assertIsNone(ensure_gzip(runner, _inventory(config)))
            self.assertEqual(config.read_text(encoding="utf-8"), original)
            runner.run.assert_not_called()

    def test_main_nginx_config_gets_gzip_inside_http_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "nginx.conf"
            config.write_text(
                "user nginx;\n"
                "events {}\n"
                "http {\n"
                "    server { proxy_pass http://127.0.0.1:3000; }\n"
                "}\n",
                encoding="utf-8",
            )
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = Result((), 0, "", "")

            changed = ensure_gzip(runner, _inventory(config))

            self.assertEqual(changed, config)
            rendered = config.read_text(encoding="utf-8")
            self.assertTrue(rendered.startswith("user nginx;\n"))
            self.assertLess(rendered.index("http {"), rendered.index("gzip on;"))
            self.assertLess(rendered.index("gzip on;"), rendered.rindex("}"))

    def test_http_include_config_keeps_top_level_gzip_block(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "panel.conf"
            config.write_text(
                "server { proxy_pass http://127.0.0.1:3000; }\n",
                encoding="utf-8",
            )
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = Result((), 0, "", "")

            ensure_gzip(runner, _inventory(config))

            self.assertTrue(
                config.read_text(encoding="utf-8").startswith(
                    "# BEGIN REMNAWAVE-MANAGER GZIP\n"
                )
            )

    def test_http_text_in_comment_is_not_treated_as_main_context(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "panel.conf"
            config.write_text(
                "# Example only: http {\n"
                "server { proxy_pass http://127.0.0.1:3000; }\n",
                encoding="utf-8",
            )
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = Result((), 0, "", "")

            ensure_gzip(runner, _inventory(config))

            rendered = config.read_text(encoding="utf-8")
            self.assertTrue(rendered.startswith("# BEGIN REMNAWAVE-MANAGER GZIP\n"))
            self.assertIn("# Example only: http {", rendered)

    def test_hardlinked_config_is_rejected_without_modification(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = root / "panel.conf"
            linked = root / "panel-linked.conf"
            original = "server { proxy_pass http://127.0.0.1:3000; }\n"
            config.write_text(original, encoding="utf-8")
            linked.hardlink_to(config)
            runner = mock.Mock(spec=Runner)

            with self.assertRaisesRegex(ValidationError, "hardlink"):
                ensure_gzip(runner, _inventory(config))

            self.assertEqual(config.read_text(encoding="utf-8"), original)
            self.assertEqual(linked.read_text(encoding="utf-8"), original)
            runner.run.assert_not_called()

    def test_parallel_edit_during_state_probe_is_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "panel.conf"
            original = "server { proxy_pass http://127.0.0.1:3000; }\n"
            manual = "# edited externally\n" + original
            config.write_text(original, encoding="utf-8")
            runner = mock.Mock(spec=Runner)
            before_write = mock.Mock()

            def edit_config(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                config.write_text(manual, encoding="utf-8")
                return True

            with (
                mock.patch(
                    "remnawave_manager.nginx.nginx_is_running",
                    side_effect=edit_config,
                ),
                self.assertRaisesRegex(
                    ValidationError,
                    "изменилась параллельно",
                ),
            ):
                ensure_gzip(
                    runner,
                    _inventory(config),
                    before_write=before_write,
                )

            self.assertEqual(config.read_text(encoding="utf-8"), manual)
            runner.run.assert_not_called()
            before_write.assert_not_called()

    def test_reload_failure_restores_file_and_reloads_original(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "panel.conf"
            original = "server { proxy_pass http://127.0.0.1:3000; }\n"
            config.write_text(original, encoding="utf-8")
            runner = mock.Mock(spec=Runner)
            reload_attempts = 0

            def run(command, **_kwargs):  # type: ignore[no-untyped-def]
                nonlocal reload_attempts
                if command == ["systemctl", "reload", "nginx"]:
                    reload_attempts += 1
                    if reload_attempts == 1:
                        raise OSError("reload failed")
                return Result(tuple(command), 0, "", "")

            runner.run.side_effect = run

            with self.assertRaisesRegex(TransactionError, "исходная конфигурация восстановлена"):
                ensure_gzip(runner, _inventory(config))

            self.assertEqual(config.read_text(encoding="utf-8"), original)
            self.assertEqual(runner.run.call_count, 5)

    def test_stopped_system_nginx_is_validated_without_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "panel.conf"
            inventory = _inventory(config)
            runner = mock.Mock(spec=Runner)
            runner.run.side_effect = [
                Result((), 3, "", ""),
                Result((), 0, "", ""),
            ]

            activate_nginx_config(runner, inventory)

            self.assertEqual(
                runner.run.call_args_list[0].args[0],
                ["systemctl", "is-active", "--quiet", "nginx"],
            )
            self.assertEqual(runner.run.call_args_list[1].args[0], ["nginx", "-t"])
            self.assertEqual(runner.run.call_count, 2)

    def test_container_activation_recreates_before_testing_loaded_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "nginx.conf"
            inventory = _container_inventory(config)
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = Result((), 0, "proxy\n", "")

            activate_nginx_config(runner, inventory)

            state_check = runner.run.call_args_list[0]
            recreate = runner.run.call_args_list[1]
            tested = runner.run.call_args_list[2]
            self.assertIn("ps", state_check.args[0])
            self.assertEqual(recreate.args[0][:3], ["docker", "compose", "--env-file"])
            self.assertIn("--force-recreate", recreate.args[0])
            self.assertIn("--pull", recreate.args[0])
            self.assertIn("never", recreate.args[0])
            self.assertEqual(recreate.args[0][-1], "proxy")
            self.assertEqual(tested.args[0][-5:], ["exec", "-T", "proxy", "nginx", "-t"])
            self.assertEqual(recreate.kwargs["cwd"], config.parent)
            self.assertFalse(tested.kwargs["check"])

    def test_container_activation_does_not_start_previously_stopped_nginx(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "nginx.conf"
            inventory = _container_inventory(config)
            runner = mock.Mock(spec=Runner)
            runner.run.side_effect = [
                Result((), 0, "", ""),
                Result((), 0, "", ""),
                Result((), 0, "", ""),
                Result((), 0, "", ""),
            ]

            activate_nginx_config(runner, inventory)

            commands = [call.args[0] for call in runner.run.call_args_list]
            self.assertIn("ps", commands[0])
            self.assertIn("create", commands[1])
            self.assertEqual(commands[2][-5:], ["--pull", "never", "proxy", "nginx", "-t"])
            self.assertFalse(any("up" in command for command in commands))
            self.assertFalse(any("stop" in command for command in commands))

    def test_stopped_container_prepare_uses_compose_create_without_starting_nginx(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "nginx.conf"
            inventory = _container_inventory(config)
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = Result((), 0, "", "")

            prepare_nginx_config(runner, inventory)

            create = runner.run.call_args_list[0].args[0]
            validate = runner.run.call_args_list[1].args[0]
            self.assertIn("create", create)
            self.assertIn("--force-recreate", create)
            self.assertIn("--pull", create)
            self.assertIn("never", create)
            self.assertEqual(create[-1], "proxy")
            self.assertNotIn("-d", create)
            self.assertIn("run", validate)
            self.assertIn("--rm", validate)
            self.assertIn("--no-deps", validate)
            self.assertEqual(validate[-3:], ["proxy", "nginx", "-t"])
            self.assertFalse(runner.run.call_args_list[1].kwargs["check"])

    def test_container_gzip_failure_restores_file_and_recreates_old_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "nginx.conf"
            original = "server { proxy_pass http://127.0.0.1:3000; }\n"
            config.write_text(original, encoding="utf-8")
            runner = mock.Mock(spec=Runner)
            runner.run.side_effect = [
                Result((), 0, "proxy\n", ""),
                Result((), 0, "", ""),
                Result((), 1, "", "invalid config"),
                Result((), 0, "", ""),
                Result((), 0, "", ""),
            ]

            with self.assertRaisesRegex(TransactionError, "nginx -t"):
                ensure_gzip(runner, _container_inventory(config))

            self.assertEqual(config.read_text(encoding="utf-8"), original)
            recreate_calls = [
                call
                for call in runner.run.call_args_list
                if "--force-recreate" in call.args[0]
            ]
            self.assertEqual(len(recreate_calls), 2)
            state_checks = [
                call
                for call in runner.run.call_args_list
                if "ps" in call.args[0]
            ]
            self.assertEqual(len(state_checks), 1)

    def test_container_test_and_reload_address_compose_service(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "nginx.conf"
            inventory = _container_inventory(config)
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = Result((), 0, "", "")

            validate_nginx(runner, inventory)
            reload_nginx(runner, inventory)

            self.assertEqual(
                runner.run.call_args_list[0].args[0][-5:],
                ["exec", "-T", "proxy", "nginx", "-t"],
            )
            self.assertEqual(
                runner.run.call_args_list[1].args[0][-6:],
                ["exec", "-T", "proxy", "nginx", "-s", "reload"],
            )


if __name__ == "__main__":
    unittest.main()
