from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remnawave_manager.errors import CommandError, ValidationError
from remnawave_manager.paths import RuntimePaths
from remnawave_manager.runner import (
    Runner,
    atomic_copy,
    atomic_write_bytes,
    command_exists,
    exclusive_lock,
    sanitize_external_text,
    sha256_file,
)
from remnawave_manager.state import StateStore


class RunnerSecurityTests(unittest.TestCase):
    def test_sanitizer_bounds_and_removes_terminal_and_bidi_controls(self) -> None:
        value = "prefix\x1b[31m\rspoof\u202ehidden\x00" + ("x" * 5000)

        sanitized = sanitize_external_text(value, limit=64)

        self.assertLessEqual(len(sanitized), 64)
        self.assertNotIn("\x1b", sanitized)
        self.assertNotIn("\r", sanitized)
        self.assertNotIn("\u202e", sanitized)
        self.assertNotIn("\x00", sanitized)

    def test_dependency_lookup_does_not_use_untrusted_posix_path(self) -> None:
        with mock.patch("remnawave_manager.runner.shutil.which") as which:
            command_exists("docker")

        if os.name == "posix":
            self.assertNotIn("/tmp", which.call_args.kwargs["path"])
        else:
            self.assertIsNone(which.call_args.kwargs["path"])

    def test_external_commands_receive_sanitized_environment(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        environment = {
            "PATH": "/untrusted/bin",
            "HOME": "/untrusted/home",
            "HTTP_PROXY": "http://proxy.example:8080",
            "DOCKER_CONFIG": "/root/.docker-custom",
            "DOCKER_CONTEXT": "remote",
            "COMPOSE_PROJECT_NAME": "unrelated-production-stack",
            "LD_PRELOAD": "/tmp/injected.so",
            "PYTHONPATH": "/tmp/injected-python",
            "RWM_REGISTRY_PASSWORD": "must-not-leak",
            "REMNAWAVE_API_TOKEN": "panel-token-must-not-leak",
            "WGCF_LICENSE_KEY": "warp-license-must-not-leak",
            "POSTGRES_PASSWORD": "shell-must-not-override-compose-env-file",
        }
        with (
            mock.patch.dict(os.environ, environment, clear=True),
            mock.patch(
                "remnawave_manager.runner.subprocess.run", return_value=completed
            ) as run,
        ):
            Runner().run(["true"])
            inherited_child = run.call_args.kwargs["env"]
            Runner().run(["docker", "compose", "version"])

        compose_child = run.call_args.kwargs["env"]
        self.assertEqual(
            inherited_child["HTTP_PROXY"], environment["HTTP_PROXY"]
        )
        for key in (
            "DOCKER_CONTEXT",
            "COMPOSE_PROJECT_NAME",
            "LD_PRELOAD",
            "PYTHONPATH",
            "RWM_REGISTRY_PASSWORD",
            "REMNAWAVE_API_TOKEN",
            "WGCF_LICENSE_KEY",
        ):
            self.assertNotIn(key, inherited_child)
        self.assertNotIn("HTTP_PROXY", compose_child)
        self.assertNotIn("POSTGRES_PASSWORD", compose_child)
        if os.name == "posix":
            self.assertNotEqual(inherited_child["PATH"], environment["PATH"])
            self.assertNotEqual(inherited_child["HOME"], environment["HOME"])
            self.assertNotEqual(
                inherited_child["DOCKER_CONFIG"], environment["DOCKER_CONFIG"]
            )

    def test_compose_rejects_explicit_process_environment(self) -> None:
        with (
            mock.patch("remnawave_manager.runner.subprocess.run") as spawn,
            self.assertRaisesRegex(ValidationError, "--env-file"),
        ):
            Runner().run(
                ["docker", "compose", "version"], env={"IMAGE": "untrusted"}
            )
        spawn.assert_not_called()

    def test_compose_stateful_command_requires_explicit_file(self) -> None:
        with (
            mock.patch("remnawave_manager.runner.subprocess.run") as spawn,
            self.assertRaisesRegex(ValidationError, "явный абсолютный Compose-файл"),
        ):
            Runner().run(["docker", "compose", "down"])
        spawn.assert_not_called()

    def test_compose_input_paths_accept_safe_absolute_files(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            compose = root / "docker-compose.yml"
            env_file = root / ".env"
            compose.write_text("services: {}\n", encoding="utf-8")
            env_file.write_text("APP_PORT=3000\n", encoding="utf-8")
            if os.name == "posix":
                compose.chmod(0o600)
                env_file.chmod(0o600)
            with mock.patch(
                "remnawave_manager.runner.subprocess.run", return_value=completed
            ):
                Runner().run(
                    [
                        "docker",
                        "compose",
                        "--env-file",
                        str(env_file),
                        "-f",
                        str(compose),
                        "config",
                        "-q",
                    ]
                )

    def test_compose_internal_env_and_bind_sources_are_validated_and_allowed(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env_file = root / ".env"
            config = root / "nginx.conf"
            site = root / "site"
            site.mkdir()
            env_file.write_text("APP_PORT=3000\n", encoding="utf-8")
            config.write_text("events {}\n", encoding="utf-8")
            compose = root / "docker-compose.yml"
            compose.write_text(
                "services:\n"
                "  app:\n"
                "    image: example/app:1\n"
                "    env_file:\n"
                "      - .env\n"
                "    volumes:\n"
                "      - ./site:/srv/site:ro\n"
                "      - type: bind\n"
                "        source: ./nginx.conf\n"
                "        target: /etc/nginx/nginx.conf\n"
                "      - app-data:/var/lib/app\n"
                "volumes:\n"
                "  app-data: {}\n",
                encoding="utf-8",
            )
            if os.name == "posix":
                for path in (env_file, config, compose):
                    path.chmod(0o600)
                site.chmod(0o700)
            with mock.patch(
                "remnawave_manager.runner.subprocess.run", return_value=completed
            ) as spawn:
                Runner().run(
                    ["docker", "compose", "-f", str(compose), "config", "-q"],
                    cwd=root,
                )

            spawn.assert_called_once()

    def test_compose_allows_only_matching_certbot_live_symlink_bind(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
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
            compose = root / "docker-compose.yml"
            compose.write_text(
                "services:\n"
                "  app:\n"
                "    image: example/app:1\n"
                "    volumes:\n"
                "      - type: bind\n"
                f"        source: {certificate}\n"
                "        target: /etc/tls/fullchain.pem\n",
                encoding="utf-8",
            )
            if os.name == "posix":
                compose.chmod(0o600)
                archived_certificate.chmod(0o600)
            with (
                mock.patch("remnawave_manager.runner._LETSENCRYPT_ROOT", letsencrypt),
                mock.patch(
                    "remnawave_manager.runner.subprocess.run", return_value=completed
                ) as spawn,
            ):
                Runner().run(
                    ["docker", "compose", "-f", str(compose), "config", "-q"]
                )
            spawn.assert_called_once()

            outside = root / "outside.pem"
            outside.write_text("untrusted target\n", encoding="utf-8")
            certificate.unlink()
            certificate.symlink_to(outside)
            with (
                mock.patch("remnawave_manager.runner._LETSENCRYPT_ROOT", letsencrypt),
                mock.patch("remnawave_manager.runner.subprocess.run") as spawn,
                self.assertRaisesRegex(ValidationError, "Certbot live-ссылка"),
            ):
                Runner().run(
                    ["docker", "compose", "-f", str(compose), "config", "-q"]
                )
            spawn.assert_not_called()

    def test_compose_rejects_executable_or_external_reference_features(self) -> None:
        payloads = {
            "build": "services:\n  app:\n    build: .\n",
            "devices": "services:\n  app:\n    devices:\n      - /dev/net/tun:/dev/net/tun\n",
            "driver_opts": "volumes:\n  data:\n    driver_opts:\n      device: /tmp/data\nservices: {}\n",
            "extends": "services:\n  app:\n    extends:\n      file: other.yml\n      service: app\n",
            "include": "include:\n  - other.yml\nservices: {}\n",
            "provider": "services:\n  app:\n    provider:\n      type: external\n",
            "configs": "configs:\n  app:\n    file: ./config.txt\nservices: {}\n",
            "secrets": "secrets:\n  token:\n    file: ./token\nservices: {}\n",
            "volumes_from": (
                "services:\n  app:\n    image: example/app:1\n"
                "    volumes_from:\n      - container:external\n"
            ),
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            compose = root / "docker-compose.yml"
            for key, payload in payloads.items():
                with self.subTest(key=key):
                    compose.write_text(payload, encoding="utf-8")
                    if os.name == "posix":
                        compose.chmod(0o600)
                    with self.assertRaisesRegex(
                        ValidationError, rf"Compose key {key} запрещён"
                    ):
                        Runner(dry_run=True).run(
                            ["docker", "compose", "-f", str(compose), "config"]
                        )

    def test_compose_rejects_nonempty_flow_mapping_but_stops_at_subcommand(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            compose = root / "docker-compose.yml"
            compose.write_text(
                "services: {app: {image: example/app:1, build: ./untrusted}}\n",
                encoding="utf-8",
            )
            if os.name == "posix":
                compose.chmod(0o600)
            with self.assertRaisesRegex(ValidationError, "flow mapping"):
                Runner(dry_run=True).run(
                    ["docker", "compose", "-f", str(compose), "config"]
                )

            compose.write_text("services: {}\n", encoding="utf-8")
            if os.name == "posix":
                compose.chmod(0o600)
            with mock.patch(
                "remnawave_manager.runner.subprocess.run", return_value=completed
            ) as spawn:
                Runner().run(
                    [
                        "docker",
                        "compose",
                        "-f",
                        str(compose),
                        "run",
                        "app",
                        "tool",
                        "--file",
                        "/inside-container/config",
                    ]
                )

            spawn.assert_called_once()

    def test_compose_option_value_cannot_bypass_input_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            compose = Path(temporary) / "docker-compose.yml"
            compose.write_text(
                "services:\n  app:\n    build: ./untrusted\n",
                encoding="utf-8",
            )
            if os.name == "posix":
                compose.chmod(0o600)

            with self.assertRaisesRegex(ValidationError, "Compose key build запрещён"):
                Runner(dry_run=True).run(
                    [
                        "docker",
                        "compose",
                        "--profile",
                        "config",
                        "-f",
                        str(compose),
                        "config",
                    ]
                )

    def test_compose_rejects_encoded_keys_and_root_flow_mapping(self) -> None:
        payloads = (
            'services:\n  app:\n    "bu\\u0069ld": ./untrusted\n',
            "{services: {app: {build: ./untrusted}}}\n",
            "--- {services: {app: {build: ./untrusted}}}\n",
            (
                "x-bad: &bad {build: ./untrusted}\nservices:\n"
                "  app:\n    <<: *bad\n    image: example/app:1\n"
            ),
            (
                "x-bad: &bad\n  image: example/app:1\nservices:\n"
                "  app:\n    <<: {build: ./untrusted}\n"
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            compose = Path(temporary) / "docker-compose.yml"
            for payload in payloads:
                with self.subTest(payload=payload):
                    compose.write_text(payload, encoding="utf-8")
                    if os.name == "posix":
                        compose.chmod(0o600)
                    with self.assertRaises(ValidationError):
                        Runner(dry_run=True).run(
                            ["docker", "compose", "-f", str(compose), "config"]
                        )

    def test_compose_allows_static_merge_alias_list(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env_file = root / ".env"
            env_file.write_text("APP_PORT=3000\n", encoding="utf-8")
            compose = root / "docker-compose.yml"
            compose.write_text(
                "x-common: &common\n  restart: always\n"
                "x-env: &env\n  env_file: .env\n"
                "services:\n  app:\n    <<: [*common, *env]\n"
                "    image: example/app:1\n",
                encoding="utf-8",
            )
            if os.name == "posix":
                compose.chmod(0o600)
                env_file.chmod(0o600)

            Runner(dry_run=True).run(
                [
                    "docker",
                    "compose",
                    "--env-file",
                    str(env_file),
                    "-f",
                    str(compose),
                    "config",
                    "-q",
                ]
            )

    def test_compose_rejects_dynamic_or_unsafe_long_volume_type(self) -> None:
        payloads = (
            (
                "services:\n  app:\n    image: example/app:1\n    volumes:\n"
                "      - type: ${MOUNT_TYPE}\n        source: /etc\n"
                "        target: /host\n"
            ),
            (
                "services:\n  app:\n    image: example/app:1\n    volumes:\n"
                "      - type: volume\n        source: ${VOLUME_NAME}\n"
                "        target: /data\n"
            ),
        )
        with tempfile.TemporaryDirectory() as temporary:
            compose = Path(temporary) / "docker-compose.yml"
            for payload in payloads:
                with self.subTest(payload=payload):
                    compose.write_text(payload, encoding="utf-8")
                    if os.name == "posix":
                        compose.chmod(0o600)
                    with self.assertRaises(ValidationError):
                        Runner(dry_run=True).run(
                            ["docker", "compose", "-f", str(compose), "config"]
                        )

    def test_compose_rejects_hardlinked_internal_env_and_missing_bind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.env"
            source.write_text("TOKEN=value\n", encoding="utf-8")
            linked = root / ".env"
            try:
                os.link(source, linked)
            except OSError as error:
                self.skipTest(f"hard links unavailable: {error}")
            compose = root / "docker-compose.yml"
            compose.write_text(
                "services:\n"
                "  app:\n"
                "    image: example/app:1\n"
                "    env_file: .env\n",
                encoding="utf-8",
            )
            if os.name == "posix":
                compose.chmod(0o600)
                source.chmod(0o600)
            with self.assertRaisesRegex(ValidationError, "hardlink"):
                Runner(dry_run=True).run(
                    ["docker", "compose", "-f", str(compose), "config"]
                )

            linked.unlink()
            source.unlink()
            compose.write_text(
                "services:\n"
                "  app:\n"
                "    image: example/app:1\n"
                "    volumes:\n"
                "      - ./missing:/srv/data:ro\n",
                encoding="utf-8",
            )
            if os.name == "posix":
                compose.chmod(0o600)
            with self.assertRaisesRegex(ValidationError, "bind source отсутствует"):
                Runner(dry_run=True).run(
                    ["docker", "compose", "-f", str(compose), "config"]
                )

    def test_compose_rejects_dynamic_reference_and_project_directory_override(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            compose = root / "docker-compose.yml"
            compose.write_text(
                "services:\n"
                "  app:\n"
                "    image: example/app:1\n"
                "    env_file: ${ENV_FILE}\n",
                encoding="utf-8",
            )
            if os.name == "posix":
                compose.chmod(0o600)
            with self.assertRaisesRegex(ValidationError, "статическим путём"):
                Runner(dry_run=True).run(
                    ["docker", "compose", "-f", str(compose), "config"]
                )
            external = root / "external/.env"
            external.parent.mkdir()
            external.write_text("TOKEN=value\n", encoding="utf-8")
            compose.write_text(
                "services:\n"
                "  app:\n"
                "    image: example/app:1\n"
                "    env_file: ./external/.env\n",
                encoding="utf-8",
            )
            if os.name == "posix":
                compose.chmod(0o600)
                external.chmod(0o600)
            with self.assertRaisesRegex(ValidationError, "контролировал его drift"):
                Runner(dry_run=True).run(
                    ["docker", "compose", "-f", str(compose), "config"]
                )
            with self.assertRaisesRegex(ValidationError, "project-directory запрещён"):
                Runner(dry_run=True).run(
                    [
                        "docker",
                        "compose",
                        "--project-directory",
                        str(root),
                        "-f",
                        str(compose),
                        "config",
                    ]
                )

    def test_compose_input_paths_reject_missing_unsafe_and_oversized_files(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            compose = root / "docker-compose.yml"
            compose.write_text("services: {}\n", encoding="utf-8")
            if os.name == "posix":
                compose.chmod(0o600)

            with self.assertRaisesRegex(ValidationError, "абсолютный путь"):
                Runner().run(["docker", "compose", "-f", "relative.yml", "config"])
            with self.assertRaisesRegex(ValidationError, "отсутствует"):
                Runner().run(
                    ["docker", "compose", "-f", str(root / "missing.yml"), "config"]
                )

            unsafe = root / "unsafe"
            unsafe.mkdir()
            with self.assertRaisesRegex(ValidationError, "обычным файлом"):
                Runner().run(["docker", "compose", "-f", str(unsafe), "config"])

            hardlink = root / "hardlink.yml"
            try:
                os.link(compose, hardlink)
            except OSError as error:
                self.skipTest(f"hard links unavailable: {error}")
            with self.assertRaisesRegex(ValidationError, "hardlink"):
                Runner().run(["docker", "compose", "-f", str(hardlink), "config"])

            oversized = root / "oversized.yml"
            oversized.write_bytes(b"x" * (16 * 1024 * 1024 + 1))
            if os.name == "posix":
                oversized.chmod(0o600)
            with self.assertRaisesRegex(ValidationError, "превышает допустимый размер"):
                Runner().run(["docker", "compose", "-f", str(oversized), "config"])

    def test_compose_rejects_unsafe_default_env_in_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            compose = root / "docker-compose.yml"
            compose.write_text("services: {}\n", encoding="utf-8")
            if os.name == "posix":
                compose.chmod(0o600)
            working_directory = root / "working"
            working_directory.mkdir()
            (working_directory / ".env").mkdir()

            with self.assertRaisesRegex(ValidationError, "Compose .env-файл"):
                Runner().run(
                    ["docker", "compose", "-f", str(compose), "config"],
                    cwd=working_directory,
                )

    def test_compose_input_path_rejects_symlink_before_spawn(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.yml"
            source.write_text("services: {}\n", encoding="utf-8")
            if os.name == "posix":
                source.chmod(0o600)
            compose = root / "docker-compose.yml"
            try:
                compose.symlink_to(source)
            except OSError as error:
                self.skipTest(f"symbolic links unavailable: {error}")

            with (
                mock.patch("remnawave_manager.runner.subprocess.run") as spawn,
                self.assertRaisesRegex(ValidationError, "symlink"),
            ):
                Runner().run(["docker", "compose", "-f", str(compose), "config"])

            spawn.assert_not_called()

    @unittest.skipUnless(
        os.name == "posix", "POSIX ownership and modes are unavailable"
    )
    def test_compose_input_paths_reject_writable_parent_and_non_root_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            parent = root / "compose"
            parent.mkdir()
            compose = parent / "docker-compose.yml"
            compose.write_text("services: {}\n", encoding="utf-8")
            compose.chmod(0o600)

            parent.chmod(0o770)
            with self.assertRaisesRegex(ValidationError, "доступен для записи"):
                Runner().run(["docker", "compose", "-f", str(compose), "config"])
            parent.chmod(0o700)

            compose.chmod(0o620)
            with self.assertRaisesRegex(ValidationError, "не только владельцу"):
                Runner().run(["docker", "compose", "-f", str(compose), "config"])
            compose.chmod(0o600)

            if os.geteuid() != 0:
                self.skipTest("non-root owner mutation requires root test process")
            os.chown(compose, 65534, os.getgid())
            with self.assertRaisesRegex(ValidationError, "другому пользователю"):
                Runner().run(["docker", "compose", "-f", str(compose), "config"])

    @unittest.skipUnless(
        os.name == "posix" and os.geteuid() == 0,
        "service-owned bind directory requires a root POSIX test process",
    )
    def test_compose_allows_service_owned_bind_leaf_under_trusted_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir(mode=0o700)
            postgres = data / "postgres"
            postgres.mkdir(mode=0o700)
            os.chown(postgres, 65534, 65534)
            compose = root / "docker-compose.yml"
            compose.write_text(
                "services:\n"
                "  postgres:\n"
                "    image: postgres:18\n"
                "    volumes:\n"
                "      - ./data/postgres:/var/lib/postgresql:rw\n",
                encoding="utf-8",
            )
            compose.chmod(0o600)

            Runner(dry_run=True).run(
                ["docker", "compose", "-f", str(compose), "config"]
            )

    def test_explicit_environment_is_validated_and_sanitized(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch(
            "remnawave_manager.runner.subprocess.run", return_value=completed
        ) as run:
            Runner().run(
                ["wgcf", "update"],
                env={
                    "WGCF_LICENSE_KEY": "license",
                    "COMPOSE_FILE": "/tmp/foreign.yml",
                    "LD_LIBRARY_PATH": "/tmp/lib",
                },
                sensitive=True,
            )

        child = run.call_args.kwargs["env"]
        self.assertEqual(child["WGCF_LICENSE_KEY"], "license")
        self.assertNotIn("COMPOSE_FILE", child)
        self.assertNotIn("LD_LIBRARY_PATH", child)

    def test_certbot_lock_marker_is_allowed_only_as_explicit_environment(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        with (
            mock.patch.dict(
                os.environ,
                {"RWM_CERTBOT_MANAGER_LOCK_HELD": "inherited"},
                clear=False,
            ),
            mock.patch(
                "remnawave_manager.runner.subprocess.run",
                return_value=completed,
            ) as run,
        ):
            Runner().run(["true"])
            inherited = run.call_args.kwargs["env"]
            Runner().run(
                ["certbot", "renew"],
                env={"RWM_CERTBOT_MANAGER_LOCK_HELD": "1"},
            )
            explicit = run.call_args.kwargs["env"]

        self.assertNotIn("RWM_CERTBOT_MANAGER_LOCK_HELD", inherited)
        self.assertEqual(explicit["RWM_CERTBOT_MANAGER_LOCK_HELD"], "1")

    def test_invalid_explicit_environment_is_rejected_before_spawn(self) -> None:
        with (
            mock.patch("remnawave_manager.runner.subprocess.run") as run,
            self.assertRaisesRegex(ValidationError, "Environment"),
        ):
            Runner().run(["true"], env={"VALUE": "bad\x00value"})
        run.assert_not_called()

    def test_run_rejects_nul_before_spawning(self) -> None:
        with (
            mock.patch("remnawave_manager.runner.subprocess.run") as run,
            self.assertRaisesRegex(ValidationError, "NUL"),
        ):
            Runner().run(["docker", "bad\x00argument"])
        run.assert_not_called()

    def test_all_docker_execution_paths_pin_local_rootful_socket(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        binary_completed = subprocess.CompletedProcess([], 0, b"", b"")
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "output"
            with mock.patch(
                "remnawave_manager.runner.subprocess.run",
                side_effect=[completed, binary_completed, completed],
            ) as run:
                runner = Runner()
                runner.run(["docker", "ps"])
                runner.run_to_file(["/usr/bin/docker", "exec", "db", "pg_dump"], target)
                runner.interactive(["docker", "logs", "--follow", "node"])

        for call in run.call_args_list:
            command = call.args[0]
            self.assertEqual(command[1], "--host=unix:///run/docker.sock")

    def test_interactive_wraps_spawn_errors(self) -> None:
        with (
            mock.patch(
                "remnawave_manager.runner.subprocess.run",
                side_effect=FileNotFoundError("docker is missing"),
            ),
            self.assertRaisesRegex(CommandError, "docker is missing") as raised,
        ):
            Runner().interactive(["docker", "logs", "node"])

        self.assertIn("--host=unix:///run/docker.sock", str(raised.exception))
        self.assertIsInstance(raised.exception.__cause__, FileNotFoundError)

    def test_docker_execution_rejects_host_or_context_override(self) -> None:
        for command in (
            ["docker", "--host", "tcp://remote.example:2375", "ps"],
            ["docker", "-Htcp://remote.example:2375", "ps"],
            ["docker", "-c=remote", "ps"],
            ["docker", "-cremote", "ps"],
            ["docker", "--context=remote", "ps"],
        ):
            with self.subTest(command=command), self.assertRaises(ValidationError):
                Runner(dry_run=True).run(command)

    def test_docker_exec_does_not_treat_inner_shell_c_as_global_context(self) -> None:
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch(
            "remnawave_manager.runner.subprocess.run", return_value=completed
        ) as run:
            Runner().run(
                ["docker", "exec", "subscription", "sh", "-c", "printf ok"]
            )

        self.assertEqual(
            run.call_args.args[0],
            (
                "docker",
                "--host=unix:///run/docker.sock",
                "exec",
                "subscription",
                "sh",
                "-c",
                "printf ok",
            ),
        )

    def test_sensitive_failure_hides_command_and_output(self) -> None:
        completed = subprocess.CompletedProcess(
            ["tool", "super-secret-token"], 1, "", "leaked detail"
        )
        with (
            mock.patch(
                "remnawave_manager.runner.subprocess.run", return_value=completed
            ),
            self.assertRaises(CommandError) as raised,
        ):
            Runner().run(["tool", "super-secret-token"], sensitive=True)

        message = str(raised.exception)
        self.assertNotIn("super-secret-token", message)
        self.assertNotIn("leaked detail", message)
        self.assertIn("<скрыта>", message)

    def test_compose_config_failure_is_automatically_sensitive(self) -> None:
        completed = subprocess.CompletedProcess(
            [], 1, "", "invalid interpolation: DATABASE_PASSWORD=must-not-leak"
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            compose = root / "docker-compose.yml"
            compose.write_text("services: {}\n", encoding="utf-8")
            if os.name == "posix":
                compose.chmod(0o600)
            with (
                mock.patch(
                    "remnawave_manager.runner.subprocess.run", return_value=completed
                ),
                self.assertRaises(CommandError) as raised,
            ):
                Runner().run(
                    [
                        "docker",
                        "compose",
                        "--profile",
                        "config",
                        "-f",
                        str(compose),
                        "config",
                        "-q",
                    ]
                )

        message = str(raised.exception)
        self.assertNotIn("must-not-leak", message)
        self.assertNotIn(str(compose), message)
        self.assertIn("<скрыта>", message)

    def test_sensitive_timeout_hides_command(self) -> None:
        with (
            mock.patch(
                "remnawave_manager.runner.subprocess.run",
                side_effect=subprocess.TimeoutExpired(
                    ["tool", "super-secret-token"], 1
                ),
            ),
            self.assertRaises(CommandError) as raised,
        ):
            Runner().run(["tool", "super-secret-token"], sensitive=True)

        self.assertNotIn("super-secret-token", str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)

    def test_sensitive_spawn_error_hides_detail_and_cause(self) -> None:
        with (
            mock.patch(
                "remnawave_manager.runner.subprocess.run",
                side_effect=OSError("failed around super-secret-token"),
            ),
            self.assertRaises(CommandError) as raised,
        ):
            Runner().run(["tool", "super-secret-token"], sensitive=True)

        message = str(raised.exception)
        self.assertNotIn("super-secret-token", message)
        self.assertIn("<скрыта>", message)
        self.assertIsNone(raised.exception.__cause__)

    def test_command_failure_sanitizes_terminal_control_characters(self) -> None:
        completed = subprocess.CompletedProcess(
            ["tool"],
            1,
            "",
            "failure\x1b[31m\rspoof\u202ehidden",
        )
        with (
            mock.patch(
                "remnawave_manager.runner.subprocess.run", return_value=completed
            ),
            self.assertRaises(CommandError) as raised,
        ):
            Runner().run(["tool"])

        message = str(raised.exception)
        self.assertIn("failure [31m spoof hidden", message)
        self.assertNotIn("\x1b", message)
        self.assertNotIn("\r", message)
        self.assertNotIn("\u202e", message)

    def test_run_to_file_failure_preserves_previous_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "dump"
            target.write_bytes(b"known-good")
            completed = subprocess.CompletedProcess(["pg_dump"], 1, b"", b"failed")

            with (
                mock.patch(
                    "remnawave_manager.runner.subprocess.run", return_value=completed
                ),
                self.assertRaises(CommandError),
            ):
                Runner().run_to_file(["pg_dump"], target)

            self.assertEqual(target.read_bytes(), b"known-good")
            self.assertEqual(list(target.parent.glob(f".{target.name}.*")), [])

    def test_atomic_write_rejects_invalid_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "state"
            with self.assertRaises(ValidationError):
                atomic_write_bytes(target, b"value", mode=0o1000)
            self.assertFalse(target.exists())

    def test_atomic_write_wraps_filesystem_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "state"
            with (
                mock.patch(
                    "remnawave_manager.runner.os.replace",
                    side_effect=OSError("read-only filesystem"),
                ),
                self.assertRaisesRegex(ValidationError, "атомарно записать") as raised,
            ):
                atomic_write_bytes(target, b"value")

            self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertFalse(target.exists())

    def test_atomic_copy_rejects_non_regular_or_oversized_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            with self.assertRaises(ValidationError):
                atomic_copy(root, target)
            with (
                mock.patch("remnawave_manager.runner._MAX_ATOMIC_COPY_SIZE", 4),
                self.assertRaisesRegex(ValidationError, "превышает"),
            ):
                source = root / "source"
                source.write_bytes(b"12345")
                atomic_copy(source, target)
            self.assertFalse(target.exists())

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks are unavailable")
    def test_atomic_copy_refuses_symlink_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.write_bytes(b"untrusted")
            link = root / "source-link"
            try:
                link.symlink_to(source)
            except OSError as error:
                self.skipTest(f"symbolic links unavailable: {error}")
            target = root / "target"
            target.write_bytes(b"known-good")

            with self.assertRaisesRegex(ValidationError, "обычным файлом"):
                atomic_copy(link, target)

            self.assertEqual(target.read_bytes(), b"known-good")

    @unittest.skipUnless(hasattr(os, "link"), "hard links are unavailable")
    def test_atomic_copy_refuses_hardlink_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.write_bytes(b"untrusted")
            link = root / "source-link"
            try:
                os.link(source, link)
            except OSError as error:
                self.skipTest(f"hard links unavailable: {error}")

            with self.assertRaisesRegex(ValidationError, "hardlink"):
                atomic_copy(link, root / "target")

            with self.assertRaisesRegex(ValidationError, "hardlink"):
                sha256_file(link)

    def test_sha256_rejects_path_replaced_during_hashing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            replacement = root / "replacement"
            source.write_bytes(b"original")
            replacement.write_bytes(b"replacement")
            original_lstat = Path.lstat
            calls = 0

            def replace_before_final_check(path: Path):
                nonlocal calls
                calls += 1
                if calls == 2:
                    os.replace(replacement, source)
                return original_lstat(path)

            with (
                mock.patch.object(Path, "lstat", new=replace_before_final_check),
                self.assertRaisesRegex(ValidationError, "изменился во время хеширования"),
            ):
                sha256_file(source)

            self.assertEqual(source.read_bytes(), b"replacement")

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks are unavailable")
    def test_lock_refuses_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.write_bytes(b"do-not-touch")
            link = root / "manager.lock"
            try:
                link.symlink_to(target)
            except OSError as error:
                self.skipTest(f"symbolic links unavailable: {error}")

            with self.assertRaises(ValidationError), exclusive_lock(link):
                pass
            self.assertEqual(target.read_bytes(), b"do-not-touch")

    @unittest.skipUnless(
        os.name == "posix", "POSIX ownership and modes are unavailable"
    )
    def test_lock_refuses_insecure_parent_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary) / "runtime-lock"
            parent.mkdir(mode=0o755)

            with (
                self.assertRaisesRegex(ValidationError, "0700"),
                exclusive_lock(parent / "manager.lock"),
            ):
                pass


class StateSecurityTests(unittest.TestCase):
    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks are unavailable")
    def test_initialize_refuses_symlinked_manager_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root / "outside"
            outside.mkdir()
            original_mode = os.stat(outside).st_mode & 0o777
            etc = root / "etc"
            etc.mkdir()
            try:
                (etc / "remnawave-manager").symlink_to(
                    outside, target_is_directory=True
                )
            except OSError as error:
                self.skipTest(f"symbolic links unavailable: {error}")

            with self.assertRaisesRegex(ValidationError, "небезопасный тип"):
                StateStore(RuntimePaths(root)).initialize()
            self.assertEqual(os.stat(outside).st_mode & 0o777, original_mode)

    def test_settings_must_be_a_json_object(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(RuntimePaths(Path(temporary)))
            store.initialize()
            store.paths.settings.write_text("[]\n", encoding="utf-8")
            if os.name == "posix":
                store.paths.settings.chmod(0o600)

            with self.assertRaisesRegex(ValidationError, "повреждён"):
                store.load_settings()

    @unittest.skipUnless(hasattr(os, "link"), "hard links are unavailable")
    def test_private_state_refuses_hardlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(RuntimePaths(Path(temporary)))
            store.initialize()
            source = store.paths.etc / "source.json"
            source.write_text("{}\n", encoding="utf-8")
            if os.name == "posix":
                source.chmod(0o600)
            try:
                os.link(source, store.paths.settings)
            except OSError as error:
                self.skipTest(f"hard links unavailable: {error}")

            with self.assertRaisesRegex(ValidationError, "hardlink"):
                store.load_settings()

    @unittest.skipUnless(os.name == "posix", "POSIX mode checks are unavailable")
    def test_secrets_must_not_be_group_or_world_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(RuntimePaths(Path(temporary)))
            store.initialize()
            store.paths.secrets.write_text("{}\n", encoding="utf-8")
            store.paths.secrets.chmod(0o644)

            with self.assertRaisesRegex(ValidationError, "доступен другим"):
                store.load_secrets()

    @unittest.skipUnless(hasattr(Path, "symlink_to"), "symlinks are unavailable")
    def test_secrets_refuse_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(RuntimePaths(root))
            store.initialize()
            target = root / "outside-secrets.json"
            target.write_text("{}\n", encoding="utf-8")
            try:
                store.paths.secrets.symlink_to(target)
            except OSError as error:
                self.skipTest(f"symbolic links unavailable: {error}")

            with self.assertRaisesRegex(ValidationError, "символьной ссылкой"):
                store.load_secrets()


if __name__ == "__main__":
    unittest.main()
