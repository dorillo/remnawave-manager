from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remnawave_manager.errors import TransactionError, ValidationError
from remnawave_manager.paths import RuntimePaths
from remnawave_manager.registry import pull_verified, registry_login, registry_status
from remnawave_manager.runner import Runner
from remnawave_manager.state import StateStore


class RegistrySecurityTests(unittest.TestCase):
    def test_node_pull_accepts_current_verified_manifest_digest(self) -> None:
        digest = "sha256:0cdf386dd49f360fc885bb34bde21132e478e40f0deac62d616086ec0fa9257e"
        runner = mock.Mock(spec=Runner)
        runner.run.side_effect = [
            mock.Mock(returncode=0, stdout="", stderr=""),
            mock.Mock(
                returncode=0,
                stdout='["remnawave/node@' + digest + '"]',
                stderr="",
            ),
        ]

        image = pull_verified(runner, "node", "docker-hub")

        self.assertEqual(image, "remnawave/node:3.4.1@" + digest)

    def test_node_pull_rejects_unverified_manifest_digest(self) -> None:
        runner = mock.Mock(spec=Runner)
        runner.run.side_effect = [
            mock.Mock(returncode=0, stdout="", stderr=""),
            mock.Mock(
                returncode=0,
                stdout='["remnawave/node@sha256:' + "f" * 64 + '"]',
                stderr="",
            ),
        ]

        with self.assertRaisesRegex(TransactionError, "Digest remnawave/node:3.4.1"):
            pull_verified(runner, "node", "docker-hub")

    def test_pull_error_sanitizes_external_registry_output(self) -> None:
        runner = mock.Mock(spec=Runner)
        runner.run.return_value = mock.Mock(
            returncode=1, stdout="", stderr="failed\x1b[31m\rspoof\u202ehidden"
        )

        with (
            mock.patch(
                "remnawave_manager.registry.component_target",
                return_value={"image": "example/image"},
            ),
            self.assertRaisesRegex(
                TransactionError, r"failed \[31m spoof hidden"
            ) as raised,
        ):
            pull_verified(runner, "panel", "docker-hub")

        message = str(raised.exception)
        self.assertNotIn("\x1b", message)
        self.assertNotIn("\r", message)
        self.assertNotIn("\u202e", message)

    def test_login_rejects_control_characters_before_docker(self) -> None:
        runner = mock.Mock(spec=Runner)

        with self.assertRaises(ValidationError):
            registry_login(
                runner,
                "docker-hub",
                username="user\n--password secret",
                password="valid-password",
            )
        with self.assertRaises(ValidationError):
            registry_login(
                runner,
                "docker-hub",
                username="valid-user",
                password="first-line\nsecond-line",
            )
        with self.assertRaises(ValidationError):
            registry_login(
                runner,
                "docker-hub",
                username="valid-user\u202e",
                password="valid-password",
            )
        with self.assertRaises(ValidationError):
            registry_login(
                runner,
                "docker-hub",
                username="valid-user",
                password="hidden\u202etoken",
            )

        runner.run.assert_not_called()

    def test_status_rejects_non_string_registry_setting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(RuntimePaths(Path(temporary)))
            with (
                mock.patch.object(
                    store,
                    "load_settings",
                    return_value={"registry": ["docker-hub"]},
                ),
                self.assertRaisesRegex(ValidationError, "неизвестный Docker Registry"),
            ):
                registry_status(store)

    def test_status_ignores_malformed_docker_auths_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(RuntimePaths(Path(temporary)))
            with (
                mock.patch(
                    "remnawave_manager.registry.docker_config_directory",
                    return_value=Path(temporary) / ".docker",
                ),
                mock.patch.object(
                    store, "load_settings", return_value={"registry": "docker-hub"}
                ),
            ):
                docker = Path(temporary) / ".docker"
                docker.mkdir()
                (docker / "config.json").write_text(
                    '{"auths": ["docker.io"]}\n', encoding="utf-8"
                )
                status = registry_status(store)

            self.assertEqual(status["authenticated_hosts"], [])

    def test_status_reads_bounded_private_regular_docker_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            docker = root / ".docker"
            docker.mkdir()
            config = docker / "config.json"
            config.write_text(
                '{"auths": {"docker.io": {}, "bad\\u202ehost": {}}}\n',
                encoding="utf-8",
            )
            if os.name == "posix":
                config.chmod(0o600)
            store = StateStore(RuntimePaths(root))
            with (
                mock.patch(
                    "remnawave_manager.registry.docker_config_directory",
                    return_value=docker,
                ),
                mock.patch.object(
                    store,
                    "load_settings",
                    return_value={"registry": "docker-hub"},
                ),
            ):
                status = registry_status(store)

            self.assertEqual(status["authenticated_hosts"], ["docker.io"])

    def test_status_ignores_symlinked_docker_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            docker = root / ".docker"
            docker.mkdir()
            config = docker / "config.json"
            target = docker / "untrusted.json"
            target.write_text('{"auths": {"docker.io": {}}}\n', encoding="utf-8")
            if os.name == "posix":
                target.chmod(0o600)
            try:
                config.symlink_to(target)
            except OSError as error:
                self.skipTest(f"symbolic links unavailable: {error}")
            store = StateStore(RuntimePaths(root))
            with (
                mock.patch(
                    "remnawave_manager.registry.docker_config_directory",
                    return_value=docker,
                ),
                mock.patch.object(
                    store,
                    "load_settings",
                    return_value={"registry": "docker-hub"},
                ),
            ):
                status = registry_status(store)

            self.assertEqual(status["authenticated_hosts"], [])

    def test_status_ignores_hardlinked_docker_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            docker = root / ".docker"
            docker.mkdir()
            config = docker / "config.json"
            config.write_text('{"auths": {"docker.io": {}}}\n', encoding="utf-8")
            if os.name == "posix":
                config.chmod(0o600)
            try:
                os.link(config, docker / "config-hardlink.json")
            except OSError as error:
                self.skipTest(f"hard links unavailable: {error}")
            store = StateStore(RuntimePaths(root))
            with (
                mock.patch(
                    "remnawave_manager.registry.docker_config_directory",
                    return_value=docker,
                ),
                mock.patch.object(
                    store,
                    "load_settings",
                    return_value={"registry": "docker-hub"},
                ),
            ):
                status = registry_status(store)

            self.assertEqual(status["authenticated_hosts"], [])

    def test_status_ignores_oversized_docker_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            docker = root / ".docker"
            docker.mkdir()
            (docker / "config.json").write_text(
                "x" * (1024 * 1024 + 1), encoding="utf-8"
            )
            store = StateStore(RuntimePaths(root))
            with (
                mock.patch(
                    "remnawave_manager.registry.docker_config_directory",
                    return_value=docker,
                ),
                mock.patch.object(
                    store,
                    "load_settings",
                    return_value={"registry": "docker-hub"},
                ),
            ):
                status = registry_status(store)

            self.assertEqual(status["authenticated_hosts"], [])


if __name__ == "__main__":
    unittest.main()
