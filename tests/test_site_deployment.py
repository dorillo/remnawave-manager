"""Node site release contract: installation, migrations and transport preservation."""
from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import ProxyHandler, build_opener

from remnawave_manager.disguise import _site_policy_changes, _site_roots, apply_template, copy_template, template_catalog
from remnawave_manager.errors import ValidationError
from remnawave_manager.install import _install_node_site
from remnawave_manager.models import Component, Inventory, ManagedFile
from remnawave_manager.runner import Result, sha256_file
from remnawave_manager.site_config import upgrade_site_config
from remnawave_manager.site_policy import NODE_CSP, TEMPLATE_POLICIES

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / 'src/remnawave_manager/data/disguises'
TRANSPORT = '''    # Yandex and Beeline XHTTP: keep headers, paths, sockets and buffering.
    location /cdn-assets/private-path/ {
        proxy_set_header X-Yandex-CDN yes;
        proxy_set_header X-Real-IP $proxy_protocol_addr;
        proxy_pass http://unix:/dev/shm/xray-xhttp.sock;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 1h;
    }
    location /beeline-private/ {
        proxy_pass http://beeline_xhttp;
        proxy_buffering off;
    }
'''
SUBSCRIPTION = '''server {
    listen 127.0.0.1:8444;
    server_name sub.example.test;
    location / { proxy_pass http://127.0.0.1:3010; }
}
'''


def legacy_config():
    return ('upstream beeline_xhttp { server unix:/dev/shm/beeline.sock; }\n'
            'server {\n    listen unix:/dev/shm/nginx.sock ssl proxy_protocol;\n'
            '    root /var/www/decoy;\n    index index.html;\n'
            '    location / { try_files $uri $uri/ /index.html; }\n'
            + TRANSPORT + '}\n' + SUBSCRIPTION)


class SiteDeploymentTests(unittest.TestCase):
    def test_every_fresh_install_matches_replacement_and_complete_module_graph(self):
        self.assertEqual(set(TEMPLATE_POLICIES), {item['id'] for item in template_catalog()})
        for template_id in TEMPLATE_POLICIES:
            with self.subTest(template=template_id), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                fresh, replacement = root / 'fresh', root / 'replacement'
                fresh.mkdir()
                self.assertEqual(_install_node_site(ASSETS / template_id, fresh), template_id)
                copy_template(template_id, replacement)
                for asset in fresh.rglob('*'):
                    if asset.is_file() and asset.name != '.rwm-template.json':
                        self.assertEqual(asset.read_bytes(), (replacement / asset.relative_to(fresh)).read_bytes())
                marker = json.loads((fresh / '.rwm-template.json').read_text())
                self.assertEqual(marker['template'], template_id)
                pending, seen = ['/index.html'], set()
                while pending:
                    route = pending.pop()
                    if route in seen:
                        continue
                    seen.add(route)
                    file = fresh / route.lstrip('/')
                    self.assertTrue(file.is_file(), route)
                    source = file.read_text()
                    pattern = (r'''(?:src|href)=["']([^"']+\.(?:js|css)(?:\?[^"']*)?)["']'''
                               if route.endswith('.html') else r'''(?:from\s*|import\s*\(?\s*)["'](\.[^"']+\.js(?:\?[^"']*)?)["']''')
                    for dependency in re.findall(pattern, source):
                        self.assertRegex(dependency, r'\?v=[a-f0-9]{16}$')
                        pending.append(urlsplit(urljoin('http://node.test' + route, dependency)).path)
                self.assertIn('/shared/feedback.css', seen)
                self.assertFalse((fresh / 'shared/site-runtime.js').exists())

    def test_all_switches_preserve_transports_and_subscription_and_are_idempotent(self):
        for previous in TEMPLATE_POLICIES:
            original, _ = upgrade_site_config(legacy_config(), previous, {'/var/www/decoy'})
            for current in TEMPLATE_POLICIES:
                with self.subTest(previous=previous, current=current):
                    result, count = upgrade_site_config(original, current, {'/var/www/decoy'})
                    self.assertEqual(count, 1)
                    self.assertIn(TRANSPORT, result)
                    self.assertTrue(result.endswith(SUBSCRIPTION))
                    self.assertEqual(upgrade_site_config(result, current, {'/var/www/decoy'}), (result, 1))
                    locations = re.findall(r'^    location (.+?) \{', result, re.M)
                    # The Subscription vhost has its own location /.
                    self.assertEqual(len(locations), len(set(locations)) + 1)

    def test_scoping_and_unsupported_custom_configs(self):
        original = legacy_config()
        foreign = original.replace('/var/www/decoy', '/var/www/foreign')
        result, count = upgrade_site_config(original + foreign, '02-aster-observatory', {'/var/www/decoy'})
        self.assertEqual(count, 1)
        self.assertTrue(result.endswith(foreign))
        for directive in (
            'add_header Content-Security-Policy "default-src custom" always;',
            'include snippets/site.conf;',
            'location /_aster/ { proxy_pass http://custom; }',
            'location ~ "^/_aster/rutube-video/[a-f0-9]+$" { proxy_pass http://custom; }',
        ):
            with self.subTest(directive=directive), self.assertRaises(ValidationError):
                upgrade_site_config(original.replace('index index.html;', 'index index.html;\n' + directive),
                                    '02-aster-observatory', {'/var/www/decoy'})
        quoted = original.replace('root /var/www/decoy;', 'root "/var/www/decoy";')
        self.assertEqual(upgrade_site_config(quoted, '01-northline', {'/var/www/decoy'})[1], 1)

    def test_crlf_config_preserves_transport_bytes_and_upgrades_known_proxies(self):
        original, _ = upgrade_site_config(legacy_config(), '02-aster-observatory', {'/var/www/decoy'})
        original = original.replace('\n', '\r\n')
        result, count = upgrade_site_config(original, '03-morrow-coffee', {'/var/www/decoy'})
        self.assertEqual(count, 1)
        self.assertIn(TRANSPORT.replace('\n', '\r\n'), result)
        self.assertTrue(result.endswith(SUBSCRIPTION.replace('\n', '\r\n')))
        self.assertNotIn('\n', result.replace('\r\n', ''))
        self.assertEqual(upgrade_site_config(result, '03-morrow-coffee', {'/var/www/decoy'}), (result, 1))

    def test_direct_and_cdn_vhosts_each_receive_their_own_routes(self):
        original = legacy_config() + legacy_config().replace('/dev/shm/nginx.sock', '/dev/shm/cdn.sock')
        result, count = upgrade_site_config(original, '02-aster-observatory', {'/var/www/decoy'})
        self.assertEqual(count, 2)
        self.assertEqual(result.count('location = /_aster/rutube-search {'), 2)
        self.assertEqual(result.count(TRANSPORT), 2)
        self.assertEqual(result.count(SUBSCRIPTION), 2)
        self.assertEqual(upgrade_site_config(result, '02-aster-observatory', {'/var/www/decoy'}), (result, 2))

    def test_unmatched_config_and_wrong_mount_fail_without_replacing_site(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            site = root / 'site'
            site.mkdir()
            index = site / 'index.html'
            index.write_text('original')
            config = root / 'site-vhost'  # nginx includes need not end with .conf.
            config.write_text(legacy_config())
            inventory = Inventory(
                schema_version=1, role='node', install_dir=str(root), compose_file=str(root / 'compose.yml'),
                env_file=None, webserver='nginx', site_dirs=[str(site)], nginx_files=[str(config)],
                components={'nginx': Component('nginx', 'nginx')},
                managed_files=[ManagedFile(str(p), sha256_file(p), 'config') for p in (index, config)],
            )
            self.assertEqual(len(_site_policy_changes(inventory, '01-northline', {'/var/www/decoy'})), 1)
            with self.assertRaisesRegex(ValidationError, 'Не найден'):
                _site_policy_changes(inventory, '01-northline', {'/var/www/other'})
            runner = mock.Mock()
            runner.run.return_value = Result(('docker',), 0, json.dumps({'services': {'nginx': {'volumes': [
                {'type': 'bind', 'source': str(root / 'other'), 'target': '/var/www/decoy'}]}}}), '')
            with self.assertRaisesRegex(ValidationError, 'Bind mount'):
                _site_roots(runner, inventory, site)
            store = mock.Mock()
            store.load_inventory.return_value = inventory
            with mock.patch('remnawave_manager.disguise._site_roots', return_value={'/var/www/other'}), \
                    mock.patch('remnawave_manager.disguise.create_backup') as backup, \
                    self.assertRaisesRegex(ValidationError, 'Не найден'):
                apply_template(runner, store, '01-northline')
            backup.assert_not_called()
            store.save_inventory.assert_not_called()
            self.assertEqual(index.read_text(), 'original')

    def test_compose_drift_blocks_replacement_before_compose_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            site = root / 'site'
            site.mkdir()
            index = site / 'index.html'
            index.write_text('original')
            compose = root / 'compose.yml'
            compose.write_text('before')
            inventory = Inventory(
                schema_version=1, role='node', install_dir=str(root), compose_file=str(compose),
                env_file=None, webserver='nginx', site_dirs=[str(site)],
                components={'nginx': Component('nginx', 'nginx')},
                managed_files=[ManagedFile(str(p), sha256_file(p), 'config') for p in (index, compose)],
            )
            compose.write_text('changed nginx mount')
            store, runner = mock.Mock(), mock.Mock()
            store.load_inventory.return_value = inventory
            with mock.patch('remnawave_manager.disguise.create_backup') as backup, \
                    self.assertRaisesRegex(ValidationError, 'после adoption'):
                apply_template(runner, store, '01-northline')
            backup.assert_not_called()
            runner.run.assert_not_called()
            self.assertEqual(index.read_text(), 'original')

    def test_replacement_from_retired_site_preserves_all_other_files_and_inventory(self):
        for running in (True, False):
            with self.subTest(running=running), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                site = root / 'site'
                site.mkdir()
                (site / 'index.html').write_text('old site 10')
                (site / 'community.svg').write_text('<svg/>')
                (site / '.rwm-template.json').write_text('{"template":"10-dev-circle"}')
                config = root / 'nginx.conf'
                config.write_text(legacy_config())
                protected = {'compose.yml': 'services: {}', '.env': 'SECRET_KEY=unchanged',
                             'warp.conf': 'PrivateKey=unchanged', 'xhttp.conf': TRANSPORT,
                             'subscription.env': 'TOKEN=unchanged', 'cert.pem': 'certificate'}
                for name, value in protected.items():
                    (root / name).write_text(value)
                inventory = Inventory(
                    schema_version=1, role='node', install_dir=str(root),
                    compose_file=str(root / 'compose.yml'), env_file=str(root / '.env'),
                    webserver='nginx', nginx_files=[str(config)], site_dirs=[str(site)],
                    components={'nginx': Component('nginx', 'nginx'), 'node': Component('node', 'node')},
                    xhttp_sockets=['/dev/shm/nginx.sock'], warp_interfaces=['warp'],
                    features={'yandex_cdn': True, 'beeline_cdn_post': True},
                    managed_files=[ManagedFile(str(path), sha256_file(path), 'site' if site in path.parents else 'config')
                                   for path in root.rglob('*') if path.is_file()],
                )
                store, runner = mock.Mock(), mock.Mock()
                store.load_inventory.return_value = inventory
                normalized = {'services': {'nginx': {'volumes': [
                    {'type': 'bind', 'source': str(site), 'target': '/var/www/decoy'}]}}}
                def run(command, **kwargs):
                    if '--format' in command:
                        return Result(tuple(command), 0, json.dumps(normalized), '')
                    return Result(tuple(command), 0, 'nginx\n' if 'ps' in command and running else '', '')
                runner.run.side_effect = run
                with mock.patch('remnawave_manager.disguise.create_backup') as backup:
                    apply_template(runner, store, '07-fokus-news')
                backup.assert_called_once()
                self.assertFalse((site / 'community.svg').exists())
                self.assertTrue((site / 'fokus-data.js').exists())
                self.assertIn(TRANSPORT, config.read_text())
                self.assertTrue(config.read_text().endswith(SUBSCRIPTION))
                for name, value in protected.items():
                    self.assertEqual((root / name).read_text(), value)
                self.assertEqual(inventory.warp_interfaces, ['warp'])
                self.assertEqual(inventory.xhttp_sockets, ['/dev/shm/nginx.sock'])
                self.assertEqual(inventory.features, {'yandex_cdn': True, 'beeline_cdn_post': True})
                self.assertTrue(all(sha256_file(Path(item.path)) == item.sha256 for item in inventory.managed_files))
                commands = [call.args[0] for call in runner.run.call_args_list]
                recreate = [command for command in commands if '--force-recreate' in command]
                self.assertEqual(len(recreate), 1)
                self.assertIn('up' if running else 'create', recreate[0])
                self.assertEqual(recreate[0][-1], 'nginx')
                self.assertIn('--no-deps', recreate[0])

    def test_retired_templates_are_not_selectable(self):
        for template_id in ('08-vector-docs', '09-pulse-monitor', '10-dev-circle'):
            with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValidationError):
                copy_template(template_id, Path(directory) / 'site')


@unittest.skipUnless(shutil.which('nginx'), 'nginx is not installed')
class DeployedNginxTests(unittest.TestCase):
    def test_production_static_files_and_api_locations_for_every_site(self):
        opener = build_opener(ProxyHandler({}))
        for template_id in TEMPLATE_POLICIES:
            with self.subTest(template=template_id), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                site = root / 'site'
                _install_node_site(ASSETS / template_id, site)
                with socket.socket() as sock:
                    sock.bind(('127.0.0.1', 0))
                    port = sock.getsockname()[1]
                text = ('server {\n listen 127.0.0.1:' + str(port) + ';\n root ' + str(site) + ';\n'
                        ' index index.html;\n add_header X-Content-Type-Options nosniff always;\n'
                        ' location / { try_files $uri $uri/ /index.html; }\n'
                        r' location ~ /\. { return 404; }' + '\n'
                        r' location ~* \.(?:css|js|png|jpg|jpeg|webp|avif|ico|woff2)$ { expires 7d; try_files $uri =404; }'
                        + '\n}')
                text, _ = upgrade_site_config(text, template_id, {str(site)})
                # The test exercises routing and static content without contacting external services.
                text = re.sub(r'proxy_pass https://[^/;]+', 'proxy_pass http://127.0.0.1:9', text)
                text = re.sub(r'^\s*proxy_ssl_[^;]+;', '', text, flags=re.M)
                config = root / 'nginx.conf'
                mime = Path(shutil.which('nginx')).resolve().parents[1] / 'etc/nginx/mime.types'
                # Homebrew keeps the configuration outside the Cellar.
                if not mime.exists():
                    mime = Path('/opt/homebrew/etc/nginx/mime.types')
                types = f'include {mime};' if mime.exists() else 'types { text/html html; application/javascript js; text/css css; }'
                config.write_text(f'pid {root}/nginx.pid; error_log stderr; events {{}} http {{ access_log off; {types}\n{text}\n}}')
                process = subprocess.Popen(['nginx', '-p', str(root), '-c', str(config), '-g', 'daemon off;'],
                                           stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                try:
                    for _ in range(60):
                        try:
                            response = opener.open(f'http://127.0.0.1:{port}/', timeout=.3)
                            break
                        except URLError:
                            if process.poll() is not None:
                                self.fail(process.stderr.read().decode())
                            time.sleep(.05)
                    else:
                        self.fail('nginx did not start')
                    with response:
                        html = response.read().decode()
                        self.assertEqual(response.headers['Cache-Control'], 'no-cache')
                        self.assertEqual(response.headers['Content-Security-Policy'], NODE_CSP)
                        self.assertIsNone(response.headers.get('ETag'))
                    for asset in re.findall(r'(?:src|href)="([^"]+\.(?:js|css)\?v=[a-f0-9]+)"', html):
                        with opener.open(urljoin(f'http://127.0.0.1:{port}/', asset), timeout=2) as resource:
                            self.assertEqual(resource.status, 200)
                            self.assertIn('max-age=604800', resource.headers['Cache-Control'])
                            self.assertNotIn('text/html', resource.headers['Content-Type'])
                    with self.assertRaises(HTTPError) as caught:
                        opener.open(f'http://127.0.0.1:{port}/.rwm-template.json', timeout=2)
                    self.assertEqual(caught.exception.code, 404)
                    caught.exception.close()
                finally:
                    process.terminate()
                    process.wait(timeout=5)
                    process.stderr.close()


if __name__ == '__main__':
    unittest.main()
