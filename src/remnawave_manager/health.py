from __future__ import annotations

import base64
import json
import re
import stat
import time
from pathlib import Path

from .errors import NodeSecretValidationError, TransactionError, ValidationError
from .models import Component, Inventory
from .runner import Runner, sanitize_external_text

_NODE_SECRET_PROBE = r"""
import { X509Certificate, createPublicKey } from 'node:crypto';
import { readFileSync } from 'node:fs';

const fail = (code) => {
    process.stderr.write(`RWM_NODE_SECRET_INVALID:${code}\n`);
    process.exit(42);
};
const check = (code, callback) => {
    try {
        return callback();
    } catch {
        fail(code);
    }
};
const normalizePem = (pem) => {
    let value = pem.replace(/\\n/g, '\n');
    value = value.replace(/\r\n/g, '\n');
    value = value.replace(/(-----BEGIN [A-Z ]+-----)/g, '$1\n');
    value = value.replace(/(-----END [A-Z ]+-----)/g, '\n$1');
    value = value.replace(/\n+/g, '\n');
    return value.trim();
};
const secret = readFileSync(0, 'utf8');
const parsed = check('payload', () => JSON.parse(Buffer.from(secret, 'base64').toString('utf8')));
const fields = ['caCertPem', 'jwtPublicKey', 'nodeCertPem', 'nodeKeyPem'];
if (!parsed || typeof parsed !== 'object' || fields.some((key) => typeof parsed[key] !== 'string')) {
    fail('payload');
}
const ca = check('ca-parse', () => new X509Certificate(normalizePem(parsed.caCertPem)));
const node = check('node-parse', () => new X509Certificate(normalizePem(parsed.nodeCertPem)));
const now = new Date();
if (new Date(ca.validFrom) > now || new Date(ca.validTo) < now) fail('ca-time');
if (!check('ca-signature', () => ca.verify(ca.publicKey))) fail('ca-signature');
if (!check('node-signature', () => node.verify(ca.publicKey))) fail('node-signature');
const certKey = node.publicKey.export({ type: 'spki', format: 'der' });
const privateKey = check('node-key', () =>
    createPublicKey(normalizePem(parsed.nodeKeyPem)).export({ type: 'spki', format: 'der' })
);
if (!certKey.equals(privateKey)) fail('node-key');
check('jwt-key', () => createPublicKey(normalizePem(parsed.jwtPublicKey)));
process.stdout.write('RWM_NODE_SECRET_OK\n');
"""

_NODE_SECRET_FAILURES = {
    "payload": "некорректная структура или кодировка payload",
    "ca-parse": "сертификат CA не удалось прочитать",
    "ca-time": "сертификат CA ещё не действует или уже истёк",
    "ca-signature": "самоподпись CA некорректна",
    "node-parse": "сертификат Node не удалось прочитать",
    "node-signature": "сертификат Node не подписан указанным CA",
    "node-key": "приватный ключ Node не соответствует сертификату",
    "jwt-key": "публичный JWT-ключ некорректен",
}

_NODE_SECRET_ASSIGNMENT = re.compile(
    r"^(?:-\s*)?(?:export\s+)?SECRET_KEY\s*(?:=|:)\s*(?P<value>.+)$"
)
_NODE_SECRET_FIELDS = ("caCertPem", "jwtPublicKey", "nodeCertPem", "nodeKeyPem")


def normalize_node_secret(secret: str) -> str:
    """Extract a Node payload when it was copied with a safe UI/Compose wrapper."""
    if not isinstance(secret, str) or any(
        ord(character) < 32 or ord(character) == 127 for character in secret
    ):
        raise ValidationError("SECRET_KEY Node пуст или имеет небезопасный формат.")
    value = secret.strip()
    assignment = _NODE_SECRET_ASSIGNMENT.fullmatch(value)
    if assignment is not None:
        value = assignment.group("value").strip()

    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        if value[0] == '"':
            try:
                decoded = json.loads(value)
            except json.JSONDecodeError as error:
                raise ValidationError("SECRET_KEY Node содержит некорректные кавычки.") from error
            if not isinstance(decoded, str):
                raise ValidationError("SECRET_KEY Node имеет небезопасный формат.")
            value = decoded
        else:
            value = value[1:-1]

    try:
        response = json.loads(value)
    except json.JSONDecodeError:
        response = None
    if isinstance(response, dict):
        payload = response.get("secretKey")
        nested = response.get("response")
        if not isinstance(payload, str) and isinstance(nested, dict):
            payload = nested.get("secretKey")
        if not isinstance(payload, str):
            raise ValidationError(
                "JSON с SECRET_KEY должен содержать строковое поле secretKey."
            )
        value = payload

    if (
        not value
        or len(value) > 64 * 1024
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValidationError("SECRET_KEY Node пуст или имеет небезопасный формат.")
    return value


def validate_node_secret_payload(secret: str) -> str:
    """Normalize and validate the non-secret envelope before starting Docker."""
    value = normalize_node_secret(secret)
    encoded = value.replace("-", "+").replace("_", "/")
    encoded += "=" * (-len(encoded) % 4)
    try:
        decoded = base64.b64decode(encoded, validate=True)
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, UnicodeError, json.JSONDecodeError) as error:
        raise NodeSecretValidationError(
            "Введённое значение не является SECRET_KEY Node 3.3.2. "
            "Скопируйте полный SECRET_KEY именно из конфигурации нужной Node в Panel; "
            "API-токен, UUID, Public Key и X25519-ключ для этого не подходят."
        ) from error
    if not isinstance(payload, dict) or any(
        not isinstance(payload.get(field), str) or not payload[field].strip()
        for field in _NODE_SECRET_FIELDS
    ):
        raise NodeSecretValidationError(
            "В SECRET_KEY отсутствует полный payload Node 3.3.2 "
            "(caCertPem, jwtPublicKey, nodeCertPem и nodeKeyPem). "
            "Скопируйте SECRET_KEY из конфигурации нужной Node в Panel."
        )
    return value


def wait_container(
    runner: Runner,
    component: Component,
    *,
    timeout: int = 300,
    require_health: bool = False,
) -> None:
    container = component.container or component.service
    deadline = time.monotonic() + timeout
    last = "контейнер не найден"
    while time.monotonic() < deadline:
        result = runner.run(
            ["docker", "inspect", "--format", "{{json .State}}", container],
            check=False,
            timeout=30,
        )
        if result.returncode == 0:
            try:
                state = json.loads(result.stdout)
            except json.JSONDecodeError:
                state = {}
            if not isinstance(state, dict):
                state = {}
            if not state.get("Running"):
                last = sanitize_external_text(
                    str(state.get("Error") or state.get("Status") or "остановлен"),
                    limit=1000,
                )
                if state.get("Status") in {"exited", "dead"}:
                    break
            else:
                health_state = state.get("Health") or {}
                health = (
                    health_state.get("Status")
                    if isinstance(health_state, dict)
                    else None
                )
                if health == "unhealthy":
                    last = "healthcheck: unhealthy"
                    break
                if health == "healthy" or (not require_health and health in {None, ""}):
                    return
                last = "healthcheck: " + sanitize_external_text(str(health), limit=500)
        time.sleep(3)
    logs = runner.run(
        ["docker", "logs", "--tail", "80", container],
        check=False,
        sensitive=True,
        timeout=30,
    )
    detail = logs.stderr.strip() or logs.stdout.strip()
    raise TransactionError(
        f"Контейнер {container} не прошёл проверку: {last}."
        + (
            f"\nПоследние логи скрыты из-за возможных секретов; строк: {len(detail.splitlines())}."
            if detail
            else ""
        )
    )


def _container_http_url(
    runner: Runner,
    component: Component,
    *,
    default_port: int,
    path: str,
    container_loopback: bool = False,
) -> str:
    container = component.container or component.service
    result = runner.run(
        ["docker", "inspect", "--format", "{{json .}}", container],
        check=False,
        sensitive=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise TransactionError(
            f"Не удалось определить локальный HTTP-порт контейнера {container}."
        )
    try:
        details = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise TransactionError(
            f"Docker вернул некорректное описание контейнера {container}."
        ) from error
    if not isinstance(details, dict):
        raise TransactionError(
            f"Docker вернул некорректное описание контейнера {container}."
        )

    port = default_port
    config = details.get("Config") or {}
    if not isinstance(config, dict):
        raise TransactionError(
            f"Docker вернул некорректное описание контейнера {container}."
        )
    for assignment in config.get("Env", []) or []:
        if isinstance(assignment, str) and assignment.startswith("APP_PORT="):
            try:
                port = int(assignment.split("=", 1)[1])
            except ValueError as error:
                raise ValidationError(
                    f"Контейнер {container} содержит некорректный APP_PORT."
                ) from error
    if not 1 <= port <= 65535:
        raise ValidationError(f"Контейнер {container} содержит некорректный APP_PORT.")

    if container_loopback:
        return f"http://127.0.0.1:{port}{path}"

    host_config = details.get("HostConfig") or {}
    if not isinstance(host_config, dict):
        raise TransactionError(
            f"Docker вернул некорректное описание контейнера {container}."
        )
    network_mode = str(host_config.get("NetworkMode", ""))
    if network_mode == "host":
        return f"http://127.0.0.1:{port}{path}"

    network_settings = details.get("NetworkSettings") or {}
    ports = (
        network_settings.get("Ports") or {}
        if isinstance(network_settings, dict)
        else {}
    )
    bindings = ports.get(f"{port}/tcp") if isinstance(ports, dict) else None
    if not isinstance(bindings, list):
        raise TransactionError(
            f"Контейнер {container} не публикует APP_PORT {port}/tcp на localhost."
        )
    candidates: list[tuple[int, int]] = []
    priority = {
        "127.0.0.1": 0,
        "0.0.0.0": 1,  # noqa: S104, RUF100 - inspected Docker binding, not a listener
        "": 1,
    }
    for binding in bindings:
        if not isinstance(binding, dict):
            continue
        host_ip = str(binding.get("HostIp", ""))
        try:
            host_port = int(str(binding.get("HostPort", "")))
        except ValueError:
            continue
        if host_ip in priority and 1 <= host_port <= 65535:
            candidates.append((priority[host_ip], host_port))
    if not candidates:
        raise TransactionError(
            f"Контейнер {container} не публикует APP_PORT {port}/tcp на IPv4 localhost."
        )
    host_port = min(candidates)[1]
    return f"http://127.0.0.1:{host_port}{path}"


def check_panel_http(runner: Runner, component: Component) -> None:
    url = _container_http_url(
        runner,
        component,
        default_port=3000,
        path="/api/auth/status",
    )
    result = runner.run(
        [
            "curl",
            "--silent",
            "--show-error",
            "--fail",
            "--max-time",
            "15",
            "--max-filesize",
            "1048576",
            "--noproxy",
            "*",
            "--header",
            "X-Forwarded-For: 127.0.0.1",
            "--header",
            "X-Forwarded-Proto: https",
            url,
        ],
        check=False,
        timeout=30,
    )
    if result.returncode != 0:
        raise TransactionError("Panel не отвечает на локальный /api/auth/status.")
    try:
        payload = json.loads(result.stdout)
        response = payload["response"]
    except (json.JSONDecodeError, KeyError, TypeError) as error:
        raise TransactionError(
            "Panel вернула некорректный /api/auth/status."
        ) from error
    if not isinstance(response, dict):
        raise TransactionError("Panel вернула некорректный /api/auth/status.")
    if response.get("isRegisterAllowed") is not False:
        raise TransactionError(
            "Panel не подтвердила запрет регистрации администратора. Это небезопасное состояние."
        )
    if response.get("isLoginAllowed") is not True:
        raise TransactionError("После обновления Panel не разрешает обычный вход.")


def wait_panel_http(
    runner: Runner,
    component: Component,
    *,
    timeout: int = 90,
    interval: int = 3,
) -> None:
    """Wait until the main API is ready after the metrics healthcheck passes."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            check_panel_http(runner, component)
            return
        except TransactionError as error:
            if time.monotonic() >= deadline:
                raise TransactionError(
                    f"Panel не стала готова за {timeout} секунд: {error}"
                ) from error
            time.sleep(interval)


def check_subscription_http(
    runner: Runner,
    component: Component,
    *,
    timeout: int = 90,
    legacy: bool = False,
) -> None:
    url = _container_http_url(
        runner,
        component,
        default_port=3010,
        path="/" if legacy else "/internal/health",
        container_loopback=not legacy,
    )
    deadline = time.monotonic() + timeout
    while True:
        command: list[str] = []
        if not legacy:
            command += [
                "docker",
                "exec",
                component.container or component.service,
            ]
        command += [
                "curl",
                "--silent",
                "--show-error",
                "--max-time",
                "15",
                "--noproxy",
                "*",
                "--output",
                "/dev/null",
            ]
        if legacy:
            command += ["--write-out", "%{http_code}"]
        else:
            command.append("--fail")
        command.append(url)
        result = runner.run(
            command,
            check=False,
            timeout=30,
        )
        if legacy:
            status = result.stdout.strip()
            http_response = (
                result.returncode == 0
                and len(status) == 3
                and status.isascii()
                and status.isdigit()
                and 200 <= int(status) < 500
            )
            # Version 7.2.6 deliberately destroys sockets for unknown requests.
            closed_by_application = result.returncode == 52 and status == "000"
            ready = http_response or closed_by_application
        else:
            ready = result.returncode == 0
        if ready:
            return
        if time.monotonic() >= deadline:
            if legacy:
                raise TransactionError(
                    "Subscription Page 7.2.6 не отвечает на локальную "
                    "liveness-проверку /."
                )
            raise TransactionError(
                "Subscription Page не отвечает на локальный /internal/health."
            )
        time.sleep(3)


def check_subscription_api_scopes(
    runner: Runner,
    panel: Component,
    subscription: Component,
) -> None:
    """Verify every read-only Panel scope required by Subscription Page v8."""

    del panel  # The probe must use the same route as Subscription Page itself.
    subscription_container = subscription.container or subscription.service
    missing: list[str] = []
    probes = (
        ("system:metadata", "/api/system/metadata", {200}),
        (
            "users:by-username",
            "/api/users/by-username/__rwm_scope_probe__",
            {200, 400, 404},
        ),
        (
            "subscription-page-configs:list",
            "/api/subscription-page-configs",
            {200},
        ),
        (
            "subscription-page-configs:get",
            "/api/subscription-page-configs/ffffffff-ffff-4fff-8fff-ffffffffffff",
            {200, 400, 404},
        ),
        (
            "subscriptions:by-short-uuid-protected",
            "/api/subscriptions/by-short-uuid/__rwm_scope_probe__",
            {200, 400, 404},
        ),
        (
            "subscriptions:subpage-config",
            "/api/subscriptions/subpage-config/__rwm_scope_probe__",
            {200, 400, 404},
        ),
    )
    probe_script = """\
const base = process.env.REMNAWAVE_PANEL_URL;
const token = process.env.REMNAWAVE_API_TOKEN;
if (!base || !token) process.exit(65);
const target = new URL(process.argv.at(-1), base.endsWith('/') ? base : `${base}/`);
if (!['http:', 'https:'].includes(target.protocol)) process.exit(64);
const headers = {
    Authorization: `Bearer ${token}`,
    'user-agent': 'Remnawave Subscription Page',
};
if (target.protocol === 'http:') {
    headers['X-Forwarded-For'] = '127.0.0.1';
    headers['X-Forwarded-Proto'] = 'https';
}
if (process.env.CADDY_AUTH_API_TOKEN) {
    headers['X-Api-Key'] = process.env.CADDY_AUTH_API_TOKEN;
}
if (process.env.CLOUDFLARE_ZERO_TRUST_CLIENT_ID &&
    process.env.CLOUDFLARE_ZERO_TRUST_CLIENT_SECRET) {
    headers['CF-Access-Client-Id'] = process.env.CLOUDFLARE_ZERO_TRUST_CLIENT_ID;
    headers['CF-Access-Client-Secret'] = process.env.CLOUDFLARE_ZERO_TRUST_CLIENT_SECRET;
}
if (process.env.EGAMES_COOKIE) {
    headers.Cookie = process.env.EGAMES_COOKIE;
}
const response = await fetch(target, {
    headers,
    signal: AbortSignal.timeout(15_000),
});
await response.body?.cancel();
process.stdout.write(String(response.status));
"""
    for scope, path, accepted_statuses in probes:
        result = runner.run(
            [
                "docker",
                "exec",
                subscription_container,
                "node",
                "--input-type=module",
                "--eval",
                probe_script,
                path,
            ],
            check=False,
            sensitive=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise TransactionError(
                f"Не удалось выполнить локальную проверку scope {scope} "
                f"(код probe {result.returncode})."
            )
        try:
            status = int(result.stdout.strip())
        except ValueError as error:
            raise TransactionError(
                f"Panel не вернула HTTP-статус при проверке scope {scope}."
            ) from error
        if status in {401, 403}:
            missing.append(scope)
        elif status not in accepted_statuses:
            raise TransactionError(
                f"Panel вернула неожиданный HTTP {status} при проверке scope {scope}."
            )
    if missing:
        raise TransactionError(
            "API-токен Subscription Page недействителен или не имеет обязательных scopes: "
            + ", ".join(missing)
            + ". Обновите scopes токена в Panel и повторите операцию."
        )


def check_node_runtime(runner: Runner, inventory: Inventory) -> None:
    component = inventory.components["node"]
    container = component.container or component.service
    checks = (
        [
            "docker",
            "exec",
            container,
            "/command/s6-svstat",
            "-o",
            "up,pid",
            "/run/service/xray",
        ],
        ["docker", "exec", container, "rw-core", "version"],
        ["docker", "exec", container, "cli", "--dump-config-raw"],
    )
    for index, command in enumerate(checks):
        result = runner.run(
            command,
            check=False,
            sensitive=index == 2,
            timeout=60,
        )
        if result.returncode != 0:
            raise TransactionError(f"Node не прошла runtime-проверку {index + 1}.")
        if index == 0:
            fields = result.stdout.split()
            if (
                len(fields) != 2
                or fields[0] != "true"
                or not fields[1].isdigit()
                or int(fields[1]) <= 0
            ):
                raise TransactionError(
                    "Сервис Xray внутри Node не находится в состоянии up."
                )
        if index == 2:
            try:
                config = json.loads(result.stdout)
            except json.JSONDecodeError as error:
                raise TransactionError(
                    "Node вернула некорректный Xray JSON."
                ) from error
            if not isinstance(config, dict):
                raise TransactionError(
                    "Xray-конфигурация Node не является JSON-объектом."
                )


def wait_node_runtime(
    runner: Runner,
    inventory: Inventory,
    *,
    timeout: int = 90,
    interval: int = 3,
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            check_node_runtime(runner, inventory)
            return
        except TransactionError as error:
            if time.monotonic() >= deadline:
                raise TransactionError(
                    f"Node не стала готова за {timeout} секунд: {error}"
                ) from error
            time.sleep(interval)


def validate_node_secret(runner: Runner, image: str, secret: str) -> None:
    """Validate the 3.3.2 SECRET_KEY contract without persisting the secret."""
    secret = validate_node_secret_payload(secret)
    result = runner.run(
        [
            "docker",
            "run",
            "--rm",
            "--interactive",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--entrypoint",
            "node",
            image,
            "--input-type=module",
            "--eval",
            _NODE_SECRET_PROBE,
        ],
        input_text=secret,
        check=False,
        sensitive=True,
        timeout=60,
    )
    marker = "RWM_NODE_SECRET_INVALID:"
    failure_code = next(
        (
            line.removeprefix(marker).strip()
            for line in (result.stderr + "\n" + result.stdout).splitlines()
            if line.startswith(marker)
        ),
        None,
    )
    if result.returncode != 0 and failure_code in _NODE_SECRET_FAILURES:
        raise NodeSecretValidationError(
            "SECRET_KEY отклонён валидатором Node 3.3.2: "
            + _NODE_SECRET_FAILURES[failure_code]
            + ". Скопируйте новый SECRET_KEY из конфигурации нужной Node "
            "в обновлённой Panel. "
            "Текущий образ Node не переключён."
        )
    if result.returncode != 0 or result.stdout.strip() != "RWM_NODE_SECRET_OK":
        raise TransactionError(
            "Не удалось выполнить изолированный валидатор SECRET_KEY в образе Node 3.3.2. "
            "Это ошибка запуска preflight, а не подтверждение повреждения ключа; "
            "текущий образ Node не переключён."
        )


def _missing_unix_sockets(paths: list[str]) -> list[str]:
    missing: list[str] = []
    for value in paths:
        path = Path(value)
        try:
            if path.is_symlink() or not stat.S_ISSOCK(path.stat().st_mode):
                missing.append(value)
        except OSError:
            missing.append(value)
    return missing


def wait_for_paths(paths: list[str], *, timeout: int = 90) -> None:
    deadline = time.monotonic() + timeout
    missing = _missing_unix_sockets(paths)
    while missing and time.monotonic() < deadline:
        time.sleep(2)
        missing = _missing_unix_sockets(paths)
    if missing:
        raise TransactionError(
            "После перезапуска не появились корректные XHTTP Unix-сокеты: "
            + ", ".join(missing)
        )
