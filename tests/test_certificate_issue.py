from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remnawave_manager.certificates import (
    CertificateSpec,
    CertificateTransaction,
    _restore_certbot_timer,
    _validate_certificate,
    build_certbot_command,
    issue_certificate,
    obtain_certificate,
)
from remnawave_manager.errors import TransactionError, ValidationError
from remnawave_manager.models import Component, Inventory
from remnawave_manager.runner import Result, Runner


def inventory(*, system_nginx: bool = False) -> Inventory:
    components = {}
    if not system_nginx:
        components["nginx"] = Component(
            "nginx", "remnawave-nginx", container="remnawave-nginx"
        )
    return Inventory(
        schema_version=1,
        role="node",
        install_dir="/opt/remnanode",
        compose_file="/opt/remnanode/docker-compose.yml",
        env_file="/opt/remnanode/.env",
        webserver="nginx",
        components=components,
    )


def result(args: list[str], *, stdout: str = "", returncode: int = 0) -> Result:
    if args == ["systemctl", "is-enabled", "certbot.timer"] and not stdout:
        stdout = "disabled\n"
        returncode = 1
    elif args == ["systemctl", "is-active", "certbot.timer"] and not stdout:
        stdout = "inactive\n"
        returncode = 3
    return Result(tuple(args), returncode, stdout, "")


class CertificateIssueTests(unittest.TestCase):
    def test_transaction_rollback_continues_after_second_keyboard_interrupt(
        self,
    ) -> None:
        transaction = CertificateTransaction(
            certificate_name="node.example.com",
            letsencrypt_root=Path("/etc/letsencrypt"),
            hooks={},
            credentials=Path("/etc/remnawave-manager/certbot/test.ini"),
            credential_snapshot=None,
            credentials_root=None,
            credentials_root_created=False,
            timer_enablement="disabled",
            timer_active=False,
            delete_lineage_on_rollback=False,
        )
        runner = mock.Mock(spec=Runner)

        with (
            mock.patch(
                "remnawave_manager.certificates._restore_hooks",
                side_effect=KeyboardInterrupt,
            ),
            mock.patch(
                "remnawave_manager.certificates._restore_credentials"
            ) as restore_credentials,
            mock.patch(
                "remnawave_manager.certificates._restore_certbot_timer"
            ) as restore_timer,
            self.assertRaisesRegex(TransactionError, "Certbot hooks"),
        ):
            transaction.rollback(runner)

        restore_credentials.assert_called_once()
        restore_timer.assert_called_once_with(
            runner,
            enablement="disabled",
            active=False,
        )

    def test_certbot_timer_restores_runtime_enablement_exactly(self) -> None:
        state = {"enablement": "enabled", "active": True}
        runner = mock.Mock(spec=Runner)

        def run(args, **_kwargs):  # type: ignore[no-untyped-def]
            values = list(args)
            if values[:2] == ["systemctl", "is-enabled"]:
                enabled = str(state["enablement"])
                return result(
                    values,
                    stdout=enabled + "\n",
                    returncode=0 if enabled.startswith("enabled") else 1,
                )
            if values[:2] == ["systemctl", "is-active"]:
                return result(
                    values,
                    stdout="active\n" if state["active"] else "inactive\n",
                    returncode=0 if state["active"] else 3,
                )
            if values[:2] == ["systemctl", "disable"]:
                state["enablement"] = "disabled"
            elif values[:3] == ["systemctl", "enable", "--runtime"]:
                state["enablement"] = "enabled-runtime"
            elif values[:2] == ["systemctl", "start"]:
                state["active"] = True
            elif values[:2] == ["systemctl", "stop"]:
                state["active"] = False
            return result(values)

        runner.run.side_effect = run

        _restore_certbot_timer(
            runner,
            enablement="enabled-runtime",
            active=False,
        )

        self.assertEqual(
            state,
            {"enablement": "enabled-runtime", "active": False},
        )
        commands = [call.args[0] for call in runner.run.call_args_list]
        self.assertIn(["systemctl", "disable", "certbot.timer"], commands)
        self.assertIn(
            ["systemctl", "enable", "--runtime", "certbot.timer"],
            commands,
        )

    def test_certbot_timer_restores_masked_active_state_exactly(self) -> None:
        state = {"enablement": "enabled", "active": False}
        runner = mock.Mock(spec=Runner)

        def run(args, **_kwargs):  # type: ignore[no-untyped-def]
            values = list(args)
            if values[:2] == ["systemctl", "is-enabled"]:
                enabled = str(state["enablement"])
                return result(
                    values,
                    stdout=enabled + "\n",
                    returncode=0 if enabled.startswith("enabled") else 1,
                )
            if values[:2] == ["systemctl", "is-active"]:
                return result(
                    values,
                    stdout="active\n" if state["active"] else "inactive\n",
                    returncode=0 if state["active"] else 3,
                )
            if values[:2] in (
                ["systemctl", "unmask"],
                ["systemctl", "disable"],
            ):
                state["enablement"] = "disabled"
            elif values[:2] == ["systemctl", "start"]:
                state["active"] = True
            elif values[:2] == ["systemctl", "mask"]:
                state["enablement"] = (
                    "masked-runtime" if "--runtime" in values else "masked"
                )
            return result(values)

        runner.run.side_effect = run

        _restore_certbot_timer(
            runner,
            enablement="masked-runtime",
            active=True,
        )

        self.assertEqual(
            state,
            {"enablement": "masked-runtime", "active": True},
        )

    def test_certbot_timer_rollback_continues_after_repeated_interrupts(self) -> None:
        calls: list[list[str]] = []
        runner = mock.Mock(spec=Runner)

        def run(args, **_kwargs):  # type: ignore[no-untyped-def]
            values = list(args)
            calls.append(values)
            if tuple(values[:2]) in {
                ("systemctl", "stop"),
                ("systemctl", "disable"),
            }:
                raise KeyboardInterrupt("interrupted compensation")
            if values == ["systemctl", "is-enabled", "certbot.timer"]:
                return result(values, stdout="enabled\n")
            if values == ["systemctl", "is-active", "certbot.timer"]:
                return result(values, stdout="active\n")
            return result(values)

        runner.run.side_effect = run

        with self.assertRaisesRegex(TransactionError, "active-state.*enablement cleanup"):
            _restore_certbot_timer(
                runner,
                enablement="disabled",
                active=False,
            )

        self.assertIn(["systemctl", "stop", "certbot.timer"], calls)
        self.assertIn(["systemctl", "disable", "certbot.timer"], calls)
        self.assertIn(["systemctl", "is-enabled", "certbot.timer"], calls)
        self.assertIn(["systemctl", "is-active", "certbot.timer"], calls)

    def test_wildcard_certificate_is_checked_with_concrete_probe_hostname(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fullchain = root / "fullchain.pem"
            private_key = root / "privkey.pem"
            fullchain.write_text("certificate", encoding="utf-8")
            private_key.write_text("private key", encoding="utf-8")
            runner = mock.Mock(spec=Runner)

            def run(args, **_kwargs):  # type: ignore[no-untyped-def]
                values = list(args)
                output = "PUBLIC KEY\n" if "-pubkey" in values or "-pubout" in values else ""
                return result(values, stdout=output)

            runner.run.side_effect = run
            _validate_certificate(
                runner,
                fullchain,
                private_key,
                ("example.com", "*.example.com"),
            )

            check_hosts = [
                call.args[0][-1]
                for call in runner.run.call_args_list
                if "-checkhost" in call.args[0]
            ]
            self.assertEqual(
                check_hosts,
                ["example.com", "rwm-wildcard-check.example.com"],
            )

    def test_clean_install_uses_stable_credentials_and_rolls_back_all_mutations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            letsencrypt = root / "letsencrypt"
            letsencrypt.mkdir()
            credentials = root / "etc/remnawave-manager/certbot"
            credentials.parent.mkdir(parents=True)
            install_dir = root / "opt/remnawave"
            install_dir.mkdir(parents=True)
            runner = mock.Mock(spec=Runner)

            def run(args, **_kwargs):  # type: ignore[no-untyped-def]
                values = list(args)
                if values == ["certbot", "plugins"]:
                    return result(values, stdout="  * dns-cloudflare\n")
                if values == ["systemctl", "is-enabled", "certbot.timer"]:
                    return result(values, stdout="disabled\n", returncode=1)
                if values == ["systemctl", "is-active", "certbot.timer"]:
                    return result(values, stdout="inactive\n", returncode=3)
                if values[:2] == ["certbot", "certonly"]:
                    (letsencrypt / "live/panel.example.com").mkdir(parents=True)
                    (letsencrypt / "archive/panel.example.com").mkdir(parents=True)
                    renewal = letsencrypt / "renewal/panel.example.com.conf"
                    renewal.parent.mkdir(parents=True)
                    renewal.write_text(
                        "[renewalparams]\nauthenticator = dns-cloudflare\n",
                        encoding="utf-8",
                    )
                if values[:2] == ["certbot", "delete"]:
                    shutil.rmtree(letsencrypt / "live/panel.example.com")
                    shutil.rmtree(letsencrypt / "archive/panel.example.com")
                    (letsencrypt / "renewal/panel.example.com.conf").unlink()
                return result(values)

            runner.run.side_effect = run
            with (
                mock.patch("remnawave_manager.certificates.command_exists", return_value=True),
                mock.patch("remnawave_manager.certificates._validate_certificate"),
            ):
                material = obtain_certificate(
                    runner,
                    ["panel.example.com", "sub.example.com"],
                    CertificateSpec(
                        method="cloudflare",
                        email="admin@example.com",
                        cloudflare_token="cloudflare-token-that-is-long-enough",
                    ),
                    install_dir=install_dir,
                    credentials_dir=credentials,
                    letsencrypt_root=letsencrypt,
                )

            credential = credentials / "cloudflare-panel.example.com.ini"
            self.assertEqual(material.credentials_file, credential)
            self.assertTrue(credential.is_file())
            certonly = next(
                call.args[0]
                for call in runner.run.call_args_list
                if call.args[0][:2] == ["certbot", "certonly"]
            )
            self.assertIn(str(credential), certonly)
            self.assertNotIn(str(install_dir / ".cloudflare.ini"), certonly)

            material.rollback(runner)

            self.assertFalse(credentials.exists())
            self.assertFalse((letsencrypt / "live/panel.example.com").exists())
            self.assertFalse(
                (letsencrypt / "renewal-hooks/deploy/remnawave-manager-nginx").exists()
            )
            runner.run.assert_any_call(
                ["systemctl", "disable", "certbot.timer"], timeout=120
            )
            runner.run.assert_any_call(
                ["systemctl", "stop", "certbot.timer"], timeout=120
            )

    def test_clean_install_refuses_existing_lineage_before_commands_or_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            letsencrypt = root / "letsencrypt"
            (letsencrypt / "renewal").mkdir(parents=True)
            (letsencrypt / "renewal/panel.example.com.conf").write_text(
                "[renewalparams]\nauthenticator = standalone\n",
                encoding="utf-8",
            )
            credentials = root / "etc/remnawave-manager/certbot"
            credentials.parent.mkdir(parents=True)
            runner = mock.Mock(spec=Runner)

            with self.assertRaisesRegex(ValidationError, "смена provider запрещена"):
                obtain_certificate(
                    runner,
                    ["panel.example.com"],
                    CertificateSpec(
                        method="cloudflare",
                        email="admin@example.com",
                        cloudflare_token="cloudflare-token-that-is-long-enough",
                    ),
                    install_dir=root / "install",
                    credentials_dir=credentials,
                    letsencrypt_root=letsencrypt,
                )

            runner.run.assert_not_called()
            self.assertFalse(credentials.exists())

    def test_clean_install_reuses_only_matching_lineage_and_does_not_delete_it_on_rollback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            letsencrypt = root / "letsencrypt"
            live = letsencrypt / "live/panel.example.com"
            live.mkdir(parents=True)
            (live / "fullchain.pem").write_text("certificate", encoding="utf-8")
            (live / "privkey.pem").write_text("key", encoding="utf-8")
            renewal = letsencrypt / "renewal/panel.example.com.conf"
            renewal.parent.mkdir(parents=True)
            renewal.write_text(
                "[renewalparams]\nauthenticator = standalone\n",
                encoding="utf-8",
            )
            runner = mock.Mock(spec=Runner)

            def run(args, **_kwargs):  # type: ignore[no-untyped-def]
                values = list(args)
                if values[:2] == ["openssl", "x509"]:
                    return result(
                        values,
                        stdout="X509v3 Subject Alternative Name:\n DNS:panel.example.com, DNS:sub.example.com\n",
                    )
                if values == ["systemctl", "is-enabled", "certbot.timer"]:
                    return result(values, stdout="disabled\n", returncode=1)
                if values == ["systemctl", "is-active", "certbot.timer"]:
                    return result(values, stdout="inactive\n", returncode=3)
                return result(values)

            runner.run.side_effect = run
            with (
                mock.patch("remnawave_manager.certificates.command_exists", return_value=True),
                mock.patch("remnawave_manager.certificates._validate_certificate"),
            ):
                material = obtain_certificate(
                    runner,
                    ["panel.example.com", "sub.example.com"],
                    CertificateSpec(method="http-01", email="admin@example.com"),
                    install_dir=root / "install",
                    letsencrypt_root=letsencrypt,
                )

            commands = [call.args[0] for call in runner.run.call_args_list]
            self.assertFalse(any(command[:2] == ["certbot", "certonly"] for command in commands))

            material.rollback(runner)

            self.assertTrue(live.is_dir())
            self.assertTrue(renewal.is_file())
            self.assertFalse(
                (letsencrypt / "renewal-hooks/deploy/remnawave-manager-nginx").exists()
            )

    def test_gcore_command_uses_credentials_without_exposing_token(self) -> None:
        token = "gcore-token-that-must-stay-secret"
        command = build_certbot_command(
            ["example.com", "*.example.com"],
            CertificateSpec(
                method="gcore",
                email="admin@example.com",
                gcore_token=token,
            ),
            credentials_file=Path("/etc/remnawave-manager/certbot/gcore-example.com.ini"),
        )

        self.assertIn("dns-gcore", command)
        self.assertIn("--dns-gcore-credentials", command)
        self.assertIn("--dns-gcore-propagation-seconds", command)
        self.assertEqual(command.count("--domain"), 2)
        self.assertNotIn(token, " ".join(command))

    def test_gcore_issue_writes_owned_credentials_and_renewal_hooks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            letsencrypt = root / "letsencrypt"
            letsencrypt.mkdir()
            credentials = root / "credentials"
            runner = mock.Mock(spec=Runner)

            def run(args, **_kwargs):  # type: ignore[no-untyped-def]
                values = list(args)
                if values == ["certbot", "plugins"]:
                    return result(values, stdout="  * dns-gcore\n")
                return result(values)

            runner.run.side_effect = run
            token = "gcore-token-that-must-stay-secret"
            with (
                mock.patch("remnawave_manager.certificates.command_exists", return_value=True),
                mock.patch("remnawave_manager.certificates._validate_certificate"),
                mock.patch(
                    "remnawave_manager.certificates._renewal_uses_standalone",
                    return_value=False,
                ),
            ):
                issued = issue_certificate(
                    runner,
                    inventory(),
                    "Example.COM.",
                    CertificateSpec(
                        method="gcore",
                        email="admin@example.com",
                        gcore_token=token,
                    ),
                    wildcard=True,
                    credentials_dir=credentials,
                    letsencrypt_root=letsencrypt,
                )

            self.assertEqual(issued.certificate_name, "example.com")
            self.assertEqual(issued.domains, ("example.com", "*.example.com"))
            credential = credentials / "gcore-example.com.ini"
            payload = credential.read_text(encoding="utf-8")
            self.assertIn("# Managed by remnawave-manager", payload)
            self.assertIn("dns_gcore_apitoken = " + token, payload)
            if os.name == "posix":
                self.assertEqual(credential.stat().st_mode & 0o777, 0o600)
            deploy = letsencrypt / "renewal-hooks/deploy/remnawave-manager-nginx"
            self.assertIn(
                "/usr/bin/docker --host=unix:///run/docker.sock "
                "exec remnawave-nginx nginx -t",
                deploy.read_text(),
            )
            self.assertFalse(
                (letsencrypt / "renewal-hooks/pre/remnawave-manager-nginx").exists()
            )
            runner.run.assert_any_call(
                ["systemctl", "enable", "--now", "certbot.timer"], timeout=120
            )
            commands = [call.args[0] for call in runner.run.call_args_list]
            certonly = next(command for command in commands if command[:2] == ["certbot", "certonly"])
            self.assertNotIn(token, " ".join(certonly))

    def test_missing_gcore_plugin_fails_before_credentials_are_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            letsencrypt = root / "letsencrypt"
            letsencrypt.mkdir()
            credentials = root / "credentials"
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = result(
                ["certbot", "plugins"],
                stdout="  * standalone\n",
            )

            with mock.patch(
                "remnawave_manager.certificates.command_exists",
                return_value=True,
            ), self.assertRaisesRegex(
                ValidationError,
                r"Повторно запустите корневой install\.sh",
            ):
                issue_certificate(
                    runner,
                    inventory(),
                    "example.com",
                    CertificateSpec(
                        method="gcore",
                        email="admin@example.com",
                        gcore_token="gcore-token-that-must-stay-secret",
                    ),
                    credentials_dir=credentials,
                    letsencrypt_root=letsencrypt,
                )

            self.assertFalse(credentials.exists())
            runner.run.assert_called_once_with(
                ["certbot", "plugins"],
                check=False,
                timeout=60,
            )

    def test_http01_issue_stops_and_restores_only_active_system_nginx(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            letsencrypt = root / "letsencrypt"
            letsencrypt.mkdir()
            runner = mock.Mock(spec=Runner)

            def run(args, **_kwargs):  # type: ignore[no-untyped-def]
                values = list(args)
                if values == ["systemctl", "is-active", "--quiet", "nginx"]:
                    return result(values)
                return result(values)

            runner.run.side_effect = run
            with (
                mock.patch("remnawave_manager.certificates.command_exists", return_value=True),
                mock.patch("remnawave_manager.certificates._validate_certificate"),
                mock.patch(
                    "remnawave_manager.certificates._renewal_uses_standalone",
                    return_value=True,
                ),
            ):
                issue_certificate(
                    runner,
                    inventory(system_nginx=True),
                    "node.example.com",
                    CertificateSpec(method="http-01", email="admin@example.com"),
                    letsencrypt_root=letsencrypt,
                )

            commands = [call.args[0] for call in runner.run.call_args_list]
            stop = commands.index(["systemctl", "stop", "nginx"])
            certonly = next(
                index
                for index, command in enumerate(commands)
                if command[:2] == ["certbot", "certonly"]
            )
            start = commands.index(["systemctl", "start", "nginx"])
            self.assertLess(stop, certonly)
            self.assertLess(certonly, start)
            post = letsencrypt / "renewal-hooks/post/remnawave-manager-nginx"
            post_text = post.read_text(encoding="utf-8")
            self.assertLess(post_text.index("start nginx"), post_text.index("rm -f"))

    def test_issue_rejects_wildcard_http_and_existing_lineage_without_commands(self) -> None:
        runner = mock.Mock(spec=Runner)
        with self.assertRaisesRegex(ValidationError, "Wildcard"):
            issue_certificate(
                runner,
                inventory(),
                "example.com",
                CertificateSpec(method="http-01", email="admin@example.com"),
                wildcard=True,
            )
        runner.run.assert_not_called()

        with tempfile.TemporaryDirectory() as temporary:
            letsencrypt = Path(temporary)
            (letsencrypt / "live/example.com").mkdir(parents=True)
            with self.assertRaisesRegex(ValidationError, "уже существует"):
                issue_certificate(
                    runner,
                    inventory(),
                    "example.com",
                    CertificateSpec(method="http-01", email="admin@example.com"),
                    letsencrypt_root=letsencrypt,
                )
        runner.run.assert_not_called()

    def test_failed_validation_restores_hooks_credentials_and_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            letsencrypt = root / "letsencrypt"
            letsencrypt.mkdir()
            credentials = root / "credentials"
            hook_contents: dict[Path, bytes] = {}
            for phase in ("deploy", "pre", "post"):
                hook = letsencrypt / "renewal-hooks" / phase / "remnawave-manager-nginx"
                hook.parent.mkdir(parents=True, exist_ok=True)
                hook.write_text(
                    f"#!/bin/sh\n# Managed by remnawave-manager\nold-{phase}\n",
                    encoding="utf-8",
                )
                hook_contents[hook] = hook.read_bytes()
            runner = mock.Mock(spec=Runner)

            def run(args, **_kwargs):  # type: ignore[no-untyped-def]
                values = list(args)
                if values == ["certbot", "plugins"]:
                    return result(values, stdout="  * dns-cloudflare\n")
                if values[:2] == ["certbot", "certonly"]:
                    (letsencrypt / "live/example.com").mkdir(parents=True)
                    (letsencrypt / "archive/example.com").mkdir(parents=True)
                    renewal = letsencrypt / "renewal/example.com.conf"
                    renewal.parent.mkdir(parents=True)
                    renewal.write_text(
                        "[renewalparams]\nauthenticator = dns-cloudflare\n",
                        encoding="utf-8",
                    )
                if values[:2] == ["certbot", "delete"]:
                    shutil.rmtree(letsencrypt / "live/example.com")
                    shutil.rmtree(letsencrypt / "archive/example.com")
                    (letsencrypt / "renewal/example.com.conf").unlink()
                return result(values)

            runner.run.side_effect = run
            with (  # noqa: SIM117 - inner assertion context keeps failure scope explicit
                mock.patch("remnawave_manager.certificates.command_exists", return_value=True),
                mock.patch(
                    "remnawave_manager.certificates._validate_certificate",
                    side_effect=ValidationError("проверка сертификата не пройдена"),
                ),
            ):
                with self.assertRaisesRegex(ValidationError, "проверка сертификата"):
                    issue_certificate(
                        runner,
                        inventory(),
                        "example.com",
                        CertificateSpec(
                            method="cloudflare",
                            email="admin@example.com",
                            cloudflare_token="cloudflare-token-that-is-long-enough",
                        ),
                        credentials_dir=credentials,
                        letsencrypt_root=letsencrypt,
                    )

            for hook, payload in hook_contents.items():
                self.assertEqual(hook.read_bytes(), payload)
            self.assertFalse(credentials.exists())
            self.assertFalse((letsencrypt / "live/example.com").exists())
            self.assertFalse((letsencrypt / "archive/example.com").exists())
            self.assertFalse((letsencrypt / "renewal/example.com.conf").exists())

    def test_rollback_rejects_certbot_delete_that_leaves_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            letsencrypt = root / "letsencrypt"
            letsencrypt.mkdir()
            runner = mock.Mock(spec=Runner)

            def run(args, **_kwargs):  # type: ignore[no-untyped-def]
                values = list(args)
                if values == ["certbot", "plugins"]:
                    return result(values, stdout="  * dns-cloudflare\n")
                if values[:2] == ["certbot", "certonly"]:
                    (letsencrypt / "live/example.com").mkdir(parents=True)
                return result(values)

            runner.run.side_effect = run
            with (  # noqa: SIM117 - inner assertion context keeps failure scope explicit
                mock.patch("remnawave_manager.certificates.command_exists", return_value=True),
                mock.patch(
                    "remnawave_manager.certificates._validate_certificate",
                    side_effect=ValidationError("invalid"),
                ),
            ):
                with self.assertRaisesRegex(TransactionError, "lineage остался"):
                    issue_certificate(
                        runner,
                        inventory(),
                        "example.com",
                        CertificateSpec(
                            method="cloudflare",
                            email="admin@example.com",
                            cloudflare_token="cloudflare-token-that-is-long-enough",
                        ),
                        credentials_dir=root / "credentials",
                        letsencrypt_root=letsencrypt,
                    )

            credential = root / "credentials/cloudflare-example.com.ini"
            self.assertTrue(credential.is_file())
            self.assertIn(
                "cloudflare-token-that-is-long-enough",
                credential.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
