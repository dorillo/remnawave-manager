from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from remnawave_manager.adopt import (
    _bind_sources,
    _find_compose,
    _nginx_features,
    _regular_files,
    _site_sources,
    _warp_interfaces,
    adopt,
)
from remnawave_manager.errors import ValidationError
from remnawave_manager.models import Component, Inventory
from remnawave_manager.paths import RuntimePaths
from remnawave_manager.runner import Result
from remnawave_manager.state import StateStore


class AdoptBindSourceTests(unittest.TestCase):
    def test_exact_nginx_directory_target_is_detected(self) -> None:
        service = {
            "volumes": [
                {
                    "type": "bind",
                    "source": "/opt/remnanode/nginx",
                    "target": "/etc/nginx",
                }
            ]
        }

        self.assertEqual(
            _bind_sources(service, "/etc/nginx/"),
            [Path("/opt/remnanode/nginx")],
        )

    def test_bind_target_must_be_inside_expected_container_directory(self) -> None:
        service = {
            "volumes": [
                {
                    "type": "bind",
                    "source": "/tmp/unrelated",
                    "target": "/srv/etc/nginx/conf.d",
                },
                {
                    "type": "bind",
                    "source": "/opt/remnanode/nginx.conf",
                    "target": "/etc/nginx/conf.d/default.conf",
                },
            ]
        }

        self.assertEqual(
            _bind_sources(service, "/etc/nginx/"),
            [Path("/opt/remnanode/nginx.conf")],
        )

    def test_nginx_directory_rejects_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            conf = root / "conf.d"
            conf.mkdir()
            first = root / "nginx.conf"
            second = conf / "xhttp.conf"
            first.write_text("events {}\n", encoding="utf-8")
            second.write_text("server {}\n", encoding="utf-8")
            link = root / "outside.conf"
            try:
                link.symlink_to(first)
            except OSError:
                link = None

            if link is None:
                self.assertEqual(set(_regular_files(root)), {first, second})
            else:
                with self.assertRaisesRegex(ValidationError, "символическую ссылку"):
                    _regular_files(root)

    def test_nginx_sources_skip_valid_certbot_live_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            letsencrypt = root / "letsencrypt"
            live = letsencrypt / "live/panel.example.com"
            archive = letsencrypt / "archive/panel.example.com"
            live.mkdir(parents=True)
            archive.mkdir(parents=True)
            archived_certificate = archive / "fullchain1.pem"
            archived_certificate.write_text("certificate\n", encoding="utf-8")
            certificate = live / "fullchain.pem"
            try:
                certificate.symlink_to("../../archive/panel.example.com/fullchain1.pem")
            except OSError as error:
                self.skipTest(f"symbolic links unavailable: {error}")
            if os.name == "posix":
                archived_certificate.chmod(0o600)

            with mock.patch(
                "remnawave_manager.runner._LETSENCRYPT_ROOT", letsencrypt
            ):
                self.assertEqual(_regular_files(certificate), [])

    def test_nginx_feature_scan_rejects_hardlinked_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "nginx.conf"
            source.write_text("events {}\n", encoding="utf-8")
            hardlink = root / "duplicate.conf"
            try:
                os.link(source, hardlink)
            except OSError as error:
                self.skipTest(f"hard links unavailable: {error}")

            with self.assertRaisesRegex(ValidationError, "hardlink"):
                _nginx_features([hardlink])

    def test_nginx_feature_scan_rejects_invalid_utf8(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "nginx.conf"
            config.write_bytes(b"events {}\n\xff")

            with self.assertRaisesRegex(ValidationError, "UTF-8"):
                _nginx_features([config])

    def test_nginx_feature_scan_detects_beeline_post_separately_from_yandex(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "nginx.conf"
            config.write_text(
                """\
# Beeline CDN POST origin.
upstream beeline_xhttp {
    server 127.0.0.1:7443;
}
server {
    listen unix:/dev/shm/nginx.sock ssl proxy_protocol;
    location = /source/origin {
        proxy_pass http://beeline_xhttp;
        proxy_set_header X-Real-IP $proxy_protocol_addr;
        proxy_request_buffering off;
        proxy_read_timeout 86400s;
    }
}
""",
                encoding="utf-8",
            )

            sockets, features = _nginx_features([config])

            self.assertEqual(sockets, ["/dev/shm/nginx.sock"])
            self.assertTrue(features["xhttp_stream_separation"])
            self.assertTrue(features["beeline_cdn_post"])
            self.assertFalse(features["beeline_cdn_get"])
            self.assertFalse(features["yandex_cdn"])

    def test_nginx_feature_scan_keeps_legacy_beeline_get_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "nginx.conf"
            config.write_text(
                """\
# Legacy Beeline CDN GET origin.
upstream xray_beeline_xhttp {
    server 127.0.0.1:2092;
}
server {
    listen unix:/dev/shm/nginx.sock ssl proxy_protocol;
    location = /cdn-assets/example/segment.ts {
        proxy_pass http://xray_beeline_xhttp/cdn-assets/example/segment.ts/;
        proxy_set_header X-Real-IP $proxy_protocol_addr;
    }
}
""",
                encoding="utf-8",
            )

            _, features = _nginx_features([config])

            self.assertTrue(features["beeline_cdn_get"])
            self.assertFalse(features["beeline_cdn_post"])
            self.assertFalse(features["yandex_cdn"])

    def test_nginx_feature_scan_detects_direct_unix_proxy_pass_socket(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "nginx.conf"
            config.write_text(
                """\
server {
    listen unix:/dev/shm/nginx.sock ssl proxy_protocol;
    location ^~ /assets/opaque/ {
        proxy_pass http://unix:/dev/shm/xrxh.socket;
    }
}
""",
                encoding="utf-8",
            )

            sockets, features = _nginx_features([config])

            self.assertEqual(
                sockets,
                ["/dev/shm/nginx.sock", "/dev/shm/xrxh.socket"],
            )
            self.assertTrue(features["xhttp_stream_separation"])

    def test_site_bind_root_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actual = root / "actual-site"
            actual.mkdir()
            link = root / "site"
            try:
                link.symlink_to(actual, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"symlinks unavailable: {error}")
            compose = {
                "services": {
                    "nginx": {
                        "volumes": [
                            {
                                "type": "bind",
                                "source": str(link),
                                "target": "/var/www/html",
                            }
                        ]
                    }
                }
            }

            with self.assertRaisesRegex(ValidationError, "небезопасный тип"):
                _site_sources(compose, {"nginx": Component("nginx", "nginx")})

    def test_warp_detection_fails_closed_when_ip_cannot_be_read(self) -> None:
        class IpRunner:
            def run(self, args, **kwargs):  # type: ignore[no-untyped-def]
                return Result(tuple(args), 1, "", "permission denied")

        with self.assertRaisesRegex(ValidationError, "WARP"):
            _warp_interfaces(IpRunner())  # type: ignore[arg-type]

    def test_warp_detection_returns_all_recognized_interfaces(self) -> None:
        class IpRunner:
            def run(self, args, **kwargs):  # type: ignore[no-untyped-def]
                return Result(
                    tuple(args),
                    0,
                    json.dumps(
                        [
                            {"ifname": "eth0"},
                            {"ifname": "warp"},
                            {"ifname": "wgcf-secondary"},
                        ]
                    ),
                    "",
                )

        self.assertEqual(
            _warp_interfaces(IpRunner()),  # type: ignore[arg-type]
            ["warp", "wgcf-secondary"],
        )

    def test_symlinked_compose_is_rejected_before_any_rewrite_can_replace_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "actual-compose.yml"
            target.write_text("services: {}\n", encoding="utf-8")
            compose = root / "docker-compose.yml"
            try:
                compose.symlink_to(target)
            except OSError:
                self.skipTest("symlink creation is unavailable")

            with self.assertRaisesRegex(ValidationError, "symlink"):
                _find_compose(root)

    def test_multiple_standard_compose_files_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
            (root / "compose.yaml").write_text("services: {}\n", encoding="utf-8")

            with self.assertRaisesRegex(ValidationError, "несколько"):
                _find_compose(root)

    def test_duplicate_component_candidates_are_rejected_as_ambiguous(self) -> None:
        class DuplicateRunner:
            def run(self, args, **kwargs):  # type: ignore[no-untyped-def]
                command = tuple(args)
                if command[:2] == ("docker", "compose"):
                    payload = {
                        "services": {
                            "panel-primary": {"image": "remnawave/backend:2"},
                            "panel-shadow": {"image": "remnawave/backend:3"},
                        }
                    }
                    return Result(command, 0, json.dumps(payload), "")
                if command[:2] == ("docker", "inspect"):
                    return Result(command, 1, "", "not found")
                return Result(command, 0, "[]", "")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "install"
            install.mkdir()
            (install / "docker-compose.yml").write_text(
                "services:\n  panel-primary:\n    image: remnawave/backend:2\n",
                encoding="utf-8",
            )
            store = StateStore(RuntimePaths(root / "runtime"))

            with self.assertRaisesRegex(ValidationError, "неоднозначно"):
                adopt(DuplicateRunner(), store, directory=install)  # type: ignore[arg-type]

    def test_adoption_cannot_overwrite_inventory_during_incomplete_update(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            install = root / "install"
            install.mkdir()
            (install / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
            store = StateStore(RuntimePaths(root / "runtime"))
            store.initialize()
            journal = store.paths.state / "active-transaction.json"
            journal.write_text('{"phase":"starting-panel"}\n', encoding="utf-8")

            with self.assertRaisesRegex(ValidationError, "незавершённая транзакция"):
                adopt(object(), store, directory=install)  # type: ignore[arg-type]


    def test_adoption_rejects_install_directory_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "actual-install"
            target.mkdir()
            (target / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
            link = root / "install"
            try:
                link.symlink_to(target, target_is_directory=True)
            except OSError:
                self.skipTest("symlink creation is unavailable")

            store = StateStore(RuntimePaths(root / "runtime"))
            with self.assertRaisesRegex(ValidationError, "symlink"):
                adopt(object(), store, directory=link)  # type: ignore[arg-type]

    def test_adoption_cannot_rebind_existing_state_to_another_installation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "current"
            other = root / "other"
            current.mkdir()
            other.mkdir()
            store = StateStore(RuntimePaths(root / "runtime"))
            store.save_inventory(
                Inventory(
                    schema_version=1,
                    role="node",
                    install_dir=str(current),
                    compose_file=str(current / "docker-compose.yml"),
                    env_file=None,
                    webserver=None,
                    components={"node": Component("node", "remnanode")},
                )
            )

            with self.assertRaisesRegex(ValidationError, "другой установкой"):
                adopt(object(), store, directory=other)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
