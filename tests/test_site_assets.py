import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remnawave_manager.site_assets import FreshAssetsHandler, revision, versioned


class SiteAssetsTests(unittest.TestCase):
    def test_deep_module_change_versions_entire_graph(self):
        with tempfile.TemporaryDirectory() as folder:
            site = Path(folder) / 'site'
            shared = Path(folder) / 'shared'
            site.mkdir(); shared.mkdir()
            (site / 'app.js').write_text("import { photo } from './ui.js';")
            (site / 'ui.js').write_text("export const photo = 'direct';")
            old = revision(site, shared)
            (site / 'ui.js').write_text("export const photo = 'proxied';")
            new = revision(site, shared)
            self.assertNotEqual(new, old)
            self.assertIn(f'ui.js?v={new}', versioned((site / 'app.js').read_bytes(), '.js', new).decode())
            html = b'<script type="module" src="app.js?v=old"></script>'
            self.assertIn(f'app.js?v={new}', versioned(html, '.html', new).decode())
            self.assertEqual(versioned(versioned(html, '.html', new), '.html', new), versioned(html, '.html', new))

    def test_preview_never_reuses_conditional_static_responses(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'index.html'
            path.write_text('<script type="module" src="app.js"></script>')
            handler = object.__new__(FreshAssetsHandler)
            handler.directory = folder
            handler.path = '/'
            handler.headers = {'If-Modified-Since': 'Wed, 01 Jan 2100 00:00:00 GMT'}
            handler.send_response = mock.Mock()
            handler.send_header = mock.Mock()
            handler.end_headers = mock.Mock()
            with handler.send_head() as response:
                self.assertIn(b'app.js?v=', response.read())
            handler.send_response.assert_called_once_with(200)
            handler.send_header.assert_any_call('Cache-Control', 'no-store')

    def test_preview_rejects_hidden_files_and_external_symlinks_including_indexes(self):
        with tempfile.TemporaryDirectory() as folder:
            site = Path(folder) / 'site'
            site.mkdir()
            secret = Path(folder) / 'secret.html'
            secret.write_text('private')
            (site / 'linked.html').symlink_to(secret)
            (site / '.private').write_text('private')
            for name in ('index.html', 'index.htm'):
                directory = site / name.replace('.', '-')
                directory.mkdir()
                (directory / name).symlink_to(secret)
            handler = object.__new__(FreshAssetsHandler)
            handler.directory = str(site)
            handler.send_error = mock.Mock()
            for route in ('/linked.html', '/.private', '/index-html/', '/index-htm/'):
                with self.subTest(route=route):
                    handler.path = route
                    self.assertIsNone(handler.send_head())
                    handler.send_error.assert_called_with(404)
