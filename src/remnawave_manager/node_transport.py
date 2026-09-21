"""Inventory metadata for existing nginx XHTTP routes; never changes routing."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .errors import ValidationError
from .nginx import _brace_depths, _structural_text

_MARKER = re.compile(r"# remnawave-manager: transport=xhttp provider=(none|yandex|beeline-get|beeline-post)\s*$")
_LEXEMES = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\#[^\r\n]*''')


@dataclass(frozen=True)
class TransportRoute:
    opening: int
    closing: int
    target: str
    provider: str | None
    explicit: bool


def _blocks(text: str, kind: str):
    structural = _structural_text(text)
    for match in re.finditer(rf"\b{kind}\s+[^{{}};]*\{{", structural):
        opening = match.end() - 1
        depth = 1
        for position in range(opening + 1, len(structural)):
            depth += (structural[position] == "{") - (structural[position] == "}")
            if depth == 0:
                yield match.start(), opening, position
                break


def transport_routes(text: str) -> list[TransportRoute]:
    """Explicit metadata must belong to a location proxying to a local backend.

    Legacy names remain hints. A generic TCP XHTTP upstream cannot identify
    a CDN provider; it stays unknown until the administrator marks the route.
    """
    structural = _structural_text(text)
    upstreams = {}
    for start, opening, closing in _blocks(text, "upstream"):
        name = structural[start:opening].split()[1]
        servers = re.findall(r"\bserver\s+([^;\s]+)[^;]*;", structural[opening + 1:closing])
        upstreams[name] = servers

    def local(target: str) -> bool:
        return bool(re.match(r"^(?:unix:/|127\.0\.0\.1:|\[::1\]:|localhost:)", target))

    markers = {}
    for token in _LEXEMES.finditer(text):
        value = token.group()
        if not value.startswith("# remnawave-manager:"):
            continue
        marker = _MARKER.fullmatch(value)
        if marker is None:
            raise ValidationError("Некорректный маркер транспорта nginx: ожидается transport=xhttp provider=none|yandex|beeline-get|beeline-post.")
        markers[token.start()] = marker[1]
    consumed = set()
    routes = []
    for _, opening, closing in _blocks(text, "location"):
        body = structural[opening + 1:closing]
        depths = _brace_depths(body)
        selected = [(offset, provider) for offset, provider in markers.items()
                    if opening < offset < closing and depths[offset - opening - 1] == 0]
        passes = [m for m in re.finditer(r"\bproxy_pass\s+[^;]*;", body) if depths[m.start()] == 0]
        if selected and (len(selected) != 1 or len(passes) != 1):
            raise ValidationError("Маркер XHTTP должен находиться в location с одним proxy_pass; повторные маркеры запрещены.")
        if len(passes) != 1:
            continue
        m = passes[0]
        directive = text[opening + 1 + m.start():opening + 1 + m.end()]
        url = directive[len("proxy_pass"):].strip().removesuffix(";").strip().strip("\"'")
        target = re.sub(r"^https?://", "", url)
        name = target.split("/")[0]
        servers = upstreams.get(name, [])
        is_local = url.startswith(("http://", "https://")) and (
            local(target) or bool(servers) and all(local(server) for server in servers)
        )
        if selected and not is_local:
            raise ValidationError("Маркер XHTTP должен ссылаться на локальный upstream или Unix socket, а не внешний ресурс сайта.")
        inferred = None
        if name == "beeline_xhttp":
            inferred = "beeline-post"
        elif name == "xray_beeline_xhttp":
            inferred = "beeline-get"
        elif "yandex" in name.lower() and ("xhttp" in name.lower() or "cdn" in name.lower()):
            inferred = "yandex"
        if selected:
            consumed.add(selected[0][0])
            provider = selected[0][1]
        else:
            provider = inferred
        if selected or is_local and ("xhttp" in name.lower() or inferred or target.startswith("unix:")):
            routes.append(TransportRoute(opening, closing, target, provider, bool(selected)))
    if consumed != markers.keys():
        raise ValidationError("Маркер XHTTP находится вне обслуживающего location; перенесите его внутрь VPN-маршрута.")
    return routes


def transport_features(text: str) -> dict[str, bool]:
    routes = transport_routes(text)
    providers = {route.provider for route in routes}
    return {
        "xhttp_stream_separation": bool(routes),
        "yandex_cdn": "yandex" in providers,
        "beeline_cdn_get": "beeline-get" in providers,
        "beeline_cdn_post": "beeline-post" in providers,
        "xhttp_direct": "none" in providers,
        "xhttp_provider_unknown": None in providers,
        "xhttp_provider_explicit": any(route.explicit for route in routes),
    }
