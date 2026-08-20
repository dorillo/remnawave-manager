from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remnawave_manager.errors import (
    NodeSecretValidationError,
    TransactionError,
    ValidationError,
)
from remnawave_manager.health import (
    _container_http_url,
    _missing_unix_sockets,
    check_node_runtime,
    check_panel_http,
    check_subscription_api_scopes,
    check_subscription_http,
    normalize_node_secret,
    validate_node_secret,
    wait_container,
    wait_node_runtime,
    wait_panel_http,
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


class NodeSecretValidationTests(unittest.TestCase):
    @staticmethod
    def payload() -> str:
        value = {
            "caCertPem": "sensitive-ca",
            "jwtPublicKey": "sensitive-jwt",
            "nodeCertPem": "sensitive-cert",
            "nodeKeyPem": "sensitive-key",
        }
        return base64.b64encode(json.dumps(value).encode()).decode()

    def test_secret_is_base64_decoded_from_stdin_in_isolated_image(self) -> None:
        secret = self.payload()
        runner = mock.Mock()
        runner.run.return_value = Result(
            ("docker", "run"), 0, "RWM_NODE_SECRET_OK\n", ""
        )

        validate_node_secret(runner, "remnawave/node:3.3.2@sha256:verified", secret)

        command = runner.run.call_args.args[0]
        options = runner.run.call_args.kwargs
        self.assertEqual(command[:3], ["docker", "run", "--rm"])
        self.assertIn("--read-only", command)
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertEqual(command[command.index("--cap-drop") + 1], "ALL")
        self.assertIn("no-new-privileges", command)
        self.assertIn("JSON.parse(Buffer.from(secret, 'base64')", command[-1])
        self.assertNotIn(secret, " ".join(command))
        self.assertEqual(options["input_text"], secret)
        self.assertTrue(options["sensitive"])
        self.assertNotIn("env", options)

    def test_node_rejection_is_distinguished_from_preflight_failure(self) -> None:
        runner = mock.Mock()
        runner.run.return_value = Result(
            ("docker", "run"),
            42,
            "",
            "RWM_NODE_SECRET_INVALID:node-key\n",
        )

        with self.assertRaisesRegex(NodeSecretValidationError, "приватный ключ"):
            validate_node_secret(runner, "remnawave/node:3.3.2", self.payload())

    def test_preflight_launch_failure_is_not_reported_as_invalid_key(self) -> None:
        runner = mock.Mock()
        runner.run.return_value = Result(("docker", "run"), 125, "", "docker unavailable")

        with self.assertRaisesRegex(TransactionError, "ошибка запуска preflight"):
            validate_node_secret(runner, "remnawave/node:3.3.2", self.payload())

    def test_rejects_control_characters_without_running_image(self) -> None:
        runner = mock.Mock()

        with self.assertRaisesRegex(ValidationError, "небезопасный формат"):
            validate_node_secret(runner, "remnawave/node:3.3.2", "secret\n")

        runner.run.assert_not_called()

    def test_rejects_a_non_node_key_without_running_image(self) -> None:
        runner = mock.Mock()

        with self.assertRaisesRegex(NodeSecretValidationError, "не является SECRET_KEY"):
            validate_node_secret(
                runner,
                "remnawave/node:3.3.2",
                "ordinary-api-token",
            )

        runner.run.assert_not_called()

    def test_normalizes_compose_assignment_and_api_response(self) -> None:
        payload = "eyJjYUNlcnRQZW0iOiJzZW5zaXRpdmUifQ=="

        self.assertEqual(
            normalize_node_secret(f'  - SECRET_KEY="{payload}"  '),
            payload,
        )
        self.assertEqual(
            normalize_node_secret(json.dumps({"response": {"secretKey": payload}})),
            payload,
        )

    def test_validation_passes_normalized_payload_to_isolated_image(self) -> None:
        payload = self.payload()
        runner = mock.Mock()
        runner.run.return_value = Result(
            ("docker", "run"), 0, "RWM_NODE_SECRET_OK\n", ""
        )

        validate_node_secret(
            runner,
            "remnawave/node:3.3.2",
            f"SECRET_KEY={payload}",
        )

        self.assertEqual(runner.run.call_args.kwargs["input_text"], payload)


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

    def test_v8_health_runs_inside_container_loopback(self) -> None:
        runner = mock.Mock()
        runner.run.return_value = Result(("curl",), 0, "", "")
        with mock.patch(
            "remnawave_manager.health._container_http_url",
            return_value="http://127.0.0.1:3010/internal/health",
        ) as endpoint:
            check_subscription_http(runner, self.component, timeout=0)

        endpoint.assert_called_once_with(
            runner,
            self.component,
            default_port=3010,
            path="/internal/health",
            container_loopback=True,
        )
        command = runner.run.call_args.args[0]
        self.assertEqual(
            command[:4],
            ["docker", "exec", "remnawave-subscription-page", "curl"],
        )

    def test_legacy_726_accepts_expected_root_not_found_response(self) -> None:
        runner = SequenceRunner([0], ["404"])
        with mock.patch(
            "remnawave_manager.health._container_http_url",
            return_value="http://127.0.0.1:13010/",
        ):
            check_subscription_http(
                runner, self.component, timeout=0, legacy=True  # type: ignore[arg-type]
            )

    def test_legacy_726_accepts_root_redirect_response(self) -> None:
        runner = SequenceRunner([0], ["302"])
        with mock.patch(
            "remnawave_manager.health._container_http_url",
            return_value="http://127.0.0.1:13010/",
        ):
            check_subscription_http(
                runner, self.component, timeout=0, legacy=True  # type: ignore[arg-type]
            )

    def test_legacy_726_accepts_intentional_socket_close(self) -> None:
        runner = SequenceRunner([52], ["000"])
        with mock.patch(
            "remnawave_manager.health._container_http_url",
            return_value="http://127.0.0.1:13010/",
        ):
            check_subscription_http(
                runner, self.component, timeout=0, legacy=True  # type: ignore[arg-type]
            )

    def test_legacy_726_rejects_missing_or_server_error_response(self) -> None:
        for returncode, status in ((7, "000"), (0, "500"), (0, "not-http")):
            with self.subTest(returncode=returncode, status=status):
                runner = SequenceRunner([returncode], [status])
                with (
                    mock.patch(
                        "remnawave_manager.health._container_http_url",
                        return_value="http://127.0.0.1:13010/",
                    ),
                    self.assertRaisesRegex(TransactionError, "liveness"),
                ):
                    check_subscription_http(
                        runner,
                        self.component,  # type: ignore[arg-type]
                        timeout=0,
                        legacy=True,
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
        runner = mock.Mock()
        runner.run.side_effect = [
            Result(("docker", "exec"), 0, str(status), "")
            for status in statuses
        ]
        return runner

    def test_probes_from_subscription_runtime_without_exporting_token(self) -> None:
        runner = self.runner([200, 404, 200, 404, 404, 400])
        check_subscription_api_scopes(runner, self.panel, self.subscription)

        self.assertEqual(runner.run.call_count, 6)
        for call in runner.run.call_args_list:
            command = call.args[0]
            self.assertEqual(
                command[:6],
                [
                    "docker",
                    "exec",
                    "remnawave-subscription-page",
                    "node",
                    "--input-type=module",
                    "--eval",
                ],
            )
            self.assertNotIn("header.payload.signature", command)
            self.assertNotIn("--request", command)
            self.assertNotIn("input_text", call.kwargs)
            self.assertIn("process.env.REMNAWAVE_API_TOKEN", command[6])
            self.assertIn("process.env.REMNAWAVE_PANEL_URL", command[6])
            self.assertIn("AbortSignal.timeout(15_000)", command[6])
            self.assertIn("headers['X-Forwarded-For'] = '127.0.0.1'", command[6])
            self.assertIn("headers['X-Forwarded-Proto'] = 'https'", command[6])
            self.assertIn("process.env.CADDY_AUTH_API_TOKEN", command[6])
            self.assertIn("process.env.CLOUDFLARE_ZERO_TRUST_CLIENT_ID", command[6])
            self.assertIn("process.env.EGAMES_COOKIE", command[6])
            self.assertTrue(command[-1].startswith("/api/"))
            self.assertTrue(call.kwargs["sensitive"])

    def test_reports_each_missing_scope(self) -> None:
        runner = self.runner([200, 403, 200, 403, 404, 400])
        with self.assertRaisesRegex(
            TransactionError,
            "users:by-username.*subscription-page-configs:get",
        ):
            check_subscription_api_scopes(runner, self.panel, self.subscription)

    def test_reports_safe_probe_exit_code_without_runtime_output(self) -> None:
        runner = mock.Mock()
        runner.run.return_value = Result(
            ("docker", "exec"),
            6,
            "000",
            "fetch failed with secret detail",
        )

        with self.assertRaisesRegex(TransactionError, r"system:metadata \(код probe 6\)") as raised:
            check_subscription_api_scopes(runner, self.panel, self.subscription)

        self.assertNotIn("secret detail", str(raised.exception))


class ContainerHttpEndpointTests(unittest.TestCase):
    def test_container_loopback_uses_effective_port_without_published_binding(self) -> None:
        runner = mock.Mock()
        runner.run.return_value = Result(
            ("docker", "inspect"),
            0,
            json.dumps(
                {
                    "Config": {"Env": ["APP_PORT=4321"]},
                    "HostConfig": {"NetworkMode": "bridge"},
                    "NetworkSettings": {"Ports": {}},
                }
            ),
            "",
        )

        url = _container_http_url(
            runner,
            Component("subscription", "subscription", "subscription"),
            default_port=3010,
            path="/internal/health",
            container_loopback=True,
        )

        self.assertEqual(url, "http://127.0.0.1:4321/internal/health")

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

    def test_retries_until_panel_api_is_ready_after_metrics_health(self) -> None:
        component = Component("panel", "remnawave", "remnawave")
        runner = SequenceRunner(
            [7, 0],
            [
                "",
                json.dumps(
                    {
                        "response": {
                            "isRegisterAllowed": False,
                            "isLoginAllowed": True,
                        }
                    }
                ),
            ],
        )
        with (
            mock.patch(
                "remnawave_manager.health._container_http_url",
                return_value="http://127.0.0.1:3000/api/auth/status",
            ),
            mock.patch(
                "remnawave_manager.health.time.monotonic",
                side_effect=[0.0, 0.0],
            ),
            mock.patch("remnawave_manager.health.time.sleep") as sleep,
        ):
            wait_panel_http(runner, component, timeout=90)  # type: ignore[arg-type]

        self.assertEqual(runner.calls, 2)
        sleep.assert_called_once_with(3)

    def test_panel_readiness_timeout_preserves_last_error(self) -> None:
        component = Component("panel", "remnawave", "remnawave")
        runner = SequenceRunner([7], [""])
        with (
            mock.patch(
                "remnawave_manager.health._container_http_url",
                return_value="http://127.0.0.1:3000/api/auth/status",
            ),
            mock.patch(
                "remnawave_manager.health.time.monotonic",
                side_effect=[0.0, 1.0],
            ),
            self.assertRaisesRegex(TransactionError, "не стала готова"),
        ):
            wait_panel_http(runner, component, timeout=0)  # type: ignore[arg-type]


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
