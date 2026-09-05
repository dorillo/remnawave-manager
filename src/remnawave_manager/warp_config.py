from __future__ import annotations

import base64
import binascii
import configparser
import ipaddress
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from .errors import ValidationError

_MAX_PROFILE_SIZE = 64 * 1024
WARP_IPV4_ROUTE_METRIC = 42760
WARP_IPV4_ROUTE_UP = (
    f"ip -4 route replace default dev %i metric {WARP_IPV4_ROUTE_METRIC}"
)
WARP_IPV4_ROUTE_DOWN = (
    f"ip -4 route del default dev %i metric {WARP_IPV4_ROUTE_METRIC} "
    "2>/dev/null || true"
)


@dataclass(frozen=True, slots=True)
class WarpProfile:
    private_key: str
    addresses: tuple[str, ...]
    public_key: str
    allowed_ips: tuple[str, ...]
    endpoint: str
    mtu: int = 1280
    keepalive: int = 25

    def render(self) -> str:
        return (
            "[Interface]\n"
            f"PrivateKey = {self.private_key}\n"
            f"Address = {', '.join(self.addresses)}\n"
            f"MTU = {self.mtu}\n"
            "Table = off\n"
            f"PostUp = {WARP_IPV4_ROUTE_UP}\n"
            f"PreDown = {WARP_IPV4_ROUTE_DOWN}\n"
            "\n"
            "[Peer]\n"
            f"PublicKey = {self.public_key}\n"
            f"AllowedIPs = {', '.join(self.allowed_ips)}\n"
            f"Endpoint = {self.endpoint}\n"
            f"PersistentKeepalive = {self.keepalive}\n"
        )


_INTERFACE_KEYS = {
    "privatekey",
    "address",
    "mtu",
    "table",
    "dns",
    "postup",
    "predown",
}
_PEER_KEYS = {"publickey", "allowedips", "endpoint", "persistentkeepalive"}
_FORBIDDEN = {
    "preup",
    "postdown",
    "saveconfig",
}


def _wireguard_key(value: str, label: str) -> str:
    selected = value.strip()
    try:
        decoded = base64.b64decode(selected, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ValidationError(f"{label}: ключ не является корректным base64.") from error
    if len(decoded) != 32:
        raise ValidationError(f"{label}: WireGuard-ключ должен содержать 32 байта.")
    return selected


def _list(value: str, label: str) -> list[str]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    if not items:
        raise ValidationError(f"{label}: список пуст.")
    return items


def _endpoint(value: str) -> str:
    selected = value.strip()
    match = re.fullmatch(
        r"(?P<host>\[[0-9A-Fa-f:]+\]|[A-Za-z0-9.-]+):(?P<port>[0-9]{1,5})",
        selected,
    )
    if not match or not 1 <= int(match.group("port")) <= 65535:
        raise ValidationError("Endpoint WARP имеет некорректный формат.")
    host = match.group("host")
    if host.startswith("["):
        try:
            if ipaddress.ip_address(host[1:-1]).version != 6:
                raise ValueError
        except ValueError as error:
            raise ValidationError("Endpoint WARP содержит некорректный IPv6-адрес.") from error
    else:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            labels = host.lower().split(".")
            if len(labels) < 2 or any(
                not re.fullmatch(
                    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
                    label,
                )
                for label in labels
            ):
                raise ValidationError("Endpoint WARP содержит некорректный hostname.")
    return selected


def parse_warp_profile(
    text: str,
    *,
    generated_profile: bool = False,
) -> WarpProfile:
    parser = configparser.ConfigParser(
        interpolation=None,
        strict=True,
        delimiters=("=",),
        comment_prefixes=("#", ";"),
        inline_comment_prefixes=None,
    )
    parser.optionxform = str
    try:
        parser.read_string(text)
    except configparser.Error as error:
        raise ValidationError(f"Некорректный WireGuard-конфиг: {error}") from error
    if parser.sections() != ["Interface", "Peer"]:
        raise ValidationError("WARP-конфиг должен содержать ровно один Interface и один Peer.")

    interface_items = list(parser.items("Interface"))
    peer_items = list(parser.items("Peer"))
    if len({key.lower() for key, _ in interface_items}) != len(interface_items):
        raise ValidationError("В Interface есть дублирующиеся параметры с разным регистром.")
    if len({key.lower() for key, _ in peer_items}) != len(peer_items):
        raise ValidationError("В Peer есть дублирующиеся параметры с разным регистром.")
    interface = {key.lower(): value for key, value in interface_items}
    peer = {key.lower(): value for key, value in peer_items}
    forbidden = (_FORBIDDEN & set(interface)) | (_FORBIDDEN & set(peer))
    if forbidden:
        raise ValidationError("WARP-конфиг содержит запрещённые hooks: " + ", ".join(sorted(forbidden)))
    unknown_interface = set(interface) - _INTERFACE_KEYS
    unknown_peer = set(peer) - _PEER_KEYS
    if unknown_interface or unknown_peer:
        raise ValidationError(
            "WARP-конфиг содержит неизвестные параметры: "
            + ", ".join(sorted(unknown_interface | unknown_peer))
        )
    managed_hooks = {
        "postup": WARP_IPV4_ROUTE_UP,
        "predown": WARP_IPV4_ROUTE_DOWN,
    }
    present_hooks = {key for key in managed_hooks if key in interface}
    if present_hooks and (
        present_hooks != set(managed_hooks)
        or any(interface[key].strip() != value for key, value in managed_hooks.items())
    ):
        raise ValidationError(
            "WARP-конфиг содержит изменённые или неполные manager-owned hooks."
        )
    if "dns" in interface and not generated_profile:
        raise ValidationError("Безопасный takeover запрещён: в WARP-конфиге указан DNS.")
    if not generated_profile and interface.get("table", "").strip().lower() != "off":
        raise ValidationError("Безопасный takeover требует Table = off.")

    try:
        addresses = tuple(str(ipaddress.ip_interface(item)) for item in _list(interface["address"], "Address"))
    except (KeyError, ValueError) as error:
        raise ValidationError("Некорректный Address в WARP-конфиге.") from error
    parsed_addresses = tuple(ipaddress.ip_interface(item) for item in addresses)
    if (
        len(parsed_addresses) > 2
        or len({item.version for item in parsed_addresses}) != len(parsed_addresses)
        or any(item.network.prefixlen != item.max_prefixlen for item in parsed_addresses)
        or any(
            item.ip.is_unspecified
            or item.ip.is_loopback
            or item.ip.is_multicast
            or item.ip.is_link_local
            for item in parsed_addresses
        )
    ):
        raise ValidationError(
            "Address WARP должен содержать не более одного IPv4 /32 и IPv6 /128 host-адреса."
        )
    try:
        allowed = tuple(str(ipaddress.ip_network(item, strict=False)) for item in _list(peer["allowedips"], "AllowedIPs"))
    except (KeyError, ValueError) as error:
        raise ValidationError("Некорректный AllowedIPs в WARP-конфиге.") from error
    if "0.0.0.0/0" not in allowed:
        raise ValidationError("AllowedIPs не содержит IPv4 default-префикс WARP.")
    try:
        mtu = int(interface.get("mtu", "1280"))
        keepalive = int(peer.get("persistentkeepalive", "25"))
    except ValueError as error:
        raise ValidationError("MTU/PersistentKeepalive должны быть целыми числами.") from error
    if mtu != 1280:
        raise ValidationError("Для WARP разрешён только MTU = 1280.")
    if keepalive != 25:
        raise ValidationError("Для WARP разрешён только PersistentKeepalive = 25.")
    try:
        private_key = _wireguard_key(interface["privatekey"], "PrivateKey")
        public_key = _wireguard_key(peer["publickey"], "PublicKey")
        endpoint = _endpoint(peer["endpoint"])
    except KeyError as error:
        raise ValidationError(f"В WARP-конфиге отсутствует {error.args[0]}.") from error
    return WarpProfile(
        private_key=private_key,
        addresses=addresses,
        public_key=public_key,
        allowed_ips=allowed,
        endpoint=endpoint,
        mtu=mtu,
        keepalive=keepalive,
    )


def load_warp_profile(path: Path, *, generated_profile: bool = False) -> WarpProfile:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValidationError(
                f"WARP-конфиг не является обычным отдельным файлом: {path}"
            )
        descriptor = os.open(path, flags)
    except ValidationError:
        raise
    except OSError as error:
        raise ValidationError(f"Не удалось безопасно открыть WARP-конфиг {path}: {error}") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (metadata.st_dev, metadata.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise ValidationError(
                f"WARP-конфиг не является обычным отдельным файлом: {path}"
            )
        if metadata.st_size <= 0 or metadata.st_size > _MAX_PROFILE_SIZE:
            raise ValidationError(
                f"WARP-конфиг {path} пуст или превышает {_MAX_PROFILE_SIZE} байт."
            )
        if os.name == "posix":
            if metadata.st_uid != os.geteuid():
                raise ValidationError(
                    f"WARP-конфиг {path} принадлежит другому пользователю."
                )
            if metadata.st_mode & 0o022:
                raise ValidationError(
                    f"WARP-конфиг {path} не должен быть доступен для записи группе или остальным."
                )
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            payload = stream.read(_MAX_PROFILE_SIZE + 1)
        after = os.fstat(descriptor)
    except OSError as error:
        raise ValidationError(
            f"Не удалось безопасно прочитать WARP-конфиг {path}."
        ) from error
    finally:
        os.close(descriptor)
    if (
        len(payload) != metadata.st_size
        or len(payload) > _MAX_PROFILE_SIZE
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
        raise ValidationError(f"WARP-конфиг {path} изменился во время чтения.")
    try:
        text = payload.decode("utf-8")
    except UnicodeError as error:
        raise ValidationError(f"WARP-конфиг {path} не является UTF-8 текстом.") from error
    return parse_warp_profile(text, generated_profile=generated_profile)
