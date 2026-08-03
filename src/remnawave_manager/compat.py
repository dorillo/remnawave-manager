from __future__ import annotations

import json
import re
from dataclasses import replace
from importlib.resources import files
from typing import TYPE_CHECKING, Any

from .errors import ValidationError

if TYPE_CHECKING:
    from .models import Component
    from .runner import Runner


_IMAGE_ID = re.compile(r"sha256:[0-9A-Za-z]{1,128}")
_SAFE_CONTAINER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def load_manifest() -> dict[str, Any]:
    resource = files("remnawave_manager").joinpath("data/compatibility.json")
    data = json.loads(resource.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ValidationError("Неизвестная версия compatibility manifest.")
    return data


def component_target(name: str, registry: str = "docker-hub") -> dict[str, str]:
    manifest = load_manifest()
    try:
        component = manifest["components"][name]
        image = component["registries"][registry]
    except KeyError as error:
        raise ValidationError(f"Нет проверенного образа для {name} в registry {registry}.") from error
    return {
        "version": component["version"],
        "image": image["image"],
        "digest": image.get("digest", ""),
    }


def detect_component_version(
    runner: Runner,
    name: str,
    component: Component,
) -> str | None:
    contract = load_manifest().get("components", {}).get(name)
    if not isinstance(contract, dict):
        raise ValidationError(f"Компонент {name} отсутствует в compatibility manifest.")
    known = contract.get("known_digests", {})
    if not isinstance(known, dict):
        raise ValidationError(f"known_digests компонента {name} повреждён.")
    by_digest = {str(digest): str(version) for version, digest in known.items()}

    # A running container is the authoritative source. Falling back to a
    # supported Compose tag after an unknown container image would approve a
    # migration from code that is not actually described by that Compose file.
    running_references = [
        value
        for value in (component.running_image,)
        if isinstance(value, str) and value
    ]
    configured_references = [
        value
        for value in (component.configured_image,)
        if isinstance(value, str) and value
    ]
    references = running_references or configured_references
    for reference in references:
        embedded = re.search(r"@(sha256:[0-9a-f]{64})$", reference)
        if embedded and embedded.group(1) in by_digest:
            return by_digest[embedded.group(1)]

    image_id = component.running_image_id or (references[0] if references else None)
    if image_id:
        result = runner.run(
            ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image_id],
            check=False,
            sensitive=True,
        )
        if result.returncode == 0:
            try:
                repo_digests = json.loads(result.stdout)
            except json.JSONDecodeError:
                repo_digests = []
            for reference in repo_digests or []:
                match = re.search(r"@(sha256:[0-9a-f]{64})$", str(reference))
                if match and match.group(1) in by_digest:
                    return by_digest[match.group(1)]

    # Tags are mutable registry pointers and cannot prove which code will be
    # migrated. Adoption records the running image ID when a container exists;
    # an offline/tag-only import therefore requires the explicit risk override.
    return None


def _live_component_provenance(runner: Runner, component: Component) -> Component:
    container = component.container or component.service
    if _SAFE_CONTAINER_NAME.fullmatch(container) is None:
        raise ValidationError(
            f"Нельзя безопасно проверить runtime provenance компонента {component.name}."
        )
    inspected = runner.run(
        ["docker", "inspect", "--format", "{{json .}}", container],
        check=False,
        sensitive=True,
    )
    if inspected.returncode != 0 or not inspected.stdout.strip():
        daemon = runner.run(
            ["docker", "info", "--format", "{{json .ServerVersion}}"],
            check=False,
            sensitive=True,
        )
        try:
            server_version = json.loads(daemon.stdout)
        except (json.JSONDecodeError, RecursionError):
            server_version = None
        if (
            daemon.returncode != 0
            or not isinstance(server_version, str)
            or not server_version.strip()
        ):
            raise ValidationError(
                "Docker daemon недоступен; runtime provenance образов нельзя проверить."
            )
        if component.running_image is not None or component.running_image_id is not None:
            raise ValidationError(
                f"Контейнер {container} исчез после adoption. Повторите rwm adopt перед update."
            )
        return component

    try:
        data = json.loads(inspected.stdout)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValidationError(
            f"Docker вернул некорректную runtime provenance контейнера {container}."
        ) from error
    if not isinstance(data, dict):
        raise ValidationError(
            f"Docker вернул некорректную runtime provenance контейнера {container}."
        )
    config = data.get("Config")
    live_reference = config.get("Image") if isinstance(config, dict) else None
    live_image_id = data.get("Image")
    if (
        not isinstance(live_reference, str)
        or not live_reference
        or len(live_reference) > 2048
        or any(character.isspace() or ord(character) < 32 for character in live_reference)
        or not isinstance(live_image_id, str)
        or _IMAGE_ID.fullmatch(live_image_id) is None
    ):
        raise ValidationError(
            f"Docker вернул некорректную runtime provenance контейнера {container}."
        )
    if (
        component.running_image_id is not None
        and component.running_image_id != live_image_id
    ):
        raise ValidationError(
            f"Образ контейнера {container} изменился после adoption. "
            "Проверьте изменение и повторите rwm adopt перед update."
        )
    if (
        component.running_image is not None
        and component.running_image != live_reference
    ):
        raise ValidationError(
            f"Ссылка на образ контейнера {container} изменилась после adoption. "
            "Проверьте изменение и повторите rwm adopt перед update."
        )
    return replace(
        component,
        running_image=live_reference,
        running_image_id=live_image_id,
    )


def require_supported_source(
    runner: Runner,
    name: str,
    component: Component,
    *,
    accept_unknown: bool = False,
) -> str | None:
    contract = load_manifest().get("components", {}).get(name)
    if not isinstance(contract, dict):
        raise ValidationError(f"Компонент {name} отсутствует в compatibility manifest.")
    live_component = _live_component_provenance(runner, component)
    version = detect_component_version(runner, name, live_component)
    if version is None:
        if accept_unknown:
            return None
        reference = live_component.running_image or live_component.configured_image or "<неизвестный образ>"
        raise ValidationError(
            f"Не удалось доказать исходную версию {name} для {reference}. "
            "Автоматическая миграция остановлена. Проверьте версию и повторите "
            "с --accept-unknown-source только если принимаете риск несовместимости."
        )
    supported = {str(item) for item in contract.get("upgrade_from", [])}
    if version not in supported:
        raise ValidationError(
            f"Обновление {name} с версии {version} до {contract.get('version')} не проверено."
        )
    return version
