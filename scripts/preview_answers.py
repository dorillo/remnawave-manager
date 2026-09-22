"""Preview the Spros template with a narrow, read-only Mail Answers proxy."""

from __future__ import annotations

import argparse
import json
import sys
import re
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

# Shared image proxy for development; installed sites use fixed nginx locations.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from remnawave_manager.image_preview import serve_preview_asset  # noqa: E402

from remnawave_manager.site_assets import FreshAssetsHandler

SITE = Path(__file__).resolve().parents[1] / "src/remnawave_manager/data/disguises/04-signal-works"
PREFIX = "/_answers/mail/"
UPSTREAM = "https://otvet.mail.ru"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
ID = re.compile(r"[0-9]{1,12}")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None


OPENER = build_opener(NoRedirect)


class AnswersPreviewHandler(FreshAssetsHandler):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(SITE), **kwargs)

    def do_HEAD(self) -> None:
        if serve_preview_asset(self):
            return
        super().do_HEAD()

    def do_GET(self) -> None:  # noqa: N802
        if serve_preview_asset(self):
            return
        request = urlsplit(self.path)
        if not request.path.startswith(PREFIX):
            super().do_GET()
            return
        upstream = self._upstream_url(request.path, request.query)
        if upstream is None:
            self._json_error(400, "Invalid Answers request")
            return
        headers = {"Accept": "application/json", "User-Agent": "Mozilla/5.0 (compatible; SprosPreview/1.0)"}
        try:
            with OPENER.open(Request(upstream, headers=headers), timeout=15) as response:
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_RESPONSE_BYTES:
                    raise ValueError("Mail response too large")
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise ValueError("Mail response too large")
                json.loads(payload)
                retry_after = response.headers.get("Retry-After")
        except HTTPError as error:
            error.close()
            status = error.code if error.code in {404, 429} else 502
            self._json_error(status, "Mail is unavailable", retry_after=error.headers.get("Retry-After") if status == 429 else None)
            return
        except (URLError, TimeoutError, ValueError, json.JSONDecodeError, OSError):
            self._json_error(502, "Mail is unavailable")
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
        if relative == "feed" and {"limit"} <= set(params) <= {"limit", "pos", "space"}:
            if params["limit"] != ["20"]:
                return None
            position = params.get("pos", [None])
            if position != [None] and (len(position) != 1 or not ID.fullmatch(position[0])):
                return None
            space = params.get("space", [None])
            if space != [None] and (len(space) != 1 or not re.fullmatch(r"[a-z0-9_-]{1,70}", space[0])):
                return None
            return f"{UPSTREAM}/api/topic/feed?{urlencode({'limit': '20', **({'pos': position[0]} if position != [None] else {}), **({'space': space[0]} if space != [None] else {})})}"
        if relative == "spaces" and not params:
            return f"{UPSTREAM}/api/topic/spaces/top"
        if relative == "search" and set(params) == {"text"} and len(params["text"]) == 1:
            term = params["text"][0].strip()
            if 1 <= len(term) <= 120 and not any(ord(char) < 32 for char in term):
                return f"{UPSTREAM}/api/topic/search?{urlencode({'text': term})}"
            return None
        profile = re.fullmatch(r"profile/([0-9]{1,12})/(topics|replies|count)", relative)
        if profile:
            identifier, kind = profile.groups()
            if kind == "count":
                return f"{UPSTREAM}/api/topic/profile/{identifier}/content/count" if not params else None
            if params == {"limit": ["20"], "dir": ["0"]}:
                return f"{UPSTREAM}/api/topic/profile/{identifier}/{kind}?limit=20&dir=0"
            if set(params) == {"limit", "dir", "pos"} and params["limit"] == ["20"] and params["dir"] == ["1"] and len(params["pos"]) == 1 and ID.fullmatch(params["pos"][0]):
                return f"{UPSTREAM}/api/topic/profile/{identifier}/{kind}?{urlencode({'limit': '20', 'dir': '1', 'pos': params['pos'][0]})}"
            return None
        route, slash, identifier = relative.partition("/")
        if slash != "/" or not ID.fullmatch(identifier):
            return None
        if route == "question" and not params:
            return f"{UPSTREAM}/api/topic/question/{identifier}"
        if route == "answers" and {"limit"} <= set(params) <= {"limit", "pos", "reply_id"} and params["limit"] == ["50"]:
            if any(len(params[key]) != 1 or not ID.fullmatch(params[key][0]) for key in params if key != "limit"):
                return None
            query = urlencode({"limit": "50", **{key: params[key][0] for key in ("pos", "reply_id") if key in params}})
            return f"{UPSTREAM}/api/topic/answers/{identifier}?{query}"
        return None

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
    parser.add_argument("--port", type=int, default=5504)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), AnswersPreviewHandler)
    print(f"Spros preview: http://{args.host}:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
