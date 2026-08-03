from __future__ import annotations

import base64
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from remnawave_manager.errors import ValidationError
from remnawave_manager.warp_config import load_warp_profile, parse_warp_profile

PRIVATE_KEY = base64.b64encode(bytes(range(32))).decode("ascii")
PUBLIC_KEY = base64.b64encode(bytes(range(32, 64))).decode("ascii")


def profile_text(
    *,
    interface_extra: str = "Table = off\n",
    peer_extra: str = "",
    private_key: str = PRIVATE_KEY,
    public_key: str = PUBLIC_KEY,
) -> str:
    return (
        "[Interface]\n"
        f"PrivateKey = {private_key}\n"
        "Address = 172.16.0.2/32, 2606:4700:110:8765::2/128\n"
        "MTU = 1280\n"
        f"{interface_extra}"
        "\n"
        "[Peer]\n"
        f"PublicKey = {public_key}\n"
        "AllowedIPs = 0.0.0.0/0, ::/0\n"
        "Endpoint = engage.cloudflareclient.com:2408\n"
        "PersistentKeepalive = 25\n"
        f"{peer_extra}"
    )


class WarpConfigTests(unittest.TestCase):
    def test_generated_wgcf_profile_is_normalized_to_table_off_without_dns(self) -> None:
        generated = profile_text(interface_extra="DNS = 1.1.1.1\n")

        parsed = parse_warp_profile(generated, generated_profile=True)
        canonical = parsed.render()

        self.assertIn("Table = off\n", canonical)
        self.assertNotIn("DNS", canonical)
        self.assertEqual(parse_warp_profile(canonical), parsed)

    def test_accepts_canonical_profile(self) -> None:
        parsed = parse_warp_profile(profile_text())

        self.assertEqual(parsed.mtu, 1280)
        self.assertEqual(parsed.keepalive, 25)
        self.assertEqual(parsed.allowed_ips, ("0.0.0.0/0", "::/0"))

    def test_requires_ipv4_default_for_bound_interface_routing(self) -> None:
        ipv6_only = profile_text().replace(
            "AllowedIPs = 0.0.0.0/0, ::/0",
            "AllowedIPs = ::/0",
        )

        with self.assertRaisesRegex(ValidationError, "IPv4 default"):
            parse_warp_profile(ipv6_only)

    def test_rejects_addresses_that_can_install_connected_network_routes(self) -> None:
        unsafe_addresses = (
            "172.16.0.2/24",
            "2606:4700:110:8765::2/64",
            "172.16.0.2/32, 172.16.0.3/32",
            "127.0.0.1/32",
        )
        for addresses in unsafe_addresses:
            with self.subTest(addresses=addresses), self.assertRaisesRegex(
                ValidationError, "host-адреса"
            ):
                parse_warp_profile(
                    profile_text().replace(
                        "172.16.0.2/32, 2606:4700:110:8765::2/128",
                        addresses,
                    )
                )

    def test_rejects_shell_hooks(self) -> None:
        for hook in ("PreUp", "PostUp", "PreDown", "PostDown", "SaveConfig"):
            with self.subTest(hook=hook), self.assertRaises(ValidationError):
                parse_warp_profile(profile_text(interface_extra=f"Table = off\n{hook} = /bin/true\n"))

    def test_rejects_dns_or_route_table_takeover_for_imported_profile(self) -> None:
        unsafe = (
            profile_text(interface_extra="Table = off\nDNS = 1.1.1.1\n"),
            profile_text(interface_extra=""),
            profile_text(interface_extra="Table = auto\n"),
        )
        for text in unsafe:
            with self.subTest(text=text), self.assertRaises(ValidationError):
                parse_warp_profile(text)

    def test_rejects_multiple_peers(self) -> None:
        text = profile_text() + "\n[Peer]\nPublicKey = " + PUBLIC_KEY + "\n"

        with self.assertRaises(ValidationError):
            parse_warp_profile(text)

    def test_rejects_duplicate_keys_that_differ_only_by_case(self) -> None:
        variants = (
            profile_text(interface_extra=f"privatekey = {PRIVATE_KEY}\nTable = off\n"),
            profile_text(peer_extra=f"publickey = {PUBLIC_KEY}\n"),
        )
        for text in variants:
            with self.subTest(text=text), self.assertRaises(ValidationError):
                parse_warp_profile(text)

    def test_rejects_malformed_or_wrong_length_wireguard_keys(self) -> None:
        bad_keys = ("not-base64!", base64.b64encode(b"short").decode("ascii"))
        for key in bad_keys:
            with self.subTest(kind="private", key=key), self.assertRaises(ValidationError):
                parse_warp_profile(profile_text(private_key=key))
            with self.subTest(kind="public", key=key), self.assertRaises(ValidationError):
                parse_warp_profile(profile_text(public_key=key))

    def test_rejects_malformed_endpoint_host_and_ipv6(self) -> None:
        endpoints = (
            "bad..host:2408",
            "-bad.example:2408",
            "[::::]:2408",
            "engage.cloudflareclient.com:0",
        )
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint), self.assertRaises(ValidationError):
                parse_warp_profile(
                    profile_text().replace(
                        "Endpoint = engage.cloudflareclient.com:2408",
                        f"Endpoint = {endpoint}",
                    )
                )

    def test_rejects_oversized_profile_before_parsing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "warp.conf"
            profile.write_bytes(b"x" * (64 * 1024 + 1))
            os.chmod(profile, 0o600)

            with self.assertRaisesRegex(ValidationError, "65536"):
                load_warp_profile(profile)

    @unittest.skipUnless(hasattr(os, "link"), "OS does not expose hard links")
    def test_rejects_hardlinked_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            profile = directory / "warp.conf"
            profile.write_text(profile_text(), encoding="utf-8")
            os.chmod(profile, 0o600)
            alias = directory / "alias.conf"
            try:
                os.link(profile, alias)
            except OSError as error:
                self.skipTest(f"hard links unavailable: {error}")

            with self.assertRaisesRegex(ValidationError, "отдельным файлом"):
                load_warp_profile(profile)

    @unittest.skipUnless(os.name == "posix", "POSIX mode checks are unavailable")
    def test_rejects_profile_writable_by_other_users(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "warp.conf"
            profile.write_text(profile_text(), encoding="utf-8")
            os.chmod(profile, 0o666)

            with self.assertRaisesRegex(ValidationError, "записи"):
                load_warp_profile(profile)

    @unittest.skipUnless(hasattr(os, "symlink"), "OS does not expose symbolic links")
    def test_rejects_symlinked_profile(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "real.conf"
            target.write_text(profile_text(), encoding="utf-8")
            link = directory / "warp.conf"
            try:
                link.symlink_to(target)
            except OSError as error:
                self.skipTest(f"symbolic links unavailable: {error}")

            with self.assertRaises(ValidationError):
                load_warp_profile(link)


if __name__ == "__main__":
    unittest.main()
