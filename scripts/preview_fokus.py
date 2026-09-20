"""Development preview for Fokus. Production uses only static files and nginx."""
from __future__ import annotations
import argparse
import gzip
import sys
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from remnawave_manager.fokus_proxy import upstream, CSP
from remnawave_manager.site_assets import FreshAssetsHandler

SITE = ROOT / 'src/remnawave_manager/data/disguises/07-fokus-news'
LIMIT = 8 * 1024 * 1024

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None

class FokusPreviewHandler(FreshAssetsHandler):
    upstream = staticmethod(upstream)
    def __init__(self, *args, **kwargs): super().__init__(*args, directory=str(SITE), **kwargs)
    def end_headers(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'none'; sandbox" if self.path.startswith('/_fokus/') else CSP)
        super().end_headers()
    def do_GET(self):
        request = urlsplit(self.path)
        if not request.path.startswith('/_fokus/'):
            if '..' in request.path or self.translate_path(request.path) and not Path(self.translate_path(request.path)).resolve().is_relative_to(SITE.resolve()):
                self.send_error(403); return
            super().do_GET(); return
        url = upstream(request.path, request.query)
        if not url: self.send_error(400); return
        try:
            with build_opener(NoRedirect).open(Request(url, headers={'User-Agent':'Mozilla/5.0 (compatible; FokusPreview/1.0)', 'Accept-Encoding':'identity'}), timeout=15) as response:
                data = response.read(LIMIT + 1)
                if len(data) > LIMIT: raise ValueError('response too large')
                if response.headers.get('Content-Encoding') == 'gzip':
                    import io
                    with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream: data = stream.read(LIMIT + 1)
                if len(data) > LIMIT: raise ValueError('response too large')
                content = response.headers.get('Content-Type', '')
                if '/media/' in request.path and content.split(';')[0] not in {'image/jpeg','image/png','image/webp'}: raise ValueError('unsupported media')
        except HTTPError as error:
            error.close()
            self.send_error(error.code if error.code in (404,429) else 502); return
        except (OSError, ValueError): self.send_error(502); return
        self.send_response(200)
        self.send_header('Content-Type', content if '/media/' in request.path else 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try: self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError): pass
    def do_HEAD(self): self.send_error(405)
    def do_POST(self): self.send_error(405)
    do_PUT = do_PATCH = do_DELETE = do_POST
    def list_directory(self, path): self.send_error(403); return None

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=5507)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host,args.port), FokusPreviewHandler)
    print(f'Fokus preview: http://{args.host}:{args.port}/', flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
