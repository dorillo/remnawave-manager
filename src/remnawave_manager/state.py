from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .models import Inventory
from .paths import RuntimePaths
from .runner import atomic_write_json

_MAX_STATE_FILE_SIZE = 16 * 1024 * 1024


def _validate_private_target(path: Path) -> None:
    if path.is_symlink():
        raise ValidationError(f"State-файл {path} является символьной ссылкой.")
    if not path.exists():
        return
    try:
        info = path.lstat()
    except OSError as error:
        raise ValidationError(
            f"Не удалось проверить state-файл {path}: {error}"
        ) from error
    if not stat.S_ISREG(info.st_mode):
        raise ValidationError(f"State-путь {path} не является обычным файлом.")
    if info.st_nlink != 1:
        raise ValidationError(f"State-файл {path} является hardlink.")
    if os.name == "posix" and info.st_uid != os.geteuid():
        raise ValidationError(f"State-файл {path} принадлежит другому пользователю.")


def _read_private_json(
    path: Path,
    *,
    label: str,
    max_size: int = _MAX_STATE_FILE_SIZE,
    required_mode: int | None = None,
) -> Any:
    if (
        isinstance(max_size, bool)
        or not isinstance(max_size, int)
        or max_size < 1
        or required_mode is not None
        and (
            isinstance(required_mode, bool)
            or not isinstance(required_mode, int)
            or not 0 <= required_mode <= 0o777
        )
    ):
        raise ValidationError(f"Некорректные ограничения чтения {label}.")
    _validate_private_target(path)
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValidationError(f"Не удалось безопасно прочитать {label}.") from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValidationError(
                f"{label.capitalize()} не является обычным файлом без hardlink."
            )
        if os.name == "posix":
            if info.st_uid != os.geteuid():
                raise ValidationError(
                    f"{label.capitalize()} принадлежит другому пользователю."
                )
            if stat.S_IMODE(info.st_mode) & 0o077:
                raise ValidationError(
                    f"{label.capitalize()} доступен другим пользователям."
                )
        if (
            os.name == "posix"
            and required_mode is not None
            and stat.S_IMODE(info.st_mode) != required_mode
        ):
            raise ValidationError(
                f"{label.capitalize()} имеет права {stat.S_IMODE(info.st_mode):o}, "
                f"ожидается {required_mode:o}."
            )
        if info.st_size > max_size:
            raise ValidationError(f"{label.capitalize()} превышает допустимый размер.")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            payload = stream.read(max_size + 1)
        if len(payload) > max_size:
            raise ValidationError(f"{label.capitalize()} превышает допустимый размер.")
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValidationError(f"{label.capitalize()} повреждён.") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class StateStore:
    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths

    def initialize(self) -> None:
        for path, mode in (
            (self.paths.etc, 0o700),
            (self.paths.state, 0o700),
            (self.paths.backups, 0o700),
            (self.paths.logs, 0o700),
        ):
            if path.is_symlink() or (path.exists() and not path.is_dir()):
                raise ValidationError(
                    f"Служебный путь менеджера имеет небезопасный тип: {path}"
                )
            path.mkdir(parents=True, exist_ok=True, mode=mode)
            try:
                info = path.lstat()
                if os.name == "posix" and info.st_uid != os.geteuid():
                    raise ValidationError(
                        f"Служебный каталог менеджера принадлежит другому пользователю: {path}"
                    )
                if stat.S_IMODE(info.st_mode) != mode:
                    path.chmod(mode)
            except OSError as error:
                raise ValidationError(
                    f"Не удалось ограничить права служебного каталога {path}: {error}"
                ) from error

    def save_inventory(self, inventory: Inventory) -> None:
        try:
            inventory.validate()
        except (TypeError, ValueError) as error:
            raise ValidationError(
                "Нельзя сохранить некорректную инвентаризацию."
            ) from error
        self.initialize()
        _validate_private_target(self.paths.inventory)
        atomic_write_json(self.paths.inventory, inventory.to_dict(), mode=0o600)

    def load_inventory(self) -> Inventory:
        if self.paths.inventory.is_symlink() or not self.paths.inventory.is_file():
            raise ValidationError(
                "Установка ещё не принята под управление. Выполните rwm adopt."
            )
        try:
            data = _read_private_json(self.paths.inventory, label="файл инвентаризации")
            inventory = Inventory.from_dict(data)
        except (OSError, ValueError, TypeError) as error:
            raise ValidationError("Файл инвентаризации повреждён.") from error
        if inventory.schema_version != 1:
            raise ValidationError("Версия инвентаризации не поддерживается.")
        return inventory

    def load_settings(self) -> dict[str, Any]:
        if self.paths.settings.is_symlink():
            raise ValidationError(
                "Файл настроек менеджера является символьной ссылкой."
            )
        if not self.paths.settings.exists():
            return {"registry": "docker-hub", "backup_retention": 10}
        try:
            data = _read_private_json(
                self.paths.settings, label="файл настроек менеджера"
            )
            if not isinstance(data, dict):
                raise ValidationError("Файл настроек менеджера повреждён.")
            return data
        except (OSError, json.JSONDecodeError) as error:
            raise ValidationError("Файл настроек менеджера повреждён.") from error

    def save_settings(self, settings: dict[str, Any]) -> None:
        self.initialize()
        _validate_private_target(self.paths.settings)
        atomic_write_json(self.paths.settings, settings, mode=0o600)

    def load_secrets(self) -> dict[str, Any]:
        if self.paths.secrets.is_symlink():
            raise ValidationError(
                "Файл секретов менеджера является символьной ссылкой."
            )
        if not self.paths.secrets.exists():
            return {}
        data = _read_private_json(
            self.paths.secrets,
            label="файл секретов менеджера",
        )
        if not isinstance(data, dict):
            raise ValidationError("Файл секретов менеджера повреждён.")
        return data
