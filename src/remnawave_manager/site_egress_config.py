"""Source-address selection for known external site proxies, never Xray routes."""
from __future__ import annotations

import ipaddress
import re

from .errors import ValidationError
from .site_ipv4 import bind_origins, unbind_origins, synchronize_upstreams
from .nginx import _brace_depths, _server_blocks, _structural_text
from .site_policy import (
    ASTER_COMMENTS_PROXY, ASTER_METADATA_PROXY, ASTER_SEARCH_PROXY,
    ANSWERS_MAIL_PROXY, FOKUS_RIA_PROXY, IMAGE_PROXY, LOOP_GIFS_PROXY,
    MORROW_YAPPY_PROXY, NORTHLINE_PROXY, SVOD_WIKIPEDIA_PROXY, _PROXY_REVISIONS,
)

MARKER = "# rwm-site-egress"
_BIND = re.compile(r"^        proxy_bind ([0-9.]+); # rwm-site-egress\n", re.M)
_PASS = re.compile(r"^        proxy_pass https://[^;\n]+;", re.M)


def validate_source(value: str) -> str:
    try:
        address = ipaddress.IPv4Address(value)
    except (ValueError, TypeError) as error:
        raise ValidationError("Укажите IPv4, назначенный сетевому интерфейсу сервера, или auto.") from error
    if address.is_unspecified or address.is_loopback or address.is_multicast or address.is_link_local or int(address) == 0xffffffff:
        raise ValidationError("Для сайта нужен обычный исходящий IPv4 сервера.")
    return str(address)


def site_servers(text: str, roots: set[str]) -> list[tuple[int, int]]:
    selected = []
    for _, opening, closing in _server_blocks(text):
        body = text[opening + 1:closing]
        structure = _structural_text(body)
        depths = _brace_depths(structure)
        matches = []
        for match in re.finditer(r"\broot\s+[^;]+;", structure):
            value = body[match.start():match.end()][4:].strip().removesuffix(";").strip().strip("\"'").rstrip("/")
            if value in roots:
                matches.append(match)
        if not matches:
            continue
        if len(matches) != 1 or depths[matches[0].start()] != 0:
            raise ValidationError("Неоднозначный root сайта в nginx.")
        if re.search(r"\binclude\s", structure):
            raise ValidationError("Server сайта содержит include; исходящий IP нельзя применить однозначно.")
        selected.append((opening, closing))
    return selected


def strip_bindings(body: str) -> tuple[str, str | None]:
    """Operate on LF text; validate markers before removing our own directives."""
    values = _BIND.findall(body)
    plain = unbind_origins(_BIND.sub("", body))
    if MARKER in plain or len(set(values)) > 1:
        raise ValidationError("Настройка исходящего IP сайта повреждена или неоднозначна.")
    source = validate_source(values[0]) if values else None
    if source is None and plain != body:
        raise ValidationError("IPv4-источники сайта не имеют исходящего IP.")
    if source is not None:
        restored, _ = bind_body(plain, source)
        legacy, _ = bind_body(plain, source, legacy=True)
        if body not in (restored, legacy):
            raise ValidationError("Исходящий IP задан не во всех известных маршрутах сайта или вне них.")
    return plain, source


def bind_body(body: str, source: str | None, *, legacy: bool = False) -> tuple[str, int]:
    if source is not None:
        source = validate_source(source)
    # Reject custom bindings rather than guessing how they interact with ours.
    if re.search(r"\bproxy_bind\s", _structural_text(body)):
        raise ValidationError("Найдена пользовательская директива proxy_bind; сначала согласуйте конфигурацию сайта.")
    known = {
        ASTER_COMMENTS_PROXY, ASTER_METADATA_PROXY, ASTER_SEARCH_PROXY,
        ANSWERS_MAIL_PROXY, FOKUS_RIA_PROXY, IMAGE_PROXY, LOOP_GIFS_PROXY,
        MORROW_YAPPY_PROXY, NORTHLINE_PROXY, SVOD_WIKIPEDIA_PROXY,
        *(old for old, _ in _PROXY_REVISIONS),
    }
    # Tokenize matched blocks before rendering so overlapping historical versions
    # are processed once and no arbitrary proxy_pass can acquire a binding.
    remainder = body
    replacements: list[tuple[str, str]] = []
    count = 0
    for block in sorted(known, key=len, reverse=True):
        if block not in remainder:
            continue
        token = f"\x00RWM_EGRESS_{len(replacements)}\x00"
        if token in body:
            raise ValidationError("Недопустимый символ в nginx-конфигурации.")
        occurrences = remainder.count(block)
        count += len(_PASS.findall(block)) * occurrences
        replacement = _PASS.sub(lambda m: f"        proxy_bind {source}; {MARKER}\n" + m[0], block) if source else block
        if source and not legacy:
            replacement = bind_origins(replacement)
        remainder = remainder.replace(block, token)
        replacements.append((token, replacement))
    structure = _structural_text(remainder)
    for location in re.finditer(r"\blocation\b[^{};]*\{", structure):
        if re.search(r"/_(?:images|northline|aster|morrow|answers|svod|loop|fokus)(?:/|\\/)", remainder[location.start():location.end()]):
            raise ValidationError("Найдены неизвестные или изменённые маршруты сайта; примените штатный шаблон после проверки конфигурации.")
    for token, replacement in replacements:
        remainder = remainder.replace(token, replacement)
    return remainder, count


def configure_egress(text: str, roots: set[str], source: str | None) -> tuple[str, int, list[str | None]]:
    # An inherited HTTP-level bind would make "auto" misleading. Other vhosts'
    # own bindings remain outside this feature's scope.
    blocks = _server_blocks(text)
    for binding in re.finditer(r"\bproxy_bind\s", _structural_text(text)):
        if not any(opening < binding.start() < closing for _, opening, closing in blocks):
            raise ValidationError("Найдена наследуемая директива proxy_bind вне server; сначала согласуйте конфигурацию nginx.")
    count = 0
    previous: list[str | None] = []
    for opening, closing in reversed(site_servers(text, roots)):
        body = text[opening + 1:closing]
        newline = "\r\n" if "\r\n" in body and "\n" not in body.replace("\r\n", "") else "\n"
        plain, old_source = strip_bindings(body.replace("\r\n", "\n"))
        updated, routes = bind_body(plain, source)
        count += routes
        previous.append(old_source)
        text = text[:opening + 1] + updated.replace("\n", newline) + text[closing:]
    return synchronize_upstreams(text), count, previous
