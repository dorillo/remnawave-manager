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
    NORTHLINE_PROXY,
    LEGACY_NORTHLINE_PROXY,
    ASTER_NODE_CSP,
    NODE_CSP,
    upgrade_northline_policy,
)


class NorthlineIntegrationTests(unittest.TestCase):
    def test_proxy_only_allows_fixed_read_operations(self) -> None:
        from remnawave_manager.northline_proxy import upstream_url

        root = "/_northline/mastodon"
        valid = [
            ("/api/v1/accounts/123", ""),
            ("/api/v1/accounts/123/statuses", "limit=20&exclude_replies=true&exclude_reblogs=true&max_id=456"),
            ("/api/v1/accounts/123/statuses", "limit=20&exclude_replies=true&max_id=456&only_media=true&pinned=true"),
            ("/api/v1/statuses/123/context", ""),
            ("/api/v1/directory", "local=true&order=active&limit=20"),
            ("/api/v1/trends/tags", "limit=8"),
            ("/api/v2/search", "q=%D0%BA%D0%BE%D1%82&resolve=false&limit=20"),
        ]
        for path, query in valid:
            with self.subTest(path=path):
                self.assertEqual(upstream_url(root + path, query), "https://mastodon.ml" + path + ("?" + query if query else ""))
        self.assertEqual(upstream_url("/_northline/legacy/api/v1/statuses/123", ""), "https://mastodon.social/api/v1/statuses/123")
        for path, query in [
            ("/api/v1/accounts/123", "access_token=secret"),
            ("/api/v1/accounts/123/statuses", "limit=200&exclude_replies=true"),
            ("/api/v1/accounts/123/statuses", "limit=20&exclude_replies=true&max_id=-1"),
            ("/api/v1/accounts/123/statuses", "limit=20&exclude_replies=true&limit=20"),
            ("/api/v1/accounts/../../admin", ""),
            ("/api/v1/accounts/123%2Fstatuses", ""),
            ("/api/v1/statuses/123/favourite", ""),
            ("/api/v1/timelines/public", ""),
            ("/api/v2/search", "q=test&resolve=true&limit=20"),
            ("/api/v1/statuses/123", "url=https://example.org"),
        ]:
            with self.subTest(path=path, query=query):
                self.assertIsNone(upstream_url(root + path, query))
        self.assertIsNone(upstream_url("/_northline/legacy/api/v1/directory", "local=true&order=active&limit=20"))
        self.assertIsNone(upstream_url("/_northline/evil.test/api/v1/statuses/123", ""))

    def test_preview_rejects_redirects_and_oversized_responses(self) -> None:
        import io
        from urllib.error import HTTPError

        script = Path(__file__).resolve().parents[1] / "scripts/preview_northline.py"
        spec = importlib.util.spec_from_file_location("preview_northline", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        handler = object.__new__(module.NorthlinePreviewHandler)
        handler.path = "/_northline/mastodon/api/v1/statuses/123"
        handler.command = "GET"
        handler._json_error = mock.Mock()
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.wfile = io.BytesIO()
        self.assertIsNone(module.NoRedirect().redirect_request(None, None, 302, "", {}, "https://evil.test"))
        with mock.patch.object(module.OPENER, "open", side_effect=HTTPError("url", 302, "Redirect", {}, None)):
            handler.do_GET()
        self.assertEqual(handler._json_error.call_args.args[0], 502)
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.headers = {}
        response.read.return_value = b"[]"
        with mock.patch.object(module.OPENER, "open", return_value=response) as opening:
            handler.do_GET()
            self.assertEqual(opening.call_args.args[0].full_url, "https://mastodon.ml/api/v1/statuses/123")
            self.assertNotIn("Authorization", opening.call_args.args[0].headers)
        self.assertEqual(handler.wfile.getvalue(), b"[]")
        response.headers = {"Content-Length": str(module.MAX_RESPONSE_BYTES + 1)}
        with mock.patch.object(module.OPENER, "open", return_value=response):
            handler.do_GET()
        self.assertEqual(handler._json_error.call_args.args[0], 502)
        with mock.patch.object(module.OPENER, "open") as opening:
            handler.do_POST()
            opening.assert_not_called()
        self.assertEqual(handler._json_error.call_args.args[0], 405)
        for name in ("date-utils.js", "storage.js"):
            shared = Path(handler.translate_path(f"/shared/{name}"))
            self.assertTrue(shared.is_file())

    def test_preview_serves_entire_module_graph_without_starting_server(self) -> None:
        from urllib.parse import urljoin, urlsplit

        script = Path(__file__).resolve().parents[1] / "scripts/preview_northline.py"
        spec = importlib.util.spec_from_file_location("preview_northline", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        handler = object.__new__(module.NorthlinePreviewHandler)
        handler.directory = str(module.SITE)
        handler.headers = {}
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler.send_error = mock.Mock()
        pending = ["/app.js"]
        visited = set()
        while pending:
            path = pending.pop()
            if path in visited:
                continue
            visited.add(path)
            handler.path = path
            with self.subTest(path=path):
                response = handler.send_head()
                self.assertIsNotNone(response, f"Missing module: {path}")
                with response:
                    source = response.read().decode("utf-8-sig")
                handler.send_error.assert_not_called()
                self.assertEqual(handler.send_response.call_args.args[0], 200)
                dependencies = re.findall(r"from ['\"]([^'\"]+)['\"]", source)
                pending.extend(
                    urlsplit(urljoin("http://preview.test" + path, dependency)).path
                    for dependency in dependencies
                )
        self.assertIn("/shared/storage.js", visited)
        self.assertIn("/shared/date-utils.js", visited)
        # The shared route is an allowlist, not access to the parent directory.
        for path in ("/shared/../catalog.json", "/shared/site-runtime.js"):
            handler.path = path
            self.assertIsNone(handler.send_head())
            self.assertEqual(handler.send_error.call_args.args[0], 404)

    def test_fresh_install_includes_shared_module_and_recognizes_only_bundled_source(self) -> None:
        from remnawave_manager.install import _install_node_site

        source = Path(__file__).resolve().parents[1] / "src/remnawave_manager/data/disguises/01-northline"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertTrue(_install_node_site(source, root / "site"))
            self.assertTrue((root / "site/shared/date-utils.js").is_file())
            self.assertTrue((root / "site/shared/storage.js").is_file())
            custom = root / "01-northline"
            custom.mkdir()
            (custom / "index.html").write_text("custom")
            self.assertFalse(_install_node_site(custom, root / "custom"))

    def test_policy_upgrade_is_idempotent_and_preserves_custom_config(self) -> None:
        old = f'add_header Content-Security-Policy "{ASTER_NODE_CSP}" always;\r\n'
        upgraded = upgrade_northline_policy(old)
        self.assertIn(NODE_CSP, upgraded)
        self.assertIn(NORTHLINE_PROXY, upgraded)
        self.assertIn("proxy_ssl_verify on;", upgraded)
        self.assertIn("proxy_pass_request_headers off;", upgraded)
        self.assertIn("proxy_pass_request_body off;", upgraded)
        self.assertIn("error_page 301 302 303 307 308 =502", upgraded)
        self.assertEqual(upgrade_northline_policy(upgraded), upgraded)
        custom = 'add_header Content-Security-Policy "default-src custom" always;'
        self.assertEqual(upgrade_northline_policy(custom), custom)

    def test_existing_proxy_is_replaced_without_duplicate_locations(self) -> None:
        original = f'add_header Content-Security-Policy "{NODE_CSP}" always;\n{LEGACY_NORTHLINE_PROXY}'
        upgraded = upgrade_northline_policy(original)
        self.assertIn(NORTHLINE_PROXY, upgraded)
        self.assertEqual(upgraded.count("location /_northline/"), 1)
        self.assertEqual(upgrade_northline_policy(upgraded), upgraded)

    def test_packaged_modules_and_atomic_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "copied"
            copy_template("01-northline", target)
            self.assertTrue((target / "northline-data.js").is_file())
            self.assertTrue((target / "favicon.svg").is_file())
            for module in target.glob("*.js"):
                for dependency in re.findall(r"from ['\"]([^'\"]+)['\"]", module.read_text()):
                    self.assertTrue((target / dependency.replace("../shared/", "shared/")).is_file(), dependency)
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
                            apply_template(mock.Mock(), store, "01-northline")
                        self.assertEqual(config.read_bytes(), original)
                        self.assertEqual(index.read_text(), "original")
                    else:
                        apply_template(mock.Mock(), store, "01-northline")
                        self.assertIn(NORTHLINE_PROXY, config.read_text())
                        self.assertTrue((site / "northline-store.js").is_file())


if __name__ == "__main__":
    unittest.main()
