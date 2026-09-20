"""Fixed anonymous RIA reads. Configuration generation, not a web service."""
from __future__ import annotations

from .proxy_policy import harden_proxy

import re

SECTIONS = 'politics|world|economy|society|science|culture'
CSP = "default-src 'self'; connect-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
# (local regex, upstream path replacement, exact query grammar, media)
ROUTES = [
    (r'/ria/feed', '/export/rss2/archive/index.xml', '', False),
    (r'/ria/home', '/', '', False),
    (r'/ria/search', '/services/search/getmore/', r'query=(?:[A-Za-z0-9._~+*-]|%[A-Fa-f0-9]{2}){1,480}&offset=[0-9]{1,7}', False),
    (rf'/ria/section/({SECTIONS})', r'/\1/', '', False),
    (rf'/ria/more/({SECTIONS})', r'/services/\1/more.html', r'id=[0-9]{1,12}&date=[0-9]{8}T[0-9]{6}(?:&view=tags)?', False),
    (r'/ria/article/([0-9]{8})/([a-z0-9-]{1,160}-[0-9]{1,12}\.html)', r'/\1/\2', '', False),
    (r'/ria/dynamics/([0-9]{8})/([0-9]{1,12})', r'/services/dynamics/\1/\2.html', '', False),
    (r'/ria/comments', '/services/chat/get/', r'article_id=[0-9]{1,12}&limit=20(?:&date=[0-9]{1,12}&date_usec=[0-9]{1,6}&id_exc=[a-f0-9]{24})?', False),
    (r'/ria/rooms', '/services/chat/get_rooms/', r'(?:cur_room_id=[0-9]{1,12})?', False),
    (r'/media/(images/[A-Za-z0-9_/:.-]{1,400}\.(?:jpg|jpeg|png|webp))', r'/\1', '', True),
]


def upstream(path: str, query: str) -> str | None:
    if not path.startswith('/_fokus/') or '%' in path or '..' in path or '//' in path:
        return None
    relative = path[len('/_fokus'):]
    for pattern, target, args, media in ROUTES:
        match = re.fullmatch(pattern, relative)
        if match and re.fullmatch(args, query):
            host = 'cdnn21.img.ria.ru' if media else 'ria.ru'
            return 'https://' + host + match.expand(target) + ('?' + query if query else '')
    return None


def render_proxy(*, include_search: bool = True, secure: bool = True) -> str:
    blocks = []
    for pattern, target, args, media in ROUTES:
        if not include_search and pattern == r'/ria/search':
            continue
        # Named captures survive the query-validation regular expression.
        n = 0
        def capture(match: re.Match) -> str:
            nonlocal n
            n += 1
            return f'(?<fk{n}>'
        nginx_pattern = re.sub(r'\((?!\?)', capture, pattern)
        destination = re.sub(r'\\([12])', r'$fk\1', target)
        host = 'cdnn21.img.ria.ru' if media else 'ria.ru'
        query_check = f'if ($args !~ "^{args}$") {{ return 400; }}' if args else 'if ($args != "") { return 400; }'
        content = '' if media else '''        proxy_hide_header Content-Type;
        add_header Content-Type "text/plain; charset=utf-8" always;
'''
        unsafe_uri = r'^[^?]*(?:%|\.\.|//)' if pattern == r'/ria/search' else r'(?:%|\.\.|//)'
        blocks.append(f'''    location ~ "^/_fokus{nginx_pattern}$" {{
        if ($request_method != GET) {{ return 405; }}
        if ($request_uri ~ "{unsafe_uri}") {{ return 400; }}
        {query_check}
        rewrite ^ {destination} break;
        proxy_pass https://{host};
        proxy_ssl_server_name on;
        proxy_ssl_name {host};
        proxy_ssl_verify on;
        proxy_ssl_verify_depth 3;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_pass_request_headers off;
        proxy_pass_request_body off;
        proxy_set_header Host {host};
        proxy_set_header User-Agent "Mozilla/5.0 (compatible; Fokus/1.0)";
        proxy_set_header Accept "*/*";
        proxy_set_header Accept-Encoding "";
        proxy_set_header Content-Length "";
        proxy_hide_header Set-Cookie;
        proxy_hide_header Location;
        proxy_hide_header Refresh;
        proxy_hide_header Content-Security-Policy;
        proxy_hide_header X-Content-Type-Options;
{content}        add_header Content-Security-Policy "default-src 'none'; sandbox" always;
        add_header X-Content-Type-Options nosniff always;
        proxy_connect_timeout 5s;
        proxy_read_timeout 15s;
    }}''')
    blocks.append('    location /_fokus/ { return 404; }')
    result = "\n\n".join(blocks)
    return harden_proxy(result, "fokus") if secure else result
