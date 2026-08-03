from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path

from .compose import compose_command
from .errors import ManagerError, TransactionError, ValidationError
from .models import Inventory
from .runner import (
    Runner,
    atomic_write_text,
    read_stable_regular_file,
    sanitize_external_text,
)

GZIP_BLOCK = """# BEGIN REMNAWAVE-MANAGER GZIP
gzip on;
gzip_vary on;
gzip_proxied any;
gzip_min_length 1024;
gzip_comp_level 6;
gzip_types application/javascript application/json application/manifest+json application/wasm application/xml font/eot font/opentype font/otf font/ttf image/svg+xml text/css text/javascript text/plain text/xml;
# END REMNAWAVE-MANAGER GZIP

"""

_MAX_NGINX_CONFIG_SIZE = 16 * 1024 * 1024
_NGINX_QUOTED_VALUE = re.compile(r'(["\'])(?:\\.|(?!\1).)*\1')
_NGINX_URL = re.compile(r"(?i)\bhttps?://\S+")
_NGINX_POSIX_PATH = re.compile(r"/[^\s:]+(?P<line>:\d+)?")
_NGINX_WINDOWS_PATH = re.compile(r"[A-Za-z]:\\[^\s:]+(?P<line>:\d+)?")
_NGINX_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_-]{24,}\b")

_REQUIRED_GZIP_TYPES = (
    "application/javascript",
    "application/json",
    "application/manifest+json",
    "application/wasm",
    "application/xml",
    "font/eot",
    "font/opentype",
    "font/otf",
    "font/ttf",
    "image/svg+xml",
    "text/css",
    "text/javascript",
    "text/plain",
    "text/xml",
)

_PANEL_PROXY = re.compile(
    r"\bproxy_pass\s+http://(?:remnawave(?:_panel)?|127\.0\.0\.1:3000)(?=[/;\s])",
    re.IGNORECASE,
)
_SUBSCRIPTION_PROXY = re.compile(
    r"\bproxy_pass\s+http://(?:json|remnawave_subscription|127\.0\.0\.1:3010)(?=[/;\s])",
    re.IGNORECASE,
)


def _structural_text(original: str) -> str:
    """Mask comments and quoted values while preserving offsets and braces."""

    output = list(original)
    quote: str | None = None
    escaped = False
    comment = False
    for index, character in enumerate(original):
        if comment:
            if character in "\r\n":
                comment = False
            else:
                output[index] = " "
            continue
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            if character not in "\r\n":
                output[index] = " "
            continue
        if character in {'"', "'"}:
            quote = character
            output[index] = " "
        elif character == "#":
            comment = True
            output[index] = " "
    return "".join(output)


def _context_scope(original: str) -> tuple[str, int, int, int, list[int]]:
    """Return the structural text and the offsets of the global HTTP scope."""

    structural = _structural_text(original)
    depths: list[int] = []
    depth = 0
    for character in structural:
        depths.append(depth)
        if character == "{":
            depth += 1
        elif character == "}":
            depth = max(0, depth - 1)

    http = re.search(r"(?m)^[ \t]*http[ \t\r\n]*\{", structural)
    if http is None:
        return structural, 0, len(structural), 0, depths

    opening = structural.find("{", http.start(), http.end())
    expected_depth = depths[opening] + 1
    closing_depth = depths[opening]
    nested = expected_depth
    end = len(structural)
    for position in range(opening + 1, len(structural)):
        if structural[position] == "{":
            nested += 1
        elif structural[position] == "}":
            nested -= 1
            if nested == closing_depth:
                end = position
                break
    return structural, opening + 1, end, expected_depth, depths


def _context_directives(
    original: str,
    name: str,
) -> tuple[str, int, int, int, list[int], list[re.Match[str]]]:
    structural, start, end, expected_depth, depths = _context_scope(original)
    directive = re.compile(
        rf"(?m)(?:^[ \t]*|(?<=[;{{}}])[ \t]*)(?P<directive>{re.escape(name)}\b(?P<args>[^;]*);)"
    )
    matches = [
        match
        for match in directive.finditer(structural[start:end])
        if depths[match.start("directive") + start] == expected_depth
    ]
    return structural, start, end, expected_depth, depths, matches


def _gzip_type_tokens(match: re.Match[str]) -> set[str]:
    """Read unquoted MIME tokens from a structural gzip_types directive."""

    return {
        token.lower()
        for token in match.group("args").split()
        if re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+/[A-Za-z0-9!#$%&'*+.^_`|~-]+", token)
    }


def _with_required_gzip(original: str) -> str:
    """Enable global gzip and ensure the API response MIME types are compressed."""

    _, start, _, _, _, type_matches = _context_directives(
        original, "gzip_types"
    )
    _, _, _, _, _, gzip_matches = _context_directives(original, "gzip")
    latest_types = type_matches[-1] if type_matches else None
    existing_types = _gzip_type_tokens(latest_types) if latest_types else set()
    missing = [
        mime
        for mime in _REQUIRED_GZIP_TYPES
        if mime not in existing_types
    ]

    updates: list[tuple[int, str]] = []
    if gzip_matches:
        latest_gzip = gzip_matches[-1]
        state = re.search(r"\b(on|off)\b", latest_gzip.group("directive"))
        if state is not None and state.group(1) == "off":
            updates.append((latest_gzip.start("directive") + state.start(1), "on"))
    if latest_types is not None and missing:
        # Insert immediately after the directive name. This remains valid when
        # an existing directive has an inline comment before its semicolon.
        updates.append(
            (
                latest_types.start("directive") + len("gzip_types"),
                " " + " ".join(missing),
            )
        )

    if not gzip_matches:
        if latest_types is None:
            return _with_gzip_block(original)
        insert_at = latest_types.start("directive")
        updates.append((insert_at, "gzip on;\n"))
    elif latest_types is None:
        latest_gzip = gzip_matches[-1]
        insert_at = latest_gzip.end("directive")
        updates.append((insert_at, "\n" + "gzip_types " + " ".join(_REQUIRED_GZIP_TYPES) + ";"))

    if not updates:
        return original
    # All offsets above are relative to the HTTP-scope slice. Convert them to
    # absolute offsets before applying in reverse order.
    absolute_updates = [(offset + start, value) for offset, value in updates]
    updated = original
    for offset, value in sorted(absolute_updates, reverse=True):
        updated = updated[:offset] + value + updated[offset:]
    return updated


def _with_gzip_block(original: str) -> str:
    structural = _structural_text(original)
    http = re.search(r"(?m)^[ \t]*http[ \t\r\n]*\{", structural)
    if http is None:
        return GZIP_BLOCK + original
    opening_brace = structural.find("{", http.start(), http.end())
    line_start = original.rfind("\n", 0, http.start()) + 1
    base_indent = original[line_start:http.start()]
    indent = base_indent + "    "
    block = "\n".join(
        indent + line if line else ""
        for line in GZIP_BLOCK.rstrip("\n").splitlines()
    )
    return original[: opening_brace + 1] + "\n" + block + "\n" + original[opening_brace + 1 :]


def _block_end(structural: str, opening: int) -> int | None:
    depth = 1
    for position in range(opening + 1, len(structural)):
        if structural[position] == "{":
            depth += 1
        elif structural[position] == "}":
            depth -= 1
            if depth == 0:
                return position
    return None


def _server_blocks(original: str) -> list[tuple[int, int, int]]:
    """Return (server keyword, opening brace, closing brace) offsets."""

    structural = _structural_text(original)
    pattern = re.compile(
        r"(?m)(?:^|[;{}])(?P<spacing>[ \t\r\n]*)(?P<server>server[ \t\r\n]*\{)"
    )
    blocks: list[tuple[int, int, int]] = []
    for match in pattern.finditer(structural):
        start = match.start("server")
        opening = structural.find("{", start, match.end("server"))
        closing = _block_end(structural, opening)
        if closing is not None:
            blocks.append((start, opening, closing))
    return blocks


def _brace_depths(structural: str) -> list[int]:
    depths: list[int] = []
    depth = 0
    for character in structural:
        depths.append(depth)
        if character == "{":
            depth += 1
        elif character == "}":
            depth = max(0, depth - 1)
    return depths


def _with_panel_proxy_compatibility(original: str) -> str:
    """Migrate only unambiguous legacy Panel/Subscription proxy blocks."""

    structural = _structural_text(original)
    depths = _brace_depths(structural)
    updates: list[tuple[int, int, str]] = []
    for start, opening, closing in _server_blocks(original):
        content = structural[opening + 1 : closing]
        panel = _PANEL_PROXY.search(content) is not None
        subscription = _SUBSCRIPTION_PROXY.search(content) is not None
        if not panel and not subscription:
            continue

        for match in re.finditer(
            r"\bproxy_(?:read|send)_timeout\s+(?P<value>60s)(?=\s*;)",
            content,
            re.IGNORECASE,
        ):
            value_start = opening + 1 + match.start("value")
            updates.append((value_start, value_start + len("60s"), "240s"))

        proxy_protocol = re.search(
            r"\blisten\b[^;]*\bproxy_protocol\b[^;]*;",
            content,
            re.IGNORECASE,
        ) is not None
        forwarded_for = "$proxy_protocol_addr" if proxy_protocol else "$remote_addr"
        for match in re.finditer(
            r"\bproxy_set_header\s+X-Forwarded-For\s+"
            r"(?P<value>\$proxy_add_x_forwarded_for)(?=\s*;)",
            content,
            re.IGNORECASE,
        ):
            value_start = opening + 1 + match.start("value")
            updates.append(
                (
                    value_start,
                    value_start + len("$proxy_add_x_forwarded_for"),
                    forwarded_for,
                )
            )

        if subscription:
            direct_depth = depths[opening] + 1
            access_logs = re.finditer(
                r"(?:^|[;{}])\s*(?P<directive>access_log\b[^;]*;)",
                content,
                re.IGNORECASE,
            )
            has_direct_access_log = any(
                depths[opening + 1 + match.start("directive")] == direct_depth
                for match in access_logs
            )
            if not has_direct_access_log:
                line_start = original.rfind("\n", 0, start) + 1
                base_indent = original[line_start:start]
                indent = base_indent + "    "
                updates.append(
                    (
                        opening + 1,
                        opening + 1,
                        f"\n{indent}access_log off;",
                    )
                )

    if not updates:
        return original
    updated = original
    for begin, end, value in sorted(updates, reverse=True):
        updated = updated[:begin] + value + updated[end:]
    return updated


def _read_nginx_config(path: Path) -> tuple[str, int]:
    snapshot = read_stable_regular_file(
        path,
        max_size=_MAX_NGINX_CONFIG_SIZE,
        label="Nginx-конфигурация",
    )
    if os.name == "posix" and (
        snapshot.uid != os.geteuid() or snapshot.mode & 0o022
    ):
        raise ValidationError(
            f"Nginx-конфигурация {path} имеет небезопасного владельца или права."
        )
    try:
        text = snapshot.data.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValidationError(
            f"Nginx-конфигурация {path} не является корректным UTF-8."
        ) from error
    return text, snapshot.mode


def _assert_nginx_config(path: Path, expected: str, expected_mode: int) -> None:
    current, mode = _read_nginx_config(path)
    if current != expected or mode != expected_mode:
        raise ValidationError(
            f"Nginx-конфигурация {path} изменилась параллельно; перезапись отменена."
        )


def _has_known_proxy(original: str, pattern: re.Pattern[str]) -> bool:
    structural = _structural_text(original)
    return any(
        pattern.search(structural[opening + 1 : closing]) is not None
        for _, opening, closing in _server_blocks(original)
    )


def ensure_gzip(
    runner: Runner,
    inventory: Inventory,
    *,
    before_write: Callable[[Path, str], None] | None = None,
) -> Path | None:
    paths = [Path(item) for item in inventory.nginx_files]
    if not paths:
        raise ValidationError("Не найден nginx-конфиг Panel/Subscription Page.")
    snapshots = {path: _read_nginx_config(path) for path in paths}
    originals = {path: snapshot[0] for path, snapshot in snapshots.items()}
    panel_paths = [
        path for path, original in originals.items() if _has_known_proxy(original, _PANEL_PROXY)
    ]
    subscription_paths = [
        path
        for path, original in originals.items()
        if _has_known_proxy(original, _SUBSCRIPTION_PROXY)
    ]
    target = (panel_paths or subscription_paths or paths)[0]
    updated_files = {
        path: _with_panel_proxy_compatibility(original)
        for path, original in originals.items()
        if path in set(panel_paths + subscription_paths)
    }
    updated_files.setdefault(target, originals[target])
    updated_files[target] = _with_required_gzip(updated_files[target])
    changed = {
        path: updated
        for path, updated in updated_files.items()
        if updated != originals[path]
    }
    if not changed:
        return None
    modes = {path: snapshots[path][1] for path in changed}
    was_running = nginx_is_running(runner, inventory)
    attempted: list[Path] = []
    try:
        for path, updated in changed.items():
            _assert_nginx_config(path, originals[path], modes[path])
            # Atomic replace can succeed even when the following fsync reports
            # an error, so record the path before attempting the write.
            attempted.append(path)
            if before_write is not None:
                before_write(path, updated)
            atomic_write_text(path, updated, mode=modes[path])
        for path, updated in changed.items():
            _assert_nginx_config(path, updated, modes[path])
        activate_nginx_config(runner, inventory, was_running=was_running)
    except BaseException as error:
        rollback_errors: list[str] = []
        for path in reversed(attempted):
            try:
                current, current_mode = _read_nginx_config(path)
                if current == originals[path] and current_mode == modes[path]:
                    continue
                if current != changed[path] or current_mode != modes[path]:
                    raise TransactionError(
                        f"файл {path} изменён внешним процессом после записи менеджера"
                    )
                atomic_write_text(path, originals[path], mode=modes[path])
                _assert_nginx_config(path, originals[path], modes[path])
            except BaseException as rollback_error:  # noqa: BLE001 - rollback must survive interrupts
                rollback_errors.append(f"восстановление файла {path}: {rollback_error}")
        if attempted and not rollback_errors:
            try:
                activate_nginx_config(
                    runner,
                    inventory,
                    was_running=was_running,
                )
            except BaseException as rollback_error:  # noqa: BLE001 - report incomplete rollback
                rollback_errors.append(f"возврат nginx к исходной конфигурации: {rollback_error}")
        if rollback_errors:
            raise TransactionError(
                "Не удалось применить совместимость nginx, rollback неполон: "
                + "; ".join(rollback_errors)
                + f". Исходная ошибка: {error}"
            ) from error
        if isinstance(error, ManagerError):
            raise
        raise TransactionError(
            f"Не удалось применить совместимость nginx; исходная конфигурация восстановлена: {error}"
        ) from error
    return target


def _compose_paths(inventory: Inventory) -> tuple[Path, Path | None]:
    compose = Path(inventory.compose_file)
    env = Path(inventory.env_file) if inventory.env_file else None
    return compose, env


def _recreate_nginx(runner: Runner, inventory: Inventory) -> None:
    component = inventory.components.get("nginx")
    if component is None:
        raise ValidationError("В inventory не найден контейнерный nginx.")
    compose, env = _compose_paths(inventory)
    runner.run(
        compose_command(
            compose,
            "up",
            "-d",
            "--no-deps",
            "--force-recreate",
            "--pull",
            "never",
            component.service,
            env_file=env,
        ),
        cwd=compose.parent,
    )


def nginx_is_running(runner: Runner, inventory: Inventory) -> bool:
    component = inventory.components.get("nginx")
    if component is None:
        result = runner.run(
            ["systemctl", "is-active", "--quiet", "nginx"],
            check=False,
        )
        if result.returncode not in {0, 3}:
            raise TransactionError(
                "Не удалось определить исходное состояние systemd-сервиса nginx."
            )
        return result.returncode == 0
    compose, env = _compose_paths(inventory)
    result = runner.run(
        compose_command(
            compose,
            "ps",
            "--services",
            "--status",
            "running",
            env_file=env,
        ),
        cwd=compose.parent,
        check=False,
    )
    if result.returncode != 0:
        raise TransactionError("Не удалось определить исходное состояние контейнера nginx.")
    return component.service in {
        line.strip() for line in result.stdout.splitlines() if line.strip()
    }


def prepare_nginx_config(runner: Runner, inventory: Inventory) -> None:
    """Recreate a stopped nginx container so its bind mounts point at new inodes."""

    component = inventory.components.get("nginx")
    if component is None:
        return
    compose, env = _compose_paths(inventory)
    runner.run(
        compose_command(
            compose,
            "create",
            "--no-deps",
            "--force-recreate",
            "--pull",
            "never",
            component.service,
            env_file=env,
        ),
        cwd=compose.parent,
    )
    _test_isolated_nginx(runner, inventory)


def _test_isolated_nginx(runner: Runner, inventory: Inventory) -> None:
    """Validate current bind sources without replacing the service container."""

    component = inventory.components.get("nginx")
    if component is None:
        raise ValidationError("В inventory не найден контейнерный nginx.")
    compose, env = _compose_paths(inventory)
    result = runner.run(
        compose_command(
            compose,
            "run",
            "--rm",
            "--no-deps",
            "--pull",
            "never",
            component.service,
            "nginx",
            "-t",
            env_file=env,
        ),
        cwd=compose.parent,
        check=False,
    )
    if result.returncode != 0:
        detail = _redact_nginx_test_error(result.stderr or result.stdout)
        suffix = f" Причина: {detail}" if detail else ""
        raise TransactionError(
            "Изолированный nginx -t завершился с ошибкой; "
            "рабочий контейнер не будет заменён."
            + suffix
        )


def activate_nginx_config(
    runner: Runner,
    inventory: Inventory,
    *,
    was_running: bool | None = None,
) -> None:
    """Load a host-side nginx config replacement and validate the loaded inode."""

    running = (
        nginx_is_running(runner, inventory)
        if was_running is None
        else was_running
    )
    if inventory.components.get("nginx") is not None:
        # A bind-mounted file remains attached to its old inode after os.replace().
        # Validate the new host path before recreating the live nginx container.
        if running:
            _test_isolated_nginx(runner, inventory)
            _recreate_nginx(runner, inventory)
            test_nginx(runner, inventory)
        else:
            prepare_nginx_config(runner, inventory)
        return
    test_nginx(runner, inventory)
    if running:
        reload_nginx(runner, inventory)


def test_nginx(runner: Runner, inventory: Inventory) -> None:
    component = inventory.components.get("nginx")
    if component:
        compose, env = _compose_paths(inventory)
        result = runner.run(
            compose_command(
                compose,
                "exec",
                "-T",
                component.service,
                "nginx",
                "-t",
                env_file=env,
            ),
            cwd=compose.parent,
            check=False,
        )
    else:
        result = runner.run(["nginx", "-t"], check=False)
    if result.returncode != 0:
        detail = _redact_nginx_test_error(result.stderr or result.stdout)
        suffix = f" Причина: {detail}" if detail else ""
        raise TransactionError(
            "nginx -t завершился с ошибкой; исходный конфиг будет возвращён."
            + suffix
        )


def _redact_nginx_test_error(value: str) -> str:
    """Keep actionable nginx syntax details without exposing config secrets."""

    selected = sanitize_external_text(value, limit=4000)
    lines = [line.strip() for line in selected.splitlines() if line.strip()][:5]
    redacted: list[str] = []
    for line in lines:
        line = _NGINX_QUOTED_VALUE.sub('"<скрыто>"', line)
        line = _NGINX_URL.sub("<URL скрыт>", line)
        line = _NGINX_WINDOWS_PATH.sub(
            lambda match: "<путь скрыт>" + (match.group("line") or ""), line
        )
        line = _NGINX_POSIX_PATH.sub(
            lambda match: "<путь скрыт>" + (match.group("line") or ""), line
        )
        line = _NGINX_LONG_TOKEN.sub("<токен скрыт>", line)
        redacted.append(line)
    return " | ".join(redacted)


def reload_nginx(runner: Runner, inventory: Inventory) -> None:
    component = inventory.components.get("nginx")
    if component:
        compose, env = _compose_paths(inventory)
        runner.run(
            compose_command(
                compose,
                "exec",
                "-T",
                component.service,
                "nginx",
                "-s",
                "reload",
                env_file=env,
            ),
            cwd=compose.parent,
        )
    else:
        runner.run(["systemctl", "reload", "nginx"])
