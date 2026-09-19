from __future__ import annotations

import importlib.util
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remnawave_manager.disguise import apply_template, copy_template
from remnawave_manager.errors import TransactionError
from remnawave_manager.models import Component, Inventory, ManagedFile
from remnawave_manager.runner import sha256_file
from remnawave_manager.site_policy import (
    FOKUS_RIA_PROXY,
    ASTER_NODE_CSP,
    NODE_CSP,
    upgrade_fokus_policy,
)


class FokusIntegrationTests(unittest.TestCase):
    def test_preview_only_allows_fixed_read_operations(self) -> None:
        from remnawave_manager.fokus_proxy import upstream
        self.assertEqual(upstream('/_fokus/ria/feed', ''), 'https://ria.ru/export/rss2/archive/index.xml')
        self.assertEqual(upstream('/_fokus/ria/article/20260919/test-123.html', ''), 'https://ria.ru/20260919/test-123.html')
        self.assertEqual(upstream('/_fokus/ria/dynamics/20260919/123', ''), 'https://ria.ru/services/dynamics/20260919/123.html')
        self.assertIsNotNone(upstream('/_fokus/ria/comments', 'article_id=123&limit=20&date=1&date_usec=2&id_exc='+'a'*24))
        for route, query in [
            ('/ria/comments', 'article_id=1&limit=9999'),
            ('/ria/comments', 'article_id=1&limit=20&url=https://evil.test'),
            ('/ria/article/20260919/../../services/article/like/', ''),
            ('/ria/section/unknown', ''), ('/ria/feed', 'extra=1'),
            ('/ria/services/article/add_emoji/', 'article_id=123&emotion=s1'),
            ('/media/images/../x.jpg', ''), ('/media/images/x.svg', ''),
            ('/media/images/%2e%2e/x.jpg', ''), ('/media/images//x.jpg', ''),
            ('/media/https://evil.test/x.jpg', ''),
        ]:
            self.assertIsNone(upstream('/_fokus'+route, query), (route, query))
        self.assertEqual(upstream('/_fokus/media/images/123_0:0:10:10_80.jpg', ''), 'https://cdnn21.img.ria.ru/images/123_0:0:10:10_80.jpg')
        self.assertIn('proxy_ssl_verify on', FOKUS_RIA_PROXY)
        self.assertIn('proxy_pass_request_headers off', FOKUS_RIA_PROXY)
        self.assertIn('proxy_hide_header Location', FOKUS_RIA_PROXY)
        self.assertIn("default-src 'none'; sandbox", FOKUS_RIA_PROXY)
        self.assertNotIn('/add_emoji/', FOKUS_RIA_PROXY)

    def test_policy_upgrade_is_idempotent_and_preserves_custom_config(self) -> None:
        old = f'add_header Content-Security-Policy "{ASTER_NODE_CSP}" always;\r\n'
        upgraded = upgrade_fokus_policy(old)
        self.assertIn(NODE_CSP, upgraded)
        self.assertIn(FOKUS_RIA_PROXY, upgraded)
        self.assertEqual(upgrade_fokus_policy(upgraded), upgraded)
        custom = 'add_header Content-Security-Policy "default-src custom" always;'
        self.assertEqual(upgrade_fokus_policy(custom), custom)

    def test_packaged_modules_and_atomic_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "copied"
            copy_template("07-fokus-news", target)
            self.assertTrue((target / "fokus-data.js").is_file())
            self.assertTrue((target / "favicon.svg").is_file())
            for module in target.glob("*.js"):
                for dependency in re.findall(r"from ['\"]([^'\"]+)['\"]", module.read_text()):
                    self.assertTrue((module.parent / dependency).is_file(), dependency)
            self.assertNotIn("site-runtime.js", (target / "index.html").read_text())

        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                site = root / "site"
                site.mkdir()
                index = site / "index.html"
                index.write_text("original")
                config = root / "nginx.conf"
                original = f'add_header Content-Security-Policy "{ASTER_NODE_CSP}" always;\n'.encode()
                config.write_bytes(original)
                inventory = Inventory(
                    schema_version=1, role="node", install_dir=str(root),
                    compose_file=str(root / "compose.yml"), env_file=None,
                    webserver="nginx", nginx_files=[str(config)], site_dirs=[str(site)],
                    components={"nginx": Component("nginx", "nginx")},
                    managed_files=[ManagedFile(str(index), sha256_file(index), "site"), ManagedFile(str(config), sha256_file(config), "nginx")],
                )
                store = mock.Mock()
                store.load_inventory.return_value = inventory
                with (
                    mock.patch("remnawave_manager.disguise.create_backup"),
                    mock.patch("remnawave_manager.disguise._target", return_value=site),
                    mock.patch("remnawave_manager.disguise._assert_trusted_site_directories"),
                    mock.patch("remnawave_manager.disguise.nginx_is_running", return_value=True),
                    mock.patch("remnawave_manager.disguise.activate_nginx_config", side_effect=[RuntimeError("failed"), None] if failure else None),
                ):
                    if failure:
                        with self.assertRaises(TransactionError):
                            apply_template(mock.Mock(), store, "07-fokus-news")
                        self.assertEqual(config.read_bytes(), original)
                        self.assertEqual(index.read_text(), "original")
                    else:
                        apply_template(mock.Mock(), store, "07-fokus-news")
                        self.assertIn(FOKUS_RIA_PROXY, config.read_text())
                        self.assertTrue((site / "fokus-store.js").is_file())


if __name__ == "__main__":
    unittest.main()
