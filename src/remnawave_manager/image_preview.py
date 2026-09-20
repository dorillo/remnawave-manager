"""Image routes for local preview scripts only; production uses nginx."""
from __future__ import annotations

from http.server import SimpleHTTPRequestHandler
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .image_proxy import PREFIX, upstream_url


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


OPENER = build_opener(NoRedirect)
MAX_IMAGE_BYTES = 32 * 1024 * 1024


def serve_preview_asset(handler: SimpleHTTPRequestHandler) -> bool:
    """Return whether a shared module or fixed image route handled the request."""
    path = urlsplit(handler.path).path
    if path == "/shared/image-proxy.js":
        payload = (Path(__file__).parent / "data/disguises/shared/image-proxy.js").read_bytes()
        content_type = "text/javascript; charset=utf-8"
    elif path.startswith(PREFIX):
        upstream = upstream_url(handler.path)
        if upstream is None:
            handler.send_error(400, "Invalid image request")
            return True
        try:
            request = Request(upstream, method=handler.command, headers={
                "Accept": "image/*",
                "User-Agent": "Mozilla/5.0 (compatible; SitePreview/1.0)",
            })
            with OPENER.open(request, timeout=20) as response:
                content_type = response.headers.get("Content-Type", "application/octet-stream")
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_IMAGE_BYTES:
                    raise ValueError("Image too large for preview")
                payload = response.read(MAX_IMAGE_BYTES + 1) if handler.command != "HEAD" else b""
                if len(payload) > MAX_IMAGE_BYTES:
                    raise ValueError("Image too large for preview")
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            if isinstance(error, HTTPError):
                error.close()
            handler.send_error(502, "Image source unavailable")
            return True
    else:
        return False
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    if path.startswith(PREFIX):
        handler.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
    if handler.command != "HEAD":
        handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    if handler.command != "HEAD":
        try:
            handler.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass
    return True
