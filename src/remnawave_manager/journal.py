from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from .errors import ValidationError
from .runner import atomic_write_json
from .state import StateStore, _read_private_json, utc_now

_MAX_JOURNAL_SIZE = 64 * 1024
_MAX_IDENTIFIER_LENGTH = 128
_MAX_PATH_LENGTH = 4096
_MAX_SERVICES = 128
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def _validated_identifier(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_IDENTIFIER_LENGTH
        or _IDENTIFIER_PATTERN.fullmatch(value) is None
    ):
        raise ValidationError(f"{label} имеет некорректное значение.")
    return value


def _validated_absolute_path(value: object, *, label: str) -> str:
    if not isinstance(value, Path):
        raise ValidationError(f"{label} должен быть абсолютным путём.")
    rendered = str(value)
    if (
        not value.is_absolute()
        or not rendered
        or len(rendered.encode("utf-8")) > _MAX_PATH_LENGTH
        or not rendered.isprintable()
        or ".." in value.parts
    ):
        raise ValidationError(f"{label} должен быть безопасным абсолютным путём.")
    return rendered


def _validated_services(value: object, *, label: str) -> set[str]:
    if not isinstance(value, (set, frozenset)) or len(value) > _MAX_SERVICES:
        raise ValidationError(f"{label} содержит некорректные имена.")
    services = set(value)
    if any(
        not isinstance(service, str)
        or _IDENTIFIER_PATTERN.fullmatch(service) is None
        for service in services
    ):
        raise ValidationError(f"{label} содержит некорректные имена.")
    return services


class TransactionJournal:
    def __init__(
        self,
        store: StateStore,
        operation: str,
        backup: Path | None = None,
    ) -> None:
        operation = _validated_identifier(operation, label="Operation journal")
        backup_value = (
            _validated_absolute_path(backup, label="Путь backup")
            if backup is not None
            else None
        )
        self.store = store
        self.path = store.paths.state / "active-transaction.json"
        self.transaction_id = uuid.uuid4().hex
        self.data: dict[str, Any] = {
            "transaction_id": self.transaction_id,
            "operation": operation,
            "started_at": utc_now(),
            "phase": "started",
        }
        if backup_value is not None:
            self.data["backup"] = backup_value
        store.initialize()
        payload = self._serialized_payload()
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except FileExistsError as error:
            raise ValidationError(
                f"Обнаружена незавершённая транзакция: {self.path}. "
                "Проверьте journal и выполните recovery перед новой операцией."
            ) from error
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
                if hasattr(os, "fchmod"):
                    os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if not hasattr(os, "fchmod"):
                os.chmod(self.path, 0o600)
            self._sync_directory()
        except BaseException:
            self.path.unlink(missing_ok=True)
            raise

    @classmethod
    def ensure_available(cls, store: StateStore) -> None:
        path = store.paths.state / "active-transaction.json"
        if path.exists() or path.is_symlink():
            raise ValidationError(
                f"Обнаружена незавершённая транзакция: {path}. "
                "Проверьте journal и выполните recovery перед новой операцией."
            )

    def _assert_owned(self) -> None:
        try:
            current = _read_private_json(
                self.path,
                label="journal текущей транзакции",
                max_size=_MAX_JOURNAL_SIZE,
                required_mode=0o600,
            )
        except (OSError, ValidationError) as error:
            raise ValidationError(
                f"Journal текущей транзакции повреждён или подменён: {self.path}."
            ) from error
        if not isinstance(current, dict):
            raise ValidationError(
                f"Journal текущей транзакции повреждён или подменён: {self.path}."
            )
        if current.get("transaction_id") != self.transaction_id:
            raise ValidationError(
                f"Journal текущей транзакции был заменён: {self.path}."
            )

    def _sync_directory(self) -> None:
        if os.name != "posix":
            return
        descriptor = os.open(self.path.parent, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _serialized_payload(self) -> str:
        payload = (
            json.dumps(self.data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        if len(payload.encode("utf-8")) > _MAX_JOURNAL_SIZE:
            raise ValidationError("Данные journal превышают допустимый размер.")
        return payload

    def _write(self) -> None:
        self._serialized_payload()
        atomic_write_json(self.path, self.data, mode=0o600)

    def phase(self, value: str) -> None:
        value = _validated_identifier(value, label="Phase journal")
        self._assert_owned()
        self.data["phase"] = value
        self.data["updated_at"] = utc_now()
        self._write()

    def set_backup(self, backup: Path) -> None:
        backup_value = _validated_absolute_path(backup, label="Путь backup")
        self._assert_owned()
        if "backup" in self.data:
            raise ValidationError("Backup уже привязан к текущей транзакции.")
        self.data["backup"] = backup_value
        self.data["updated_at"] = utc_now()
        self._write()

    def set_running_services(self, services: set[str]) -> None:
        self._assert_owned()
        if "running_services" in self.data:
            raise ValidationError("Снимок запущенных сервисов уже записан в journal.")
        validated = _validated_services(
            services,
            label="Снимок запущенных сервисов",
        )
        self.data["running_services"] = sorted(validated)
        self.data["updated_at"] = utc_now()
        self._write()

    def set_archive_metadata(
        self,
        *,
        install_directory: tuple[Path, Path],
        inventory: tuple[Path, Path],
        secrets: tuple[Path, Path] | None,
        created_services: set[str] | frozenset[str],
        running_services: set[str] | frozenset[str],
    ) -> None:
        self._assert_owned()
        metadata_keys = {"archive_targets", "created_services", "running_services"}
        if metadata_keys & self.data.keys():
            raise ValidationError("Метаданные архивирования уже записаны в journal.")

        targets: dict[str, dict[str, str]] = {}
        raw_targets = {
            "install_directory": install_directory,
            "inventory": inventory,
        }
        if secrets is not None:
            raw_targets["secrets"] = secrets
        all_paths: list[str] = []
        for name, pair in raw_targets.items():
            if not isinstance(pair, tuple) or len(pair) != 2:
                raise ValidationError(
                    f"Пути архивирования {name} имеют некорректный тип."
                )
            original = _validated_absolute_path(
                pair[0],
                label=f"Исходный путь {name}",
            )
            archived = _validated_absolute_path(
                pair[1],
                label=f"Архивный путь {name}",
            )
            if original == archived:
                raise ValidationError(
                    f"Исходный и архивный пути {name} должны различаться."
                )
            targets[name] = {"original": original, "archive": archived}
            all_paths.extend((original, archived))
        if len(all_paths) != len(set(all_paths)):
            raise ValidationError("Пути архивирования должны быть уникальными.")

        created = _validated_services(
            created_services,
            label="Снимок созданных сервисов",
        )
        running = _validated_services(
            running_services,
            label="Снимок запущенных сервисов",
        )
        if not running <= created:
            raise ValidationError(
                "Запущенные сервисы должны быть подмножеством созданных сервисов."
            )

        self.data["archive_targets"] = targets
        self.data["created_services"] = sorted(created)
        self.data["running_services"] = sorted(running)
        self.data["updated_at"] = utc_now()
        self._write()

    def complete(self) -> None:
        self._assert_owned()
        self.path.unlink(missing_ok=True)
        self._sync_directory()
