from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remnawave_manager.backup import BackupResult
from remnawave_manager.cli import build_parser, dispatch, CliContext, _is_mutating, _interactive_arguments
from remnawave_manager.errors import TransactionError, ValidationError
from remnawave_manager.models import Component, Inventory, ManagedFile
from remnawave_manager.paths import RuntimePaths
from remnawave_manager.runner import Result, sha256_file
from remnawave_manager.site_config import upgrade_site_config
from remnawave_manager.site_egress_config import configure_egress, validate_source
from remnawave_manager.site_egress import (
    Probe, PROBES, _context, _local_target, _probe, egress_status,
    set_egress, source_addresses,
)
from remnawave_manager.site_policy import TEMPLATE_POLICIES
from test_site_deployment import legacy_config, TRANSPORT, SUBSCRIPTION

ROOTS = {"/var/www/decoy"}
OLD, NEW = "198.51.100.10", "198.51.100.20"


def config(template="03-morrow-coffee"):
    return upgrade_site_config(legacy_config(), template, ROOTS)[0]


def ip_output():
    return json.dumps([
        {"ifname": "ens3", "flags": ["UP"], "addr_info": [
            {"family": "inet", "scope": "global", "local": address} for address in (OLD, NEW)]},
        {"ifname": "warp", "flags": ["UP"], "addr_info": [{"family": "inet", "scope": "global", "local": "172.16.0.2"}]},
        {"ifname": "docker0", "flags": ["UP"], "addr_info": [{"family": "inet", "scope": "global", "local": "172.17.0.1"}]},
    ])


class EgressDNSTestCase(unittest.TestCase):
    def setUp(self):
        patch = mock.patch("remnawave_manager.site_ipv4.system_resolvers", return_value=("127.0.0.53",))
        patch.start()
        self.addCleanup(patch.stop)


class EgressConfigTests(EgressDNSTestCase):
    def test_every_template_and_transition_preserves_binding_and_transports(self):
        for template in TEMPLATE_POLICIES:
            original = config(template)
            selected, routes, previous = configure_egress(original, ROOTS, NEW)
            self.assertEqual(previous, [None])
            self.assertGreater(routes, 0)
            self.assertEqual(selected.count(f"proxy_bind {NEW};"), routes)
            self.assertIn(TRANSPORT, selected)
            self.assertTrue(selected.endswith(SUBSCRIPTION))
            self.assertEqual(configure_egress(selected, ROOTS, NEW)[0], selected)
            self.assertEqual(configure_egress(selected, ROOTS, None)[0], original)
            for destination in TEMPLATE_POLICIES:
                with self.subTest(template=template, destination=destination):
                    switched, _ = upgrade_site_config(selected, destination, ROOTS)
                    _, count, values = configure_egress(switched, ROOTS, None)
                    self.assertEqual(values, [NEW])
                    self.assertEqual(switched.count(f"proxy_bind {NEW};"), count)
                    self.assertIn(TRANSPORT, switched)
                    self.assertTrue(switched.endswith(SUBSCRIPTION))
                    self.assertEqual(upgrade_site_config(switched, destination, ROOTS)[0], switched)

    def test_foreign_vhost_crlf_and_multiple_site_vhosts(self):
        original = config().replace("\n", "\r\n")
        foreign = original.replace("/var/www/decoy", "/var/www/other")
        selected, routes, values = configure_egress(original + foreign + original, ROOTS, NEW)
        self.assertIn(foreign, selected)
        self.assertEqual(values, [None, None])
        self.assertNotIn("\n", selected.replace("\r\n", ""))
        self.assertEqual(selected.count(f"proxy_bind {NEW};"), routes)
        self.assertEqual(configure_egress(selected, ROOTS, None)[0], original + foreign + original)

    def test_rejects_unknown_custom_and_partial_bindings(self):
        original = config()
        selected = configure_egress(original, ROOTS, NEW)[0]
        bad = [
            original.replace("proxy_pass https://yappy.media;", "proxy_pass https://custom.example;"),
            original.replace("root /var/www/decoy;", "root /var/www/decoy; include custom.conf;"),
            selected.replace(f"proxy_bind {NEW}; # rwm-site-egress\n", "", 1),
            selected.replace(f"proxy_bind {NEW};", f"proxy_bind {OLD};", 1),
            selected.replace("# rwm-site-egress", "# custom", 1),
            original.replace("root /var/www/decoy;", f"root /var/www/decoy; proxy_bind {OLD};"),
            f"proxy_bind {OLD};\n" + original,
        ]
        for text in bad:
            with self.subTest(text=text[:80]), self.assertRaises(ValidationError):
                configure_egress(text, ROOTS, NEW)

    def test_rejects_invalid_sources(self):
        for address in ("1.2.3.4; include evil;", "127.0.0.1", "0.0.0.0", "224.0.0.1", "::1", "2001:db8::1", "169.254.1.1", "255.255.255.255"):
            with self.subTest(address=address), self.assertRaises(ValidationError):
                validate_source(address)


class EgressProbeTests(unittest.TestCase):
    def test_address_discovery_excludes_tunnels_and_containers(self):
        runner = mock.Mock()
        runner.run.return_value = Result((), 0, ip_output(), "")
        self.assertEqual([x.address for x in source_addresses(runner)], [OLD, NEW])

    def test_probe_requires_data_and_chosen_local_address(self):
        runner = mock.Mock()
        metadata = {"http_code": 200, "time_total": 0.2, "local_ip": NEW, "remote_ip": "203.0.113.1"}
        for body, local, rc, expected in (("{}", NEW, 0, True), ("<html>error</html>", NEW, 0, False), ("{}", OLD, 0, False), ("{}", NEW, 28, False)):
            metadata["local_ip"] = local
            runner.run.return_value = Result((), rc, body + "\nRWM_EGRESS_META:" + json.dumps(metadata), "")
            self.assertEqual(_probe(runner, "https://yappy.media/api/feed", NEW).ok, expected)
        args = runner.run.call_args.args[0]
        self.assertIn("--interface", args)
        self.assertNotIn("--insecure", args)
        self.assertNotIn("-L", args)

    def test_local_probe_maps_unix_socket_and_sends_proxy_protocol(self):
        origin, extra = _local_target([(Path("/x"), config(), 0o600)], ROOTS,
                                     {"volumes": [{"type": "bind", "source": "/host/shm", "target": "/dev/shm"}]})
        self.assertEqual(origin, "https://localhost")
        self.assertIn("--haproxy-protocol", extra)
        self.assertIn("/host/shm/nginx.sock", extra)

    def test_probe_routes_match_provider_contracts(self):
        from urllib.parse import urlsplit
        from remnawave_manager.northline_proxy import upstream_url
        from remnawave_manager.aster_proxy import search_upstream
        from remnawave_manager.fokus_proxy import upstream
        for template, validate in (("01-northline", upstream_url), ("02-aster-observatory", search_upstream), ("07-fokus-news", upstream)):
            url, path = PROBES[template][0]
            parsed = urlsplit(path)
            self.assertEqual(validate(parsed.path, parsed.query), url)


class EgressContextTests(EgressDNSTestCase):
    def test_real_inventory_mounts_and_network_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            site = root / "site"
            site.mkdir()
            configuration = root / "nginx.conf"
            configuration.write_text(config())
            configuration.chmod(0o600)
            compose_file = root / "compose.yml"
            compose_file.write_text("services: {}\n")
            inventory = Inventory(schema_version=1, role="node", install_dir=str(root), compose_file=str(compose_file),
                                  env_file=None, webserver="nginx", nginx_files=[str(configuration)], site_dirs=[str(site)],
                                  components={"nginx": Component("nginx", "nginx")},
                                  managed_files=[ManagedFile(str(configuration), sha256_file(configuration), "nginx")])
            store, runner = mock.Mock(), mock.Mock()
            store.load_inventory.return_value = inventory
            service = {"network_mode": "host", "volumes": [{"type": "bind", "source": str(site), "target": "/var/www/decoy"}]}
            runner.run.return_value = Result((), 0, json.dumps({"services": {"nginx": service}}), "")
            result = _context(runner, store)
            self.assertEqual(result[3], ROOTS)
            self.assertEqual(result[4], [(configuration, config(), 0o600)])
            service["network_mode"] = "bridge"
            runner.run.return_value = Result((), 0, json.dumps({"services": {"nginx": service}}), "")
            with self.assertRaisesRegex(ValidationError, "network_mode: host"):
                _context(runner, store)
            self.assertEqual(configuration.read_text(), config())

    def test_mixed_vhost_selection_is_rejected(self):
        from remnawave_manager.site_egress import _current
        original = config()
        selected = configure_egress(original, ROOTS, NEW)[0]
        with self.assertRaisesRegex(ValidationError, "разные исходящие IP"):
            _current([(Path("nginx.conf"), original + selected, 0o600)], ROOTS)


class EgressTransactionTests(EgressDNSTestCase):
    def setUp(self):
        super().setUp()
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.site = root / "site"
        self.site.mkdir()
        (self.site / ".rwm-template.json").write_text(json.dumps({"template": "03-morrow-coffee"}))
        self.path = root / "nginx.conf"
        self.original = config()
        self.path.write_text(self.original)
        self.path.chmod(0o600)
        self.inventory = Inventory(schema_version=1, role="node", install_dir=str(root), compose_file=str(root / "compose.yml"),
                                   env_file=None, webserver="nginx", components={"nginx": Component("nginx", "nginx")},
                                   managed_files=[ManagedFile(str(self.path), sha256_file(self.path), "nginx")])
        self.store = mock.Mock()
        self.runner = mock.Mock()
        self.runner.run.return_value = Result((), 0, ip_output(), "")
        self.service = {"network_mode": "host", "volumes": [{"type": "bind", "source": "/dev/shm", "target": "/dev/shm"}]}
        self.snapshots = [(self.path, self.original, 0o600)]
        patches = {
            "_context": dict(return_value=(self.inventory, self.site, self.service, ROOTS, self.snapshots)),
            "_managed_nginx_configs": dict(return_value=self.snapshots),
            "_site_roots": dict(return_value=ROOTS),
            "_validate_configuration": {},
            "nginx_is_running": dict(return_value=True),
            "create_backup": dict(return_value=BackupResult(root / "backup", {})),
            "activate_nginx_config": {},
            "_probe": dict(return_value=Probe(NEW, "url", True, 200, NEW, "203.0.113.1", 0.2, "OK")),
        }
        self.mocks = {}
        for name, kwargs in patches.items():
            patch = mock.patch("remnawave_manager.site_egress." + name, **kwargs)
            self.mocks[name] = patch.start()
            self.addCleanup(patch.stop)

    def test_applies_checks_and_records_exact_hash(self):
        value = set_egress(self.runner, self.store, NEW)
        self.assertTrue(value["changed"])
        self.assertIn(f"proxy_bind {NEW};", self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        saved = self.store.save_inventory.call_args.args[0]
        self.assertEqual(saved.managed_files[0].sha256, sha256_file(self.path))
        self.assertEqual(self.mocks["_probe"].call_count, 8)
        self.mocks["create_backup"].assert_called_once()
        self.assertIn(TRANSPORT, self.path.read_text())

    def test_rejects_unassigned_source_before_backup(self):
        with self.assertRaisesRegex(ValidationError, "не назначен"):
            set_egress(self.runner, self.store, "198.51.100.99")
        self.mocks["create_backup"].assert_not_called()
        self.assertEqual(self.path.read_text(), self.original)

    def test_preflight_failure_does_not_write(self):
        self.mocks["_probe"].return_value = Probe(NEW, "url", False, 0, "", "", 5, "timeout")
        with self.assertRaisesRegex(ValidationError, "Источник недоступен"):
            set_egress(self.runner, self.store, NEW)
        self.assertEqual(self.path.read_text(), self.original)
        self.mocks["create_backup"].assert_not_called()

    def test_failed_nginx_activation_rolls_back(self):
        self.mocks["activate_nginx_config"].side_effect = [TransactionError("nginx failed"), None]
        with self.assertRaisesRegex(TransactionError, "прежняя конфигурация восстановлена"):
            set_egress(self.runner, self.store, NEW)
        self.assertEqual(self.path.read_text(), self.original)
        self.store.save_inventory.assert_not_called()
        self.assertEqual(self.mocks["activate_nginx_config"].call_count, 2)

    def test_live_site_failure_rolls_back(self):
        good = self.mocks["_probe"].return_value
        self.mocks["_probe"].side_effect = [good, good, Probe("auto", "url", False, 504, "", "", 5, "HTTP=504"), *([good] * 5)]
        with self.assertRaisesRegex(TransactionError, "Проверка сайта"):
            set_egress(self.runner, self.store, NEW)
        self.assertEqual(self.path.read_text(), self.original)
        self.assertEqual(self.mocks["activate_nginx_config"].call_count, 2)

    def test_failed_inventory_write_restores_inventory_and_config(self):
        old_inventory = self.inventory.to_dict()
        self.store.save_inventory.side_effect = [OSError("disk full"), None]
        with self.assertRaisesRegex(TransactionError, "disk full"):
            set_egress(self.runner, self.store, NEW)
        self.assertEqual(self.path.read_text(), self.original)
        self.assertEqual(self.store.save_inventory.call_args.args[0].to_dict(), old_inventory)

    def test_concurrent_edit_during_backup_is_not_overwritten(self):
        self.mocks["_managed_nginx_configs"].return_value = [(self.path, "operator edit", 0o600)]
        with self.assertRaisesRegex(ValidationError, "изменилась"):
            set_egress(self.runner, self.store, NEW)
        self.mocks["activate_nginx_config"].assert_not_called()

    def test_skip_network_checks_still_activates_and_backs_up(self):
        value = set_egress(self.runner, self.store, NEW, skip_check=True)
        self.assertFalse(value["checked"])
        self.mocks["_probe"].assert_not_called()
        self.mocks["activate_nginx_config"].assert_called_once()
        self.mocks["create_backup"].assert_called_once()

    def test_status_reports_removed_address_without_changing_config(self):
        configured = configure_egress(self.original, ROOTS, "198.51.100.99")[0]
        self.mocks["_context"].return_value = (self.inventory, self.site, self.service, ROOTS, [(self.path, configured, 0o600)])
        value = egress_status(self.runner, self.store)
        self.assertFalse(value["available"])
        self.assertEqual(value["source"], "198.51.100.99")
        self.assertEqual(self.path.read_text(), self.original)

    def test_noop_avoids_container_recreation_and_backup(self):
        value = set_egress(self.runner, self.store, None)
        self.assertFalse(value["changed"])
        self.mocks["activate_nginx_config"].assert_not_called()
        self.mocks["create_backup"].assert_not_called()


class EgressCliTests(unittest.TestCase):
    def context(self, answers=(), json_output=False):
        answers = iter(answers)
        return CliContext(mock.Mock(), mock.Mock(), RuntimePaths(), io.StringIO(), io.StringIO(),
                          lambda _: next(answers), lambda _: "", json_output)

    def test_parser_mutation_classification(self):
        for arguments, handler, mutation in (
            (["status"], "site-egress-status", False),
            (["check", NEW], "site-egress-check", False),
            (["set", NEW, "--yes"], "site-egress-set", True),
            (["set", "auto", "--skip-check", "--yes"], "site-egress-set", True),
        ):
            parsed = build_parser().parse_args(["disguise", "egress", *arguments])
            self.assertEqual(parsed.handler, handler)
            self.assertEqual(_is_mutating(parsed), mutation)

    def test_json_status_and_failed_check(self):
        context = self.context(json_output=True)
        with mock.patch("remnawave_manager.cli.egress_status", return_value={"source": NEW, "available": True}):
            self.assertEqual(dispatch(build_parser().parse_args(["disguise", "egress", "status"]), context), 0)
        self.assertEqual(json.loads(context.stdout.getvalue())["source"], NEW)
        context = self.context(json_output=True)
        with mock.patch("remnawave_manager.cli.check_egress", return_value=[Probe(OLD, "url", False, 0, "", "", 5, "timeout")]):
            self.assertEqual(dispatch(build_parser().parse_args(["disguise", "egress", "check", OLD]), context), 1)
        self.assertFalse(json.loads(context.stdout.getvalue())[0]["ok"])

    def test_menu_selects_address_without_automatic_failover(self):
        context = self.context(["3", "3", "3"])
        with mock.patch("remnawave_manager.cli.egress_status", return_value={"source": OLD, "available": True,
                             "addresses": [{"address": ip, "interface": "ens3"} for ip in (OLD, NEW)]}):
            self.assertEqual(_interactive_arguments(context, 8), ["disguise", "egress", "set", NEW])

    def test_set_auto_dispatches_explicit_options(self):
        context = self.context(json_output=True)
        arguments = build_parser().parse_args(["disguise", "egress", "set", "auto", "--skip-check", "--yes"])
        with mock.patch("remnawave_manager.cli.set_egress", return_value={"source": "auto", "changed": True}) as setting:
            self.assertEqual(dispatch(arguments, context), 0)
        setting.assert_called_once_with(context.runner, context.store, None, skip_check=True)

class ExpandedProbeTests(EgressDNSTestCase):
    def test_live_probe_catches_alternating_success_failure(self):
        from remnawave_manager.site_egress import _check_live
        good = Probe('auto', 'url', True, 200, '', '', 0.1, 'OK')
        bad = Probe('auto', 'url', False, 500, '', '', 0.1, 'HTTP=500')
        with mock.patch('remnawave_manager.site_egress._probe', side_effect=[good, bad, good]) as probe:
            with self.assertRaises(TransactionError):
                _check_live(mock.Mock(), ('https://site.test', ('--unix-socket', '/socket')), (('url', '/api'),))
        self.assertEqual(probe.call_count, 3)

    def test_live_probe_exercises_discovered_media_and_posts(self):
        from remnawave_manager.site_egress import _live_results, _sample_routes
        value = [{'id': '123', 'avatar': 'https://mastodon.ml/system/avatar.jpg'}]
        samples = _sample_routes(value, 'https://mastodon.ml/api/v1/directory')
        self.assertEqual(len(samples), 2)
        good = Probe('auto', 'url', True, 200, '', '', 0.1, 'OK', 'nginx', samples)
        with mock.patch('remnawave_manager.site_egress._probe', return_value=good) as probe:
            results = _live_results(mock.Mock(), ('https://site.test', ('--unix-socket', '/socket')), (('url', '/api'),))
        self.assertEqual(len(results), 9)
        self.assertTrue(any('/_images/' in call.args[1] for call in probe.call_args_list))
        self.assertTrue(any('/statuses?' in call.args[1] for call in probe.call_args_list))

    def test_api_payload_cannot_choose_arbitrary_probe_destinations(self):
        from remnawave_manager.site_egress import _sample_routes
        values = [{'id': '../admin', 'avatar': 'https://127.0.0.1/admin', 'url': 'https://evil.example/x.jpg', 'image': 'https://user:pw@mastodon.ml/x.jpg'}]
        self.assertEqual(_sample_routes(values, 'https://mastodon.ml/api/v1/directory'), ())
        loop = _sample_routes({'result': [{'id': '8kyX1o', 'cloudSource300': 'https://media.gifs.ru/' + 'a' * 40 + '_300.webp'}]}, 'https://site.test/_loop/gifs/popular')
        self.assertEqual(loop[0][1], '/_loop/gifs/item/8kyX1o')
        self.assertTrue(loop[1][1].startswith('/_loop/media/'))

    def test_spros_probe_uses_required_feed_limit(self):
        self.assertEqual(PROBES['04-signal-works'], ((
            'https://otvet.mail.ru/api/topic/feed?limit=20', '/_answers/mail/feed?limit=20',
        ),))

    def test_current_ip_check_includes_nginx_and_other_ip_does_not(self):
        from remnawave_manager.site_egress import check_egress
        good = Probe(NEW, 'url', True, 200, NEW, '', 0.1, 'OK')
        with (
            mock.patch('remnawave_manager.site_egress._context', return_value=(mock.Mock(), mock.Mock(), {}, ROOTS, [])),
            mock.patch('remnawave_manager.site_egress._assigned_source'),
            mock.patch('remnawave_manager.site_egress._template_probes', return_value=(('url', '/api'),)),
            mock.patch('remnawave_manager.site_egress._current', return_value=(NEW, 1)),
            mock.patch('remnawave_manager.site_egress._local_target', return_value=('https://site.test', ())),
            mock.patch('remnawave_manager.site_egress.nginx_is_running', return_value=True),
            mock.patch('remnawave_manager.site_egress._probe', return_value=good),
            mock.patch('remnawave_manager.site_egress._live_results', return_value=[good] * 3) as live,
        ):
            self.assertEqual(len(check_egress(mock.Mock(), mock.Mock(), NEW)), 4)
            self.assertEqual(len(check_egress(mock.Mock(), mock.Mock(), OLD)), 1)
            live.assert_called_once()
