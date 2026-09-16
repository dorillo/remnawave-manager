"""Serve the Morrow template locally with its narrow, read-only Yappy proxy."""

from __future__ import annotations

import argparse
import json
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen


SITE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "remnawave_manager"
    / "data"
    / "disguises"
    / "03-morrow-coffee"
)
UPSTREAM = "https://yappy.media"
PREFIX = "/_morrow/yappy/"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
ID = re.compile(r"[a-f0-9]{32}")
CREATED = re.compile(r"[0-9A-Fa-fT:.Z%+\-]{1,100}")


class MorrowPreviewHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(SITE), **kwargs)

    def do_GET(self) -> None:  # noqa: N802 - inherited HTTP handler name
        request = urlsplit(self.path)
        if not request.path.startswith(PREFIX):
            super().do_GET()
            return

        upstream = self._upstream_url(request.path, request.query)
        if upstream is None:
            self._json_error(400, "Invalid Morrow API request")
            return

        headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; MorrowVideoPreview/1.0)",
        }
        try:
            with urlopen(Request(upstream, headers=headers), timeout=15) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_RESPONSE_BYTES:
                    raise ValueError("Yappy response is too large")
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise ValueError("Yappy response is too large")
                json.loads(payload)
                retry_after = response.headers.get("Retry-After")
        except HTTPError as error:
            status = error.code if error.code in {404, 429} else 502
            self.log_error("Yappy request failed: %s", error)
            self._json_error(
                status,
                "Yappy is temporarily unavailable",
                retry_after=error.headers.get("Retry-After") if status == 429 else None,
            )
            return
        except (URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
            self.log_error("Yappy request failed: %s", error)
            self._json_error(502, "Yappy is temporarily unavailable")
            return

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if retry_after:
            self.send_header("Retry-After", retry_after)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self._write(payload)

    def do_POST(self) -> None:  # noqa: N802 - inherited HTTP handler name
        self._reject_write()

    def do_PUT(self) -> None:  # noqa: N802 - inherited HTTP handler name
        self._reject_write()

    def do_PATCH(self) -> None:  # noqa: N802 - inherited HTTP handler name
        self._reject_write()

    def do_DELETE(self) -> None:  # noqa: N802 - inherited HTTP handler name
        self._reject_write()

    def _reject_write(self) -> None:
        if urlsplit(self.path).path.startswith(PREFIX):
            self._json_error(405, "Only GET is allowed")
            return
        self.send_error(405, "Method Not Allowed")

    def _upstream_url(self, path: str, query: str) -> str | None:
        relative = path.removeprefix(PREFIX)
        try:
            params = parse_qs(query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            return None

        if relative == "feed" and set(params) == {"page"}:
            page = self._page(params)
            if page:
                return f"{UPSTREAM}/api/feed?{urlencode({'page': page, 'fingerprint': ''})}"
            return None

        if relative == "search" and set(params) == {"query", "page"}:
            page = self._page(params)
            term = self._single(params, "query")
            if page and term and len(term) <= 120:
                return f"{UPSTREAM}/api/search/video?{urlencode({'query': term, 'page': page})}"
            return None

        route, separator, identifier = relative.partition("/")
        if separator != "/" or not ID.fullmatch(identifier):
            return None

        if route in {"video", "author"} and not params:
            resource = "video" if route == "video" else "profile/uid"
            return f"{UPSTREAM}/api/{resource}/{identifier}"

        if route == "comments" and set(params) == {"page"}:
            page = self._page(params)
            if page:
                return f"{UPSTREAM}/api/video/comments/{identifier}?{urlencode({'page': page})}"
            return None

        if route == "author-videos" and set(params) <= {"created"}:
            created = self._single(params, "created")
            if created is None:
                return f"{UPSTREAM}/api/video/list/{identifier}"
            if CREATED.fullmatch(created):
                return f"{UPSTREAM}/api/video/list/{identifier}?{urlencode({'created': created})}"
        return None

    @staticmethod
    def _single(params: dict[str, list[str]], name: str) -> str | None:
        values = params.get(name)
        return values[0] if values and len(values) == 1 else None

    def _page(self, params: dict[str, list[str]]) -> str | None:
        value = self._single(params, "page")
        if value and value.isascii() and value.isdigit() and 1 <= int(value) <= 9999:
            return value
        return None

    def _json_error(
        self, status: int, message: str, *, retry_after: str | None = None
    ) -> None:
        payload = json.dumps({"detail": message}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if retry_after:
            self.send_header("Retry-After", retry_after[:100])
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self._write(payload)

    def _write(self, payload: bytes) -> None:
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            # Browsers routinely cancel prefetched pages while the upstream
            # response is in flight. It is not a preview-server failure.
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5502)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), MorrowPreviewHandler)
    print(f"Morrow preview: http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
