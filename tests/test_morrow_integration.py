from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remnawave_manager.disguise import _aster_policy_changes, apply_template, copy_template
from remnawave_manager.errors import TransactionError, ValidationError
from remnawave_manager.models import Component, Inventory, ManagedFile
from remnawave_manager.runner import sha256_file
from remnawave_manager.site_policy import (
    ASTER_NODE_CSP,
    ASTER_SEARCH_PROXY,
    LEGACY_NODE_CSP,
    NODE_CSP,
    upgrade_morrow_policy,
)


class MorrowIntegrationTests(unittest.TestCase):
    def test_upgrade_preserves_routes_and_custom_policies(self) -> None:
        for old_policy in (LEGACY_NODE_CSP, ASTER_NODE_CSP, NODE_CSP):
            original = f'add_header Content-Security-Policy "{old_policy}" always;\r\n'
            updated = upgrade_morrow_policy(original)
            self.assertIn(NODE_CSP, updated)
            self.assertTrue(updated.endswith('\r\n'))
            self.assertEqual(upgrade_morrow_policy(updated), updated)
            self.assertNotIn('proxy_pass', updated)
            self.assertIn(ASTER_SEARCH_PROXY, upgrade_morrow_policy(original + ASTER_SEARCH_PROXY))
        custom = 'add_header Content-Security-Policy "default-src none" always;'
        self.assertEqual(upgrade_morrow_policy(custom), custom)

    def test_all_module_imports_survive_template_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'site'
            copy_template('03-morrow-coffee', target)
            self.assertGreaterEqual(len(list(target.glob('*.js'))), 8)
            for module in target.glob('*.js'):
                source = module.read_text(encoding='utf-8')
                for dependency in re.findall(r"from ['\"]([^'\"]+)['\"]", source):
                    self.assertTrue((module.parent / dependency).is_file(), dependency)
            html = (target / 'index.html').read_text(encoding='utf-8')
            self.assertNotIn('site-runtime.js', html)
            self.assertIn('https://prexzyapis.com', html)

    def test_apply_upgrades_or_rolls_back_csp_and_template(self) -> None:
        for failure in (False, True):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                site = root / 'site'
                site.mkdir()
                index = site / 'index.html'
                index.write_text('original', encoding='utf-8')
                config = root / 'nginx.conf'
                old = f'add_header Content-Security-Policy "{ASTER_NODE_CSP}" always;\r\n'.encode()
                config.write_bytes(old)
                inventory = Inventory(
                    schema_version=1, role='node', install_dir=str(root),
                    compose_file=str(root / 'compose.yml'), env_file=None,
                    webserver='nginx', nginx_files=[str(config)], site_dirs=[str(site)],
                    components={'nginx': Component('nginx', 'nginx')},
                    managed_files=[ManagedFile(str(index), sha256_file(index), 'site'), ManagedFile(str(config), sha256_file(config), 'nginx')],
                )
                store = mock.Mock()
                store.load_inventory.return_value = inventory
                with (
                    mock.patch('remnawave_manager.disguise.create_backup'),
                    mock.patch('remnawave_manager.disguise._target', return_value=site),
                    mock.patch('remnawave_manager.disguise._assert_trusted_site_directories'),
                    mock.patch('remnawave_manager.disguise.nginx_is_running', return_value=True),
                    mock.patch('remnawave_manager.disguise.activate_nginx_config', side_effect=[RuntimeError('failed'), None] if failure else None),
                ):
                    if failure:
                        with self.assertRaises(TransactionError):
                            apply_template(mock.Mock(), store, '03-morrow-coffee')
                        self.assertEqual(config.read_bytes(), old)
                        self.assertEqual(index.read_text(), 'original')
                    else:
                        apply_template(mock.Mock(), store, '03-morrow-coffee')
                        self.assertIn(NODE_CSP, config.read_text())
                        self.assertTrue((site / 'morrow-ai.js').is_file())
                        saved = store.save_inventory.call_args.args[0]
                        self.assertEqual(next(f.sha256 for f in saved.managed_files if f.path == str(config)), sha256_file(config))
                        config.write_text('operator edit', encoding='utf-8')
                        with self.assertRaises(ValidationError):
                            _aster_policy_changes(saved, morrow=True)
