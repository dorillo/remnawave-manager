from __future__ import annotations

import json
import re
import stat
import time
from pathlib import Path

from .errors import TransactionError, ValidationError
from .models import Component, Inventory
from .runner import Runner, sanitize_external_text


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

    subscription_container = subscription.container or subscription.service
    token_result = runner.run(
        [
            "docker",
            "exec",
            subscription_container,
            "printenv",
            "REMNAWAVE_API_TOKEN",
        ],
        check=False,
        sensitive=True,
        timeout=30,
    )
    token = token_result.stdout.strip()
    if token_result.returncode != 0 or not re.fullmatch(
        r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){2}", token
    ):
        raise TransactionError(
            "Не удалось безопасно прочитать JWT-токен Subscription Page для проверки scopes."
        )

    origin = _container_http_url(
        runner,
        panel,
        default_port=3000,
        path="",
        container_loopback=True,
    )
    panel_container = panel.container or panel.service
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
    curl_config = f'header = "Authorization: Bearer {token}"\n'
    for scope, path, accepted_statuses in probes:
        result = runner.run(
            [
                "docker",
                "exec",
                "-i",
                panel_container,
                "curl",
                "--silent",
                "--show-error",
                "--output",
                "/dev/null",
                "--write-out",
                "%{http_code}",
                "--max-time",
                "15",
                "--noproxy",
                "*",
                "--config",
                "-",
                origin + path,
            ],
            input_text=curl_config,
            check=False,
            sensitive=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise TransactionError(
                f"Не удалось выполнить локальную проверку scope {scope}."
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
