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
    SVOD_WIKIPEDIA_PROXY,
    ASTER_NODE_CSP,
    NODE_CSP,
    upgrade_svod_policy,
)


class SvodIntegrationTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch("remnawave_manager.disguise._site_roots", return_value={"/var/www/html"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_preview_only_allows_fixed_read_operations(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts/preview_svod.py"
        spec = importlib.util.spec_from_file_location("preview_svod", script)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        url = module.SvodPreviewHandler._upstream_url
        self.assertIn("action=parse", url("/_svod/wikipedia/article", "title=Earth"))
        self.assertIn("srsearch=Earth", url("/_svod/wikipedia/search", "q=Earth&offset=0"))
        self.assertIn("list=random", url("/_svod/wikipedia/random", ""))
        self.assertNotIn("cmcontinue", url("/_svod/wikipedia/category", "title=Category%3AScience&continue="))
        self.assertIn("cmcontinue=page%7C1", url("/_svod/wikipedia/category", "title=Category%3AScience&continue=page%7C1"))
        for route, query in [("article", "title=x&action=edit"), ("article", "title=x&title=y"), ("article", "title=%00"), ("random", "url=https://example.org"), ("search", "q=x&offset=-1"), ("edit", "title=x")]:
            self.assertIsNone(url("/_svod/wikipedia/" + route, query))

    def test_policy_upgrade_is_idempotent_and_preserves_custom_config(self) -> None:
        old = f'add_header Content-Security-Policy "{ASTER_NODE_CSP}" always;\r\n'
        upgraded = upgrade_svod_policy(old)
        self.assertIn(NODE_CSP, upgraded)
        self.assertIn(SVOD_WIKIPEDIA_PROXY, upgraded)
        self.assertEqual(upgrade_svod_policy(upgraded), upgraded)
        custom = 'add_header Content-Security-Policy "default-src custom" always;'
        self.assertEqual(upgrade_svod_policy(custom), custom)

    def test_packaged_modules_and_atomic_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "copied"
            copy_template("05-field-notes", target)
            self.assertTrue((target / "svod-data.js").is_file())
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
                            apply_template(mock.Mock(), store, "05-field-notes")
                        self.assertEqual(config.read_bytes(), original)
                        self.assertEqual(index.read_text(), "original")
                    else:
                        apply_template(mock.Mock(), store, "05-field-notes")
                        self.assertIn(SVOD_WIKIPEDIA_PROXY, config.read_text())
                        self.assertTrue((site / "svod-store.js").is_file())


if __name__ == "__main__":
    unittest.main()
