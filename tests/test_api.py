from __future__ import annotations

import importlib
import io
import json
import os
import sys
import tempfile
import traceback
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import Self
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import remnawave_manager.api as api_module
from remnawave_manager.api import (
    REALITY_RECOVERY_NAME,
    SUBSCRIPTION_SCOPES,
    RemnawaveApi,
    _domain_or_address,
    _NoRedirectHandler,
    _profile_config,
    build_reality_config,
    complete_reality_credentials_handoff,
    configure_warp_routing,
    provision_reality_node,
    validate_admin_password,
)
from remnawave_manager.errors import TransactionError, ValidationError
from remnawave_manager.paths import RuntimePaths
from remnawave_manager.state import StateStore


class FakeResponse:
    def __init__(self, status: int, payload: object | bytes | None = None) -> None:
        self.status = status
        if payload is None:
            self.payload = b""
        elif isinstance(payload, bytes):
            self.payload = payload
        else:
            self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, _limit: int = -1) -> bytes:
        return self.payload


PROFILE_UUID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
INBOUND_UUID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
NODE_UUID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
HOST_UUID = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
SQUAD_ONE_UUID = "11111111-1111-4111-8111-111111111111"
SQUAD_TWO_UUID = "22222222-2222-4222-8222-222222222222"
EXISTING_ONE_UUID = "33333333-3333-4333-8333-333333333333"
EXISTING_TWO_UUID = "44444444-4444-4444-8444-444444444444"
PARALLEL_UUID = "55555555-5555-4555-8555-555555555555"


class AdminPasswordValidationTests(unittest.TestCase):
    def test_accepts_strong_password(self) -> None:
        validate_admin_password("A" + "a" * 22 + "1")

    def test_rejects_non_string_control_characters_and_oversized_values(self) -> None:
        invalid = [
            None,
            "A" + "a" * 21 + "1\n",
            "A" + "a" * 22 + "1\ud800",
            "A" + "a" * 21 + "1\u202e",
            "A" + "a" * 1023 + "1",
        ]
        for value in invalid:
            with (
                self.subTest(
                    value_type=type(value).__name__,
                    length=len(value) if isinstance(value, str) else None,
                ),
                self.assertRaises(ValidationError),
            ):
                validate_admin_password(value)  # type: ignore[arg-type]


class AdminAuthenticationValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = RemnawaveApi("https://panel.example.test")

    def test_registration_requires_strong_password(self) -> None:
        with (
            mock.patch.object(
                self.api,
                "auth_status",
                return_value={"isRegisterAllowed": True},
            ),
            mock.patch.object(self.api, "request") as request,
            self.assertRaises(ValidationError),
        ):
            self.api.register_or_login("admin", "legacy-password")

        request.assert_not_called()

    def test_login_accepts_existing_legacy_password(self) -> None:
        with (
            mock.patch.object(
                self.api,
                "auth_status",
                return_value={"isLoginAllowed": True},
            ),
            mock.patch.object(
                self.api,
                "request",
                return_value={"response": {"accessToken": "admin-token"}},
            ) as request,
        ):
            token = self.api.register_or_login("admin", "legacy-password")

        self.assertEqual(token, "admin-token")
        request.assert_called_once_with(
            "POST",
            "/api/auth/login",
            data={"username": "admin", "password": "legacy-password"},
            expected=(200,),
        )

    def test_invalid_login_credentials_are_rejected_before_api_probe(self) -> None:
        invalid = (
            ("ad", "legacy-password"),
            ("admin", ""),
            ("admin", "legacy\npassword"),
            ("admin", "legacy\u202epassword"),
        )
        with mock.patch.object(self.api, "auth_status") as auth_status:
            for username, password in invalid:
                with self.subTest(username=username, password=password), self.assertRaises(
                    ValidationError
                ):
                    self.api.register_or_login(username, password)

        auth_status.assert_not_called()


class FakeProvisioningApi:
    def __init__(self, *, fail_squad: str | None = None) -> None:
        self.squads = {
            SQUAD_ONE_UUID: [EXISTING_ONE_UUID],
            SQUAD_TWO_UUID: [EXISTING_TWO_UUID],
        }
        self.fail_squad = fail_squad
        self.failed = False
        self.get_calls = {SQUAD_ONE_UUID: 0, SQUAD_TWO_UUID: 0}
        self.updates: list[tuple[str, list[str]]] = []
        self.deleted: list[tuple[str, str]] = []

    def generate_x25519_private_key(self, _token: str) -> str:
        return "private-key"

    def create_config_profile(
        self, _token: str, _name: str, _config: dict[str, object]
    ) -> tuple[str, str]:
        return PROFILE_UUID, INBOUND_UUID

    def create_node(self, _token: str, **_kwargs: object) -> str:
        return NODE_UUID

    def create_host(self, _token: str, **_kwargs: object) -> str:
        return HOST_UUID

    def keygen(self, _token: str) -> str:
        return "node-secret"

    def list_internal_squad_uuids(self, _token: str) -> tuple[str, ...]:
        return tuple(self.squads)

    def get_internal_squad_inbounds(
        self, _token: str, squad_uuid: str
    ) -> tuple[str, ...]:
        self.get_calls[squad_uuid] += 1
        if (
            self.fail_squad is None
            and squad_uuid == SQUAD_ONE_UUID
            and self.get_calls[squad_uuid] == 2
        ):
            self.squads[squad_uuid].append(PARALLEL_UUID)
        if (
            self.failed
            and squad_uuid == SQUAD_ONE_UUID
            and PARALLEL_UUID not in self.squads[squad_uuid]
        ):
            self.squads[squad_uuid].append(PARALLEL_UUID)
        return tuple(self.squads[squad_uuid])

    def update_internal_squad_inbounds(
        self, _token: str, squad_uuid: str, inbounds: list[str]
    ) -> tuple[str, ...]:
        self.updates.append((squad_uuid, list(inbounds)))
        if squad_uuid == self.fail_squad and not self.failed:
            self.failed = True
            raise RuntimeError("second squad update failed")
        self.squads[squad_uuid] = list(inbounds)
        return tuple(inbounds)

    def delete(self, _token: str, resource: str, item_uuid: str) -> None:
        self.deleted.append((resource, item_uuid))


class FakeRoutingApi:
    def __init__(self, config: dict[str, object]) -> None:
        self.config = config
        self.patches: list[dict[str, object]] = []

    def request(self, method: str, path: str, **kwargs: object) -> dict[str, object]:
        if method == "GET":
            return {"response": {"config": json.loads(json.dumps(self.config))}}
        if method == "PATCH":
            self.patches.append(kwargs["data"])  # type: ignore[arg-type]
            self.config = json.loads(
                json.dumps(kwargs["data"]["config"])  # type: ignore[index]
            )
            return {"response": {}}
        raise AssertionError((method, path))


class RemnawaveApiHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.api = RemnawaveApi("https://panel.example.test", timeout=7)

    def test_accepts_explicit_success_status_and_sends_json(self) -> None:
        response = FakeResponse(201, {"response": {"uuid": "created"}})
        with mock.patch(
            "remnawave_manager.api._open_api_request", return_value=response
        ) as open_request:
            payload = self.api.request(
                "POST",
                "/api/example",
                token="admin-token",
                data={"name": "node"},
                expected=(201,),
            )

        self.assertEqual(payload, {"response": {"uuid": "created"}})
        request = open_request.call_args.args[0]
        self.assertEqual(open_request.call_args.kwargs, {"timeout": 7})
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer admin-token")
        self.assertIsNone(request.get_header("X-forwarded-for"))
        self.assertIsNone(request.get_header("X-forwarded-proto"))
        self.assertEqual(json.loads(request.data), {"name": "node"})

    def test_global_opener_disables_environment_proxies_and_redirects(self) -> None:
        proxy_environment = {
            "HTTP_PROXY": "http://proxy.example.test:8080",
            "HTTPS_PROXY": "http://proxy.example.test:8080",
            "NO_PROXY": "",
        }
        with (
            mock.patch.dict("os.environ", proxy_environment, clear=False),
            mock.patch.object(
                urllib.request,
                "build_opener",
                wraps=urllib.request.build_opener,
            ) as build_opener,
        ):
            importlib.reload(api_module)

        handlers = build_opener.call_args.args
        self.assertEqual(len(handlers), 2)
        self.assertIsInstance(handlers[0], urllib.request.ProxyHandler)
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], api_module._NoRedirectHandler)

    def test_request_rejects_ambiguous_paths_methods_tokens_and_statuses(self) -> None:
        invalid_calls = (
            {"method": "TRACE", "path": "/api/example"},
            {"method": "GET", "path": "/api/../admin"},
            {"method": "GET", "path": "/api/%2e%2e/admin"},
            {"method": "GET", "path": "/api/%252e%252e/admin"},
            {"method": "GET", "path": "/api/example?redirect=https://evil.test"},
            {"method": "GET", "path": "/api/example#fragment"},
            {"method": "GET", "path": "/api/example\nheader"},
            {"method": "GET", "path": "/api/example\u202e"},
            {"method": "GET", "path": "/api/example name"},
            {"method": "GET", "path": "/api/example", "token": ""},
            {"method": "GET", "path": "/api/example", "data": []},
            {"method": "GET", "path": "/api/example", "data": {"value": float("nan")}},
            {"method": "GET", "path": "/api/example", "expected": ()},
            {"method": "GET", "path": "/api/example", "expected": (True,)},
        )

        with mock.patch("remnawave_manager.api._open_api_request") as open_request:
            for arguments in invalid_calls:
                with (
                    self.subTest(arguments=arguments),
                    self.assertRaises(ValidationError),
                ):
                    self.api.request(**arguments)  # type: ignore[arg-type]
        open_request.assert_not_called()

    def test_loopback_request_sets_required_reverse_proxy_headers(self) -> None:
        api = RemnawaveApi("http://127.0.0.1:3000")
        with mock.patch(
            "remnawave_manager.api._open_api_request",
            return_value=FakeResponse(200, {"response": {}}),
        ) as open_request:
            api.request("GET", "/api/example")

        request = open_request.call_args.args[0]
        self.assertEqual(request.get_header("X-forwarded-for"), "127.0.0.1")
        self.assertEqual(request.get_header("X-forwarded-proto"), "https")

    def test_rejects_status_not_in_explicit_expected_set(self) -> None:
        with (
            mock.patch(
                "remnawave_manager.api._open_api_request",
                return_value=FakeResponse(200, {"response": {}}),
            ),
            self.assertRaisesRegex(TransactionError, "HTTP 200"),
        ):
            self.api.request("POST", "/api/example", expected=(201,))

    def test_reports_http_error_status_and_api_message(self) -> None:
        error = urllib.error.HTTPError(
            "https://panel.example.test/api/example",
            403,
            "Forbidden",
            {},
            io.BytesIO(json.dumps({"message": ["scope denied", "read only"]}).encode()),
        )
        self.addCleanup(error.close)
        with (
            mock.patch("remnawave_manager.api._open_api_request", side_effect=error),
            self.assertRaisesRegex(
                TransactionError,
                r"HTTP 403.*scope denied; read only",
            ),
        ):
            self.api.request("GET", "/api/example")

    def test_http_error_response_is_closed_after_reading(self) -> None:
        error = urllib.error.HTTPError(
            "https://panel.example.test/api/example",
            503,
            "Unavailable",
            {},
            io.BytesIO(b'{"message":"maintenance"}'),
        )
        with (
            mock.patch.object(error, "close", wraps=error.close) as close,
            mock.patch(
                "remnawave_manager.api._open_api_request",
                side_effect=error,
            ),
            self.assertRaises(TransactionError),
        ):
            self.api.request("GET", "/api/example")

        close.assert_called_once_with()

    def test_deeply_nested_success_response_is_reported_without_recursion_error(
        self,
    ) -> None:
        with (
            mock.patch(
                "remnawave_manager.api._open_api_request",
                return_value=FakeResponse(200, b"{}"),
            ),
            mock.patch(
                "remnawave_manager.api.json.loads",
                side_effect=RecursionError("too deeply nested"),
            ),
            self.assertRaisesRegex(TransactionError, "не-JSON"),
        ):
            self.api.request("GET", "/api/example")

    def test_deeply_nested_error_response_does_not_escape_as_recursion_error(
        self,
    ) -> None:
        error = urllib.error.HTTPError(
            "https://panel.example.test/api/example",
            500,
            "Internal Server Error",
            {},
            io.BytesIO(b"{}"),
        )
        with (
            mock.patch(
                "remnawave_manager.api._open_api_request",
                side_effect=error,
            ),
            mock.patch(
                "remnawave_manager.api.json.loads",
                side_effect=RecursionError("too deeply nested"),
            ),
            self.assertRaisesRegex(TransactionError, "HTTP 500"),
        ):
            self.api.request("GET", "/api/example")

    def test_deeply_nested_embedded_profile_config_is_controlled(self) -> None:
        with (
            mock.patch(
                "remnawave_manager.api.json.loads",
                side_effect=RecursionError("too deeply nested"),
            ),
            self.assertRaisesRegex(TransactionError, "некорректный JSON"),
        ):
            _profile_config({"response": {"config": "{}"}}, "profile")

    def test_sanitizes_control_characters_in_api_error_message(self) -> None:
        error = urllib.error.HTTPError(
            "https://panel.example.test/api/example",
            400,
            "Bad Request",
            {},
            io.BytesIO(json.dumps({"message": "bad\n\x1b[31mmessage\u202e"}).encode()),
        )
        self.addCleanup(error.close)
        with (
            mock.patch(
                "remnawave_manager.api._open_api_request",
                side_effect=error,
            ),
            self.assertRaises(TransactionError) as raised,
        ):
            self.api.request("GET", "/api/example")

        self.assertIn("bad [31mmessage", str(raised.exception))
        self.assertNotIn("\n", str(raised.exception))
        self.assertNotIn("\x1b", str(raised.exception))
        self.assertNotIn("\u202e", str(raised.exception))

    def test_api_errors_redact_request_tokens_passwords_and_private_keys(self) -> None:
        token = "admin-token-that-must-not-leak"
        password = "Password-that-must-not-leak-123"
        private_key = "private-key-that-must-not-leak"
        message = f"invalid {token}; password={password}; private={private_key}"
        error = urllib.error.HTTPError(
            "https://panel.example.test/api/example",
            400,
            "Bad Request",
            {},
            io.BytesIO(json.dumps({"message": message}).encode()),
        )
        self.addCleanup(error.close)

        with (
            mock.patch(
                "remnawave_manager.api._open_api_request",
                side_effect=error,
            ),
            self.assertRaises(TransactionError) as raised,
        ):
            self.api.request(
                "POST",
                "/api/example",
                token=token,
                data={
                    "password": password,
                    "config": {"privateKey": private_key},
                },
            )

        text = str(raised.exception)
        self.assertNotIn(token, text)
        self.assertNotIn(password, text)
        self.assertNotIn(private_key, text)
        self.assertGreaterEqual(text.count("<скрыто>"), 3)

    def test_network_errors_are_redacted_and_terminal_sanitized(self) -> None:
        token = "admin-token-that-must-not-leak"
        reason = f"offline\x1b[31m\rspoof\u202e token={token}"
        with (
            mock.patch(
                "remnawave_manager.api._open_api_request",
                side_effect=urllib.error.URLError(reason),
            ),
            self.assertRaises(TransactionError) as raised,
        ):
            self.api.request("GET", "/api/example", token=token)

        text = str(raised.exception)
        self.assertNotIn(token, text)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\r", text)
        self.assertNotIn("\u202e", text)
        formatted = "".join(traceback.format_exception(raised.exception))
        self.assertNotIn(token, formatted)

    def test_success_response_rejects_nonstandard_json_constants(self) -> None:
        with (
            mock.patch(
                "remnawave_manager.api._open_api_request",
                return_value=FakeResponse(200, b'{"response": NaN}'),
            ),
            self.assertRaisesRegex(TransactionError, "не-JSON"),
        ):
            self.api.request("GET", "/api/example")

    def test_keygen_accepts_v3_secret_key_and_v2_pub_key(self) -> None:
        variants = (("secretKey", "v3-secret"), ("pubKey", "v2-key"))
        for field, expected in variants:
            with (
                self.subTest(field=field),
                mock.patch(
                    "remnawave_manager.api._open_api_request",
                    return_value=FakeResponse(200, {"response": {field: expected}}),
                ),
            ):
                self.assertEqual(self.api.keygen("admin-token"), expected)

    def test_rejects_malformed_secrets_returned_by_panel(self) -> None:
        responses = (
            ("keygen", {"response": {"secretKey": "bad\nkey"}}),
            ("x25519", {"response": {"keypairs": [{"privateKey": "bad key"}]}}),
        )
        for operation, payload in responses:
            with (
                self.subTest(operation=operation),
                mock.patch(
                    "remnawave_manager.api._open_api_request",
                    return_value=FakeResponse(200, payload),
                ),
                self.assertRaises(TransactionError),
            ):
                if operation == "keygen":
                    self.api.keygen("admin-token")
                else:
                    self.api.generate_x25519_private_key("admin-token")

    def test_subscription_token_uses_only_explicit_minimum_scopes(self) -> None:
        with mock.patch(
            "remnawave_manager.api._open_api_request",
            return_value=FakeResponse(
                201, {"response": {"token": "subscription-token"}}
            ),
        ) as open_request:
            token = self.api.create_subscription_token(
                "admin-token", name="subscription-page", expires_days=90
            )

        self.assertEqual(token, "subscription-token")
        request = open_request.call_args.args[0]
        body = json.loads(request.data)
        self.assertEqual(body["scopes"], SUBSCRIPTION_SCOPES)
        self.assertEqual(
            body["scopes"],
            [
                "system:metadata",
                "users:by-username",
                "subscription-page-configs:list",
                "subscription-page-configs:get",
                "subscriptions:by-short-uuid-protected",
                "subscriptions:subpage-config",
            ],
        )
        self.assertFalse(any("*" in scope for scope in body["scopes"]))

    def test_subscription_token_rejects_invalid_lifetime_and_name(self) -> None:
        for name, expires_days in (
            ("ok-name", True),
            ("bad\nname", 90),
            (" edge", 90),
            ("edge ", 90),
            ("\ud800bad", 90),
        ):
            with (
                self.subTest(name=name, expires_days=expires_days),
                self.assertRaises(ValidationError),
            ):
                self.api.create_subscription_token(
                    "admin-token",
                    name=name,
                    expires_days=expires_days,
                )

    def test_delete_accepts_empty_204_response(self) -> None:
        item_uuid = "01234567-89ab-cdef-0123-456789abcdef"
        with mock.patch(
            "remnawave_manager.api._open_api_request",
            return_value=FakeResponse(204),
        ) as open_request:
            result = self.api.delete("admin-token", "nodes", item_uuid)

        self.assertIsNone(result)
        request = open_request.call_args.args[0]
        self.assertEqual(request.get_method(), "DELETE")
        self.assertTrue(request.full_url.endswith("/api/nodes/" + item_uuid))

    def test_plain_http_is_limited_to_loopback(self) -> None:
        self.assertEqual(
            RemnawaveApi("http://127.0.0.1:3000/").base_url,
            "http://127.0.0.1:3000",
        )
        self.assertEqual(
            RemnawaveApi("http://[::1]:3000").base_url, "http://[::1]:3000"
        )
        with self.assertRaisesRegex(ValidationError, "используйте HTTPS"):
            RemnawaveApi("http://panel.example.test")

    def test_base_url_rejects_credentials_path_query_and_fragment(self) -> None:
        for value in (
            "https://user:secret@panel.example.test",
            "https://panel.example.test/api",
            "https://panel.example.test?target=other",
            "https://panel.example.test#fragment",
            "https://panel.example.test:",
            "https://panel example.test",
            "https://panel.example.test.",
            "http://[::1",
            " https://panel.example.test",
            "https://panel.example.test\n",
        ):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                RemnawaveApi(value)

        with self.assertRaises(ValidationError):
            RemnawaveApi(None)  # type: ignore[arg-type]

    def test_rejects_invalid_timeout_port_and_header_token_before_request(self) -> None:
        with self.assertRaises(ValidationError):
            RemnawaveApi("https://panel.example.test", timeout=0)
        with self.assertRaises(ValidationError):
            RemnawaveApi("https://panel.example.test:0")
        with (
            mock.patch("remnawave_manager.api._open_api_request") as open_request,
            self.assertRaisesRegex(ValidationError, "недопустимые символы"),
        ):
            self.api.request("GET", "/api/example", token="secret\r\nX-Leak: yes")
        open_request.assert_not_called()

    def test_http_redirect_is_not_followed(self) -> None:
        request = urllib.request.Request(
            "https://panel.example.test/api/example",
            headers={"Authorization": "Bearer admin-token"},
        )
        redirected = _NoRedirectHandler().redirect_request(
            request,
            None,
            302,
            "Found",
            {},
            "https://attacker.example/api/capture",
        )
        self.assertIsNone(redirected)

    def test_internal_squad_v3_list_get_and_patch_contract(self) -> None:
        squad_uuid = "11111111-1111-4111-8111-111111111111"
        first_inbound = "22222222-2222-4222-8222-222222222222"
        second_inbound = "33333333-3333-4333-8333-333333333333"

        def squad(inbounds: list[str]) -> dict[str, object]:
            return {
                "uuid": squad_uuid,
                "inbounds": [{"uuid": item} for item in inbounds],
            }

        responses = [
            FakeResponse(
                200,
                {
                    "response": {
                        "total": 1,
                        "internalSquads": [squad([first_inbound])],
                    }
                },
            ),
            FakeResponse(200, {"response": squad([first_inbound])}),
            FakeResponse(
                200,
                {"response": squad([first_inbound, second_inbound])},
            ),
        ]
        with mock.patch(
            "remnawave_manager.api._open_api_request", side_effect=responses
        ) as open_request:
            self.assertEqual(
                self.api.list_internal_squad_uuids("admin-token"),
                (squad_uuid,),
            )
            self.assertEqual(
                self.api.get_internal_squad_inbounds("admin-token", squad_uuid),
                (first_inbound,),
            )
            self.assertEqual(
                self.api.update_internal_squad_inbounds(
                    "admin-token",
                    squad_uuid,
                    [first_inbound, second_inbound],
                ),
                (first_inbound, second_inbound),
            )

        requests = [item.args[0] for item in open_request.call_args_list]
        self.assertEqual(requests[0].get_method(), "GET")
        self.assertTrue(requests[0].full_url.endswith("/api/internal-squads"))
        self.assertEqual(requests[1].get_method(), "GET")
        self.assertTrue(
            requests[1].full_url.endswith("/api/internal-squads/" + squad_uuid)
        )
        self.assertEqual(requests[2].get_method(), "PATCH")
        self.assertTrue(requests[2].full_url.endswith("/api/internal-squads"))
        self.assertEqual(
            json.loads(requests[2].data),
            {
                "uuid": squad_uuid,
                "inbounds": [first_inbound, second_inbound],
            },
        )

    def test_create_host_uses_exact_v3_body_without_removed_fields(self) -> None:
        profile_uuid = "11111111-1111-4111-8111-111111111111"
        inbound_uuid = "22222222-2222-4222-8222-222222222222"
        host_uuid = "33333333-3333-4333-8333-333333333333"
        with mock.patch(
            "remnawave_manager.api._open_api_request",
            return_value=FakeResponse(201, {"response": {"uuid": host_uuid}}),
        ) as open_request:
            result = self.api.create_host(
                "admin-token",
                remark="Node One",
                address="node.example.com",
                profile_uuid=profile_uuid,
                inbound_uuid=inbound_uuid,
            )

        self.assertEqual(result, host_uuid)
        request = open_request.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertTrue(request.full_url.endswith("/api/hosts"))
        self.assertEqual(
            json.loads(request.data),
            {
                "inbound": {
                    "configProfileUuid": profile_uuid,
                    "configProfileInboundUuid": inbound_uuid,
                },
                "remark": "Node One",
                "address": "node.example.com",
                "port": 443,
                "sni": "node.example.com",
                "fingerprint": "chrome",
                "isDisabled": False,
                "securityLayer": "DEFAULT",
            },
        )

    def test_api_object_names_reject_control_characters_and_edge_whitespace(
        self,
    ) -> None:
        with mock.patch("remnawave_manager.api._open_api_request") as open_request:
            with self.assertRaises(ValidationError):
                self.api.create_config_profile("token", "bad\nname", {})
            with self.assertRaises(ValidationError):
                self.api.create_config_profile("token", " profile", {})
            with self.assertRaises(ValidationError):
                self.api.create_config_profile("token", "profile ", {})
            with self.assertRaises(ValidationError):
                self.api.create_config_profile("token", "profile\u202e", {})
            with self.assertRaises(ValidationError):
                self.api.create_node(
                    "token",
                    name=" Node One",
                    address="node.example.com",
                    profile_uuid=PROFILE_UUID,
                    inbound_uuid=INBOUND_UUID,
                )
            with self.assertRaises(ValidationError):
                self.api.create_host(
                    "token",
                    remark="Node\u0000One",
                    address="node.example.com",
                    profile_uuid=PROFILE_UUID,
                    inbound_uuid=INBOUND_UUID,
                )

        open_request.assert_not_called()

    def test_warp_routing_uses_ipv4_with_table_off_contract(self) -> None:
        original = {"outbounds": [], "routing": {"rules": []}}
        api = FakeRoutingApi(original)

        with tempfile.TemporaryDirectory() as temporary:
            configure_warp_routing(
                api,  # type: ignore[arg-type]
                "admin-token",
                StateStore(RuntimePaths(Path(temporary))),
                "01234567-89ab-cdef-0123-456789abcdef",
                ["example.com"],
            )

        config = api.patches[0]["config"]
        self.assertIsInstance(config, dict)
        outbound = config["outbounds"][0]  # type: ignore[index]
        self.assertEqual(outbound["settings"]["domainStrategy"], "UseIPv4")
        self.assertEqual(outbound["streamSettings"]["sockopt"]["interface"], "warp")

    def test_warp_routing_backup_contains_original_and_noop_creates_no_backup(
        self,
    ) -> None:
        original = {"outbounds": [], "routing": {"rules": []}}
        api = FakeRoutingApi(original)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(RuntimePaths(root))
            configure_warp_routing(
                api,  # type: ignore[arg-type]
                "admin-token",
                store,
                PROFILE_UUID,
                ["example.com"],
            )
            backups = list((store.paths.state / "api-backups").glob("*.json"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8")), original)

        managed = {
            "outbounds": [
                {
                    "tag": "RWM_WARP",
                    "protocol": "freedom",
                    "settings": {"domainStrategy": "UseIPv4"},
                    "streamSettings": {
                        "sockopt": {"interface": "warp", "tcpFastOpen": True}
                    },
                }
            ],
            "routing": {
                "rules": [
                    {
                        "type": "field",
                        "domain": ["example.com"],
                        "outboundTag": "RWM_WARP",
                    }
                ]
            },
        }
        noop_api = FakeRoutingApi(managed)
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(RuntimePaths(Path(temporary)))
            configure_warp_routing(
                noop_api,  # type: ignore[arg-type]
                "admin-token",
                store,
                PROFILE_UUID,
                ["example.com"],
            )
            self.assertFalse((store.paths.state / "api-backups").exists())
        self.assertEqual(noop_api.patches, [])

    def test_warp_routing_normalizes_domains_and_rejects_non_dns_values(self) -> None:
        api = FakeRoutingApi({"outbounds": [], "routing": {"rules": []}})
        with tempfile.TemporaryDirectory() as temporary:
            configure_warp_routing(
                api,  # type: ignore[arg-type]
                "admin-token",
                StateStore(RuntimePaths(Path(temporary))),
                "01234567-89ab-cdef-0123-456789abcdef",
                ["Example.COM", "example.com"],
            )

        config = api.patches[0]["config"]
        self.assertEqual(
            config["routing"]["rules"][0]["domain"],  # type: ignore[index]
            ["example.com"],
        )

        for domains in (["bad\x00.example"], ["localhost"], ["*.example.com"]):
            with (
                self.subTest(domains=domains),
                tempfile.TemporaryDirectory() as temporary,
                self.assertRaises(ValidationError),
            ):
                configure_warp_routing(
                    FakeRoutingApi({"outbounds": [], "routing": {"rules": []}}),  # type: ignore[arg-type]
                    "admin-token",
                    StateStore(RuntimePaths(Path(temporary))),
                    "01234567-89ab-cdef-0123-456789abcdef",
                    domains,
                )

    def test_warp_routing_apply_migrates_recognized_legacy_profile(self) -> None:
        legacy_outbound = {
            "tag": "warp-out",
            "protocol": "freedom",
            "settings": {"domainStrategy": "UseIP"},
            "streamSettings": {"sockopt": {"interface": "warp", "tcpFastOpen": True}},
        }
        original = {
            "outbounds": [
                {"tag": "DIRECT", "protocol": "freedom"},
                legacy_outbound,
            ],
            "routing": {
                "rules": [
                    {
                        "type": "field",
                        "domain": ["whoer.net", "2ip.io"],
                        "outboundTag": "warp-out",
                    },
                    {
                        "type": "field",
                        "protocol": ["bittorrent"],
                        "outboundTag": "BLOCK",
                    },
                ]
            },
        }
        api = FakeRoutingApi(original)

        with tempfile.TemporaryDirectory() as temporary:
            configure_warp_routing(
                api,  # type: ignore[arg-type]
                "admin-token",
                StateStore(RuntimePaths(Path(temporary))),
                "01234567-89ab-cdef-0123-456789abcdef",
                ["example.net", "example.com", "example.net"],
            )

        config = api.patches[0]["config"]
        self.assertIsInstance(config, dict)
        outbounds = config["outbounds"]  # type: ignore[index]
        self.assertEqual(
            [item["tag"] for item in outbounds],  # type: ignore[index]
            ["DIRECT", "RWM_WARP"],
        )
        rules = config["routing"]["rules"]  # type: ignore[index]
        self.assertEqual(
            rules[0],
            {
                "type": "field",
                "domain": ["example.com", "example.net"],
                "outboundTag": "RWM_WARP",
            },
        )
        self.assertEqual(rules[1]["outboundTag"], "BLOCK")

    def test_warp_routing_remove_deletes_only_recognized_legacy_objects(self) -> None:
        original = {
            "outbounds": [
                {"tag": "DIRECT", "protocol": "freedom"},
                {
                    "tag": "warp-out",
                    "protocol": "freedom",
                    "settings": {"domainStrategy": "UseIP"},
                    "streamSettings": {
                        "sockopt": {"interface": "warp", "tcpFastOpen": True}
                    },
                },
            ],
            "routing": {
                "rules": [
                    {
                        "type": "field",
                        "domain": ["whoer.net"],
                        "outboundTag": "warp-out",
                    },
                    {
                        "type": "field",
                        "protocol": ["bittorrent"],
                        "outboundTag": "BLOCK",
                    },
                ]
            },
        }
        api = FakeRoutingApi(original)

        with tempfile.TemporaryDirectory() as temporary:
            configure_warp_routing(
                api,  # type: ignore[arg-type]
                "admin-token",
                StateStore(RuntimePaths(Path(temporary))),
                "01234567-89ab-cdef-0123-456789abcdef",
                [],
                remove=True,
            )

        config = api.patches[0]["config"]
        self.assertEqual(
            config["outbounds"],  # type: ignore[index]
            [{"tag": "DIRECT", "protocol": "freedom"}],
        )
        self.assertEqual(
            config["routing"]["rules"],  # type: ignore[index]
            [
                {
                    "type": "field",
                    "protocol": ["bittorrent"],
                    "outboundTag": "BLOCK",
                }
            ],
        )

    def test_warp_routing_rejects_foreign_legacy_tag_without_patch(self) -> None:
        original = {
            "outbounds": [
                {
                    "tag": "warp-out",
                    "protocol": "freedom",
                    "settings": {"domainStrategy": "AsIs"},
                    "streamSettings": {
                        "sockopt": {"interface": "custom", "tcpFastOpen": True}
                    },
                }
            ],
            "routing": {"rules": []},
        }
        api = FakeRoutingApi(original)

        with (
            tempfile.TemporaryDirectory() as temporary,
            self.assertRaisesRegex(ValidationError, "не принадлежит менеджеру"),
        ):
            configure_warp_routing(
                api,  # type: ignore[arg-type]
                "admin-token",
                StateStore(RuntimePaths(Path(temporary))),
                "01234567-89ab-cdef-0123-456789abcdef",
                [],
                remove=True,
            )

        self.assertEqual(api.patches, [])

    def test_warp_routing_detects_unapplied_patch_without_overwriting_again(
        self,
    ) -> None:
        class IgnoredPatchApi(FakeRoutingApi):
            def request(
                self, method: str, path: str, **kwargs: object
            ) -> dict[str, object]:
                if method == "PATCH":
                    self.patches.append(kwargs["data"])  # type: ignore[arg-type]
                    return {"response": {}}
                return super().request(method, path, **kwargs)

        api = IgnoredPatchApi({"outbounds": [], "routing": {"rules": []}})
        with (
            tempfile.TemporaryDirectory() as temporary,
            self.assertRaisesRegex(
                TransactionError, "не подтвердила итоговую конфигурацию"
            ),
        ):
            configure_warp_routing(
                api,  # type: ignore[arg-type]
                "admin-token",
                StateStore(RuntimePaths(Path(temporary))),
                "01234567-89ab-cdef-0123-456789abcdef",
                ["example.com"],
            )

        self.assertEqual(len(api.patches), 1)


class RealityProvisioningTests(unittest.TestCase):
    def test_generated_reality_uses_google_dns_and_core_client_default(self) -> None:
        config = build_reality_config(
            "node.example.com",
            "REALITY",
            "private-key",
        )

        self.assertEqual(
            config["dns"],
            {
                "queryStrategy": "UseIPv4",
                "servers": [
                    {
                        "address": "https://dns.google/dns-query",
                        "skipFallback": False,
                    }
                ],
            },
        )
        reality = config["inbounds"][0]["streamSettings"]["realitySettings"]
        self.assertNotIn("minClientVer", reality)

    def test_reality_domain_and_node_address_validation_is_strict(self) -> None:
        build_reality_config("node.example.com", "REALITY", "private-key")
        build_reality_config("xn--e1afmkfd.xn--p1ai", "REALITY", "private-key")
        _domain_or_address("node.example.com")
        _domain_or_address("192.0.2.10")
        _domain_or_address("2001:db8::10")

        for invalid in (
            "192.0.2.10",
            "a..example.com",
            "-node.example.com",
            "node-.example.com",
            "localhost",
            "999.999.999.999",
        ):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(ValidationError, "домен Reality"),
            ):
                build_reality_config(invalid, "REALITY", "private-key")

        for invalid in (
            "node..example.com",
            "-node.example.com",
            "node-.example.com",
            "999.999.999.999",
            "fe80::1%eth0",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                _domain_or_address(invalid)

    def test_adds_new_inbound_to_every_squad_without_losing_parallel_addition(
        self,
    ) -> None:
        api = FakeProvisioningApi()

        result = provision_reality_node(
            api,  # type: ignore[arg-type]
            "admin-token",
            profile_name="Reality Profile",
            inbound_tag="REALITY",
            node_name="Node One",
            domain="node.example.com",
        )

        self.assertEqual(result.profile_uuid, PROFILE_UUID)
        self.assertEqual(result.inbound_uuid, INBOUND_UUID)
        self.assertEqual(result.node_uuid, NODE_UUID)
        self.assertEqual(result.host_uuid, HOST_UUID)
        self.assertEqual(result.secret_key, "node-secret")
        self.assertEqual(
            api.squads[SQUAD_ONE_UUID],
            [EXISTING_ONE_UUID, PARALLEL_UUID, INBOUND_UUID],
        )
        self.assertEqual(
            api.squads[SQUAD_TWO_UUID],
            [EXISTING_TWO_UUID, INBOUND_UUID],
        )
        self.assertEqual(
            api.updates,
            [
                (
                    SQUAD_ONE_UUID,
                    [EXISTING_ONE_UUID, PARALLEL_UUID, INBOUND_UUID],
                ),
                (SQUAD_TWO_UUID, [EXISTING_TWO_UUID, INBOUND_UUID]),
            ],
        )
        self.assertEqual(api.deleted, [])

    def test_invalid_reality_input_is_rejected_before_key_generation(self) -> None:
        invalid_arguments = (
            {"profile_name": "bad\nname"},
            {"inbound_tag": "bad tag"},
            {"node_name": " node"},
            {"domain": "localhost"},
        )
        defaults = {
            "profile_name": "Reality Profile",
            "inbound_tag": "REALITY",
            "node_name": "Node One",
            "domain": "node.example.com",
        }
        for replacement in invalid_arguments:
            api = mock.Mock()
            with self.subTest(replacement=replacement), self.assertRaises(ValidationError):
                provision_reality_node(
                    api,
                    "admin-token",
                    **(defaults | replacement),
                )
            api.generate_x25519_private_key.assert_not_called()

    def test_reality_secret_has_private_recovery_until_handoff(self) -> None:
        api = FakeProvisioningApi()
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(RuntimePaths(Path(temporary)))
            result = provision_reality_node(
                api,  # type: ignore[arg-type]
                "admin-token",
                profile_name="Reality Profile",
                inbound_tag="REALITY",
                node_name="Node One",
                domain="node.example.com",
                store=store,
            )
            recovery = store.paths.state / REALITY_RECOVERY_NAME
            payload = json.loads(recovery.read_text(encoding="utf-8"))
            self.assertEqual(payload["secret_key"], result.secret_key)
            self.assertNotIn("admin-token", recovery.read_text(encoding="utf-8"))
            if os.name == "posix":
                self.assertEqual(recovery.stat().st_mode & 0o777, 0o600)

            complete_reality_credentials_handoff(store, result)

            self.assertFalse(recovery.exists())

    def test_reality_handoff_distinguishes_directory_fsync_failure(self) -> None:
        api = FakeProvisioningApi()
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(RuntimePaths(Path(temporary)))
            value = provision_reality_node(
                api,  # type: ignore[arg-type]
                "admin-token",
                profile_name="Reality Profile",
                inbound_tag="REALITY",
                node_name="Node One",
                domain="node.example.com",
                store=store,
            )
            recovery = store.paths.state / REALITY_RECOVERY_NAME
            with (
                mock.patch(
                    "remnawave_manager.api._fsync_reality_recovery_directory",
                    side_effect=OSError("fsync open"),
                ),
                self.assertRaisesRegex(TransactionError, "удалён.*не подтверждена"),
            ):
                complete_reality_credentials_handoff(store, value)

            self.assertFalse(recovery.exists())

    def test_stale_reality_recovery_blocks_api_before_key_generation(self) -> None:
        api = mock.Mock()
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(RuntimePaths(Path(temporary)))
            store.initialize()
            recovery = store.paths.state / REALITY_RECOVERY_NAME
            recovery.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValidationError, "recovery-копия"):
                provision_reality_node(
                    api,
                    "admin-token",
                    profile_name="Reality Profile",
                    inbound_tag="REALITY",
                    node_name="Node One",
                    domain="node.example.com",
                    store=store,
                )

        api.generate_x25519_private_key.assert_not_called()

    def test_failure_restores_squads_and_deletes_all_created_resources(self) -> None:
        api = FakeProvisioningApi(fail_squad=SQUAD_TWO_UUID)

        with self.assertRaisesRegex(RuntimeError, "second squad update failed"):
            provision_reality_node(
                api,  # type: ignore[arg-type]
                "admin-token",
                profile_name="Reality Profile",
                inbound_tag="REALITY",
                node_name="Node One",
                domain="node.example.com",
            )

        self.assertEqual(
            api.squads[SQUAD_ONE_UUID],
            [EXISTING_ONE_UUID, PARALLEL_UUID],
        )
        self.assertEqual(api.squads[SQUAD_TWO_UUID], [EXISTING_TWO_UUID])
        self.assertNotIn(INBOUND_UUID, api.squads[SQUAD_ONE_UUID])
        self.assertNotIn(INBOUND_UUID, api.squads[SQUAD_TWO_UUID])
        self.assertEqual(
            api.deleted,
            [
                ("hosts", HOST_UUID),
                ("nodes", NODE_UUID),
                ("config-profiles", PROFILE_UUID),
            ],
        )
        rollback_update = api.updates[-1]
        self.assertEqual(
            rollback_update,
            (SQUAD_ONE_UUID, [EXISTING_ONE_UUID, PARALLEL_UUID]),
        )


if __name__ == "__main__":
    unittest.main()
