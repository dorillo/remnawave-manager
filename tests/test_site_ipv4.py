from __future__ import annotations

import unittest
from unittest import mock

from remnawave_manager.errors import ValidationError
from remnawave_manager.site_ipv4 import system_resolvers, synchronize_upstreams, bind_origins
from remnawave_manager.site_egress_config import bind_body, configure_egress
from remnawave_manager.site_config import upgrade_site_config
from remnawave_manager.site_policy import TEMPLATE_POLICIES
from test_site_deployment import legacy_config, TRANSPORT, SUBSCRIPTION

ROOTS = {"/var/www/decoy"}
SOURCE = "198.51.100.20"


class IPv4UpstreamTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch("remnawave_manager.site_ipv4.system_resolvers", return_value=("127.0.0.53",))
        patch.start()
        self.addCleanup(patch.stop)

    def test_all_templates_use_dns_a_only_without_changing_uri_or_tls(self):
        for template in TEMPLATE_POLICIES:
            with self.subTest(template=template):
                plain, _ = upgrade_site_config(legacy_config(), template, ROOTS)
                bound, count, _ = configure_egress(plain, ROOTS, SOURCE)
                self.assertGreater(count, 0)
                self.assertIn('resolver 127.0.0.53 ipv6=off;', bound)
                self.assertIn('server mastodon.ml:443 resolve;', bound)
                self.assertIn('proxy_pass https://rwm_site_ipv4_mastodon_ml/;', bound)
                self.assertEqual(bound.count('proxy_ssl_name '), plain.count('proxy_ssl_name '))
                self.assertEqual(bound.count('proxy_ssl_verify on;'), plain.count('proxy_ssl_verify on;'))
                self.assertNotIn('proxy_pass https://$', bound)
                self.assertIn(TRANSPORT, bound)
                self.assertTrue(bound.endswith(SUBSCRIPTION))
                self.assertEqual(configure_egress(bound, ROOTS, None)[0], plain)

    def test_legacy_bindings_migrate_even_when_source_is_unchanged(self):
        plain, _ = upgrade_site_config(legacy_config(), '01-northline', ROOTS)
        old, _ = bind_body(plain, SOURCE, legacy=True)
        new, _, previous = configure_egress(old, ROOTS, SOURCE)
        self.assertEqual(previous, [SOURCE])
        self.assertIn('ipv6=off', new)
        self.assertEqual(configure_egress(new, ROOTS, SOURCE)[0], new)
        self.assertEqual(upgrade_site_config(old, '01-northline', ROOTS)[0], new)

    def test_unknown_or_edited_upstream_blocks_are_rejected(self):
        plain, _ = upgrade_site_config(legacy_config(), '01-northline', ROOTS)
        selected = configure_egress(plain, ROOTS, SOURCE)[0]
        for text in (
            selected.replace('ipv6=off', 'ipv6=on', 1),
            selected.replace('server mastodon.ml:443', 'server evil.example:443', 1),
            selected.replace('zone rwm_site_ipv4_mastodon_ml 256k;', 'zone custom 256k;'),
            selected.replace(f'        proxy_bind {SOURCE}; # rwm-site-egress\n', ''),
            'upstream rwm_site_ipv4_mastodon_ml { server evil.example; }\n' + plain,
        ):
            with self.subTest(text=text[:80]), self.assertRaises(ValidationError):
                configure_egress(text, ROOTS, SOURCE)

    def test_unused_upstreams_disappear_and_foreign_bound_vhost_stays_working(self):
        plain, _ = upgrade_site_config(legacy_config(), '01-northline', ROOTS)
        other = plain.replace('/var/www/decoy', '/var/www/other')
        both = configure_egress(plain + other, ROOTS | {'/var/www/other'}, SOURCE)[0]
        remaining = configure_egress(both, ROOTS, None)[0]
        self.assertIn('ipv6=off', remaining)
        self.assertEqual(configure_egress(remaining, {'/var/www/other'}, None)[0], plain + other)

    def test_literal_paths_and_escaped_image_prefix_semantics_are_retained(self):
        for uri in ('', '/', '/api/search/video/', '/api/topic/feed'):
            original = f'        proxy_pass https://example.com{uri};'
            self.assertEqual(bind_origins(original), f'        proxy_pass https://rwm_site_ipv4_example_com{uri};')


class ResolverTests(unittest.TestCase):
    def test_uses_configured_ipv4_and_ipv6_nameservers(self):
        with mock.patch('pathlib.Path.read_text', return_value='nameserver 127.0.0.53\nnameserver 2001:db8::53\nnameserver 127.0.0.53\n'):
            self.assertEqual(system_resolvers(), ('127.0.0.53', '[2001:db8::53]'))

    def test_missing_or_unsafe_nameserver_fails_without_public_fallback(self):
        for content in ('', 'nameserver example.org', 'nameserver 0.0.0.0', 'nameserver 224.0.0.1', 'nameserver fe80::1%ens3'):
            with self.subTest(content=content), mock.patch('pathlib.Path.read_text', return_value=content), self.assertRaises(ValidationError):
                system_resolvers()
