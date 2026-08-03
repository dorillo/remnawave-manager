from __future__ import annotations

import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

from .certificates import configure_adopted_certbot
from .compose import inspect_compose
from .errors import ValidationError
from .journal import TransactionJournal
from .models import Component, Inventory, ManagedFile, Role
from .runner import Runner, read_stable_regular_file, sha256_file
from .state import StateStore, utc_now

COMPOSE_NAMES = ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")
DEFAULT_DIRS = (Path("/opt/remnawave"), Path("/opt/remnanode"))
_MAX_NGINX_CONFIG_SIZE = 16 * 1024 * 1024


def _find_compose(directory: Path) -> Path:
    found: list[Path] = []
    for name in COMPOSE_NAMES:
        candidate = directory / name
        if candidate.is_file():
            if candidate.is_symlink():
                raise ValidationError(
                    f"Compose-файл {candidate} является symlink. "
                    "Adoption остановлен, чтобы update не заменил ссылку обычным файлом."
                )
            found.append(candidate)
    if len(found) > 1:
        raise ValidationError(
            f"В {directory} найдено несколько стандартных Compose-файлов: "
            + ", ".join(path.name for path in found)
            + ". Оставьте один однозначный файл перед adoption."
        )
    if found:
        return found[0]
    raise ValidationError(f"В {directory} не найден compose-файл.")


def _component_for(service_name: str, service: dict[str, Any]) -> str | None:
    image = str(service.get("image", "")).lower()
    container = str(service.get("container_name", "")).lower()
    joined = f"{service_name.lower()} {container} {image}"
    if "subscription-page" in joined:
        return "subscription"
    if "remnawave/node" in joined or "remnanode" in joined:
        return "node"
    if "remnawave/backend" in joined or container == "remnawave":
        return "panel"
    if "postgres" in image and ("remnawave" in joined or "postgres" in service_name.lower()):
        return "database"
    if ("nginx" in image or "nginx" in joined) and "subscription-page" not in joined:
        return "nginx"
    if "valkey" in joined or "redis" in joined:
        return "cache"
    return None


def _role(components: dict[str, Component], requested: Role | None) -> Role:
    has_panel = "panel" in components
    has_node = "node" in components
    if has_panel and has_node:
        raise ValidationError(
            "Совмещённая Panel+Node установка обнаружена, но не поддерживается новым менеджером. "
            "Сначала разнесите роли по серверам."
        )
    detected: Role | None = "panel" if has_panel else "node" if has_node else None
    if detected is None:
        raise ValidationError("Не удалось обнаружить сервис Panel или Node.")
    if requested is not None and requested != detected:
        raise ValidationError(f"Запрошена роль {requested}, но обнаружена роль {detected}.")
    return detected


def _bind_sources(service: dict[str, Any], target_fragment: str) -> list[Path]:
    expected_target = PurePosixPath(target_fragment.rstrip("/"))
    found: list[Path] = []
    for volume in service.get("volumes", []) or []:
        if not isinstance(volume, dict) or volume.get("type") != "bind":
            continue
        source = volume.get("source")
        target_value = volume.get("target")
        if not isinstance(target_value, str) or not target_value.startswith("/"):
            continue
        target = PurePosixPath(target_value)
        if ".." in target.parts:
            continue
        if source and (target == expected_target or expected_target in target.parents):
            found.append(Path(str(source)))
    return found


def _regular_files(source: Path) -> list[Path]:
    if source.is_symlink():
        raise ValidationError(f"Bind source nginx является symlink: {source}")
    if source.is_file():
        return [source]
    if not source.is_dir():
        raise ValidationError(
            f"Bind source nginx отсутствует или имеет небезопасный тип: {source}"
        )
    regular: list[Path] = []
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValidationError(
                f"Bind source nginx содержит символическую ссылку: {path}"
            )
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValidationError(
                f"Bind source nginx содержит неподдерживаемый объект: {path}"
            )
        regular.append(path)
    return regular


def _nginx_sources(compose: dict[str, Any], components: dict[str, Component]) -> list[Path]:
    services = compose["services"]
    paths: list[Path] = []
    nginx_component = components.get("nginx")
    if nginx_component:
        service = services[nginx_component.service]
        for source in _bind_sources(service, "/etc/nginx/"):
            paths.extend(_regular_files(source))
    else:
        sites = Path("/etc/nginx/sites-enabled")
        if sites.is_dir():
            for path in sites.iterdir():
                if not path.is_file():
                    continue
                try:
                    selected = path.resolve(strict=True)
                except (OSError, RuntimeError) as error:
                    raise ValidationError(
                        f"Не удалось безопасно разрешить конфигурацию nginx {path}."
                    ) from error
                text = _read_nginx_text(selected)
                if re.search(r"(?:remnawave|127\.0\.0\.1:(?:3000|3010))", text, re.IGNORECASE):
                    paths.append(selected)
    return sorted({path.resolve() for path in paths if path.is_file()}, key=str)


def _site_sources(compose: dict[str, Any], components: dict[str, Component]) -> list[Path]:
    nginx_component = components.get("nginx")
    if not nginx_component:
        return []
    service = compose["services"][nginx_component.service]
    paths: set[Path] = set()
    for path in _bind_sources(service, "/var/www/"):
        if path.is_symlink() or not path.is_dir():
            raise ValidationError(
                f"Bind source маскировочного сайта имеет небезопасный тип: {path}"
            )
        paths.add(path.absolute())
    return sorted(paths, key=str)


def _read_nginx_text(path: Path) -> str:
    snapshot = read_stable_regular_file(
        path,
        max_size=_MAX_NGINX_CONFIG_SIZE,
        label="Конфигурация nginx",
    )
    if os.name == "posix" and (
        snapshot.uid != os.geteuid() or snapshot.mode & 0o022
    ):
        raise ValidationError(
            f"Nginx-конфигурация {path} имеет небезопасного владельца или права."
        )
    try:
        return snapshot.data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValidationError(
            f"Nginx-конфигурация {path} не является корректным UTF-8."
        ) from error


def _inspect_container(runner: Runner, component: Component) -> None:
    name = component.container or component.service
    result = runner.run(
        [
            "docker",
            "inspect",
            "--format",
            "{{json .}}",
            name,
        ],
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        component.status = "not-created"
        return
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        component.status = "unknown"
        return
    component.running_image = data.get("Config", {}).get("Image")
    component.running_image_id = data.get("Image")
    state = data.get("State", {})
    component.status = "running" if state.get("Running") else str(state.get("Status", "stopped"))


def _warp_interfaces(runner: Runner) -> list[str]:
    result = runner.run(["ip", "-j", "link", "show"], check=False)
    if result.returncode != 0:
        raise ValidationError(
            "Не удалось проверить сетевые интерфейсы через ip; adoption остановлен, "
            "чтобы не потерять контроль существующего WARP."
        )
    try:
        links = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValidationError(
            "Команда ip вернула некорректный JSON; adoption остановлен."
        ) from error
    if not isinstance(links, list) or any(not isinstance(item, dict) for item in links):
        raise ValidationError(
            "Команда ip вернула неожиданный список интерфейсов; adoption остановлен."
        )
    return sorted(
        str(item["ifname"])
        for item in links
        if isinstance(item.get("ifname"), str)
        and item["ifname"].lower().startswith(("warp", "wgcf"))
    )


def _nginx_features(paths: list[Path]) -> tuple[list[str], dict[str, bool]]:
    combined = "\n".join(_read_nginx_text(path) for path in paths)
    sockets = sorted(
        set(
            re.findall(
                r"(?:listen|server)[ \t]+(?:unix:)?(/(?:dev/shm|run)/[^; \t]+\.sock(?:et)?)",
                combined,
            )
        )
    )
    features = {
        "xhttp_stream_separation": bool(sockets) or "xhttp" in combined.lower(),
        "yandex_cdn": "yandex" in combined.lower()
        or ("proxy_protocol_addr" in combined and "cdn" in combined.lower()),
        "cookie_gate": "$http_cookie" in combined
        or "$cookie_" in combined
        or "auth_cookie" in combined,
        "gzip": bool(re.search(r"(?m)^\s*gzip\s+on\s*;", combined)),
    }
    return sockets, features


def adopt(
    runner: Runner,
    store: StateStore,
    *,
    directory: Path | None = None,
    requested_role: Role | None = None,
    allow_active_transaction: bool = False,
) -> Inventory:
    if not allow_active_transaction:
        TransactionJournal.ensure_available(store)
    existing_inventory = (
        store.load_inventory()
        if store.paths.inventory.exists() or store.paths.inventory.is_symlink()
        else None
    )
    selected = directory
    if selected is None:
        candidates = [path for path in DEFAULT_DIRS if path.is_dir()]
        if len(candidates) != 1:
            raise ValidationError(
                "Укажите каталог явно: rwm adopt --path /opt/remnawave или /opt/remnanode."
            )
        selected = candidates[0]
    absolute = selected.absolute()
    for candidate in (absolute, *absolute.parents):
        if candidate.is_symlink():
            raise ValidationError(
                f"Каталог установки проходит через symlink: {candidate}. "
                "Укажите реальный абсолютный путь перед adoption."
            )
    selected = absolute.resolve()
    if existing_inventory is not None and Path(existing_inventory.install_dir).resolve() != selected:
        raise ValidationError(
            "Менеджер уже управляет другой установкой: "
            f"{existing_inventory.install_dir}. Сначала явно снимите её с управления."
        )
    compose_file = _find_compose(selected)
    env_candidate = selected / ".env"
    env_file = env_candidate if env_candidate.is_file() else None
    if env_file is not None and env_file.is_symlink():
        raise ValidationError(
            f"Env-файл {env_file} является symlink. "
            "Adoption остановлен, чтобы миграция не заменила ссылку обычным файлом."
        )
    compose = inspect_compose(runner, compose_file, env_file)

    components: dict[str, Component] = {}
    for service_name, service_data in compose["services"].items():
        kind = _component_for(service_name, service_data)
        if kind is None:
            continue
        if kind in components:
            previous = components[kind].service
            raise ValidationError(
                f"В Compose неоднозначно определён компонент {kind}: "
                f"сервисы {previous} и {service_name}. Разделите стек или укажите "
                "один однозначный сервис перед adoption."
            )
        component = Component(
            name=kind,
            service=service_name,
            container=service_data.get("container_name"),
            configured_image=service_data.get("image"),
        )
        _inspect_container(runner, component)
        components[kind] = component

    role = _role(components, requested_role)
    if existing_inventory is not None and existing_inventory.role != role:
        raise ValidationError(
            f"Текущая роль manager state ({existing_inventory.role}) не совпадает с обнаруженной ({role})."
        )
    nginx_paths = _nginx_sources(compose, components)
    site_paths = _site_sources(compose, components)
    sockets, features = _nginx_features(nginx_paths)
    features["containerized_nginx"] = "nginx" in components
    features["subscription_page"] = "subscription" in components
    warp = _warp_interfaces(runner)
    features["warp"] = bool(warp)

    protected = [compose_file, *nginx_paths]
    if env_file:
        protected.append(env_file)
    for path in selected.glob(".env*"):
        if path.is_symlink():
            raise ValidationError(f"Env-файл {path} является symlink; adoption остановлен.")
        if path.is_file() and path not in protected:
            protected.append(path)
    for site in site_paths:
        for path in site.rglob("*"):
            if path.is_symlink():
                raise ValidationError(
                    f"Файл маскировочного сайта {path} является symlink; adoption остановлен."
                )
            if path.is_file():
                protected.append(path)
    managed_files = [
        ManagedFile(path=str(path), sha256=sha256_file(path), kind=_file_kind(path, compose_file, env_file))
        for path in sorted(set(protected), key=str)
    ]
    inventory = Inventory(
        schema_version=1,
        role=role,
        install_dir=str(selected),
        compose_file=str(compose_file),
        env_file=str(env_file) if env_file else None,
        webserver="nginx" if nginx_paths else None,
        nginx_files=[str(path) for path in nginx_paths],
        site_dirs=[str(path) for path in site_paths],
        components=components,
        managed_files=managed_files,
        xhttp_sockets=sockets,
        warp_interfaces=warp,
        features=features,
        adopted_at=utc_now(),
    )
    configure_adopted_certbot(
        runner,
        inventory,
        compose,
        store=store,
    )
    return inventory


def _file_kind(path: Path, compose: Path, env: Path | None) -> str:
    if path == compose:
        return "compose"
    if env is not None and path == env:
        return "env"
    if path.name == ".env" or path.name.startswith(".env."):
        return "env"
    if "nginx" in path.name.lower() or "/nginx/" in path.as_posix():
        return "nginx"
    if "/var/www/" in path.as_posix() or path.name == ".rwm-template.json":
        return "site"
    return "config"
