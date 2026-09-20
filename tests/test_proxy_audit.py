"""Release regression checks against real nginx and hostile upstream responses."""
from __future__ import annotations

import base64
import importlib.util
import json
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from remnawave_manager import site_policy as policy
from remnawave_manager.aster_proxy import search_upstream
from remnawave_manager.install import _install_node_site

ROOT = Path(__file__).resolve().parents[1]
GROUPS = ('northline', 'aster', 'morrow', 'answers', 'svod', 'loop', 'fokus')


def all_proxies():
    config = f'add_header Content-Security-Policy "{policy.NODE_CSP}" always;'
    for group in GROUPS:
        config = getattr(policy, f'upgrade_{group}_policy')(config)
    return config


class ProxyAuditTests(unittest.TestCase):
    def test_all_old_proxy_groups_upgrade_once(self):
        old = json.loads((ROOT / 'tests/fixtures/proxy_revisions/before_audit.json').read_text())
        targets = {'LEGACY_IMAGE_PROXY': 'IMAGE_PROXY', 'LEGACY_NORTHLINE_PROXY': 'NORTHLINE_PROXY', 'FOKUS_BEFORE_SEARCH': 'FOKUS_RIA_PROXY'}
        marker = f'add_header Content-Security-Policy "{policy.NODE_CSP}" always;'
        for name, block in old.items():
            for group in GROUPS:
                with self.subTest(name=name, group=group):
                    upgrade = getattr(policy, f'upgrade_{group}_policy')
                    result = upgrade(marker + '\n' + block)
                    self.assertIn(getattr(policy, targets.get(name, name)), result)
                    self.assertEqual(upgrade(result), result)
                    locations = re.findall(r'^    location (.+?) \{', result, re.M)
                    self.assertEqual(len(locations), len(set(locations)))

    def test_fresh_install_includes_entire_versioned_module_graph(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'site'
            source = ROOT / 'src/remnawave_manager/data/disguises/01-northline'
            self.assertTrue(_install_node_site(source, target))
            self.assertTrue((target / 'shared/image-proxy.js').is_file())
            self.assertRegex((target / 'index.html').read_text(), r'app\.js\?v=[a-f0-9]{16}')
            for module in target.rglob('*.js'):
                for dependency in re.findall(r"from ['\"]([^'\"]+)['\"]", module.read_text()):
                    relative = urlsplit(dependency).path.replace('../shared/', 'shared/')
                    self.assertTrue((module.parent / relative).is_file(), dependency)

    def test_search_preserves_pagination_and_rejects_extra_parameters(self):
        query = 'query=%D0%BA%D0%BE%D1%82&client=wdp&page=2'
        self.assertEqual(search_upstream('/_aster/rutube-search', query), 'https://rutube.ru/api/search/video/?' + query)
        for bad in ('query=x&page=21', 'query=x&url=https://evil.test', 'query=x&page=2&page=3', 'query=%ZZ'):
            self.assertIsNone(search_upstream('/_aster/rutube-search', bad))


@unittest.skipUnless(shutil.which('nginx'), 'nginx is not installed')
class NginxProxyAuditTests(unittest.TestCase):
    def test_all_routes_with_hostile_upstream_and_production_static_locations(self):
        calls = []

        class Upstream(BaseHTTPRequestHandler):
            def respond(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                calls.append((self.command, self.path, dict(self.headers), body))
                redirect = '/redirect.png' in self.path or bool(re.search(r'(?:query|text)=redirect(?:&|$)', self.path))
                self.send_response(302 if redirect else 206 if self.headers.get('Range') else 200)
                self.send_header('Content-Type', 'text/javascript')
                self.send_header('X-Accel-Redirect', '/private-secret')
                self.send_header('X-Accel-Expires', '3600')
                self.send_header('Cache-Control', 'public, max-age=3600')
                self.send_header('Set-Cookie', 'private=session')
                self.send_header('Location', 'https://evil.test/')
                self.send_header('Refresh', '0; url=https://evil.test/')
                if self.headers.get('Range'):
                    self.send_header('Content-Range', 'bytes 0-9/100')
                self.end_headers()
                if self.command != 'HEAD':
                    self.wfile.write(b'upstream-body')
            do_GET = do_POST = do_HEAD = respond
            def log_message(self, *args): pass

        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        process = None
        opener = build_opener(ProxyHandler({}))
        try:
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                config = all_proxies()
                config = re.sub(r'proxy_pass https://[^/;]+', f'proxy_pass http://127.0.0.1:{upstream.server_port}', config)
                config = '\n'.join(line for line in config.splitlines() if 'proxy_ssl_' not in line)
                config += '\n    location /private-secret { internal; return 200 "SECRET"; }\n'
                config += '    location ~* \\.(?:css|js|png|jpg|jpeg|webp|avif|ico|woff2)$ { expires 7d; return 404; }\n'
                file = root / 'nginx.conf'
                file.write_text(f'pid {root}/nginx.pid;\nerror_log stderr;\nevents {{}}\nhttp {{ access_log off; server {{ listen 127.0.0.1:{port};\n{config}\n}} }}')
                process = subprocess.Popen(['nginx', '-p', str(root), '-c', str(file), '-g', 'daemon off;'], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                base = f'http://127.0.0.1:{port}'
                for _ in range(60):
                    try:
                        opener.open(base + '/_images/unknown/a.png', timeout=.2)
                    except HTTPError as error:
                        error.close(); break
                    except URLError:
                        if process.poll() is not None: self.fail(process.stderr.read().decode())
                        time.sleep(.05)
                cases = [
                    ('/_northline/mastodon/api/v1/accounts/123/statuses?limit=20&exclude_replies=true&max_id=456', 'mastodon.ml', '/api/v1/accounts/123/statuses?limit=20&exclude_replies=true&max_id=456', 'application/json'),
                    ('/_aster/rutube-video/' + 'a' * 32, 'rutube.ru', '/api/video/' + 'a' * 32 + '/', 'application/json'),
                    ('/_aster/rutube-profile/123', 'rutube.ru', '/api/profile/user/123/', 'application/json'),
                    ('/_aster/rutube-search?query=cat&client=wdp&page=2', 'rutube.ru', '/api/search/video/?query=cat&client=wdp&page=2', 'application/json'),
                    ('/_morrow/yappy/feed?page=2', 'yappy.media', '/api/feed?page=2&fingerprint=', 'application/json'),
                    ('/_morrow/yappy/search?query=%D0%BA%D0%BE%D1%82&page=1', 'yappy.media', '/api/search/video?query=%D0%BA%D0%BE%D1%82&page=1', 'application/json'),
                    ('/_answers/mail/question/123', 'otvet.mail.ru', '/api/topic/question/123', 'application/json'),
                    ('/_svod/wikipedia/article?title=A%26action%3Dedit', 'ru.wikipedia.org', '/w/api.php?format=json&action=parse&page=A%26action%3Dedit&prop=text%7Ccategories%7Crevid&redirects=1&disableeditsection=1', 'application/json'),
                    ('/_loop/gifs/item/123', 'gifs.ru', '/api/v1/File/FileById/123', 'application/json'),
                    ('/_fokus/ria/article/20260919/news-123.html', 'ria.ru', '/20260919/news-123.html', 'text/plain'),
                    ('/_fokus/media/images/test.jpg', 'cdnn21.img.ria.ru', '/images/test.jpg', 'image/jpeg'),
                    ('/_images/upload.wikimedia.org/wikipedia/%D0%A4%D0%BE%D1%82%D0%BE.svg.png?width=400', 'upload.wikimedia.org', '/wikipedia/%D0%A4%D0%BE%D1%82%D0%BE.svg.png?width=400', 'image/png'),
                    ('/_images/filin.mail.ru/pic?d=abc~', 'filin.mail.ru', '/pic?d=abc~', 'application/octet-stream'),
                ]
                for route, host, target, mime in cases:
                    with self.subTest(route=route):
                        request = Request(base + route, headers={'Cookie': 'secret', 'Authorization': 'Bearer secret'})
                        with opener.open(request, timeout=3) as response:
                            self.assertEqual(response.read(), b'upstream-body')
                            self.assertEqual(response.headers.get('Cache-Control'), 'no-store')
                            self.assertTrue(response.headers['Content-Type'].startswith(mime))
                            self.assertIn('sandbox', response.headers['Content-Security-Policy'])
                            for header in ('Set-Cookie', 'Refresh', 'Location'):
                                self.assertIsNone(response.headers.get(header))
                        self.assertEqual(calls[-1][1], target)
                        self.assertEqual(calls[-1][2]['Host'], host)
                        self.assertNotIn('Cookie', calls[-1][2]); self.assertNotIn('Authorization', calls[-1][2])
                body = b'{"query":"cat","skip":0,"take":24}'
                with opener.open(Request(base + '/_loop/gifs/search-gif', data=body, headers={'Content-Type':'application/json'}), timeout=3) as response:
                    self.assertEqual(response.status, 200)
                self.assertEqual(calls[-1][3], body)
                media = '/_loop/media/' + 'a' * 32 + '.mp4'
                with opener.open(Request(base + media, headers={'Range':'bytes=0-9'}), timeout=3) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.headers['Content-Range'], 'bytes 0-9/100')
                for route, code in [('/_images/mastodon.ml/api/v2/search', 404), ('/_images/pic.rtbcdn.ru/redirect.png', 502), ('/_answers/mail/search?text=redirect', 502), ('/_fokus/ria/search?query=redirect&offset=0', 502), ('/_aster/rutube-search?query=x&page=21', 400)]:
                    with self.subTest(route=route), self.assertRaises(HTTPError) as caught:
                        opener.open(base + route, timeout=3)
                    self.assertEqual(caught.exception.code, code); caught.exception.close()
                process.terminate(); process.wait(timeout=5)
        finally:
            if process:
                if process.poll() is None: process.terminate(); process.wait(timeout=5)
                process.stderr.close()
            upstream.shutdown(); upstream.server_close(); thread.join(timeout=2)
