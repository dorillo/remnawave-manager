from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import shlex
import shutil
import stat
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .errors import TransactionError, ValidationError
from .runner import (
    Runner,
    atomic_write_json,
    command_exists,
    ensure_within,
    sha256_file,
)

FirewallRole = Literal["panel", "node"]


@dataclass(frozen=True, slots=True)
class FirewallPlan:
    role: FirewallRole
    ssh_ports: tuple[int, ...]
    commands: tuple[tuple[str, ...], ...]
    panel_ip: str | None = None


@dataclass(frozen=True, slots=True)
class FirewallPaths:
    before_rules: Path = Path("/etc/ufw/before.rules")
    before6_rules: Path = Path("/etc/ufw/before6.rules")
    after_rules: Path = Path("/etc/ufw/after.rules")
    after6_rules: Path = Path("/etc/ufw/after6.rules")
    user_rules: Path = Path("/etc/ufw/user.rules")
    user6_rules: Path = Path("/etc/ufw/user6.rules")
    ufw_conf: Path = Path("/etc/ufw/ufw.conf")
    defaults: Path = Path("/etc/default/ufw")

    def items(self) -> tuple[tuple[str, Path], ...]:
        return (
            ("before.rules", self.before_rules),
            ("before6.rules", self.before6_rules),
            ("after.rules", self.after_rules),
            ("after6.rules", self.after6_rules),
            ("user.rules", self.user_rules),
            ("user6.rules", self.user6_rules),
            ("ufw.conf", self.ufw_conf),
            ("default.ufw", self.defaults),
        )


@dataclass(frozen=True, slots=True)
class _FirewallFileSnapshot:
    path: Path
    snapshot: Path
    sha256: str
    mode: int
    uid: int
    gid: int
    device: int
    inode: int


@dataclass(slots=True)
class FirewallTransaction:
    runner: Runner
    root: Path
    directory: Path
    files: tuple[_FirewallFileSnapshot, ...]
    was_active: bool
    closed: bool = False

    @property
    def artifact_path(self) -> Path:
        return self.directory

    def rollback(self) -> None:
        if self.closed:
            return
        _validate_restore_targets(self.files)
        for item in self.files:
            _restore_snapshot_file(item)
        _verify_restored_files(self.files)

        if self.was_active:
            self.runner.run(["ufw", "reload"], timeout=120)
        else:
            # disable меняет ufw.conf, поэтому после изменения runtime возвращаются
            # точные байты всех конфигурационных файлов.
            self.runner.run(["ufw", "--force", "disable"], timeout=120)
            for item in self.files:
                _restore_snapshot_file(item)
        _verify_restored_files(self.files)
        if _ufw_is_active(self.runner) != self.was_active:
            raise TransactionError("UFW не вернулся в исходное runtime-состояние.")
        self.closed = True
        _remove_transaction_directory(self.root, self.directory)

    def commit(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            _remove_transaction_directory(self.root, self.directory)
        except (OSError, ValidationError):
            # Снимок имеет 0700/0600 и безопасен, а применение firewall уже завершено.
            pass


def detect_ssh_ports(runner: Runner) -> tuple[int, ...]:
    environment = dict(os.environ)
    environment["LC_ALL"] = "C"
    result = runner.run(
        ["sshd", "-T"],
        check=False,
        timeout=30,
        env=environment,
    )
    if result.returncode != 0:
        raise ValidationError(
            "Не удалось определить SSH-порты через sshd -T. "
            "Исправьте конфигурацию OpenSSH или укажите --ssh-port явно; "
            "UFW не изменён."
        )
    ports: set[int] = set()
    for line in result.stdout.splitlines():
        match = re.fullmatch(r"port\s+(\d{1,5})", line.strip(), re.IGNORECASE)
        if match:
            port = int(match.group(1))
            if 1 <= port <= 65535:
                ports.add(port)
    if not ports:
        raise ValidationError(
            "sshd -T не вернул ни одного SSH-порта. "
            "Укажите --ssh-port явно; UFW не изменён."
        )
    ports.update(_active_ssh_socket_ports(runner))
    connection_port = _current_ssh_port()
    if connection_port is not None and connection_port not in ports:
        raise ValidationError(
            f"Текущая SSH-сессия сообщает порт {connection_port}, которого нет в "
            "проверенной конфигурации sshd/ssh.socket. Укажите --ssh-port явно после "
            "проверки OpenSSH; UFW не изменён."
        )
    listening = _listening_tcp_ports(runner)
    missing = sorted(ports - listening)
    if missing:
        raise ValidationError(
            "SSH-порты из sshd/ssh.socket/текущей сессии не слушаются: "
            + ", ".join(str(port) for port in missing)
            + ". Перезапустите OpenSSH или укажите корректные --ssh-port; UFW не изменён."
        )
    return tuple(sorted(ports))


def _active_ssh_socket_ports(runner: Runner) -> set[int]:
    active = runner.run(
        ["systemctl", "is-active", "--quiet", "ssh.socket"],
        check=False,
        timeout=30,
    )
    if active.returncode in {3, 4}:
        return set()
    if active.returncode != 0:
        raise ValidationError(
            "Не удалось определить состояние ssh.socket; UFW не изменён."
        )
    details = runner.run(
        ["systemctl", "show", "--property=Listen", "--value", "ssh.socket"],
        check=False,
        timeout=30,
    )
    if details.returncode != 0:
        raise ValidationError("Не удалось прочитать Listen ssh.socket; UFW не изменён.")
    ports: set[int] = set()
    for line in details.stdout.splitlines():
        for value in re.findall(
            r"(?:^|:)(\d{1,5})(?=\s+\(Stream\)(?:\s|$)|$)",
            line.strip(),
        ):
            ports.add(_port(int(value)))
    if not ports:
        raise ValidationError(
            "Активный ssh.socket не сообщил ни одного TCP-порта; UFW не изменён."
        )
    return ports


def _current_ssh_port() -> int | None:
    value = os.environ.get("SSH_CONNECTION")
    if value is None:
        return None
    fields = value.split()
    if len(fields) != 4:
        raise ValidationError("SSH_CONNECTION повреждён; UFW не изменён.")
    try:
        ipaddress.ip_address(fields[0])
        ipaddress.ip_address(fields[2])
        _port(int(fields[1]))
        server_port = _port(int(fields[3]))
    except (ValueError, ValidationError) as error:
        raise ValidationError("SSH_CONNECTION повреждён; UFW не изменён.") from error
    return server_port


def _listening_tcp_ports(runner: Runner) -> set[int]:
    result = runner.run(["ss", "-H", "-ltn"], check=False, timeout=30)
    if result.returncode != 0:
        raise ValidationError(
            "Не удалось проверить слушающие TCP-порты через ss; UFW не изменён."
        )
    ports: set[int] = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        match = re.search(r":(\d{1,5})$", fields[3])
        if match:
            ports.add(_port(int(match.group(1))))
    if not ports:
        raise ValidationError("ss не вернул слушающие TCP-порты; UFW не изменён.")
    return ports


def _validate_explicit_ssh_ports(runner: Runner, ports: Sequence[int]) -> None:
    selected = {_port(port) for port in ports}
    if not selected:
        raise ValidationError("Укажите хотя бы один SSH-порт; UFW не изменён.")
    current = _current_ssh_port()
    if current is not None and current not in selected:
        raise ValidationError(
            f"Текущая SSH-сессия использует порт {current}, но он не указан в --ssh-port; "
            "UFW не изменён."
        )
    missing = sorted(selected - _listening_tcp_ports(runner))
    if missing:
        raise ValidationError(
            "Явно указанные SSH-порты сейчас не слушаются: "
            + ", ".join(str(port) for port in missing)
            + ". Сначала запустите SSH на этих портах; UFW не изменён."
        )


def build_firewall_commands(
    role: FirewallRole,
    ssh_ports: Sequence[int],
    *,
    panel_ip: str | None = None,
) -> list[list[str]]:
    if role not in {"panel", "node"}:
        raise ValidationError(f"Неизвестная роль firewall: {role}")
    normalized_ssh = sorted({_port(item) for item in ssh_ports})
    if not normalized_ssh:
        raise ValidationError("Не найден порт SSH; UFW не будет включён.")
    commands: list[list[str]] = []
    for port in normalized_ssh:
        commands.append(
            ["ufw", "allow", f"{port}/tcp", "comment", "remnawave-manager:ssh"]
        )
    commands.extend(
        [
            ["ufw", "default", "deny", "incoming"],
            ["ufw", "default", "allow", "outgoing"],
        ]
    )
    commands.extend(
        [
            ["ufw", "allow", "80/tcp", "comment", "remnawave-manager:http"],
            ["ufw", "allow", "443/tcp", "comment", "remnawave-manager:https"],
        ]
    )
    if role == "node":
        if 2222 in normalized_ssh:
            raise ValidationError(
                "Порт 2222 зарезервирован для Node API и не может одновременно использоваться SSH."
            )
        address = _panel_ipv4(panel_ip)
        commands.extend(
            [
                list(_insert_rule(1, _node_api_allow_rule(address, "panel-api"))),
                list(_insert_rule(2, _node_api_deny_rule("node-api-deny"))),
            ]
        )
    commands += [["ufw", "--force", "enable"], ["ufw", "reload"]]
    return commands


def configure_firewall(
    runner: Runner,
    role: FirewallRole,
    *,
    panel_ip: str | None = None,
    ssh_ports: Sequence[int] | None = None,
    transaction_root: Path = Path("/var/lib/remnawave-manager/firewall-transactions"),
) -> tuple[int, ...]:
    plan = plan_firewall(
        runner,
        role,
        panel_ip=panel_ip,
        ssh_ports=ssh_ports,
    )
    transaction = apply_firewall_transactional(
        runner,
        plan,
        transaction_root=transaction_root,
    )
    transaction.commit()
    return plan.ssh_ports


def plan_firewall(
    runner: Runner,
    role: FirewallRole,
    *,
    panel_ip: str | None = None,
    ssh_ports: Sequence[int] | None = None,
) -> FirewallPlan:
    if not command_exists("ufw"):
        raise ValidationError(
            "UFW не установлен. Повторно запустите корневой install.sh."
        )
    if ssh_ports is None:
        selected_ports = detect_ssh_ports(runner)
    else:
        selected_ports = tuple(ssh_ports)
        _validate_explicit_ssh_ports(runner, selected_ports)
    commands = build_firewall_commands(role, selected_ports, panel_ip=panel_ip)
    normalized_ports = tuple(sorted({_port(item) for item in selected_ports}))
    normalized_panel_ip = str(_panel_ipv4(panel_ip)) if role == "node" else None
    return FirewallPlan(
        role=role,
        ssh_ports=normalized_ports,
        commands=tuple(tuple(command) for command in commands),
        panel_ip=normalized_panel_ip,
    )


def apply_firewall(runner: Runner, plan: FirewallPlan) -> None:
    transition_setup: tuple[tuple[str, ...], ...] = ()
    transition_cleanup: tuple[tuple[str, ...], ...] = ()
    old_deletions: tuple[tuple[str, ...], ...]
    if plan.role == "node":
        address = _panel_ipv4(plan.panel_ip)
        has_existing_rules, old_deletions = _existing_ufw_rule_state(runner)
        final_allow = _insert_rule(1, _node_api_allow_rule(address, "panel-api"))
        final_deny = _insert_rule(2, _node_api_deny_rule("node-api-deny"))
        if final_allow not in plan.commands or final_deny not in plan.commands:
            raise ValidationError(
                "План Node firewall не содержит финальную пару allow/deny для порта 2222."
            )
        transition_denies = _node_api_transition_deny_rules()
        transition_setup = tuple(
            _insert_rule(1, rule)
            if has_existing_rules or index > 0
            else ("ufw", *rule)
            for index, rule in enumerate(transition_denies)
        )
        stale_transition_deletions = tuple(
            command
            for command in old_deletions
            if (
                (comment := _manager_rule_comment(command)) is not None
                and comment.startswith("remnawave-manager:transition-")
            )
        )
        old_deletions = tuple(
            command
            for command in old_deletions
            if command not in stale_transition_deletions
        )
        cleanup_commands = [
            *stale_transition_deletions,
            *(_delete_rule(rule) for rule in reversed(transition_denies)),
        ]
        transition_cleanup = tuple(
            command
            for index, command in enumerate(cleanup_commands)
            if command not in cleanup_commands[:index]
        )
    elif plan.role != "panel":
        raise ValidationError(f"Неизвестная роль firewall: {plan.role}")
    else:
        _, old_deletions = _existing_ufw_rule_state(runner)

    for command in transition_setup:
        runner.run(list(command), timeout=120)
    for command in old_deletions:
        runner.run(list(command), timeout=120)
    for command in plan.commands:
        runner.run(list(command), timeout=120)
    for command in transition_cleanup:
        runner.run(list(command), timeout=120)
    _verify_applied_manager_rules(runner, plan)


def _panel_ipv4(value: str | None) -> ipaddress.IPv4Address:
    if not isinstance(value, str):
        raise ValidationError("Для Node нужно указать IP-адрес Panel.")
    try:
        address = ipaddress.ip_address(value)
    except ValueError as error:
        raise ValidationError("Некорректный IP-адрес Panel.") from error
    if not isinstance(address, ipaddress.IPv4Address):
        raise ValidationError(
            "Для ограничения Node API сейчас поддерживается IPv4 Panel."
        )
    return address


def _node_api_allow_rule(
    address: ipaddress.IPv4Address,
    comment: str,
) -> tuple[str, ...]:
    return (
        "allow",
        "from",
        str(address),
        "to",
        "any",
        "port",
        "2222",
        "proto",
        "tcp",
        "comment",
        f"remnawave-manager:{comment}",
    )


def _node_api_deny_rule(comment: str) -> tuple[str, ...]:
    return (
        "deny",
        "to",
        "any",
        "port",
        "2222",
        "proto",
        "tcp",
        "comment",
        f"remnawave-manager:{comment}",
    )


def _node_api_transition_deny_rules() -> tuple[tuple[str, ...], ...]:
    networks = (
        ("0.0.0.0/1", "v4-low"),
        ("128.0.0.0/1", "v4-high"),
        ("::/1", "v6-low"),
        ("8000::/1", "v6-high"),
    )
    return tuple(
        (
            "deny",
            "from",
            network,
            "to",
            "any",
            "port",
            "2222",
            "proto",
            "tcp",
            "comment",
            f"remnawave-manager:transition-node-api-deny-{suffix}",
        )
        for network, suffix in networks
    )


def _insert_rule(position: int, specification: tuple[str, ...]) -> tuple[str, ...]:
    return ("ufw", "insert", str(position), *specification)


def _delete_rule(specification: tuple[str, ...]) -> tuple[str, ...]:
    return ("ufw", "--force", "delete", *specification)


def _manager_rule_comment(command: Sequence[str]) -> str | None:
    indexes = [index for index, token in enumerate(command) if token == "comment"]
    if len(indexes) != 1 or indexes[0] + 1 >= len(command):
        return None
    comment = command[indexes[0] + 1]
    if not re.fullmatch(r"remnawave-manager:[a-z0-9-]{1,64}", comment):
        return None
    return comment


def _verify_applied_manager_rules(runner: Runner, plan: FirewallPlan) -> None:
    expected = {
        comment
        for command in plan.commands
        if (comment := _manager_rule_comment(command)) is not None
    }
    _, deletions = _existing_ufw_rule_state(runner)
    actual = {
        comment
        for command in deletions
        if (comment := _manager_rule_comment(command)) is not None
    }
    missing = sorted(expected - actual)
    transitional = sorted(
        comment for comment in actual if comment.startswith("remnawave-manager:transition-")
    )
    if missing or transitional:
        details: list[str] = []
        if missing:
            details.append("отсутствуют: " + ", ".join(missing))
        if transitional:
            details.append("остались временные: " + ", ".join(transitional))
        raise TransactionError(
            "UFW не подтвердил итоговый набор правил менеджера; " + "; ".join(details)
        )


def _existing_ufw_rule_state(
    runner: Runner,
) -> tuple[bool, tuple[tuple[str, ...], ...]]:
    environment = dict(os.environ)
    environment["LC_ALL"] = "C"
    result = runner.run(
        ["ufw", "show", "added"],
        check=False,
        timeout=30,
        env=environment,
    )
    if result.returncode != 0:
        raise ValidationError(
            "Не удалось прочитать существующие UFW rules; UFW не изменён."
        )
    has_rules = False
    commands: list[tuple[str, ...]] = []
    for raw_line in result.stdout.splitlines():
        if not re.match(r"^\s*ufw(?:\s|$)", raw_line):
            continue
        has_rules = True
        if "remnawave-manager:" not in raw_line:
            continue
        try:
            tokens = shlex.split(raw_line, posix=True)
        except ValueError as error:
            raise ValidationError(
                "Не удалось безопасно разобрать существующее manager-правило UFW."
            ) from error
        if not tokens or tokens[0] != "ufw":
            raise ValidationError(
                "Существующее manager-правило UFW имеет неожиданный формат."
            )
        specification = tokens[1:]
        if len(specification) >= 2 and specification[0] == "insert":
            if not specification[1].isdigit():
                raise ValidationError(
                    "Существующее manager-правило UFW имеет некорректную позицию."
                )
            specification = specification[2:]
        comment_indexes = [
            index
            for index, token in enumerate(specification)
            if token == "comment"  # noqa: S105, RUF100 - UFW grammar token, not a credential
        ]
        if (
            not specification
            or specification[0] not in {"allow", "deny"}
            or len(comment_indexes) != 1
            or comment_indexes[0] + 1 >= len(specification)
            or not re.fullmatch(
                r"remnawave-manager:[a-z0-9-]{1,64}",
                specification[comment_indexes[0] + 1],
            )
            or comment_indexes[0] + 2 != len(specification)
        ):
            raise ValidationError(
                "Существующее manager-правило UFW имеет неожиданный формат."
            )
        commands.append(("ufw", "--force", "delete", *specification))
    return has_rules, tuple(commands)


def apply_firewall_transactional(
    runner: Runner,
    plan: FirewallPlan,
    *,
    transaction_root: Path,
    paths: FirewallPaths | None = None,
) -> FirewallTransaction:
    transaction = _capture_firewall_transaction(
        runner,
        transaction_root,
        paths or FirewallPaths(),
    )
    try:
        _verify_source_files_unchanged(transaction.files)
        if _ufw_is_active(runner) != transaction.was_active:
            raise ValidationError("Состояние UFW изменилось перед применением правил.")
    except BaseException:
        transaction.commit()
        raise
    try:
        apply_firewall(runner, plan)
        if not _ufw_is_active(runner):
            raise TransactionError("После применения правил UFW не активен.")
    except BaseException as error:
        try:
            transaction.rollback()
        except BaseException as rollback_error:  # noqa: BLE001
            raise TransactionError(
                "Применение UFW не завершено, автоматический rollback неполон. "
                "Текущее состояние firewall требует ручной проверки; "
                f"снимок сохранён в {transaction.artifact_path}: {rollback_error}. "
                f"Исходная ошибка: {error}"
            ) from error
        raise TransactionError(
            f"Применение UFW не завершено; точное исходное состояние восстановлено: {error}"
        ) from error
    return transaction


def _capture_firewall_transaction(
    runner: Runner,
    transaction_root: Path,
    paths: FirewallPaths,
) -> FirewallTransaction:
    root = _prepare_transaction_root(transaction_root)
    directory = root / f"ufw-{uuid.uuid4().hex}"
    ensure_within(directory, root)
    try:
        directory.mkdir(mode=0o700)
    except OSError as error:
        raise ValidationError(
            f"Не удалось создать каталог снимка UFW: {error}"
        ) from error
    try:
        active_before = _ufw_is_active(runner)
        snapshots = tuple(
            _snapshot_firewall_file(path, directory / f"{name}.snapshot")
            for name, path in paths.items()
        )
        enabled_before = _ufw_conf_is_enabled(
            next(item.snapshot for item in snapshots if item.path == paths.ufw_conf)
        )
        active_after = _ufw_is_active(runner)
        if active_after != active_before:
            raise ValidationError(
                "Состояние UFW изменилось параллельно во время создания снимка."
            )
        _verify_source_files_unchanged(snapshots)
        if enabled_before != active_before:
            raise ValidationError(
                "Состояния UFW не согласованы: /etc/ufw/ufw.conf и ufw status "
                "сообщают разные значения. UFW не изменён."
            )
        atomic_write_json(
            directory / "manifest.json",
            {
                "schema_version": 1,
                "active": active_before,
                "enabled": enabled_before,
                "files": [
                    {
                        "path": str(item.path),
                        "snapshot": item.snapshot.name,
                        "sha256": item.sha256,
                        "mode": item.mode,
                        "uid": item.uid,
                        "gid": item.gid,
                    }
                    for item in snapshots
                ],
            },
            mode=0o600,
        )
        return FirewallTransaction(
            runner,
            root,
            directory,
            snapshots,
            active_before,
        )
    except BaseException:
        _remove_transaction_directory(root, directory)
        raise


def _prepare_transaction_root(path: Path) -> Path:
    if not path.is_absolute():
        raise ValidationError("Каталог транзакций UFW должен быть абсолютным.")
    _assert_directory_chain_without_symlinks(path.parent)
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValidationError(f"Каталог транзакций UFW имеет небезопасный тип: {path}")
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as error:
        raise ValidationError(
            f"Не удалось создать каталог транзакций UFW {path}: {error}"
        ) from error
    _assert_directory_chain_without_symlinks(path)
    info = path.lstat()
    if os.name == "posix":
        if _requires_root_owner(path) and info.st_uid != 0:
            raise ValidationError(f"Каталог транзакций UFW не принадлежит root: {path}")
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise ValidationError(
                f"Каталог транзакций UFW доступен другим пользователям: {path}"
            )
    pending = list(path.iterdir())
    if pending:
        raise ValidationError(
            "Обнаружена незавершённая UFW-транзакция: "
            + ", ".join(str(item) for item in pending)
            + ". Сначала завершите rollback по сохранённому manifest.json."
        )
    return path


def _assert_directory_chain_without_symlinks(path: Path) -> None:
    require_root_control = os.name == "posix" and _requires_root_controlled_chain(path)
    for candidate in (path, *path.parents):
        if not candidate.exists() and not candidate.is_symlink():
            continue
        info = candidate.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValidationError(
                f"Родитель UFW-пути имеет небезопасный тип: {candidate}"
            )
        if require_root_control and (
            info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o022
        ):
            raise ValidationError(
                f"Родитель UFW-пути не контролируется исключительно root: {candidate}"
            )


def _requires_root_controlled_chain(path: Path) -> bool:
    value = path.as_posix()
    return value == "/etc" or value.startswith(("/etc/", "/var/lib/remnawave-manager"))


def _requires_root_owner(path: Path) -> bool:
    value = path.as_posix()
    return value == "/etc/default/ufw" or value.startswith(
        ("/etc/ufw/", "/var/lib/remnawave-manager/")
    )


def _validate_firewall_file(path: Path) -> os.stat_result:
    if not path.is_absolute():
        raise ValidationError(f"Путь UFW должен быть абсолютным: {path}")
    _assert_directory_chain_without_symlinks(path.parent)
    try:
        info = path.lstat()
    except FileNotFoundError as error:
        raise ValidationError(
            f"Обязательный конфигурационный файл UFW отсутствует: {path}"
        ) from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ValidationError(
            f"Конфигурационный файл UFW имеет небезопасный тип: {path}"
        )
    if info.st_nlink != 1:
        raise ValidationError(f"Конфигурационный файл UFW является hardlink: {path}")
    if os.name == "posix":
        if _requires_root_owner(path) and info.st_uid != 0:
            raise ValidationError(
                f"Конфигурационный файл UFW не принадлежит root: {path}"
            )
        if stat.S_IMODE(info.st_mode) & 0o022:
            raise ValidationError(
                f"Конфигурационный файл UFW доступен для записи не только root: {path}"
            )
    return info


def _open_regular_nofollow(path: Path) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValidationError(
            f"Не удалось безопасно открыть UFW-файл {path}: {error}"
        ) from error
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        os.close(descriptor)
        raise ValidationError(f"UFW-файл изменил тип во время открытия: {path}")
    return descriptor, info


def _snapshot_firewall_file(path: Path, snapshot: Path) -> _FirewallFileSnapshot:
    before = _validate_firewall_file(path)
    descriptor, opened = _open_regular_nofollow(path)
    if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
        os.close(descriptor)
        raise ValidationError(f"UFW-файл был подменён во время создания снимка: {path}")
    digest = hashlib.sha256()
    try:
        output_descriptor = os.open(
            snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
    except BaseException:
        os.close(descriptor)
        raise
    try:
        with (
            os.fdopen(descriptor, "rb") as source,
            os.fdopen(output_descriptor, "wb") as target,
        ):
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
                target.write(block)
            target.flush()
            os.fsync(target.fileno())
    except BaseException:
        snapshot.unlink(missing_ok=True)
        raise
    os.chmod(snapshot, 0o600)
    return _FirewallFileSnapshot(
        path=path,
        snapshot=snapshot,
        sha256=digest.hexdigest(),
        mode=stat.S_IMODE(opened.st_mode),
        uid=opened.st_uid,
        gid=opened.st_gid,
        device=opened.st_dev,
        inode=opened.st_ino,
    )


def _secure_file_sha256(path: Path) -> tuple[str, os.stat_result]:
    descriptor, info = _open_regular_nofollow(path)
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest(), info


def _ufw_conf_is_enabled(snapshot: Path) -> bool:
    descriptor, _ = _open_regular_nofollow(snapshot)
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            lines = stream.read().splitlines()
    except UnicodeDecodeError as error:
        raise ValidationError(
            "ufw.conf имеет некорректную кодировку; UFW не изменён."
        ) from error

    assignment = re.compile(
        r"""^ENABLED\s*=\s*(?P<quote>["']?)(?P<value>yes|no)(?P=quote)\s*(?:#.*)?$""",
        re.IGNORECASE,
    )
    values: list[bool] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not re.match(r"^ENABLED(?:\s*=|\s*$)", stripped, re.IGNORECASE):
            continue
        match = assignment.fullmatch(stripped)
        if match is None:
            raise ValidationError(
                "Не удалось однозначно прочитать ENABLED из ufw.conf; UFW не изменён."
            )
        values.append(match.group("value").lower() == "yes")
    if len(values) != 1:
        raise ValidationError(
            "ufw.conf должен содержать ровно одно значение ENABLED=yes или ENABLED=no; "
            "UFW не изменён."
        )
    return values[0]


def _verify_source_files_unchanged(files: tuple[_FirewallFileSnapshot, ...]) -> None:
    for item in files:
        current = _validate_firewall_file(item.path)
        checksum, opened = _secure_file_sha256(item.path)
        if (
            (current.st_dev, current.st_ino) != (item.device, item.inode)
            or (opened.st_dev, opened.st_ino) != (item.device, item.inode)
            or checksum != item.sha256
            or stat.S_IMODE(current.st_mode) != item.mode
            or current.st_uid != item.uid
            or current.st_gid != item.gid
        ):
            raise ValidationError(f"UFW-файл изменился параллельно: {item.path}")


def _validate_restore_targets(files: tuple[_FirewallFileSnapshot, ...]) -> None:
    for item in files:
        _validate_firewall_file(item.path)
        checksum, info = _secure_file_sha256(item.snapshot)
        if checksum != item.sha256 or (
            os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise TransactionError(f"Снимок UFW повреждён: {item.snapshot}")


def _restore_snapshot_file(item: _FirewallFileSnapshot) -> None:
    _validate_firewall_file(item.path)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{item.path.name}.rwm-", dir=item.path.parent
    )
    temporary_path = Path(temporary)
    try:
        source_descriptor, _ = _open_regular_nofollow(item.snapshot)
        with (
            os.fdopen(source_descriptor, "rb") as source,
            os.fdopen(descriptor, "wb") as target,
        ):
            shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        if os.name == "posix":
            temporary_info = temporary_path.stat()
            if (temporary_info.st_uid, temporary_info.st_gid) != (item.uid, item.gid):
                os.chown(temporary_path, item.uid, item.gid)
        os.chmod(temporary_path, item.mode)
        if sha256_file(temporary_path) != item.sha256:
            raise TransactionError(f"Временная копия UFW повреждена: {item.path}")
        _validate_firewall_file(item.path)
        os.replace(temporary_path, item.path)
        if os.name == "posix":
            directory_fd = os.open(item.path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temporary_path.unlink(missing_ok=True)


def _verify_restored_files(files: tuple[_FirewallFileSnapshot, ...]) -> None:
    for item in files:
        info = _validate_firewall_file(item.path)
        checksum, opened = _secure_file_sha256(item.path)
        if (
            checksum != item.sha256
            or stat.S_IMODE(info.st_mode) != item.mode
            or info.st_uid != item.uid
            or info.st_gid != item.gid
            or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino)
        ):
            raise TransactionError(f"UFW-файл не восстановлен точно: {item.path}")


def _ufw_is_active(runner: Runner) -> bool:
    environment = dict(os.environ)
    environment["LC_ALL"] = "C"
    result = runner.run(["ufw", "status"], timeout=30, env=environment)
    match = re.search(r"(?mi)^Status:\s*(active|inactive)\s*$", result.stdout)
    if match is None:
        raise ValidationError("ufw status вернул неожиданный ответ; UFW не изменён.")
    return match.group(1).lower() == "active"


def _remove_transaction_directory(root: Path, directory: Path) -> None:
    ensure_within(directory, root)
    if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
        raise ValidationError(f"Каталог снимка UFW имеет небезопасный тип: {directory}")
    if directory.exists():
        shutil.rmtree(directory)


def _port(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValidationError(f"Некорректный порт SSH: {value}")
    return value
