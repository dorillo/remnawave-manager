from __future__ import annotations

from pathlib import Path

from .errors import ValidationError
from .models import Inventory
from .runner import sha256_file


def configuration_drift(inventory: Inventory, *, ignore_kinds: set[str] | None = None) -> list[str]:
    ignored = ignore_kinds or set()
    drift: list[str] = []
    seen: set[Path] = set()
    for item in inventory.managed_files:
        if item.kind in ignored:
            continue
        path = Path(item.path)
        if not path.is_absolute():
            drift.append(f"относительный managed-путь: {path}")
            continue
        resolved = path.resolve()
        if resolved in seen:
            drift.append(f"повторяется в инвентаризации: {resolved}")
            continue
        seen.add(resolved)
        if path.is_symlink():
            drift.append(f"символьная ссылка вместо managed-файла: {path}")
        elif not path.is_file():
            drift.append(f"отсутствует: {path}")
        else:
            try:
                if sha256_file(path) != item.sha256:
                    drift.append(f"изменён после инвентаризации: {path}")
            except (OSError, ValidationError) as error:
                drift.append(f"не удалось безопасно прочитать: {path}: {error}")
    return drift


def snapshot_hashes(inventory: Inventory, *, ignore_kinds: set[str] | None = None) -> dict[str, str]:
    ignored = ignore_kinds or set()
    result: dict[str, str] = {}
    seen: set[Path] = set()
    for item in inventory.managed_files:
        if item.kind in ignored:
            continue
        path = Path(item.path)
        if not path.is_absolute():
            raise ValidationError(f"Managed-файл содержит относительный путь: {path}.")
        if path.is_symlink():
            raise ValidationError(f"Managed-файл является символической ссылкой: {path}.")
        if not path.is_file():
            raise ValidationError(f"Managed-файл отсутствует: {path}.")
        resolved = path.resolve()
        if resolved in seen:
            raise ValidationError(
                f"Managed-файл повторяется в инвентаризации: {resolved}."
            )
        seen.add(resolved)
        result[item.path] = sha256_file(path)
    return result
