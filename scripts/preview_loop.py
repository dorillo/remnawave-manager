"""Local Loop preview: static files and restricted anonymous GIFS proxy."""
from __future__ import annotations

import argparse
import json
import re
import sys
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from remnawave_manager.loop_proxy import MEDIA_PATTERN, ROUTES
from remnawave_manager.site_assets import FreshAssetsHandler

SITE = Path(__file__).resolve().parents[1] / "src/remnawave_manager/data/disguises/06-loop-archive"

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        return None

OPENER = build_opener(NoRedirect)

class LoopPreviewHandler(FreshAssetsHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(SITE), **kwargs)

    def list_directory(self, path):
        self.send_error(404)
        return None

    def translate_path(self, path):
        result = Path(super().translate_path(path)).resolve()
        return str(result if result.is_relative_to(SITE.resolve()) else SITE / '__missing__')

    @staticmethod
    def upstream(path, query, method):
        name = path.removeprefix('/_loop/gifs/')
        if path.startswith('/_loop/gifs/') and name in ROUTES:
            expected, upstream, pattern = ROUTES[name]
            if method == expected and re.fullmatch(pattern, query):
                return 'https://gifs.ru' + upstream + ('?' + query if query else '')
        match = re.fullmatch(r'/_loop/gifs/item/([0-9]{1,12})', path)
        if match and not query and method == 'GET':
            return 'https://gifs.ru/api/v1/File/FileById/' + match[1]
        match = re.fullmatch(r'/_loop/media/(' + MEDIA_PATTERN + ')', path)
        if match and not query and method in ('GET', 'HEAD'):
            return 'https://media.gifs.ru/' + match[1]
        return None

    def do_GET(self):
        self.handle_request()

    def do_HEAD(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def handle_request(self):
        parts = urlsplit(self.path)
        if not parts.path.startswith('/_loop/'):
            if self.command == 'GET': return super().do_GET()
            if self.command == 'HEAD': return super().do_HEAD()
            return self.send_error(405)
        url = self.upstream(parts.path, parts.query, self.command)
        if url is None: return self.send_error(400)
        headers = {'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0 (compatible; Loop/1.0)'}
        body = None
        if self.command == 'POST':
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 2048 or self.headers.get('Transfer-Encoding'): raise ValueError()
                body = self.rfile.read(size)
                if not isinstance(json.loads(body), dict): raise ValueError()
            except (ValueError, UnicodeError): return self.send_error(400)
            headers['Content-Type'] = 'application/json'
        media = parts.path.startswith('/_loop/media/')
        if media:
            headers['Accept'] = '*/*'
            for key in ('Range', 'If-Range'):
                if self.headers.get(key): headers[key] = self.headers[key]
        try:
            with OPENER.open(Request(url, data=body, headers=headers, method=self.command), timeout=20) as response:
                if self.command == 'HEAD': payload = b''
                else:
                    limit = 48 * 1024 * 1024 if media else 4 * 1024 * 1024
                    payload = response.read(limit + 1)
                    if len(payload) > limit: raise ValueError()
                    if not media: json.loads(payload)
                self.send_response(response.status)
                self.send_header('Content-Type', response.headers.get('Content-Type', 'application/octet-stream'))
                self.send_header('Content-Length', response.headers.get('Content-Length', '0') if self.command == 'HEAD' else str(len(payload)))
                self.send_header('X-Content-Type-Options', 'nosniff')
                self.send_header('Cache-Control', 'private, max-age=300' if media else 'no-store')
                for key in ('Accept-Ranges', 'Content-Range'):
                    if response.headers.get(key): self.send_header(key, response.headers[key])
                self.end_headers()
                if self.command != 'HEAD':
                    try: self.wfile.write(payload)
                    except (BrokenPipeError, ConnectionResetError): pass
        except HTTPError as error:
            error.close()
            self.send_error(error.code if error.code in (404, 429, 416) else 502)
        except (URLError, TimeoutError, OSError, ValueError): self.send_error(502)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5506)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), LoopPreviewHandler)
    print(f'Loop: http://{args.host}:{args.port}/', flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
