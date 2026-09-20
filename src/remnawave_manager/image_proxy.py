"""Fixed image origins shared by nginx generation and development previews.

No application backend is installed. Each nginx location has a literal upstream;
client input cannot choose a host, scheme or port. Redirects are not followed.
"""
from __future__ import annotations

from .proxy_policy import harden_proxy

import re
from urllib.parse import urlsplit

PREFIX = "/_images/"
HOSTS = (
    "mastodon.ml",
    "mastodon.social",
    "files.mastodon.social",
    "pic.rtbcdn.ru",
    "pic.rutube.ru",
    "static.rutubelist.ru",
    "cdn-st.rutubelist.ru",
    "cdn-st.yappy.media",
    "otvet.cdn-vk.net",
    "filin.mail.ru",
    "otvet-static.cdn-vk.net",
    "ru.wikipedia.org",
    "upload.wikimedia.org",
    "thumb.wikimedia.org",
    "wikimedia.org",
)
# Reject ambiguous escaping; ordinary UTF-8 filenames and query strings survive.
UNSAFE_PATH = r"(?i)(?:%(?:00|0a|0d|2f|5c|25)|\\|(?:^|/)(?:\.|%2e){1,2}(?:/|$)|//)"


IMAGE_SUFFIX = r"\.(?:png|jpe?g|gif|webp|avif|svg|ico)$"
MATH_PATH = r"/api/rest_v1/media/math/render/(?:svg|png)/[a-f0-9]{1,128}"


def allowed_image_path(host: str, path: str) -> bool:
    if host == "filin.mail.ru":
        return path == "/pic"
    return bool(re.search(IMAGE_SUFFIX, path, re.I) or
                (host == "ru.wikipedia.org" and re.fullmatch(MATH_PATH, path)))


def upstream_url(target: str) -> str | None:
    if not target.startswith(PREFIX) or any(ord(c) < 32 for c in target):
        return None
    parts = urlsplit(target)
    if parts.fragment or re.search(UNSAFE_PATH, parts.path):
        return None
    host, separator, path = parts.path[len(PREFIX):].partition("/")
    if host not in HOSTS or not separator or not path or not allowed_image_path(host, "/" + path):
        return None
    return "https://" + host + "/" + path + ("?" + parts.query if parts.query else "")


def render_proxy(hosts: tuple[str, ...] = HOSTS, *, secure: bool = True) -> str:
    blocks = []
    for host in hosts:
        # Prefix proxy_pass preserves escaped Unicode filenames and the query.
        blocks.append(f'''    location ^~ {PREFIX}{host}/ {{
        if ($request_method !~ "^(GET|HEAD)$") {{ return 405; }}
        if ($request_uri ~* "^[^?]*%(?:00|0a|0d|2f|5c|25)") {{ return 400; }}
        proxy_pass https://{host}/;
        proxy_ssl_server_name on;
        proxy_ssl_name {host};
        proxy_ssl_verify on;
        proxy_ssl_verify_depth 4;
        proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;
        proxy_pass_request_headers off;
        proxy_pass_request_body off;
        proxy_set_header Host {host};
        proxy_set_header Accept "image/*";
        proxy_set_header User-Agent "Mozilla/5.0 (compatible; SitePreview/1.0)";
        proxy_set_header Content-Length "";
        proxy_hide_header Set-Cookie;
        proxy_hide_header Location;
        proxy_hide_header Refresh;
        proxy_hide_header Cache-Control;
        proxy_hide_header Expires;
        proxy_hide_header Content-Security-Policy;
        proxy_hide_header Access-Control-Allow-Origin;
        proxy_hide_header X-Content-Type-Options;
        proxy_cache off;
        proxy_store off;
        proxy_max_temp_file_size 0;
        proxy_redirect off;
        proxy_intercept_errors on;
        error_page 301 302 303 307 308 =502 @image_upstream_error;
        add_header Cache-Control "no-store" always;
        add_header Content-Security-Policy "default-src 'none'; sandbox" always;
        add_header X-Content-Type-Options nosniff always;
        proxy_connect_timeout 5s;
        proxy_read_timeout 20s;
        proxy_send_timeout 5s;
    }}''')
    blocks.extend([
        "    location @image_upstream_error { return 502; }",
        "    location /_images/ { return 404; }",
    ])
    result = "\n\n".join(blocks)
    return harden_proxy(result, "images") if secure else result
