"""Serve the ASTER template locally with its narrow RUTUBE search proxy."""

from __future__ import annotations

import argparse
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from urllib.request import Request, urlopen


SITE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "remnawave_manager"
    / "data"
    / "disguises"
    / "02-aster-observatory"
)
SHARED = SITE.parent / "shared"
SEARCH_PATH = "/_aster/rutube-search"
SEARCH_URL = "https://rutube.ru/api/search/video/"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


class AsterPreviewHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(SITE), **kwargs)

    def do_GET(self) -> None:  # noqa: N802 - inherited HTTP handler name
        request = urlsplit(self.path)
        if request.path != SEARCH_PATH:
            super().do_GET()
            return

        params = parse_qs(request.query, keep_blank_values=True)
        query = params.get("query", [""])[0].strip()[:200]
        if not query:
            self._json_error(400, "Search query is required")
            return

        upstream = f"{SEARCH_URL}?{urlencode({'query': query, 'client': 'wdp'})}"
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; AsterVideoPreview/1.0)",
        }
        try:
            with urlopen(Request(upstream, headers=request_headers), timeout=15) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_RESPONSE_BYTES:
                    raise ValueError("RUTUBE response is too large")
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise ValueError("RUTUBE response is too large")
                json.loads(payload)
        except (HTTPError, URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
            self.log_error("RUTUBE search failed: %s", error)
            self._json_error(502, "RUTUBE search is temporarily unavailable")
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

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
        self.wfile.write(payload)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5500)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), AsterPreviewHandler)
    print(f"ASTER preview: http://{args.host}:{args.port}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
