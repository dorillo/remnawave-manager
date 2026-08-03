from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remnawave_manager.errors import TransactionError
from remnawave_manager.health import (
    _container_http_url,
    _missing_unix_sockets,
    check_node_runtime,
    check_panel_http,
    check_subscription_api_scopes,
    check_subscription_http,
    wait_container,
    wait_node_runtime,
)
from remnawave_manager.models import Component, Inventory
from remnawave_manager.runner import Result


class SequenceRunner:
    def __init__(
        self, returncodes: list[int], stdouts: list[str] | None = None
    ) -> None:
        self.returncodes = iter(returncodes)
        self.stdouts = iter(stdouts or [""] * len(returncodes))
        self.calls = 0

    def run(self, args, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        code = next(self.returncodes)
        return Result(
            tuple(args), code, next(self.stdouts), "not ready" if code else ""
        )


class ContainerWaitTests(unittest.TestCase):
    def test_sanitizes_external_state_error(self) -> None:
        runner = mock.Mock()
        runner.run.side_effect = [
            Result(
                ("docker", "inspect"),
                0,
                json.dumps(
                    {
                        "Running": False,
                        "Status": "exited",
                        "Error": "failed\x1b[31m\rspoof\u202ehidden",
                    }
                ),
                "",
            ),
            Result(("docker", "logs"), 0, "", ""),
        ]

        with self.assertRaisesRegex(
            TransactionError,
            "failed \\[31m spoof hidden",
        ) as raised:
            wait_container(runner, Component("node", "node", "node"), timeout=30)

        message = str(raised.exception)
        self.assertNotIn("\x1b", message)
        self.assertNotIn("\r", message)
        self.assertNotIn("\u202e", message)


class SubscriptionHealthTests(unittest.TestCase):
    component = Component(
        "subscription",
        "remnawave-subscription-page",
        "remnawave-subscription-page",
    )

    def test_retries_until_legacy_subscription_endpoint_is_ready(self) -> None:
        runner = SequenceRunner([7, 0])
        with (
            mock.patch(
                "remnawave_manager.health._container_http_url",
                return_value="http://127.0.0.1:13010/internal/health",
            ),
            mock.patch(
                "remnawave_manager.health.time.monotonic", side_effect=[0.0, 0.0]
            ),
            mock.patch("remnawave_manager.health.time.sleep") as sleep,
        ):
            check_subscription_http(runner, self.component, timeout=90)  # type: ignore[arg-type]

        self.assertEqual(runner.calls, 2)
        sleep.assert_called_once_with(3)

    def test_reports_failure_when_retry_deadline_expires(self) -> None:
        runner = SequenceRunner([7])
        with (
            mock.patch(
                "remnawave_manager.health._container_http_url",
                return_value="http://127.0.0.1:13010/internal/health",
            ),
            mock.patch(
                "remnawave_manager.health.time.monotonic", side_effect=[0.0, 0.0]
            ),
            self.assertRaises(TransactionError),
        ):
            check_subscription_http(runner, self.component, timeout=0)  # type: ignore[arg-type]

    def test_legacy_726_accepts_expected_root_not_found_response(self) -> None:
        runner = SequenceRunner([0], ["404"])
        with mock.patch(
            "remnawave_manager.health._container_http_url",
            return_value="http://127.0.0.1:13010/",
        ):
            check_subscription_http(
                runner, self.component, timeout=0, legacy=True  # type: ignore[arg-type]
            )


class SubscriptionScopeTests(unittest.TestCase):
    panel = Component("panel", "remnawave", "remnawave")
    subscription = Component(
        "subscription",
        "remnawave-subscription-page",
        "remnawave-subscription-page",
    )

    @staticmethod
    def runner(statuses: list[int]) -> mock.Mock:
        token = "header.payload.signature"
        runner = mock.Mock()
        runner.run.side_effect = [
            Result(("docker", "exec"), 0, token + "\n", ""),
            *[Result(("curl",), 0, str(status), "") for status in statuses],
        ]
        return runner

    def test_accepts_all_required_read_only_scopes_without_exposing_token(self) -> None:
        runner = self.runner([200, 404, 200, 404, 404, 400])
        with mock.patch(
            "remnawave_manager.health._container_http_url",
            return_value="http://127.0.0.1:3000",
        ):
            check_subscription_api_scopes(runner, self.panel, self.subscription)

        self.assertEqual(runner.run.call_count, 7)
        for call in runner.run.call_args_list[1:]:
            command = call.args[0]
            self.assertNotIn("header.payload.signature", command)
            self.assertNotIn("--request", command)
            self.assertEqual(
                call.kwargs["input_text"],
                'header = "Authorization: Bearer header.payload.signature"\n',
            )
            self.assertTrue(call.kwargs["sensitive"])

    def test_reports_each_missing_scope(self) -> None:
        runner = self.runner([200, 403, 200, 403, 404, 400])
        with (
            mock.patch(
                "remnawave_manager.health._container_http_url",
                return_value="http://127.0.0.1:3000",
            ),
            self.assertRaisesRegex(
                TransactionError,
                "users:by-username.*subscription-page-configs:get",
            ),
        ):
            check_subscription_api_scopes(runner, self.panel, self.subscription)


class ContainerHttpEndpointTests(unittest.TestCase):
    def test_uses_effective_app_port_and_nonstandard_loopback_mapping(self) -> None:
        runner = mock.Mock()
        runner.run.return_value = Result(
            ("docker", "inspect"),
            0,
            json.dumps(
                {
                    "Config": {"Env": ["APP_PORT=4321"]},
                    "HostConfig": {"NetworkMode": "remnawave-network"},
                    "NetworkSettings": {
                        "Ports": {
                            "4321/tcp": [{"HostIp": "127.0.0.1", "HostPort": "14321"}]
                        }
                    },
                }
            ),
            "",
        )

        url = _container_http_url(
            runner,
            Component("panel", "remnawave", "remnawave"),
            default_port=3000,
            path="/api/auth/status",
        )

        self.assertEqual(url, "http://127.0.0.1:14321/api/auth/status")

    def test_host_network_uses_effective_app_port(self) -> None:
        runner = mock.Mock()
        runner.run.return_value = Result(
            ("docker", "inspect"),
            0,
            json.dumps(
                {
                    "Config": {"Env": ["APP_PORT=4321"]},
                    "HostConfig": {"NetworkMode": "host"},
                    "NetworkSettings": {"Ports": {}},
                }
            ),
            "",
        )

        url = _container_http_url(
            runner,
            Component("panel", "remnawave", "remnawave"),
            default_port=3000,
            path="/api/auth/status",
        )

        self.assertEqual(url, "http://127.0.0.1:4321/api/auth/status")

    def test_rejects_container_without_ipv4_local_binding(self) -> None:
        runner = mock.Mock()
        runner.run.return_value = Result(
            ("docker", "inspect"),
            0,
            json.dumps(
                {
                    "Config": {"Env": ["APP_PORT=3000"]},
                    "HostConfig": {"NetworkMode": "bridge"},
                    "NetworkSettings": {
                        "Ports": {
                            "3000/tcp": [{"HostIp": "192.0.2.10", "HostPort": "3000"}]
                        }
                    },
                }
            ),
            "",
        )

        with self.assertRaisesRegex(TransactionError, "IPv4 localhost"):
            _container_http_url(
                runner,
                Component("panel", "remnawave", "remnawave"),
                default_port=3000,
                path="/api/auth/status",
            )


class PanelHealthTests(unittest.TestCase):
    def test_requires_exact_closed_registration_and_enabled_login(self) -> None:
        component = Component("panel", "remnawave", "remnawave")
        invalid = (
            {},
            {"isRegisterAllowed": None, "isLoginAllowed": True},
            {"isRegisterAllowed": False, "isLoginAllowed": None},
            {"isRegisterAllowed": True, "isLoginAllowed": True},
        )
        for response in invalid:
            with self.subTest(response=response):
                runner = mock.Mock()
                runner.run.return_value = Result(
                    ("curl",),
                    0,
                    json.dumps({"response": response}),
                    "",
                )
                with (
                    mock.patch(
                        "remnawave_manager.health._container_http_url",
                        return_value="http://127.0.0.1:3000/api/auth/status",
                    ),
                    self.assertRaises(TransactionError),
                ):
                    check_panel_http(runner, component)

    def test_accepts_safe_auth_state_and_bypasses_environment_proxy(self) -> None:
        component = Component("panel", "remnawave", "remnawave")
        runner = mock.Mock()
        runner.run.return_value = Result(
            ("curl",),
            0,
            json.dumps(
                {
                    "response": {
                        "isRegisterAllowed": False,
                        "isLoginAllowed": True,
                    }
                }
            ),
            "",
        )
        with mock.patch(
            "remnawave_manager.health._container_http_url",
            return_value="http://127.0.0.1:3000/api/auth/status",
        ):
            check_panel_http(runner, component)

        command = runner.run.call_args.args[0]
        self.assertIn("--noproxy", command)
        self.assertIn("--max-filesize", command)


class UnixSocketHealthTests(unittest.TestCase):
    def test_regular_file_does_not_satisfy_xhttp_socket_check(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "stale.sock"
            path.write_text("not a socket", encoding="utf-8")

            self.assertEqual(_missing_unix_sockets([str(path)]), [str(path)])


class NodeRuntimeHealthTests(unittest.TestCase):
    def inventory(self) -> Inventory:
        return Inventory(
            schema_version=1,
            role="node",
            install_dir="/opt/remnanode",
            compose_file="/opt/remnanode/docker-compose.yml",
            env_file="/opt/remnanode/.env",
            webserver="nginx",
            components={"node": Component("node", "remnanode", "remnanode")},
        )

    def test_retries_until_s6_xray_and_config_are_ready(self) -> None:
        runner = SequenceRunner(
            [1, 0, 0, 0],
            ["", "true 321\n", "Xray 26.7.28\n", "{}\n"],
        )
        with (
            mock.patch(
                "remnawave_manager.health.time.monotonic", side_effect=[0.0, 0.0]
            ),
            mock.patch("remnawave_manager.health.time.sleep") as sleep,
        ):
            wait_node_runtime(runner, self.inventory(), timeout=90)  # type: ignore[arg-type]

        self.assertEqual(runner.calls, 4)
        sleep.assert_called_once_with(3)

    def test_reports_failure_after_runtime_deadline(self) -> None:
        runner = SequenceRunner([1])
        with (
            mock.patch(
                "remnawave_manager.health.time.monotonic", side_effect=[0.0, 0.0]
            ),
            self.assertRaisesRegex(TransactionError, "не стала готова"),
        ):
            wait_node_runtime(runner, self.inventory(), timeout=0)  # type: ignore[arg-type]

    def test_rejects_s6_service_that_is_supervised_but_down(self) -> None:
        runner = SequenceRunner([0], ["false 0\n"])

        with self.assertRaisesRegex(TransactionError, "не находится в состоянии up"):
            check_node_runtime(runner, self.inventory())  # type: ignore[arg-type]

    def test_rejects_invalid_dumped_xray_json(self) -> None:
        runner = SequenceRunner(
            [0, 0, 0],
            ["true 321\n", "Xray 26.7.28\n", "not-json\n"],
        )

        with self.assertRaisesRegex(TransactionError, "некорректный Xray JSON"):
            check_node_runtime(runner, self.inventory())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
