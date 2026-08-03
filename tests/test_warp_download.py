from __future__ import annotations

import hashlib
import io
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from typing import ClassVar
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from remnawave_manager import warp, warp_download
from remnawave_manager.errors import TransactionError, ValidationError
from remnawave_manager.paths import RuntimePaths


class WarpDownloadTests(unittest.TestCase):
    def test_download_disables_environment_proxy_and_limits_redirects(self) -> None:
        payload = b"pinned wgcf test executable\n"
        contract = {
            "version": "test-version",
            "asset": "wgcf-test",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "url": "https://github.com/example/wgcf-test",
        }

        class Response:
            headers: ClassVar[dict[str, str]] = {}

            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *_args):  # type: ignore[no-untyped-def]
                return False

            def geturl(self) -> str:
                return contract["url"]

            def read(self, _limit: int) -> bytes:
                return payload

        opener = mock.Mock()
        opener.open.return_value = Response()
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(warp_download, "wgcf_contract", return_value=contract),
            mock.patch.object(
                warp_download.urllib.request,
                "build_opener",
                return_value=opener,
            ) as build_opener,
            mock.patch.dict(
                os.environ,
                {"HTTPS_PROXY": "http://untrusted-proxy.example:8080"},
                clear=False,
            ),
        ):
            installed = warp_download.install_wgcf(Path(temporary) / "bin")
            installed_payload = installed.read_bytes()

        self.assertEqual(installed_payload, payload)
        handlers = build_opener.call_args.args
        self.assertEqual(len(handlers), 2)
        self.assertIsInstance(handlers[0], urllib.request.ProxyHandler)
        self.assertEqual(handlers[0].proxies, {})
        self.assertIsInstance(handlers[1], warp_download._SafeRedirect)

    def test_download_closes_http_error(self) -> None:
        contract = {
            "version": "test-version",
            "asset": "wgcf-test",
            "sha256": "0" * 64,
            "url": "https://github.com/example/wgcf-test",
        }
        error = urllib.error.HTTPError(
            contract["url"],
            503,
            "Unavailable",
            {},
            io.BytesIO(b"unavailable"),
        )
        opener = mock.Mock()
        opener.open.side_effect = error
        with (
            tempfile.TemporaryDirectory() as temporary,
            mock.patch.object(warp_download, "wgcf_contract", return_value=contract),
            mock.patch.object(
                warp_download.urllib.request,
                "build_opener",
                return_value=opener,
            ),
            mock.patch.object(error, "close", wraps=error.close) as close,
            self.assertRaisesRegex(TransactionError, "HTTP 503"),
        ):
            warp_download.install_wgcf(Path(temporary) / "bin")

        close.assert_called_once_with()

    def test_installs_verified_local_asset_and_reuses_verified_target(self) -> None:
        payload = b"pinned wgcf test executable\n"
        digest = hashlib.sha256(payload).hexdigest()
        contract = {
            "version": "test-version",
            "asset": "wgcf-test",
            "sha256": digest,
            "url": "https://github.com/example/wgcf-test",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local = root / "local-wgcf"
            local.write_bytes(payload)
            destination = root / "bin"

            with mock.patch.object(
                warp_download, "wgcf_contract", return_value=contract
            ):
                installed = warp_download.install_wgcf(destination, local_file=local)
                reused = warp_download.install_wgcf(destination)

            self.assertEqual(installed, destination / "wgcf-test-version")
            self.assertEqual(reused, installed)
            self.assertEqual(installed.read_bytes(), payload)
            notice = warp_download.wgcf_notice_path(installed)
            self.assertIn(
                "Copyright (c) 2020 ViRb3", notice.read_text(encoding="utf-8")
            )

    def test_reinstalls_wgcf_notice_without_downloading_binary(self) -> None:
        payload = b"pinned wgcf test executable\n"
        contract = {
            "version": "test-version",
            "asset": "wgcf-test",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "url": "https://github.com/example/wgcf-test",
        }
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "bin"
            destination.mkdir()
            binary = destination / "wgcf-test-version"
            binary.write_bytes(payload)
            notice = warp_download.wgcf_notice_path(binary)
            notice.write_text("повреждено", encoding="utf-8")

            with (
                mock.patch.object(
                    warp_download, "wgcf_contract", return_value=contract
                ),
                mock.patch.object(
                    warp_download.urllib.request, "build_opener"
                ) as opener,
            ):
                reused = warp_download.install_wgcf(destination)

            self.assertEqual(reused, binary)
            self.assertFalse(opener.called)
            self.assertIn(
                "Permission is hereby granted", notice.read_text(encoding="utf-8")
            )

    def test_wgcf_binary_and_notice_are_both_tracked_as_owned(self) -> None:
        payload = b"pinned wgcf test executable\n"
        contract = {
            "version": "test-version",
            "asset": "wgcf-test",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "url": "https://github.com/example/wgcf-test",
        }
        with tempfile.TemporaryDirectory() as temporary:
            paths = warp.WarpPaths(RuntimePaths(Path(temporary)))
            local = Path(temporary) / "local-wgcf"
            local.write_bytes(payload)
            with mock.patch.object(
                warp_download, "wgcf_contract", return_value=contract
            ):
                binary = warp_download.install_wgcf(paths.bin_dir, local_file=local)

            owned = warp._owned(paths, binary)

            self.assertIn(str(binary), owned)
            self.assertIn(str(warp_download.wgcf_notice_path(binary)), owned)

    def test_rejects_local_asset_with_wrong_hash_without_installing_it(self) -> None:
        contract = {
            "version": "test-version",
            "asset": "wgcf-test",
            "sha256": "0" * 64,
            "url": "https://github.com/example/wgcf-test",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local = root / "local-wgcf"
            local.write_bytes(b"tampered")
            destination = root / "bin"

            with (
                mock.patch.object(
                    warp_download, "wgcf_contract", return_value=contract
                ),
                self.assertRaises(ValidationError),
            ):
                warp_download.install_wgcf(destination, local_file=local)

            self.assertFalse((destination / "wgcf-test-version").exists())
            self.assertFalse((destination / "wgcf-test-version.LICENSE.txt").exists())

    def test_rejects_unknown_architecture(self) -> None:
        manifest = {
            "tools": {
                "wgcf": {
                    "version": "2.2.32",
                    "base_url": "https://github.com/ViRb3/wgcf/releases/download/v2.2.32",
                    "architectures": {"x86_64": {}, "aarch64": {}},
                }
            }
        }
        with (
            mock.patch.object(
                warp_download.platform, "machine", return_value="riscv64"
            ),
            mock.patch.object(warp_download, "load_manifest", return_value=manifest),
            self.assertRaises(ValidationError),
        ):
            warp_download.wgcf_contract()

    def test_rejects_unsafe_manifest_version_before_path_construction(self) -> None:
        manifest = {
            "tools": {
                "wgcf": {
                    "version": "../../escape",
                    "base_url": "https://github.com/ViRb3/wgcf/releases/download/v2.2.32",
                    "architectures": {
                        "x86_64": {
                            "asset": "wgcf_2.2.32_linux_amd64",
                            "sha256": "0" * 64,
                        }
                    },
                }
            }
        }
        with (
            mock.patch.object(warp_download.platform, "machine", return_value="x86_64"),
            mock.patch.object(warp_download, "load_manifest", return_value=manifest),
            self.assertRaisesRegex(ValidationError, "версию"),
        ):
            warp_download.wgcf_contract()

    def test_notice_failure_removes_new_partial_binary(self) -> None:
        payload = b"pinned wgcf test executable\n"
        contract = {
            "version": "test-version",
            "asset": "wgcf-test",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "url": "https://github.com/example/wgcf-test",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local = root / "local-wgcf"
            local.write_bytes(payload)
            destination = root / "bin"
            target = destination / "wgcf-test-version"

            def fail_after_write(binary: Path) -> None:
                warp_download.wgcf_notice_path(binary).write_text(
                    "partial notice\n",
                    encoding="utf-8",
                )
                raise OSError("notice write failed")

            with (
                mock.patch.object(
                    warp_download, "wgcf_contract", return_value=contract
                ),
                mock.patch.object(
                    warp_download,
                    "_install_notice",
                    side_effect=fail_after_write,
                ),
                self.assertRaisesRegex(TransactionError, "notice write failed"),
            ):
                warp_download.install_wgcf(destination, local_file=local)

            self.assertFalse(target.exists())
            self.assertFalse(warp_download.wgcf_notice_path(target).exists())

    def test_existing_binary_failure_restores_notice_and_mode(self) -> None:
        payload = b"pinned wgcf test executable\n"
        contract = {
            "version": "test-version",
            "asset": "wgcf-test",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "url": "https://github.com/example/wgcf-test",
        }
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "bin"
            destination.mkdir()
            target = destination / "wgcf-test-version"
            target.write_bytes(payload)
            os.chmod(target, 0o700)
            notice = warp_download.wgcf_notice_path(target)
            notice.write_text("operator notice\n", encoding="utf-8")

            def fail_after_write(binary: Path) -> None:
                warp_download.wgcf_notice_path(binary).write_text(
                    "partial replacement\n",
                    encoding="utf-8",
                )
                raise OSError("notice replacement failed")

            with (
                mock.patch.object(
                    warp_download, "wgcf_contract", return_value=contract
                ),
                mock.patch.object(
                    warp_download,
                    "_install_notice",
                    side_effect=fail_after_write,
                ),
                self.assertRaisesRegex(TransactionError, "notice replacement failed"),
            ):
                warp_download.install_wgcf(destination)

            self.assertEqual(target.read_bytes(), payload)
            if os.name == "posix":
                self.assertEqual(target.stat().st_mode & 0o777, 0o700)
            self.assertEqual(notice.read_text(encoding="utf-8"), "operator notice\n")

    def test_orphan_notice_is_never_overwritten(self) -> None:
        payload = b"pinned wgcf test executable\n"
        contract = {
            "version": "test-version",
            "asset": "wgcf-test",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "url": "https://github.com/example/wgcf-test",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            local = root / "local-wgcf"
            local.write_bytes(payload)
            destination = root / "bin"
            destination.mkdir()
            target = destination / "wgcf-test-version"
            notice = warp_download.wgcf_notice_path(target)
            notice.write_text("foreign notice\n", encoding="utf-8")

            with (
                mock.patch.object(
                    warp_download, "wgcf_contract", return_value=contract
                ),
                self.assertRaisesRegex(ValidationError, "уже существует"),
            ):
                warp_download.install_wgcf(destination, local_file=local)

            self.assertFalse(target.exists())
            self.assertEqual(notice.read_text(encoding="utf-8"), "foreign notice\n")

    @unittest.skipUnless(hasattr(os, "symlink"), "OS does not expose symbolic links")
    def test_rejects_symlinked_local_asset(self) -> None:
        payload = b"pinned wgcf test executable\n"
        contract = {
            "version": "test-version",
            "asset": "wgcf-test",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "url": "https://github.com/example/wgcf-test",
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real-wgcf"
            real.write_bytes(payload)
            local = root / "local-wgcf"
            try:
                local.symlink_to(real)
            except OSError as error:
                self.skipTest(f"symbolic links unavailable: {error}")

            with (
                mock.patch.object(
                    warp_download, "wgcf_contract", return_value=contract
                ),
                self.assertRaises(ValidationError),
            ):
                warp_download.install_wgcf(root / "bin", local_file=local)

    def test_rejects_non_https_download_contract_before_network(self) -> None:
        contract = {
            "version": "test-version",
            "asset": "wgcf-test",
            "sha256": "0" * 64,
            "url": "http://github.com/example/wgcf-test",
        }
        with tempfile.TemporaryDirectory() as temporary:
            with (
                mock.patch.object(
                    warp_download, "wgcf_contract", return_value=contract
                ),
                mock.patch.object(
                    warp_download.urllib.request, "build_opener"
                ) as opener,
                self.assertRaisesRegex(ValidationError, "URL"),
            ):
                warp_download.install_wgcf(Path(temporary) / "bin")

            opener.assert_not_called()

    def test_trusted_download_url_rejects_userinfo_and_nonstandard_port(self) -> None:
        self.assertTrue(
            warp_download._is_trusted_url(
                "https://github.com/ViRb3/wgcf/releases/download/v2.2.32/wgcf"
            )
        )
        self.assertFalse(
            warp_download._is_trusted_url(
                "https://operator@github.com/ViRb3/wgcf/releases/download/v2.2.32/wgcf"
            )
        )
        self.assertFalse(
            warp_download._is_trusted_url(
                "https://github.com:444/ViRb3/wgcf/releases/download/v2.2.32/wgcf"
            )
        )
        for value in (
            "https://github.com/path with space",
            "https://github.com/path\nheader",
            "https://github.com/path\u202eexe",
        ):
            with self.subTest(value=value):
                self.assertFalse(warp_download._is_trusted_url(value))


if __name__ == "__main__":
    unittest.main()
