"""Fixed anonymous Rutube comment reads for nginx and local preview."""

from .proxy_policy import harden_proxy
import re

PREFIX = '/_aster/rutube-comments/'
QUERY = r'(?:comment_id=[0-9]{1,24}(?:&parent_id=[0-9]{1,24})?|parent_id=[0-9]{1,24})?'


def upstream_url(path: str, query: str) -> str | None:
    match = re.fullmatch(re.escape(PREFIX) + r'([a-f0-9]{32})', path)
    if not match or not re.fullmatch(QUERY, query):
        return None
    return f'https://rutube.ru/api/v2/comments/video/{match[1]}/?client=wdp&sort_by=date_added_desc&direction=earliest' + ('&' + query if query else '')


def render_proxy(*, secure: bool = True) -> str:
    result = f'''    location ~ "^{PREFIX}(?<aster_video>[a-f0-9]{{32}})$" {{
        if ($request_method != GET) {{ return 405; }}
        if ($args !~ "^{QUERY}$") {{ return 400; }}
        rewrite ^ /api/v2/comments/video/$aster_video/?client=wdp&sort_by=date_added_desc&direction=earliest break;
        proxy_pass https://rutube.ru;
        proxy_ssl_server_name on;
        proxy_ssl_name rutube.ru;
        proxy_ssl_verify on;
        proxy_ssl_verify_depth 4;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_pass_request_headers off;
        proxy_pass_request_body off;
        proxy_set_header Host rutube.ru;
        proxy_set_header Accept application/json;
        proxy_set_header User-Agent "Mozilla/5.0";
        proxy_set_header Referer "https://rutube.ru/";
        proxy_set_header Content-Length "";
        proxy_hide_header Set-Cookie;
        proxy_hide_header Location;
        proxy_hide_header Cache-Control;
        proxy_cache off;
        proxy_redirect off;
        proxy_intercept_errors on;
        error_page 301 302 303 307 308 =502 @aster_comments_error;
        add_header Cache-Control "no-store" always;
        add_header X-Content-Type-Options nosniff always;
        proxy_connect_timeout 5s;
        proxy_read_timeout 15s;
    }}
    location @aster_comments_error {{ return 502; }}
    location /_aster/rutube-comments/ {{ return 404; }}'''

    return harden_proxy(result, "aster_comments") if secure else result


SEARCH_PATH = '/_aster/rutube-search'
SEARCH_QUERY = r'query=(?=[^&]{1,1800}(?:&|$))(?:[A-Za-z0-9._~*+-]|%[A-Fa-f0-9]{2})+(?:&client=wdp)?(?:&page=(?:[1-9]|1[0-9]|20))?'


def search_upstream(path: str, query: str) -> str | None:
    if path != SEARCH_PATH or not re.fullmatch(SEARCH_QUERY, query):
        return None
    return 'https://rutube.ru/api/search/video/?' + query


def render_search_proxy() -> str:
    return harden_proxy(f'''    location = {SEARCH_PATH} {{
        if ($request_method != GET) {{ return 405; }}
        if ($args !~ "^{SEARCH_QUERY}$") {{ return 400; }}
        proxy_pass https://rutube.ru/api/search/video/;
        proxy_ssl_server_name on;
        proxy_ssl_name rutube.ru;
        proxy_pass_request_headers off;
        proxy_set_header Host rutube.ru;
        proxy_set_header Accept application/json;
        proxy_set_header User-Agent "Mozilla/5.0";
        proxy_set_header Referer "https://rutube.ru/";
        proxy_connect_timeout 5s;
        proxy_read_timeout 15s;
    }}''', 'aster_search')
