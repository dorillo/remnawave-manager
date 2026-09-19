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
    LOOP_GIFS_PROXY,
    ASTER_NODE_CSP,
    NODE_CSP,
    upgrade_loop_policy,
)


class LoopIntegrationTests(unittest.TestCase):
    def test_preview_only_allows_fixed_read_operations(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts/preview_loop.py"
        spec = importlib.util.spec_from_file_location("preview_loop", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        url = module.LoopPreviewHandler.upstream
        self.assertEqual(url("/_loop/gifs/search-gif", "", "POST"), "https://gifs.ru/api/v1/Gif/GetGifs")
        self.assertIn("Popular", url("/_loop/gifs/popular", "contentType=1&skip=0&take=24", "GET"))
        for route, query, method in [
            ("search-gif", "", "GET"), ("popular", "contentType=1&skip=0&take=24&url=evil", "GET"),
            ("Auth/SignIn", "", "POST"), ("item/1", "", "POST"),
            ("popular", "contentType=4&skip=0&take=24", "GET"),
        ]:
            self.assertIsNone(url("/_loop/gifs/" + route, query, method))
        self.assertIsNone(url("/_loop/media/https://evil.com/x.mp4", "", "GET"))
        self.assertIsNone(url("/_loop/media/../secret", "", "GET"))
        self.assertIsNotNone(url("/_loop/media/" + "a" * 40 + "_preview.mp4", "", "GET"))
        self.assertIn("proxy_ssl_verify on", LOOP_GIFS_PROXY)
        self.assertIn("proxy_pass_request_headers off", LOOP_GIFS_PROXY)
        self.assertIn("proxy_hide_header Location", LOOP_GIFS_PROXY)

    def test_policy_upgrade_is_idempotent_and_preserves_custom_config(self) -> None:
        old = f'add_header Content-Security-Policy "{ASTER_NODE_CSP}" always;\r\n'
        upgraded = upgrade_loop_policy(old)
        self.assertIn(NODE_CSP, upgraded)
        self.assertIn(LOOP_GIFS_PROXY, upgraded)
        self.assertEqual(upgrade_loop_policy(upgraded), upgraded)
        custom = 'add_header Content-Security-Policy "default-src custom" always;'
        self.assertEqual(upgrade_loop_policy(custom), custom)

    def test_packaged_modules_and_atomic_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "copied"
            copy_template("06-loop-archive", target)
            self.assertTrue((target / "loop-data.js").is_file())
            self.assertTrue((target / "favicon.svg").is_file())
            for module in target.glob("*.js"):
                for dependency in re.findall(r"from ['\"]([^'\"]+)['\"]", module.read_text()):
                    self.assertTrue((module.parent / dependency.split("?", 1)[0]).is_file(), dependency)
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
                            apply_template(mock.Mock(), store, "06-loop-archive")
                        self.assertEqual(config.read_bytes(), original)
                        self.assertEqual(index.read_text(), "original")
                    else:
                        apply_template(mock.Mock(), store, "06-loop-archive")
                        self.assertIn(LOOP_GIFS_PROXY, config.read_text())
                        self.assertTrue((site / "loop-store.js").is_file())


if __name__ == "__main__":
    unittest.main()
