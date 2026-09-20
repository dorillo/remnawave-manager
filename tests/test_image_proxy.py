from __future__ import annotations

import io
import json
import re
import unittest
from unittest import mock
from pathlib import Path

from remnawave_manager import image_preview, site_policy
from remnawave_manager.image_proxy import HOSTS, render_proxy, upstream_url


class ImageProxyTests(unittest.TestCase):
    def test_fixed_origins_and_escaped_filenames(self):
        for host in HOSTS:
            target = f"/_images/{host}/pic?d=abc~" if host == "filin.mail.ru" else f"/_images/{host}/images/%D0%A4%D0%BE%D1%82%D0%BE.svg.png?width=400&v=2"
            self.assertEqual(upstream_url(target), target.replace('/_images/', 'https://', 1))
        for suffix in [
            'mastodon.ml/api/v2/search?resolve=true', 'filin.mail.ru/admin',
            'pic.rtbcdn.ru/movie.mp4', 'mastodon.ml/script.js',
            'evil.test/a.png', '127.0.0.1/a.png', 'mastodon.ml:8443/a.png',
            'mastodon.ml@127.0.0.1/a.png', 'mastodon.ml.evil.test/a.png',
            'mastodon.ml//evil.test/a.png', 'mastodon.ml/../admin',
            'mastodon.ml/%2e%2e/admin', 'mastodon.ml/a%2fb',
            'mastodon.ml/a%5cb', 'mastodon.ml/a%252fb', 'mastodon.ml/a#fragment',
        ]:
            with self.subTest(suffix=suffix):
                self.assertIsNone(upstream_url('/_images/' + suffix))

    def test_frontend_and_nginx_allow_same_hosts(self):
        script = (Path(image_preview.__file__).parent / 'data/disguises/shared/image-proxy.js').read_text()
        hosts = json.loads(re.search(r'new Set\((\[.*?\])\)', script, re.S).group(1).replace("'", '"').replace(",\n]", "\n]"))
        self.assertEqual(set(hosts), set(HOSTS))
        proxy = render_proxy()
        self.assertNotIn('proxy_pass https://$', proxy)
        for host in HOSTS:
            self.assertIn(f'proxy_pass https://{host}/;', proxy)
        self.assertEqual(proxy.count('proxy_cache off;'), len(HOSTS))
        self.assertEqual(proxy.count('proxy_pass_request_headers off;'), len(HOSTS))
        self.assertEqual(proxy.count('proxy_hide_header Set-Cookie;'), len(HOSTS))
        self.assertIn('error_page 301 302 303 307 308 =502', proxy)
        self.assertIn("default-src 'none'; sandbox", proxy)

    def test_all_template_upgrades_install_images_once(self):
        original = f'add_header Content-Security-Policy "{site_policy.NODE_CSP}" always;'
        text = original
        for name in ['aster', 'morrow', 'answers', 'svod', 'northline', 'loop', 'fokus']:
            upgrade = getattr(site_policy, f'upgrade_{name}_policy')
            fresh = upgrade(original)
            self.assertIn(render_proxy(), fresh)
            self.assertEqual(upgrade(fresh), fresh)
            text = upgrade(text)
            self.assertEqual(text.count('location /_images/'), 1)
            self.assertEqual(upgrade('custom config'), 'custom config')

    def test_existing_image_routes_upgrade_without_duplicate_locations(self):
        original = f'add_header Content-Security-Policy "{site_policy.NODE_CSP}" always;\n{site_policy.LEGACY_IMAGE_PROXY}'
        upgraded = site_policy.upgrade_answers_policy(original)
        self.assertIn('location ^~ /_images/filin.mail.ru/', upgraded)
        self.assertEqual(upgraded.count('location ^~ /_images/otvet.cdn-vk.net/'), 1)
        self.assertEqual(upgraded.count('location @image_upstream_error'), 1)
        self.assertEqual(site_policy.upgrade_answers_policy(upgraded), upgraded)

    def test_preview_strips_credentials_and_does_not_follow_redirects(self):
        handler = mock.Mock()
        handler.path = '/_images/pic.rtbcdn.ru/a.jpg?width=100'
        handler.command = 'GET'
        handler.headers = {'Cookie': 'private', 'Authorization': 'secret'}
        handler.wfile = io.BytesIO()
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers = {'Content-Type': 'image/jpeg', 'Set-Cookie': 'bad', 'Location': 'https://evil.test'}
        response.read.return_value = b'image'
        with mock.patch.object(image_preview.OPENER, 'open', return_value=response) as opening:
            self.assertTrue(image_preview.serve_preview_asset(handler))
        request = opening.call_args.args[0]
        self.assertEqual(request.full_url, 'https://pic.rtbcdn.ru/a.jpg?width=100')
        self.assertFalse(request.has_header('Cookie'))
        self.assertFalse(request.has_header('Authorization'))
        self.assertEqual(handler.wfile.getvalue(), b'image')
        self.assertNotIn('Set-Cookie', [x.args[0] for x in handler.send_header.call_args_list])
        self.assertIsNone(image_preview.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.test'))
