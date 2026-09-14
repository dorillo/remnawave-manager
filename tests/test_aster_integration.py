from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remnawave_manager.disguise import _aster_policy_changes, apply_template, copy_template
from remnawave_manager.errors import TransactionError, ValidationError
from remnawave_manager.models import Component, Inventory, ManagedFile
from remnawave_manager.runner import sha256_file
from remnawave_manager.site_policy import (
    ASTER_SEARCH_PROXY,
    LEGACY_NODE_CSP,
    NODE_CSP,
    upgrade_aster_policy,
)


class AsterIntegrationTests(unittest.TestCase):
    def test_policy_upgrade_is_narrow_and_idempotent(self) -> None:
        old = f'add_header Content-Security-Policy "{LEGACY_NODE_CSP}" always;'
        updated = upgrade_aster_policy(old)
        self.assertIn(NODE_CSP, updated)
        self.assertIn(ASTER_SEARCH_PROXY, updated)
        self.assertEqual(upgrade_aster_policy(updated), updated)
        current = f'add_header Content-Security-Policy "{NODE_CSP}" always;'
        self.assertIn(ASTER_SEARCH_PROXY, upgrade_aster_policy(current))
        custom = 'add_header Content-Security-Policy "default-src none" always;'
        self.assertEqual(upgrade_aster_policy(custom), custom)

    def test_packaged_template_contains_catalog_and_modules(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "site"
            copy_template("02-aster-observatory", target)
            catalog = json.loads((target / "data/catalog.json").read_text(encoding="utf-8"))
            self.assertGreater(len(catalog["videos"]), 20)
            self.assertEqual(len({v["id"] for v in catalog["videos"]}), len(catalog["videos"]))
            self.assertTrue(any(video.get("publicComments") for video in catalog["videos"]))
            comments = [
                comment
                for video in catalog["videos"]
                for comment in video.get("publicComments", [])
            ]
            self.assertTrue(all(comment.get("user", {}).get("avatar") for comment in comments))
            self.assertTrue(all(video.get("avatar") for video in catalog["videos"]))
            self.assertTrue(all(video.get("publicLikes") is not None for video in catalog["videos"]))
            self.assertGreater(len(catalog["channels"]), 10)
            self.assertGreater(sum(len(channel["videos"]) for channel in catalog["channels"]), 500)
            for name in ("app", "aster-auth", "aster-comments", "aster-data", "aster-store", "aster-i18n", "aster-player", "aster-ui", "aster-uploads"):
                self.assertTrue((target / f"{name}.js").is_file())
            self.assertTrue((target / "shared/storage.js").is_file())

    def fixture(self, root: Path) -> tuple[Inventory, Path, Path]:
        site = root / "site"
        site.mkdir()
        index = site / "index.html"
        index.write_text("original", encoding="utf-8")
        config = root / "nginx.conf"
        config.write_text(f'add_header Content-Security-Policy "{LEGACY_NODE_CSP}" always;\n', encoding="utf-8")
        inventory = Inventory(
            schema_version=1, role="node", install_dir=str(root), compose_file=str(root / "compose.yml"),
            env_file=None, webserver="nginx", nginx_files=[str(config)], site_dirs=[str(site)],
            components={"nginx": Component("nginx", "nginx")},
            managed_files=[ManagedFile(str(index), sha256_file(index), "site"), ManagedFile(str(config), sha256_file(config), "nginx")],
        )
        return inventory, site, config

    def test_csp_and_site_rollback_together_when_nginx_rejects(self) -> None:
        for fail in (True, False):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as temporary:
                inventory, site, config = self.fixture(Path(temporary))
                old = config.read_bytes()
                store = mock.Mock()
                store.load_inventory.return_value = inventory
                with mock.patch("remnawave_manager.disguise.create_backup"), mock.patch("remnawave_manager.disguise._target", return_value=site), mock.patch("remnawave_manager.disguise._assert_trusted_site_directories"), mock.patch("remnawave_manager.disguise.nginx_is_running", return_value=True), mock.patch("remnawave_manager.disguise.activate_nginx_config", side_effect=[RuntimeError("nginx test failed"), None] if fail else None):
                    if fail:
                        with self.assertRaises(TransactionError):
                            apply_template(mock.Mock(), store, "02-aster-observatory")
                        self.assertEqual(config.read_bytes(), old)
                        self.assertEqual((site / "index.html").read_text(), "original")
                    else:
                        apply_template(mock.Mock(), store, "02-aster-observatory")
                        self.assertIn(NODE_CSP, config.read_text())
                        self.assertIn(ASTER_SEARCH_PROXY, config.read_text())
                        self.assertTrue((site / "aster-player.js").exists())
                        saved = store.save_inventory.call_args.args[0]
                        self.assertEqual(next(item.sha256 for item in saved.managed_files if item.path == str(config)), sha256_file(config))

    def test_operator_edits_are_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            inventory, _site, config = self.fixture(Path(temporary))
            config.write_text("operator edit", encoding="utf-8")
            with self.assertRaises(ValidationError):
                _aster_policy_changes(inventory)
            self.assertEqual(config.read_text(), "operator edit")


if __name__ == "__main__":
    unittest.main()
