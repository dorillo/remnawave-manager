"""Security defaults for generated, fixed-origin disguise proxy locations.

This transforms our own templates, never arbitrary nginx configuration. Keeping
raw templates available also permits exact upgrades of previously shipped blocks.
"""
from __future__ import annotations

import re

MEDIA_TYPES = {
    'png': 'image/png', 'jpg|jpeg': 'image/jpeg', 'gif': 'image/gif',
    'webp': 'image/webp', 'avif': 'image/avif', 'svg': 'image/svg+xml',
    'mp4': 'video/mp4',
}
HIDDEN_HEADERS = (
    'Set-Cookie', 'Location', 'Refresh', 'Cache-Control', 'Expires',
    'Content-Security-Policy', 'X-Content-Type-Options', 'Content-Type',
    'Access-Control-Allow-Origin', 'Content-Disposition', 'Link',
)


def harden_proxy(text: str, name: str) -> str:
    """Apply consistent transport, cache and response isolation to every route."""
    error = re.search(r'error_page 301 302 303 307 308 =502 (@\w+);', text)
    error_target = error[1] if error else f'@rwm_{name}_upstream_error'
    chunks = re.split(r'(?=^    location )', text, flags=re.M)
    for index, chunk in enumerate(chunks):
        if 'proxy_pass ' not in chunk:
            continue
        media = any(path in chunk.splitlines()[0] for path in ('/_images/', '/_loop/media/', '/_fokus/media/'))
        # Discard only settings replaced below. Request routes and guards stay intact.
        directives = ('proxy_cache', 'proxy_store', 'proxy_max_temp_file_size',
                      'proxy_redirect', 'proxy_intercept_errors', 'proxy_send_timeout',
                      'proxy_ssl_verify', 'proxy_ssl_verify_depth', 'proxy_ssl_trusted_certificate')
        chunk = re.sub(r'^        (?:' + '|'.join(directives) + r')\s+[^\n]+;\n', '', chunk, flags=re.M)
        chunk = re.sub(r'^        proxy_hide_header (?:' + '|'.join(HIDDEN_HEADERS) + r');\n', '', chunk, flags=re.M)
        chunk = re.sub(r'^        add_header (?:Cache-Control|Content-Security-Policy|X-Content-Type-Options|Content-Type) [^\n]+;\n', '', chunk, flags=re.M)
        chunk = re.sub(r'^        error_page 301 302 303 307 308 =502 @\w+;\n', '', chunk, flags=re.M)
        settings = [
            'proxy_ssl_verify on;', 'proxy_ssl_verify_depth 4;',
            'proxy_ssl_trusted_certificate /etc/ssl/certs/ca-certificates.crt;',
            'proxy_ignore_headers X-Accel-Redirect X-Accel-Expires X-Accel-Limit-Rate X-Accel-Buffering X-Accel-Charset;',
            'proxy_cache off;', 'proxy_store off;', 'proxy_max_temp_file_size 0;',
            'expires off;', 'proxy_redirect off;', 'proxy_intercept_errors on;',
            f'error_page 301 302 303 307 308 =502 {error_target};',
            'proxy_send_timeout 5s;',
            *(f'proxy_hide_header {header};' for header in HIDDEN_HEADERS),
            'add_header Cache-Control "no-store" always;',
            'add_header Content-Security-Policy "default-src \'none\'; sandbox" always;',
            'add_header X-Content-Type-Options nosniff always;',
        ]
        if name == 'images':
            # Fixed hosts are not a license to expose arbitrary API/admin paths.
            from .image_proxy import IMAGE_SUFFIX, MATH_PATH
            host = re.search(r'proxy_pass https://([^/;]+)', chunk)[1]
            prefix = re.escape('/_images/' + host)
            allowed = (f'^{prefix}/pic$' if host == 'filin.mail.ru' else
                       f'^{prefix}/.*{IMAGE_SUFFIX}')
            if host == 'ru.wikipedia.org':
                allowed = f'(?:{allowed}|^{prefix}{MATH_PATH}$)'
            settings.append(f'if ($uri !~* "{allowed}") {{ return 404; }}')
        if media:
            # Do not let an upstream serve same-origin HTML/JS through an image URL.
            # Raster images without an extension (Mail /pic) can be sniffed by img;
            # nosniff still prevents their use as scripts/styles. SVG needs its MIME.
            settings.append('set $rwm_media_type application/octet-stream;')
            settings.append('if ($uri ~ "/media/math/render/svg/") { set $rwm_media_type image/svg+xml; }')
            for extensions, mime in MEDIA_TYPES.items():
                settings.append(f'if ($uri ~* "\\.(?:{extensions})$") {{ set $rwm_media_type {mime}; }}')
            settings.append('add_header Content-Type $rwm_media_type always;')
        else:
            mime = 'text/plain' if name == 'fokus' else 'application/json'
            settings.append(f'add_header Content-Type "{mime}; charset=utf-8" always;')
        if 'proxy_set_header Content-Type application/json;' not in chunk:
            if 'proxy_pass_request_body off;' not in chunk:
                settings.append('proxy_pass_request_body off;')
            if 'proxy_set_header Content-Length "";' not in chunk:
                settings.append('proxy_set_header Content-Length "";')
        # Rewrite-phase sets must precede any rewrite ... break directive.
        line_end = chunk.index('\n') + 1
        chunks[index] = chunk[:line_end] + ''.join('        ' + line + '\n' for line in settings) + chunk[line_end:]
    result = ''.join(chunks)
    failure = (f'    location {error_target} {{\n'
               '        add_header Cache-Control "no-store" always;\n'
               '        add_header X-Content-Type-Options nosniff always;\n'
               '        return 502;\n    }')
    if error:
        result = result.replace(f'    location {error_target} {{ return 502; }}', failure)
    else:
        result += '\n\n' + failure
    return result
