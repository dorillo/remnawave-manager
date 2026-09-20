"""Preview the Northline template with a narrow, read-only Mastodon proxy."""

from __future__ import annotations

import argparse
import json
import sys
from http.server import ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

# Works from a checkout and from an installed remnawave-manager package.
SOURCE = Path(__file__).resolve().parents[1] / "src"
if SOURCE.is_dir():
    sys.path.insert(0, str(SOURCE))
from remnawave_manager.northline_proxy import PREFIX, upstream_url  # noqa: E402

# Shared image proxy for development; installed sites use fixed nginx locations.
from remnawave_manager.image_preview import serve_preview_asset

from remnawave_manager.site_assets import FreshAssetsHandler

SITE = Path(str(files("remnawave_manager").joinpath("data/disguises/01-northline")))

MAX_RESPONSE_BYTES = 6 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


OPENER = build_opener(NoRedirect)


class NorthlinePreviewHandler(FreshAssetsHandler):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(SITE), **kwargs)

    def list_directory(self, path):
        self.send_error(404)
        return None

    def translate_path(self, path):
        resource = urlsplit(path).path
        if resource in {"/shared/date-utils.js", "/shared/storage.js", "/shared/image-proxy.js"}:
            return str(SITE.parent / resource.lstrip("/"))
        result = Path(super().translate_path(path)).resolve()
        if not result.is_relative_to(SITE.resolve()):
            return str(SITE / "__not_found__")
        return str(result)

    def do_GET(self) -> None:  # noqa: N802
        if serve_preview_asset(self):
            return
        request = urlsplit(self.path)
        if not request.path.startswith(PREFIX):
            super().do_GET()
            return
        upstream = self._upstream_url(request.path, request.query)
        if upstream is None:
            self._json_error(400, "Invalid Mastodon request")
            return
        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; NorthlinePreview/1.0)",
        }
        try:
            with OPENER.open(Request(upstream, headers=headers), timeout=15) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_RESPONSE_BYTES:
                    raise ValueError("Mastodon response too large")
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise ValueError("Mastodon response too large")
                json.loads(payload)
                retry_after = response.headers.get("Retry-After")
        except HTTPError as error:
            error.close()
            status = error.code if error.code in {404, 429} else 502
            retry_after = error.headers.get("Retry-After") if status == 429 else None
            error.close()
            self._json_error(status, "Mastodon is unavailable", retry_after=retry_after)
            return
        except (URLError, TimeoutError, ValueError, json.JSONDecodeError, OSError):
            self._json_error(502, "Mastodon is unavailable")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if retry_after:
            self.send_header("Retry-After", retry_after[:100])
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self._write(payload)

    def do_HEAD(self) -> None:  # noqa: N802
        if serve_preview_asset(self):
            return
        if urlsplit(self.path).path.startswith(PREFIX):
            self._json_error(405, "Only GET is allowed")
        else:
            super().do_HEAD()

    def do_POST(self) -> None:  # noqa: N802
        self._reject_write()

    def do_PUT(self) -> None:  # noqa: N802
        self._reject_write()

    def do_PATCH(self) -> None:  # noqa: N802
        self._reject_write()

    def do_DELETE(self) -> None:  # noqa: N802
        self._reject_write()


    def _reject_write(self) -> None:
        if urlsplit(self.path).path.startswith(PREFIX):
            self._json_error(405, "Only GET is allowed")
        else:
            self.send_error(405)

    _upstream_url = staticmethod(upstream_url)

    def _json_error(self, status: int, message: str, *, retry_after: str | None = None) -> None:
        payload = json.dumps({"detail": message}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if retry_after:
            self.send_header("Retry-After", retry_after[:100])
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self._write(payload)

    def _write(self, payload: bytes) -> None:
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5501)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), NorthlinePreviewHandler)
    print(f"Northline preview: http://{args.host}:{server.server_address[1]}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
