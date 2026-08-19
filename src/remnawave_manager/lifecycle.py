from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from .compose import compose_command
from .errors import TransactionError, ValidationError
from .health import (
    check_subscription_http,
    wait_container,
    wait_for_paths,
    wait_node_runtime,
    wait_panel_http,
)
from .models import Component, Inventory
from .nginx import test_nginx
from .runner import Runner

Action = Literal["start", "stop", "restart"]

_DURATION = re.compile(r"(?:[0-9]+(?:\.[0-9]+)?(?:ns|us|ms|s|m|h))+")
_UNIX_TIMESTAMP = re.compile(r"[0-9]+(?:\.[0-9]{1,9})?")
_ISO_TIMESTAMP = re.compile(
    r"(?P<date>[0-9]{4}-[0-9]{2}-[0-9]{2})"
    r"(?:T(?P<hour>[0-9]{2}):(?P<minute>[0-9]{2})"
    r"(?::(?P<second>[0-9]{2})(?:\.[0-9]{1,9})?)?"
    r"(?P<zone>Z|[+-][0-9]{2}:[0-9]{2})?)?"
)


def _component(inventory: Inventory, name: str) -> Component:
    try:
        return inventory.components[name]
    except KeyError as error:
        raise ValidationError(f"Компонент {name} не найден на этом сервере.") from error


def validate_log_since(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValidationError(
            "--since содержит пустое значение или управляющие символы."
        )
    if _DURATION.fullmatch(value) or _UNIX_TIMESTAMP.fullmatch(value):
        return value

    match = _ISO_TIMESTAMP.fullmatch(value)
    if match is not None:
        try:
            date.fromisoformat(match.group("date"))
            if match.group("hour") is not None:
                datetime(
                    2000,
                    1,
                    1,
                    int(match.group("hour")),
                    int(match.group("minute")),
                    int(match.group("second") or 0),
                    tzinfo=UTC,
                )
                zone = match.group("zone")
                if zone not in {None, "Z"}:
                    zone_hour, zone_minute = map(int, zone[1:].split(":"))
                    if zone_hour > 23 or zone_minute > 59:
                        raise ValueError
        except ValueError:
            pass
        else:
            return value

    raise ValidationError(
        "--since должен быть Docker duration, Unix timestamp или ISO/RFC3339 timestamp."
    )


def manage_component(
    runner: Runner, inventory: Inventory, component: str, action: Action
) -> None:
    compose = Path(inventory.compose_file)
    env = Path(inventory.env_file) if inventory.env_file else None
    if component == "all":
        if action == "start":
            arguments = ("up", "-d")
        elif action == "stop":
            arguments = ("stop",)
        elif action == "restart":
            arguments = ("restart",)
        else:
            raise ValidationError(f"Неизвестное действие: {action}")
    else:
        selected = _component(inventory, component)
        if action == "start":
            arguments = ("up", "-d", "--no-deps", selected.service)
        elif action == "stop":
            arguments = ("stop", selected.service)
        elif action == "restart":
            arguments = ("restart", selected.service)
        else:
            raise ValidationError(f"Неизвестное действие: {action}")
    runner.run(compose_command(compose, *arguments, env_file=env), cwd=compose.parent)
    if action != "stop" and getattr(runner, "dry_run", False) is not True:
        _verify_started_components(runner, inventory, component)


def _verify_started_components(
    runner: Runner,
    inventory: Inventory,
    selected: str,
) -> None:
    names = list(inventory.components) if selected == "all" else [selected]
    for name in names:
        wait_container(runner, _component(inventory, name))

    nginx_selected = selected == "all" or "nginx" in names
    if nginx_selected and inventory.webserver == "nginx":
        test_nginx(runner, inventory)
    if (
        selected == "all" or "node" in names or "nginx" in names
    ) and inventory.xhttp_sockets:
        wait_for_paths(inventory.xhttp_sockets)

    if inventory.role == "panel":
        if "panel" in names:
            wait_panel_http(runner, _component(inventory, "panel"))
        if "subscription" in names:
            check_subscription_http(runner, _component(inventory, "subscription"))
    elif "node" in names:
        wait_node_runtime(runner, inventory)


def component_logs(
    runner: Runner,
    inventory: Inventory,
    component: str,
    *,
    tail: int = 100,
    follow: bool = False,
    since: str | None = None,
) -> None:
    if isinstance(tail, bool) or not isinstance(tail, int) or not 1 <= tail <= 10_000:
        raise ValidationError("--tail должен быть целым числом от 1 до 10000.")
    if component == "all":
        compose = Path(inventory.compose_file)
        env = Path(inventory.env_file) if inventory.env_file else None
        arguments = ["logs", "--tail", str(tail)]
        if since is not None:
            arguments.extend(("--since", validate_log_since(since)))
        if follow:
            arguments.append("--follow")
        runner.interactive(
            compose_command(compose, *arguments, env_file=env), cwd=compose.parent
        )
        return
    selected = _component(inventory, component)
    command = ["docker", "logs", "--tail", str(tail)]
    if since is not None:
        command.extend(("--since", validate_log_since(since)))
    if follow:
        command.append("--follow")
    command.append(selected.container or selected.service)
    runner.interactive(command)


def panel_cli(runner: Runner, inventory: Inventory) -> None:
    selected = _component(inventory, "panel")
    runner.interactive(
        ["docker", "exec", "-it", selected.container or selected.service, "cli"]
    )


def component_status(
    runner: Runner, inventory: Inventory
) -> list[dict[str, str | None]]:
    output: list[dict[str, str | None]] = []
    for name, component in inventory.components.items():
        container = component.container or component.service
        result = runner.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{json .State}}",
                container,
            ],
            check=False,
        )
        status = "не создан"
        health: str | None = None
        if result.returncode != 0:
            daemon = runner.run(
                ["docker", "info", "--format", "{{json .ServerVersion}}"],
                check=False,
                timeout=30,
            )
            if daemon.returncode != 0:
                raise TransactionError(
                    "Docker daemon недоступен или текущий пользователь не может к нему подключиться."
                )
        else:
            try:
                state = json.loads(result.stdout)
                if not isinstance(state, dict):
                    raise TypeError
                health_state = state.get("Health") or {}
                if not isinstance(health_state, dict):
                    raise TypeError
                status = (
                    "запущен"
                    if state.get("Running")
                    else str(state.get("Status", "остановлен"))
                )
                health_value = health_state.get("Status")
                health = str(health_value) if health_value is not None else None
            except (json.JSONDecodeError, TypeError):
                status = "неизвестно"
                health = None
        output.append(
            {
                "component": name,
                "container": container,
                "status": status,
                "health": health,
            }
        )
    return output
