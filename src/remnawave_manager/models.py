from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, Literal

Role = Literal["panel", "node"]


@dataclass(slots=True)
class ManagedFile:
    path: str
    sha256: str
    kind: str


@dataclass(slots=True)
class Component:
    name: str
    service: str
    container: str | None = None
    configured_image: str | None = None
    running_image: str | None = None
    running_image_id: str | None = None
    status: str | None = None


@dataclass(slots=True)
class Inventory:
    schema_version: int
    role: Role
    install_dir: str
    compose_file: str
    env_file: str | None
    webserver: str | None
    nginx_files: list[str] = field(default_factory=list)
    site_dirs: list[str] = field(default_factory=list)
    components: dict[str, Component] = field(default_factory=dict)
    managed_files: list[ManagedFile] = field(default_factory=list)
    xhttp_sockets: list[str] = field(default_factory=list)
    warp_interfaces: list[str] = field(default_factory=list)
    features: dict[str, bool] = field(default_factory=dict)
    adopted_at: str | None = None

    @property
    def directory(self) -> Path:
        return Path(self.install_dir)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Inventory:
        if not isinstance(data, dict):
            raise TypeError("Inventory должен быть JSON-объектом.")
        values = dict(data)
        raw_components = values.get("components", {})
        if not isinstance(raw_components, dict):
            raise TypeError("components должен быть JSON-объектом.")
        components: dict[str, Component] = {}
        for key, item in raw_components.items():
            if not isinstance(key, str) or not isinstance(item, dict):
                raise TypeError("Некорректный компонент inventory.")
            component = Component(**item)
            if component.name != key:
                raise ValueError(
                    f"Имя компонента {component.name!r} не совпадает с ключом {key!r}."
                )
            components[key] = component
        values["components"] = components

        raw_managed = values.get("managed_files", [])
        if not isinstance(raw_managed, list):
            raise TypeError("managed_files должен быть JSON-массивом.")
        managed_files: list[ManagedFile] = []
        for item in raw_managed:
            if not isinstance(item, dict):
                raise TypeError("Некорректный managed-файл inventory.")
            managed_files.append(ManagedFile(**item))
        values["managed_files"] = managed_files

        inventory = cls(**values)
        inventory.validate()
        return inventory

    def validate(self) -> None:
        if isinstance(self.schema_version, bool) or not isinstance(
            self.schema_version, int
        ):
            raise TypeError("schema_version inventory должен быть целым числом.")
        if self.schema_version != 1:
            raise ValueError("Версия inventory не поддерживается.")
        if self.role not in {"panel", "node"}:
            raise ValueError("Некорректная роль inventory.")
        install_dir, _ = _absolute_path(self.install_dir, "install_dir")
        if _is_path_root(install_dir):
            raise ValueError(
                "install_dir inventory не может быть корнем файловой системы."
            )
        compose_file, _ = _absolute_path(self.compose_file, "compose_file")
        _require_child_path(compose_file, install_dir, "compose_file")
        if self.env_file is not None:
            env_file, _ = _absolute_path(self.env_file, "env_file")
            _require_child_path(env_file, install_dir, "env_file")
        if self.webserver not in {None, "nginx"}:
            raise ValueError("Некорректный webserver inventory.")

        if not isinstance(self.components, dict):
            raise TypeError("components inventory должен быть JSON-объектом.")
        for key, component in self.components.items():
            if not isinstance(component, Component):
                raise TypeError("Некорректный компонент inventory.")
            _safe_name(key, "ключ component")
            _safe_name(component.name, "component.name")
            if component.name != key:
                raise ValueError(
                    f"Имя компонента {component.name!r} не совпадает с ключом {key!r}."
                )
            _safe_name(component.service, "component.service")
            if component.container is not None:
                _safe_name(component.container, "component.container")
            for label, value in (
                ("component.configured_image", component.configured_image),
                ("component.running_image", component.running_image),
                ("component.running_image_id", component.running_image_id),
                ("component.status", component.status),
            ):
                if value is not None:
                    _nonempty_text(value, label)

        if not isinstance(self.managed_files, list):
            raise TypeError("managed_files inventory должен быть JSON-массивом.")
        managed_paths: set[str] = set()
        for item in self.managed_files:
            if not isinstance(item, ManagedFile):
                raise TypeError("Некорректный managed-файл inventory.")
            _, path_key = _absolute_path(item.path, "managed_file.path")
            if path_key in managed_paths:
                raise ValueError("Managed-файл повторяется в inventory.")
            managed_paths.add(path_key)
            if (
                not isinstance(item.sha256, str)
                or len(item.sha256) != 64
                or any(character not in "0123456789abcdef" for character in item.sha256)
            ):
                raise ValueError("Некорректный sha256 managed-файла inventory.")
            _nonempty_text(item.kind, "managed_file.kind")

        path_collections = (
            ("nginx_files", self.nginx_files),
            ("site_dirs", self.site_dirs),
            ("xhttp_sockets", self.xhttp_sockets),
        )
        for label, values in path_collections:
            if not isinstance(values, list):
                raise TypeError(f"{label} inventory должен быть JSON-массивом.")
            seen: set[str] = set()
            for value in values:
                _, path_key = _absolute_path(value, label)
                if path_key in seen:
                    raise ValueError(f"{label} inventory содержит повторяющийся путь.")
                seen.add(path_key)
        if not isinstance(self.warp_interfaces, list):
            raise TypeError("warp_interfaces inventory должен быть JSON-массивом.")
        if len(set(self.warp_interfaces)) != len(self.warp_interfaces):
            raise ValueError("warp_interfaces inventory содержит повторяющееся имя.")
        for value in self.warp_interfaces:
            _nonempty_text(value, "warp_interfaces")
            if len(value) > 15 or value in {".", ".."} or "/" in value or "\\" in value:
                raise ValueError("Некорректное имя WARP-интерфейса inventory.")
        if not isinstance(self.features, dict) or any(
            not isinstance(key, str)
            or not key
            or any(
                unicodedata.category(character) in {"Cc", "Cf", "Cs"}
                for character in key
            )
            or not isinstance(value, bool)
            for key, value in self.features.items()
        ):
            raise TypeError(
                "features inventory должен содержать только boolean-значения."
            )
        if self.adopted_at is not None:
            _nonempty_text(self.adopted_at, "adopted_at")


def _nonempty_text(value: object, label: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in value
        )
    ):
        raise TypeError(
            f"{label} inventory должен быть непустой строкой без control-символов."
        )


def _safe_name(value: object, label: str) -> None:
    _nonempty_text(value, label)
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value
    ):
        raise ValueError(f"Некорректное имя {label} inventory.")


def _absolute_path(value: object, label: str) -> tuple[PurePath, str]:
    _nonempty_text(value, label)
    if not isinstance(value, str):
        raise TypeError(f"{label} inventory должен быть строкой.")
    if value.startswith("/"):
        if value.startswith("//"):
            raise ValueError(
                f"{label} inventory должен быть однозначным абсолютным путём."
            )
        path: PurePath = PurePosixPath(value)
        raw_parts = value.split("/")
        key = "posix:" + str(path)
    else:
        windows_path = PureWindowsPath(value)
        if not windows_path.is_absolute():
            raise ValueError(f"{label} inventory должен быть абсолютным путём.")
        path = windows_path
        raw_parts = re.split(r"[\\/]", value)
        key = "windows:" + str(path).casefold()
    if any(part in {".", ".."} for part in raw_parts):
        raise ValueError(f"{label} inventory содержит path traversal.")
    return path, key


def _is_path_root(path: PurePath) -> bool:
    return path == type(path)(path.anchor)


def _require_child_path(path: PurePath, parent: PurePath, label: str) -> None:
    if type(path) is not type(parent) or path == parent:
        raise ValueError(f"{label} inventory находится вне install_dir.")
    try:
        path.relative_to(parent)
    except ValueError as error:
        raise ValueError(f"{label} inventory находится вне install_dir.") from error
