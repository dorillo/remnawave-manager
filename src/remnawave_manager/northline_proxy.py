"""Fixed anonymous Mastodon read routes shared by nginx and local preview."""

from __future__ import annotations

from .proxy_policy import harden_proxy

import re

PREFIX = "/_northline/"
HOSTS = {"mastodon": "mastodon.ml", "legacy": "mastodon.social"}
# Query strings intentionally match the client verbatim. No arbitrary upstream,
# credentials, remote resolution, writes, or unbounded listing parameters.
READ_ROUTES = (
    (r"/api/v1/accounts/[0-9]{1,20}", r""),
    (
        r"/api/v1/accounts/[0-9]{1,20}/statuses",
        r"limit=20&exclude_replies=true(?:&exclude_reblogs=true)?(?:&max_id=[0-9]{1,20})?(?:&only_media=true)?(?:&pinned=true)?",
    ),
    (r"/api/v1/statuses/[0-9]{1,20}(?:/context)?", r""),
)
DISCOVERY_ROUTES = (
    (r"/api/v1/directory", r"local=true&order=active&limit=20"),
    (r"/api/v1/trends/tags", r"limit=8"),
    (r"/api/v2/search", r"q=[A-Za-z0-9_.~%*+-]{1,1800}&resolve=false&limit=20"),
)


def upstream_url(path: str, query: str) -> str | None:
    """Validate the complete route and query without decoding ambiguous escapes."""
    for source, host in HOSTS.items():
        prefix = PREFIX + source
        if not path.startswith(prefix + "/"):
            continue
        relative = path[len(prefix):]
        routes = READ_ROUTES + (DISCOVERY_ROUTES if source == "mastodon" else ())
        if any(re.fullmatch(route, relative) and re.fullmatch(args, query) for route, args in routes):
            return f"https://{host}{relative}" + (f"?{query}" if query else "")
    return None


def render_proxy(*, legacy: bool = False, secure: bool = True) -> str:
    """Render static upstreams, so user input can never select a destination."""
    blocks = []
    for source, host in HOSTS.items():
        prefix = PREFIX + source
        routes = READ_ROUTES + (DISCOVERY_ROUTES if source == "mastodon" else ())
        for route, args in routes:
            if legacy:
                args = args.replace("(?:&exclude_reblogs=true)?", "")
            blocks.append(f'''    location ~ "^{prefix}{route}$" {{
        if ($request_method != GET) {{ return 405; }}
        if ($args !~ "^{args}$") {{ return 400; }}
        rewrite ^{prefix}(/api/.*)$ $1 break;
        proxy_pass https://{host};
        proxy_ssl_server_name on;
        proxy_ssl_name {host};
        proxy_ssl_verify on;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_ssl_verify_depth 4;
        proxy_pass_request_headers off;
        proxy_pass_request_body off;
        proxy_set_header Host {host};
        proxy_set_header Accept application/json;
        proxy_set_header User-Agent "Mozilla/5.0 (compatible; Northline/1.0)";
        proxy_set_header Content-Length "";
        proxy_hide_header Set-Cookie;
        proxy_hide_header Access-Control-Allow-Origin;
        proxy_hide_header Cache-Control;
        add_header Cache-Control "no-store" always;
        add_header X-Content-Type-Options "nosniff" always;
        proxy_intercept_errors on;
        error_page 301 302 303 307 308 =502 @northline_upstream_error;
        proxy_connect_timeout 5s;
        proxy_read_timeout 15s;
        proxy_send_timeout 5s;
    }}''')
    blocks.extend([
        "    location @northline_upstream_error { return 502; }",
        "    location /_northline/ { return 404; }",
    ])
    result = "\n\n".join(blocks)
    return harden_proxy(result, "northline") if secure else result
