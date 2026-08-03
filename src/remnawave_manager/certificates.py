from __future__ import annotations

import os
import posixpath
import re
import stat
from collections.abc import Sequence
from configparser import ConfigParser
from configparser import Error as ConfigParserError
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from .errors import TransactionError, ValidationError
from .models import Inventory
from .runner import (
    RegularFileSnapshot,
    Runner,
    atomic_copy,
    atomic_write_bytes,
    atomic_write_text,
    command_exists,
    read_stable_regular_file,
)
from .state import StateStore

CertificateMethod = Literal["existing", "http-01", "cloudflare", "gcore"]
CertbotTimerEnablement = Literal[
    "enabled",
    "enabled-runtime",
    "disabled",
    "masked",
    "masked-runtime",
    "static",
    "indirect",
]
_HOOK_MARKER = "# Managed by remnawave-manager"
_CREDENTIAL_MARKER = "# Managed by remnawave-manager"
_LEGACY_CRON_LOG = ">> /usr/local/remnawave_reverse/cron_jobs.log 2>&1"
_DEFAULT_HOOK_ROOT = Path("/etc/letsencrypt/renewal-hooks")
_DEFAULT_CREDENTIALS_ROOT = Path("/etc/remnawave-manager/certbot")
_MAX_INVENTORY_SIZE = 16 * 1024 * 1024
_MAX_CERTBOT_TEXT_SIZE = 1024 * 1024
_MAX_NGINX_CONFIG_SIZE = 16 * 1024 * 1024
_CERTBOT_MARKER_PREFIX = "remnawave-manager-certbot-nginx-"
_CERTBOT_MARKER_ROOT = "/run"
_CERTBOT_LOCK_DIR = "/run/remnawave-manager"
_CERTBOT_LOCK_PATH = "/run/remnawave-manager/manager.lock"
CERTBOT_MANAGER_LOCK_HELD_ENV = "RWM_CERTBOT_MANAGER_LOCK_HELD"


@dataclass(frozen=True, slots=True)
class CertificateSpec:
    method: CertificateMethod
    email: str | None = None
    fullchain: Path | None = None
    private_key: Path | None = None
    cloudflare_token: str | None = None
    gcore_token: str | None = None


@dataclass(frozen=True, slots=True)
class CertificateMaterial:
    host_root: Path
    container_root: str
    fullchain: str
    private_key: str
    managed_by_certbot: bool
    lineage_name: str | None = None
    credentials_file: Path | None = None
    transaction: CertificateTransaction | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def compose_mounts(self, *, container_root: str | None = None) -> tuple[str, ...]:
        target_root = PurePosixPath(container_root or self.container_root)
        if not target_root.is_absolute() or ".." in target_root.parts:
            raise ValidationError("Container root сертификата должен быть абсолютным POSIX-путём.")
        if os.name == "posix" and not self.host_root.is_absolute():
            raise ValidationError("Host root сертификата должен быть абсолютным путём.")
        if not self.managed_by_certbot:
            return (f"{self.host_root}:{target_root}:ro",)

        lineage = self.lineage_name or self._lineage_from_paths()
        normalized = normalize_domain(lineage)
        if normalized != lineage:
            raise ValidationError("Имя Certbot lineage сертификата некорректно.")
        self._validate_certbot_container_paths(normalized)
        return tuple(
            f"{self.host_root / branch / normalized}:{target_root / branch / normalized}:ro"
            for branch in ("live", "archive")
        )

    def _lineage_from_paths(self) -> str:
        root = PurePosixPath(self.container_root)
        try:
            relative = PurePosixPath(self.fullchain).relative_to(root)
        except ValueError as error:
            raise ValidationError(
                "Путь fullchain не находится внутри Certbot container root."
            ) from error
        if len(relative.parts) != 3 or relative.parts[0] != "live":
            raise ValidationError("Не удалось однозначно определить Certbot lineage.")
        return relative.parts[1]

    def _validate_certbot_container_paths(self, lineage: str) -> None:
        root = PurePosixPath(self.container_root)
        expected_fullchain = root / "live" / lineage / "fullchain.pem"
        expected_private_key = root / "live" / lineage / "privkey.pem"
        if (
            PurePosixPath(self.fullchain) != expected_fullchain
            or PurePosixPath(self.private_key) != expected_private_key
        ):
            raise ValidationError(
                "Пути Certbot fullchain/private key не совпадают с выбранным lineage."
            )

    def commit(self) -> None:
        if self.transaction is not None:
            self.transaction.active = False

    def rollback(self, runner: Runner) -> None:
        if self.transaction is not None:
            self.transaction.rollback(runner)


@dataclass(slots=True)
class CertificateTransaction:
    certificate_name: str
    letsencrypt_root: Path
    hooks: dict[Path, tuple[bytes, int] | None]
    credentials: Path | None
    credential_snapshot: tuple[bytes, int] | None
    credentials_root: Path | None
    credentials_root_created: bool
    timer_enablement: CertbotTimerEnablement
    timer_active: bool
    delete_lineage_on_rollback: bool
    active: bool = True

    def rollback(self, runner: Runner) -> None:
        if not self.active:
            return
        errors: list[str] = []
        lineage_retained = False
        if self.delete_lineage_on_rollback and _lineage_exists(
            self.letsencrypt_root,
            self.certificate_name,
        ):
            command = [
                "certbot",
                "delete",
                "--cert-name",
                self.certificate_name,
                "--non-interactive",
            ]
            if self.letsencrypt_root != Path("/etc/letsencrypt"):
                command += ["--config-dir", str(self.letsencrypt_root)]
            try:
                result = runner.run(
                    command,
                    check=False,
                    timeout=120,
                    sensitive=True,
                )
                if _lineage_exists(self.letsencrypt_root, self.certificate_name):
                    lineage_retained = True
                    if result.returncode != 0:
                        errors.append("Certbot не удалил незавершённый lineage")
                    else:
                        errors.append(
                            "Certbot сообщил об удалении, но незавершённый lineage остался"
                        )
            except BaseException as error:  # noqa: BLE001 - continue compensation
                lineage_retained = _lineage_exists(
                    self.letsencrypt_root,
                    self.certificate_name,
                )
                errors.append(f"Certbot lineage: {error}")
        if lineage_retained:
            raise TransactionError(
                "Rollback сертификата выполнен не полностью: "
                + "; ".join(errors)
                + ". Текущие hooks, credential и certbot.timer сохранены, чтобы оставшийся "
                "lineage не получил заведомо неработающее продление."
            )
        try:
            _restore_hooks(self.hooks)
        except BaseException as error:  # noqa: BLE001 - continue compensation
            errors.append(f"Certbot hooks: {error}")
        if self.credentials is not None:
            try:
                _restore_credentials(self.credentials, self.credential_snapshot)
            except BaseException as error:  # noqa: BLE001 - continue compensation
                errors.append(f"учётные данные DNS: {error}")
        if self.credentials_root_created and self.credentials_root is not None:
            try:
                _remove_empty_credentials_root(self.credentials_root)
            except BaseException as error:  # noqa: BLE001 - continue compensation
                errors.append(f"каталог учётных данных DNS: {error}")
        try:
            _restore_certbot_timer(
                runner,
                enablement=self.timer_enablement,
                active=self.timer_active,
            )
        except BaseException as error:  # noqa: BLE001 - continue compensation
            errors.append(f"certbot.timer: {error}")
        if errors:
            raise TransactionError(
                "Rollback сертификата выполнен не полностью: " + "; ".join(errors)
            )
        self.active = False


@dataclass(frozen=True, slots=True)
class IssuedCertificate:
    certificate_name: str
    domains: tuple[str, ...]
    fullchain: Path
    private_key: Path
    method: CertificateMethod


@dataclass(frozen=True, slots=True)
class CertbotRenewalPlan:
    certificate_names: tuple[str, ...]
    missing_renewal_configs: tuple[str, ...]
    authenticators: tuple[tuple[str, str], ...]
    legacy_renew_hooks: tuple[str, ...]

    @property
    def uses_standalone(self) -> bool:
        return any(value == "standalone" for _, value in self.authenticators)

    @property
    def detected(self) -> bool:
        return bool(self.certificate_names)


def normalize_domain(value: str) -> str:
    selected = value.strip().rstrip(".")
    try:
        selected = selected.encode("idna").decode("ascii").lower()
    except UnicodeError as error:
        raise ValidationError(f"Некорректный домен: {value}") from error
    if len(selected) > 253 or not selected or ".." in selected:
        raise ValidationError(f"Некорректный домен: {value}")
    labels = selected.split(".")
    if len(labels) < 2 or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
        for label in labels
    ):
        raise ValidationError(f"Некорректный домен: {value}")
    return selected


def build_certbot_command(
    domains: Sequence[str],
    spec: CertificateSpec,
    *,
    credentials_file: Path | None = None,
) -> list[str]:
    normalized = _normalized_domains(domains)
    if spec.method not in {"http-01", "cloudflare", "gcore"}:
        raise ValidationError(
            "Certbot применим только для HTTP-01, Cloudflare DNS-01 или Gcore DNS-01."
        )
    email = (spec.email or "").strip()
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        raise ValidationError("Для Let's Encrypt нужен корректный адрес электронной почты.")
    command = [
        "certbot",
        "certonly",
        "--non-interactive",
        "--agree-tos",
        "--no-eff-email",
        "--email",
        email,
        "--cert-name",
        normalized[0],
    ]
    if spec.method == "http-01":
        command += ["--standalone", "--preferred-challenges", "http-01"]
    elif spec.method == "cloudflare":
        if credentials_file is None:
            raise ValidationError("Не указан файл учётных данных Cloudflare.")
        command += [
            "--dns-cloudflare",
            "--dns-cloudflare-credentials",
            str(credentials_file),
            "--dns-cloudflare-propagation-seconds",
            "30",
        ]
    else:
        if credentials_file is None:
            raise ValidationError("Не указан файл учётных данных Gcore.")
        command += [
            "--authenticator",
            "dns-gcore",
            "--dns-gcore-credentials",
            str(credentials_file),
            "--dns-gcore-propagation-seconds",
            "80",
        ]
    command += ["--key-type", "ecdsa", "--elliptic-curve", "secp384r1"]
    for domain in normalized:
        command += ["--domain", domain]
    return command


def obtain_certificate(
    runner: Runner,
    domains: Sequence[str],
    spec: CertificateSpec,
    *,
    install_dir: Path,
    nginx_container: str = "remnawave-nginx",
    stop_nginx_for_http01: bool = True,
    credentials_dir: Path | None = None,
    letsencrypt_root: Path | None = None,
    hook_root: Path | None = None,
) -> CertificateMaterial:
    normalized = _normalized_domains(domains)
    if spec.method == "existing":
        if spec.fullchain is None or spec.private_key is None:
            raise ValidationError("Укажите fullchain и закрытый ключ существующего сертификата.")
        _validate_certificate(runner, spec.fullchain, spec.private_key, normalized)
        target = install_dir / "certificates"
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        fullchain = target / "fullchain.pem"
        private_key = target / "privkey.pem"
        atomic_copy(spec.fullchain, fullchain, mode=0o600)
        atomic_copy(spec.private_key, private_key, mode=0o600)
        _validate_certificate(runner, fullchain, private_key, normalized)
        return CertificateMaterial(
            host_root=target,
            container_root="/etc/nginx/ssl",
            fullchain="/etc/nginx/ssl/fullchain.pem",
            private_key="/etc/nginx/ssl/privkey.pem",
            managed_by_certbot=False,
        )

    selected_letsencrypt_root = letsencrypt_root or Path("/etc/letsencrypt")
    selected_hook_root = hook_root or (
        _DEFAULT_HOOK_ROOT
        if selected_letsencrypt_root == Path("/etc/letsencrypt")
        else selected_letsencrypt_root / "renewal-hooks"
    )
    selected_credentials_root = credentials_dir or _DEFAULT_CREDENTIALS_ROOT
    hook_paths = _renewal_hook_paths(selected_hook_root)
    _assert_hook_ownership(hook_paths)
    lineage_exists = _lineage_exists(selected_letsencrypt_root, normalized[0])
    if lineage_exists:
        _validate_reusable_lineage(
            runner,
            selected_letsencrypt_root,
            normalized,
            spec,
            selected_credentials_root,
        )
    else:
        _assert_new_lineage(selected_letsencrypt_root, normalized[0])
    _require_certbot(runner, spec.method)
    timer_enablement, timer_active = _certbot_timer_state(runner)
    credentials: Path | None = None
    credential_snapshot: tuple[bytes, int] | None = None
    credential_root_created = False
    if spec.method in {"cloudflare", "gcore"}:
        credential_root_created = not selected_credentials_root.exists()
        credentials = _credentials_path(
            selected_credentials_root,
            provider=spec.method,
            certificate_name=normalized[0],
        )
        credential_snapshot = _snapshot_owned_credentials(credentials)
    transaction = CertificateTransaction(
        certificate_name=normalized[0],
        letsencrypt_root=selected_letsencrypt_root,
        hooks=_snapshot_hooks(hook_paths),
        credentials=credentials,
        credential_snapshot=credential_snapshot,
        credentials_root=selected_credentials_root if credentials is not None else None,
        credentials_root_created=credential_root_created,
        timer_enablement=timer_enablement,
        timer_active=timer_active,
        delete_lineage_on_rollback=not lineage_exists,
    )
    try:
        if credentials is not None:
            _write_credentials(credentials, spec)
        if not lineage_exists:
            command = build_certbot_command(normalized, spec, credentials_file=credentials)
            if selected_letsencrypt_root != Path("/etc/letsencrypt"):
                command += ["--config-dir", str(selected_letsencrypt_root)]
            runner.run(command, timeout=600, sensitive=True)
        live = selected_letsencrypt_root / "live" / normalized[0]
        fullchain = live / "fullchain.pem"
        private_key = live / "privkey.pem"
        _validate_certificate(runner, fullchain, private_key, normalized)
        install_renewal_hooks(
            nginx_container=nginx_container,
            stop_for_standalone=spec.method == "http-01" and stop_nginx_for_http01,
            hook_root=selected_hook_root,
        )
        runner.run(["systemctl", "enable", "--now", "certbot.timer"], timeout=120)
    except BaseException as error:
        try:
            transaction.rollback(runner)
        except BaseException as rollback_error:  # noqa: BLE001 - rollback must survive interrupts
            raise TransactionError(
                "Выпуск сертификата завершился ошибкой, а rollback выполнен не полностью: "
                f"{rollback_error}"
            ) from error
        raise
    return CertificateMaterial(
        host_root=selected_letsencrypt_root,
        container_root="/etc/letsencrypt",
        fullchain=f"/etc/letsencrypt/live/{normalized[0]}/fullchain.pem",
        private_key=f"/etc/letsencrypt/live/{normalized[0]}/privkey.pem",
        managed_by_certbot=True,
        lineage_name=normalized[0],
        credentials_file=credentials,
        transaction=transaction,
    )


def issue_certificate(
    runner: Runner,
    inventory: Inventory,
    domain: str,
    spec: CertificateSpec,
    *,
    wildcard: bool = False,
    credentials_dir: Path = _DEFAULT_CREDENTIALS_ROOT,
    letsencrypt_root: Path = Path("/etc/letsencrypt"),
    hook_root: Path | None = None,
) -> IssuedCertificate:
    """Issue a new, currently unreferenced Certbot lineage without changing nginx."""
    if spec.method == "existing":
        raise ValidationError(
            "Команда issue выпускает только сертификаты Let's Encrypt. "
            "Существующий сертификат выбирается при чистой установке."
        )
    selected_domain = normalize_domain(domain)
    if wildcard and spec.method == "http-01":
        raise ValidationError("Wildcard-сертификат нельзя выпустить через HTTP-01.")
    if spec.method == "cloudflare":
        _validated_dns_token(spec.cloudflare_token, "Cloudflare")
    elif spec.method == "gcore":
        _validated_dns_token(spec.gcore_token, "Gcore")
    domains = (selected_domain, f"*.{selected_domain}") if wildcard else (selected_domain,)
    selected_hook_root = hook_root or letsencrypt_root / "renewal-hooks"
    hook_paths = _renewal_hook_paths(selected_hook_root)
    _assert_hook_ownership(hook_paths)
    _assert_new_lineage(letsencrypt_root, selected_domain)
    _require_certbot(runner, spec.method)
    timer_enablement, timer_active = _certbot_timer_state(runner)
    nginx_container, system_nginx = _nginx_backend(inventory)

    credentials: Path | None = None
    credential_snapshot: tuple[bytes, int] | None = None
    credential_root_created = False
    hook_snapshots = _snapshot_hooks(hook_paths)
    nginx_stopped = False
    try:
        if spec.method in {"cloudflare", "gcore"}:
            credential_root_created = not credentials_dir.exists()
            credentials = _credentials_path(
                credentials_dir,
                provider=spec.method,
                certificate_name=selected_domain,
            )
            credential_snapshot = _snapshot_owned_credentials(credentials)
        if credentials is not None:
            _write_credentials(credentials, spec)
        if spec.method == "http-01":
            nginx_stopped = _stop_nginx_if_active(
                runner,
                nginx_container=nginx_container,
                system_nginx=system_nginx,
            )
        command = build_certbot_command(domains, spec, credentials_file=credentials)
        if letsencrypt_root != Path("/etc/letsencrypt"):
            command += ["--config-dir", str(letsencrypt_root)]
        runner.run(command, timeout=600, sensitive=True)
        if nginx_stopped:
            _start_nginx(
                runner,
                nginx_container=nginx_container,
                system_nginx=system_nginx,
            )
            nginx_stopped = False

        live = letsencrypt_root / "live" / selected_domain
        fullchain = live / "fullchain.pem"
        private_key = live / "privkey.pem"
        _validate_certificate(runner, fullchain, private_key, domains)
        install_renewal_hooks(
            nginx_container=nginx_container,
            stop_for_standalone=_renewal_uses_standalone(letsencrypt_root),
            hook_root=selected_hook_root,
            system_nginx=system_nginx,
        )
        runner.run(["systemctl", "enable", "--now", "certbot.timer"], timeout=120)
    except BaseException as error:
        rollback_errors: list[str] = []
        lineage_retained = False
        if nginx_stopped:
            try:
                _start_nginx(
                    runner,
                    nginx_container=nginx_container,
                    system_nginx=system_nginx,
                )
            except BaseException as rollback_error:  # noqa: BLE001 - continue compensation
                rollback_errors.append(f"nginx: {rollback_error}")
        if _lineage_exists(letsencrypt_root, selected_domain):
            delete_command = [
                "certbot",
                "delete",
                "--cert-name",
                selected_domain,
                "--non-interactive",
            ]
            if letsencrypt_root != Path("/etc/letsencrypt"):
                delete_command += ["--config-dir", str(letsencrypt_root)]
            try:
                result = runner.run(
                    delete_command,
                    check=False,
                    timeout=120,
                    sensitive=True,
                )
                lineage_retained = _lineage_exists(
                    letsencrypt_root, selected_domain
                )
                if lineage_retained and result.returncode != 0:
                    rollback_errors.append("Certbot не удалил незавершённый lineage")
                elif lineage_retained:
                    rollback_errors.append(
                        "Certbot сообщил об удалении, но незавершённый lineage остался"
                    )
            except BaseException as rollback_error:  # noqa: BLE001 - continue compensation
                lineage_retained = _lineage_exists(
                    letsencrypt_root, selected_domain
                )
                if lineage_retained:
                    rollback_errors.append(f"Certbot lineage: {rollback_error}")
        if not lineage_retained:
            try:
                _restore_hooks(hook_snapshots)
            except BaseException as rollback_error:  # noqa: BLE001 - continue compensation
                rollback_errors.append(f"Certbot hooks: {rollback_error}")
        if credentials is not None and not lineage_retained:
            try:
                _restore_credentials(credentials, credential_snapshot)
            except BaseException as rollback_error:  # noqa: BLE001 - continue compensation
                rollback_errors.append(f"учётные данные DNS: {rollback_error}")
        if credential_root_created and not lineage_retained:
            try:
                _remove_empty_credentials_root(credentials_dir)
            except BaseException as rollback_error:  # noqa: BLE001 - continue compensation
                rollback_errors.append(f"каталог учётных данных DNS: {rollback_error}")
        if not lineage_retained:
            try:
                _restore_certbot_timer(
                    runner,
                    enablement=timer_enablement,
                    active=timer_active,
                )
            except BaseException as rollback_error:  # noqa: BLE001 - continue compensation
                rollback_errors.append(f"certbot.timer: {rollback_error}")
        if rollback_errors:
            retained = (
                " Hooks, credential и certbot.timer сохранены для оставшегося lineage."
                if lineage_retained
                else ""
            )
            raise TransactionError(
                "Выпуск сертификата завершился ошибкой, а rollback выполнен не полностью: "
                + "; ".join(rollback_errors)
                + retained
            ) from error
        raise

    return IssuedCertificate(
        certificate_name=selected_domain,
        domains=domains,
        fullchain=fullchain,
        private_key=private_key,
        method=spec.method,
    )


def _write_credentials(path: Path, spec: CertificateSpec) -> None:
    _assert_no_symlink_ancestors(path.parent)
    if path.parent.exists() and not path.parent.is_dir():
        raise ValidationError(
            f"Каталог учётных данных DNS не является каталогом: {path.parent}"
        )
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValidationError(f"Небезопасный путь учётных данных DNS: {path}")
    if path.is_file() and not _is_manager_owned_text(path, _CREDENTIAL_MARKER):
        raise ValidationError(
            f"Файл учётных данных {path} создан не менеджером; перезапись запрещена."
        )
    if spec.method == "cloudflare":
        token = _validated_dns_token(spec.cloudflare_token, "Cloudflare")
        setting = "dns_cloudflare_api_token"
    elif spec.method == "gcore":
        token = _validated_dns_token(spec.gcore_token, "Gcore")
        setting = "dns_gcore_apitoken"
    else:
        raise ValidationError("Учётные данные применимы только для DNS-01.")
    atomic_write_text(
        path,
        f"{_CREDENTIAL_MARKER}\n{setting} = {token}\n",
        mode=0o600,
    )


def _validated_dns_token(value: str | None, provider: str) -> str:
    token = value or ""
    if not 20 <= len(token) <= 4096 or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in token
    ):
        raise ValidationError(f"Некорректный {provider} API Token.")
    return token


def _credentials_path(
    root: Path, *, provider: Literal["cloudflare", "gcore"], certificate_name: str
) -> Path:
    if not root.is_absolute():
        raise ValidationError("Каталог учётных данных DNS должен быть абсолютным путём.")
    _assert_no_symlink_ancestors(root)
    if not root.parent.is_dir():
        raise ValidationError(
            f"Родительский каталог учётных данных DNS не существует: {root.parent}"
        )
    if root.exists() and not root.is_dir():
        raise ValidationError(f"Путь учётных данных DNS не является каталогом: {root}")
    marker = root / ".managed-by-remnawave-manager"
    if root.is_dir():
        info = root.lstat()
        if os.name == "posix" and (
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ValidationError(
                f"Каталог {root} должен принадлежать root и иметь права 0700."
            )
        marker_snapshot = _manager_owned_snapshot(
            marker,
            _CREDENTIAL_MARKER,
            private=True,
        )
        if marker_snapshot is None or marker_snapshot.data != (
            _CREDENTIAL_MARKER + "\n"
        ).encode():
            raise ValidationError(f"Маркер каталога {root} повреждён.")
    else:
        root.mkdir(mode=0o700)
        atomic_write_text(marker, _CREDENTIAL_MARKER + "\n", mode=0o600)
    try:
        root.chmod(0o700)
    except OSError as error:
        raise ValidationError(f"Не удалось ограничить права каталога {root}.") from error
    return root / f"{provider}-{certificate_name}.ini"


def _remove_empty_credentials_root(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise ValidationError(f"Небезопасный каталог учётных данных DNS: {root}")
    marker = root / ".managed-by-remnawave-manager"
    entries = list(root.iterdir())
    if entries == [marker] or set(entries) == {marker}:
        marker_snapshot = _manager_owned_snapshot(
            marker,
            _CREDENTIAL_MARKER,
            private=True,
        )
        if marker_snapshot is None or marker_snapshot.data != (
            _CREDENTIAL_MARKER + "\n"
        ).encode():
            raise ValidationError(f"Маркер каталога {root} изменён во время rollback.")
        marker.unlink()
        root.rmdir()
    elif entries:
        raise ValidationError(
            f"Каталог {root} получил посторонние файлы во время rollback; удаление запрещено."
        )
    else:
        root.rmdir()


def _snapshot_owned_credentials(path: Path) -> tuple[bytes, int] | None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValidationError(f"Небезопасный путь учётных данных DNS: {path}")
    if not path.is_file():
        return None
    snapshot = _manager_owned_snapshot(path, _CREDENTIAL_MARKER, private=True)
    if snapshot is None:
        raise ValidationError(
            f"Файл учётных данных {path} создан не менеджером; перезапись запрещена."
        )
    return snapshot.data, snapshot.mode


def _restore_credentials(path: Path, snapshot: tuple[bytes, int] | None) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValidationError(f"Небезопасный путь учётных данных DNS: {path}")
    if snapshot is None:
        if path.is_file() and not _is_manager_owned_text(
            path, _CREDENTIAL_MARKER, private=True
        ):
            raise ValidationError(
                f"Файл учётных данных {path} изменён не менеджером во время rollback."
            )
        path.unlink(missing_ok=True)
    else:
        payload, mode = snapshot
        atomic_write_bytes(path, payload, mode=mode)


def _assert_no_symlink_ancestors(path: Path) -> None:
    selected = path.absolute()
    for current in (selected, *selected.parents):
        if current.is_symlink():
            raise ValidationError(
                f"Путь {path} содержит символьную ссылку {current}; операция запрещена."
            )


def _is_manager_owned_text(
    path: Path,
    marker: str,
    *,
    private: bool = False,
) -> bool:
    return _manager_owned_snapshot(path, marker, private=private) is not None


def _manager_owned_snapshot(
    path: Path,
    marker: str,
    *,
    private: bool = False,
) -> RegularFileSnapshot | None:
    try:
        snapshot = read_stable_regular_file(
            path,
            max_size=_MAX_CERTBOT_TEXT_SIZE,
            label="Файл Certbot",
        )
    except ValidationError:
        return None
    if os.name == "posix" and (
        snapshot.uid != os.geteuid()
        or snapshot.mode & (0o077 if private else 0o022)
    ):
        return None
    try:
        text = snapshot.data.decode("utf-8", errors="strict")
    except UnicodeError:
        return None
    return snapshot if marker in text.splitlines() else None


def _read_renewal_snapshot(path: Path) -> RegularFileSnapshot:
    snapshot = read_stable_regular_file(
        path,
        max_size=_MAX_CERTBOT_TEXT_SIZE,
        label="Renewal-конфигурация Certbot",
    )
    if os.name == "posix" and (
        snapshot.uid != os.geteuid() or snapshot.mode & 0o022
    ):
        raise ValidationError(
            f"Renewal-конфигурация Certbot должна принадлежать root и не быть "
            f"доступной для записи группе/прочим: {path}"
        )
    return snapshot


def _read_renewal_text(path: Path) -> tuple[str, int]:
    snapshot = _read_renewal_snapshot(path)
    try:
        return snapshot.data.decode("utf-8"), snapshot.mode
    except UnicodeError as error:
        raise ValidationError(
            f"Некорректная renewal-конфигурация Certbot: {path}"
        ) from error


def _read_nginx_config_text(path: Path) -> str:
    snapshot = read_stable_regular_file(
        path,
        max_size=_MAX_NGINX_CONFIG_SIZE,
        label="Конфигурация nginx",
    )
    return snapshot.data.decode("utf-8", errors="replace")


def _validate_reusable_lineage(
    runner: Runner,
    letsencrypt_root: Path,
    domains: Sequence[str],
    spec: CertificateSpec,
    credentials_root: Path,
) -> None:
    certificate_name = domains[0]
    renewal = letsencrypt_root / "renewal" / f"{certificate_name}.conf"
    if renewal.is_symlink() or not renewal.is_file():
        raise ValidationError(
            f"Существующий Certbot lineage {certificate_name} не имеет безопасной renewal-конфигурации."
        )
    authenticator, _ = _read_renewal_config(renewal)
    expected_authenticator = {
        "http-01": "standalone",
        "cloudflare": "dns-cloudflare",
        "gcore": "dns-gcore",
    }[spec.method]
    if authenticator != expected_authenticator:
        raise ValidationError(
            f"Certbot lineage {certificate_name} использует {authenticator}, "
            f"а запрошен {expected_authenticator}; автоматическая смена provider запрещена."
        )
    if spec.method in {"cloudflare", "gcore"}:
        option = f"dns_{spec.method}_credentials"
        configured = _read_renewal_option(renewal, option)
        expected = credentials_root / f"{spec.method}-{certificate_name}.ini"
        if configured is None or Path(configured) != expected:
            raise ValidationError(
                f"Certbot lineage {certificate_name} ссылается не на стабильный manager credential {expected}. "
                "Автоматическое переиспользование запрещено."
            )

    live = letsencrypt_root / "live" / certificate_name
    fullchain = live / "fullchain.pem"
    private_key = live / "privkey.pem"
    _validate_certificate(runner, fullchain, private_key, domains)
    result = runner.run(
        ["openssl", "x509", "-in", str(fullchain), "-noout", "-ext", "subjectAltName"],
        check=False,
        sensitive=True,
    )
    if result.returncode != 0:
        raise ValidationError(
            f"Не удалось прочитать SAN существующего сертификата {certificate_name}."
        )
    certificate_domains = {
        _normalize_certificate_domain(item)
        for item in re.findall(r"DNS:([^,\s]+)", result.stdout, flags=re.IGNORECASE)
    }
    if certificate_domains != set(domains):
        raise ValidationError(
            f"Домены существующего Certbot lineage {certificate_name} не совпадают с запрошенными."
        )


def _normalize_certificate_domain(value: str) -> str:
    selected = value.strip().rstrip(".")
    if selected.startswith("*."):
        return "*." + normalize_domain(selected[2:])
    return normalize_domain(selected)


def _read_renewal_option(path: Path, option: str) -> str | None:
    parser = ConfigParser(interpolation=None, strict=False)
    try:
        text, _ = _read_renewal_text(path)
        parser.read_string("[certificate]\n" + text)
        value = parser.get("renewalparams", option, fallback="").strip()
    except (ConfigParserError, KeyError) as error:
        raise ValidationError(f"Некорректная renewal-конфигурация Certbot: {path}") from error
    return value or None


def _assert_new_lineage(letsencrypt_root: Path, certificate_name: str) -> None:
    _assert_no_symlink_ancestors(letsencrypt_root)
    candidates = (
        letsencrypt_root / "live" / certificate_name,
        letsencrypt_root / "archive" / certificate_name,
        letsencrypt_root / "renewal" / f"{certificate_name}.conf",
    )
    for path in candidates:
        if path.is_symlink():
            raise ValidationError(
                f"Путь Certbot lineage {path} является символьной ссылкой; выпуск запрещён."
            )
        if path.exists():
            raise ValidationError(
                f"Certbot lineage {certificate_name} уже существует. "
                "Используйте rwm certificate renew; issue не перезаписывает сертификаты."
            )


def _lineage_exists(letsencrypt_root: Path, certificate_name: str) -> bool:
    return any(
        path.exists() or path.is_symlink()
        for path in (
            letsencrypt_root / "live" / certificate_name,
            letsencrypt_root / "archive" / certificate_name,
            letsencrypt_root / "renewal" / f"{certificate_name}.conf",
        )
    )


def _certbot_timer_enablement(runner: Runner) -> CertbotTimerEnablement:
    enabled = runner.run(
        ["systemctl", "is-enabled", "certbot.timer"],
        check=False,
        timeout=30,
    )
    enabled_text = enabled.stdout.strip().lower()
    supported_enablement = {
        "enabled",
        "enabled-runtime",
        "disabled",
        "masked",
        "masked-runtime",
        "static",
        "indirect",
    }
    enabled_code_valid = (
        enabled_text in {"enabled", "enabled-runtime"}
        and enabled.returncode == 0
        or enabled_text not in {"enabled", "enabled-runtime"}
        and enabled.returncode in {0, 1}
    )
    if enabled_text not in supported_enablement or not enabled_code_valid:
        raise TransactionError("Не удалось определить состояние автозапуска certbot.timer.")
    return cast(CertbotTimerEnablement, enabled_text)


def _certbot_timer_active(runner: Runner) -> bool:
    active = runner.run(
        ["systemctl", "is-active", "certbot.timer"],
        check=False,
        timeout=30,
    )
    active_text = active.stdout.strip().lower()
    if active.returncode == 0 and active_text == "active":
        return True
    if active.returncode == 3 and active_text in {"inactive", "failed"}:
        return False
    raise TransactionError("Не удалось определить активность certbot.timer.")


def _certbot_timer_state(
    runner: Runner,
) -> tuple[CertbotTimerEnablement, bool]:
    return _certbot_timer_enablement(runner), _certbot_timer_active(runner)


def _restore_certbot_timer(
    runner: Runner,
    *,
    enablement: CertbotTimerEnablement,
    active: bool,
) -> None:
    errors: list[str] = []

    def run_step(label: str, command: list[str]) -> None:
        try:
            runner.run(command, timeout=120)
        except BaseException as error:  # noqa: BLE001 - continue independent compensation
            errors.append(f"{label}: {str(error) or type(error).__name__}")

    run_step(
        "runtime unmask",
        ["systemctl", "unmask", "--runtime", "certbot.timer"],
    )
    run_step("persistent unmask", ["systemctl", "unmask", "certbot.timer"])
    run_step(
        "active-state",
        ["systemctl", "start" if active else "stop", "certbot.timer"],
    )
    if enablement == "enabled-runtime":
        run_step(
            "persistent enablement cleanup",
            ["systemctl", "disable", "certbot.timer"],
        )
        run_step(
            "runtime enablement",
            ["systemctl", "enable", "--runtime", "certbot.timer"],
        )
    elif enablement == "enabled":
        run_step("enablement", ["systemctl", "enable", "certbot.timer"])
    else:
        run_step(
            "enablement cleanup",
            ["systemctl", "disable", "certbot.timer"],
        )
    if enablement == "masked-runtime":
        run_step(
            "runtime mask",
            ["systemctl", "mask", "--runtime", "certbot.timer"],
        )
    elif enablement == "masked":
        run_step("mask", ["systemctl", "mask", "certbot.timer"])

    restored_enablement: CertbotTimerEnablement | None = None
    restored_active: bool | None = None
    try:
        restored_enablement = _certbot_timer_enablement(runner)
    except BaseException as error:  # noqa: BLE001 - verify active independently
        errors.append(
            f"enablement verification: {str(error) or type(error).__name__}"
        )
    try:
        restored_active = _certbot_timer_active(runner)
    except BaseException as error:  # noqa: BLE001 - report both verification failures
        errors.append(f"active verification: {str(error) or type(error).__name__}")
    if restored_enablement is not None and restored_enablement != enablement:
        errors.append(
            f"enablement {restored_enablement!r} вместо {enablement!r}"
        )
    if restored_active is not None and restored_active != active:
        errors.append(f"active-state {restored_active!r} вместо {active!r}")
    if errors:
        raise TransactionError(
            "certbot.timer не вернулся в исходное enabled/active-состояние: "
            + "; ".join(errors)
        )


def _nginx_backend(inventory: Inventory) -> tuple[str | None, bool]:
    nginx = inventory.components.get("nginx")
    if nginx is not None:
        container = nginx.container or nginx.service
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", container):
            raise ValidationError("Некорректное имя nginx-контейнера в inventory.")
        return container, False
    if inventory.webserver == "nginx":
        return None, True
    raise ValidationError("В inventory не найден управляемый nginx.")


def _stop_nginx_if_active(
    runner: Runner,
    *,
    nginx_container: str | None,
    system_nginx: bool,
) -> bool:
    if system_nginx:
        result = runner.run(
            ["systemctl", "is-active", "--quiet", "nginx"],
            check=False,
            timeout=30,
        )
        if result.returncode == 3:
            return False
        if result.returncode != 0:
            raise TransactionError("Не удалось определить состояние системного nginx.")
        runner.run(["systemctl", "stop", "nginx"], timeout=120)
        return True
    if nginx_container is None:  # pragma: no cover - guarded by _nginx_backend
        raise ValidationError("Не определён nginx-контейнер.")
    result = runner.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", nginx_container],
        check=False,
        timeout=30,
    )
    state = result.stdout.strip().lower()
    if result.returncode != 0 or state not in {"true", "false"}:
        raise TransactionError("Не удалось определить состояние nginx-контейнера.")
    if state == "false":
        return False
    runner.run(["docker", "stop", nginx_container], timeout=120)
    return True


def _start_nginx(
    runner: Runner,
    *,
    nginx_container: str | None,
    system_nginx: bool,
) -> None:
    if system_nginx:
        runner.run(["systemctl", "start", "nginx"], timeout=120)
    elif nginx_container is not None:
        runner.run(["docker", "start", nginx_container], timeout=120)
    else:  # pragma: no cover - guarded by _nginx_backend
        raise ValidationError("Не определён nginx-контейнер.")


def _renewal_uses_standalone(letsencrypt_root: Path) -> bool:
    renewal_dir = letsencrypt_root / "renewal"
    if not renewal_dir.is_dir():
        return False
    for path in renewal_dir.glob("*.conf"):
        if path.is_symlink() or not path.is_file():
            raise ValidationError(f"Небезопасная renewal-конфигурация Certbot: {path}")
        authenticator, _ = _read_renewal_config(path)
        if authenticator == "standalone":
            return True
    return False


def _assert_hook_ownership(paths: Sequence[Path]) -> None:
    for path in paths:
        _assert_no_symlink_ancestors(path.parent)
        if path.is_symlink():
            raise ValidationError(
                f"Certbot hook {path} является символьной ссылкой; изменение запрещено."
            )
        if path.exists() and not path.is_file():
            raise ValidationError(f"Путь Certbot hook {path} не является обычным файлом.")
        if path.is_file() and not _is_manager_owned_text(path, _HOOK_MARKER):
            raise ValidationError(
                f"Certbot hook {path} создан не менеджером; автоматическое изменение запрещено."
            )


def install_renewal_hooks(
    *,
    nginx_container: str | None,
    stop_for_standalone: bool,
    hook_root: Path = _DEFAULT_HOOK_ROOT,
    system_nginx: bool = False,
) -> None:
    if system_nginx and nginx_container is not None:
        raise ValidationError(
            "Нельзя одновременно выбрать системный nginx и nginx-контейнер."
        )
    if not system_nginx and (
        nginx_container is None
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", nginx_container)
    ):
        raise ValidationError("Некорректное имя nginx-контейнера.")
    deploy, pre, post = _renewal_hook_paths(hook_root)
    _assert_hook_ownership((deploy, pre, post))
    hook_lock_prelude = (
        "set -eu\n"
        "umask 077\n"
        f'lock_dir="{_CERTBOT_LOCK_DIR}"\n'
        f'lock_file="{_CERTBOT_LOCK_PATH}"\n'
        '/usr/bin/install -d -m 700 "$lock_dir"\n'
        f'if [ "${{{CERTBOT_MANAGER_LOCK_HELD_ENV}:-}}" != "1" ]; then\n'
        '    exec 9>>"$lock_file"\n'
        "    /usr/bin/flock -w 120 9\n"
        "fi\n"
    )
    if system_nginx:
        deploy_script = (
            "#!/bin/sh\n"
            f"{_HOOK_MARKER}\n"
            f"{hook_lock_prelude}"
            "if /usr/bin/systemctl is-active --quiet nginx; then\n"
            "    /usr/sbin/nginx -t >/dev/null\n"
            "    /usr/bin/systemctl reload nginx >/dev/null\n"
            "fi\n"
        )
    else:
        docker = "/usr/bin/docker --host=unix:///run/docker.sock"
        deploy_script = (
            "#!/bin/sh\n"
            f"{_HOOK_MARKER}\n"
            f"{hook_lock_prelude}"
            f"running=\"$({docker} inspect -f '{{{{.State.Running}}}}' {nginx_container})\"\n"
            "if [ \"$running\" = true ]; then\n"
            f"    {docker} exec {nginx_container} nginx -t >/dev/null\n"
            f"    {docker} exec {nginx_container} nginx -s reload >/dev/null\n"
            "fi\n"
        )
    atomic_write_text(
        deploy,
        deploy_script,
        mode=0o700,
    )
    if not stop_for_standalone:
        pre.unlink(missing_ok=True)
        post.unlink(missing_ok=True)
        return
    hook_prelude = (
        "#!/bin/sh\n"
        f"{_HOOK_MARKER}\n"
        f"{hook_lock_prelude}"
        f'marker="{_CERTBOT_MARKER_ROOT}/{_CERTBOT_MARKER_PREFIX}${{PPID}}"\n'
    )
    if system_nginx:
        pre_script = (
            hook_prelude
            + "printf '%s\\n' inactive > \"$marker\"\n"
            + "if /usr/bin/systemctl is-active --quiet nginx; then\n"
            + "    printf '%s\\n' restart > \"$marker\"\n"
            + "    /usr/bin/systemctl stop nginx >/dev/null\n"
            + "fi\n"
        )
        post_script = (
            hook_prelude
            + 'if [ -f "$marker" ]; then\n'
            + '    marker_state="$(/usr/bin/cat -- "$marker")"\n'
            + '    case "$marker_state" in\n'
            + "        restart) /usr/bin/systemctl start nginx >/dev/null ;;\n"
            + "        inactive) ;;\n"
            + "        *) exit 1 ;;\n"
            + "    esac\n"
            + '    rm -f "$marker"\n'
            + "fi\n"
        )
    else:
        pre_script = (
            hook_prelude
            + "printf '%s\\n' inactive > \"$marker\"\n"
            + f"running=\"$({docker} inspect -f '{{{{.State.Running}}}}' {nginx_container})\"\n"
            + 'case "$running" in\n'
            + "    true)\n"
            + "        printf '%s\\n' restart > \"$marker\"\n"
            + f"        {docker} stop {nginx_container} >/dev/null\n"
            + "        ;;\n"
            + "    false) ;;\n"
            + "    *) exit 1 ;;\n"
            + "esac\n"
        )
        post_script = (
            hook_prelude
            + 'if [ -f "$marker" ]; then\n'
            + '    marker_state="$(/usr/bin/cat -- "$marker")"\n'
            + '    case "$marker_state" in\n'
            + f"        restart) {docker} start {nginx_container} >/dev/null ;;\n"
            + "        inactive) ;;\n"
            + "        *) exit 1 ;;\n"
            + "    esac\n"
            + '    rm -f "$marker"\n'
            + "fi\n"
        )
    atomic_write_text(
        pre,
        pre_script,
        mode=0o700,
    )
    atomic_write_text(
        post,
        post_script,
        mode=0o700,
    )


def assert_no_active_certbot_renewal(
    *,
    marker_root: Path = Path(_CERTBOT_MARKER_ROOT),
) -> None:
    if marker_root.is_symlink() or (
        marker_root.exists() and not marker_root.is_dir()
    ):
        raise ValidationError(
            f"Каталог marker-файлов Certbot имеет небезопасный тип: {marker_root}"
        )
    if not marker_root.is_dir():
        return
    try:
        markers = sorted(marker_root.glob(f"{_CERTBOT_MARKER_PREFIX}*"))
    except OSError as error:
        raise ValidationError(
            f"Не удалось проверить marker-файлы Certbot в {marker_root}: {error}"
        ) from error
    for marker in markers:
        suffix = marker.name.removeprefix(_CERTBOT_MARKER_PREFIX)
        if not re.fullmatch(r"[1-9][0-9]*", suffix):
            raise ValidationError(
                f"Обнаружен marker Certbot с неожиданным именем: {marker}. "
                "Проверьте его вручную."
            )
        try:
            info = marker.lstat()
        except OSError as error:
            raise ValidationError(
                f"Не удалось проверить marker Certbot {marker}: {error}"
            ) from error
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValidationError(
                f"Marker Certbot имеет небезопасный тип: {marker}."
            )
        if os.name == "posix" and (
            info.st_uid != 0 or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ValidationError(
                f"Marker Certbot должен принадлежать root и иметь приватные права: {marker}."
            )
        pid = int(suffix)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            raise ValidationError(
                f"Обнаружен stale marker незавершённого Certbot renewal: {marker}. "
                "Проверьте состояние nginx и удалите marker вручную."
            ) from None
        except PermissionError:
            pass
        except OSError as error:
            raise ValidationError(
                f"Не удалось проверить процесс Certbot из marker {marker}: {error}"
            ) from error
        raise ValidationError(
            f"Сейчас выполняется Certbot renewal (marker {marker}); "
            "повторите изменяющую команду после его завершения."
        )


def discover_certbot_renewal(
    compose: dict[str, Any],
    nginx_files: Sequence[Path],
    *,
    letsencrypt_root: Path = Path("/etc/letsencrypt"),
) -> CertbotRenewalPlan:
    """Find Certbot certificates actually exposed to the managed stack."""
    root = _posix_path(letsencrypt_root)
    certificate_names: set[str] = set()
    certbot_mounts: list[tuple[str, str]] = []
    for service in compose.get("services", {}).values():
        if not isinstance(service, dict):
            continue
        for volume in service.get("volumes", []) or []:
            if not isinstance(volume, dict) or volume.get("type") != "bind":
                continue
            source = _normalized_path_text(volume.get("source"))
            target = _normalized_path_text(volume.get("target"))
            if not source or not target or not _within_text_path(source, root):
                continue
            certbot_mounts.append((source, target))
            relative = source[len(root) :].strip("/")
            parts = relative.split("/") if relative else []
            if len(parts) >= 2 and parts[0] == "live":
                certificate_names.add(parts[1])

    nginx_text = "\n".join(_read_nginx_config_text(path) for path in nginx_files)
    certificate_names.update(_names_below_mount(nginx_text, root, "live"))
    for source, target in certbot_mounts:
        relative = source[len(root) :].strip("/")
        parts = relative.split("/") if relative else []
        if not parts:
            certificate_names.update(_names_below_mount(nginx_text, target, "live"))
        elif parts == ["live"]:
            certificate_names.update(_names_below_mount(nginx_text, target, None))

    renewal_dir = letsencrypt_root / "renewal"
    broad_mount = any(
        source in {root, root.rstrip("/") + "/live"}
        for source, _ in certbot_mounts
    )
    if broad_mount and not certificate_names and renewal_dir.is_dir():
        certificate_names.update(path.stem for path in renewal_dir.glob("*.conf"))

    missing: list[str] = []
    authenticators: list[tuple[str, str]] = []
    legacy_hooks: list[str] = []
    for name in sorted(certificate_names):
        if not re.fullmatch(r"[A-Za-z0-9*_.-]+", name):
            raise ValidationError(f"Некорректное имя Certbot-сертификата: {name}")
        renewal = renewal_dir / f"{name}.conf"
        if renewal.is_symlink():
            raise ValidationError(
                f"Renewal-конфигурация {renewal} является символьной ссылкой; принятие запрещено."
            )
        if not renewal.is_file():
            missing.append(name)
            continue
        authenticator, renew_hook = _read_renewal_config(renewal)
        authenticators.append((name, authenticator))
        if renew_hook and _is_legacy_reverse_proxy_hook(renew_hook):
            legacy_hooks.append(name)
    return CertbotRenewalPlan(
        certificate_names=tuple(sorted(certificate_names)),
        missing_renewal_configs=tuple(missing),
        authenticators=tuple(authenticators),
        legacy_renew_hooks=tuple(legacy_hooks),
    )


def _snapshot_inventory_file(path: Path) -> tuple[bytes, int] | None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValidationError(f"Небезопасный путь inventory: {path}")
    if not path.exists():
        return None
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValidationError(
            f"Не удалось безопасно открыть inventory {path}."
        ) from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValidationError(f"Inventory имеет небезопасный тип: {path}")
        if os.name == "posix" and (
            info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ValidationError(
                f"Inventory должен принадлежать root и иметь права 0600: {path}"
            )
        if info.st_size > _MAX_INVENTORY_SIZE:
            raise ValidationError(f"Inventory превышает допустимый размер: {path}")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            payload = stream.read(_MAX_INVENTORY_SIZE + 1)
        if len(payload) > _MAX_INVENTORY_SIZE:
            raise ValidationError(f"Inventory превышает допустимый размер: {path}")
        return payload, stat.S_IMODE(info.st_mode)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _restore_inventory_file(
    path: Path,
    snapshot: tuple[bytes, int] | None,
) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValidationError(
            f"Inventory изменил тип во время rollback: {path}"
        )
    if path.is_file():
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValidationError(
                f"Inventory изменил тип во время rollback: {path}"
            )
        if os.name == "posix" and info.st_uid != os.geteuid():
            raise ValidationError(
                f"Inventory изменил владельца во время rollback: {path}"
            )
    if snapshot is None:
        path.unlink(missing_ok=True)
        if os.name == "posix" and path.parent.is_dir():
            descriptor = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return
    atomic_write_bytes(path, snapshot[0], mode=snapshot[1])


def _save_inventory_only(store: StateStore, inventory: Inventory) -> None:
    snapshot = _snapshot_inventory_file(store.paths.inventory)
    try:
        store.save_inventory(inventory)
    except BaseException as error:
        try:
            _restore_inventory_file(store.paths.inventory, snapshot)
        except BaseException as rollback_error:  # noqa: BLE001 - rollback must survive interrupts
            raise TransactionError(
                "Запись inventory завершилась ошибкой, rollback inventory неполон: "
                f"{rollback_error}"
            ) from error
        raise


def configure_adopted_certbot(
    runner: Runner,
    inventory: Inventory,
    compose: dict[str, Any],
    *,
    store: StateStore | None = None,
    letsencrypt_root: Path = Path("/etc/letsencrypt"),
    hook_root: Path | None = None,
) -> CertbotRenewalPlan:
    plan = discover_certbot_renewal(
        compose,
        [Path(item) for item in inventory.nginx_files],
        letsencrypt_root=letsencrypt_root,
    )
    if not plan.detected:
        if store is not None:
            _save_inventory_only(store, inventory)
        return plan
    if plan.missing_renewal_configs:
        names = ", ".join(plan.missing_renewal_configs)
        raise ValidationError(
            "Сертификаты смонтированы из /etc/letsencrypt, но renewal-конфигурации "
            f"не найдены: {names}. Сначала восстановите структуру Certbot."
        )
    nginx = inventory.components.get("nginx")
    system_nginx = False
    nginx_container: str | None = None
    if nginx is not None:
        nginx_container = nginx.container or nginx.service
    elif inventory.webserver == "nginx":
        referenced = _system_nginx_certificate_names(inventory, letsencrypt_root)
        if set(plan.certificate_names).issubset(referenced):
            system_nginx = True
    if nginx_container is None and not system_nginx:
        raise ValidationError(
            "Обнаружены сертификаты Certbot, но не найдены ни nginx-контейнер в Compose, "
            "ни подтверждённая системная конфигурация nginx с этими сертификатами. "
            "Автоматическая настройка renewal hooks остановлена."
        )
    selected_hook_root = hook_root or letsencrypt_root / "renewal-hooks"
    hook_paths = _renewal_hook_paths(selected_hook_root)
    snapshots = _snapshot_hooks(hook_paths)
    renewal_snapshots = _snapshot_renewal_configs(
        letsencrypt_root, plan.legacy_renew_hooks
    )
    timer_enablement, timer_active = _certbot_timer_state(runner)
    inventory_snapshot = (
        _snapshot_inventory_file(store.paths.inventory)
        if store is not None
        else None
    )
    features_snapshot = dict(inventory.features)
    inventory_write_attempted = False
    previous_crontab: str | None = None
    cron_changed = False
    try:
        install_renewal_hooks(
            nginx_container=nginx_container,
            stop_for_standalone=plan.uses_standalone,
            hook_root=selected_hook_root,
            system_nginx=system_nginx,
        )
        for path in renewal_snapshots:
            _remove_legacy_renew_hook(path)
        previous_crontab, replacement_crontab = _legacy_certbot_cron_update(
            runner
        )
        if replacement_crontab is not None:
            cron_changed = True
            runner.run(
                ["crontab", "-u", "root", "-"],
                input_text=replacement_crontab,
                timeout=30,
            )
        runner.run(["systemctl", "enable", "--now", "certbot.timer"], timeout=120)
        inventory.features["certbot_renewal"] = True
        inventory.features["certbot_standalone"] = plan.uses_standalone
        inventory.features["certbot_legacy_renew_hook_removed"] = bool(
            plan.legacy_renew_hooks
        )
        inventory.features["certbot_legacy_cron_removed"] = cron_changed
        if store is not None:
            inventory_write_attempted = True
            store.save_inventory(inventory)
    except BaseException as error:
        rollback_errors: list[str] = []
        inventory.features.clear()
        inventory.features.update(features_snapshot)
        try:
            _restore_hooks(snapshots)
        except BaseException as rollback_error:  # noqa: BLE001 - continue compensation
            rollback_errors.append(f"Certbot hooks: {rollback_error}")
        try:
            _restore_regular_files(renewal_snapshots)
        except BaseException as rollback_error:  # noqa: BLE001 - continue compensation
            rollback_errors.append(f"renewal-конфигурации: {rollback_error}")
        if cron_changed and previous_crontab is not None:
            try:
                runner.run(
                    ["crontab", "-u", "root", "-"],
                    input_text=previous_crontab,
                    timeout=30,
                )
            except BaseException as rollback_error:  # noqa: BLE001 - continue compensation
                rollback_errors.append(f"root crontab: {rollback_error}")
        try:
            _restore_certbot_timer(
                runner,
                enablement=timer_enablement,
                active=timer_active,
            )
        except BaseException as rollback_error:  # noqa: BLE001 - continue compensation
            rollback_errors.append(f"certbot.timer: {rollback_error}")
        if store is not None and inventory_write_attempted:
            try:
                _restore_inventory_file(
                    store.paths.inventory,
                    inventory_snapshot,
                )
            except BaseException as rollback_error:  # noqa: BLE001 - continue compensation
                rollback_errors.append(f"inventory: {rollback_error}")
        if rollback_errors:
            raise TransactionError(
                "Настройка Certbot renewal завершилась ошибкой, rollback неполон: "
                + "; ".join(rollback_errors)
            ) from error
        raise
    return plan


def _system_nginx_certificate_names(
    inventory: Inventory, letsencrypt_root: Path
) -> set[str]:
    root = _posix_path(letsencrypt_root)
    names: set[str] = set()
    for value in inventory.nginx_files:
        path = Path(value)
        text = _read_nginx_config_text(path)
        names.update(_names_below_mount(text, root, "live"))
    return names


def _renewal_hook_paths(hook_root: Path) -> tuple[Path, Path, Path]:
    return (
        hook_root / "deploy" / "remnawave-manager-nginx",
        hook_root / "pre" / "remnawave-manager-nginx",
        hook_root / "post" / "remnawave-manager-nginx",
    )


def _snapshot_hooks(paths: Sequence[Path]) -> dict[Path, tuple[bytes, int] | None]:
    _assert_hook_ownership(paths)
    snapshots: dict[Path, tuple[bytes, int] | None] = {}
    for path in paths:
        if not path.is_file():
            snapshots[path] = None
            continue
        snapshot = _manager_owned_snapshot(path, _HOOK_MARKER)
        if snapshot is None:
            raise ValidationError(
                f"Certbot hook {path} изменился во время создания snapshot."
            )
        snapshots[path] = (snapshot.data, snapshot.mode)
    return snapshots


def _restore_hooks(snapshots: dict[Path, tuple[bytes, int] | None]) -> None:
    _assert_hook_ownership(tuple(snapshots))
    for path, snapshot in snapshots.items():
        if snapshot is None:
            path.unlink(missing_ok=True)
        else:
            payload, mode = snapshot
            atomic_write_bytes(path, payload, mode=mode)


def _snapshot_renewal_configs(
    letsencrypt_root: Path, names: Sequence[str]
) -> dict[Path, tuple[bytes, int]]:
    snapshots: dict[Path, tuple[bytes, int]] = {}
    for name in names:
        path = letsencrypt_root / "renewal" / f"{name}.conf"
        snapshot = _read_renewal_snapshot(path)
        snapshots[path] = (snapshot.data, snapshot.mode)
    return snapshots


def _restore_regular_files(snapshots: dict[Path, tuple[bytes, int]]) -> None:
    for path, (payload, mode) in snapshots.items():
        _read_renewal_snapshot(path)
        atomic_write_bytes(path, payload, mode=mode)


def _remove_legacy_renew_hook(path: Path) -> None:
    original, mode = _read_renewal_text(path)
    output: list[str] = []
    section = ""
    removed = False
    for line in original.splitlines(keepends=True):
        header = re.fullmatch(r"\s*\[([^]]+)]\s*(?:\r?\n)?", line)
        if header:
            section = header.group(1).strip().lower()
        option = re.fullmatch(r"\s*renew_hook\s*=\s*(.*?)\s*(?:\r?\n)?", line)
        if (
            section == "renewalparams"
            and option
            and _is_legacy_reverse_proxy_hook(option.group(1))
        ):
            removed = True
            continue
        output.append(line)
    if not removed:
        raise ValidationError(
            f"Legacy renew_hook в {path} изменился после проверки; миграция остановлена."
        )
    atomic_write_text(path, "".join(output), mode=mode)


def _is_legacy_reverse_proxy_hook(value: str) -> bool:
    normalized = " ".join(value.split())
    return bool(
        re.fullmatch(
            r"sh -c (?P<quote>['\"])cd /opt/(?:remnawave|remnanode) && "
            r"(?:docker compose down remnawave-nginx && )?"
            r"docker compose up -d remnawave-nginx"
            r"(?: && docker compose exec remnawave-nginx nginx -s reload)?"
            r"(?P=quote)",
            normalized,
        )
    )


def _migrate_legacy_certbot_cron(runner: Runner) -> tuple[str | None, bool]:
    original, replacement = _legacy_certbot_cron_update(runner)
    if replacement is None:
        return original, False
    runner.run(
        ["crontab", "-u", "root", "-"],
        input_text=replacement,
        timeout=30,
    )
    return original, True


def _legacy_certbot_cron_update(
    runner: Runner,
) -> tuple[str | None, str | None]:
    if not command_exists("crontab"):
        return None, None
    result = runner.run(["crontab", "-u", "root", "-l"], check=False, timeout=30)
    if result.returncode != 0:
        return None, None
    original = result.stdout
    lines = original.splitlines(keepends=True)
    selected = [line for line in lines if not _is_legacy_certbot_cron(line)]
    if selected == lines:
        return original, None
    return original, "".join(selected)


def _is_legacy_certbot_cron(line: str) -> bool:
    return "/usr/bin/certbot renew" in line and _LEGACY_CRON_LOG in line


def _read_renewal_config(path: Path) -> tuple[str, str | None]:
    parser = ConfigParser(interpolation=None, strict=False)
    try:
        text, _ = _read_renewal_text(path)
        parser.read_string("[certificate]\n" + text)
        authenticator = parser.get("renewalparams", "authenticator").strip().lower()
        renew_hook = parser.get("renewalparams", "renew_hook", fallback="").strip() or None
    except (ConfigParserError, KeyError) as error:
        raise ValidationError(f"Некорректная renewal-конфигурация Certbot: {path}") from error
    if not re.fullmatch(r"[a-z0-9_-]+", authenticator):
        raise ValidationError(f"Некорректный authenticator в {path}.")
    return authenticator, renew_hook


def _normalized_path_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    selected = posixpath.normpath(value.strip().replace("\\", "/"))
    return selected.rstrip("/") or "/"


def _posix_path(path: Path) -> str:
    return path.as_posix().rstrip("/") or "/"


def _within_text_path(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent + "/")


def _names_below_mount(text: str, mount: str, middle: str | None) -> set[str]:
    prefix = re.escape(mount.rstrip("/"))
    if middle:
        prefix += "/" + re.escape(middle)
    return set(
        re.findall(prefix + r"/([A-Za-z0-9*_.-]+)(?:/|$)", text)
    )


def _normalized_domains(domains: Sequence[str]) -> list[str]:
    normalized_values: list[str] = []
    for item in domains:
        selected = item.strip().rstrip(".")
        if selected.startswith("*."):
            normalized_values.append("*." + normalize_domain(selected[2:]))
        else:
            normalized_values.append(normalize_domain(selected))
    normalized = list(dict.fromkeys(normalized_values))
    if not normalized:
        raise ValidationError("Не указан домен для сертификата.")
    return normalized


def _require_certbot(runner: Runner, method: CertificateMethod) -> None:
    if not command_exists("certbot"):
        raise ValidationError("Certbot не установлен. Повторно запустите корневой install.sh.")
    required_plugin = {
        "cloudflare": ("dns-cloudflare", "python3-certbot-dns-cloudflare"),
        "gcore": ("dns-gcore", "certbot-dns-gcore 0.1.8"),
    }.get(method)
    if required_plugin is not None:
        plugin_name, package_name = required_plugin
        plugins = runner.run(["certbot", "plugins"], check=False, timeout=60)
        output = plugins.stdout + "\n" + plugins.stderr
        if plugins.returncode != 0 or not re.search(
            rf"(?m)^\s*\*?\s*{re.escape(plugin_name)}\s*$", output
        ):
            if method == "gcore":
                raise ValidationError(
                    "Плагин dns-gcore не виден системной команде Certbot. "
                    "Повторно запустите корневой install.sh: он устанавливает закреплённый "
                    "certbot-dns-gcore 0.1.8 с проверкой SHA-256."
                )
            raise ValidationError(
                "Плагин Certbot для Cloudflare не установлен. "
                f"Установите пакет {package_name}."
            )


def _validate_certificate(
    runner: Runner,
    fullchain: Path,
    private_key: Path,
    domains: Sequence[str],
) -> None:
    if not fullchain.is_file() or not private_key.is_file():
        raise ValidationError("Файлы TLS-сертификата не найдены.")
    runner.run(
        ["openssl", "x509", "-in", str(fullchain), "-noout", "-checkend", "86400"],
        sensitive=True,
    )
    for domain in domains:
        checked_host = (
            f"rwm-wildcard-check.{domain[2:]}"
            if domain.startswith("*.")
            else domain
        )
        result = runner.run(
            [
                "openssl",
                "x509",
                "-in",
                str(fullchain),
                "-noout",
                "-checkhost",
                checked_host,
            ],
            check=False,
            sensitive=True,
        )
        if result.returncode != 0:
            raise ValidationError(f"TLS-сертификат не подходит для домена {domain}.")
    certificate_public = runner.run(
        ["openssl", "x509", "-in", str(fullchain), "-pubkey", "-noout"],
        sensitive=True,
    ).stdout
    key_public = runner.run(
        ["openssl", "pkey", "-in", str(private_key), "-pubout"],
        sensitive=True,
    ).stdout
    if certificate_public.strip() != key_public.strip():
        raise TransactionError("Закрытый ключ не соответствует TLS-сертификату.")
