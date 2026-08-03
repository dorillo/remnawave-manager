from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from .adopt import adopt
from .backup import BackupResult, create_backup, restore_backup
from .compat import require_supported_source
from .compose import ComposeDocument, compose_command, validate_rendered_compose
from .envfile import EnvDocument
from .errors import TransactionError, ValidationError
from .health import (
    check_panel_http,
    check_subscription_api_scopes,
    check_subscription_http,
    wait_container,
    wait_for_paths,
    wait_node_runtime,
)
from .integrity import configuration_drift, snapshot_hashes
from .journal import TransactionJournal
from .models import Inventory
from .nginx import ensure_gzip, test_nginx
from .registry import REGISTRIES, pull_verified
from .runner import Runner, sha256_file
from .state import StateStore

_MAX_NODE_CONFIG_SIZE = 16 * 1024 * 1024
_PRIVATE_FILE_KINDS = frozenset({"compose", "env", "nginx", "secret"})


def _require_clean_inventory(inventory: Inventory) -> None:
    drift = configuration_drift(inventory)
    if drift:
        raise ValidationError(
            "Конфигурация изменилась после adoption:\n- "
            + "\n- ".join(drift)
            + "\nПроверьте изменения и выполните rwm adopt --path "
            + inventory.install_dir
        )
    if os.name == "posix":
        _require_private_permissions(inventory, expected_uid=os.geteuid())


def _require_private_permissions(
    inventory: Inventory,
    *,
    expected_uid: int,
) -> None:
    unsafe_permissions: list[str] = []
    for item in inventory.managed_files:
        if item.kind not in _PRIVATE_FILE_KINDS:
            continue
        path = Path(item.path)
        try:
            info = path.lstat()
        except OSError as error:
            unsafe_permissions.append(f"{path}: не удалось проверить: {error}")
            continue
        mode = stat.S_IMODE(info.st_mode)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            unsafe_permissions.append(f"{path}: небезопасный тип или hardlink")
        elif info.st_uid != expected_uid or mode != 0o600:
            unsafe_permissions.append(
                f"{path}: UID {info.st_uid}, права {mode:o}; "
                f"ожидаются UID {expected_uid} и 600"
            )
    if unsafe_permissions:
        raise ValidationError(
            "Update запрещён: приватные managed-файлы имеют небезопасные права:\n- "
            + "\n- ".join(unsafe_permissions)
            + "\nВыполните sudo rwm diagnose --repair-permissions, затем "
            "sudo rwm diagnose и повторите update."
        )


def _managed_key(path: Path) -> str:
    return str(path.absolute())


def _unexpected_rollback_drift(
    inventory: Inventory,
    manager_hashes: dict[str, str],
) -> list[str]:
    unexpected: list[str] = []
    managed_keys = {_managed_key(Path(item.path)) for item in inventory.managed_files}
    unknown_targets = sorted(set(manager_hashes) - managed_keys)
    if unknown_targets:
        unexpected.extend(f"неизвестная manager-цель: {path}" for path in unknown_targets)
    for item in inventory.managed_files:
        path = Path(item.path)
        key = _managed_key(path)
        allowed = {item.sha256}
        if key in manager_hashes:
            allowed.add(manager_hashes[key])
        try:
            current = sha256_file(path)
        except ValidationError as error:
            unexpected.append(f"{path}: {error}")
            continue
        if current not in allowed:
            unexpected.append(str(path))
    return unexpected


def _settings(store: StateStore) -> tuple[str, int]:
    settings = store.load_settings()
    registry = settings.get("registry", "docker-hub")
    if not isinstance(registry, str) or registry not in REGISTRIES:
        raise ValidationError("В настройках менеджера указан неизвестный Docker Registry.")
    retention = settings.get("backup_retention", 10)
    if (
        isinstance(retention, bool)
        or not isinstance(retention, int)
        or not 1 <= retention <= 1000
    ):
        raise ValidationError(
            "Число хранимых backup в настройках должно быть целым числом от 1 до 1000."
        )
    return registry, retention


def _running_component_services(runner: Runner, inventory: Inventory) -> set[str]:
    compose_path = Path(inventory.compose_file)
    env_path = Path(inventory.env_file) if inventory.env_file else None
    result = runner.run(
        compose_command(
            compose_path,
            "ps",
            "--services",
            "--status",
            "running",
            env_file=env_path,
        ),
        cwd=compose_path.parent,
    )
    known = {component.service for component in inventory.components.values()}
    return {
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() in known
    }


def _reconcile_running_services(
    runner: Runner,
    inventory: Inventory,
    expected: set[str],
    *,
    legacy_subscription: bool = False,
) -> None:
    """Return managed services to the state captured before an update."""

    compose_path = Path(inventory.compose_file)
    env_path = Path(inventory.env_file) if inventory.env_file else None
    known = {component.service for component in inventory.components.values()}
    if not expected <= known:
        raise TransactionError("Снимок состояния сервисов содержит неизвестные Compose-сервисы.")

    current = _running_component_services(runner, inventory)
    extra = current - expected
    stop_order = ("subscription", "panel", "node", "nginx", "cache", "database")
    ordered_extra = [
        inventory.components[name].service
        for name in stop_order
        if name in inventory.components and inventory.components[name].service in extra
    ]
    ordered_extra.extend(sorted(extra - set(ordered_extra)))
    if ordered_extra:
        runner.run(
            compose_command(
                compose_path,
                "stop",
                *ordered_extra,
                env_file=env_path,
            ),
            cwd=compose_path.parent,
        )

    current -= extra
    missing = expected - current
    start_order = ("database", "cache", "panel", "subscription", "node", "nginx")
    ordered_names = [
        name
        for name in start_order
        if name in inventory.components and inventory.components[name].service in missing
    ]
    remaining = missing - {
        inventory.components[name].service for name in ordered_names
    }
    for name in ordered_names:
        component = inventory.components[name]
        runner.run(
            compose_command(
                compose_path,
                "up",
                "-d",
                "--no-deps",
                component.service,
                env_file=env_path,
            ),
            cwd=compose_path.parent,
        )
        wait_container(
            runner,
            component,
            timeout=600 if name == "panel" else 300,
            require_health=name in {"database", "panel"},
        )
        if name == "panel":
            check_panel_http(runner, component)
        elif name == "subscription":
            check_subscription_http(
                runner, component, legacy=legacy_subscription
            )
        elif name == "node":
            wait_node_runtime(runner, inventory)
            wait_for_paths(inventory.xhttp_sockets)
        elif name == "nginx":
            test_nginx(runner, inventory)
    for service in sorted(remaining):
        runner.run(
            compose_command(
                compose_path,
                "up",
                "-d",
                "--no-deps",
                service,
                env_file=env_path,
            ),
            cwd=compose_path.parent,
        )

    final = _running_component_services(runner, inventory)
    if final != expected:
        raise TransactionError(
            "Не удалось вернуть исходное состояние Compose-сервисов: "
            f"ожидалось {sorted(expected)}, запущено {sorted(final)}."
        )


def update_panel_stack(
    runner: Runner,
    store: StateStore,
    *,
    accept_unknown_source: bool = False,
) -> BackupResult:
    inventory = store.load_inventory()
    if inventory.role != "panel":
        raise ValidationError("Эта команда предназначена для panel-сервера.")
    for required in ("panel", "subscription", "database"):
        if required not in inventory.components:
            raise ValidationError(f"В инвентаризации отсутствует компонент {required}.")
    if not inventory.env_file:
        raise ValidationError("Для Panel не найден .env.")
    _require_clean_inventory(inventory)
    require_supported_source(
        runner,
        "panel",
        inventory.components["panel"],
        accept_unknown=accept_unknown_source,
    )
    subscription_source_version = require_supported_source(
        runner,
        "subscription",
        inventory.components["subscription"],
        accept_unknown=accept_unknown_source,
    )
    require_supported_source(
        runner,
        "database",
        inventory.components["database"],
        accept_unknown=accept_unknown_source,
    )
    TransactionJournal.ensure_available(store)
    registry, retention = _settings(store)
    if registry != "docker-hub":
        raise ValidationError(
            "Для Panel stack нужен проверенный образ Subscription Page из Docker Hub. "
            "Выполните rwm registry select docker-hub."
        )
    compose_path = Path(inventory.compose_file)
    env_path = Path(inventory.env_file)
    compose = ComposeDocument.load(compose_path)
    env = EnvDocument.load(env_path)
    env.migrate_panel_v3()
    env.validate_panel_v3()

    # Pull and validate every target before opening the maintenance window.
    panel_image = pull_verified(runner, "panel", registry)
    subscription_image = pull_verified(runner, "subscription", "docker-hub")
    database_image = pull_verified(runner, "database", "docker-hub")
    compose.set_image(inventory.components["panel"].service, panel_image)
    compose.set_image(inventory.components["subscription"].service, subscription_image)
    compose.set_image(inventory.components["database"].service, database_image)
    validate_rendered_compose(runner, compose_path, compose.render(), env_path)
    _require_clean_inventory(inventory)
    running_before = _running_component_services(runner, inventory)

    journal = TransactionJournal(store, "panel-update", None)
    backup: BackupResult | None = None
    service_state_touched = False
    persistent_mutation_started = False
    manager_hashes: dict[str, str] = {}

    def mark_write(path: Path, payload: str) -> None:
        nonlocal persistent_mutation_started
        persistent_mutation_started = True
        manager_hashes[_managed_key(path)] = hashlib.sha256(
            payload.encode("utf-8")
        ).hexdigest()

    try:
        journal.set_running_services(running_before)
        services = [inventory.components["subscription"].service, inventory.components["panel"].service]
        journal.phase("stopping-applications")
        # Mark this before invoking Compose because stop may succeed only partly.
        service_state_touched = True
        runner.run(
            compose_command(compose_path, "stop", *services, env_file=env_path),
            cwd=compose_path.parent,
        )
        still_running = _running_component_services(runner, inventory) & set(services)
        if still_running:
            raise TransactionError(
                "Не удалось остановить write-path Panel перед backup: "
                + ", ".join(sorted(still_running))
            )

        journal.phase("creating-backup")
        backup = create_backup(
            runner,
            store,
            reason="pre-panel-update",
            retention=retention,
        )
        journal.set_backup(backup.path)
        for name in ("panel", "subscription", "database"):
            require_supported_source(
                runner,
                name,
                inventory.components[name],
                accept_unknown=accept_unknown_source,
            )
        reopened = _running_component_services(runner, inventory) & set(services)
        if reopened:
            raise TransactionError(
                "Write-path Panel снова запустился во время backup; "
                "транзакционный архив не будет использован: "
                + ", ".join(sorted(reopened))
            )
        # The dump and archive can take a long time. Refuse to overwrite an
        # operator edit made after the earlier preflight snapshot.
        _require_clean_inventory(inventory)

        journal.phase("migrating-nginx")
        # ensure_gzip has its own compensation, but a failed compensation still
        # needs the verified transaction backup.
        ensure_gzip(runner, inventory, before_write=mark_write)

        journal.phase("migrating-configuration")
        env.save(
            env_path,
            before_write=lambda: mark_write(env_path, env.render()),
        )
        compose.save(
            compose_path,
            before_write=lambda: mark_write(compose_path, compose.render()),
        )
        runner.run(
            compose_command(compose_path, "config", "-q", env_file=env_path),
            cwd=compose_path.parent,
        )

        database = inventory.components["database"]
        journal.phase("recreating-database")
        runner.run(
            compose_command(
                compose_path,
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                database.service,
                env_file=env_path,
            ),
            cwd=compose_path.parent,
        )
        wait_container(runner, database, timeout=300, require_health=True)

        panel = inventory.components["panel"]
        journal.phase("starting-panel")
        runner.run(
            compose_command(
                compose_path,
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                panel.service,
                env_file=env_path,
            ),
            cwd=compose_path.parent,
        )
        wait_container(runner, panel, timeout=600, require_health=True)
        check_panel_http(runner, panel)

        subscription = inventory.components["subscription"]
        journal.phase("starting-subscription")
        runner.run(
            compose_command(
                compose_path,
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                subscription.service,
                env_file=env_path,
            ),
            cwd=compose_path.parent,
        )
        wait_container(runner, subscription, timeout=300, require_health=True)
        check_subscription_http(runner, subscription)
        check_subscription_api_scopes(runner, panel, subscription)
        test_nginx(runner, inventory)
        journal.phase("restoring-service-state")
        _reconcile_running_services(
            runner,
            inventory,
            running_before,
            legacy_subscription=subscription_source_version == "7.2.6",
        )
        journal.phase("committed")
        adopt(
            runner,
            store,
            directory=Path(inventory.install_dir),
            requested_role="panel",
            allow_active_transaction=True,
        )
        journal.complete()
        return backup
    except BaseException as error:
        rollback_errors: list[str] = []
        try:
            journal.phase("rolling-back")
        except BaseException as rollback_error:  # noqa: BLE001 - continue compensation
            rollback_errors.append(f"обновление journal: {rollback_error}")

        if persistent_mutation_started:
            if backup is None:
                rollback_errors.append(
                    "persistent mutation началась без проверенного backup"
                )
            else:
                unexpected = _unexpected_rollback_drift(inventory, manager_hashes)
                if unexpected:
                    rollback_errors.append(
                        "автоматический restore пропущен из-за внешнего изменения managed-файлов: "
                        + ", ".join(unexpected)
                    )
                else:
                    try:
                        restore_backup(
                            runner,
                            store,
                            backup.path,
                            restore_database=True,
                            clear_recovery_journal=False,
                        )
                    except BaseException as rollback_error:  # noqa: BLE001 - report all compensation failures
                        rollback_errors.append(f"восстановление backup: {rollback_error}")

        if service_state_touched:
            try:
                _reconcile_running_services(
                    runner,
                    inventory,
                    running_before,
                    legacy_subscription=subscription_source_version == "7.2.6",
                )
            except BaseException as rollback_error:  # noqa: BLE001 - report all compensation failures
                rollback_errors.append(f"возврат состояния сервисов: {rollback_error}")

        if not rollback_errors:
            try:
                journal.complete()
            except BaseException as rollback_error:  # noqa: BLE001 - journal must remain visible
                rollback_errors.append(f"завершение journal: {rollback_error}")

        if rollback_errors:
            backup_detail = (
                f"\nПроверенный backup: {backup.path}"
                if backup is not None
                else "\nПроверенный backup ещё не был создан."
            )
            raise TransactionError(
                f"Обновление не удалось: {error}\nАвтоматический откат также не завершён: "
                + "; ".join(rollback_errors)
                + backup_detail
            ) from error
        if not persistent_mutation_started:
            raise TransactionError(
                "Обновление остановлено до изменения конфигурации; "
                f"исходное состояние сервисов восстановлено: {error}"
            ) from error
        if backup is None:
            raise TransactionError(
                "Обновление завершило rollback без привязанного backup; "
                "journal сохранён для ручной проверки."
            ) from error
        raise TransactionError(
            f"Обновление не удалось, предыдущая версия восстановлена из {backup.path}: {error}"
        ) from error


def _dump_node_config(runner: Runner, inventory: Inventory) -> tuple[dict[str, Any], Path]:
    component = inventory.components["node"]
    container = component.container or component.service
    result = runner.run(
        ["docker", "exec", container, "cli", "--dump-config-raw"],
        sensitive=True,
    )
    if len(result.stdout) > _MAX_NODE_CONFIG_SIZE:
        raise ValidationError(
            "Xray JSON от Node превышает допустимый размер; обновление остановлено."
        )
    try:
        config = json.loads(result.stdout)
    except (json.JSONDecodeError, RecursionError) as error:
        raise ValidationError("Node вернула некорректный Xray JSON; обновление остановлено.") from error
    if not isinstance(config, dict):
        raise ValidationError(
            "Node вернула Xray JSON, который не является объектом; обновление остановлено."
        )
    descriptor, name = tempfile.mkstemp(prefix="rwm-xray-", suffix=".json")
    path = Path(name)
    primary_error: BaseException | None = None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = -1
            json.dump(config, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o600)
    except BaseException as error:  # noqa: BLE001 - remove a partial sensitive file
        primary_error = error
    if primary_error is not None:
        cleanup_errors: list[str] = []
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except BaseException as cleanup_error:  # noqa: BLE001 - continue cleanup
                cleanup_errors.append(f"закрытие descriptor: {cleanup_error}")
        try:
            path.unlink(missing_ok=True)
            if path.exists() or path.is_symlink():
                raise OSError("временный файл остался на диске")
        except BaseException as cleanup_error:  # noqa: BLE001 - report leaked sensitive file
            cleanup_errors.append(f"удаление файла: {cleanup_error}")
        if cleanup_errors:
            raise TransactionError(
                f"Не удалось создать временный Xray-конфиг и полностью очистить {path}: "
                + "; ".join(cleanup_errors)
                + f". Исходная ошибка: {primary_error}"
            ) from primary_error
        raise primary_error
    return config, path


def _reality_without_min_version(config: dict[str, Any]) -> list[str]:
    risky: list[str] = []
    for inbound in config.get("inbounds", []) or []:
        if not isinstance(inbound, dict):
            continue
        stream = inbound.get("streamSettings") or {}
        if not isinstance(stream, dict):
            continue
        reality = stream.get("realitySettings")
        if (
            str(stream.get("security", "")).lower() == "reality"
            and isinstance(reality, dict)
            and (
                not isinstance(reality.get("minClientVer"), str)
                or not str(reality.get("minClientVer")).strip()
            )
        ):
            risky.append(str(inbound.get("tag") or "<без tag>"))
    return risky


def _preflight_node_config(
    runner: Runner,
    inventory: Inventory,
    image: str,
    *,
    accept_reality_client_risk: bool,
) -> None:
    config, path = _dump_node_config(runner, inventory)
    primary_error: BaseException | None = None
    try:
        risky = _reality_without_min_version(config)
        if risky and not accept_reality_client_risk:
            raise ValidationError(
                "Reality inbound без явного minClientVer: "
                + ", ".join(risky)
                + ". Node 3.0.0 по умолчанию требует клиент 26.3.27. "
                "Проверьте версии клиентов и повторите с --accept-reality-client-risk. "
                "Менеджер не будет автоматически ставить небезопасное 0.0.0."
            )
        node = inventory.components["node"]
        _validate_xray_image(
            runner,
            image,
            path,
            node.container or node.service,
        )
    except BaseException as error:  # noqa: BLE001 - cleanup must preserve the preflight failure
        primary_error = error
    try:
        path.unlink(missing_ok=True)
        if path.exists() or path.is_symlink():
            raise OSError("временный файл остался на диске")
    except BaseException as cleanup_error:
        if primary_error is not None:
            raise TransactionError(
                f"Node preflight завершился ошибкой, а временный Xray-конфиг {path} "
                f"удалить не удалось: {cleanup_error}. Исходная ошибка: {primary_error}"
            ) from primary_error
        raise TransactionError(
            f"Node preflight пройден, но временный Xray-конфиг {path} удалить не удалось: "
            f"{cleanup_error}. Обновление остановлено до изменения Compose."
        ) from cleanup_error
    if primary_error is not None:
        raise primary_error


def _validate_xray_image(
    runner: Runner,
    image: str,
    config_path: Path,
    runtime_container: str,
) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", runtime_container):
        raise ValidationError("Не удалось безопасно определить контейнер Node для Xray preflight.")
    result = runner.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--volumes-from",
            f"{runtime_container}:ro",
            "--volume",
            f"{config_path}:/tmp/config.json:ro",
            "--entrypoint",
            "rw-core",
            image,
            "run",
            "-test",
            "-config",
            "/tmp/config.json",  # noqa: S108, RUF100 - read-only bind path inside the disposable container
        ],
        check=False,
        sensitive=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise ValidationError(
            "Текущий Xray-конфиг не прошёл тест новым core 26.7.28. "
            "Образ Node не переключён."
        )


def _existing_paths(paths: list[str]) -> list[str]:
    return [path for path in paths if Path(path).exists()]


def _existing_warp_interfaces(names: list[str] | set[str]) -> set[str]:
    return {name for name in names if Path("/sys/class/net", name).exists()}


def update_node(
    runner: Runner,
    store: StateStore,
    *,
    accept_reality_client_risk: bool = False,
    accept_unknown_source: bool = False,
) -> BackupResult:
    inventory = store.load_inventory()
    if inventory.role != "node" or "node" not in inventory.components:
        raise ValidationError("Эта команда предназначена для отдельного node-сервера.")
    _require_clean_inventory(inventory)
    require_supported_source(
        runner,
        "node",
        inventory.components["node"],
        accept_unknown=accept_unknown_source,
    )
    TransactionJournal.ensure_available(store)
    registry, retention = _settings(store)
    compose_path = Path(inventory.compose_file)
    env_path = Path(inventory.env_file) if inventory.env_file else None
    backup = create_backup(runner, store, reason="pre-node-update", retention=retention)
    journal = TransactionJournal(store, "node-update", backup.path)
    mutation_started = False
    manager_hashes: dict[str, str] = {}
    running_before: set[str] | None = None

    def mark_compose_write() -> None:
        nonlocal mutation_started
        mutation_started = True
        manager_hashes[_managed_key(compose_path)] = hashlib.sha256(
            compose.render().encode("utf-8")
        ).hexdigest()

    try:
        journal.phase("pulling-image")
        image = pull_verified(runner, "node", registry)
        _preflight_node_config(
            runner,
            inventory,
            image,
            accept_reality_client_risk=accept_reality_client_risk,
        )

        _require_clean_inventory(inventory)
        running_before = _running_component_services(runner, inventory)
        journal.set_running_services(running_before)
        unchanged_before = snapshot_hashes(inventory, ignore_kinds={"compose"})
        sockets_before = _existing_paths(inventory.xhttp_sockets)
        warp_before = _existing_warp_interfaces(inventory.warp_interfaces)
        node = inventory.components["node"]
        compose = ComposeDocument.load(compose_path)
        compose.set_image(inventory.components["node"].service, image)
        compose.replace_volume_target(
            inventory.components["node"].service,
            "/var/log/remnanode",
            "/var/log/xray",
        )
        validate_rendered_compose(runner, compose_path, compose.render(), env_path)

        journal.phase("recreating-node")
        compose.save(compose_path, before_write=mark_compose_write)
        runner.run(
            compose_command(
                compose_path,
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                node.service,
                env_file=env_path,
            ),
            cwd=compose_path.parent,
        )
        wait_container(runner, node, timeout=300)
        wait_node_runtime(runner, inventory)
        wait_for_paths(sockets_before)

        unchanged_after = snapshot_hashes(inventory, ignore_kinds={"compose"})
        changed = [path for path, digest in unchanged_before.items() if unchanged_after.get(path) != digest]
        if changed:
            raise TransactionError("Node update изменила защищённые файлы: " + ", ".join(changed))
        missing_warp = sorted(warp_before - _existing_warp_interfaces(warp_before))
        if missing_warp:
            raise TransactionError("После Node update пропал WARP-интерфейс: " + ", ".join(missing_warp))

        journal.phase("restoring-service-state")
        _reconcile_running_services(runner, inventory, running_before)
        journal.phase("committed")
        adopt(
            runner,
            store,
            directory=Path(inventory.install_dir),
            requested_role="node",
            allow_active_transaction=True,
        )
        journal.complete()
        return backup
    except BaseException as error:
        if not mutation_started:
            try:
                journal.complete()
            except BaseException as journal_error:  # noqa: BLE001 - preserve both failures
                raise TransactionError(
                    f"Node update остановлена до изменений: {error}\n"
                    f"Не удалось безопасно завершить journal: {journal_error}"
                ) from error
            raise

        rollback_errors: list[str] = []
        try:
            journal.phase("rolling-back")
        except BaseException as rollback_error:  # noqa: BLE001 - continue compensation
            rollback_errors.append(f"обновление journal: {rollback_error}")
        unexpected = _unexpected_rollback_drift(inventory, manager_hashes)
        if unexpected:
            rollback_errors.append(
                "автоматический restore пропущен из-за внешнего изменения managed-файлов: "
                + ", ".join(unexpected)
            )
        else:
            try:
                restore_backup(
                    runner,
                    store,
                    backup.path,
                    restore_database=False,
                    clear_recovery_journal=False,
                )
            except BaseException as rollback_error:  # noqa: BLE001 - continue compensation
                rollback_errors.append(f"восстановление backup: {rollback_error}")
        if running_before is not None:
            try:
                _reconcile_running_services(runner, inventory, running_before)
            except BaseException as rollback_error:  # noqa: BLE001 - report all compensation failures
                rollback_errors.append(f"возврат состояния сервисов: {rollback_error}")
        else:
            rollback_errors.append("не найден исходный снимок состояния сервисов")

        if not rollback_errors:
            try:
                journal.complete()
            except BaseException as rollback_error:  # noqa: BLE001 - journal must remain visible
                rollback_errors.append(f"завершение journal: {rollback_error}")

        if rollback_errors:
            raise TransactionError(
                f"Node update не удалась: {error}\nАвтоматический откат также не завершён: "
                + "; ".join(rollback_errors)
                + f"\nBackup: {backup.path}"
            ) from error
        raise TransactionError(
            f"Node update не удалась, предыдущая конфигурация восстановлена из {backup.path}: {error}"
        ) from error
