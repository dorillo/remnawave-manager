from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from remnawave_manager.errors import TransactionError, ValidationError
from remnawave_manager.lifecycle import (
    component_logs,
    component_status,
    manage_component,
    panel_cli,
    validate_log_since,
)
from remnawave_manager.models import Component, Inventory
from remnawave_manager.runner import Result


def inventory(*components: Component) -> Inventory:
    return Inventory(
        schema_version=1,
        role="panel",
        install_dir="/opt/remnawave",
        compose_file="/opt/remnawave/docker-compose.yml",
        env_file="/opt/remnawave/.env",
        webserver="nginx",
        components={component.name: component for component in components},
    )


class ServiceToolTests(unittest.TestCase):
    def test_all_lifecycle_uses_compose_project_without_no_deps(self) -> None:
        current = inventory(Component("panel", "remnawave"))
        expected_arguments = {
            "start": ["up", "-d"],
            "stop": ["stop"],
            "restart": ["restart"],
        }

        for action, arguments in expected_arguments.items():
            with self.subTest(action=action):
                runner = mock.Mock()
                with mock.patch(
                    "remnawave_manager.lifecycle._verify_started_components"
                ) as verify:
                    manage_component(runner, current, "all", action)  # type: ignore[arg-type]

                command = runner.run.call_args.args[0]
                self.assertEqual(
                    command,
                    [
                        "docker",
                        "compose",
                        "--env-file",
                        str(Path("/opt/remnawave/.env")),
                        "-f",
                        str(Path("/opt/remnawave/docker-compose.yml")),
                        *arguments,
                    ],
                )
                self.assertNotIn("--no-deps", command)
                self.assertEqual(
                    runner.run.call_args.kwargs["cwd"], Path("/opt/remnawave")
                )
                if action == "stop":
                    verify.assert_not_called()
                else:
                    verify.assert_called_once_with(runner, current, "all")

    def test_all_logs_use_compose_aggregated_output(self) -> None:
        runner = mock.Mock()
        current = inventory(Component("panel", "remnawave"))

        component_logs(runner, current, "all", tail=250, follow=True, since="30m")

        runner.interactive.assert_called_once_with(
            [
                "docker",
                "compose",
                "--env-file",
                str(Path("/opt/remnawave/.env")),
                "-f",
                str(Path("/opt/remnawave/docker-compose.yml")),
                "logs",
                "--tail",
                "250",
                "--since",
                "30m",
                "--follow",
            ],
            cwd=Path("/opt/remnawave"),
        )

    def test_individual_start_keeps_no_deps(self) -> None:
        runner = mock.Mock()
        current = inventory(Component("panel", "remnawave"))

        with mock.patch("remnawave_manager.lifecycle._verify_started_components"):
            manage_component(runner, current, "panel", "start")

        self.assertIn("--no-deps", runner.run.call_args.args[0])

    def test_panel_start_waits_for_containers_nginx_and_application_endpoints(
        self,
    ) -> None:
        runner = mock.Mock()
        current = inventory(
            Component("database", "remnawave-db"),
            Component("panel", "remnawave"),
            Component("subscription", "remnawave-subscription-page"),
            Component("nginx", "remnawave-nginx"),
        )

        with (
            mock.patch("remnawave_manager.lifecycle.wait_container") as wait,
            mock.patch("remnawave_manager.lifecycle.test_nginx") as nginx,
            mock.patch("remnawave_manager.lifecycle.wait_panel_http") as panel_http,
            mock.patch(
                "remnawave_manager.lifecycle.check_subscription_http"
            ) as sub_http,
        ):
            manage_component(runner, current, "all", "start")

        self.assertEqual(wait.call_count, 4)
        nginx.assert_called_once_with(runner, current)
        panel_http.assert_called_once_with(runner, current.components["panel"])
        sub_http.assert_called_once_with(runner, current.components["subscription"])

    def test_node_restart_waits_for_runtime_and_xhttp_sockets(self) -> None:
        runner = mock.Mock()
        current = Inventory(
            schema_version=1,
            role="node",
            install_dir="/opt/remnanode",
            compose_file="/opt/remnanode/docker-compose.yml",
            env_file="/opt/remnanode/.env",
            webserver="nginx",
            components={
                "node": Component("node", "remnanode"),
                "nginx": Component("nginx", "remnawave-nginx"),
            },
            xhttp_sockets=["/dev/shm/nginx.sock"],
        )

        with (
            mock.patch("remnawave_manager.lifecycle.wait_container") as wait,
            mock.patch("remnawave_manager.lifecycle.test_nginx") as nginx,
            mock.patch("remnawave_manager.lifecycle.wait_for_paths") as sockets,
            mock.patch("remnawave_manager.lifecycle.wait_node_runtime") as runtime,
        ):
            manage_component(runner, current, "all", "restart")

        self.assertEqual(wait.call_count, 2)
        nginx.assert_called_once_with(runner, current)
        sockets.assert_called_once_with(["/dev/shm/nginx.sock"])
        runtime.assert_called_once_with(runner, current)

    def test_individual_node_restart_also_waits_for_xhttp_sockets(self) -> None:
        runner = mock.Mock()
        current = Inventory(
            schema_version=1,
            role="node",
            install_dir="/opt/remnanode",
            compose_file="/opt/remnanode/docker-compose.yml",
            env_file=None,
            webserver="nginx",
            components={"node": Component("node", "remnanode")},
            xhttp_sockets=["/dev/shm/xray-xhttp.sock"],
        )

        with (
            mock.patch("remnawave_manager.lifecycle.wait_container"),
            mock.patch("remnawave_manager.lifecycle.wait_for_paths") as sockets,
            mock.patch("remnawave_manager.lifecycle.wait_node_runtime"),
        ):
            manage_component(runner, current, "node", "restart")

        sockets.assert_called_once_with(["/dev/shm/xray-xhttp.sock"])

    def test_individual_nginx_restart_waits_for_xhttp_listener_socket(self) -> None:
        runner = mock.Mock()
        current = Inventory(
            schema_version=1,
            role="node",
            install_dir="/opt/remnanode",
            compose_file="/opt/remnanode/docker-compose.yml",
            env_file=None,
            webserver="nginx",
            components={"nginx": Component("nginx", "remnawave-nginx")},
            xhttp_sockets=["/dev/shm/nginx.sock"],
        )

        with (
            mock.patch("remnawave_manager.lifecycle.wait_container"),
            mock.patch("remnawave_manager.lifecycle.test_nginx"),
            mock.patch("remnawave_manager.lifecycle.wait_for_paths") as sockets,
        ):
            manage_component(runner, current, "nginx", "restart")

        sockets.assert_called_once_with(["/dev/shm/nginx.sock"])

    def test_health_failure_is_returned_instead_of_false_success(self) -> None:
        runner = mock.Mock()
        current = inventory(Component("panel", "remnawave"))

        with (
            mock.patch(
                "remnawave_manager.lifecycle.wait_container",
                side_effect=TransactionError("unhealthy"),
            ),
            self.assertRaisesRegex(TransactionError, "unhealthy"),
        ):
            manage_component(runner, current, "panel", "restart")

    def test_dry_run_does_not_wait_for_runtime(self) -> None:
        runner = mock.Mock()
        runner.dry_run = True
        current = inventory(Component("panel", "remnawave"))

        with mock.patch(
            "remnawave_manager.lifecycle._verify_started_components"
        ) as verify:
            manage_component(runner, current, "panel", "start")

        verify.assert_not_called()

    def test_logs_command_uses_inventory_container_and_interactive_runner(self) -> None:
        runner = mock.Mock()
        current = inventory(Component("node", "remnanode", container="custom-node"))

        component_logs(runner, current, "node", tail=250, follow=True, since="1h30m")

        runner.interactive.assert_called_once_with(
            [
                "docker",
                "logs",
                "--tail",
                "250",
                "--since",
                "1h30m",
                "--follow",
                "custom-node",
            ]
        )

    def test_logs_rejects_unknown_component_and_unsafe_values(self) -> None:
        runner = mock.Mock()
        current = inventory(Component("node", "remnanode"))

        with self.assertRaises(ValidationError):
            component_logs(runner, current, "panel")
        with self.assertRaises(ValidationError):
            component_logs(runner, current, "node", tail=10_001)
        with self.assertRaises(ValidationError):
            component_logs(runner, current, "node", since="1h\n--follow")

        runner.interactive.assert_not_called()

    def test_since_accepts_supported_docker_formats(self) -> None:
        values = (
            "500ms",
            "1h30m",
            "1722600000.123456789",
            "2026-08-02",
            "2026-08-02T12:30:00Z",
            "2026-08-02T12:30:00.123456789+03:00",
        )

        self.assertEqual([validate_log_since(value) for value in values], list(values))

    def test_panel_cli_uses_panel_container_and_cli_binary(self) -> None:
        runner = mock.Mock()
        current = inventory(Component("panel", "remnawave", container="panel-backend"))

        panel_cli(runner, current)

        runner.interactive.assert_called_once_with(
            ["docker", "exec", "-it", "panel-backend", "cli"]
        )

    def test_status_handles_unexpected_docker_state_without_crashing(self) -> None:
        current = inventory(Component("panel", "remnawave"))
        for payload in ('["unexpected"]', '{"Health":"invalid"}', "not-json"):
            with self.subTest(payload=payload):
                runner = mock.Mock()
                runner.run.return_value = mock.Mock(returncode=0, stdout=payload)

                result = component_status(runner, current)

                self.assertEqual(
                    result,
                    [
                        {
                            "component": "panel",
                            "container": "remnawave",
                            "status": "неизвестно",
                            "health": None,
                        }
                    ],
                )

    def test_status_does_not_report_missing_containers_when_daemon_is_unavailable(
        self,
    ) -> None:
        runner = mock.Mock()
        runner.run.side_effect = (
            Result(("docker", "inspect"), 1, "", "cannot connect"),
            Result(("docker", "info"), 1, "", "cannot connect"),
        )

        with self.assertRaisesRegex(TransactionError, "Docker daemon недоступен"):
            component_status(runner, inventory(Component("panel", "remnawave")))

    def test_status_keeps_missing_state_when_daemon_probe_succeeds(self) -> None:
        runner = mock.Mock()
        runner.run.side_effect = (
            Result(("docker", "inspect"), 1, "", "no such container"),
            Result(("docker", "info"), 0, '"28.0.0"\n', ""),
        )

        result = component_status(runner, inventory(Component("panel", "remnawave")))

        self.assertEqual(result[0]["status"], "не создан")


if __name__ == "__main__":
    unittest.main()
