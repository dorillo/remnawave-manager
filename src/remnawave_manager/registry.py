from __future__ import annotations

import getpass
import json
import unicodedata

from .compat import component_target
from .errors import TransactionError, ValidationError
from .runner import Runner, docker_config_directory, sanitize_external_text
from .state import StateStore, _read_private_json

REGISTRIES = {
    "docker-hub": "docker.io",
    "ghcr": "ghcr.io",
}


def validate_registry_username(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError("Имя пользователя Docker Registry некорректно.")
    selected = value.strip()
    if (
        not selected
        or selected != value
        or len(selected) > 256
        or not selected.isascii()
        or any(not 33 <= ord(character) <= 126 for character in selected)
    ):
        raise ValidationError("Имя пользователя Docker Registry некорректно.")
    return selected


def _validated_password(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 16_384
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"}
            for character in value
        )
    ):
        raise ValidationError("Пароль или access token некорректен.")
    return value


def registry_login(
    runner: Runner,
    registry: str,
    *,
    username: str | None = None,
    password: str | None = None,
) -> None:
    try:
        host = REGISTRIES[registry]
    except KeyError as error:
        raise ValidationError(f"Неизвестный registry: {registry}") from error
    selected_user = validate_registry_username(
        username if username is not None else input(f"Имя пользователя для {host}: ")
    )
    selected_password = _validated_password(
        password
        if password is not None
        else getpass.getpass("Пароль или access token: ")
    )
    runner.run(
        ["docker", "login", host, "--username", selected_user, "--password-stdin"],
        input_text=selected_password + "\n",
        sensitive=True,
    )


def registry_logout(runner: Runner, registry: str) -> None:
    try:
        host = REGISTRIES[registry]
    except KeyError as error:
        raise ValidationError(f"Неизвестный registry: {registry}") from error
    runner.run(["docker", "logout", host])


def select_registry(store: StateStore, registry: str) -> None:
    if registry not in REGISTRIES:
        raise ValidationError(f"Неизвестный registry: {registry}")
    settings = store.load_settings()
    settings["registry"] = registry
    store.save_settings(settings)


def pull_verified(runner: Runner, component: str, registry: str) -> str:
    target = component_target(component, registry)
    image = target["image"]
    result = runner.run(["docker", "pull", image], check=False, timeout=1800)
    if result.returncode != 0:
        detail = sanitize_external_text(
            result.stderr, limit=2000
        ) or sanitize_external_text(result.stdout, limit=2000)
        raise TransactionError(
            f"Не удалось скачать {image}. На сервере с ограниченным доступом сначала выполните "
            f"rwm registry login --registry {registry}."
            + (f"\n{detail}" if detail else "")
        )
    expected = target.get("digest")
    if expected:
        inspect = runner.run(
            ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image]
        )
        try:
            digests = json.loads(inspect.stdout)
        except (json.JSONDecodeError, RecursionError) as error:
            raise TransactionError(
                f"Docker не вернул digest образа {image}."
            ) from error
        if not isinstance(digests, list) or any(
            not isinstance(value, str) for value in digests
        ):
            raise TransactionError(
                f"Docker вернул некорректный список digest образа {image}."
            )
        if not any(value.endswith("@" + expected) for value in digests):
            raise TransactionError(
                f"Digest {image} не совпал с проверенным manifest ({expected}). "
                "Обновите compatibility manifest только после проверки релиза."
            )
    return f"{image}@{expected}" if expected else image


def registry_status(store: StateStore) -> dict[str, object]:
    selected = store.load_settings().get("registry", "docker-hub")
    if not isinstance(selected, str) or selected not in REGISTRIES:
        raise ValidationError(
            "В настройках менеджера указан неизвестный Docker Registry."
        )
    config = docker_config_directory() / "config.json"
    authenticated: list[str] = []
    if config.exists() or config.is_symlink():
        try:
            data = _read_private_json(
                config,
                label="Docker config.json",
                max_size=1 * 1024 * 1024,
            )
            auths = data.get("auths") if isinstance(data, dict) else None
            if isinstance(auths, dict):
                authenticated = sorted(
                    host
                    for host in auths
                    if isinstance(host, str)
                    and len(host) <= 512
                    and host.isascii()
                    and all(33 <= ord(character) <= 126 for character in host)
                )[:256]
        except (
            OSError,
            UnicodeError,
            ValidationError,
            json.JSONDecodeError,
            RecursionError,
        ):
            pass
    return {"selected": selected, "authenticated_hosts": authenticated}
