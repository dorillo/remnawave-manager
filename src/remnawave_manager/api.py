from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import stat
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import TransactionError, ValidationError
from .runner import atomic_write_json, sanitize_external_text
from .state import StateStore, _read_private_json, utc_now

SUBSCRIPTION_SCOPES = [
    "system:metadata",
    "users:by-username",
    "subscription-page-configs:list",
    "subscription-page-configs:get",
    "subscriptions:by-short-uuid-protected",
    "subscriptions:subpage-config",
]

MANAGER_WARP_TAG = "RWM_WARP"
LEGACY_WARP_TAG = "warp-out"
REALITY_RECOVERY_NAME = "api-reality-credentials.json"
_MAX_API_BODY_SIZE = 8 * 1024 * 1024
_MAX_REALITY_RECOVERY_SIZE = 64 * 1024
_SENSITIVE_API_KEY_PARTS = ("password", "privatekey", "secret", "token")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self,
        _request: urllib.request.Request,
        _file_pointer: Any,
        _code: int,
        _message: str,
        _headers: Any,
        _new_url: str,
    ) -> None:
        return None


_API_OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    _NoRedirectHandler(),
)


def _open_api_request(request: urllib.request.Request, *, timeout: int) -> Any:
    return _API_OPENER.open(request, timeout=timeout)


def _validated_base_url(value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("Некорректный URL Panel API.")
    if value != value.strip():
        raise ValidationError("Некорректный URL Panel API.")
    selected = value.rstrip("/")
    if not selected or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in selected
    ):
        raise ValidationError("Некорректный URL Panel API.")
    try:
        parsed = urllib.parse.urlsplit(selected)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValidationError("Некорректный URL Panel API.") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.netloc.endswith(":")
        or port == 0
    ):
        raise ValidationError("Некорректный URL Panel API.")

    normalized_hostname = hostname.rstrip(".").lower()
    if normalized_hostname != hostname.lower():
        raise ValidationError("Некорректный URL Panel API.")
    try:
        address = ipaddress.ip_address(normalized_hostname)
    except ValueError:
        if normalized_hostname != "localhost":
            _dns_name(normalized_hostname, label="домен Panel API")
    else:
        if getattr(address, "scope_id", None):
            raise ValidationError("Некорректный URL Panel API.")
    if parsed.scheme == "http":
        loopback = normalized_hostname == "localhost"
        if not loopback:
            try:
                loopback = ipaddress.ip_address(normalized_hostname).is_loopback
            except ValueError:
                loopback = False
        if not loopback:
            raise ValidationError(
                "Незашифрованный Panel API разрешён только через loopback; "
                "для удалённого адреса используйте HTTPS."
            )
    return selected


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"Недопустимая JSON-константа: {value}")


def _request_secret_values(
    token: str | None,
    data: dict[str, Any] | None,
) -> tuple[str, ...]:
    values: list[str] = [token] if token else []
    stack: list[object] = [data] if data is not None else []
    visited: set[int] = set()
    remaining = 100_000
    while stack and remaining > 0:
        current = stack.pop()
        remaining -= 1
        if isinstance(current, dict):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            for key, item in current.items():
                normalized = (
                    re.sub(r"[^a-z0-9]", "", key.casefold())
                    if isinstance(key, str)
                    else ""
                )
                if any(part in normalized for part in _SENSITIVE_API_KEY_PARTS):
                    if isinstance(item, str) and item:
                        values.append(item)
                    elif isinstance(item, (list, tuple)):
                        values.extend(
                            value for value in item if isinstance(value, str) and value
                        )
                elif isinstance(item, (dict, list, tuple)):
                    stack.append(item)
        elif isinstance(current, (list, tuple)):
            identity = id(current)
            if identity in visited:
                continue
            visited.add(identity)
            stack.extend(current)
    return tuple(dict.fromkeys(values))


def _redact_sensitive_text(
    value: object,
    secrets_to_redact: Iterable[str],
    *,
    limit: int,
) -> str:
    try:
        selected = str(value)
    except (RecursionError, UnicodeError):
        return ""
    for secret in sorted(set(secrets_to_redact), key=len, reverse=True):
        if secret:
            selected = selected.replace(secret, "<скрыто>")
    return sanitize_external_text(selected, limit=limit)


@dataclass(slots=True)
class ProvisionedReality:
    profile_uuid: str
    inbound_uuid: str
    node_uuid: str
    host_uuid: str
    secret_key: str


class RemnawaveApi:
    def __init__(
        self, base_url: str = "http://127.0.0.1:3000", *, timeout: int = 30
    ) -> None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int)
            or not 1 <= timeout <= 300
        ):
            raise ValidationError(
                "Timeout Panel API должен быть целым числом от 1 до 300 секунд."
            )
        self.base_url = _validated_base_url(base_url)
        self.timeout = timeout
        hostname = urllib.parse.urlsplit(self.base_url).hostname
        normalized_hostname = hostname.rstrip(".").lower() if hostname else ""
        self._loopback = normalized_hostname == "localhost"
        if hostname and not self._loopback:
            try:
                self._loopback = ipaddress.ip_address(normalized_hostname).is_loopback
            except ValueError:
                self._loopback = False

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        data: dict[str, Any] | None = None,
        expected: Iterable[int] = (200,),
    ) -> dict[str, Any] | None:
        method_name = method.upper() if isinstance(method, str) else ""
        if method_name not in {"GET", "POST", "PATCH", "DELETE"}:
            raise ValidationError("Некорректный HTTP-метод Panel API.")
        if (
            not isinstance(path, str)
            or not path.isascii()
            or any(not 33 <= ord(character) <= 126 for character in path)
        ):
            raise ValidationError("Некорректный API path.")
        parsed_path = urllib.parse.urlsplit(path)
        if (
            not path.startswith("/api/")
            or path.startswith("//")
            or parsed_path.scheme
            or parsed_path.netloc
            or parsed_path.query
            or parsed_path.fragment
            or "%" in parsed_path.path
            or "\\" in parsed_path.path
            or any(part in {".", ".."} for part in parsed_path.path.split("/"))
        ):
            raise ValidationError(
                "API path должен быть абсолютным путём /api/... без query, fragment или traversal."
            )
        try:
            expected_statuses = frozenset(expected)
        except TypeError as error:
            raise ValidationError(
                "Некорректный набор ожидаемых HTTP-статусов."
            ) from error
        if not expected_statuses or any(
            isinstance(status, bool)
            or not isinstance(status, int)
            or not 100 <= status <= 599
            for status in expected_statuses
        ):
            raise ValidationError("Некорректный набор ожидаемых HTTP-статусов.")
        headers = {
            "Accept": "application/json",
            "X-Remnawave-Client-Type": "browser",
        }
        if self._loopback:
            headers.update(
                {
                    "X-Forwarded-For": "127.0.0.1",
                    "X-Forwarded-Proto": "https",
                }
            )
        if token is not None:
            if (
                not isinstance(token, str)
                or not token
                or len(token) > 16_384
                or any(
                    ord(character) < 33 or ord(character) > 126 for character in token
                )
            ):
                raise ValidationError("Panel API token содержит недопустимые символы.")
            headers["Authorization"] = "Bearer " + token
        if data is not None and not isinstance(data, dict):
            raise ValidationError("Тело запроса Panel API должно быть JSON-объектом.")
        secrets_to_redact = _request_secret_values(token, data)
        payload: bytes | None = None
        if data is not None:
            try:
                payload = json.dumps(
                    data,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            except (TypeError, ValueError, RecursionError, UnicodeError) as error:
                raise ValidationError(
                    "Тело запроса Panel API нельзя безопасно сериализовать."
                ) from error
            if len(payload) > _MAX_API_BODY_SIZE:
                raise ValidationError("Тело запроса Panel API превышает допустимый размер.")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(  # noqa: S310, RUF100 - URL scheme validated above
            self.base_url + path,
            data=payload,
            headers=headers,
            method=method_name,
        )
        try:
            with _open_api_request(request, timeout=self.timeout) as response:
                status = response.status
                body = response.read(_MAX_API_BODY_SIZE + 1)
        except urllib.error.HTTPError as error:
            try:
                status = error.code
                body = error.read(1024 * 1024)
            except OSError as read_error:
                raise TransactionError(
                    f"Panel API вернула HTTP {error.code}, но тело ошибки прочитать не удалось."
                ) from read_error
            finally:
                with suppress(OSError):
                    error.close()
        except (OSError, urllib.error.URLError) as error:
            detail = _redact_sensitive_text(error, secrets_to_redact, limit=1000)
            raise TransactionError(
                "Panel API недоступна" + (f": {detail}" if detail else ".")
            ) from None
        if status not in expected_statuses:
            message = _error_message(body, secrets_to_redact=secrets_to_redact)
            raise TransactionError(
                f"Panel API: {method.upper()} {path} вернул HTTP {status}"
                + (f": {message}" if message else ".")
            )
        if status == 204 or not body:
            return None
        if len(body) > _MAX_API_BODY_SIZE:
            raise TransactionError("Ответ Panel API превышает допустимый размер.")
        try:
            parsed_body = json.loads(body, parse_constant=_reject_json_constant)
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            RecursionError,
            ValueError,
        ) as error:
            raise TransactionError(f"Panel API вернула не-JSON для {path}.") from error
        if not isinstance(parsed_body, dict):
            raise TransactionError(f"Panel API вернула неожиданный объект для {path}.")
        return parsed_body

    def auth_status(self) -> dict[str, Any]:
        payload = self.request("GET", "/api/auth/status", expected=(200,))
        try:
            response = payload["response"]  # type: ignore[index]
        except (KeyError, TypeError) as error:
            raise TransactionError("Некорректный ответ /api/auth/status.") from error
        if not isinstance(response, dict):
            raise TransactionError("Некорректный ответ /api/auth/status.")
        return response

    def register_or_login(self, username: str, password: str) -> str:
        _validate_login_credentials(username, password)
        status = self.auth_status()
        registering = status.get("isRegisterAllowed") is True
        if registering:
            validate_admin_password(password)
            path, expected = "/api/auth/register", (201,)
        elif status.get("isLoginAllowed") is True:
            path, expected = "/api/auth/login", (200,)
        else:
            raise ValidationError("Panel не разрешает регистрацию или вход по паролю.")
        payload = self.request(
            "POST",
            path,
            data={"username": username, "password": password},
            expected=expected,
        )
        try:
            token = payload["response"]["accessToken"]  # type: ignore[index]
        except (KeyError, TypeError) as error:
            raise TransactionError("Panel не вернула admin JWT.") from error
        return _api_secret(token, "admin JWT")

    def create_subscription_token(
        self,
        admin_token: str,
        *,
        name: str = "subscription-page",
        expires_days: int = 365,
    ) -> str:
        if not _printable_name(name, minimum=2, maximum=30):
            raise ValidationError("Имя API-токена должно содержать 2-30 символов.")
        if (
            isinstance(expires_days, bool)
            or not isinstance(expires_days, int)
            or not 1 <= expires_days <= 3650
        ):
            raise ValidationError(
                "Срок API-токена должен быть целым числом от 1 до 3650 дней."
            )
        payload = self.request(
            "POST",
            "/api/tokens",
            token=admin_token,
            data={
                "name": name,
                "expiresInDays": expires_days,
                "scopes": list(SUBSCRIPTION_SCOPES),
            },
            expected=(201,),
        )
        try:
            token = payload["response"]["token"]  # type: ignore[index]
        except (KeyError, TypeError) as error:
            raise TransactionError(
                "Panel не вернула API-токен Subscription Page."
            ) from error
        return _api_secret(token, "API-токен Subscription Page")

    def keygen(self, token: str) -> str:
        payload = self.request("GET", "/api/keygen", token=token, expected=(200,))
        response = payload.get("response", {}) if payload else {}
        key = response.get("secretKey") or response.get("pubKey")
        return _api_secret(key, "secretKey/pubKey keygen")

    def generate_x25519_private_key(self, token: str) -> str:
        payload = self.request(
            "GET", "/api/system/tools/x25519/generate", token=token, expected=(200,)
        )
        try:
            key = payload["response"]["keypairs"][0]["privateKey"]  # type: ignore[index]
        except (KeyError, IndexError, TypeError) as error:
            raise TransactionError("Panel не вернула X25519 private key.") from error
        return _api_secret(key, "X25519 private key")

    def create_config_profile(
        self, token: str, name: str, config: dict[str, Any]
    ) -> tuple[str, str]:
        _validate_profile_name(name)
        if not isinstance(config, dict):
            raise ValidationError("Конфигурация профиля должна быть JSON-объектом.")
        payload = self.request(
            "POST",
            "/api/config-profiles",
            token=token,
            data={"name": name, "config": config},
            expected=(201,),
        )
        try:
            response = payload["response"]  # type: ignore[index]
            profile_uuid = response["uuid"]
            inbound_uuid = response["inbounds"][0]["uuid"]
        except (KeyError, IndexError, TypeError) as error:
            raise TransactionError("Panel не вернула UUID профиля/inbound.") from error
        _uuid(profile_uuid, "profile UUID")
        _uuid(inbound_uuid, "inbound UUID")
        return profile_uuid, inbound_uuid

    def create_node(
        self,
        token: str,
        *,
        name: str,
        address: str,
        profile_uuid: str,
        inbound_uuid: str,
    ) -> str:
        _validate_node_name(name)
        _domain_or_address(address)
        selected_profile_uuid = _input_uuid(profile_uuid, "Config Profile UUID")
        selected_inbound_uuid = _input_uuid(inbound_uuid, "inbound UUID")
        payload = self.request(
            "POST",
            "/api/nodes",
            token=token,
            data={
                "name": name,
                "address": address,
                "port": 2222,
                "configProfile": {
                    "activeConfigProfileUuid": selected_profile_uuid,
                    "activeInbounds": [selected_inbound_uuid],
                },
                "isTrafficTrackingActive": False,
                "trafficLimitBytes": 0,
                "notifyPercent": 0,
                "trafficResetDay": 31,
                "countryCode": "XX",
                "consumptionMultiplier": 1.0,
            },
            expected=(201,),
        )
        try:
            node_uuid = payload["response"]["uuid"]  # type: ignore[index]
        except (KeyError, TypeError) as error:
            raise TransactionError("Panel не вернула UUID ноды.") from error
        return _uuid(node_uuid, "node UUID")

    def create_host(
        self,
        token: str,
        *,
        remark: str,
        address: str,
        profile_uuid: str,
        inbound_uuid: str,
    ) -> str:
        if not _printable_name(remark, minimum=1, maximum=40):
            raise ValidationError("Remark хоста должен содержать 1-40 символов.")
        _domain_or_address(address)
        selected_profile_uuid = _input_uuid(profile_uuid, "Config Profile UUID")
        selected_inbound_uuid = _input_uuid(inbound_uuid, "inbound UUID")
        payload = self.request(
            "POST",
            "/api/hosts",
            token=token,
            data={
                "inbound": {
                    "configProfileUuid": selected_profile_uuid,
                    "configProfileInboundUuid": selected_inbound_uuid,
                },
                "remark": remark,
                "address": address,
                "port": 443,
                "sni": address,
                "fingerprint": "chrome",
                "isDisabled": False,
                "securityLayer": "DEFAULT",
            },
            expected=(201,),
        )
        try:
            host_uuid = payload["response"]["uuid"]  # type: ignore[index]
        except (KeyError, TypeError) as error:
            raise TransactionError("Panel не вернула UUID хоста.") from error
        return _uuid(host_uuid, "host UUID")

    def list_internal_squad_uuids(self, token: str) -> tuple[str, ...]:
        payload = self.request(
            "GET", "/api/internal-squads", token=token, expected=(200,)
        )
        try:
            response = payload["response"]  # type: ignore[index]
            total = response["total"]
            squads = response["internalSquads"]
        except (KeyError, TypeError) as error:
            raise TransactionError(
                "Panel вернула некорректный список Internal Squads."
            ) from error
        if (
            isinstance(total, bool)
            or not isinstance(total, int)
            or total < 0
            or not isinstance(squads, list)
            or total != len(squads)
        ):
            raise TransactionError("Panel вернула некорректный список Internal Squads.")
        squad_uuids = tuple(
            _internal_squad(item, "элемент списка Internal Squads")[0]
            for item in squads
        )
        if len(set(squad_uuids)) != len(squad_uuids):
            raise TransactionError("Panel вернула повторяющиеся UUID Internal Squads.")
        return squad_uuids

    def get_internal_squad_inbounds(
        self, token: str, squad_uuid: str
    ) -> tuple[str, ...]:
        selected_uuid = _uuid(squad_uuid, "Internal Squad UUID")
        payload = self.request(
            "GET",
            f"/api/internal-squads/{selected_uuid}",
            token=token,
            expected=(200,),
        )
        try:
            response = payload["response"]  # type: ignore[index]
        except (KeyError, TypeError) as error:
            raise TransactionError(
                "Panel вернула некорректный Internal Squad."
            ) from error
        returned_uuid, inbounds = _internal_squad(response, "Internal Squad")
        if returned_uuid != selected_uuid:
            raise TransactionError("Panel вернула другой Internal Squad UUID.")
        return inbounds

    def update_internal_squad_inbounds(
        self,
        token: str,
        squad_uuid: str,
        inbounds: list[str],
    ) -> tuple[str, ...]:
        selected_uuid = _uuid(squad_uuid, "Internal Squad UUID")
        selected_inbounds = [
            _uuid(item, "Internal Squad inbound UUID") for item in inbounds
        ]
        if len(set(selected_inbounds)) != len(selected_inbounds):
            raise ValidationError("Список inbounds Internal Squad содержит повторы.")
        payload = self.request(
            "PATCH",
            "/api/internal-squads",
            token=token,
            data={"uuid": selected_uuid, "inbounds": selected_inbounds},
            expected=(200,),
        )
        try:
            response = payload["response"]  # type: ignore[index]
        except (KeyError, TypeError) as error:
            raise TransactionError(
                "Panel вернула некорректный обновлённый Internal Squad."
            ) from error
        returned_uuid, returned_inbounds = _internal_squad(
            response, "обновлённый Internal Squad"
        )
        if returned_uuid != selected_uuid:
            raise TransactionError(
                "Panel вернула другой Internal Squad UUID после PATCH."
            )
        return returned_inbounds

    def delete(self, token: str, resource: str, item_uuid: str) -> None:
        if resource not in {"hosts", "nodes", "config-profiles"}:
            raise ValidationError("Удаление этого API-ресурса запрещено.")
        self.request(
            "DELETE",
            f"/api/{resource}/{_uuid(item_uuid, 'UUID')}",
            token=token,
            expected=(200, 204),
        )


def _internal_squad(value: object, label: str) -> tuple[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        raise TransactionError(f"Panel вернула некорректный {label}.")
    try:
        squad_uuid = _uuid(value["uuid"], f"{label} UUID")
        raw_inbounds = value["inbounds"]
    except KeyError as error:
        raise TransactionError(f"Panel вернула некорректный {label}.") from error
    if not isinstance(raw_inbounds, list):
        raise TransactionError(f"Panel вернула некорректные inbounds для {label}.")
    inbounds: list[str] = []
    for item in raw_inbounds:
        if not isinstance(item, dict) or "uuid" not in item:
            raise TransactionError(f"Panel вернула некорректные inbounds для {label}.")
        inbounds.append(_uuid(item["uuid"], f"{label} inbound UUID"))
    if len(set(inbounds)) != len(inbounds):
        raise TransactionError(f"Panel вернула повторяющиеся inbounds для {label}.")
    return squad_uuid, tuple(inbounds)


def validate_admin_password(password: str) -> None:
    if (
        not isinstance(password, str)
        or not 24 <= len(password) <= 1024
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in password
        )
        or not re.search(r"[A-Z]", password)
        or not re.search(r"[a-z]", password)
        or not re.search(r"[0-9]", password)
    ):
        raise ValidationError(
            "Пароль администратора: 24-1024 символа без управляющих знаков, "
            "хотя бы одна заглавная, строчная буква и цифра."
        )


def _validate_login_credentials(username: object, password: object) -> None:
    if not isinstance(username, str) or not re.fullmatch(
        r"[A-Za-z0-9_-]{3,32}", username
    ):
        raise ValidationError(
            "Имя администратора: 3-32 латинских букв, цифр, _ или -."
        )
    if (
        not isinstance(password, str)
        or not 1 <= len(password) <= 1024
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in password
        )
    ):
        raise ValidationError(
            "Пароль администратора должен содержать 1-1024 символа "
            "без управляющих знаков."
        )


def _printable_name(value: object, *, minimum: int, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and minimum <= len(value) <= maximum
        and value == value.strip()
        and all(
            unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
            for character in value
        )
    )


def _error_message(
    body: bytes,
    *,
    secrets_to_redact: Iterable[str] = (),
) -> str:
    try:
        payload = json.loads(body, parse_constant=_reject_json_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return ""
    message = payload.get("message") if isinstance(payload, dict) else None
    try:
        if isinstance(message, list):
            message = "; ".join(str(item) for item in message)
    except RecursionError:
        return ""
    if not message:
        return ""
    return " ".join(
        _redact_sensitive_text(message, secrets_to_redact, limit=500).split()
    )


def _api_secret(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 16_384
        or any(not 33 <= ord(character) <= 126 for character in value)
    ):
        raise TransactionError(f"Panel вернула некорректный {label}.")
    return value


def _uuid(value: Any, label: str) -> str:
    selected = str(value)
    if not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        selected,
    ):
        raise TransactionError(f"Panel вернула некорректный {label}.")
    return selected


def _input_uuid(value: object, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        value,
    ):
        raise ValidationError(f"Некорректный {label}.")
    return value


def _validate_profile_name(value: object) -> None:
    if not _printable_name(value, minimum=2, maximum=30) or not re.fullmatch(
        r"[A-Za-z0-9_ -]{2,30}", value  # type: ignore[arg-type]
    ):
        raise ValidationError(
            "Имя профиля: 2-30 латинских букв, цифр, пробелов, _ или -."
        )


def _validate_node_name(value: object) -> None:
    if not _printable_name(value, minimum=3, maximum=30):
        raise ValidationError("Имя ноды должно содержать 3-30 символов.")


def _dns_name(
    value: str,
    *,
    label: str = "домен",
    require_fqdn: bool = False,
) -> None:
    """Validate an ASCII DNS name without accepting ambiguous host syntax."""
    if not isinstance(value, str) or not 1 <= len(value) <= 253:
        raise ValidationError(f"Некорректный {label}.")
    if value.endswith(".") or any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValidationError(f"Некорректный {label}.")
    labels = value.split(".")
    if require_fqdn and len(labels) < 2:
        raise ValidationError(f"Некорректный {label}.")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ValidationError(f"Некорректный {label}.")
    if len(labels) > 1 and all(part.isdigit() for part in labels):
        raise ValidationError(f"Некорректный {label}.")
    if any(
        not 1 <= len(part) <= 63
        or part[0] == "-"
        or part[-1] == "-"
        or not re.fullmatch(r"[A-Za-z0-9-]+", part)
        for part in labels
    ):
        raise ValidationError(f"Некорректный {label}.")


def _domain_or_address(value: str) -> None:
    if not isinstance(value, str) or not 2 <= len(value) <= 253:
        raise ValidationError("Некорректный адрес/домен ноды.")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        _dns_name(value, label="адрес/домен ноды")
    else:
        if address.version == 6 and getattr(address, "scope_id", None):
            raise ValidationError("IPv6-адрес с zone id не поддерживается.")


def build_reality_config(
    domain: str,
    inbound_tag: str,
    private_key: str,
    *,
    socket_path: str = "/dev/shm/nginx.sock",  # noqa: S108, RUF100 - intentional shared XHTTP Unix socket
) -> dict[str, Any]:
    _dns_name(domain, label="домен Reality", require_fqdn=True)
    _validate_inbound_tag(inbound_tag)
    _api_secret(private_key, "X25519 private key")
    if not re.fullmatch(r"/(?:dev/shm|run)/[A-Za-z0-9_.-]+", socket_path):
        raise ValidationError("Некорректный Unix socket path.")
    return {
        "log": {"loglevel": "warning"},
        "dns": {
            "queryStrategy": "UseIPv4",
            "servers": [
                {"address": "https://1.1.1.1/dns-query", "skipFallback": False}
            ],
        },
        "inbounds": [
            {
                "tag": inbound_tag,
                "port": 443,
                "protocol": "vless",
                "settings": {"clients": [], "decryption": "none"},
                "sniffing": {"enabled": True, "destOverride": ["http", "tls", "quic"]},
                "streamSettings": {
                    "network": "tcp",
                    "security": "reality",
                    "realitySettings": {
                        "show": False,
                        "xver": 1,
                        "dest": socket_path,
                        "spiderX": "",
                        "shortIds": [secrets.token_hex(8)],
                        "privateKey": private_key,
                        "serverNames": [domain],
                        "minClientVer": "26.3.27",
                    },
                },
            }
        ],
        "outbounds": [
            {"tag": "DIRECT", "protocol": "freedom"},
            {"tag": "BLOCK", "protocol": "blackhole"},
        ],
        "routing": {
            "rules": [
                {"ip": ["geoip:private"], "type": "field", "outboundTag": "BLOCK"},
                {"type": "field", "protocol": ["bittorrent"], "outboundTag": "BLOCK"},
            ]
        },
    }


def _validate_inbound_tag(value: object) -> None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{2,40}", value):
        raise ValidationError(
            "Inbound tag должен содержать 2-40 латинских букв, цифр, _ или -."
        )


def validate_reality_inputs(
    *,
    profile_name: object,
    inbound_tag: object,
    node_name: object,
    domain: object,
) -> None:
    _validate_profile_name(profile_name)
    _validate_inbound_tag(inbound_tag)
    _validate_node_name(node_name)
    if not isinstance(domain, str):
        raise ValidationError("Некорректный домен Reality.")
    _dns_name(domain, label="домен Reality", require_fqdn=True)


def _stable_internal_squad_inbounds(
    api: RemnawaveApi,
    token: str,
    squad_uuid: str,
    *,
    attempts: int = 4,
) -> tuple[str, ...]:
    previous: tuple[str, ...] | None = None
    for _ in range(attempts):
        current = api.get_internal_squad_inbounds(token, squad_uuid)
        if previous is not None and set(current) == set(previous):
            return current
        previous = current
    raise TransactionError(
        f"Internal Squad {squad_uuid} изменяется параллельно; provisioning остановлен."
    )


def _replace_reality_squad_membership(
    api: RemnawaveApi,
    token: str,
    squad_uuid: str,
    inbound_uuid: str,
    current: tuple[str, ...],
    *,
    add: bool,
) -> None:
    if add:
        desired = [*current, inbound_uuid]
    else:
        desired = [item for item in current if item != inbound_uuid]
    required = set(desired)

    returned = api.update_internal_squad_inbounds(token, squad_uuid, desired)
    returned_set = set(returned)
    if (
        (add and inbound_uuid not in returned_set)
        or (not add and inbound_uuid in returned_set)
        or not required.issubset(returned_set)
    ):
        raise TransactionError(
            f"Panel не подтвердила безопасное обновление Internal Squad {squad_uuid}."
        )

    verified = set(_stable_internal_squad_inbounds(api, token, squad_uuid))
    if (
        (add and inbound_uuid not in verified)
        or (not add and inbound_uuid in verified)
        or not required.issubset(verified)
    ):
        raise TransactionError(
            f"Проверка Internal Squad {squad_uuid} после PATCH не пройдена."
        )


def _remove_reality_inbound_from_squad(
    api: RemnawaveApi,
    token: str,
    squad_uuid: str,
    inbound_uuid: str,
) -> None:
    current = _stable_internal_squad_inbounds(api, token, squad_uuid)
    if inbound_uuid not in current:
        return
    _replace_reality_squad_membership(
        api,
        token,
        squad_uuid,
        inbound_uuid,
        current,
        add=False,
    )


def _reality_recovery_path(store: StateStore) -> Path:
    return store.paths.state / REALITY_RECOVERY_NAME


def _reality_recovery_payload(value: ProvisionedReality) -> dict[str, object]:
    return {
        "schema_version": 1,
        "profile_uuid": value.profile_uuid,
        "inbound_uuid": value.inbound_uuid,
        "node_uuid": value.node_uuid,
        "host_uuid": value.host_uuid,
        "secret_key": value.secret_key,
    }


def _fsync_reality_recovery_directory(path: Path) -> None:
    if os.name != "posix":
        return
    directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _prepare_reality_recovery(store: StateStore) -> Path:
    store.initialize()
    path = _reality_recovery_path(store)
    if path.exists() or path.is_symlink():
        raise ValidationError(
            f"Обнаружена невыданная recovery-копия Reality credentials: {path}. "
            "Сохраните SECRET_KEY и удалите файл вручную до нового provisioning."
        )
    return path


def complete_reality_credentials_handoff(
    store: StateStore,
    value: ProvisionedReality,
) -> None:
    """Delete the recovery copy only after the caller durably displayed it."""
    path = _reality_recovery_path(store)
    if not path.exists() and not path.is_symlink():
        return
    try:
        payload = _read_private_json(
            path,
            label="recovery-файл Reality credentials",
            max_size=_MAX_REALITY_RECOVERY_SIZE,
            required_mode=0o600,
        )
        info = path.lstat()
    except (OSError, ValidationError) as error:
        raise TransactionError(
            f"Не удалось безопасно проверить recovery-файл Reality credentials {path}."
        ) from error
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or (os.name == "posix" and info.st_uid != os.geteuid())
        or (os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o600)
    ):
        raise TransactionError(
            f"Recovery-файл Reality credentials имеет небезопасный тип или права: {path}."
        )
    if payload != _reality_recovery_payload(value):
        raise TransactionError(
            f"Recovery-файл Reality credentials изменился и не удалён: {path}."
        )
    try:
        path.unlink()
    except OSError as error:
        raise TransactionError(
            "Reality provisioning завершён, но recovery-файл с SECRET_KEY не удалось "
            f"удалить: {path}. Удалите его вручную."
        ) from error
    try:
        _fsync_reality_recovery_directory(path.parent)
    except OSError as error:
        raise TransactionError(
            "Recovery-файл Reality credentials удалён, но durable-запись каталога "
            f"не подтверждена: {path}. Проверьте отсутствие файла перед повторной операцией."
        ) from error


def provision_reality_node(
    api: RemnawaveApi,
    token: str,
    *,
    profile_name: str,
    inbound_tag: str,
    node_name: str,
    domain: str,
    store: StateStore | None = None,
) -> ProvisionedReality:
    validate_reality_inputs(
        profile_name=profile_name,
        inbound_tag=inbound_tag,
        node_name=node_name,
        domain=domain,
    )
    recovery_path = _prepare_reality_recovery(store) if store is not None else None
    private_key = api.generate_x25519_private_key(token)
    profile_uuid: str | None = None
    inbound_uuid: str | None = None
    node_uuid: str | None = None
    host_uuid: str | None = None
    secret_key: str | None = None
    squad_uuids: tuple[str, ...] = ()
    try:
        profile_uuid, inbound_uuid = api.create_config_profile(
            token,
            profile_name,
            build_reality_config(domain, inbound_tag, private_key),
        )
        node_uuid = api.create_node(
            token,
            name=node_name,
            address=domain,
            profile_uuid=profile_uuid,
            inbound_uuid=inbound_uuid,
        )
        host_uuid = api.create_host(
            token,
            remark=node_name,
            address=domain,
            profile_uuid=profile_uuid,
            inbound_uuid=inbound_uuid,
        )
        secret_key = api.keygen(token)
        squad_uuids = api.list_internal_squad_uuids(token)
        for squad_uuid in squad_uuids:
            current = _stable_internal_squad_inbounds(api, token, squad_uuid)
            if inbound_uuid in current:
                continue
            _replace_reality_squad_membership(
                api,
                token,
                squad_uuid,
                inbound_uuid,
                current,
                add=True,
            )
        result = ProvisionedReality(
            profile_uuid=profile_uuid,
            inbound_uuid=inbound_uuid,
            node_uuid=node_uuid,
            host_uuid=host_uuid,
            secret_key=secret_key,
        )
        if recovery_path is not None:
            atomic_write_json(
                recovery_path,
                _reality_recovery_payload(result),
                mode=0o600,
            )
        return result
    except BaseException as error:
        rollback_errors: list[str] = []
        if (
            store is not None
            and profile_uuid is not None
            and inbound_uuid is not None
            and node_uuid is not None
            and host_uuid is not None
            and secret_key is not None
        ):
            try:
                complete_reality_credentials_handoff(
                    store,
                    ProvisionedReality(
                        profile_uuid=profile_uuid,
                        inbound_uuid=inbound_uuid,
                        node_uuid=node_uuid,
                        host_uuid=host_uuid,
                        secret_key=secret_key,
                    ),
                )
            except BaseException as rollback_error:  # noqa: BLE001 - rollback must survive interrupts
                rollback_errors.append(
                    "очистка recovery-файла: "
                    + _redact_sensitive_text(
                        rollback_error,
                        (token, private_key, secret_key),
                        limit=1000,
                    )
                )
        if inbound_uuid is not None:
            for squad_uuid in reversed(squad_uuids):
                try:
                    _remove_reality_inbound_from_squad(
                        api, token, squad_uuid, inbound_uuid
                    )
                except BaseException as rollback_error:  # noqa: BLE001 - rollback must survive interrupts
                    rollback_errors.append(
                        f"Internal Squad {squad_uuid}: "
                        + _redact_sensitive_text(
                            rollback_error,
                            (token, private_key, secret_key or ""),
                            limit=1000,
                        )
                    )
        for resource, item in (
            ("hosts", host_uuid),
            ("nodes", node_uuid),
            ("config-profiles", profile_uuid),
        ):
            if item:
                try:
                    api.delete(token, resource, item)
                except BaseException as rollback_error:  # noqa: BLE001 - rollback must survive interrupts
                    rollback_errors.append(
                        f"удаление {resource}/{item}: "
                        + _redact_sensitive_text(
                            rollback_error,
                            (token, private_key, secret_key or ""),
                            limit=1000,
                        )
                    )
        if rollback_errors:
            original_error = _redact_sensitive_text(
                error,
                (token, private_key, secret_key or ""),
                limit=1000,
            )
            raise TransactionError(
                "Reality provisioning не завершён, автоматический rollback неполон: "
                + "; ".join(rollback_errors)
                + f". Исходная ошибка: {original_error or 'неизвестна'}"
            ) from error
        raise


def _config_hash(config: dict[str, Any]) -> str:
    payload = json.dumps(
        config, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _warp_outbound(tag: str, domain_strategy: str) -> dict[str, Any]:
    return {
        "tag": tag,
        "protocol": "freedom",
        "settings": {"domainStrategy": domain_strategy},
        "streamSettings": {"sockopt": {"interface": "warp", "tcpFastOpen": True}},
    }


def _owned_warp_rule(value: object, tag: str) -> bool:
    if not isinstance(value, dict) or set(value) != {
        "type",
        "domain",
        "outboundTag",
    }:
        return False
    domains = value.get("domain")
    return (
        value.get("type") == "field"
        and value.get("outboundTag") == tag
        and isinstance(domains, list)
        and bool(domains)
        and all(
            isinstance(domain, str)
            and bool(domain)
            and len(domain) <= 253
            and not any(character.isspace() for character in domain)
            for domain in domains
        )
    )


def _validate_warp_ownership(
    outbounds: list[object],
    rules: list[object],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tagged_outbounds: dict[str, list[dict[str, Any]]] = {}
    tagged_rules: dict[str, list[dict[str, Any]]] = {}
    expected_outbounds = {
        MANAGER_WARP_TAG: _warp_outbound(MANAGER_WARP_TAG, "UseIPv4"),
        LEGACY_WARP_TAG: _warp_outbound(LEGACY_WARP_TAG, "UseIP"),
    }
    for tag in (MANAGER_WARP_TAG, LEGACY_WARP_TAG):
        selected_outbounds = [
            item
            for item in outbounds
            if isinstance(item, dict) and item.get("tag") == tag
        ]
        if len(selected_outbounds) > 1:
            raise ValidationError(f"В профиле несколько outbound с tag {tag}.")
        if selected_outbounds and selected_outbounds[0] != expected_outbounds[tag]:
            raise ValidationError(
                f"Outbound {tag} существует, но его структура не принадлежит менеджеру."
            )
        selected_rules = [
            item
            for item in rules
            if isinstance(item, dict) and item.get("outboundTag") == tag
        ]
        if any(not _owned_warp_rule(item, tag) for item in selected_rules):
            raise ValidationError(
                f"Routing rule для {tag} существует, но его структура не принадлежит менеджеру."
            )
        tagged_outbounds[tag] = selected_outbounds
        tagged_rules[tag] = selected_rules
    return (
        tagged_outbounds[MANAGER_WARP_TAG] + tagged_outbounds[LEGACY_WARP_TAG],
        tagged_rules[MANAGER_WARP_TAG] + tagged_rules[LEGACY_WARP_TAG],
    )


def _profile_config(payload: object, context: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise TransactionError(f"Config Profile {context} имеет некорректный ответ.")
    response = payload.get("response")
    config = response.get("config") if isinstance(response, dict) else None
    if isinstance(config, str):
        try:
            config = json.loads(config, parse_constant=_reject_json_constant)
        except (ValueError, UnicodeDecodeError, RecursionError) as error:
            raise TransactionError(
                f"Config Profile {context} содержит некорректный JSON."
            ) from error
    if not isinstance(config, dict):
        raise TransactionError(f"Config Profile {context} не содержит объект config.")
    return config


def validate_warp_routing_inputs(
    profile_uuid: object,
    domains: object,
    *,
    remove: bool,
) -> tuple[str, list[str]]:
    selected_uuid = _input_uuid(profile_uuid, "Config Profile UUID")
    if not isinstance(remove, bool):
        raise ValidationError("Некорректный режим изменения WARP routing.")
    if not isinstance(domains, list) or any(
        not isinstance(domain, str) for domain in domains
    ):
        raise ValidationError("Домены WARP routing должны быть переданы списком строк.")
    normalized_domains = sorted({domain.lower() for domain in domains})
    if not remove and not normalized_domains:
        raise ValidationError("Нужно указать хотя бы один домен для WARP routing.")
    for domain in normalized_domains:
        _dns_name(domain, label="домен WARP routing", require_fqdn=True)
    return selected_uuid, normalized_domains


def configure_warp_routing(
    api: RemnawaveApi,
    token: str,
    store: StateStore,
    profile_uuid: str,
    domains: list[str],
    *,
    remove: bool = False,
) -> None:
    profile_uuid, normalized_domains = validate_warp_routing_inputs(
        profile_uuid,
        domains,
        remove=remove,
    )
    payload = api.request(
        "GET", f"/api/config-profiles/{profile_uuid}", token=token, expected=(200,)
    )
    config = _profile_config(payload, "до изменения")
    original_hash = _config_hash(config)
    backup_dir = store.paths.state / "api-backups"
    backup_name = (
        f"profile-{profile_uuid}-{utc_now().replace(':', '')}-{original_hash[:12]}.json"
    )

    outbounds = config.setdefault("outbounds", [])
    routing = config.setdefault("routing", {})
    if not isinstance(outbounds, list) or not isinstance(routing, dict):
        raise ValidationError("Неподдерживаемая структура outbounds/routing.")
    rules = routing.setdefault("rules", [])
    if not isinstance(rules, list):
        raise ValidationError("Неподдерживаемая структура routing.rules.")
    owned_outbounds, owned_rules = _validate_warp_ownership(outbounds, rules)
    if remove:
        config["outbounds"] = [
            item for item in outbounds if item not in owned_outbounds
        ]
        routing["rules"] = [item for item in rules if item not in owned_rules]
    else:
        expected_outbound = _warp_outbound(MANAGER_WARP_TAG, "UseIPv4")
        manager_outbound = next(
            (item for item in owned_outbounds if item.get("tag") == MANAGER_WARP_TAG),
            None,
        )
        config["outbounds"] = [
            item
            for item in outbounds
            if item not in owned_outbounds or item is manager_outbound
        ]
        if manager_outbound is None:
            config["outbounds"].append(expected_outbound)

        routing["rules"] = [item for item in rules if item not in owned_rules]
        routing["rules"].insert(
            0,
            {
                "type": "field",
                "domain": normalized_domains,
                "outboundTag": MANAGER_WARP_TAG,
            },
        )

    if _config_hash(config) == original_hash:
        return

    latest = api.request(
        "GET", f"/api/config-profiles/{profile_uuid}", token=token, expected=(200,)
    )
    latest_config = _profile_config(latest, "при повторной проверке")
    if _config_hash(latest_config) != original_hash:
        raise TransactionError("Config Profile изменился параллельно; PATCH отменён.")
    desired_hash = _config_hash(config)
    atomic_write_json(backup_dir / backup_name, latest_config, mode=0o600)
    api.request(
        "PATCH",
        "/api/config-profiles",
        token=token,
        data={"uuid": profile_uuid, "config": config},
        expected=(200,),
    )
    verified = api.request(
        "GET", f"/api/config-profiles/{profile_uuid}", token=token, expected=(200,)
    )
    verified_config = _profile_config(verified, "после PATCH")
    if _config_hash(verified_config) != desired_hash:
        raise TransactionError(
            "Panel не подтвердила итоговую конфигурацию WARP routing. "
            "Автоматический overwrite не выполнялся; исходный config сохранён в api-backups."
        )
