from __future__ import annotations

import getpass
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from remnawave_manager.api import ProvisionedReality
from remnawave_manager.backup import BackupResult
from remnawave_manager.backup_schedule import BackupSchedule
from remnawave_manager.certificates import CertbotRenewalPlan, IssuedCertificate
from remnawave_manager.cli import build_parser, main
from remnawave_manager.errors import NodeSecretValidationError, ValidationError
from remnawave_manager.host import HostStatus, OperatingSystemUpdate
from remnawave_manager.install import NodeInstallResult
from remnawave_manager.models import Component, Inventory
from remnawave_manager.paths import RuntimePaths
from remnawave_manager.runner import Result
from remnawave_manager.security import EmergencyAccess


def inventory(role: str = "node") -> Inventory:
    return Inventory(
        schema_version=1,
        role=role,  # type: ignore[arg-type]
        install_dir="/opt/remnanode" if role == "node" else "/opt/remnawave",
        compose_file="/opt/remnanode/docker-compose.yml",
        env_file="/opt/remnanode/.env",
        webserver="nginx",
    )


class CliParserTests(unittest.TestCase):
    def test_help_is_russian_and_secret_flags_are_not_exposed(self) -> None:
        help_text = build_parser().format_help()

        self.assertIn("Показать эту справку", help_text)
        self.assertIn("Безопасное управление", help_text)
        self.assertNotIn("show this help", help_text)
        self.assertNotIn("--password", help_text)
        self.assertNotIn("--api-token", help_text)
        self.assertNotIn("--secret-key", help_text)
        self.assertNotIn("--license-key", help_text)

    def test_parser_error_sanitizes_terminal_control_characters(self) -> None:
        output = io.StringIO()
        with redirect_stderr(output), self.assertRaises(SystemExit):
            build_parser().error("bad\x1b[31m\rspoof\u202ehidden")

        text = output.getvalue()
        self.assertIn("bad [31m spoof hidden", text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\r", text)
        self.assertNotIn("\u202e", text)

    def test_node_install_parser_requires_site_source_or_template(self) -> None:
        parser = build_parser()
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(
                [
                    "install",
                    "node",
                    "--domain",
                    "node.example.com",
                    "--panel-ip",
                    "192.0.2.10",
                    "--panel-3-3-ready",
                    "--certificate-method",
                    "http-01",
                    "--email",
                    "admin@example.com",
                ]
            )

    def test_service_logs_parser_validates_options(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "service",
                "logs",
                "node",
                "--tail",
                "10000",
                "--follow",
                "--since",
                "1h30m",
            ]
        )

        self.assertEqual(args.handler, "service-logs")
        self.assertEqual(args.component, "node")
        self.assertEqual(args.tail, 10_000)
        self.assertTrue(args.follow)
        self.assertEqual(args.since, "1h30m")

        for arguments in (
            ["service", "logs", "node", "--tail", "0"],
            ["service", "logs", "node", "--tail", "10001"],
            ["service", "logs", "node", "--since", "1h\n--follow"],
        ):
            with (
                self.subTest(arguments=arguments),
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit),
            ):
                parser.parse_args(arguments)

    def test_panel_cli_parser(self) -> None:
        args = build_parser().parse_args(["service", "panel-cli"])

        self.assertEqual(args.handler, "service-panel-cli")

    def test_certificate_repair_renewal_parser(self) -> None:
        args = build_parser().parse_args(["certificate", "repair-renewal", "--yes"])

        self.assertEqual(args.handler, "certificate-repair-renewal")
        self.assertTrue(args.yes)

    def test_certificate_issue_parser_supports_gcore_wildcard(self) -> None:
        args = build_parser().parse_args(
            [
                "certificate",
                "issue",
                "--domain",
                "example.com",
                "--method",
                "gcore",
                "--email",
                "admin@example.com",
                "--wildcard",
                "--yes",
            ]
        )

        self.assertEqual(args.handler, "certificate-issue")
        self.assertEqual(args.method, "gcore")
        self.assertTrue(args.wildcard)
        self.assertTrue(args.yes)

    def test_system_and_maintenance_parsers(self) -> None:
        system = build_parser().parse_args(["system", "status"])
        system_update = build_parser().parse_args(["system", "update", "--yes"])
        maintenance = build_parser().parse_args(
            ["maintenance", "archive-stack", "--yes"]
        )

        self.assertEqual(system.handler, "system-status")
        self.assertEqual(system_update.handler, "system-update")
        self.assertTrue(system_update.yes)
        self.assertEqual(maintenance.handler, "maintenance-archive-stack")
        self.assertTrue(maintenance.yes)

        manager = build_parser().parse_args(["manager", "update", "--yes"])
        self.assertEqual(manager.handler, "manager-update")
        self.assertTrue(manager.yes)

    def test_backup_schedule_and_emergency_access_parsers(self) -> None:
        schedule = build_parser().parse_args(
            [
                "backup",
                "schedule-enable",
                "--frequency",
                "weekly",
                "--time",
                "03:15",
                "--retention",
                "12",
                "--yes",
            ]
        )
        emergency = build_parser().parse_args(
            ["security", "emergency-open", "--minutes", "45", "--yes"]
        )

        self.assertEqual(schedule.handler, "backup-schedule-enable")
        self.assertEqual(schedule.time_of_day, "03:15")
        self.assertEqual(schedule.retention, 12)
        self.assertEqual(emergency.handler, "security-emergency-open")
        self.assertEqual(emergency.minutes, 45)

        delete = build_parser().parse_args(
            ["backup", "delete", "first.tar.gz", "second.tar.gz", "--yes"]
        )
        self.assertEqual(delete.handler, "backup-delete")
        self.assertEqual(delete.paths, [Path("first.tar.gz"), Path("second.tar.gz")])
        self.assertTrue(delete.yes)


class CliDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        ubuntu = mock.patch("remnawave_manager.cli.require_ubuntu_2404")
        ubuntu.start()
        self.addCleanup(ubuntu.stop)
        self.paths = RuntimePaths(Path(self.temporary.name))
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def run_main(
        self,
        arguments: list[str],
        *,
        answers: list[str] | None = None,
        secrets: list[str] | None = None,
    ) -> int:
        answer_values = iter(answers or [])
        secret_values = iter(secrets or [])
        with mock.patch("remnawave_manager.cli.require_root"):
            return main(
                arguments,
                runtime_paths=self.paths,
                runner=mock.Mock(),
                input_fn=lambda _prompt: next(answer_values),
                secret_fn=lambda _prompt: next(secret_values),
                stdout=self.stdout,
                stderr=self.stderr,
            )

    def test_node_secret_comes_from_environment_not_argv(self) -> None:
        result = NodeInstallResult(inventory(), "node.example.com")
        with (
            mock.patch.dict(
                os.environ, {"RWM_NODE_SECRET_KEY": "node-secret-from-env"}, clear=False
            ),
            mock.patch(
                "remnawave_manager.cli.install_node", return_value=result
            ) as install,
        ):
            code = self.run_main(
                [
                    "install",
                    "node",
                    "--domain",
                    "node.example.com",
                    "--panel-ip",
                    "192.0.2.10",
                    "--panel-3-3-ready",
                    "--template",
                    "01-northline",
                    "--certificate-method",
                    "http-01",
                    "--email",
                    "admin@example.com",
                ]
            )

        self.assertEqual(code, 0, self.stderr.getvalue())
        options = install.call_args.args[2]
        self.assertEqual(options.secret_key, "node-secret-from-env")
        self.assertTrue(options.panel_3_3_ready)
        self.assertNotIn("RWM_NODE_SECRET_KEY", os.environ)
        self.assertNotIn("node-secret-from-env", self.stdout.getvalue())
        self.assertNotIn("node-secret-from-env", self.stderr.getvalue())

    def test_active_certbot_renewal_blocks_panel_install_before_dispatch(self) -> None:
        with (
            mock.patch(
                "remnawave_manager.cli.assert_no_active_certbot_renewal",
                side_effect=ValidationError("active certbot renewal"),
            ),
            mock.patch("remnawave_manager.cli.install_panel") as install,
        ):
            code = self.run_main(
                [
                    "install",
                    "panel",
                    "--panel-domain",
                    "panel.example.com",
                    "--subscription-domain",
                    "sub.example.com",
                    "--certificate-method",
                    "http-01",
                    "--email",
                    "admin@example.com",
                ]
            )

        self.assertEqual(code, 2)
        install.assert_not_called()
        self.assertIn("active certbot renewal", self.stderr.getvalue())

    def test_unsupported_host_blocks_mutation_before_lock_and_dispatch(self) -> None:
        with (
            mock.patch(
                "remnawave_manager.cli.require_ubuntu_2404",
                side_effect=ValidationError("unsupported host"),
            ),
            mock.patch("remnawave_manager.cli.exclusive_lock") as lock,
            mock.patch("remnawave_manager.cli.install_panel") as install,
        ):
            code = self.run_main(
                [
                    "install",
                    "panel",
                    "--panel-domain",
                    "panel.example.com",
                    "--subscription-domain",
                    "sub.example.com",
                    "--certificate-method",
                    "http-01",
                    "--email",
                    "admin@example.com",
                ]
            )

        self.assertEqual(code, 2)
        self.assertIn("unsupported host", self.stderr.getvalue())
        lock.assert_not_called()
        install.assert_not_called()

    def test_certificate_renew_marks_inherited_manager_lock(self) -> None:
        runner = mock.Mock()
        runner.run.return_value = Result(("certbot", "renew"), 0, "renewed", "")

        with mock.patch("remnawave_manager.cli.require_root"):
            code = main(
                ["certificate", "renew", "--yes"],
                runtime_paths=self.paths,
                runner=runner,
                stdout=self.stdout,
                stderr=self.stderr,
            )

        self.assertEqual(code, 0, self.stderr.getvalue())
        runner.run.assert_called_once_with(
            ["certbot", "renew", "--non-interactive"],
            check=False,
            timeout=1800,
            env={"RWM_CERTBOT_MANAGER_LOCK_HELD": "1"},
        )

    def test_registry_password_is_read_with_hidden_prompt(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("remnawave_manager.cli.registry_login") as login,
        ):
            code = self.run_main(
                [
                    "registry",
                    "login",
                    "--registry",
                    "docker-hub",
                    "--username",
                    "alice",
                ],
                secrets=["registry-token"],
            )

        self.assertEqual(code, 0, self.stderr.getvalue())
        self.assertEqual(login.call_args.kwargs["password"], "registry-token")
        self.assertNotIn("registry-token", self.stdout.getvalue())

    def test_registry_password_rejects_format_controls_before_docker(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {"RWM_REGISTRY_PASSWORD": "hidden\u202etoken"},
                clear=False,
            ),
            mock.patch("remnawave_manager.cli.registry_login") as login,
        ):
            code = self.run_main(
                [
                    "registry",
                    "login",
                    "--registry",
                    "docker-hub",
                    "--username",
                    "alice",
                ]
            )

        self.assertEqual(code, 2)
        login.assert_not_called()
        self.assertNotIn("RWM_REGISTRY_PASSWORD", os.environ)
        self.assertNotIn("\u202e", self.stderr.getvalue())

    def test_external_command_status_output_is_terminal_sanitized(self) -> None:
        result = Result(("command",), 1, "", "failed\x1b[31m\rspoof\u202ehidden")
        for arguments in (
            ["certificate", "status"],
            ["certificate", "renew", "--yes"],
            ["firewall", "status"],
        ):
            with self.subTest(arguments=arguments):
                self.stdout = io.StringIO()
                self.stderr = io.StringIO()
                runner = mock.Mock()
                runner.run.return_value = result
                with mock.patch("remnawave_manager.cli.require_root"):
                    code = main(
                        arguments,
                        runtime_paths=self.paths,
                        runner=runner,
                        stdout=self.stdout,
                        stderr=self.stderr,
                    )
                self.assertEqual(code, 1)
                output = self.stdout.getvalue()
                self.assertIn("failed [31m spoof hidden", output)
                self.assertNotIn("\x1b", output)
                self.assertNotIn("\r", output)
                self.assertNotIn("\u202e", output)

    def test_manager_error_output_is_terminal_sanitized(self) -> None:
        with mock.patch(
            "remnawave_manager.cli.registry_status",
            side_effect=ValidationError("bad\x1b[31m\rspoof\u202ehidden"),
        ):
            code = self.run_main(["registry", "status"])

        self.assertEqual(code, 2)
        output = self.stderr.getvalue()
        self.assertIn("bad [31m spoof hidden", output)
        self.assertNotIn("\x1b", output)
        self.assertNotIn("\r", output)
        self.assertNotIn("\u202e", output)

    def test_getpass_never_falls_back_to_echoed_input_without_tty(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch(
                "remnawave_manager.cli.getpass.getpass",
                side_effect=getpass.GetPassWarning("нет защищённого терминала"),
            ),
            mock.patch("remnawave_manager.cli.registry_login") as login,
            mock.patch("remnawave_manager.cli.require_root"),
        ):
            code = main(
                [
                    "registry",
                    "login",
                    "--registry",
                    "docker-hub",
                    "--username",
                    "alice",
                ],
                runtime_paths=self.paths,
                runner=mock.Mock(),
                input_fn=lambda _prompt: "",
                stdout=self.stdout,
                stderr=self.stderr,
            )

        self.assertEqual(code, 2)
        login.assert_not_called()
        self.assertIn("Защищённый терминал недоступен", self.stderr.getvalue())

    def test_api_token_is_prompted_and_never_printed(self) -> None:
        provisioned = ProvisionedReality(
            profile_uuid="11111111-1111-1111-1111-111111111111",
            inbound_uuid="22222222-2222-2222-2222-222222222222",
            node_uuid="33333333-3333-3333-3333-333333333333",
            host_uuid="44444444-4444-4444-4444-444444444444",
            secret_key="new-node-secret",
        )
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("remnawave_manager.cli.RemnawaveApi"),
            mock.patch(
                "remnawave_manager.cli.provision_reality_node",
                return_value=provisioned,
            ) as provision,
            mock.patch(
                "remnawave_manager.cli.complete_reality_credentials_handoff"
            ) as complete_handoff,
        ):
            code = self.run_main(
                [
                    "api",
                    "reality",
                    "--profile-name",
                    "Moscow",
                    "--inbound-tag",
                    "reality_msk",
                    "--node-name",
                    "Moscow Node",
                    "--domain",
                    "node.example.com",
                    "--yes",
                ],
                secrets=["admin-api-token"],
            )

        self.assertEqual(code, 0, self.stderr.getvalue())
        self.assertEqual(provision.call_args.args[1], "admin-api-token")
        self.assertIs(provision.call_args.kwargs["store"].paths, self.paths)
        complete_handoff.assert_called_once_with(mock.ANY, provisioned)
        self.assertNotIn("admin-api-token", self.stdout.getvalue())
        self.assertIn("new-node-secret", self.stdout.getvalue())

    def test_invalid_reality_input_is_rejected_before_api_token_is_consumed(
        self,
    ) -> None:
        token = "admin-api-token"
        with (
            mock.patch.dict(os.environ, {"RWM_API_TOKEN": token}, clear=False),
            mock.patch("remnawave_manager.cli.provision_reality_node") as provision,
        ):
            code = self.run_main(
                [
                    "api",
                    "reality",
                    "--profile-name",
                    "bad\nname",
                    "--inbound-tag",
                    "REALITY",
                    "--node-name",
                    "Node One",
                    "--domain",
                    "node.example.com",
                    "--yes",
                ]
            )
            self.assertEqual(os.environ.get("RWM_API_TOKEN"), token)

        self.assertEqual(code, 2)
        provision.assert_not_called()
        self.assertNotIn(token, self.stdout.getvalue())
        self.assertNotIn(token, self.stderr.getvalue())

    def test_reality_output_failure_keeps_recovery_handoff_incomplete(self) -> None:
        class FailingFlush(io.StringIO):
            def flush(self) -> None:
                raise OSError("broken output")

        provisioned = ProvisionedReality(
            profile_uuid="11111111-1111-1111-1111-111111111111",
            inbound_uuid="22222222-2222-2222-2222-222222222222",
            node_uuid="33333333-3333-3333-3333-333333333333",
            host_uuid="44444444-4444-4444-4444-444444444444",
            secret_key="new-node-secret",
        )
        output = FailingFlush()
        with (
            mock.patch("remnawave_manager.cli.require_root"),
            mock.patch("remnawave_manager.cli.RemnawaveApi"),
            mock.patch(
                "remnawave_manager.cli.provision_reality_node",
                return_value=provisioned,
            ),
            mock.patch(
                "remnawave_manager.cli.complete_reality_credentials_handoff"
            ) as complete_handoff,
        ):
            code = main(
                [
                    "api",
                    "reality",
                    "--profile-name",
                    "Moscow",
                    "--inbound-tag",
                    "REALITY",
                    "--node-name",
                    "Node One",
                    "--domain",
                    "node.example.com",
                    "--yes",
                ],
                runtime_paths=self.paths,
                runner=mock.Mock(),
                secret_fn=lambda _prompt: "admin-api-token",
                stdout=output,
                stderr=self.stderr,
            )

        self.assertEqual(code, 2)
        complete_handoff.assert_not_called()
        self.assertIn("recovery-файл сохранён", self.stderr.getvalue())

    def test_update_is_not_called_when_confirmation_is_rejected(self) -> None:
        store_inventory = inventory("panel")
        with (
            mock.patch(
                "remnawave_manager.cli.StateStore.load_inventory",
                return_value=store_inventory,
            ),
            mock.patch("remnawave_manager.cli.update_panel_stack") as update,
        ):
            code = self.run_main(["update"], answers=["нет"])

        self.assertEqual(code, 2)
        update.assert_not_called()
        self.assertIn("отменена", self.stderr.getvalue())

    def test_node_update_retries_with_validated_replacement_secret(self) -> None:
        backup = BackupResult(Path(self.temporary.name) / "pre-node.tar.gz", {})
        with (
            mock.patch(
                "remnawave_manager.cli.StateStore.load_inventory",
                return_value=inventory("node"),
            ),
            mock.patch("remnawave_manager.cli.assert_no_active_certbot_renewal"),
            mock.patch(
                "remnawave_manager.cli.update_node",
                side_effect=[
                    NodeSecretValidationError("old SECRET_KEY rejected"),
                    backup,
                ],
            ) as update,
        ):
            code = self.run_main(
                ["update", "--yes", "--panel-3-3-ready"],
                secrets=["replacement-node-secret"],
            )

        self.assertEqual(code, 0, self.stderr.getvalue())
        self.assertEqual(update.call_count, 2)
        self.assertNotIn("replacement-node-secret", self.stdout.getvalue())
        self.assertNotIn("replacement-node-secret", self.stderr.getvalue())
        self.assertEqual(
            update.call_args_list[1].kwargs["replacement_secret"],
            "replacement-node-secret",
        )

    def test_json_node_update_requires_replacement_secret_on_retry(self) -> None:
        with (
            mock.patch(
                "remnawave_manager.cli.StateStore.load_inventory",
                return_value=inventory("node"),
            ),
            mock.patch("remnawave_manager.cli.assert_no_active_certbot_renewal"),
            mock.patch(
                "remnawave_manager.cli.update_node",
                side_effect=NodeSecretValidationError("old SECRET_KEY rejected"),
            ) as update,
        ):
            code = self.run_main(["--json", "update", "--yes"])

        self.assertEqual(code, 2)
        update.assert_called_once()
        self.assertIn("RWM_NODE_SECRET_KEY", self.stderr.getvalue())

    def test_json_node_update_uses_replacement_secret_from_environment(self) -> None:
        backup = BackupResult(Path(self.temporary.name) / "pre-node.tar.gz", {})
        with (
            mock.patch.dict(
                os.environ,
                {"RWM_NODE_SECRET_KEY": "replacement-node-secret"},
                clear=False,
            ),
            mock.patch(
                "remnawave_manager.cli.StateStore.load_inventory",
                return_value=inventory("node"),
            ),
            mock.patch("remnawave_manager.cli.assert_no_active_certbot_renewal"),
            mock.patch(
                "remnawave_manager.cli.update_node",
                side_effect=[
                    NodeSecretValidationError("old SECRET_KEY rejected"),
                    backup,
                ],
            ) as update,
        ):
            code = self.run_main(["--json", "update", "--yes"])

        self.assertEqual(code, 0, self.stderr.getvalue())
        self.assertEqual(
            update.call_args_list[1].kwargs["replacement_secret"],
            "replacement-node-secret",
        )
        self.assertNotIn("RWM_NODE_SECRET_KEY", os.environ)
        self.assertNotIn("replacement-node-secret", self.stdout.getvalue())

    def test_node_update_can_fetch_replacement_secret_from_panel_api(self) -> None:
        backup = BackupResult(Path(self.temporary.name) / "pre-node.tar.gz", {})
        api = mock.Mock()
        api.keygen.return_value = "api-node-secret"
        with (
            mock.patch(
                "remnawave_manager.cli.StateStore.load_inventory",
                return_value=inventory("node"),
            ),
            mock.patch("remnawave_manager.cli.assert_no_active_certbot_renewal"),
            mock.patch("remnawave_manager.cli.RemnawaveApi", return_value=api) as api_type,
            mock.patch(
                "remnawave_manager.cli.update_node",
                side_effect=[
                    NodeSecretValidationError("old key rejected"),
                    NodeSecretValidationError("copied key rejected"),
                    backup,
                ],
            ) as update,
        ):
            code = self.run_main(
                ["update", "--yes"],
                answers=["y", "https://panel.example.com"],
                secrets=["copied-node-secret", "admin-api-token", ""],
            )

        self.assertEqual(code, 0, self.stderr.getvalue())
        api_type.assert_called_once_with("https://panel.example.com")
        api.keygen.assert_called_once_with("admin-api-token")
        self.assertEqual(update.call_count, 3)
        self.assertEqual(update.call_args.kwargs["replacement_secret"], "api-node-secret")
        output = self.stdout.getvalue() + self.stderr.getvalue()
        self.assertNotIn("copied-node-secret", output)
        self.assertNotIn("admin-api-token", output)
        self.assertNotIn("api-node-secret", output)

    def test_node_update_prompts_for_panel_cookie_gate_for_keygen_api(self) -> None:
        backup = BackupResult(Path(self.temporary.name) / "pre-node.tar.gz", {})
        api = mock.Mock()
        api.keygen.return_value = "api-node-secret"
        with (
            mock.patch(
                "remnawave_manager.cli.StateStore.load_inventory",
                return_value=inventory("node"),
            ),
            mock.patch("remnawave_manager.cli.assert_no_active_certbot_renewal"),
            mock.patch("remnawave_manager.cli.RemnawaveApi", return_value=api) as api_type,
            mock.patch(
                "remnawave_manager.cli.update_node",
                side_effect=[
                    NodeSecretValidationError("old key rejected"),
                    NodeSecretValidationError("copied key rejected"),
                    backup,
                ],
            ),
        ):
            code = self.run_main(
                ["update", "--yes"],
                answers=["y", "https://panel.example.com"],
                secrets=[
                    "copied-node-secret",
                    "admin-api-token",
                    '{"rwm_access":"cookie-value"}',
                ],
            )

        self.assertEqual(code, 0, self.stderr.getvalue())
        api_type.assert_called_once_with(
            "https://panel.example.com", cookies={"rwm_access": "cookie-value"}
        )

    def test_warp_plus_key_comes_only_from_environment(self) -> None:
        with (
            mock.patch.dict(
                os.environ, {"WGCF_LICENSE_KEY": "warp-plus-key"}, clear=False
            ),
            mock.patch(
                "remnawave_manager.cli.install_warp", return_value={}
            ) as install,
        ):
            code = self.run_main(["warp", "install", "--accept-tos"])

        self.assertEqual(code, 0, self.stderr.getvalue())
        self.assertEqual(install.call_args.kwargs["license_key"], "warp-plus-key")
        self.assertNotIn("WGCF_LICENSE_KEY", os.environ)
        self.assertNotIn("warp-plus-key", self.stdout.getvalue())

    def test_disguise_apply_delegates_atomic_refresh_to_production_api(self) -> None:
        with mock.patch(
            "remnawave_manager.cli.apply_template",
            return_value=Path("/opt/remnanode/site"),
        ) as apply:
            code = self.run_main(["disguise", "apply", "01-northline", "--yes"])

        self.assertEqual(code, 0, self.stderr.getvalue())
        apply.assert_called_once()

    def test_interactive_menu_can_exit_without_mutation(self) -> None:
        code = self.run_main([], answers=["0"])

        self.assertEqual(code, 0)
        self.assertIn("Главное меню", self.stdout.getvalue())
        self.assertIn("Работа завершена", self.stdout.getvalue())

    def test_manager_update_uses_installer_lock_instead_of_outer_cli_lock(self) -> None:
        with (
            mock.patch(
                "remnawave_manager.cli.update_manager", return_value="rwm 0.1.3"
            ) as update,
            mock.patch(
                "remnawave_manager.cli.assert_no_active_certbot_renewal"
            ) as certbot_guard,
            mock.patch("remnawave_manager.cli.exclusive_lock") as lock,
        ):
            code = self.run_main(["manager", "update", "--yes"])

        self.assertEqual(code, 0, self.stderr.getvalue())
        update.assert_called_once()
        certbot_guard.assert_called_once_with()
        lock.assert_not_called()
        self.assertIn("rwm 0.1.3", self.stdout.getvalue())

    def test_interactive_manager_update_exits_old_menu_process(self) -> None:
        with (
            mock.patch(
                "remnawave_manager.cli.update_manager", return_value="rwm 0.1.3"
            ) as update,
            mock.patch("remnawave_manager.cli.assert_no_active_certbot_renewal"),
        ):
            code = self.run_main([], answers=["17", "y"])

        self.assertEqual(code, 0, self.stderr.getvalue())
        update.assert_called_once()
        self.assertIn("следующий запуск использует новую версию", self.stdout.getvalue())

    def test_interactive_ubuntu_section_can_update_packages(self) -> None:
        with (
            mock.patch(
                "remnawave_manager.cli.update_operating_system",
                return_value=OperatingSystemUpdate(reboot_required=False),
            ) as update,
            mock.patch("remnawave_manager.cli.assert_no_active_certbot_renewal"),
        ):
            code = self.run_main([], answers=["15", "3", "y", "0", "0"])

        self.assertEqual(code, 0, self.stderr.getvalue())
        update.assert_called_once_with(mock.ANY, self.paths)
        self.assertIn("Обновление пакетов Ubuntu завершено", self.stdout.getvalue())

    def test_interactive_menu_redraws_only_on_a_real_terminal(self) -> None:
        class TerminalBuffer(io.StringIO):
            def isatty(self) -> bool:
                return True

        terminal = TerminalBuffer()
        answers = iter(["0"])
        with mock.patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=False):
            code = main(
                [],
                runtime_paths=self.paths,
                runner=mock.Mock(),
                input_fn=lambda _prompt: next(answers),
                stdout=terminal,
                stderr=self.stderr,
            )

        rendered = terminal.getvalue()
        self.assertEqual(code, 0)
        self.assertTrue(rendered.startswith("\033[H\033[2J\033[3J"))
        self.assertIn("Remnawave Manager", rendered)
        self.assertIn("Главное меню", rendered)

    def test_interactive_menu_pauses_before_redrawing_command_output(self) -> None:
        class TerminalBuffer(io.StringIO):
            def isatty(self) -> bool:
                return True

        terminal = TerminalBuffer()
        answers = iter(["14", "", "0"])
        prompts: list[str] = []

        def prompt(message: str) -> str:
            prompts.append(message)
            return next(answers)

        with (
            mock.patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=False),
            mock.patch(
                "remnawave_manager.cli.StateStore.load_inventory",
                return_value=inventory(),
            ),
        ):
            code = main(
                [],
                runtime_paths=self.paths,
                runner=mock.Mock(),
                input_fn=prompt,
                stdout=terminal,
                stderr=self.stderr,
            )

        self.assertEqual(code, 0, self.stderr.getvalue())
        self.assertEqual(terminal.getvalue().count("\033[H\033[2J\033[3J"), 2)
        self.assertIn("Нажмите Enter, чтобы продолжить", prompts[1])

    def test_interactive_firewall_auto_panel_does_not_request_panel_ip(self) -> None:
        with (
            mock.patch(
                "remnawave_manager.cli.StateStore.load_inventory",
                return_value=inventory("panel"),
            ),
            mock.patch(
                "remnawave_manager.cli.configure_firewall", return_value=(22,)
            ) as configure,
        ):
            code = self.run_main([], answers=["12", "2", "1", "y", "0", "0"])

        self.assertEqual(code, 0, self.stderr.getvalue())
        configure.assert_called_once_with(
            mock.ANY,
            "panel",
            panel_ip=None,
            ssh_ports=None,
            transaction_root=self.paths.state / "firewall-transactions",
        )

    def test_interactive_firewall_auto_node_requests_panel_ip(self) -> None:
        with (
            mock.patch(
                "remnawave_manager.cli.StateStore.load_inventory",
                return_value=inventory("node"),
            ),
            mock.patch(
                "remnawave_manager.cli.configure_firewall", return_value=(22,)
            ) as configure,
        ):
            code = self.run_main(
                [], answers=["12", "2", "1", "192.0.2.10", "y", "0", "0"]
            )

        self.assertEqual(code, 0, self.stderr.getvalue())
        configure.assert_called_once_with(
            mock.ANY,
            "node",
            panel_ip="192.0.2.10",
            ssh_ports=None,
            transaction_root=self.paths.state / "firewall-transactions",
        )

    def test_interactive_menu_pauses_after_argument_collection_error(self) -> None:
        class TerminalBuffer(io.StringIO):
            def isatty(self) -> bool:
                return True

        terminal = TerminalBuffer()
        answers = iter(["12", "2", "1", "", "0", "0"])
        prompts: list[str] = []

        def prompt(message: str) -> str:
            prompts.append(message)
            return next(answers)

        with (
            mock.patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=False),
            mock.patch(
                "remnawave_manager.cli.StateStore.load_inventory",
                side_effect=ValidationError("inventory не найден"),
            ),
        ):
            code = main(
                [],
                runtime_paths=self.paths,
                runner=mock.Mock(),
                input_fn=prompt,
                stdout=terminal,
                stderr=self.stderr,
            )

        self.assertEqual(code, 0)
        self.assertIn("Ошибка: inventory не найден", self.stderr.getvalue())
        self.assertIn("Нажмите Enter, чтобы продолжить", prompts[3])

    def test_interactive_menu_exits_on_end_of_input(self) -> None:
        prompt = mock.Mock(side_effect=EOFError)

        code = main(
            [],
            runtime_paths=self.paths,
            runner=mock.Mock(),
            input_fn=prompt,
            stdout=self.stdout,
            stderr=self.stderr,
        )

        self.assertEqual(code, 130)
        self.assertIn("Интерактивный режим завершён", self.stderr.getvalue())
        prompt.assert_called_once()

    def test_service_logs_dispatch_is_read_only(self) -> None:
        current = inventory()
        current.components["node"] = Component(
            "node", "remnanode", container="node-container"
        )
        runner = mock.Mock()
        with (
            mock.patch(
                "remnawave_manager.cli.StateStore.load_inventory", return_value=current
            ),
            mock.patch("remnawave_manager.cli.require_root") as require_root,
        ):
            code = main(
                [
                    "service",
                    "logs",
                    "node",
                    "--tail",
                    "42",
                    "--since",
                    "30m",
                    "--follow",
                ],
                runtime_paths=self.paths,
                runner=runner,
                stdout=self.stdout,
                stderr=self.stderr,
            )

        self.assertEqual(code, 0, self.stderr.getvalue())
        require_root.assert_not_called()
        runner.interactive.assert_called_once_with(
            [
                "docker",
                "logs",
                "--tail",
                "42",
                "--since",
                "30m",
                "--follow",
                "node-container",
            ]
        )

    def test_all_service_logs_dispatch_uses_compose_and_is_read_only(self) -> None:
        current = inventory()
        runner = mock.Mock()
        with (
            mock.patch(
                "remnawave_manager.cli.StateStore.load_inventory", return_value=current
            ),
            mock.patch("remnawave_manager.cli.require_root") as require_root,
        ):
            code = main(
                ["service", "logs", "all", "--tail", "42", "--since", "30m"],
                runtime_paths=self.paths,
                runner=runner,
                stdout=self.stdout,
                stderr=self.stderr,
            )

        self.assertEqual(code, 0, self.stderr.getvalue())
        require_root.assert_not_called()
        runner.interactive.assert_called_once_with(
            [
                "docker",
                "compose",
                "--env-file",
                str(Path("/opt/remnanode/.env")),
                "-f",
                str(Path("/opt/remnanode/docker-compose.yml")),
                "logs",
                "--tail",
                "42",
                "--since",
                "30m",
            ],
            cwd=Path("/opt/remnanode"),
        )

    def test_panel_cli_dispatch_requires_manager_lock(self) -> None:
        current = inventory("panel")
        current.components["panel"] = Component(
            "panel", "remnawave", container="panel-container"
        )
        runner = mock.Mock()
        with (
            mock.patch(
                "remnawave_manager.cli.StateStore.load_inventory", return_value=current
            ),
            mock.patch("remnawave_manager.cli.require_root") as require_root,
        ):
            code = main(
                ["service", "panel-cli"],
                runtime_paths=self.paths,
                runner=runner,
                stdout=self.stdout,
                stderr=self.stderr,
            )

        self.assertEqual(code, 0, self.stderr.getvalue())
        require_root.assert_called_once_with()
        runner.interactive.assert_called_once_with(
            ["docker", "exec", "-it", "panel-container", "cli"]
        )

    def test_backup_schedule_status_is_read_only(self) -> None:
        status = BackupSchedule(True, True, "daily", "02:30", 7, "завтра")
        with (
            mock.patch(
                "remnawave_manager.cli.backup_schedule_status", return_value=status
            ),
            mock.patch("remnawave_manager.cli.require_root") as require_root,
        ):
            code = main(
                ["backup", "schedule-status"],
                runtime_paths=self.paths,
                runner=mock.Mock(),
                stdout=self.stdout,
                stderr=self.stderr,
            )

        self.assertEqual(code, 0, self.stderr.getvalue())
        require_root.assert_not_called()
        self.assertIn("Расписание backup: включено", self.stdout.getvalue())

    def test_backup_delete_requires_confirmation_and_delegates_paths(self) -> None:
        first = Path("/var/backups/remnawave-manager/first.tar.gz")
        second = Path("/var/backups/remnawave-manager/second.tar.gz")
        with mock.patch("remnawave_manager.cli.delete_backups") as delete:
            rejected = self.run_main(
                ["backup", "delete", str(first), str(second)], answers=["ДА"]
            )

        self.assertEqual(rejected, 2)
        delete.assert_not_called()
        self.assertIn("отменена", self.stderr.getvalue())

        self.stderr = io.StringIO()
        with mock.patch(
            "remnawave_manager.cli.delete_backups", return_value=[first, second]
        ) as delete:
            code = self.run_main(
                ["backup", "delete", str(first), str(second)],
                answers=["y"],
            )

        self.assertEqual(code, 0, self.stderr.getvalue())
        delete.assert_called_once_with(mock.ANY, [first, second])
        self.assertIn(f"Backup удалён: {first}", self.stdout.getvalue())
        self.assertIn(f"Backup удалён: {second}", self.stdout.getvalue())

    def test_json_backup_delete_emits_selected_paths(self) -> None:
        first = Path("/var/backups/remnawave-manager/first.tar.gz")
        second = Path("/var/backups/remnawave-manager/second.tar.gz")
        with mock.patch(
            "remnawave_manager.cli.delete_backups", return_value=[first, second]
        ):
            code = self.run_main(
                [
                    "--json",
                    "backup",
                    "delete",
                    str(first),
                    str(second),
                    "--yes",
                ]
            )

        self.assertEqual(code, 0, self.stderr.getvalue())
        self.assertEqual(
            json.loads(self.stdout.getvalue()),
            {"status": "deleted", "backups": [str(first), str(second)]},
        )

    def test_interactive_backup_delete_selects_multiple_numbers(self) -> None:
        backups = [
            Path("/var/backups/remnawave-manager/newest.tar.gz"),
            Path("/var/backups/remnawave-manager/middle.tar.gz"),
            Path("/var/backups/remnawave-manager/oldest.tar.gz"),
        ]
        with (
            mock.patch("remnawave_manager.cli.list_backups", return_value=backups),
            mock.patch(
                "remnawave_manager.cli.delete_backups",
                return_value=[backups[0], backups[2]],
            ) as delete,
        ):
            code = self.run_main(
                [],
                answers=["4", "3", "1, 1", "1, 3", "y", "0", "0"],
            )

        self.assertEqual(code, 0, self.stderr.getvalue())
        delete.assert_called_once_with(mock.ANY, [backups[0], backups[2]])
        self.assertIn(f"1. {backups[0]}", self.stdout.getvalue())
        self.assertIn("Номера backup не должны повторяться", self.stderr.getvalue())

    def test_interactive_action_returns_to_its_submenu(self) -> None:
        class TerminalBuffer(io.StringIO):
            def isatty(self) -> bool:
                return True

        backup = Path("/var/backups/remnawave-manager/latest.tar.gz")
        terminal = TerminalBuffer()
        answers = iter(["4", "2", "", "0", "0"])
        prompts: list[str] = []

        def prompt(message: str) -> str:
            prompts.append(message)
            return next(answers)

        with (
            mock.patch.dict(os.environ, {"TERM": "xterm-256color"}, clear=False),
            mock.patch("remnawave_manager.cli.list_backups", return_value=[backup]),
        ):
            code = main(
                [],
                runtime_paths=self.paths,
                runner=mock.Mock(),
                input_fn=prompt,
                stdout=terminal,
                stderr=self.stderr,
            )

        self.assertEqual(code, 0, self.stderr.getvalue())
        self.assertEqual(terminal.getvalue().count("Backup:"), 2)
        self.assertIn(str(backup), terminal.getvalue())
        self.assertIn("Нажмите Enter, чтобы продолжить", prompts[2])

    def test_all_sections_with_action_menus_remain_open(self) -> None:
        sections = (2, 4, 5, 6, 7, 8, 9, 11, 12, 13, 15)
        for section in sections:
            with (
                self.subTest(section=section),
                mock.patch(
                    "remnawave_manager.cli._interactive_arguments",
                    side_effect=(["inventory"], None),
                ) as arguments,
                mock.patch("remnawave_manager.cli.execute", return_value=0) as execute,
            ):
                code = self.run_main([], answers=[str(section), "0"])

            self.assertEqual(code, 0, self.stderr.getvalue())
            self.assertEqual(arguments.call_count, 2)
            execute.assert_called_once()

    def test_success_output_has_terminal_control_barrier(self) -> None:
        status = BackupSchedule(
            True,
            True,
            "daily",
            "02:30",
            7,
            "tomorrow\x1b[31m\rspoof\u202ehidden",
        )
        with mock.patch(
            "remnawave_manager.cli.backup_schedule_status",
            return_value=status,
        ):
            code = self.run_main(["backup", "schedule-status"])

        self.assertEqual(code, 0, self.stderr.getvalue())
        output = self.stdout.getvalue()
        self.assertIn("tomorrow [31m spoof hidden", output)
        self.assertNotIn("\x1b", output)
        self.assertNotIn("\r", output)
        self.assertNotIn("\u202e", output)

    def test_certificate_repair_renewal_rebuilds_from_current_inventory(self) -> None:
        current = inventory("panel")
        current.components["nginx"] = Component(
            "nginx", "remnawave-nginx", container="remnawave-nginx"
        )
        plan = CertbotRenewalPlan(
            certificate_names=("panel.example.com",),
            missing_renewal_configs=(),
            authenticators=(("panel.example.com", "standalone"),),
            legacy_renew_hooks=("panel.example.com",),
        )
        with (
            mock.patch(
                "remnawave_manager.cli.StateStore.load_inventory",
                return_value=current,
            ),
            mock.patch(
                "remnawave_manager.cli.inspect_compose", return_value={}
            ) as inspect,
            mock.patch(
                "remnawave_manager.cli.configure_adopted_certbot",
                return_value=plan,
            ) as configure,
        ):
            code = self.run_main(["certificate", "repair-renewal", "--yes"])

        self.assertEqual(code, 0, self.stderr.getvalue())
        inspect.assert_called_once()
        configure.assert_called_once_with(
            mock.ANY,
            current,
            {},
            store=mock.ANY,
        )
        self.assertIn("panel.example.com", self.stdout.getvalue())

    def test_certificate_issue_reads_gcore_token_from_environment_and_emits_json(
        self,
    ) -> None:
        issued = IssuedCertificate(
            certificate_name="example.com",
            domains=("example.com", "*.example.com"),
            fullchain=Path("/etc/letsencrypt/live/example.com/fullchain.pem"),
            private_key=Path("/etc/letsencrypt/live/example.com/privkey.pem"),
            method="gcore",
        )
        token = "gcore-token-that-must-stay-secret"
        with (
            mock.patch.dict(os.environ, {"RWM_GCORE_TOKEN": token}, clear=False),
            mock.patch(
                "remnawave_manager.cli.StateStore.load_inventory",
                return_value=inventory(),
            ),
            mock.patch(
                "remnawave_manager.cli.issue_certificate",
                return_value=issued,
            ) as issue,
        ):
            code = self.run_main(
                [
                    "--json",
                    "certificate",
                    "issue",
                    "--domain",
                    "example.com",
                    "--method",
                    "gcore",
                    "--email",
                    "admin@example.com",
                    "--wildcard",
                    "--yes",
                ]
            )

        self.assertEqual(code, 0, self.stderr.getvalue())
        payload = json.loads(self.stdout.getvalue())
        self.assertEqual(payload["certificate_name"], "example.com")
        self.assertEqual(payload["domains"], ["example.com", "*.example.com"])
        spec = issue.call_args.args[3]
        self.assertEqual(spec.gcore_token, token)
        self.assertTrue(issue.call_args.kwargs["wildcard"])
        self.assertNotIn(token, self.stdout.getvalue())
        self.assertNotIn(token, self.stderr.getvalue())

    def test_system_status_is_read_only(self) -> None:
        status = HostStatus(
            bbr_available=True,
            bbr_enabled=True,
            fq_enabled=True,
            unattended_configured=True,
            apt_daily_timer_enabled=True,
            apt_daily_timer_active=True,
            apt_upgrade_timer_enabled=True,
            apt_upgrade_timer_active=True,
            unattended_service_enabled=True,
            unattended_service_active=True,
        )
        with (
            mock.patch("remnawave_manager.cli.host_status", return_value=status),
            mock.patch("remnawave_manager.cli.require_root") as require_root,
        ):
            code = main(
                ["system", "status"],
                runtime_paths=self.paths,
                runner=mock.Mock(),
                stdout=self.stdout,
                stderr=self.stderr,
            )

        self.assertEqual(code, 0, self.stderr.getvalue())
        require_root.assert_not_called()
        self.assertIn("BBR: включён", self.stdout.getvalue())

    def test_system_update_emits_machine_readable_reboot_status(self) -> None:
        with mock.patch(
            "remnawave_manager.cli.update_operating_system",
            return_value=OperatingSystemUpdate(reboot_required=True),
        ) as update:
            code = self.run_main(["--json", "system", "update", "--yes"])

        self.assertEqual(code, 0, self.stderr.getvalue())
        self.assertEqual(
            json.loads(self.stdout.getvalue()),
            {"reboot_required": True, "status": "updated"},
        )
        update.assert_called_once_with(mock.ANY, self.paths)

    def test_backup_schedule_enable_delegates_validated_values(self) -> None:
        status = BackupSchedule(True, True, "weekly", "03:15", 12, None)
        with mock.patch(
            "remnawave_manager.cli.install_backup_schedule", return_value=status
        ) as install:
            code = self.run_main(
                [
                    "backup",
                    "schedule-enable",
                    "--frequency",
                    "weekly",
                    "--time",
                    "03:15",
                    "--retention",
                    "12",
                    "--yes",
                ]
            )

        self.assertEqual(code, 0, self.stderr.getvalue())
        self.assertEqual(install.call_args.kwargs["frequency"], "weekly")
        self.assertEqual(install.call_args.kwargs["time_of_day"], "03:15")
        self.assertEqual(install.call_args.kwargs["retention"], 12)

    def test_emergency_status_is_read_only_and_open_is_mutating(self) -> None:
        closed = EmergencyAccess(False, None, None, None)
        opened = EmergencyAccess(
            True,
            "http://127.0.0.1:8443/auth/login",
            "2026-08-02T12:30:00+00:00",
            "ssh -L 8443:127.0.0.1:8443 root@SERVER",
        )
        with (
            mock.patch(
                "remnawave_manager.cli.emergency_access_status", return_value=closed
            ),
            mock.patch("remnawave_manager.cli.require_root") as require_root,
        ):
            code = main(
                ["security", "emergency-status"],
                runtime_paths=self.paths,
                runner=mock.Mock(),
                stdout=self.stdout,
                stderr=self.stderr,
            )

        self.assertEqual(code, 0, self.stderr.getvalue())
        require_root.assert_not_called()

        with mock.patch(
            "remnawave_manager.cli.open_emergency_access", return_value=opened
        ) as open_access:
            code = self.run_main(
                ["security", "emergency-open", "--minutes", "45", "--yes"]
            )

        self.assertEqual(code, 0, self.stderr.getvalue())
        self.assertEqual(open_access.call_args.kwargs["minutes"], 45)
        self.assertIn("SSH-туннель", self.stdout.getvalue())

    def test_json_backup_restore_emits_one_structured_result(self) -> None:
        backup = Path(self.temporary.name) / "backup.tar.gz"
        with mock.patch("remnawave_manager.cli.restore_backup") as restore:
            code = self.run_main(
                [
                    "--json",
                    "backup",
                    "restore",
                    str(backup),
                    "--without-database",
                    "--yes",
                ]
            )

        self.assertEqual(code, 0, self.stderr.getvalue())
        self.assertEqual(
            json.loads(self.stdout.getvalue()),
            {
                "status": "restored",
                "backup": str(backup),
                "database_restored": False,
            },
        )
        restore.assert_called_once()

    def test_json_adopt_does_not_append_human_warnings(self) -> None:
        current = inventory()
        current.features.update(
            {
                "certbot_legacy_renew_hook_removed": True,
                "certbot_legacy_cron_removed": True,
            }
        )
        with mock.patch("remnawave_manager.cli.adopt", return_value=current):
            code = self.run_main(["--json", "adopt"])

        self.assertEqual(code, 0, self.stderr.getvalue())
        payload = json.loads(self.stdout.getvalue())
        self.assertTrue(payload["features"]["certbot_legacy_renew_hook_removed"])
        self.assertTrue(payload["features"]["certbot_legacy_cron_removed"])

    def test_json_rejects_streaming_commands_before_execution(self) -> None:
        for command in (
            ["--json", "service", "logs", "node"],
            ["--json", "service", "panel-cli"],
            ["--json", "manager", "update", "--yes"],
            ["--json", "menu"],
        ):
            with self.subTest(command=command):
                runner = mock.Mock()
                stdout = io.StringIO()
                stderr = io.StringIO()
                code = main(
                    command,
                    runtime_paths=self.paths,
                    runner=runner,
                    stdout=stdout,
                    stderr=stderr,
                )

                self.assertEqual(code, 2)
                self.assertEqual(stdout.getvalue(), "")
                self.assertIn("--json", stderr.getvalue())
                runner.interactive.assert_not_called()

    def test_json_confirmation_requires_yes_without_prompting(self) -> None:
        prompt = mock.Mock(side_effect=AssertionError("input не должен вызываться"))
        with mock.patch("remnawave_manager.cli.restore_backup") as restore:
            code = main(
                ["--json", "backup", "restore", "backup.tar.gz"],
                runtime_paths=self.paths,
                runner=mock.Mock(),
                input_fn=prompt,
                stdout=self.stdout,
                stderr=self.stderr,
            )

        self.assertEqual(code, 2)
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertIn("--yes", self.stderr.getvalue())
        prompt.assert_not_called()
        restore.assert_not_called()

    def test_json_registry_login_requires_explicit_username(self) -> None:
        secret = mock.Mock(
            side_effect=AssertionError("secret prompt не должен вызываться")
        )
        with mock.patch("remnawave_manager.cli.registry_login") as login:
            code = main(
                ["--json", "registry", "login", "--registry", "ghcr"],
                runtime_paths=self.paths,
                runner=mock.Mock(),
                secret_fn=secret,
                stdout=self.stdout,
                stderr=self.stderr,
            )

        self.assertEqual(code, 2)
        self.assertEqual(self.stdout.getvalue(), "")
        self.assertIn("--username", self.stderr.getvalue())
        secret.assert_not_called()
        login.assert_not_called()


if __name__ == "__main__":
    unittest.main()
