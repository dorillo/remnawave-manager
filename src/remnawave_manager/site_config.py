"""Upgrade only the virtual hosts serving the selected node's static bind mount."""
from __future__ import annotations

import re

from .errors import ValidationError
from .nginx import _brace_depths, _server_blocks, _structural_text
from .site_egress_config import bind_body, strip_bindings
from .site_policy import (
    KNOWN_NODE_CSPS, NODE_CSP,
    TEMPLATE_POLICIES, _PROXY_REVISIONS,
    ASTER_METADATA_PROXY, ASTER_COMMENTS_PROXY, ASTER_SEARCH_PROXY,
    IMAGE_PROXY, NORTHLINE_PROXY, MORROW_YAPPY_PROXY, ANSWERS_MAIL_PROXY,
    SVOD_WIKIPEDIA_PROXY, LOOP_GIFS_PROXY, FOKUS_RIA_PROXY,
)


INDEX_LOCATION = """    location = /index.html {
        expires -1;
        etag off;
        if_modified_since off;
        try_files $uri =404;
    }"""


def upgrade_site_config(text: str, template_id: str, roots: set[str]) -> tuple[str, int]:
    """Preserve transports and other vhosts; refuse ambiguous custom site policies."""
    matched = 0
    for _start, opening, closing in reversed(_server_blocks(text)):
        body = text[opening + 1:closing]
        newline = '\r\n' if '\r\n' in body and '\n' not in body.replace('\r\n', '') else '\n'
        if newline == '\r\n':
            body = body.replace('\r\n', '\n')
        structural = _structural_text(body)
        depths = _brace_depths(structural)
        root_matches = list(re.finditer(r'\broot\s+[^;]+;', structural))
        selected = []
        for match in root_matches:
            directive = body[match.start():match.end()]
            value = directive[4:].strip().removesuffix(';').strip().strip('"\'').rstrip('/')
            if value in roots:
                selected.append(match)
        if not selected:
            continue
        if len(selected) != 1 or depths[selected[0].start()] != 0:
            raise ValidationError("Корень сайта nginx должен быть задан однозначно директивой root внутри server.")
        # Includes may contain conflicting locations or an inherited custom CSP.
        if re.search(r'\binclude\s', structural):
            raise ValidationError("Server сайта содержит include; объедините настройки этого server перед заменой сайта.")
        body, source_ip = strip_bindings(body)
        structural = _structural_text(body)
        depths = _brace_depths(structural)
        marker = f'add_header Content-Security-Policy "{NODE_CSP}" always;'
        headers = list(re.finditer(r'\badd_header\s+Content-Security-Policy\b[^;]*;', structural, re.I))
        known = {f'add_header Content-Security-Policy "{value}" always;'
                 for value in KNOWN_NODE_CSPS}
        # Sandbox headers in known proxy blocks are intentionally retained.
        stripped = body
        for old, new in _PROXY_REVISIONS:
            stripped = stripped.replace(old, '').replace(new, '')
        for proxy in (ASTER_METADATA_PROXY, ASTER_COMMENTS_PROXY, ASTER_SEARCH_PROXY,
                      IMAGE_PROXY, NORTHLINE_PROXY, MORROW_YAPPY_PROXY, ANSWERS_MAIL_PROXY,
                      SVOD_WIKIPEDIA_PROXY, LOOP_GIFS_PROXY, FOKUS_RIA_PROXY):
            stripped = stripped.replace(proxy, '')
        stripped_structure = _structural_text(stripped)
        for header in re.finditer(r'\badd_header\s+Content-Security-Policy\b[^;]*;', stripped_structure, re.I):
            original = stripped[header.start():header.end()]
            if original not in known:
                raise ValidationError("Пользовательский CSP сайта не распознан; согласуйте его с политикой шаблона перед заменой.")
        # Unknown edits to a reserved route must not silently shadow a new API.
        for location in re.finditer(r'\blocation\b[^{};]*\{', stripped_structure):
            arguments = stripped[location.start():location.end()]
            if re.search(r'/_(?:images|northline|aster|morrow|answers|svod|loop|fokus)(?:/|\\/)', arguments):
                raise ValidationError("Найдены изменённые вручную маршруты сайта; восстановите известную конфигурацию перед заменой.")
        server_headers = [header for header in headers if depths[header.start()] == 0]
        if len(server_headers) > 1:
            raise ValidationError("В server сайта найдено несколько CSP; устраните неоднозначность перед заменой.")
        if not server_headers:
            body = '\n    ' + marker + '\n' + body
        body = TEMPLATE_POLICIES[template_id](body)
        if not re.search(r'\blocation\s*=\s*/index\.html\s*\{', _structural_text(body)):
            body += '\n' + INDEX_LOCATION + '\n'
        if source_ip is not None:
            body, _ = bind_body(body, source_ip)
        text = text[:opening + 1] + body.replace('\n', newline) + text[closing:]
        matched += 1
    return text, matched
