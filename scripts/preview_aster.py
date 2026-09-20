"""Preview ASTER with fixed Rutube search, comment and image routes."""

from __future__ import annotations

import argparse
import json
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import Request


# Shared image proxy for development; installed sites use fixed nginx locations.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from remnawave_manager.image_preview import OPENER, serve_preview_asset  # noqa: E402

from remnawave_manager.site_assets import FreshAssetsHandler
from remnawave_manager.aster_proxy import PREFIX as COMMENTS_PREFIX, upstream_url, search_upstream, SEARCH_PATH, metadata_upstream

SITE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "remnawave_manager"
    / "data"
    / "disguises"
    / "02-aster-observatory"
)
SHARED = SITE.parent / "shared"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class AsterPreviewHandler(FreshAssetsHandler):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(SITE), **kwargs)

    def do_GET(self) -> None:  # noqa: N802 - inherited HTTP handler name
        if serve_preview_asset(self):
            return
        request = urlsplit(self.path)
        if request.path.startswith(("/_aster/rutube-video/", "/_aster/rutube-profile/")):
            upstream = metadata_upstream(request.path, request.query)
            if upstream is None:
                self._json_error(400, "Invalid metadata request")
            else:
                self._serve_json(upstream)
            return
        if request.path.startswith(COMMENTS_PREFIX):
            self._serve_comments(request)
            return
        if request.path != SEARCH_PATH:
            self._serve_static()
            return

        upstream = search_upstream(request.path, request.query)
        if upstream is None:
            self._json_error(400, "Invalid search request")
            return
        self._serve_json(upstream)

    def _serve_comments(self, target) -> None:
        upstream = upstream_url(target.path, target.query)
        if upstream is None:
            self._json_error(400, "Invalid comments request")
            return
        self._serve_json(upstream)

    def _serve_json(self, upstream: str) -> None:
        try:
            request = Request(upstream, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0", "Referer": "https://rutube.ru/"})
            with OPENER.open(request, timeout=15) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise ValueError("RUTUBE response too large")
                json.loads(payload)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            status = error.code if isinstance(error, HTTPError) and error.code in {404, 429} else 502
            if isinstance(error, HTTPError):
                error.close()
            self._json_error(status, "RUTUBE is temporarily unavailable")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; sandbox")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self._write(payload)

    def do_HEAD(self) -> None:  # noqa: N802 - inherited HTTP handler name
        """Keep the preview's static files out of a different site's cache."""
        if serve_preview_asset(self):
            return
        if urlsplit(self.path).path.startswith("/_aster/"):
            self.send_error(405)
            return
        self._serve_static(head_only=True)

    def _serve_static(self, *, head_only: bool = False) -> None:
        # Morrow's preview historically used the same localhost port.  A browser
        # can then validate Morrow's cached /index.html against this server and
        # reuse it after a 304 response, making ASTER request morrow-i18n.js.
        # Preview files are small, so always sending a fresh, non-cacheable
        # response is the least surprising behaviour during template work.
        if "If-Modified-Since" in self.headers:
            del self.headers["If-Modified-Since"]
        self._serving_static = True
        try:
            if head_only:
                super().do_HEAD()
            else:
                super().do_GET()
        finally:
            self._serving_static = False

    def end_headers(self) -> None:
        if getattr(self, "_serving_static", False):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def translate_path(self, path: str) -> str:
        """Expose shared browser modules in the same layout as an installed site."""
        request_path = unquote(urlsplit(path).path)
        if request_path == "/shared" or request_path.startswith("/shared/"):
            relative = request_path.removeprefix("/shared").lstrip("/")
            candidate = (SHARED / relative).resolve()
            if candidate == SHARED or SHARED in candidate.parents:
                return str(candidate)
            return str(SHARED / "__invalid_path__")
        return super().translate_path(path)

    def _json_error(self, status: int, message: str) -> None:
        payload = json.dumps({"detail": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self._write(payload)

    def _write(self, payload: bytes) -> None:
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5502)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), AsterPreviewHandler)
    print(f"ASTER preview: http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
