from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import TransactionError, ValidationError
from .runner import (
    Runner,
    atomic_write_text,
    read_stable_regular_file,
)

_KEY = re.compile(r"^(?P<indent> +)(?P<key>[A-Za-z0-9_.-]+):(?=[ \t\r\n]|$)")
_IMAGE = re.compile(
    r"^(?P<prefix> +image:[ \t]*)(?P<anchor>&[A-Za-z0-9_.-]+[ \t]+)?(?P<value>[^#\r\n]*?)(?P<comment>[ \t]+#[^\r\n]*)?(?P<newline>\r\n|\n)?$"
)
_VOLUME_TARGET = re.compile(
    r"^(?P<prefix> +target:[ \t]*)(?P<value>[^#\r\n]*?)(?P<comment>[ \t]+#[^\r\n]*)?(?P<newline>\r\n|\n)?$"
)
_LIST_SCALAR = re.compile(
    r"^(?P<prefix> +-[ \t]+)(?P<value>[^#\r\n]*?)(?P<comment>[ \t]+#[^\r\n]*)?(?P<newline>\r\n|\n)?$"
)
_MAPPING_SCALAR = re.compile(
    r"^(?P<prefix> +)(?P<key>[A-Za-z_][A-Za-z0-9_]*):[ \t]*(?P<value>[^#\r\n]*?)(?P<comment>[ \t]+#[^\r\n]*)?(?P<newline>\r\n|\n)?$"
)
_MAX_COMPOSE_DOCUMENT_SIZE = 16 * 1024 * 1024


@dataclass(slots=True)
class ServiceBlock:
    name: str
    start: int
    end: int
    indent: int


class ComposeDocument:
    """Точечный редактор Compose без переформатирования пользовательского файла."""

    def __init__(
        self,
        text: str,
        *,
        source_bytes: bytes | None = None,
        source_mode: int | None = None,
    ) -> None:
        if "\t" in text:
            raise ValidationError("Compose с tab-отступами не поддерживается безопасным редактором.")
        self.lines = text.splitlines(keepends=True)
        self._source_bytes = source_bytes
        self._source_mode = source_mode

    @classmethod
    def load(cls, path: Path) -> ComposeDocument:
        try:
            snapshot = read_stable_regular_file(
                path,
                max_size=_MAX_COMPOSE_DOCUMENT_SIZE,
                label="Compose-файл",
            )
            if os.name == "posix" and (
                snapshot.uid != os.geteuid() or snapshot.mode & 0o022
            ):
                raise ValidationError(
                    f"Compose-файл {path} имеет небезопасного владельца или права."
                )
            return cls(
                snapshot.data.decode("utf-8"),
                source_bytes=snapshot.data,
                source_mode=snapshot.mode,
            )
        except UnicodeError as error:
            raise ValidationError(f"Не удалось безопасно прочитать Compose-файл {path}.") from error

    def service_blocks(self) -> dict[str, ServiceBlock]:
        services_index: int | None = None
        services_indent = 0
        services_matches: list[tuple[int, int]] = []
        for index, line in enumerate(self.lines):
            stripped = line.strip()
            if stripped == "services:":
                services_matches.append(
                    (index, len(line) - len(line.lstrip(" ")))
                )
        if not services_matches:
            raise ValidationError("В compose не найден раздел services.")
        if len(services_matches) != 1 or services_matches[0][1] != 0:
            raise ValidationError(
                "Раздел services в compose неоднозначен или находится не на верхнем уровне."
            )
        services_index, services_indent = services_matches[0]

        candidates: list[tuple[str, int, int]] = []
        service_indent: int | None = None
        for index in range(services_index + 1, len(self.lines)):
            line = self.lines[index]
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            indent = len(line) - len(line.lstrip(" "))
            if indent <= services_indent:
                break
            match = _KEY.match(line)
            if not match:
                continue
            if service_indent is None:
                service_indent = indent
            if indent == service_indent:
                candidates.append((match.group("key"), index, indent))
        if not candidates:
            raise ValidationError("В compose не найдены сервисы.")

        blocks: dict[str, ServiceBlock] = {}
        for position, (name, start, indent) in enumerate(candidates):
            if name in blocks:
                raise ValidationError(f"В compose повторяется сервис {name}.")
            end = candidates[position + 1][1] if position + 1 < len(candidates) else len(self.lines)
            for index in range(start + 1, end):
                line = self.lines[index]
                if not line.strip() or line.lstrip().startswith("#"):
                    continue
                current_indent = len(line) - len(line.lstrip(" "))
                if current_indent <= services_indent:
                    end = index
                    break
            blocks[name] = ServiceBlock(name, start, end, indent)
        return blocks

    def image(self, service: str) -> str | None:
        block = self._block(service)
        match = self._direct_image(block)
        return match[1].group("value").strip().strip('"\'') if match else None

    def set_image(self, service: str, image: str) -> str | None:
        if not re.fullmatch(r"[A-Za-z0-9./:@_-]+", image):
            raise ValidationError(f"Некорректное имя Docker image: {image}")
        block = self._block(service)
        found = self._direct_image(block)
        if found:
            index, match = found
            previous = match.group("value").strip().strip('"\'')
            self.lines[index] = (
                f"{match.group('prefix')}{match.group('anchor') or ''}{image}"
                f"{match.group('comment') or ''}{match.group('newline') or ''}"
            )
            return previous
        newline = "\r\n" if any(line.endswith("\r\n") for line in self.lines) else "\n"
        self.lines.insert(block.start + 1, " " * (block.indent + 2) + f"image: {image}{newline}")
        return None

    def replace_volume_target(self, service: str, previous: str, current: str) -> bool:
        if not all(re.fullmatch(r"/[A-Za-z0-9._/-]+", value) for value in (previous, current)):
            raise ValidationError("Некорректный путь Docker volume.")
        block = self._block(service)
        key_lines: list[tuple[int, re.Match[str]]] = []
        for index in range(block.start + 1, block.end):
            match = _KEY.match(self.lines[index])
            if match:
                key_lines.append((index, match))
        if not key_lines:
            return False
        direct_indent = min(len(match.group("indent")) for _, match in key_lines)
        volume_keys = [
            (index, match)
            for index, match in key_lines
            if len(match.group("indent")) == direct_indent and match.group("key") == "volumes"
        ]
        if not volume_keys:
            return False
        if len(volume_keys) != 1:
            raise ValidationError(f"Не удалось однозначно определить volumes сервиса {service}.")

        start = volume_keys[0][0] + 1
        end = block.end
        for index in range(start, block.end):
            line = self.lines[index]
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if len(line) - len(line.lstrip(" ")) <= direct_indent:
                end = index
                break

        replacements: list[tuple[int, int, int]] = []
        for index in range(start, end):
            line = self.lines[index]
            target_match = _VOLUME_TARGET.match(line)
            if target_match and self._unquoted(target_match.group("value")) == previous:
                value_start, value_end = target_match.span("value")
                path_start = line.find(previous, value_start, value_end)
                replacements.append((index, path_start, path_start + len(previous)))
                continue

            list_match = _LIST_SCALAR.match(line)
            if not list_match:
                continue
            raw_value = list_match.group("value")
            value = self._unquoted(raw_value)
            marker = ":" + previous
            marker_at = value.rfind(marker)
            if marker_at < 0:
                continue
            suffix = value[marker_at + len(marker) :]
            if suffix and not re.fullmatch(r":[A-Za-z0-9,._-]+", suffix):
                continue
            value_start, value_end = list_match.span("value")
            path_start = line.rfind(previous, value_start, value_end)
            replacements.append((index, path_start, path_start + len(previous)))

        if not replacements:
            return False
        if len(replacements) != 1:
            raise ValidationError(
                f"Не удалось однозначно определить volume target {previous} сервиса {service}."
            )
        index, start_at, end_at = replacements[0]
        self.lines[index] = self.lines[index][:start_at] + current + self.lines[index][end_at:]
        return True

    def set_service_environment(self, service: str, key: str, value: str) -> None:
        """Replace one direct environment value without reformatting the Compose file."""
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValidationError(f"Некорректное имя переменной окружения: {key}")
        if not isinstance(value, str) or any(
            ord(character) < 32 or ord(character) == 127 for character in value
        ):
            raise ValidationError(f"Некорректное значение переменной окружения {key}.")

        block = self._block(service)
        direct_keys: list[tuple[int, re.Match[str]]] = []
        for index in range(block.start + 1, block.end):
            match = _KEY.match(self.lines[index])
            if match and len(match.group("indent")) == block.indent + 2:
                direct_keys.append((index, match))
        environment_keys = [
            (index, match)
            for index, match in direct_keys
            if match.group("key") == "environment"
        ]
        if len(environment_keys) != 1:
            raise ValidationError(
                f"Не удалось однозначно определить environment сервиса {service} для {key}."
            )

        environment_index, environment_match = environment_keys[0]
        environment_indent = len(environment_match.group("indent"))
        end = block.end
        for index in range(environment_index + 1, block.end):
            line = self.lines[index]
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if len(line) - len(line.lstrip(" ")) <= environment_indent:
                end = index
                break

        entries = [
            index
            for index in range(environment_index + 1, end)
            if self.lines[index].strip() and not self.lines[index].lstrip().startswith("#")
        ]
        if not entries:
            raise ValidationError(
                f"Environment сервиса {service} пуст или имеет неподдерживаемый формат."
            )

        first = self.lines[entries[0]]
        rendered_value = json.dumps(value)
        if first.lstrip().startswith("-"):
            matches: list[tuple[int, re.Match[str]]] = []
            for index in entries:
                match = _LIST_SCALAR.match(self.lines[index])
                if not match:
                    raise ValidationError(
                        f"Environment сервиса {service} смешивает неподдерживаемые форматы."
                    )
                raw = self._unquoted(match.group("value"))
                if raw.startswith(key + "="):
                    matches.append((index, match))
            if len(matches) != 1:
                raise ValidationError(
                    f"Не удалось однозначно определить {key} в environment сервиса {service}."
                )
            index, match = matches[0]
            self.lines[index] = (
                f"{match.group('prefix')}{key}={value}{match.group('comment') or ''}"
                f"{match.group('newline') or ''}"
            )
            return

        matches = []
        for index in entries:
            match = _MAPPING_SCALAR.match(self.lines[index])
            if not match:
                raise ValidationError(
                    f"Environment сервиса {service} смешивает неподдерживаемые форматы."
                )
            if match.group("key") == key:
                matches.append((index, match))
        if len(matches) != 1:
            raise ValidationError(
                f"Не удалось однозначно определить {key} в environment сервиса {service}."
            )
        index, match = matches[0]
        self.lines[index] = (
            f"{match.group('prefix')}{key}: {rendered_value}{match.group('comment') or ''}"
            f"{match.group('newline') or ''}"
        )

    @staticmethod
    def _unquoted(value: str) -> str:
        selected = value.strip()
        if len(selected) >= 2 and selected[0] in {'"', "'"} and selected[-1] == selected[0]:
            return selected[1:-1]
        return selected

    def _block(self, service: str) -> ServiceBlock:
        try:
            return self.service_blocks()[service]
        except KeyError as error:
            raise ValidationError(f"В compose нет сервиса {service}.") from error

    def _direct_image(self, block: ServiceBlock) -> tuple[int, re.Match[str]] | None:
        key_indents: list[int] = []
        matches: list[tuple[int, re.Match[str], int]] = []
        for index in range(block.start + 1, block.end):
            line = self.lines[index]
            key_match = _KEY.match(line)
            if key_match:
                key_indents.append(len(key_match.group("indent")))
            image_match = _IMAGE.match(line)
            if image_match:
                matches.append((index, image_match, len(image_match.group("prefix")) - len("image: ")))
        if not matches:
            return None
        direct_indent = min(key_indents) if key_indents else block.indent + 2
        direct = [item for item in matches if len(self.lines[item[0]]) - len(self.lines[item[0]].lstrip(" ")) == direct_indent]
        if len(direct) != 1:
            raise ValidationError(f"Не удалось однозначно определить image сервиса {block.name}.")
        return direct[0][0], direct[0][1]

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
            max_size=_MAX_COMPOSE_DOCUMENT_SIZE,
            label="Compose-файл",
        )
        if self._source_bytes is not None and snapshot.data != self._source_bytes:
            raise ValidationError(
                f"Compose-файл {path} изменился после загрузки; перезапись отменена."
            )
        if self._source_mode is not None and snapshot.mode != self._source_mode:
            raise ValidationError(
                f"Права Compose-файла {path} изменились после загрузки; перезапись отменена."
            )
        if os.name == "posix" and (
            snapshot.uid != os.geteuid() or snapshot.mode & 0o022
        ):
            raise ValidationError(
                f"Compose-файл {path} имеет небезопасного владельца или права."
            )
        selected_mode = mode if mode is not None else snapshot.mode
        rendered = self.render()
        if before_write is not None:
            before_write()
        atomic_write_text(path, rendered, mode=selected_mode)
        self._source_bytes = rendered.encode("utf-8")
        self._source_mode = selected_mode


def compose_command(
    compose_file: Path,
    *arguments: str,
    env_file: Path | None = None,
) -> list[str]:
    command = ["docker", "compose"]
    if env_file is not None:
        command += ["--env-file", str(env_file)]
    command += ["-f", str(compose_file), *arguments]
    return command


def inspect_compose(runner: Runner, compose_file: Path, env_file: Path | None) -> dict[str, Any]:
    result = runner.run(
        compose_command(compose_file, "config", "--format", "json", env_file=env_file),
        cwd=compose_file.parent,
    )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValidationError("docker compose config вернул некорректный JSON.") from error
    if not isinstance(data, dict) or not isinstance(data.get("services"), dict):
        raise ValidationError("В нормализованном compose отсутствует services.")
    return data


def validate_rendered_compose(
    runner: Runner,
    original_path: Path,
    rendered: str,
    env_file: Path | None,
) -> None:
    descriptor, temporary = tempfile.mkstemp(
        prefix=".rwm-compose-", suffix=original_path.suffix, dir=original_path.parent
    )
    path = Path(temporary)
    primary_error: BaseException | None = None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            descriptor = -1
            stream.write(rendered)
        runner.run(
            compose_command(path, "config", "-q", env_file=env_file),
            cwd=original_path.parent,
        )
    except BaseException as error:  # noqa: BLE001 - cleanup must preserve the primary failure
        primary_error = error
    cleanup_errors: list[str] = []
    if descriptor >= 0:
        try:
            os.close(descriptor)
        except BaseException as cleanup_error:  # noqa: BLE001 - continue cleanup after interruption
            cleanup_errors.append(f"закрытие descriptor: {cleanup_error}")
    try:
        path.unlink(missing_ok=True)
        if path.exists() or path.is_symlink():
            raise OSError("временный файл остался на диске")
    except BaseException as cleanup_error:  # noqa: BLE001 - report leaked preflight file
        cleanup_errors.append(f"удаление файла: {cleanup_error}")
    if cleanup_errors:
        cleanup_detail = "; ".join(cleanup_errors)
        if primary_error is not None:
            raise TransactionError(
                "Проверка будущего Compose завершилась ошибкой, а временный файл "
                f"{path} очистить не удалось: {cleanup_detail}. Исходная ошибка: "
                f"{primary_error}"
            ) from primary_error
        raise TransactionError(
            f"Будущий Compose проверен, но временный файл {path} очистить не удалось: "
            f"{cleanup_detail}"
        )
    if primary_error is not None:
        raise primary_error
