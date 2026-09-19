"""Exercise the generated nginx locations against a local deterministic upstream."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from remnawave_manager.aster_proxy import render_proxy as render_comments
from remnawave_manager.image_proxy import HOSTS, render_proxy


@unittest.skipUnless(shutil.which('nginx'), 'nginx is not installed')
class ImageNginxTests(unittest.TestCase):
    def test_rewrite_headers_methods_and_redirects(self):
        seen = []

        class Upstream(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, dict(self.headers)))
                self.send_response(302 if self.path == '/redirect.png' else 200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Set-Cookie', 'upstream=private')
                self.send_header('Cache-Control', 'public, max-age=3600')
                self.send_header('Location', 'https://evil.test/redirect.png')
                self.end_headers()
                self.wfile.write(b'image')

            def log_message(self, *args):
                pass

        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        # Reserve a free port without starting a second test web server.
        import socket
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        opener = build_opener(ProxyHandler({}))
        process = None
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                proxy = render_proxy() + "\n" + render_comments()
                for host in HOSTS:
                    proxy = proxy.replace(f'proxy_pass https://{host}/;', f'proxy_pass http://127.0.0.1:{upstream.server_port}/;')
                proxy = proxy.replace('proxy_pass https://rutube.ru;', f'proxy_pass http://127.0.0.1:{upstream.server_port};')
                # HTTP fixture: TLS correctness remains covered by configuration checks.
                proxy = '\n'.join(line for line in proxy.splitlines() if 'proxy_ssl_' not in line)
                config = root / 'nginx.conf'
                config.write_text(f'pid {root}/nginx.pid;\nerror_log stderr;\nevents {{}}\nhttp {{ access_log off; server {{ listen 127.0.0.1:{port};\n{proxy}\n}} }}')
                process = subprocess.Popen(['nginx', '-p', str(root), '-c', str(config), '-g', 'daemon off;'], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                base = f'http://127.0.0.1:{port}'
                for _ in range(50):
                    try:
                        opener.open(base + '/_images/unknown/a.png', timeout=.2)
                    except HTTPError as error:
                        self.assertEqual(error.code, 404)
                        error.close()
                        break
                    except URLError:
                        if process.poll() is not None:
                            self.fail(process.stderr.read().decode())
                        time.sleep(.05)
                else:
                    self.fail('nginx did not start')
                path = '/wikipedia/commons/%D0%A4%D0%BE%D1%82%D0%BE.svg.png?width=400&v=2'
                request = Request(base + '/_images/upload.wikimedia.org' + path, headers={'Cookie': 'session=secret', 'Authorization': 'Bearer secret'})
                with opener.open(request, timeout=3) as response:
                    self.assertEqual(response.read(), b'image')
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')
                    self.assertIsNone(response.headers.get('Location'))
                    self.assertIsNone(response.headers.get('Set-Cookie'))
                    self.assertIn('sandbox', response.headers['Content-Security-Policy'])
                self.assertEqual(seen[0][0], path)
                self.assertEqual(seen[0][1]['Host'], 'upload.wikimedia.org')
                self.assertNotIn('Cookie', seen[0][1])
                self.assertNotIn('Authorization', seen[0][1])
                for method, suffix, status in [('POST', '/a.png', 405), ('GET', '/redirect.png', 502), ('GET', '/a%252fb.png', 400)]:
                    with self.subTest(method=method, suffix=suffix), self.assertRaises(HTTPError) as caught:
                        opener.open(Request(base + '/_images/pic.rtbcdn.ru' + suffix, method=method), timeout=3)
                    self.assertEqual(caught.exception.code, status)
                    caught.exception.close()
                self.assertEqual(len(seen), 2)
                video_id = 'a' * 32
                with opener.open(base + '/_aster/rutube-comments/' + video_id + '?comment_id=123&parent_id=456', timeout=3) as response:
                    self.assertEqual(response.status, 200)
                self.assertEqual(seen[-1][0], f'/api/v2/comments/video/{video_id}/?client=wdp&sort_by=date_added_desc&direction=earliest&comment_id=123&parent_id=456')
                self.assertEqual(seen[-1][1]['Referer'], 'https://rutube.ru/')
                process.terminate()
                process.wait(timeout=5)
        finally:
            if process is not None:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)
                process.stderr.close()
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=2)
