from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from .errors import CommandError, ValidationError

_MAX_ATOMIC_COPY_SIZE = 128 * 1024 * 1024
_MAX_SANITIZED_TEXT = 16 * 1024
_MAX_COMPOSE_INPUT_SIZE = 16 * 1024 * 1024
_LETSENCRYPT_ROOT = Path("/etc/letsencrypt")
_CERTBOT_LIVE_FILENAMES = {"cert.pem", "chain.pem", "fullchain.pem", "privkey.pem"}
_FORBIDDEN_COMPOSE_REFERENCE_KEYS = {
    "build",
    "configs",
    "credential_spec",
    "develop",
    "devices",
    "driver_opts",
    "extends",
    "include",
    "label_file",
    "models",
    "provider",
    "secrets",
    "volumes_from",
}
_COMPOSE_SUBCOMMANDS = {
    "attach",
    "build",
    "config",
    "cp",
    "create",
    "down",
    "events",
    "exec",
    "images",
    "kill",
    "logs",
    "ls",
    "pause",
    "port",
    "ps",
    "publish",
    "pull",
    "push",
    "restart",
    "rm",
    "run",
    "scale",
    "start",
    "stats",
    "stop",
    "top",
    "unpause",
    "up",
    "version",
    "wait",
    "watch",
}
_COMPOSE_GLOBAL_FLAGS = {
    "--all-resources",
    "--compatibility",
    "--dry-run",
    "--verbose",
}
_COMPOSE_GLOBAL_VALUE_OPTIONS = {
    "--ansi",
    "--env-file",
    "--file",
    "--parallel",
    "--profile",
    "--progress",
    "--project-name",
    "-f",
    "-p",
}
_COMPOSE_KEY = re.compile(
    r"^(?P<indent> *)(?P<key>[A-Za-z0-9_.-]+|\"[A-Za-z0-9_.-]+\"|'[A-Za-z0-9_.-]+')"
    r"[ \t]*:[ \t]*(?P<value>.*)$"
)
_COMPOSE_LIST_ITEM = re.compile(r"^(?P<indent> *)-[ \t]+(?P<value>.*)$")
_COMPOSE_NAMED_VOLUME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_SAFE_POSIX_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
_BLOCKED_ENVIRONMENT_KEYS = {
    "BASH_ENV",
    "CDPATH",
    "CURL_HOME",
    "ENV",
    "GCONV_PATH",
    "GLOBIGNORE",
    "IFS",
    "LOCPATH",
    "NODE_OPTIONS",
    "NLSPATH",
    "OPENSSL_CONF",
    "OPENSSL_MODULES",
    "PERL5OPT",
    "RUBYOPT",
    "SHELLOPTS",
    "SSLKEYLOGFILE",
    "WGETRC",
}
_BLOCKED_ENVIRONMENT_PREFIXES = (
    "COMPOSE_",
    "DYLD_",
    "LD_",
    "PYTHON",
    "RWM_",
)
_BLOCKED_INHERITED_ENVIRONMENT_KEYS = {
    "REMNAWAVE_API_TOKEN",
    "WGCF_LICENSE_KEY",
}
_EXPLICIT_ONLY_ENVIRONMENT_KEYS = {
    "RWM_CERTBOT_MANAGER_LOCK_HELD",
}


@dataclass(slots=True)
class Result:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class RegularFileSnapshot:
    data: bytes
    mode: int
    uid: int
    gid: int


def _display_command(args: Sequence[str]) -> str:
    hidden_after = {"--password", "--token", "--secret"}
    output: list[str] = []
    hide_next = False
    for value in args:
        if hide_next:
            output.append("<скрыто>")
            hide_next = False
        elif any(value.startswith(flag + "=") for flag in hidden_after):
            output.append(value.split("=", 1)[0] + "=<скрыто>")
        else:
            output.append(value)
            hide_next = value in hidden_after
    return _sanitized_detail(" ".join(output), limit=4000)


def sanitize_external_text(value: str, *, limit: int = 4000) -> str:
    if (
        not isinstance(value, str)
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= _MAX_SANITIZED_TEXT
    ):
        raise ValidationError("Некорректные параметры санитизации текста.")
    selected = value[-limit:]
    return "".join(
        character
        if character in {"\n", "\t"}
        or unicodedata.category(character) not in {"Cc", "Cf", "Cs"}
        else " "
        for character in selected
    ).strip()


def _sanitized_detail(value: str, *, limit: int = 4000) -> str:
    return sanitize_external_text(value, limit=limit)


def _validated_command(args: Sequence[str]) -> tuple[str, ...]:
    if not args or any(not isinstance(item, str) or "\x00" in item for item in args):
        raise ValidationError(
            "Команда должна быть непустым списком строк без NUL-байтов."
        )
    return tuple(args)


def _local_docker_command(
    command: tuple[str, ...],
    *,
    cwd: Path | str | None = None,
) -> tuple[str, ...]:
    executable = command[0].replace("\\", "/").rsplit("/", 1)[-1]
    if executable != "docker":
        return command
    if any(
        value in {"--host", "--context"}
        or value.startswith(("-H", "-c", "--host=", "--context="))
        for value in command[1:]
    ):
        raise ValidationError(
            "Переопределение Docker host/context внутри команды запрещено."
        )
    if len(command) >= 2 and command[1] == "compose":
        _validate_compose_inputs(command, cwd=cwd)
    return (command[0], "--host=unix:///run/docker.sock", *command[1:])


def _parse_compose_global_options(
    command: tuple[str, ...],
) -> tuple[int | None, list[str], list[str]]:
    compose_files: list[str] = []
    env_files: list[str] = []
    index = 2
    while index < len(command):
        argument = command[index]
        if argument in _COMPOSE_SUBCOMMANDS:
            return index, compose_files, env_files
        if argument in _COMPOSE_GLOBAL_VALUE_OPTIONS:
            index += 1
            if index >= len(command):
                raise ValidationError(f"Docker Compose требует значение после {argument}.")
            value = command[index]
            if argument == "--env-file":
                env_files.append(value)
            elif argument in {"-f", "--file"}:
                compose_files.append(value)
        elif argument == "--project-directory" or argument.startswith(
            "--project-directory="
        ):
            raise ValidationError(
                "Docker Compose --project-directory запрещён: пути должны "
                "разрешаться относительно проверенного основного Compose-файла."
            )
        elif argument in _COMPOSE_GLOBAL_FLAGS:
            pass
        elif argument.startswith(("--file=", "-f=")):
            compose_files.append(argument.split("=", 1)[1])
        elif argument.startswith("-f") and not argument.startswith("--"):
            compose_files.append(argument[2:])
        elif argument.startswith("--env-file="):
            env_files.append(argument.split("=", 1)[1])
        elif argument.startswith(
            ("--ansi=", "--parallel=", "--profile=", "--progress=", "--project-name=")
        ):
            pass
        elif argument.startswith("-p") and not argument.startswith("--"):
            if len(argument) == 2:
                raise ValidationError("Docker Compose требует значение после -p.")
        elif argument.startswith("-"):
            raise ValidationError(
                f"Неподдерживаемая глобальная опция Docker Compose: {argument}"
            )
        else:
            raise ValidationError(
                f"Неизвестная подкоманда Docker Compose: {argument}"
            )
        index += 1
    return None, compose_files, env_files


def _is_docker_compose_command(command: tuple[str, ...]) -> bool:
    executable = command[0].replace("\\", "/").rsplit("/", 1)[-1]
    return executable == "docker" and len(command) >= 2 and command[1] == "compose"


def _is_compose_config_command(command: tuple[str, ...]) -> bool:
    if not _is_docker_compose_command(command):
        return False
    subcommand_index, _, _ = _parse_compose_global_options(command)
    return subcommand_index is not None and command[subcommand_index] == "config"


def _validate_compose_inputs(
    command: tuple[str, ...],
    *,
    cwd: Path | str | None,
) -> None:
    subcommand_index, compose_files, env_files = _parse_compose_global_options(command)
    if not compose_files:
        subcommand = (
            command[subcommand_index] if subcommand_index is not None else None
        )
        if subcommand in {"ls", "version"}:
            return
        raise ValidationError(
            "Docker Compose требует явный абсолютный Compose-файл через -f/--file."
        )
    compose_payloads: list[tuple[Path, bytes]] = []
    for value in compose_files:
        payload = _validate_compose_input_path(value, label="Compose-файл")
        compose_payloads.append((Path(value), payload))
    for value in env_files:
        _validate_compose_input_path(value, label="Compose env-файл")
    if not env_files:
        working_directory = Path(cwd) if cwd is not None else Path.cwd()
        if not working_directory.is_absolute():
            working_directory = Path.cwd() / working_directory
        candidates = {
            working_directory / ".env",
            Path(compose_files[0]).parent / ".env",
        }
        for default_env in candidates:
            if default_env.exists() or default_env.is_symlink():
                _validate_compose_input_path(
                    str(default_env), label="Compose .env-файл"
                )
    project_directory = compose_payloads[0][0].parent
    for path, payload in compose_payloads:
        _validate_compose_document_references(
            path,
            payload,
            project_directory=project_directory,
        )


def _validate_compose_input_path(value: str, *, label: str) -> bytes:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValidationError(f"{label} имеет некорректный путь.")
    path = Path(value)
    if not path.is_absolute():
        raise ValidationError(f"{label} должен иметь абсолютный путь: {value}")
    try:
        before = path.lstat()
    except OSError as error:
        raise ValidationError(f"{label} отсутствует или недоступен: {path}") from error
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValidationError(
            f"{label} должен быть обычным файлом без symlink/hardlink: {path}"
        )
    if os.name == "posix":
        if before.st_uid != os.geteuid():
            raise ValidationError(f"{label} принадлежит другому пользователю: {path}")
        if stat.S_IMODE(before.st_mode) & 0o022:
            raise ValidationError(
                f"{label} доступен для записи не только владельцу: {path}"
            )
    if before.st_size > _MAX_COMPOSE_INPUT_SIZE:
        raise ValidationError(
            f"{label} превышает допустимый размер {_MAX_COMPOSE_INPUT_SIZE} байт: {path}"
        )
    _validate_compose_parent_chain(path.parent, label=label)
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValidationError(f"{label} нельзя безопасно открыть: {path}") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size > _MAX_COMPOSE_INPUT_SIZE
        ):
            raise ValidationError(
                f"{label} был подменён или имеет небезопасный тип: {path}"
            )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            payload = stream.read(_MAX_COMPOSE_INPUT_SIZE + 1)
            after_open = os.fstat(stream.fileno())
        if len(payload) > _MAX_COMPOSE_INPUT_SIZE:
            raise ValidationError(
                f"{label} изменился и превысил допустимый размер "
                f"{_MAX_COMPOSE_INPUT_SIZE} байт: {path}"
            )
        try:
            after_path = path.lstat()
        except OSError as error:
            raise ValidationError(f"{label} исчез во время проверки: {path}") from error
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(
            getattr(opened, field) != getattr(after_open, field)
            or getattr(before, field) != getattr(after_path, field)
            for field in stable_fields
        ):
            raise ValidationError(f"{label} изменился во время проверки: {path}")
        return payload
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _compose_key(line: str) -> tuple[int, str, str] | None:
    match = _COMPOSE_KEY.match(line)
    if match is None:
        return None
    raw_key = match.group("key")
    key = raw_key[1:-1] if raw_key[:1] in {'"', "'"} else raw_key
    return len(match.group("indent")), key, match.group("value").strip()


def _strip_yaml_comment(line: str) -> str:
    single_quoted = False
    double_quoted = False
    escaped = False
    for index, character in enumerate(line):
        if double_quoted and character == "\\" and not escaped:
            escaped = True
            continue
        if character == '"' and not single_quoted and not escaped:
            double_quoted = not double_quoted
        elif character == "'" and not double_quoted:
            single_quoted = not single_quoted
        elif character == "#" and not single_quoted and not double_quoted:
            return line[:index].rstrip()
        escaped = False
    return line.rstrip()


def _compose_scalar(value: str, *, label: str) -> str:
    selected = value.strip()
    if not selected or "\x00" in selected:
        raise ValidationError(f"{label} имеет пустое или некорректное значение.")
    if selected.startswith('"'):
        try:
            decoded = json.loads(selected)
        except (json.JSONDecodeError, UnicodeError) as error:
            raise ValidationError(f"{label} имеет неподдерживаемую quoted-форму.") from error
        if not isinstance(decoded, str):
            raise ValidationError(f"{label} должен быть строкой.")
        return decoded
    if selected.startswith("'"):
        if len(selected) < 2 or not selected.endswith("'"):
            raise ValidationError(f"{label} имеет незакрытую кавычку.")
        return selected[1:-1].replace("''", "'")
    if selected[0] in "[{&*!|>" or "\t" in selected:
        raise ValidationError(
            f"{label} использует неподдерживаемую динамическую YAML-форму."
        )
    return selected


def _compose_block_end(
    lines: Sequence[str],
    start: int,
    indent: int,
) -> int:
    for index in range(start + 1, len(lines)):
        line = lines[index]
        if not line.strip():
            continue
        current = len(line) - len(line.lstrip(" "))
        if current <= indent:
            return index
    return len(lines)


def _compose_reference_path(
    value: str,
    *,
    project_directory: Path,
    label: str,
) -> Path:
    if not value or "\x00" in value or "$" in value or value.startswith("~"):
        raise ValidationError(
            f"{label} должен быть статическим путём без interpolation или '~': {value!r}"
        )
    source = Path(value)
    if not source.is_absolute():
        source = project_directory / source
    return Path(os.path.abspath(source))


def _validate_compose_env_reference(
    value: str,
    *,
    project_directory: Path,
) -> None:
    path = _compose_reference_path(
        value,
        project_directory=project_directory,
        label="Внутренний Compose env_file",
    )
    expected_parent = Path(os.path.abspath(project_directory))
    if path.parent != expected_parent or not (
        path.name == ".env" or path.name.startswith(".env.")
    ):
        raise ValidationError(
            "Внутренний Compose env_file должен быть файлом .env/.env.* рядом с "
            f"основным Compose-файлом, чтобы manager контролировал его drift: {path}"
        )
    _validate_compose_input_path(str(path), label="Внутренний Compose env_file")


def validate_certbot_live_symlink(path: Path) -> Path:
    try:
        before = path.lstat()
    except OSError as error:
        raise ValidationError(f"Certbot live-ссылка отсутствует: {path}") from error
    if not stat.S_ISLNK(before.st_mode):
        raise ValidationError(f"Ожидалась Certbot live-ссылка: {path}")
    live_root = _LETSENCRYPT_ROOT / "live"
    archive_root = _LETSENCRYPT_ROOT / "archive"
    try:
        relative = path.relative_to(live_root)
    except ValueError as error:
        raise ValidationError(
            f"Compose bind source не может быть символической ссылкой: {path}"
        ) from error
    if (
        len(relative.parts) != 2
        or path.name not in _CERTBOT_LIVE_FILENAMES
        or before.st_nlink != 1
        or (os.name == "posix" and before.st_uid != os.geteuid())
    ):
        raise ValidationError(
            f"Compose bind source не является безопасной Certbot live-ссылкой: {path}"
        )
    try:
        raw_target = os.readlink(path)
    except OSError as error:
        raise ValidationError(f"Certbot live-ссылку нельзя прочитать: {path}") from error
    link_target = Path(raw_target)
    if link_target.is_absolute():
        raise ValidationError(
            f"Certbot live-ссылка должна использовать относительную цель: {path}"
        )
    target = Path(os.path.abspath(path.parent / link_target))
    expected_parent = archive_root / relative.parts[0]
    expected_name = re.fullmatch(
        rf"{re.escape(path.stem)}[1-9][0-9]*\.pem", target.name
    )
    if target.parent != expected_parent or expected_name is None:
        raise ValidationError(
            f"Certbot live-ссылка ведёт вне соответствующего archive lineage: {path}"
        )

    _validate_compose_parent_chain(path.parent, label="Compose bind source")
    _validate_compose_input_path(str(target), label="Certbot archive source")
    try:
        after = path.lstat()
        after_target = os.readlink(path)
    except OSError as error:
        raise ValidationError(
            f"Certbot live-ссылка изменилась во время проверки: {path}"
        ) from error
    stable_fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_uid", "st_gid")
    if after_target != raw_target or any(
        getattr(before, field) != getattr(after, field) for field in stable_fields
    ):
        raise ValidationError(
            f"Certbot live-ссылка изменилась во время проверки: {path}"
        )
    return target


def _validate_compose_bind_reference(
    value: str,
    *,
    project_directory: Path,
) -> None:
    if os.name != "posix" and value.rstrip("/") == "/dev/shm":  # noqa: S108, RUF100 - fixed Linux shared-memory mount
        # Linux-only manager input used by generator tests on a Windows workstation.
        # The real production path is still checked on Ubuntu before Docker runs.
        return
    path = _compose_reference_path(
        value,
        project_directory=project_directory,
        label="Compose bind source",
    )
    try:
        info = path.lstat()
    except OSError as error:
        raise ValidationError(f"Compose bind source отсутствует или недоступен: {path}") from error
    if stat.S_ISREG(info.st_mode):
        _validate_compose_input_path(str(path), label="Compose bind source")
        return
    if stat.S_ISLNK(info.st_mode):
        validate_certbot_live_symlink(path)
        return
    if not stat.S_ISDIR(info.st_mode):
        raise ValidationError(
            f"Compose bind source должен быть обычным файлом или каталогом: {path}"
        )
    # Writable container data directories may intentionally belong to a service
    # UID (for example PostgreSQL 999). The root-owned, non-writable parent chain
    # prevents that service from swapping the bind path itself.
    _validate_compose_parent_chain(path.parent, label="Compose bind source")


def _validate_compose_volume_block(
    lines: Sequence[str],
    start: int,
    indent: int,
    value: str,
    *,
    project_directory: Path,
) -> None:
    if value:
        if value in {"[]", "{}"}:
            return
        raise ValidationError(
            "Compose volumes использует flow/alias-форму, которую нельзя безопасно "
            "проверить перед запуском."
        )
    end = _compose_block_end(lines, start, indent)
    items: list[tuple[int, re.Match[str]]] = []
    for index in range(start + 1, end):
        match = _COMPOSE_LIST_ITEM.match(lines[index])
        if match is not None:
            items.append((index, match))
    if not items:
        return
    item_indent = min(len(match.group("indent")) for _, match in items)
    direct_items = [
        (index, match)
        for index, match in items
        if len(match.group("indent")) == item_indent
    ]
    for position, (item_index, match) in enumerate(direct_items):
        item_end = (
            direct_items[position + 1][0]
            if position + 1 < len(direct_items)
            else end
        )
        first = match.group("value").strip()
        first_key = _compose_key(" " * (item_indent + 2) + first)
        if first_key is None or first_key[1] not in {
            "bind",
            "consistency",
            "read_only",
            "source",
            "target",
            "tmpfs",
            "type",
            "volume",
        }:
            scalar = _compose_scalar(first, label="Compose volume")
            if ":" not in scalar:
                continue
            source = scalar.split(":", 1)[0]
            if not source:
                continue
            if _COMPOSE_NAMED_VOLUME.fullmatch(source):
                continue
            _validate_compose_bind_reference(
                source,
                project_directory=project_directory,
            )
            continue

        fields: dict[str, str] = {first_key[1]: first_key[2]}
        for nested_index in range(item_index + 1, item_end):
            nested = _compose_key(lines[nested_index])
            if nested is None or nested[0] <= item_indent:
                continue
            if nested[1] in {"type", "source"}:
                if nested[1] in fields:
                    raise ValidationError(
                        f"Compose volume повторяет поле {nested[1]}."
                    )
                fields[nested[1]] = nested[2]
        if "type" not in fields:
            raise ValidationError(
                "Compose volume long syntax не содержит обязательное поле type."
            )
        volume_type = _compose_scalar(fields["type"], label="Compose volume type")
        if volume_type not in {"bind", "tmpfs", "volume"}:
            raise ValidationError(
                f"Compose volume использует неподдерживаемый или динамический type: {volume_type!r}"
            )
        if volume_type == "tmpfs":
            if "source" in fields:
                raise ValidationError("Compose tmpfs volume не должен содержать source.")
            continue
        if volume_type == "volume":
            if "source" in fields:
                source = _compose_scalar(
                    fields["source"], label="Compose named volume source"
                )
                if _COMPOSE_NAMED_VOLUME.fullmatch(source) is None:
                    raise ValidationError(
                        "Compose named volume source должен быть статическим именем."
                    )
            continue
        if "source" not in fields:
            raise ValidationError("Compose bind volume не содержит source.")
        source = _compose_scalar(fields["source"], label="Compose bind source")
        _validate_compose_bind_reference(
            source,
            project_directory=project_directory,
        )


def _validate_compose_document_references(
    path: Path,
    payload: bytes,
    *,
    project_directory: Path,
) -> None:
    try:
        text_payload = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValidationError(f"Compose-файл не является корректным UTF-8: {path}") from error
    lines = [_strip_yaml_comment(line) for line in text_payload.splitlines()]
    for line in lines:
        leading = line[: len(line) - len(line.lstrip())]
        if "\t" in leading:
            raise ValidationError(
                f"Compose-файл содержит tab в отступе и не может быть безопасно проверен: {path}"
            )
    for index, line in enumerate(lines):
        parsed = _compose_key(line)
        if parsed is None:
            stripped = line.lstrip()
            if not stripped or stripped in {"---", "..."}:
                continue
            list_item = _COMPOSE_LIST_ITEM.match(line)
            if list_item is not None:
                item_value = list_item.group("value").strip()
                if item_value.startswith(("&", "*", "!", "%", "?", "[", "{")):
                    raise ValidationError(
                        "Compose-файл использует неподдерживаемый динамический "
                        f"YAML list item: {path}"
                    )
                continue
            if re.fullmatch(
                r"<<:[ \t]+(?:\*[A-Za-z0-9_.-]+|"
                r"\[[ \t]*\*[A-Za-z0-9_.-]+(?:[ \t]*,[ \t]*"
                r"\*[A-Za-z0-9_.-]+)*[ \t]*\])",
                stripped,
            ):
                continue
            if stripped.startswith("<<:"):
                raise ValidationError(
                    "Compose merge должен ссылаться на статический anchor или их "
                    f"статический список: {path}"
                )
            if stripped.startswith(("--- ", "... ")) or stripped[0] in {
                '"',
                "'",
                "!",
                "%",
                "&",
                "*",
                "?",
                "[",
                "{",
            }:
                raise ValidationError(
                    "Compose-файл использует неподдерживаемую динамическую YAML-форму "
                    f"ключа или mapping: {path}"
                )
            continue
        indent, key, value = parsed
        if re.match(r"^&[A-Za-z0-9_.-]+[ \t]+[\[{]", value):
            raise ValidationError(
                f"Compose anchor не должен скрывать flow collection: {path}"
            )
        if value.startswith("{") and value != "{}":
            raise ValidationError(
                f"Compose key {key} использует непустую flow mapping, которую "
                f"manager не может безопасно проверить: {path}"
            )
        if key in _FORBIDDEN_COMPOSE_REFERENCE_KEYS:
            raise ValidationError(
                f"Compose key {key} запрещён manager trust policy: {path}. "
                "Используйте только проверенные image и локальные env/bind sources."
            )
        if key == "env_file":
            if value:
                if value == "[]":
                    continue
                _validate_compose_env_reference(
                    _compose_scalar(value, label="Compose env_file"),
                    project_directory=project_directory,
                )
                continue
            end = _compose_block_end(lines, index, indent)
            references: list[str] = []
            for nested in lines[index + 1 : end]:
                item = _COMPOSE_LIST_ITEM.match(nested)
                if item is None:
                    if nested.strip():
                        raise ValidationError(
                            "Compose env_file использует неподдерживаемую long/alias-форму."
                        )
                    continue
                references.append(
                    _compose_scalar(item.group("value"), label="Compose env_file")
                )
            if not references:
                raise ValidationError("Compose env_file не содержит путей.")
            for reference in references:
                _validate_compose_env_reference(
                    reference,
                    project_directory=project_directory,
                )
        elif key == "volumes":
            _validate_compose_volume_block(
                lines,
                index,
                indent,
                value,
                project_directory=project_directory,
            )


def _validate_compose_parent_chain(path: Path, *, label: str) -> None:
    if not path.is_absolute():
        raise ValidationError(f"Родитель {label} должен иметь абсолютный путь: {path}")
    expected_uid = os.geteuid() if os.name == "posix" else None
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except OSError as error:
            raise ValidationError(
                f"Родитель {label} недоступен: {candidate}"
            ) from error
        if not stat.S_ISDIR(info.st_mode):
            raise ValidationError(
                f"Родитель {label} имеет небезопасный тип: {candidate}"
            )
        if os.name != "posix":
            continue
        if expected_uid == 0:
            owner_ok = info.st_uid == 0
        else:
            owner_ok = info.st_uid in {0, expected_uid}
        if not owner_ok:
            raise ValidationError(
                f"Родитель {label} принадлежит другому пользователю: {candidate}"
            )
        mode = stat.S_IMODE(info.st_mode)
        world_writable_sticky = (
            info.st_uid == 0 and mode & stat.S_ISVTX and mode & 0o002
        )
        if mode & 0o022 and not world_writable_sticky:
            raise ValidationError(
                f"Родитель {label} доступен для записи группе/прочим: {candidate}"
            )


def trusted_home() -> Path:
    if os.name != "posix":
        return Path.home()
    try:
        import pwd

        return Path(pwd.getpwuid(os.geteuid()).pw_dir)
    except (ImportError, KeyError, OSError):
        return Path("/root" if os.geteuid() == 0 else "/")


def docker_config_directory() -> Path:
    return trusted_home() / ".docker"


def _subprocess_environment(
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    source = os.environ if overrides is None else overrides
    result: dict[str, str] = {}
    for key, value in source.items():
        if (
            not isinstance(key, str)
            or not isinstance(value, str)
            or not key
            or "=" in key
            or "\x00" in key
            or "\x00" in value
        ):
            raise ValidationError(
                "Environment внешней команды содержит некорректное значение."
            )
        normalized = key.upper()
        blocked_prefix = normalized.startswith(_BLOCKED_ENVIRONMENT_PREFIXES)
        explicitly_allowed = (
            overrides is not None
            and normalized in _EXPLICIT_ONLY_ENVIRONMENT_KEYS
        )
        if normalized in _BLOCKED_ENVIRONMENT_KEYS or (
            blocked_prefix and not explicitly_allowed
        ):
            continue
        if overrides is None and normalized in _BLOCKED_INHERITED_ENVIRONMENT_KEYS:
            continue
        if normalized.startswith("DOCKER_"):
            continue
        result[key] = value

    if os.name == "posix":
        result["PATH"] = _SAFE_POSIX_PATH
        result["TMPDIR"] = "/tmp"  # noqa: S108, RUF100 - fixed sticky system temp, replacing inherited TMPDIR
        result.pop("TMP", None)
        result.pop("TEMP", None)
        result["HOME"] = str(trusted_home())
        result["DOCKER_CONFIG"] = str(docker_config_directory())
    elif not any(key.upper() == "PATH" for key in result):
        result["PATH"] = os.defpath
    return result


def _fsync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Runner:
    def __init__(self, *, dry_run: bool = False) -> None:
        self.dry_run = dry_run

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        input_text: str | None = None,
        timeout: int | None = 120,
        check: bool = True,
        sensitive: bool = False,
    ) -> Result:
        validated = _validated_command(args)
        compose_invocation = _is_docker_compose_command(validated)
        if compose_invocation and env is not None:
            raise ValidationError(
                "Передача process environment в Docker Compose запрещена; "
                "используйте проверенный --env-file."
            )
        sensitive = sensitive or _is_compose_config_command(validated)
        command = _local_docker_command(validated, cwd=cwd)
        if self.dry_run:
            return Result(command, 0, "", "")
        try:
            completed = subprocess.run(  # noqa: S603, RUF100 - validated argv, shell=False
                command,
                cwd=cwd,
                env=_subprocess_environment({} if compose_invocation else env),
                input=input_text,
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            displayed = "<скрыта>" if sensitive else _display_command(command)
            failure = CommandError(f"Превышено время выполнения команды: {displayed}")
            if sensitive:
                raise failure from None
            raise failure from error
        except OSError as error:
            displayed = "<скрыта>" if sensitive else _display_command(command)
            detail = "" if sensitive else f": {error}"
            failure = CommandError(
                f"Не удалось запустить команду: {displayed}{detail}"
            )
            if sensitive:
                raise failure from None
            raise failure from error
        result = Result(
            command, completed.returncode, completed.stdout, completed.stderr
        )
        if check and completed.returncode != 0:
            detail = (
                ""
                if sensitive
                else _sanitized_detail(completed.stderr or completed.stdout)
            )
            displayed = "<скрыта>" if sensitive else _display_command(command)
            message = f"Команда завершилась с кодом {completed.returncode}: {displayed}"
            if detail:
                message += f"\n{detail}"
            raise CommandError(message)
        return result

    def run_to_file(
        self,
        args: Sequence[str],
        target: Path,
        *,
        cwd: Path | str | None = None,
        timeout: int = 600,
        sensitive: bool = False,
    ) -> None:
        validated = _validated_command(args)
        compose_invocation = _is_docker_compose_command(validated)
        sensitive = sensitive or _is_compose_config_command(validated)
        command = _local_docker_command(validated, cwd=cwd)
        if self.dry_run:
            return
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{target.name}.", dir=target.parent
        )
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                try:
                    completed = subprocess.run(  # noqa: S603, RUF100 - validated argv, shell=False
                        command,
                        cwd=cwd,
                        env=_subprocess_environment({} if compose_invocation else None),
                        stdout=stream,
                        stderr=subprocess.PIPE,
                        timeout=timeout,
                        check=False,
                    )
                except subprocess.TimeoutExpired as error:
                    displayed = "<скрыта>" if sensitive else _display_command(command)
                    failure = CommandError(
                        f"Превышено время выполнения команды: {displayed}"
                    )
                    if sensitive:
                        raise failure from None
                    raise failure from error
                except OSError as error:
                    displayed = "<скрыта>" if sensitive else _display_command(command)
                    detail = "" if sensitive else f": {error}"
                    failure = CommandError(
                        f"Не удалось запустить команду: {displayed}{detail}"
                    )
                    if sensitive:
                        raise failure from None
                    raise failure from error
                stream.flush()
                os.fsync(stream.fileno())
            if completed.returncode != 0:
                detail = (
                    ""
                    if sensitive
                    else _sanitized_detail(completed.stderr.decode("utf-8", "replace"))
                )
                displayed = "<скрыта>" if sensitive else _display_command(command)
                raise CommandError(
                    f"Команда завершилась с кодом {completed.returncode}: {displayed}"
                    + (f"\n{detail}" if detail else "")
                )
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, target)
            _fsync_directory(target.parent)
        finally:
            temporary_path.unlink(missing_ok=True)

    def interactive(
        self,
        args: Sequence[str],
        *,
        input_text: str | None = None,
        cwd: Path | str | None = None,
    ) -> None:
        validated = _validated_command(args)
        compose_invocation = _is_docker_compose_command(validated)
        command = _local_docker_command(validated, cwd=cwd)
        if self.dry_run:
            return
        try:
            completed = subprocess.run(  # noqa: S603, RUF100 - validated argv, shell=False
                command,
                cwd=cwd,
                env=_subprocess_environment({} if compose_invocation else None),
                input=input_text,
                text=True,
                check=False,
            )
        except OSError as error:
            raise CommandError(
                f"Не удалось запустить команду: {_display_command(command)}: {error}"
            ) from error
        if completed.returncode != 0:
            raise CommandError(
                f"Команда завершилась с кодом {completed.returncode}: {_display_command(command)}"
            )


def sha256_file(path: Path) -> str:
    try:
        before = path.lstat()
    except OSError as error:
        raise ValidationError(
            f"Не удалось проверить файл {path}: {error}"
        ) from error
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValidationError(f"Путь не является обычным файлом без hardlink: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValidationError(
            f"Не удалось безопасно открыть файл {path}: {error}"
        ) from error
    try:
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                raise ValidationError(
                    f"Файл {path} был подменён или имеет небезопасный тип."
                )
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = -1
                remaining = opened.st_size
                while remaining:
                    block = stream.read(min(1024 * 1024, remaining))
                    if not block:
                        break
                    digest.update(block)
                    remaining -= len(block)
                grew = stream.read(1)
                after_open = os.fstat(stream.fileno())
            after_path = path.lstat()
        except OSError as error:
            raise ValidationError(
                f"Не удалось стабильно прочитать файл {path}: {error}"
            ) from error
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if remaining or grew or any(
            getattr(opened, field) != getattr(after_open, field)
            or getattr(before, field) != getattr(after_path, field)
            for field in stable_fields
        ):
            raise ValidationError(f"Файл {path} изменился во время хеширования.")
        return digest.hexdigest()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def read_stable_regular_file(
    path: Path,
    *,
    max_size: int,
    label: str = "Файл",
) -> RegularFileSnapshot:
    if (
        isinstance(max_size, bool)
        or not isinstance(max_size, int)
        or not 1 <= max_size <= _MAX_ATOMIC_COPY_SIZE
        or not isinstance(label, str)
        or not label.strip()
    ):
        raise ValidationError("Некорректные ограничения безопасного чтения файла.")
    try:
        before = path.lstat()
    except OSError as error:
        raise ValidationError(f"{label} отсутствует или недоступен: {path}") from error
    if not stat.S_ISREG(before.st_mode):
        raise ValidationError(f"{label} должен быть обычным файлом: {path}")
    if before.st_nlink != 1:
        raise ValidationError(f"{label} является hardlink: {path}")
    if before.st_size > max_size:
        raise ValidationError(f"{label} превышает допустимый размер {max_size} байт: {path}")
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValidationError(f"{label} нельзя безопасно открыть: {path}") from error
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            or opened.st_size > max_size
        ):
            raise ValidationError(
                f"{label} был подменён или имеет небезопасный тип: {path}"
            )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            payload = stream.read(max_size + 1)
            after_open = os.fstat(stream.fileno())
        if len(payload) > max_size:
            raise ValidationError(
                f"{label} изменился и превысил допустимый размер {max_size} байт: {path}"
            )
        try:
            after_path = path.lstat()
        except OSError as error:
            raise ValidationError(f"{label} исчез во время чтения: {path}") from error
        stable_fields = (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_gid",
            "st_nlink",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
        if any(
            getattr(opened, field) != getattr(after_open, field)
            or getattr(before, field) != getattr(after_path, field)
            for field in stable_fields
        ):
            raise ValidationError(f"{label} изменился во время чтения: {path}")
        return RegularFileSnapshot(
            data=payload,
            mode=stat.S_IMODE(opened.st_mode),
            uid=opened.st_uid,
            gid=opened.st_gid,
        )
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = 0o600) -> None:
    if isinstance(mode, bool) or not isinstance(mode, int) or not 0 <= mode <= 0o777:
        raise ValidationError(f"Некорректные права файла {path}.")
    temp_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        temp_path = Path(temporary)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
        _fsync_directory(path.parent)
    except OSError as error:
        raise ValidationError(
            f"Не удалось атомарно записать файл {path}: {error}"
        ) from error
    finally:
        if temp_path is not None:
            with contextlib.suppress(OSError):
                temp_path.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def atomic_write_json(path: Path, value: object, *, mode: int = 0o600) -> None:
    try:
        payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    except (TypeError, ValueError, RecursionError) as error:
        raise ValidationError(f"Не удалось сериализовать JSON для {path}.") from error
    atomic_write_text(path, payload, mode=mode)


def atomic_copy(source: Path, target: Path, *, mode: int | None = None) -> None:
    snapshot = read_stable_regular_file(
        source,
        max_size=_MAX_ATOMIC_COPY_SIZE,
        label="Источник копирования",
    )
    selected_mode = mode if mode is not None else snapshot.mode
    atomic_write_bytes(target, snapshot.data, mode=selected_mode)


def require_root() -> None:
    if os.name == "posix" and os.geteuid() != 0:
        raise ValidationError(
            "Эта операция должна выполняться от root (используйте sudo)."
        )


def require_ubuntu_2404(os_release: Path = Path("/etc/os-release")) -> None:
    try:
        values = {}
        for line in os_release.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value.strip().strip('"')
    except OSError as error:
        raise ValidationError(f"Не удалось прочитать {os_release}: {error}") from error
    if values.get("ID") != "ubuntu" or values.get("VERSION_ID") != "24.04":
        raise ValidationError("Поддерживается только Ubuntu 24.04 LTS.")


@contextlib.contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    if not path.is_absolute():
        raise ValidationError(f"Lock-путь должен быть абсолютным: {path}.")
    parent = path.parent
    if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
        raise ValidationError(f"Lock-каталог {parent} имеет небезопасный тип.")
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        parent_info = parent.lstat()
    except OSError as error:
        raise ValidationError(
            f"Не удалось безопасно подготовить lock-каталог {parent}: {error}"
        ) from error
    if not stat.S_ISDIR(parent_info.st_mode):
        raise ValidationError(f"Lock-каталог {parent} имеет небезопасный тип.")
    if os.name == "posix" and (
        parent_info.st_uid != os.geteuid()
        or parent_info.st_gid != os.getegid()
        or stat.S_IMODE(parent_info.st_mode) != 0o700
    ):
        raise ValidationError(
            f"Lock-каталог {parent} должен принадлежать текущему пользователю и иметь права 0700."
        )
    if path.is_symlink():
        raise ValidationError(f"Lock-файл {path} является символьной ссылкой.")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise ValidationError(
            f"Не удалось безопасно открыть lock-файл {path}: {error}"
        ) from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValidationError(f"Lock-файл {path} имеет небезопасный тип.")
        if os.name == "posix":
            if info.st_uid != os.geteuid():
                raise ValidationError(
                    f"Lock-файл {path} принадлежит другому пользователю."
                )
            os.fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "a+b")
        descriptor = -1
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        if os.name == "posix":
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValidationError(
                    "Другой процесс remnawave-manager уже выполняет операцию."
                ) from error
        try:
            yield
        finally:
            if os.name == "posix":
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def command_exists(name: str) -> bool:
    search_path = _SAFE_POSIX_PATH if os.name == "posix" else None
    return shutil.which(name, path=search_path) is not None


def ensure_within(path: Path, parent: Path) -> Path:
    resolved = path.resolve()
    base = parent.resolve()
    if resolved != base and base not in resolved.parents:
        raise ValidationError(
            f"Путь {resolved} находится вне разрешённого каталога {base}."
        )
    return resolved
