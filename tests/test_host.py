from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remnawave_manager.errors import TransactionError, ValidationError
from remnawave_manager.host import (
    _required_unit_state,
    _restore_unit_state,
    configure_host,
    host_status,
    update_operating_system,
)
from remnawave_manager.paths import RuntimePaths
from remnawave_manager.runner import Result


def _successful_result(args: list[str]) -> Result:
    if args[:3] == ["sysctl", "-n", "net.ipv4.tcp_available_congestion_control"]:
        stdout = "cubic bbr\n"
    elif args[:3] == ["sysctl", "-n", "net.ipv4.tcp_congestion_control"]:
        stdout = "bbr\n"
    elif args[:3] == ["sysctl", "-n", "net.core.default_qdisc"]:
        stdout = "fq\n"
    elif args[:2] == ["systemctl", "is-enabled"]:
        stdout = "enabled\n"
    elif args[:2] == ["systemctl", "is-active"]:
        stdout = "active\n"
    else:
        stdout = ""
    return Result(tuple(args), 0, stdout, "")


class HostManagementTests(unittest.TestCase):
    def test_os_update_runs_full_upgrade_noninteractively_and_reports_reboot(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = RuntimePaths(Path(temporary))
            marker = runtime.root / "var/run/reboot-required"
            marker.parent.mkdir(parents=True)
            marker.write_text("", encoding="utf-8")
            runner = mock.Mock()
            runner.run.return_value = Result(("apt-get",), 0, "", "")

            with mock.patch.dict(
                os.environ,
                {
                    "HTTPS_PROXY": "http://proxy.example:3128",
                    "APT_CONFIG": "/untrusted/apt.conf",
                    "RWM_API_TOKEN": "must-not-leak",
                },
                clear=True,
            ):
                result = update_operating_system(runner, runtime)

        self.assertTrue(result.reboot_required)
        self.assertEqual(runner.run.call_count, 2)
        update_call, upgrade_call = runner.run.call_args_list
        self.assertEqual(
            update_call.args[0],
            ["apt-get", "-o", "DPkg::Lock::Timeout=600", "update"],
        )
        self.assertEqual(upgrade_call.args[0][-2:], ["-y", "full-upgrade"])
        self.assertIn("Dpkg::Options::=--force-confdef", upgrade_call.args[0])
        self.assertIn("Dpkg::Options::=--force-confold", upgrade_call.args[0])
        environment = update_call.kwargs["env"]
        self.assertEqual(environment, upgrade_call.kwargs["env"])
        self.assertEqual(environment["DEBIAN_FRONTEND"], "noninteractive")
        self.assertEqual(environment["NEEDRESTART_MODE"], "a")
        self.assertEqual(
            environment["HTTPS_PROXY"], "http://proxy.example:3128"
        )
        self.assertNotIn("APT_CONFIG", environment)
        self.assertNotIn("RWM_API_TOKEN", environment)
        self.assertEqual(update_call.kwargs["timeout"], 1800)
        self.assertEqual(upgrade_call.kwargs["timeout"], 7200)

    def test_os_update_stops_if_package_lists_cannot_be_refreshed(self) -> None:
        runner = mock.Mock()
        runner.run.side_effect = TransactionError("apt update failed")

        with self.assertRaisesRegex(TransactionError, "apt update failed"):
            update_operating_system(runner, RuntimePaths(Path("/")))

        runner.run.assert_called_once()

    def test_unit_snapshot_accepts_all_restorable_systemd_enablement_states(self) -> None:
        for enabled_state in (
            "enabled",
            "enabled-runtime",
            "disabled",
            "masked",
            "masked-runtime",
            "static",
            "indirect",
        ):
            with self.subTest(enabled_state=enabled_state):
                enabled_code = 0 if enabled_state in {
                    "enabled",
                    "enabled-runtime",
                    "static",
                    "indirect",
                } else 1
                runner = mock.Mock()
                runner.run.side_effect = (
                    Result(("systemctl", "is-enabled"), enabled_code, enabled_state, ""),
                    Result(("systemctl", "is-active"), 3, "inactive", ""),
                )

                self.assertEqual(
                    _required_unit_state(runner, "apt-daily.timer"),
                    {"enabled": enabled_state, "active": "inactive"},
                )

    def test_unit_rollback_continues_after_repeated_interrupts(self) -> None:
        calls: list[list[str]] = []
        runner = mock.Mock()

        def run(args: list[str], **_kwargs: object) -> Result:
            calls.append(args)
            if tuple(args[:2]) in {
                ("systemctl", "stop"),
                ("systemctl", "disable"),
            }:
                raise KeyboardInterrupt("interrupted compensation")
            if args[:2] == ["systemctl", "is-enabled"]:
                return Result(tuple(args), 0, "enabled\n", "")
            if args[:2] == ["systemctl", "is-active"]:
                return Result(tuple(args), 0, "active\n", "")
            return Result(tuple(args), 0, "", "")

        runner.run.side_effect = run

        with self.assertRaisesRegex(TransactionError, "active-state.*enablement cleanup"):
            _restore_unit_state(
                runner,
                "apt-daily.timer",
                {"enabled": "disabled", "active": "inactive"},
            )

        self.assertIn(["systemctl", "stop", "apt-daily.timer"], calls)
        self.assertIn(["systemctl", "disable", "apt-daily.timer"], calls)
        self.assertIn(["systemctl", "is-enabled", "apt-daily.timer"], calls)
        self.assertIn(["systemctl", "is-active", "apt-daily.timer"], calls)

    def test_runtime_enablement_is_restored_without_becoming_persistent(self) -> None:
        state = {"enabled": "enabled", "active": "inactive"}
        runner = mock.Mock()

        def run(args: list[str], **_kwargs: object) -> Result:
            if args[:2] == ["systemctl", "is-enabled"]:
                code = 0 if state["enabled"] != "disabled" else 1
                return Result(tuple(args), code, state["enabled"] + "\n", "")
            if args[:2] == ["systemctl", "is-active"]:
                code = 0 if state["active"] == "active" else 3
                return Result(tuple(args), code, state["active"] + "\n", "")
            if args[:2] == ["systemctl", "disable"]:
                state["enabled"] = "disabled"
            elif args[:3] == ["systemctl", "enable", "--runtime"]:
                state["enabled"] = "enabled-runtime"
            elif args[:2] == ["systemctl", "start"]:
                state["active"] = "active"
            elif args[:2] == ["systemctl", "stop"]:
                state["active"] = "inactive"
            return Result(tuple(args), 0, "", "")

        runner.run.side_effect = run

        _restore_unit_state(
            runner,
            "apt-daily.timer",
            {"enabled": "enabled-runtime", "active": "active"},
        )

        self.assertEqual(state, {"enabled": "enabled-runtime", "active": "active"})
        commands = [call.args[0] for call in runner.run.call_args_list]
        self.assertIn(["systemctl", "disable", "apt-daily.timer"], commands)
        self.assertIn(
            ["systemctl", "enable", "--runtime", "apt-daily.timer"],
            commands,
        )

    def test_masked_active_unit_is_unmasked_started_and_remasked(self) -> None:
        state = {"enabled": "enabled", "active": "inactive"}
        runner = mock.Mock()

        def run(args: list[str], **_kwargs: object) -> Result:
            if args[:2] == ["systemctl", "is-enabled"]:
                code = 0 if state["enabled"].startswith("enabled") else 1
                return Result(tuple(args), code, state["enabled"] + "\n", "")
            if args[:2] == ["systemctl", "is-active"]:
                code = 0 if state["active"] == "active" else 3
                return Result(tuple(args), code, state["active"] + "\n", "")
            if args[:2] in (["systemctl", "unmask"], ["systemctl", "disable"]):
                state["enabled"] = "disabled"
            elif args[:2] == ["systemctl", "start"]:
                state["active"] = "active"
            elif args[:2] == ["systemctl", "mask"]:
                state["enabled"] = (
                    "masked-runtime" if "--runtime" in args else "masked"
                )
            return Result(tuple(args), 0, "", "")

        runner.run.side_effect = run

        _restore_unit_state(
            runner,
            "apt-daily.timer",
            {"enabled": "masked-runtime", "active": "active"},
        )

        self.assertEqual(
            state,
            {"enabled": "masked-runtime", "active": "active"},
        )
        commands = [call.args[0] for call in runner.run.call_args_list]
        self.assertLess(
            commands.index(["systemctl", "start", "apt-daily.timer"]),
            commands.index(
                ["systemctl", "mask", "--runtime", "apt-daily.timer"]
            ),
        )

    def test_unit_restore_continues_active_compensation_after_interrupt(self) -> None:
        state = {"enabled": "enabled", "active": "active"}
        runner = mock.Mock()
        interrupted = False

        def run(args: list[str], **_kwargs: object) -> Result:
            nonlocal interrupted
            if args[:2] == ["systemctl", "is-enabled"]:
                return Result(tuple(args), 0, state["enabled"] + "\n", "")
            if args[:2] == ["systemctl", "is-active"]:
                code = 0 if state["active"] == "active" else 3
                return Result(tuple(args), code, state["active"] + "\n", "")
            if args[:2] == ["systemctl", "disable"] and not interrupted:
                interrupted = True
                raise KeyboardInterrupt
            if args[:3] == ["systemctl", "enable", "--runtime"]:
                state["enabled"] = "enabled-runtime"
            elif args[:2] == ["systemctl", "stop"]:
                state["active"] = "inactive"
            return Result(tuple(args), 0, "", "")

        runner.run.side_effect = run

        with self.assertRaisesRegex(TransactionError, "KeyboardInterrupt"):
            _restore_unit_state(
                runner,
                "apt-daily.timer",
                {"enabled": "enabled-runtime", "active": "inactive"},
            )

        self.assertEqual(
            state,
            {"enabled": "enabled-runtime", "active": "inactive"},
        )

    def test_unit_restore_verifies_activity_after_enablement_query_interrupt(self) -> None:
        runner = mock.Mock()

        def run(args: list[str], **_kwargs: object) -> Result:
            if args[:2] == ["systemctl", "is-enabled"]:
                raise KeyboardInterrupt
            if args[:2] == ["systemctl", "is-active"]:
                return Result(tuple(args), 3, "inactive\n", "")
            return Result(tuple(args), 0, "", "")

        runner.run.side_effect = run

        with self.assertRaisesRegex(TransactionError, "enablement verification"):
            _restore_unit_state(
                runner,
                "apt-daily.timer",
                {"enabled": "disabled", "active": "inactive"},
            )

        self.assertIn(
            ["systemctl", "is-active", "apt-daily.timer"],
            [call.args[0] for call in runner.run.call_args_list],
        )

    def test_apply_writes_owned_configs_and_verifies_effective_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = RuntimePaths(Path(temporary))
            runner = mock.Mock()
            runner.run.side_effect = lambda args, **_kwargs: _successful_result(args)

            status = configure_host(runner, runtime)

            sysctl = runtime.root / "etc/sysctl.d/90-remnawave-manager-bbr.conf"
            apt = (
                runtime.root
                / "etc/apt/apt.conf.d/52remnawave-manager-unattended-upgrades"
            )
            self.assertIn("tcp_congestion_control = bbr", sysctl.read_text(encoding="utf-8"))
            self.assertIn("Automatic-Reboot \"false\"", apt.read_text(encoding="utf-8"))
            self.assertTrue(status.bbr_enabled)
            self.assertTrue(status.unattended_configured)
            self.assertTrue(status.apt_daily_timer_active)
            self.assertTrue(status.apt_upgrade_timer_active)
            self.assertTrue(status.unattended_service_enabled)
            commands = [call.args[0] for call in runner.run.call_args_list]
            self.assertIn(["modprobe", "tcp_bbr"], commands)
            self.assertIn(["sysctl", "--load", str(sysctl)], commands)
            self.assertIn(
                [
                    "systemctl",
                    "enable",
                    "--now",
                    "apt-daily.timer",
                    "apt-daily-upgrade.timer",
                ],
                commands,
            )
            self.assertIn(
                [
                    "systemctl",
                    "enable",
                    "--now",
                    "unattended-upgrades.service",
                ],
                commands,
            )

    def test_apply_refuses_foreign_config_before_system_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = RuntimePaths(Path(temporary))
            sysctl = runtime.root / "etc/sysctl.d/90-remnawave-manager-bbr.conf"
            sysctl.parent.mkdir(parents=True)
            sysctl.write_text("net.ipv4.tcp_congestion_control = cubic\n", encoding="utf-8")
            runner = mock.Mock()

            with self.assertRaisesRegex(ValidationError, "создан не менеджером"):
                configure_host(runner, runtime)

            runner.run.assert_not_called()
            self.assertIn("cubic", sysctl.read_text(encoding="utf-8"))

    def test_apply_refuses_marker_outside_first_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = RuntimePaths(Path(temporary))
            sysctl = runtime.root / "etc/sysctl.d/90-remnawave-manager-bbr.conf"
            sysctl.parent.mkdir(parents=True)
            sysctl.write_text(
                "# Local administrator config\n"
                "# Managed by remnawave-manager\n"
                "net.ipv4.tcp_congestion_control = cubic\n",
                encoding="utf-8",
            )
            runner = mock.Mock()

            with self.assertRaisesRegex(ValidationError, "создан не менеджером"):
                configure_host(runner, runtime)

            runner.run.assert_not_called()

    def test_systemd_failure_rolls_back_files_runtime_and_units(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = RuntimePaths(Path(temporary))
            sysctl = runtime.root / "etc/sysctl.d/90-remnawave-manager-bbr.conf"
            apt = (
                runtime.root
                / "etc/apt/apt.conf.d/52remnawave-manager-unattended-upgrades"
            )
            sysctl.parent.mkdir(parents=True)
            apt.parent.mkdir(parents=True)
            old_sysctl = (
                "# Managed by remnawave-manager\n"
                "net.core.default_qdisc = fq_codel\n"
                "net.ipv4.tcp_congestion_control = cubic\n"
            )
            old_apt = (
                "// Managed by remnawave-manager\n"
                'APT::Periodic::Unattended-Upgrade "0";\n'
            )
            sysctl.write_text(old_sysctl, encoding="utf-8")
            apt.write_text(old_apt, encoding="utf-8")
            sysctl.chmod(0o600)
            apt.chmod(0o640)
            old_sysctl_mode = sysctl.stat().st_mode & 0o777
            old_apt_mode = apt.stat().st_mode & 0o777

            kernel = {"congestion": "cubic", "qdisc": "fq_codel"}
            units = {
                "apt-daily.timer": {"enabled": False, "active": False},
                "apt-daily-upgrade.timer": {"enabled": False, "active": False},
                "unattended-upgrades.service": {
                    "enabled": False,
                    "active": False,
                },
            }

            def run(args: list[str], **_kwargs: object) -> Result:
                if args[:3] == [
                    "sysctl",
                    "-n",
                    "net.ipv4.tcp_available_congestion_control",
                ]:
                    return Result(tuple(args), 0, "cubic bbr\n", "")
                if args[:3] == [
                    "sysctl",
                    "-n",
                    "net.ipv4.tcp_congestion_control",
                ]:
                    return Result(tuple(args), 0, f"{kernel['congestion']}\n", "")
                if args[:3] == ["sysctl", "-n", "net.core.default_qdisc"]:
                    return Result(tuple(args), 0, f"{kernel['qdisc']}\n", "")
                if args == ["sysctl", "--load", str(sysctl)]:
                    payload = sysctl.read_text(encoding="utf-8")
                    kernel["congestion"] = "bbr" if "= bbr" in payload else "cubic"
                    kernel["qdisc"] = "fq" if "= fq\n" in payload else "fq_codel"
                    return Result(tuple(args), 0, "", "")
                if args[:2] == ["sysctl", "-w"]:
                    name, value = args[2].split("=", 1)
                    key = "congestion" if name.endswith("congestion_control") else "qdisc"
                    kernel[key] = value
                    return Result(tuple(args), 0, "", "")
                if args[:2] == ["systemctl", "is-enabled"]:
                    unit = args[2]
                    code = 0 if units[unit]["enabled"] else 1
                    return Result(tuple(args), code, "enabled\n" if code == 0 else "disabled\n", "")
                if args[:2] == ["systemctl", "is-active"]:
                    unit = args[2]
                    code = 0 if units[unit]["active"] else 3
                    return Result(tuple(args), code, "active\n" if code == 0 else "inactive\n", "")
                if args == [
                    "systemctl",
                    "enable",
                    "--now",
                    "apt-daily.timer",
                    "apt-daily-upgrade.timer",
                ]:
                    for unit in args[3:]:
                        units[unit]["enabled"] = True
                        units[unit]["active"] = True
                    return Result(tuple(args), 0, "", "")
                if args == [
                    "systemctl",
                    "enable",
                    "--now",
                    "unattended-upgrades.service",
                ]:
                    units["unattended-upgrades.service"]["enabled"] = True
                    units["unattended-upgrades.service"]["active"] = True
                    raise RuntimeError("systemd failure")
                if args[0] == "systemctl" and args[1] in {
                    "enable",
                    "disable",
                    "start",
                    "stop",
                }:
                    action, unit = args[1], args[2]
                    if action in {"enable", "disable"}:
                        units[unit]["enabled"] = action == "enable"
                    else:
                        units[unit]["active"] = action == "start"
                    return Result(tuple(args), 0, "", "")
                return Result(tuple(args), 0, "", "")

            runner = mock.Mock()
            runner.run.side_effect = run

            with self.assertRaisesRegex(TransactionError, "исходное состояние восстановлено"):
                configure_host(runner, runtime)

            self.assertEqual(sysctl.read_text(encoding="utf-8"), old_sysctl)
            self.assertEqual(apt.read_text(encoding="utf-8"), old_apt)
            self.assertEqual(sysctl.stat().st_mode & 0o777, old_sysctl_mode)
            self.assertEqual(apt.stat().st_mode & 0o777, old_apt_mode)
            self.assertEqual(kernel, {"congestion": "cubic", "qdisc": "fq_codel"})
            self.assertFalse(units["apt-daily.timer"]["enabled"])
            self.assertFalse(units["apt-daily.timer"]["active"])
            self.assertFalse(units["apt-daily-upgrade.timer"]["enabled"])
            self.assertFalse(units["apt-daily-upgrade.timer"]["active"])
            self.assertFalse(units["unattended-upgrades.service"]["enabled"])
            self.assertFalse(units["unattended-upgrades.service"]["active"])
            commands = [call.args[0] for call in runner.run.call_args_list]
            self.assertIn(
                [
                    "sysctl",
                    "-w",
                    "net.ipv4.tcp_congestion_control=cubic",
                ],
                commands,
            )
            self.assertIn(
                ["sysctl", "-w", "net.core.default_qdisc=fq_codel"],
                commands,
            )

    def test_apply_aborts_when_original_unit_state_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = RuntimePaths(Path(temporary))
            runner = mock.Mock()

            def run(args: list[str], **_kwargs: object) -> Result:
                if args[:2] == ["sysctl", "-n"]:
                    values = {
                        "net.ipv4.tcp_congestion_control": "cubic\n",
                        "net.core.default_qdisc": "fq_codel\n",
                    }
                    return Result(tuple(args), 0, values.get(args[2], "cubic bbr\n"), "")
                if args[:2] == ["systemctl", "is-enabled"]:
                    return Result(tuple(args), 4, "", "unit not found")
                return Result(tuple(args), 3, "", "")

            runner.run.side_effect = run

            with self.assertRaisesRegex(ValidationError, "исходное состояние"):
                configure_host(runner, runtime)

            sysctl, apt = (
                runtime.root / "etc/sysctl.d/90-remnawave-manager-bbr.conf",
                runtime.root
                / "etc/apt/apt.conf.d/52remnawave-manager-unattended-upgrades",
            )
            self.assertFalse(sysctl.exists())
            self.assertFalse(apt.exists())
            self.assertFalse(any(call.args[0][0] == "modprobe" for call in runner.run.call_args_list))

    def test_status_rejects_drifted_managed_apt_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = RuntimePaths(Path(temporary))
            apt = (
                runtime.root
                / "etc/apt/apt.conf.d/52remnawave-manager-unattended-upgrades"
            )
            apt.parent.mkdir(parents=True)
            apt.write_text(
                "// Managed by remnawave-manager\n"
                'APT::Periodic::Unattended-Upgrade "0";\n',
                encoding="utf-8",
            )
            runner = mock.Mock()
            runner.run.side_effect = lambda args, **_kwargs: _successful_result(args)

            status = host_status(runner, runtime)

            self.assertFalse(status.unattended_configured)

    def test_status_reports_disabled_host_without_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runtime = RuntimePaths(Path(temporary))
            runner = mock.Mock()
            runner.run.return_value = Result(("check",), 1, "", "")

            status = host_status(runner, runtime)

            self.assertFalse(status.bbr_available)
            self.assertFalse(status.bbr_enabled)
            self.assertFalse(status.unattended_configured)
            self.assertFalse(status.apt_daily_timer_active)
            self.assertFalse(status.apt_upgrade_timer_active)
            self.assertFalse(status.unattended_service_enabled)


if __name__ == "__main__":
    unittest.main()
