from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import uuid
from importlib.resources import files
from pathlib import Path

from .backup import create_backup
from .errors import ManagerError, TransactionError, ValidationError
from .models import Inventory, ManagedFile
from .nginx import activate_nginx_config, nginx_is_running
from .runner import (
    Runner,
    atomic_copy,
    atomic_write_json,
    atomic_write_text,
    ensure_within,
    read_stable_regular_file,
    sha256_file,
)
from .site_policy import upgrade_aster_policy, upgrade_morrow_policy
from .state import StateStore, utc_now

DISGUISE_TEMPLATE_COUNT = 10


def template_catalog() -> list[dict[str, str]]:
    resource = files("remnawave_manager").joinpath("data/disguises/catalog.json")
    try:
        data = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValidationError("Каталог маскировочных шаблонов повреждён.") from error
    templates = data.get("templates") if isinstance(data, dict) else None
    schema_version = data.get("schema_version") if isinstance(data, dict) else None
    if schema_version != 1 or not isinstance(templates, list):
        raise ValidationError("Каталог маскировочных шаблонов повреждён.")
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in templates:
        if not isinstance(item, dict):
            raise ValidationError("Каталог маскировочных шаблонов повреждён.")
        template_id = item.get("id")
        name = item.get("name")
        description = item.get("description")
        text_values = (name, description)
        if (
            not isinstance(template_id, str)
            or not re.fullmatch(r"[0-9]{2}-[a-z0-9]+(?:-[a-z0-9]+)*", template_id)
            or template_id in seen
            or any(
                not isinstance(value, str)
                or not value.strip()
                or value != value.strip()
                or len(value) > 256
                or any(ord(character) < 32 or ord(character) == 127 for character in value)
                for value in text_values
            )
        ):
            raise ValidationError("Каталог маскировочных шаблонов повреждён.")
        seen.add(template_id)
        normalized.append(
            {"id": template_id, "name": name, "description": description}
        )
    if len(normalized) != DISGUISE_TEMPLATE_COUNT:
        raise ValidationError(
            f"Каталог должен содержать ровно {DISGUISE_TEMPLATE_COUNT} "
            "маскировочных шаблонов."
        )
    return normalized


def _template(template_id: str) -> Path:
    available = {item["id"] for item in template_catalog()}
    if template_id not in available:
        raise ValidationError(f"Неизвестный шаблон: {template_id}")
    resource = files("remnawave_manager").joinpath(f"data/disguises/{template_id}")
    path = Path(str(resource))
    if not path.is_dir() or not (path / "index.html").is_file():
        raise ValidationError(f"Файлы шаблона {template_id} не найдены.")
    return path


def copy_template(template_id: str, target: Path) -> None:
    source = _template(template_id)
    shared = Path(str(files("remnawave_manager").joinpath("data/disguises/shared")))
    if not shared.is_dir():
        raise ValidationError("Общие файлы маскировочных шаблонов не найдены.")
    if target.exists() or target.is_symlink():
        raise ValidationError(f"Целевой каталог уже существует: {target}")
    target.mkdir(mode=0o755)
    file_count = 0
    total_size = 0
    try:
        resources = [
            (item, item.relative_to(source)) for item in sorted(source.rglob("*"), key=str)
        ]
        resources.extend(
            (item, Path("shared") / item.relative_to(shared))
            for item in sorted(shared.rglob("*"), key=str)
        )
        for item, relative in resources:
            if item.is_symlink():
                raise ValidationError("Шаблон не может содержать символические ссылки.")
            destination = target / relative
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True, mode=0o755)
                continue
            if not item.is_file():
                raise ValidationError(f"Неподдерживаемый элемент шаблона: {item}")
            file_count += 1
            if file_count > 5000:
                raise ValidationError("Шаблон маскировочного сайта слишком большой.")
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            atomic_copy(item, destination, mode=0o644)
            total_size += destination.stat().st_size
            if total_size > 100 * 1024 * 1024:
                raise ValidationError("Шаблон маскировочного сайта слишком большой.")
        atomic_write_json(
            target / ".rwm-template.json",
            {
                "schema_version": 1,
                "template": template_id,
                "applied_at": utc_now(),
            },
            mode=0o644,
        )
    except BaseException:
        shutil.rmtree(target, ignore_errors=True)
        raise


def _target(inventory: Inventory) -> Path:
    if len(inventory.site_dirs) != 1:
        raise ValidationError(
            "Не удалось однозначно определить bind mount сайта. Проверьте nginx volumes и повторите adoption."
        )
    declared = Path(inventory.site_dirs[0])
    if declared.is_symlink():
        raise ValidationError(
            f"Целевой каталог сайта заменён символической ссылкой: {declared}"
        )
    if not declared.is_dir():
        raise ValidationError(f"Целевой каталог сайта не найден: {declared}")
    target = declared.resolve()
    allowed_roots = [Path(inventory.install_dir).resolve(), Path("/var/www").resolve(), Path("/srv/www").resolve()]
    if target in allowed_roots or not any(root in target.parents for root in allowed_roots):
        raise ValidationError(f"Небезопасный целевой каталог сайта: {target}")
    return target


def _assert_trusted_site_directories(target: Path) -> None:
    if os.name != "posix":
        return
    effective_uid = os.geteuid()
    trusted_uids = {0, effective_uid}
    for candidate in (target, *target.parents):
        try:
            info = candidate.lstat()
        except OSError as error:
            raise ValidationError(
                f"Не удалось проверить каталог сайта {candidate}: {error}"
            ) from error
        mode = stat.S_IMODE(info.st_mode)
        sticky_root_ancestor = (
            candidate != target
            and info.st_uid == 0
            and bool(mode & stat.S_ISVTX)
        )
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in trusted_uids
            or (mode & 0o022 and not sticky_root_ancestor)
        ):
            raise ValidationError(
                f"Каталог сайта или его родитель небезопасен для атомарной замены: {candidate}"
            )


def _validate_site_inventory(inventory: Inventory, target: Path) -> None:
    expected: dict[Path, str] = {}
    for item in inventory.managed_files:
        path = Path(item.path)
        resolved = path.resolve()
        if resolved == target or target in resolved.parents:
            expected[resolved] = item.sha256

    current: set[Path] = set()
    for path in sorted(target.rglob("*"), key=str):
        try:
            info = path.lstat()
        except OSError as error:
            raise ValidationError(f"Не удалось проверить файл сайта {path}: {error}") from error
        if stat.S_ISDIR(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValidationError(
                f"Сайт содержит symlink, hardlink или неподдерживаемый объект: {path}"
            )
        resolved = path.resolve()
        current.add(resolved)
        recorded = expected.get(resolved)
        if recorded is None:
            raise ValidationError(
                f"Сайт содержит неучтённый файл {path}; выполните rwm adopt перед заменой."
            )
        if sha256_file(path) != recorded:
            raise ValidationError(
                f"Файл сайта изменён после adoption: {path}. Выполните rwm adopt после проверки изменений."
            )
    missing = sorted(expected.keys() - current, key=str)
    if missing:
        raise ValidationError(
            "В inventory остались отсутствующие файлы сайта: "
            + ", ".join(str(path) for path in missing[:10])
        )


def _aster_policy_changes(
    inventory: Inventory, *, morrow: bool = False
) -> list[tuple[Path, str, str, int]]:
    """Prepare only known CSP upgrades, and only for unchanged managed nginx files."""
    changes: list[tuple[Path, str, str, int]] = []
    nginx_roots = [Path(value).resolve() for value in inventory.nginx_files]
    install_root = Path(inventory.install_dir).resolve()
    for item in inventory.managed_files:
        path = Path(item.path)
        resolved = path.resolve()
        if not any(resolved == root or root in resolved.parents for root in nginx_roots):
            continue
        if path.suffix != ".conf":
            continue
        if install_root not in resolved.parents or path.is_symlink():
            raise ValidationError(f"Небезопасный путь конфигурации nginx: {path}")
        _assert_trusted_site_directories(path.parent)
        snapshot = read_stable_regular_file(
            path, max_size=4 * 1024 * 1024, label="Конфигурация nginx"
        )
        if hashlib.sha256(snapshot.data).hexdigest() != item.sha256:
            raise ValidationError(f"Конфигурация nginx изменена после adoption: {path}")
        # Decode bytes directly so rollback preserves CRLF as well as LF.
        old = snapshot.data.decode("utf-8")
        new = upgrade_morrow_policy(old) if morrow else upgrade_aster_policy(old)
        if old != new:
            changes.append((path, old, new, snapshot.mode))
    return changes


def apply_template(
    runner: Runner,
    store: StateStore,
    template_id: str,
) -> Path:
    inventory = store.load_inventory()
    if inventory.role != "node":
        raise ValidationError("Выбираемые сайты-заглушки применяются на node-серверах.")
    nginx = inventory.components.get("nginx")
    if nginx is None:
        raise ValidationError("В инвентаризации не найден nginx-сервис.")
    target = _target(inventory)
    _assert_trusted_site_directories(target)
    _validate_site_inventory(inventory, target)
    _template(template_id)
    policy_changes = (
        _aster_policy_changes(inventory, morrow=template_id == "03-morrow-coffee")
        if template_id in {"02-aster-observatory", "03-morrow-coffee"}
        else []
    )
    create_backup(runner, store, reason=f"pre-disguise-{template_id}", retention=None)
    parent = target.parent
    staging = parent / f".{target.name}.rwm-new-{uuid.uuid4().hex}"
    previous = parent / f".{target.name}.rwm-old-{uuid.uuid4().hex}"
    ensure_within(staging, parent)
    ensure_within(previous, parent)
    inventory_snapshot = Inventory.from_dict(inventory.to_dict())
    was_running = False
    previous_created = False
    replacement_installed = False
    inventory_may_have_changed = False
    changed_policies: list[tuple[Path, str, str, int]] = []
    try:
        copy_template(template_id, staging)
        # Backup can take long enough for an operator or another process to edit
        # the site. Revalidate the complete trust boundary immediately before
        # the destructive directory rename so such changes are never discarded.
        _assert_trusted_site_directories(target)
        _validate_site_inventory(inventory, target)
        current_policy_changes = (
            _aster_policy_changes(inventory, morrow=template_id == "03-morrow-coffee")
            if template_id in {"02-aster-observatory", "03-morrow-coffee"}
            else []
        )
        if policy_changes != current_policy_changes:
            raise ValidationError("Конфигурация nginx изменилась во время подготовки шаблона.")
        was_running = nginx_is_running(runner, inventory)
        os.replace(target, previous)
        previous_created = True
        os.replace(staging, target)
        replacement_installed = True
        for path, old, new, mode in policy_changes:
            changed_policies.append((path, old, new, mode))
            atomic_write_text(path, new, mode=mode)
        activate_nginx_config(runner, inventory, was_running=was_running)
        inventory_may_have_changed = True
        for item in inventory.managed_files:
            if any(Path(item.path) == change[0] for change in changed_policies):
                item.sha256 = sha256_file(Path(item.path))
        _refresh_inventory(store, inventory, target)
    except BaseException as error:
        rollback_errors: list[str] = []
        for path, old, _new, mode in reversed(changed_policies):
            try:
                atomic_write_text(path, old, mode=mode)
            except BaseException as rollback_error:
                rollback_errors.append(f"восстановление CSP nginx: {rollback_error}")
        failed: Path | None = None
        if previous_created:
            if replacement_installed and (target.exists() or target.is_symlink()):
                failed = parent / f".{target.name}.rwm-failed-{uuid.uuid4().hex}"
                ensure_within(failed, parent)
                try:
                    os.replace(target, failed)
                except BaseException as rollback_error:  # noqa: BLE001 - rollback must survive interrupts
                    rollback_errors.append(
                        f"изоляция неудачного шаблона: {rollback_error}"
                    )
            if not target.exists() and not target.is_symlink():
                try:
                    os.replace(previous, target)
                except BaseException as rollback_error:  # noqa: BLE001 - rollback must survive interrupts
                    rollback_errors.append(
                        f"восстановление прежнего сайта: {rollback_error}"
                    )
            else:
                rollback_errors.append(
                    "восстановление прежнего сайта: целевой путь занят"
                )
            if target.is_dir() and not target.is_symlink():
                try:
                    activate_nginx_config(
                        runner,
                        inventory_snapshot,
                        was_running=was_running,
                    )
                except BaseException as rollback_error:  # noqa: BLE001 - rollback must survive interrupts
                    rollback_errors.append(
                        f"перезапуск nginx на прежнем сайте: {rollback_error}"
                    )
        if inventory_may_have_changed:
            try:
                store.save_inventory(inventory_snapshot)
            except BaseException as rollback_error:  # noqa: BLE001 - rollback must survive interrupts
                rollback_errors.append(
                    f"восстановление inventory: {rollback_error}"
                )
        if failed is not None and failed.exists():
            shutil.rmtree(failed, ignore_errors=True)
        if rollback_errors:
            raise TransactionError(
                "Замена сайта-заглушки завершилась ошибкой, rollback неполон: "
                + "; ".join(rollback_errors)
                + f". Исходная ошибка: {error}"
            ) from error
        if isinstance(error, ManagerError):
            raise
        raise TransactionError(
            f"Замена сайта-заглушки не выполнена; прежний сайт восстановлен: {error}"
        ) from error
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
    ensure_within(previous, parent)
    try:
        shutil.rmtree(previous)
    except OSError as error:
        raise TransactionError(
            f"Новый шаблон работает, но не удалось удалить временную копию {previous}: {error}"
        ) from error
    return target


def _refresh_inventory(store: StateStore, inventory: Inventory, target: Path) -> None:
    target = target.resolve()

    def belongs_to_site(path: str) -> bool:
        resolved = Path(path).resolve()
        return resolved == target or target in resolved.parents

    inventory.managed_files = [
        item
        for item in inventory.managed_files
        if not belongs_to_site(item.path)
    ]
    for path in sorted(target.rglob("*"), key=str):
        if path.is_symlink():
            raise TransactionError(f"После установки шаблона обнаружена символическая ссылка: {path}")
        if path.is_file():
            inventory.managed_files.append(
                ManagedFile(path=str(path.resolve()), sha256=sha256_file(path), kind="site")
            )
    store.save_inventory(inventory)
