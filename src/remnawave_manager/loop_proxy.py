"""Fixed anonymous GIFS read routes. No upstream write/auth endpoints."""

from .proxy_policy import harden_proxy

MEDIA_PATTERN = r"[a-f0-9]{32,64}(?:_(?:150|300|500|preview))?\.(?:gif|webp|mp4|png|jpg)"
# name -> (method, upstream path, exact allowed query expression)
ROUTES = {
    "popular": ("GET", "/api/v1/Popular", r"contentType=[123]&skip=[0-9]{1,6}&take=24"),
    "categories": ("POST", "/api/v1/Community/GetCommunities", ""),
    "category": ("GET", "/api/v1/Community/GetFiles", r"communityId=[0-9]{1,12}&contentType=[123]&skip=[0-9]{1,6}&take=24"),
    "trending": ("GET", "/api/v1/Tag/GetTrendingSearches", r"period=24h&take=8"),
}
for kind, upstream in (("gif", "Gif"), ("sticker", "Sticker"), ("clip", "Loop")):
    ROUTES[f"search-{kind}"] = ("POST", f"/api/v1/{upstream}/Get{'Gifs' if kind == 'gif' else 'Stickers' if kind == 'sticker' else 'Loops'}", "")
    ROUTES[f"related-{kind}"] = ("POST", f"/api/v1/{upstream}/GetRelated", "")


def _upstream(host: str, *, media: bool = False, post: bool = False) -> str:
    return f'''        proxy_pass https://{host};
        proxy_ssl_server_name on;
        proxy_ssl_name {host};
        proxy_ssl_verify on;
        proxy_ssl_verify_depth 3;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_pass_request_headers off;
        proxy_set_header Host {host};
        proxy_set_header User-Agent "Mozilla/5.0 (compatible; Loop/1.0)";
        proxy_set_header Accept {"*/*" if media else "application/json"};
        proxy_hide_header Set-Cookie;
        proxy_hide_header Location;
        proxy_redirect off;
        proxy_intercept_errors on;
        error_page 301 302 303 307 308 =502 @loop_upstream_error;
        proxy_connect_timeout 5s;
        proxy_read_timeout 20s;
''' + ('''        client_max_body_size 2k;
        proxy_set_header Content-Type application/json;
''' if post else '''        proxy_pass_request_body off;
        proxy_set_header Content-Length "";
''') + ('''        proxy_set_header Range $http_range;
        proxy_set_header If-Range $http_if_range;
''' if media else "")


def render_proxy(*, secure: bool = True) -> str:
    blocks = []
    for name, (method, path, query) in ROUTES.items():
        guard = f'if ($args !~ "^{query}$") {{ return 400; }}' if query else 'if ($args != "") { return 400; }'
        blocks.append(f'''    location = /_loop/gifs/{name} {{
        if ($request_method != {method}) {{ return 405; }}
        {guard}
        rewrite ^ {path} break;
{_upstream("gifs.ru", post=method == "POST")}    }}''')
    blocks.append(f'''    location ~ "^/_loop/gifs/item/(?<loop_id>[0-9]{{1,12}})$" {{
        if ($request_method != GET) {{ return 405; }}
        if ($args != "") {{ return 400; }}
        rewrite ^ /api/v1/File/FileById/$loop_id break;
{_upstream("gifs.ru")}    }}''')
    blocks.append(f'''    location ~ "^/_loop/media/(?<loop_file>{MEDIA_PATTERN})$" {{
        if ($request_method !~ "^(GET|HEAD)$") {{ return 405; }}
        if ($args != "") {{ return 400; }}
        rewrite ^ /$loop_file break;
{_upstream("media.gifs.ru", media=True)}    }}''')
    blocks.extend(['    location @loop_upstream_error { return 502; }', '    location /_loop/ { return 404; }'])
    result = "\n\n".join(blocks)
    return harden_proxy(result, "loop") if secure else result
