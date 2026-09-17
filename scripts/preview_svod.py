"""Preview the Svod template with a narrow, read-only Wikipedia proxy."""

from __future__ import annotations

import argparse
import json
import re
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

SITE = Path(__file__).resolve().parents[1] / "src/remnawave_manager/data/disguises/05-field-notes"
PREFIX = "/_svod/wikipedia/"
UPSTREAM = "https://ru.wikipedia.org"
MAX_RESPONSE_BYTES = 6 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


OPENER = build_opener(NoRedirect)


class SvodPreviewHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(SITE), **kwargs)

    def list_directory(self, path):
        self.send_error(404)
        return None

    def translate_path(self, path):
        result = Path(super().translate_path(path)).resolve()
        if not result.is_relative_to(SITE.resolve()):
            return str(SITE / "__not_found__")
        return str(result)

    def do_GET(self) -> None:  # noqa: N802
        request = urlsplit(self.path)
        if not request.path.startswith(PREFIX):
            super().do_GET()
            return
        upstream = self._upstream_url(request.path, request.query)
        if upstream is None:
            self._json_error(400, "Invalid encyclopedia request")
            return
        headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 (compatible; SvodPreview/1.0)"}
        try:
            with OPENER.open(Request(upstream, headers=headers), timeout=15) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_RESPONSE_BYTES:
                    raise ValueError("Wikipedia response too large")
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise ValueError("Wikipedia response too large")
                json.loads(payload)
                retry_after = response.headers.get("Retry-After")
        except HTTPError as error:
            status = error.code if error.code in {404, 429} else 502
            self._json_error(status, "Wikipedia is unavailable", retry_after=error.headers.get("Retry-After") if status == 429 else None)
            return
        except (URLError, TimeoutError, ValueError, json.JSONDecodeError, OSError):
            self._json_error(502, "Wikipedia is unavailable")
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if retry_after:
            self.send_header("Retry-After", retry_after[:100])
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self._write(payload)

    def do_POST(self) -> None: self._reject_write()  # noqa: N802
    def do_PUT(self) -> None: self._reject_write()  # noqa: N802
    def do_PATCH(self) -> None: self._reject_write()  # noqa: N802
    def do_DELETE(self) -> None: self._reject_write()  # noqa: N802

    def _reject_write(self) -> None:
        if urlsplit(self.path).path.startswith(PREFIX):
            self._json_error(405, "Only GET is allowed")
        else:
            self.send_error(405)

    @staticmethod
    def _upstream_url(path: str, query: str) -> str | None:
        relative = path.removeprefix(PREFIX)
        try:
            params = parse_qs(query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            return None
        if any(len(values) != 1 for values in params.values()):
            return None
        values = {key: value[0] for key, value in params.items()}
        common = {"format": "json"}
        def title(value):
            return bool(value) and len(value) <= 300 and not any(ord(c) < 32 for c in value)
        if relative == "search" and set(values) == {"q", "offset"}:
            if not title(values["q"]) or not re.fullmatch(r"[0-9]{1,6}", values["offset"]):
                return None
            common.update(action="query", list="search", srsearch=values["q"], sroffset=values["offset"], srlimit="20", srnamespace="0")
        elif relative == "article" and set(values) == {"title"} and title(values["title"]):
            common.update(action="parse", page=values["title"], prop="text|categories|revid", redirects="1", disableeditsection="1")
        elif relative == "random" and not values:
            common.update(action="query", list="random", rnnamespace="0", rnlimit="1")
        elif relative == "category" and set(values) == {"title", "continue"} and title(values["title"]):
            if len(values["continue"]) > 1000 or any(ord(c) < 32 for c in values["continue"]):
                return None
            common.update(action="query", list="categorymembers", cmtitle=values["title"], cmlimit="30", cmtype="page|subcat")
            if values["continue"]:
                common["cmcontinue"] = values["continue"]
        else:
            return None
        return UPSTREAM + "/w/api.php?" + urlencode(common)

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
        self._write(payload)

    def _write(self, payload: bytes) -> None:
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5505)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), SvodPreviewHandler)
    print(f"Svod preview: http://{args.host}:{server.server_address[1]}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
