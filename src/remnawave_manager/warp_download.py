from __future__ import annotations

import hashlib
import os
import platform
import re
import stat
import urllib.error
import urllib.parse
import urllib.request
from contextlib import suppress
from importlib.resources import files
from pathlib import Path

from .compat import load_manifest
from .errors import ManagerError, TransactionError, ValidationError
from .runner import atomic_write_bytes

_ALLOWED_HOSTS = {
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
_MAX_BINARY_SIZE = 64 * 1024 * 1024
_WGCF_NOTICE_RESOURCE = "data/licenses/wgcf-MIT.txt"
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){2}(?:[-+][0-9A-Za-z.-]+)?")


def _is_trusted_url(value: str) -> bool:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 4096
        or any(not 0x21 <= ord(character) <= 0x7E for character in value)
    ):
        return False
    try:
        parsed = urllib.parse.urlparse(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and (parsed.hostname or "").lower() in _ALLOWED_HOSTS
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
    )


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if not _is_trusted_url(newurl):
            raise ValidationError(
                f"wgcf redirect на недоверенный URL: {newurl}"
            )
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def wgcf_contract() -> dict[str, str]:
    machine = platform.machine().lower()
    if machine == "amd64":
        machine = "x86_64"
    elif machine == "arm64":
        machine = "aarch64"
    try:
        tool = load_manifest()["tools"]["wgcf"]
    except (KeyError, TypeError) as error:
        raise ValidationError("Manifest не содержит контракт wgcf.") from error
    if not isinstance(tool, dict) or not isinstance(tool.get("architectures"), dict):
        raise ValidationError("Manifest содержит некорректный контракт wgcf.")
    try:
        architecture = tool["architectures"][machine]
    except KeyError as error:
        raise ValidationError(f"Архитектура {machine} не поддерживается для wgcf.") from error
    if not isinstance(architecture, dict):
        raise ValidationError(f"Manifest содержит некорректный контракт wgcf для {machine}.")
    version = tool.get("version")
    asset = architecture.get("asset")
    sha256 = architecture.get("sha256")
    base_url = tool.get("base_url")
    if not isinstance(version, str) or _VERSION_RE.fullmatch(version) is None:
        raise ValidationError("Manifest содержит небезопасную версию wgcf.")
    if (
        not isinstance(asset, str)
        or not asset
        or Path(asset).name != asset
        or "/" in asset
        or "\\" in asset
    ):
        raise ValidationError("Manifest содержит небезопасное имя wgcf asset.")
    if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
        raise ValidationError("Manifest содержит некорректный SHA-256 wgcf.")
    if not isinstance(base_url, str):
        raise ValidationError("Manifest содержит некорректный base URL wgcf.")
    url = base_url.rstrip("/") + "/" + asset
    if not _is_trusted_url(url):
        raise ValidationError("Manifest содержит недоверенный URL wgcf.")
    return {
        "version": version,
        "asset": asset,
        "sha256": sha256,
        "url": url,
    }


def _verified_payload(
    path: Path,
    expected: str | None,
    *,
    max_size: int = _MAX_BINARY_SIZE,
    require_owner: bool = False,
) -> tuple[bytes, int]:
    try:
        before = path.lstat()
    except OSError as error:
        raise ValidationError(f"Не удалось проверить wgcf {path}: {error}") from error
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise ValidationError(f"wgcf {path} не является обычным отдельным файлом.")
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValidationError(f"Не удалось безопасно открыть wgcf {path}: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (metadata.st_dev, metadata.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ValidationError(f"wgcf {path} не является обычным отдельным файлом.")
        if metadata.st_size <= 0 or metadata.st_size > max_size:
            raise ValidationError(
                "Файл wgcf пуст или превышает допустимый размер."
            )
        if (
            require_owner
            and os.name == "posix"
            and (metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022)
        ):
            raise ValidationError(
                f"Установленный wgcf {path} имеет небезопасные владельца или права."
            )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(max_size + 1)
        after = os.fstat(descriptor)
    except ValidationError:
        raise
    except OSError as error:
        raise ValidationError(f"Не удалось безопасно прочитать wgcf {path}.") from error
    finally:
        os.close(descriptor)
    if (
        len(payload) != metadata.st_size
        or len(payload) > max_size
        or (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_nlink,
        )
        != (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            metadata.st_nlink,
        )
    ):
        raise ValidationError(f"Файл wgcf {path} изменился во время проверки.")
    actual = hashlib.sha256(payload).hexdigest()
    if expected is not None and actual != expected:
        raise ValidationError(f"SHA-256 wgcf не совпал: ожидался {expected}, получен {actual}.")
    return payload, metadata.st_mode & 0o777


def _assert_safe_directory(path: Path) -> None:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValidationError(f"Не удалось проверить каталог wgcf {path}: {error}") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise ValidationError(f"Каталог wgcf имеет небезопасный тип: {path}")
    if os.name == "posix":
        if metadata.st_uid != os.geteuid():
            raise ValidationError(f"Каталог wgcf {path} принадлежит другому пользователю.")
        if metadata.st_mode & 0o022:
            raise ValidationError(f"Каталог wgcf {path} доступен для записи группе или остальным.")


def _snapshot_notice(path: Path) -> tuple[bytes, int] | None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValidationError(f"License notice wgcf имеет небезопасный тип: {path}")
    if not path.exists():
        return None
    return _verified_payload(path, None, max_size=1024 * 1024)


def _restore_notice(path: Path, snapshot: tuple[bytes, int] | None) -> None:
    if snapshot is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write_bytes(path, snapshot[0], mode=snapshot[1])


def wgcf_notice_path(binary: Path) -> Path:
    return binary.with_name(f"{binary.name}.LICENSE.txt")


def _install_notice(binary: Path) -> None:
    try:
        payload = files("remnawave_manager").joinpath(_WGCF_NOTICE_RESOURCE).read_bytes()
        atomic_write_bytes(wgcf_notice_path(binary), payload, mode=0o644)
    except OSError as error:
        raise TransactionError("Не удалось установить license notice для wgcf.") from error


def install_wgcf(target_directory: Path, *, local_file: Path | None = None) -> Path:
    contract = wgcf_contract()
    target = target_directory / f"wgcf-{contract['version']}"
    if target_directory.is_symlink():
        raise ValidationError(f"Каталог wgcf имеет небезопасный тип: {target_directory}")
    if not target_directory.exists():
        try:
            target_directory.mkdir(parents=True, exist_ok=True, mode=0o755)
        except OSError as error:
            raise TransactionError(f"Не удалось создать каталог wgcf {target_directory}: {error}") from error
    _assert_safe_directory(target_directory)
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise ValidationError(f"Путь wgcf имеет небезопасный тип: {target}")
    if target.is_file():
        _, previous_mode = _verified_payload(
            target,
            contract["sha256"],
            require_owner=True,
        )
        notice = wgcf_notice_path(target)
        notice_snapshot = _snapshot_notice(notice)
        try:
            os.chmod(target, 0o755)  # noqa: S103, RUF100 - verified binary must be executable
            _install_notice(target)
        except BaseException as error:
            rollback_errors: list[str] = []
            try:
                os.chmod(target, previous_mode)
            except BaseException as rollback_error:  # noqa: BLE001 - continue rollback
                rollback_errors.append(f"права binary: {rollback_error}")
            try:
                _restore_notice(notice, notice_snapshot)
            except BaseException as rollback_error:  # noqa: BLE001 - continue rollback
                rollback_errors.append(f"license notice: {rollback_error}")
            if rollback_errors:
                raise TransactionError(
                    "Установка license notice wgcf завершилась ошибкой, а rollback неполон: "
                    + "; ".join(rollback_errors)
                ) from error
            if isinstance(error, ManagerError):
                raise
            raise TransactionError(
                f"Не удалось подготовить существующий wgcf: {error}"
            ) from error
        return target

    if local_file is not None:
        payload, _ = _verified_payload(local_file, contract["sha256"])
    else:
        if not _is_trusted_url(contract["url"]):
            raise ValidationError(f"Недоверенный URL wgcf: {contract['url']}")
        request = urllib.request.Request(  # noqa: S310, RUF100 - trusted HTTPS allowlist checked above
            contract["url"],
            headers={"User-Agent": "remnawave-manager/0.1"},
            method="GET",
        )
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            _SafeRedirect(),
        )
        try:
            with opener.open(request, timeout=60) as response:
                if not _is_trusted_url(response.geturl()):
                    raise ValidationError(f"wgcf получен с недоверенного URL: {response.geturl()}")
                length = response.headers.get("Content-Length")
                if length:
                    try:
                        declared_length = int(length)
                    except ValueError as error:
                        raise ValidationError(
                            "Сервер wgcf вернул некорректный Content-Length."
                        ) from error
                    if declared_length < 0 or declared_length > _MAX_BINARY_SIZE:
                        raise ValidationError("wgcf превышает допустимый размер.")
                payload = response.read(_MAX_BINARY_SIZE + 1)
        except urllib.error.HTTPError as error:
            status = error.code
            with suppress(OSError):
                error.close()
            raise TransactionError(
                f"Не удалось скачать фиксированный wgcf: HTTP {status}"
            ) from error
        except (OSError, urllib.error.URLError) as error:
            raise TransactionError(f"Не удалось скачать фиксированный wgcf: {error}") from error
        if len(payload) > _MAX_BINARY_SIZE:
            raise ValidationError("wgcf превышает допустимый размер.")

    actual = hashlib.sha256(payload).hexdigest()
    if actual != contract["sha256"]:
        raise ValidationError(f"SHA-256 wgcf не совпал: ожидался {contract['sha256']}, получен {actual}.")
    notice = wgcf_notice_path(target)
    if notice.exists() or notice.is_symlink():
        raise ValidationError(
            f"License notice wgcf {notice} уже существует без проверенного binary."
        )
    _assert_safe_directory(target_directory)
    try:
        atomic_write_bytes(target, payload, mode=0o755)
        _install_notice(target)
        _verified_payload(target, contract["sha256"], require_owner=True)
    except BaseException as error:
        rollback_errors: list[str] = []
        try:
            target.unlink(missing_ok=True)
        except BaseException as rollback_error:  # noqa: BLE001 - continue rollback
            rollback_errors.append(f"binary: {rollback_error}")
        try:
            notice.unlink(missing_ok=True)
        except BaseException as rollback_error:  # noqa: BLE001 - continue rollback
            rollback_errors.append(f"license notice: {rollback_error}")
        if rollback_errors:
            raise TransactionError(
                "Установка wgcf завершилась ошибкой, а rollback неполон: "
                + "; ".join(rollback_errors)
            ) from error
        if isinstance(error, ManagerError):
            raise
        raise TransactionError(f"Не удалось установить wgcf: {error}") from error
    return target
