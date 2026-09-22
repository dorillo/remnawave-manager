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
    ANSWERS_MAIL_PROXY,
    ASTER_NODE_CSP,
    NODE_CSP,
    upgrade_answers_policy,
)


class AnswersIntegrationTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("remnawave_manager.disguise._site_roots", return_value={"/var/www/html"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_preview_routes_are_narrow_and_read_only(self) -> None:
        script = Path(__file__).parents[1] / "scripts/preview_answers.py"
        spec = importlib.util.spec_from_file_location("preview_answers", script)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        url = module.AnswersPreviewHandler._upstream_url
        self.assertEqual(url("/_answers/mail/feed", "limit=20&pos=123&space=programming"), "https://otvet.mail.ru/api/topic/feed?limit=20&pos=123&space=programming")
        self.assertEqual(url("/_answers/mail/question/123", ""), "https://otvet.mail.ru/api/topic/question/123")
        self.assertEqual(url("/_answers/mail/answers/123", "limit=50"), "https://otvet.mail.ru/api/topic/answers/123?limit=50")
        self.assertIn("%D0%BA%D0%BE%D1%82", url("/_answers/mail/search", "text=%D0%BA%D0%BE%D1%82"))
        self.assertEqual(url("/_answers/mail/answers/123", "limit=50&pos=456&reply_id=789"), "https://otvet.mail.ru/api/topic/answers/123?limit=50&pos=456&reply_id=789")
        self.assertEqual(url("/_answers/mail/profile/9/topics", "limit=20&dir=0"), "https://otvet.mail.ru/api/topic/profile/9/topics?limit=20&dir=0")
        self.assertEqual(url("/_answers/mail/profile/9/replies", "limit=20&dir=1&pos=456"), "https://otvet.mail.ru/api/topic/profile/9/replies?limit=20&dir=1&pos=456")
        self.assertEqual(url("/_answers/mail/profile/9/count", ""), "https://otvet.mail.ru/api/topic/profile/9/content/count")
        for path, query in (
            ("/_answers/mail/profile/9/topics", "limit=20&dir=1"),
            ("/_answers/mail/profile/9/replies", "limit=20&dir=1&pos=1&pos=2"),
            ("/_answers/mail/profile/9/count", "url=https://evil.test"),
            ("/_answers/mail/answers/123", "limit=50&pos=-1"),
            ("/_answers/mail/answers/123", "limit=50&reply_id=1&reply_id=2"),
            ("/_answers/mail/feed", "limit=21"),
            ("/_answers/mail/feed", "limit=20&pos=abc"),
            ("/_answers/mail/search", "text=x&url=https://example.org"),
            ("/_answers/mail/question/../../etc", ""),
            ("/_answers/mail/answers/123", "limit=500"),
            ("/_answers/mail/unknown", ""),
        ):
            self.assertIsNone(url(path, query), (path, query))

    def test_policy_upgrade_is_idempotent_and_preserves_custom_config(self) -> None:
        old = f'add_header Content-Security-Policy "{ASTER_NODE_CSP}" always;\r\n'
        upgraded = upgrade_answers_policy(old)
        self.assertIn(NODE_CSP, upgraded)
        self.assertIn(ANSWERS_MAIL_PROXY, upgraded)
        self.assertEqual(upgrade_answers_policy(upgraded), upgraded)
        custom = 'add_header Content-Security-Policy "default-src custom" always;'
        self.assertEqual(upgrade_answers_policy(custom), custom)

    def test_existing_bound_spros_migrates_and_keeps_source(self):
        from remnawave_manager.site_policy import _previous_answers
        from remnawave_manager.proxy_policy import harden_proxy
        from remnawave_manager.site_config import upgrade_site_config
        from remnawave_manager.site_egress_config import configure_egress
        old = 'server {\n    listen 443 ssl;\n    root /var/www/html;\n' + harden_proxy(_previous_answers, 'answers') + '\n}\n'
        with mock.patch('remnawave_manager.site_ipv4.system_resolvers', return_value=('127.0.0.53',)):
            old = configure_egress(old, {'/var/www/html'}, '198.51.100.10')[0]
            new = upgrade_site_config(old, '04-signal-works', {'/var/www/html'})[0]
            self.assertIn('/profile/', new)
            self.assertIn('(&pos=', new)
            self.assertIn('proxy_bind 198.51.100.10;', new)
            self.assertEqual(configure_egress(new, {'/var/www/html'}, '198.51.100.10')[0], new)
            self.assertEqual(upgrade_site_config(new, '04-signal-works', {'/var/www/html'})[0], new)

    def test_packaged_modules_and_atomic_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "copied"
            copy_template("04-signal-works", target)
            self.assertTrue((target / "answers-data.js").is_file())
            self.assertTrue((target / "favicon.svg").is_file())
            for module in target.glob("*.js"):
                for dependency in re.findall(r"from ['\"]([^'\"]+)['\"]", module.read_text()):
                    dependency = dependency.split("?", 1)[0]
                    # At the installed site's URL root, ../shared resolves to /shared.
                    if dependency.startswith("../shared/"):
                        dependency = dependency[3:]
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
                original = f'server {{\n root /var/www/html;\n add_header Content-Security-Policy "{ASTER_NODE_CSP}" always;\n}}\n'.encode()
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
                            apply_template(mock.Mock(), store, "04-signal-works")
                        self.assertEqual(config.read_bytes(), original)
                        self.assertEqual(index.read_text(), "original")
                    else:
                        apply_template(mock.Mock(), store, "04-signal-works")
                        self.assertIn(ANSWERS_MAIL_PROXY, config.read_text())
                        self.assertTrue((site / "answers-store.js").is_file())


if __name__ == "__main__":
    unittest.main()
