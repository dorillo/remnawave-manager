"""IPv4-only DNS upstreams for explicitly bound site proxies (nginx >= 1.27.3).

Keep literal proxy_pass URIs: variable-based proxying changes prefix replacement
and escaping of image filenames. Only generated external site routes use these
upstreams; system DNS, VPN routes and unbound sites are left alone.
"""
from __future__ import annotations

import ipaddress
import re
from pathlib import Path

from .errors import ValidationError
from .nginx import _structural_text

PREFIX = "rwm_site_ipv4_"
BEGIN = "# BEGIN REMNAWAVE-MANAGER SITE IPV4 UPSTREAMS"
END = "# END REMNAWAVE-MANAGER SITE IPV4 UPSTREAMS"
_BLOCK = re.compile(r"(?m)^" + re.escape(BEGIN) + r"\r?\n.*?^" + re.escape(END) + r"\r?\n\r?\n", re.S)
_HOST = r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?"
_PASS = re.compile(r"(?m)^(        proxy_pass https://)(" + _HOST + r")(?=[/;])")
_ALIAS = re.compile(r"(?m)^        proxy_pass https://" + PREFIX + r"([a-z0-9_-]+)(?=[/;])")


def system_resolvers() -> tuple[str, ...]:
    """Reuse the host's configured DNS, including systemd-resolved's local stub."""
    try:
        content = Path('/etc/resolv.conf').read_text()
    except OSError as error:
        raise ValidationError("Не удалось прочитать DNS сервера из /etc/resolv.conf.") from error
    addresses = []
    for line in content.splitlines():
        fields = line.split('#', 1)[0].split(';', 1)[0].split()
        if len(fields) >= 2 and fields[0] == 'nameserver':
            try:
                address = ipaddress.ip_address(fields[1])
            except ValueError as error:
                raise ValidationError("Некорректный DNS-адрес в /etc/resolv.conf.") from error
            if address.is_unspecified or address.is_multicast or '%' in fields[1]:
                raise ValidationError("Неподдерживаемый DNS-адрес в /etc/resolv.conf.")
            token = f'[{address}]' if address.version == 6 else str(address)
            if token not in addresses:
                addresses.append(token)
    if not addresses:
        raise ValidationError("В /etc/resolv.conf нет DNS-серверов для IPv4-маршрутов сайта.")
    return tuple(addresses)


def bind_origins(block: str) -> str:
    return _PASS.sub(lambda m: m[1] + PREFIX + m[2].replace('.', '_'), block)


def unbind_origins(block: str) -> str:
    return _ALIAS.sub(lambda m: '        proxy_pass https://' + m[1].replace('_', '.'), block)


def _render(hosts: set[str], resolvers: tuple[str, ...]) -> str:
    # Strict tokens: callers cannot inject nginx directives through DNS settings.
    if not resolvers:
        raise ValidationError("Не заданы DNS-серверы для маршрутов сайта.")
    for token in resolvers:
        try:
            address = ipaddress.ip_address(token.strip('[]'))
            expected = f'[{address}]' if address.version == 6 else str(address)
            if token != expected or address.is_unspecified or address.is_multicast or '%' in token:
                raise ValueError()
        except ValueError as error:
            raise ValidationError("Некорректный DNS-сервер в настройке сайта.") from error
    groups = []
    for host in sorted(hosts):
        if not re.fullmatch(_HOST, host):
            raise ValidationError("Некорректный внешний источник сайта.")
        name = PREFIX + host.replace('.', '_')
        groups.append(f'upstream {name} {{\n'
                      f'    zone {name} 256k;\n'
                      f'    resolver {" ".join(resolvers)} ipv6=off;\n'
                      '    resolver_timeout 5s;\n'
                      f'    server {host}:443 resolve;\n}}')
    return BEGIN + '\n' + '\n\n'.join(groups) + '\n' + END + '\n\n'


def synchronize_upstreams(text: str) -> str:
    newline = '\r\n' if '\r\n' in text and '\n' not in text.replace('\r\n', '') else '\n'
    blocks = list(_BLOCK.finditer(text))
    resolvers = None
    plain = text
    if blocks:
        if len(blocks) != 1:
            raise ValidationError("Дублируется блок IPv4-источников сайта.")
        block = blocks[0][0].replace('\r\n', '\n')
        newline = '\r\n' if '\r\n' in blocks[0][0] else '\n'
        hosts = set(re.findall(r'^    server (' + _HOST + r'):443 resolve;$', block, re.M))
        dns = re.search(r'^    resolver (.+) ipv6=off;$', block, re.M)
        if not hosts or not dns:
            raise ValidationError("Повреждён блок IPv4-источников сайта.")
        resolvers = tuple(dns[1].split())
        if block != _render(hosts, resolvers):
            raise ValidationError("Блок IPv4-источников сайта изменён вручную.")
        plain = text[:blocks[0].start()] + text[blocks[0].end():]
    if BEGIN in plain or END in plain or re.search(r'\bupstream\s+' + PREFIX, _structural_text(plain)):
        raise ValidationError("Конфликт служебного блока IPv4-источников сайта.")
    hosts = {match[1].replace('_', '.') for match in _ALIAS.finditer(plain)}
    if not hosts:
        return plain
    return _render(hosts, resolvers or system_resolvers()).replace('\n', newline) + plain
