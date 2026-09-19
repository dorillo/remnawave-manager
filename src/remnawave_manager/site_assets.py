"""Version static module graphs so new HTML cannot load an older CSP-incompatible UI."""
from __future__ import annotations

import hashlib
import io
import re
from urllib.parse import urlsplit
from http.server import SimpleHTTPRequestHandler
from pathlib import Path

MODULE = re.compile(r'''((?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s*)["'])(\.{1,2}/[^"'?]+\.js)(?:\?[^"']*)?(["'])''')
HTML_ASSET = re.compile(r'''((?:src|href)=["'])(?!https?:|//)([^"'?]+\.(?:js|css))(?:\?[^"']*)?(["'])''')


def revision(site: Path, shared: Path) -> str:
    digest = hashlib.sha256()
    for scope, directory in enumerate((site, shared)):
        for path in sorted(directory.rglob('*')):
            if not path.is_symlink() and path.is_file() and path.suffix in {'.js', '.html', '.css'}:
                key = f'{scope}/{path.relative_to(directory)}'.encode()
                payload = path.read_bytes()
                digest.update(len(key).to_bytes(8, 'big'))
                digest.update(key)
                digest.update(len(payload).to_bytes(8, 'big'))
                digest.update(payload)
    return digest.hexdigest()[:16]


def versioned(source: bytes, suffix: str, version: str) -> bytes:
    pattern = MODULE if suffix == '.js' else HTML_ASSET
    return pattern.sub(lambda m: f'{m[1]}{m[2]}?v={version}{m[3]}', source.decode('utf-8-sig')).encode()


class FreshAssetsHandler(SimpleHTTPRequestHandler):
    """Development only: serve an entire module graph at one content revision."""
    def setup(self):
        super().setup()
        self.connection.settimeout(25)

    def send_header(self, keyword, value):
        path = urlsplit(getattr(self, 'path', '')).path
        if path.startswith('/_'):
            if keyword.lower() in {'cache-control', 'content-security-policy', 'x-content-type-options'}:
                return  # Emit one consistent policy, including error responses.
            if keyword.lower() == 'content-type':
                from .proxy_policy import MEDIA_TYPES
                if path.startswith(('/_images/', '/_loop/media/', '/_fokus/media/')):
                    value = 'image/svg+xml' if '/media/math/render/svg/' in path else 'application/octet-stream'
                    for extensions, mime in MEDIA_TYPES.items():
                        if re.search(r'\.(?:' + extensions + r')$', path, re.I):
                            value = mime
                            break
                else:
                    value = 'text/plain; charset=utf-8' if path.startswith('/_fokus/') else 'application/json; charset=utf-8'
        super().send_header(keyword, value)

    def end_headers(self):
        if urlsplit(getattr(self, 'path', '')).path.startswith('/_'):
            super().send_header('Cache-Control', 'no-store')
            super().send_header('Content-Security-Policy', "default-src 'none'; sandbox")
            super().send_header('X-Content-Type-Options', 'nosniff')
        super().end_headers()

    def list_directory(self, path):
        self.send_error(404)
        return None

    def send_head(self):
        path = Path(self.translate_path(self.path))
        site = Path(self.directory).resolve()
        shared = site.parent / 'shared'
        roots = (site, shared.resolve()) if not shared.is_symlink() else (site,)
        if path.is_dir():
            path = next((path / name for name in ('index.html', 'index.htm')
                         if (path / name).exists()), path / 'index.html')
        resolved = path.resolve()
        if not any(resolved.is_relative_to(root) and
                   not any(part.startswith('.') for part in resolved.relative_to(root).parts)
                   for root in roots):
            self.send_error(404)
            return None
        if path.suffix not in {'.js', '.html', '.css'} or not path.is_file():
            return super().send_head()
        site = Path(self.directory)
        payload = path.read_bytes()
        if path.suffix in {'.js', '.html'}:
            payload = versioned(payload, path.suffix, revision(site, site.parent / 'shared'))
        self.send_response(200)
        self.send_header('Content-Type', self.guess_type(str(path)))
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        return io.BytesIO(payload)
