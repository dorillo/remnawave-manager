import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remnawave_manager.certificates import (
    assert_no_active_certbot_renewal,
    configure_adopted_certbot,
    install_renewal_hooks,
)
from remnawave_manager.errors import ValidationError
from remnawave_manager.models import Component, Inventory
from remnawave_manager.runner import Runner


class SystemNginxCertbotTests(unittest.TestCase):
    def _assert_shared_lock(self, script: str, *, before: str) -> None:
        flock = "/usr/bin/flock -w 120 9"
        self.assertIn('/run/remnawave-manager/manager.lock', script)
        self.assertIn(flock, script)
        self.assertIn('RWM_CERTBOT_MANAGER_LOCK_HELD', script)
        self.assertLess(script.index(flock), script.index(before))

    def _fixture(
        self,
        temporary: str,
        *,
        authenticator: str = "standalone",
        reference_certificate: bool = True,
    ) -> tuple[Path, Path, Inventory, dict[str, object]]:
        root = Path(temporary) / "letsencrypt"
        renewal = root / "renewal"
        renewal.mkdir(parents=True)
        (renewal / "panel.example.com.conf").write_text(
            "version = 2.11.0\n"
            "[renewalparams]\n"
            f"authenticator = {authenticator}\n",
            encoding="utf-8",
        )
        nginx = Path(temporary) / "panel.conf"
        certificate_root = root if reference_certificate else Path("/foreign")
        nginx.write_text(
            "server {\n"
            f"    ssl_certificate {certificate_root.as_posix()}"
            "/live/panel.example.com/fullchain.pem;\n"
            "}\n",
            encoding="utf-8",
        )
        inventory = Inventory(
            schema_version=1,
            role="panel",
            install_dir="/opt/remnawave",
            compose_file="/opt/remnawave/docker-compose.yml",
            env_file="/opt/remnawave/.env",
            webserver="nginx",
            nginx_files=[str(nginx)],
            components={
                "panel": Component("panel", "remnawave", container="remnawave")
            },
            features={"containerized_nginx": False},
        )
        compose: dict[str, object] = {"services": {"remnawave": {}}}
        if not reference_certificate:
            compose = {
                "services": {
                    "remnawave": {
                        "volumes": [
                            {
                                "type": "bind",
                                "source": str(
                                    root / "live" / "panel.example.com"
                                ),
                                "target": "/certificate",
                            }
                        ]
                    }
                }
            }
        return root, nginx, inventory, compose

    def test_adoption_installs_system_nginx_standalone_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, _, inventory, compose = self._fixture(temporary)
            runner = mock.Mock(spec=Runner)

            with mock.patch(
                "remnawave_manager.certificates.command_exists", return_value=False
            ), mock.patch(
                "remnawave_manager.certificates._certbot_timer_state",
                return_value=("disabled", False),
            ):
                plan = configure_adopted_certbot(
                    runner, inventory, compose, letsencrypt_root=root
                )

            self.assertEqual(plan.certificate_names, ("panel.example.com",))
            self.assertTrue(plan.uses_standalone)
            hook_root = root / "renewal-hooks"
            deploy = (hook_root / "deploy/remnawave-manager-nginx").read_text(
                encoding="utf-8"
            )
            pre = (hook_root / "pre/remnawave-manager-nginx").read_text(
                encoding="utf-8"
            )
            post = (hook_root / "post/remnawave-manager-nginx").read_text(
                encoding="utf-8"
            )

            self.assertIn("if /usr/bin/systemctl is-active --quiet nginx; then", deploy)
            self.assertLess(deploy.index("is-active"), deploy.index("nginx -t"))
            self.assertLess(deploy.index("nginx -t"), deploy.index("reload nginx"))
            self.assertNotIn("docker", deploy + pre + post)
            marker = 'marker="/run/remnawave-manager-certbot-nginx-${PPID}"'
            self._assert_shared_lock(deploy, before="nginx -t")
            self._assert_shared_lock(pre, before=marker)
            self._assert_shared_lock(post, before=marker)
            self.assertIn(marker, pre)
            self.assertIn(marker, post)
            self.assertLess(
                pre.index("printf '%s\\n' inactive"), pre.index("stop nginx")
            )
            self.assertLess(
                pre.index("printf '%s\\n' restart"), pre.index("stop nginx")
            )
            self.assertNotIn('rm -f "$marker"', pre)
            self.assertIn('if [ -f "$marker" ]; then', post)
            self.assertIn('marker_state="$(/usr/bin/cat -- "$marker")"', post)
            self.assertIn('rm -f "$marker"', post)
            self.assertLess(
                post.index("start nginx"), post.index('rm -f "$marker"')
            )
            runner.run.assert_called_once_with(
                ["systemctl", "enable", "--now", "certbot.timer"], timeout=120
            )
            self.assertTrue(inventory.features["certbot_renewal"])
            self.assertTrue(inventory.features["certbot_standalone"])

    def test_system_nginx_dns_renewal_has_no_stop_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, _, inventory, compose = self._fixture(
                temporary, authenticator="dns-cloudflare"
            )
            runner = mock.Mock(spec=Runner)

            with mock.patch(
                "remnawave_manager.certificates.command_exists", return_value=False
            ), mock.patch(
                "remnawave_manager.certificates._certbot_timer_state",
                return_value=("disabled", False),
            ):
                configure_adopted_certbot(
                    runner, inventory, compose, letsencrypt_root=root
                )

            hook_root = root / "renewal-hooks"
            self.assertTrue(
                (hook_root / "deploy/remnawave-manager-nginx").is_file()
            )
            self.assertFalse((hook_root / "pre/remnawave-manager-nginx").exists())
            self.assertFalse((hook_root / "post/remnawave-manager-nginx").exists())
            deploy = (
                hook_root / "deploy/remnawave-manager-nginx"
            ).read_text(encoding="utf-8")
            self._assert_shared_lock(deploy, before="nginx -t")
            self.assertFalse(inventory.features["certbot_standalone"])

    def test_system_nginx_requires_certificate_reference_in_owned_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, _, inventory, compose = self._fixture(
                temporary, reference_certificate=False
            )
            runner = mock.Mock(spec=Runner)

            with self.assertRaisesRegex(
                ValidationError, "подтверждённая системная конфигурация nginx"
            ):
                configure_adopted_certbot(
                    runner, inventory, compose, letsencrypt_root=root
                )

            self.assertFalse((root / "renewal-hooks").exists())
            runner.run.assert_not_called()

    def test_adoption_rejects_hardlinked_or_oversized_renewal_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, _, inventory, compose = self._fixture(temporary)
            renewal = root / "renewal/panel.example.com.conf"
            hardlink = root / "renewal/duplicate.conf"
            try:
                os.link(renewal, hardlink)
            except OSError as error:
                self.skipTest(f"hard links unavailable: {error}")
            runner = mock.Mock(spec=Runner)

            with self.assertRaisesRegex(ValidationError, "hardlink"):
                configure_adopted_certbot(
                    runner, inventory, compose, letsencrypt_root=root
                )

            runner.run.assert_not_called()
            hardlink.unlink()
            with (
                mock.patch(
                    "remnawave_manager.certificates._MAX_CERTBOT_TEXT_SIZE",
                    16,
                ),
                self.assertRaisesRegex(ValidationError, "превышает допустимый размер"),
            ):
                configure_adopted_certbot(
                    runner, inventory, compose, letsencrypt_root=root
                )

            runner.run.assert_not_called()

    def test_system_nginx_refuses_foreign_hook_without_partial_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root, _, inventory, compose = self._fixture(temporary)
            foreign = root / "renewal-hooks/deploy/remnawave-manager-nginx"
            foreign.parent.mkdir(parents=True)
            foreign.write_text("#!/bin/sh\nforeign-command\n", encoding="utf-8")
            runner = mock.Mock(spec=Runner)

            with self.assertRaisesRegex(ValidationError, "создан не менеджером"):
                configure_adopted_certbot(
                    runner, inventory, compose, letsencrypt_root=root
                )

            self.assertEqual(
                foreign.read_text(encoding="utf-8"),
                "#!/bin/sh\nforeign-command\n",
            )
            self.assertFalse(
                (root / "renewal-hooks/pre/remnawave-manager-nginx").exists()
            )
            runner.run.assert_not_called()

    def test_container_hook_backend_remains_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            hook_root = Path(temporary)

            install_renewal_hooks(
                nginx_container="remnawave-nginx",
                stop_for_standalone=True,
                hook_root=hook_root,
            )

            phase_scripts = {
                phase: (hook_root / phase / "remnawave-manager-nginx").read_text(
                    encoding="utf-8"
                )
                for phase in ("deploy", "pre", "post")
            }
            scripts = "\n".join(phase_scripts.values())
            docker = "/usr/bin/docker --host=unix:///run/docker.sock"
            self.assertIn(f"{docker} inspect", scripts)
            self.assertIn(f"{docker} exec remnawave-nginx nginx -t", scripts)
            self.assertIn(f"{docker} stop remnawave-nginx", scripts)
            self.assertIn(f"{docker} start remnawave-nginx", scripts)
            self.assertNotIn("/usr/bin/docker inspect", scripts)
            self.assertNotIn("|| true", scripts)
            self.assertNotIn("systemctl is-active --quiet nginx", scripts)
            marker = 'marker="/run/remnawave-manager-certbot-nginx-${PPID}"'
            for phase, action in (
                ("deploy", "nginx -t"),
                ("pre", marker),
                ("post", marker),
            ):
                self._assert_shared_lock(phase_scripts[phase], before=action)
            self.assertIn(marker, phase_scripts["pre"])
            self.assertIn(marker, phase_scripts["post"])
            self.assertLess(
                phase_scripts["pre"].index("printf '%s\\n' inactive"),
                phase_scripts["pre"].index("stop remnawave-nginx"),
            )
            self.assertLess(
                phase_scripts["pre"].index("printf '%s\\n' restart"),
                phase_scripts["pre"].index("stop remnawave-nginx"),
            )
            self.assertNotIn('rm -f "$marker"', phase_scripts["pre"])
            self.assertIn('if [ -f "$marker" ]; then', phase_scripts["post"])
            self.assertIn('rm -f "$marker"', phase_scripts["post"])

    def test_active_certbot_marker_blocks_manager_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker_root = Path(temporary)
            marker = marker_root / "remnawave-manager-certbot-nginx-4321"
            marker.write_text("restart\n", encoding="utf-8")

            with mock.patch(
                "remnawave_manager.certificates.os.kill"
            ) as process_exists, self.assertRaisesRegex(
                ValidationError, "Сейчас выполняется Certbot renewal"
            ):
                assert_no_active_certbot_renewal(marker_root=marker_root)

            process_exists.assert_called_once_with(4321, 0)

    def test_stale_certbot_marker_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker_root = Path(temporary)
            marker = marker_root / "remnawave-manager-certbot-nginx-4321"
            marker.write_text("restart\n", encoding="utf-8")

            with mock.patch(
                "remnawave_manager.certificates.os.kill",
                side_effect=ProcessLookupError,
            ), self.assertRaisesRegex(ValidationError, "stale marker"):
                assert_no_active_certbot_renewal(marker_root=marker_root)

    def test_malformed_certbot_marker_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker_root = Path(temporary)
            marker = marker_root / "remnawave-manager-certbot-nginx-unexpected"
            marker.write_text("restart\n", encoding="utf-8")

            with self.assertRaisesRegex(ValidationError, "неожиданным именем"):
                assert_no_active_certbot_renewal(marker_root=marker_root)

    def test_symlink_certbot_marker_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            marker_root = Path(temporary)
            target = marker_root / "target"
            target.write_text("restart\n", encoding="utf-8")
            marker = marker_root / "remnawave-manager-certbot-nginx-4321"
            try:
                marker.symlink_to(target)
            except OSError as error:
                self.skipTest(f"symlink недоступен в тестовой среде: {error}")

            with self.assertRaisesRegex(ValidationError, "небезопасный тип"):
                assert_no_active_certbot_renewal(marker_root=marker_root)


if __name__ == "__main__":
    unittest.main()
