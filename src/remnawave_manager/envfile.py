from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .errors import ValidationError
from .runner import (
    atomic_write_text,
    read_stable_regular_file,
)

_ASSIGNMENT = re.compile(
    r"^(?P<prefix>[ \t]*(?:export[ \t]+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)(?P<separator>[ \t]*=[ \t]*)(?P<value>.*?)(?P<newline>\r?\n)?$"
)
_MAX_ENV_DOCUMENT_SIZE = 16 * 1024 * 1024


@dataclass(slots=True)
class Assignment:
    index: int
    prefix: str
    key: str
    separator: str
    value: str
    newline: str


class EnvDocument:
    """Редактор dotenv, сохраняющий порядок, комментарии и правую часть значений."""

    def __init__(
        self,
        text: str,
        *,
        source_bytes: bytes | None = None,
        source_mode: int | None = None,
    ) -> None:
        self.lines = text.splitlines(keepends=True)
        if text and not self.lines:
            self.lines = [text]
        self._source_bytes = source_bytes
        self._source_mode = source_mode

    @classmethod
    def load(cls, path: Path) -> EnvDocument:
        try:
            snapshot = read_stable_regular_file(
                path,
                max_size=_MAX_ENV_DOCUMENT_SIZE,
                label="Env-файл",
            )
            if os.name == "posix" and (
                snapshot.uid != os.geteuid() or snapshot.mode & 0o022
            ):
                raise ValidationError(
                    f"Env-файл {path} имеет небезопасного владельца или права."
                )
            return cls(
                snapshot.data.decode("utf-8"),
                source_bytes=snapshot.data,
                source_mode=snapshot.mode,
            )
        except UnicodeError as error:
            raise ValidationError(f"Не удалось безопасно прочитать env-файл {path}.") from error

    def _assignments(self, key: str | None = None) -> list[Assignment]:
        found: list[Assignment] = []
        for index, line in enumerate(self.lines):
            match = _ASSIGNMENT.match(line)
            if not match or line.lstrip().startswith("#"):
                continue
            current = match.group("key")
            if key is None or current == key:
                found.append(
                    Assignment(
                        index=index,
                        prefix=match.group("prefix"),
                        key=current,
                        separator=match.group("separator"),
                        value=match.group("value"),
                        newline=match.group("newline") or "",
                    )
                )
        return found

    def has(self, key: str) -> bool:
        return bool(self._assignments(key))

    def raw_value(self, key: str) -> str | None:
        assignments = self._assignments(key)
        return assignments[-1].value if assignments else None

    def effective_value(self, key: str) -> str | None:
        raw = self.raw_value(key)
        if raw is None:
            return None
        value = raw.strip()
        if value[:1] in {'"', "'"}:
            quote = value[0]
            closing = value.find(quote, 1)
            if closing >= 0 and (
                not value[closing + 1 :]
                or value[closing + 1 :].lstrip().startswith("#")
            ):
                value = value[1:closing]
        else:
            comment = re.search(r"[ \t]+#", value)
            if comment is not None:
                value = value[: comment.start()].rstrip()
        return value

    def remove(self, key: str) -> bool:
        indices = {item.index for item in self._assignments(key)}
        if not indices:
            return False
        self.lines = [line for index, line in enumerate(self.lines) if index not in indices]
        return True

    def set(self, key: str, value: str, *, preserve_raw: bool = False) -> None:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValidationError(f"Некорректное имя переменной: {key}")
        assignments = self._assignments(key)
        rendered = value if preserve_raw else value.replace("\r", "").replace("\n", "")
        if assignments:
            effective = assignments[-1]
            self.lines[effective.index] = (
                f"{effective.prefix}{key}{effective.separator}{rendered}{effective.newline}"
            )
            for item in reversed(assignments[:-1]):
                del self.lines[item.index]
            return
        if self.lines and not self.lines[-1].endswith(("\n", "\r")):
            self.lines[-1] += "\n"
        self.lines.append(f"{key}={rendered}\n")

    def migrate_panel_v3(self) -> dict[str, object]:
        """Применяет необратимый env-контракт Panel 3, не изменяя APP_SECRET."""
        app_secret = self.raw_value("APP_SECRET")
        jwt_secret = self.raw_value("JWT_AUTH_SECRET")
        if app_secret is None:
            if jwt_secret is None or not self.effective_value("JWT_AUTH_SECRET"):
                raise ValidationError(
                    "В .env отсутствуют APP_SECRET и непустой JWT_AUTH_SECRET. "
                    "Обновление остановлено, чтобы не сломать пароль администратора."
                )
            assignments = self._assignments("JWT_AUTH_SECRET")
            effective = assignments[-1]
            self.lines[effective.index] = (
                f"{effective.prefix}APP_SECRET{effective.separator}{effective.value}{effective.newline}"
            )
            for item in reversed(assignments[:-1]):
                del self.lines[item.index]
            source = "JWT_AUTH_SECRET"
        else:
            if not self.effective_value("APP_SECRET"):
                raise ValidationError("APP_SECRET задан, но пуст. Обновление остановлено.")
            self.set("APP_SECRET", app_secret, preserve_raw=True)
            self.remove("JWT_AUTH_SECRET")
            source = "APP_SECRET"

        removed: list[str] = []
        for key in (
            "JWT_API_TOKENS_SECRET",
            "SWAGGER_PATH",
            "SCALAR_PATH",
            "IS_DOCS_ENABLED",
        ):
            if self.remove(key):
                removed.append(key)
        return {"secret_source": source, "removed": removed}

    def validate_panel_v3(self) -> None:
        required = (
            "DATABASE_URL",
            "APP_SECRET",
            "FRONT_END_DOMAIN",
            "METRICS_USER",
            "METRICS_PASS",
            "SUB_PUBLIC_DOMAIN",
        )
        missing = [key for key in required if not self.effective_value(key)]
        if missing:
            raise ValidationError(
                "В .env отсутствуют обязательные переменные Panel 3: "
                + ", ".join(missing)
                + "."
            )
        if self.effective_value("APP_SECRET") == "change_me":
            raise ValidationError("APP_SECRET не может иметь небезопасное значение change_me.")

        redis_socket = self.effective_value("REDIS_SOCKET")
        redis_host = self.effective_value("REDIS_HOST")
        redis_port = self.effective_value("REDIS_PORT")
        if not redis_socket and not (redis_host and redis_port):
            raise ValidationError(
                "Panel 3 требует REDIS_SOCKET либо одновременно REDIS_HOST и REDIS_PORT."
            )
        if redis_socket and (redis_host or redis_port):
            raise ValidationError(
                "REDIS_SOCKET нельзя задавать одновременно с REDIS_HOST или REDIS_PORT."
            )
        if redis_port:
            try:
                port = int(redis_port)
            except ValueError as error:
                raise ValidationError("REDIS_PORT должен быть целым числом.") from error
            if not 1 <= port <= 65535:
                raise ValidationError("REDIS_PORT должен быть в диапазоне 1-65535.")

        lifetime = self.effective_value("JWT_AUTH_LIFETIME")
        if lifetime:
            try:
                lifetime_hours = int(lifetime)
            except ValueError as error:
                raise ValidationError("JWT_AUTH_LIFETIME должен быть целым числом.") from error
            if not 12 <= lifetime_hours <= 168:
                raise ValidationError("JWT_AUTH_LIFETIME должен быть в диапазоне 12-168 часов.")

        boolean_keys = (
            "IS_TELEGRAM_NOTIFICATIONS_ENABLED",
            "WEBHOOK_ENABLED",
            "IS_HTTP_LOGGING_ENABLED",
            "ENABLE_DEBUG_LOGS",
            "SERVICE_CLEAN_USAGE_HISTORY",
            "SERVICE_DISABLE_USER_USAGE_RECORDS",
            "SERVICE_DISABLE_SRH_RECORDS",
            "EXPORT_TO_STREAM_ENABLED",
            "BANDWIDTH_USAGE_NOTIFICATIONS_ENABLED",
            "NOT_CONNECTED_USERS_NOTIFICATIONS_ENABLED",
            "EXPIRATION_NOTIFICATIONS_ENABLED",
        )
        invalid_booleans = [
            key
            for key in boolean_keys
            if self.effective_value(key) not in {None, "", "true", "false"}
        ]
        if invalid_booleans:
            raise ValidationError(
                "Boolean-переменные Panel 3 должны содержать true или false: "
                + ", ".join(invalid_booleans)
                + "."
            )

        branch = self.effective_value("REMNAWAVE_BRANCH")
        if branch not in {None, "", "dev", "main"}:
            raise ValidationError("REMNAWAVE_BRANCH должен иметь значение dev или main.")

    def render(self) -> str:
        return "".join(self.lines)

    def save(
        self,
        path: Path,
        *,
        mode: int | None = None,
        before_write: Callable[[], None] | None = None,
    ) -> None:
        snapshot = read_stable_regular_file(
            path,
            max_size=_MAX_ENV_DOCUMENT_SIZE,
            label="Env-файл",
        )
        if self._source_bytes is not None and snapshot.data != self._source_bytes:
            raise ValidationError(
                f"Env-файл {path} изменился после загрузки; перезапись отменена."
            )
        if self._source_mode is not None and snapshot.mode != self._source_mode:
            raise ValidationError(
                f"Права env-файла {path} изменились после загрузки; перезапись отменена."
            )
        if os.name == "posix" and (
            snapshot.uid != os.geteuid() or snapshot.mode & 0o022
        ):
            raise ValidationError(
                f"Env-файл {path} имеет небезопасного владельца или права."
            )
        rendered = self.render()
        selected_mode = snapshot.mode if mode is None else mode
        if before_write is not None:
            before_write()
        atomic_write_text(path, rendered, mode=selected_mode)
        self._source_bytes = rendered.encode("utf-8")
        self._source_mode = selected_mode
