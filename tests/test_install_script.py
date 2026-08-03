from __future__ import annotations

import json
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = (PROJECT_ROOT / "install.sh").read_text(encoding="utf-8")
COMPATIBILITY = json.loads(
    (PROJECT_ROOT / "src/remnawave_manager/data/compatibility.json").read_text(
        encoding="utf-8"
    )
)


class InstallScriptTests(unittest.TestCase):
    def test_installer_sanitizes_root_command_environment(self) -> None:
        platform_probe = INSTALL_SCRIPT.index("uname -s")

        self.assertTrue(INSTALL_SCRIPT.startswith("#!/bin/bash\n"))
        self.assertLess(INSTALL_SCRIPT.index("export PATH="), platform_probe)
        self.assertLess(INSTALL_SCRIPT.index("unset BASH_ENV"), platform_probe)
        self.assertIn("set +x", INSTALL_SCRIPT)
        self.assertIn("PYTHONNOUSERSITE=1", INSTALL_SCRIPT)
        self.assertIn("PYTHONSAFEPATH=1", INSTALL_SCRIPT)
        for variable in (
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "DOCKER_CLI_PLUGIN_EXTRA_DIRS",
            "COMPOSE_FILE",
            "PYTHONPATH",
            "PIP_CONFIG_FILE",
        ):
            self.assertIn(variable, INSTALL_SCRIPT)

    def test_platform_preflight_precedes_apt_changes(self) -> None:
        first_apt_change = INSTALL_SCRIPT.index("enable_ubuntu_universe\n")

        self.assertLess(INSTALL_SCRIPT.index("uname -s"), first_apt_change)
        self.assertLess(
            INSTALL_SCRIPT.index("dpkg --print-architecture"),
            first_apt_change,
        )
        self.assertIn('amd64 | arm64) ;;', INSTALL_SCRIPT)

    def test_os_release_is_parsed_without_executing_shell_content(self) -> None:
        self.assertNotIn("source /etc/os-release", INSTALL_SCRIPT)
        self.assertNotIn(". /etc/os-release", INSTALL_SCRIPT)
        self.assertIn("read_os_release_value() {", INSTALL_SCRIPT)
        self.assertIn("^[A-Za-z0-9._-]+$", INSTALL_SCRIPT)
        self.assertIn("'/usr/lib/os-release'", INSTALL_SCRIPT)
        self.assertIn("stat -c '%u:%g:%h:%a:%s'", INSTALL_SCRIPT)
        self.assertIn("os_release_size > 65536", INSTALL_SCRIPT)

    def test_universe_is_enabled_before_remnawave_dependencies(self) -> None:
        universe_call = INSTALL_SCRIPT.index("enable_ubuntu_universe\n")
        dependencies = INSTALL_SCRIPT.index("ca-certificates", universe_call)

        self.assertLess(universe_call, dependencies)
        self.assertIn(
            "add-apt-repository --yes --no-update universe",
            INSTALL_SCRIPT,
        )

    def test_rootful_docker_service_is_an_explicit_install_invariant(self) -> None:
        docker_install = INSTALL_SCRIPT.index("install_docker_components\n")
        service_check = INSTALL_SCRIPT.index("ensure_rootful_docker_service\n")

        self.assertLess(docker_install, service_check)
        self.assertIn(
            "systemctl enable --now docker.service",
            INSTALL_SCRIPT,
        )
        self.assertIn("docker --host unix:///run/docker.sock info", INSTALL_SCRIPT)
        self.assertIn("Rootless Docker", INSTALL_SCRIPT)

    def test_gcore_plugin_is_pinned_hashed_and_visible_to_system_certbot(self) -> None:
        plugin_install = INSTALL_SCRIPT.index("install_gcore_certbot_plugin\n")
        contract = COMPATIBILITY["tools"]["certbot_dns_gcore"]

        self.assertIn(f"GCORE_PLUGIN_VERSION='{contract['version']}'", INSTALL_SCRIPT)
        self.assertIn(
            f"GCORE_PLUGIN_SHA256='{contract['sha256']}'",
            INSTALL_SCRIPT,
        )
        self.assertIn(f"GCORE_PLUGIN_URL='{contract['url']}'", INSTALL_SCRIPT)
        self.assertIn("curl --disable --fail", INSTALL_SCRIPT)
        self.assertIn("--proto '=https' --proto-redir '=https'", INSTALL_SCRIPT)
        self.assertIn("sha256sum --check --status", INSTALL_SCRIPT)
        self.assertIn("--break-system-packages", INSTALL_SCRIPT)
        self.assertIn("--no-deps", INSTALL_SCRIPT)
        self.assertIn("python3 -m pip --isolated install", INSTALL_SCRIPT)
        self.assertIn("certbot plugins", INSTALL_SCRIPT)
        self.assertLess(INSTALL_SCRIPT.index("python3-pip"), plugin_install)

    def test_certificate_config_root_has_ownership_and_private_permissions(self) -> None:
        self.assertIn('CONFIG_ROOT="/etc/remnawave-manager"', INSTALL_SCRIPT)
        self.assertIn(
            'CONFIG_OWNERSHIP_MARKER="${CONFIG_ROOT}/.managed-by-remnawave-manager"',
            INSTALL_SCRIPT,
        )
        self.assertIn(
            'install -d -o root -g root -m 0700 "${CONFIG_ROOT}"',
            INSTALL_SCRIPT,
        )
        self.assertIn('chmod 0600 "${CONFIG_OWNERSHIP_MARKER}"', INSTALL_SCRIPT)
        self.assertIn(
            'Каталог ${CONFIG_ROOT} не принадлежит Remnawave Manager',
            INSTALL_SCRIPT,
        )

    def test_managed_directories_and_markers_are_validated_before_apt(self) -> None:
        first_apt_change = INSTALL_SCRIPT.index("enable_ubuntu_universe\n")
        directory_validation = INSTALL_SCRIPT.index(
            'validate_managed_directory "${MANAGED_ROOT}"'
        )
        marker_validation = INSTALL_SCRIPT.index(
            'validate_ownership_marker "${OWNERSHIP_MARKER}" \'644\''
        )

        self.assertLess(directory_validation, first_apt_change)
        self.assertLess(marker_validation, first_apt_change)
        self.assertIn("stat -c '%u:%g:%a'", INSTALL_SCRIPT)
        self.assertIn("(8#${permissions} & 0022)", INSTALL_SCRIPT)
        self.assertIn("stat -c '%u:%g:%h:%a:%s'", INSTALL_SCRIPT)
        self.assertIn("marker_size > 1024", INSTALL_SCRIPT)
        self.assertIn(
            'validate_ownership_marker "${active_venv_target}/${VENV_MARKER}" \'644\' true',
            INSTALL_SCRIPT,
        )
        self.assertIn(
            'validate_ownership_marker "${previous_venv_target}/${VENV_MARKER}" \'644\' true',
            INSTALL_SCRIPT,
        )
        self.assertIn(
            'install -d -o root -g root -m 0755 "${MANAGED_ROOT}" "${RUNTIME_DIR}"',
            INSTALL_SCRIPT,
        )

    def test_manager_upgrade_is_staged_validated_and_atomically_activated(self) -> None:
        stage = INSTALL_SCRIPT.index("mktemp -d", INSTALL_SCRIPT.index("staged_venv"))
        install = INSTALL_SCRIPT.index(
            '"${staged_venv}/bin/python" -m pip --isolated install'
        )
        validate = INSTALL_SCRIPT.index('"${staged_venv}/bin/rwm" --help')
        activate = INSTALL_SCRIPT.index(
            'mv -Tf -- "${ACTIVE_VENV_LINK}.new.$$" "${ACTIVE_VENV_LINK}"'
        )

        self.assertLess(stage, install)
        self.assertLess(install, validate)
        self.assertLess(validate, activate)
        previous_pointer = INSTALL_SCRIPT.index(
            'mv -Tf -- "${PREVIOUS_VENV_LINK}.new.$$" "${PREVIOUS_VENV_LINK}"'
        )
        self.assertLess(previous_pointer, activate)
        self.assertIn(
            "rollback pointer; исходные release-ссылки восстановлены",
            INSTALL_SCRIPT,
        )
        self.assertIn("flock --nonblock 9", INSTALL_SCRIPT)
        self.assertIn(
            'readonly MANAGER_LOCK_DIR="/run/remnawave-manager"',
            INSTALL_SCRIPT,
        )
        self.assertIn(
            'readonly MANAGER_LOCK="${MANAGER_LOCK_DIR}/manager.lock"',
            INSTALL_SCRIPT,
        )
        self.assertIn(
            'install -d -o root -g root -m 0700 -- "${MANAGER_LOCK_DIR}"',
            INSTALL_SCRIPT,
        )
        self.assertIn("stat -c '%u:%g:%a'", INSTALL_SCRIPT)
        self.assertIn("stat -c '%u:%g:%h'", INSTALL_SCRIPT)
        self.assertNotIn("remnawave-manager-install.lock", INSTALL_SCRIPT)
        self.assertIn('PREVIOUS_VENV_LINK="${RUNTIME_DIR}/previous"', INSTALL_SCRIPT)
        self.assertIn('chmod 0755 "${staged_venv}"', INSTALL_SCRIPT)
        self.assertNotIn("--force-reinstall", INSTALL_SCRIPT)
        self.assertIn("активная версия не изменена", INSTALL_SCRIPT)
        self.assertLess(
            INSTALL_SCRIPT.index(
                'assert_managed_link_unchanged "${ACTIVE_VENV_LINK}"'
            ),
            activate,
        )
        self.assertLess(
            INSTALL_SCRIPT.index(
                'assert_managed_link_unchanged "${ENTRYPOINT}"'
            ),
            activate,
        )

    def test_partial_release_switch_restores_every_previous_link(self) -> None:
        helper_start = INSTALL_SCRIPT.index("restore_release_links() {")
        helper_end = INSTALL_SCRIPT.index("\n}\n", helper_start) + 3
        helper = INSTALL_SCRIPT[helper_start:helper_end]

        self.assertIn('initial_active_link_state', helper)
        self.assertIn('initial_entrypoint_state', helper)
        self.assertIn('initial_previous_link_state', helper)
        self.assertIn('restore_managed_link', helper)

        guarded_restore_start = INSTALL_SCRIPT.index("restore_managed_link() {")
        guarded_restore_end = INSTALL_SCRIPT.index(
            "\n}\n", guarded_restore_start
        ) + 3
        guarded_restore = INSTALL_SCRIPT[guarded_restore_start:guarded_restore_end]
        self.assertIn('current_state', guarded_restore)
        self.assertIn('transaction_state', guarded_restore)
        self.assertIn('автоматический rollback его не перезаписывает', guarded_restore)

        switch_failure = INSTALL_SCRIPT.index(
            "if ! ln -s -- \"${staged_venv}\" \"${ACTIVE_VENV_LINK}.new.$$\""
        )
        health_failure = INSTALL_SCRIPT.index(
            'if ! "${ENTRYPOINT}" --help >/dev/null;', switch_failure
        )
        self.assertIn(
            "if restore_release_links; then",
            INSTALL_SCRIPT[switch_failure:health_failure],
        )
        self.assertIn(
            "if ! restore_release_links; then",
            INSTALL_SCRIPT[health_failure:],
        )

    def test_signals_exit_and_rollback_uncommitted_release_switch(self) -> None:
        cleanup_start = INSTALL_SCRIPT.index("cleanup_install_artifacts() {")
        cleanup_end = INSTALL_SCRIPT.index("\n}\n", cleanup_start) + 3
        cleanup = INSTALL_SCRIPT[cleanup_start:cleanup_end]

        self.assertIn("release_switch_started", cleanup)
        self.assertIn("release_switch_committed", cleanup)
        self.assertIn("restore_release_links", cleanup)
        self.assertIn("rollback_incomplete", cleanup)
        self.assertIn("trap cleanup_install_artifacts EXIT", INSTALL_SCRIPT)
        self.assertIn("trap 'exit 129' HUP", INSTALL_SCRIPT)
        self.assertIn("trap 'exit 130' INT", INSTALL_SCRIPT)
        self.assertIn("trap 'exit 143' TERM", INSTALL_SCRIPT)
        self.assertLess(
            INSTALL_SCRIPT.index("release_switch_started='true'"),
            INSTALL_SCRIPT.index(
                'mv -Tf -- "${PREVIOUS_VENV_LINK}.new.$$"'
            ),
        )
        self.assertLess(
            INSTALL_SCRIPT.index('if ! "${ENTRYPOINT}" --help'),
            INSTALL_SCRIPT.index("release_switch_committed='true'"),
        )


if __name__ == "__main__":
    unittest.main()
