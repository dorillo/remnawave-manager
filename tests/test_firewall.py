from __future__ import annotations

import ipaddress
import json
import os
import shlex
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remnawave_manager.errors import CommandError, TransactionError, ValidationError
from remnawave_manager.firewall import (
    FirewallPaths,
    FirewallPlan,
    apply_firewall,
    apply_firewall_transactional,
    build_firewall_commands,
    configure_firewall,
    detect_ssh_ports,
    plan_firewall,
)
from remnawave_manager.runner import Result, Runner


def firewall_paths(root: Path, *, enabled: bool = False) -> FirewallPaths:
    etc_ufw = root / "etc" / "ufw"
    etc_default = root / "etc" / "default"
    etc_ufw.mkdir(parents=True)
    etc_default.mkdir(parents=True)
    paths = FirewallPaths(
        before_rules=etc_ufw / "before.rules",
        before6_rules=etc_ufw / "before6.rules",
        after_rules=etc_ufw / "after.rules",
        after6_rules=etc_ufw / "after6.rules",
        user_rules=etc_ufw / "user.rules",
        user6_rules=etc_ufw / "user6.rules",
        ufw_conf=etc_ufw / "ufw.conf",
        defaults=etc_default / "ufw",
    )
    for index, (_, path) in enumerate(paths.items()):
        payload = f"original-{index}\n"
        if path == paths.ufw_conf:
            payload = f"ENABLED={'yes' if enabled else 'no'}\n"
        path.write_text(payload, encoding="utf-8")
        os.chmod(path, 0o600)
    return paths


def firewall_plan() -> FirewallPlan:
    panel_ip = "203.0.113.10"
    return FirewallPlan(
        role="node",
        ssh_ports=(22022,),
        commands=tuple(
            tuple(command)
            for command in build_firewall_commands(
                "node",
                (22022,),
                panel_ip=panel_ip,
            )
        ),
        panel_ip=panel_ip,
    )


def ufw_rule_identity(rule: list[str]) -> tuple[str, ...]:
    return tuple(rule)


def ufw_insert_match_identity(rule: list[str]) -> tuple[str, ...]:
    normalized = [token for token in rule[1:] if token not in {"log", "log-all"}]
    if "comment" in normalized:
        index = normalized.index("comment")
        normalized = [*normalized[:index], *normalized[index + 2 :]]
    return tuple(normalized)


def apply_ufw_rule_command(rules: list[list[str]], command: tuple[str, ...]) -> None:
    if command[:2] == ("ufw", "insert"):
        position = int(command[2])
        if not rules or position > len(rules):
            raise CommandError(f"Invalid position '{position}'")
        candidate = list(command[3:])
        if ufw_insert_match_identity(candidate) not in {
            ufw_insert_match_identity(rule) for rule in rules
        }:
            rules.insert(position - 1, candidate)
    elif command[:3] == ("ufw", "--force", "delete"):
        target = ufw_rule_identity(list(command[3:]))
        rules.remove(next(rule for rule in rules if ufw_rule_identity(rule) == target))
    elif command[:2] in {("ufw", "allow"), ("ufw", "deny")}:
        candidate = list(command[1:])
        if ufw_rule_identity(candidate) not in {
            ufw_rule_identity(rule) for rule in rules
        }:
            rules.append(candidate)


def ufw_added_output(rules: list[list[str]]) -> str:
    return "Added user rules:\n" + "".join(
        f"ufw {shlex.join(rule)}\n" for rule in rules
    )


class UfwRunner:
    def __init__(
        self,
        paths: FirewallPaths,
        *,
        active: bool,
        fail_apply_at: int | None = None,
    ) -> None:
        self.paths = paths
        self.active = active
        self.fail_apply_at = fail_apply_at
        self.apply_calls = 0
        self.calls: list[tuple[str, ...]] = []
        self.rules: list[list[str]] = []

    def run(self, args, **kwargs):  # type: ignore[no-untyped-def]
        command = tuple(args)
        self.calls.append(command)
        if command == ("ufw", "status"):
            state = "active" if self.active else "inactive"
            return Result(command, 0, f"Status: {state}\n", "")
        if command == ("ufw", "show", "added"):
            return Result(command, 0, ufw_added_output(self.rules), "")
        if command[0] != "ufw":
            return Result(command, 0, "", "")

        self.apply_calls += 1
        if command in {("ufw", "reload"), ("ufw", "--force", "disable")}:
            if command[-1] == "disable":
                self.active = False
                self.paths.ufw_conf.write_text("ENABLED=no\n", encoding="utf-8")
            if self.fail_apply_at == self.apply_calls:
                self.fail_apply_at = None
                raise CommandError("ufw apply failed")
            return Result(command, 0, "", "")
        if "default" in command:
            self.paths.defaults.write_text(
                "DEFAULT_INPUT_POLICY=DROP\n", encoding="utf-8"
            )
            with self.paths.after_rules.open("a", encoding="utf-8") as stream:
                stream.write("manager-after-rule\n")
            with self.paths.after6_rules.open("a", encoding="utf-8") as stream:
                stream.write("manager-after6-rule\n")
        elif "enable" in command:
            self.active = True
            self.paths.ufw_conf.write_text("ENABLED=yes\n", encoding="utf-8")
        else:
            apply_ufw_rule_command(self.rules, command)
            with self.paths.user_rules.open("a", encoding="utf-8") as stream:
                stream.write("manager-rule\n")
            with self.paths.user6_rules.open("a", encoding="utf-8") as stream:
                stream.write("manager-rule6\n")
        if self.fail_apply_at == self.apply_calls:
            self.fail_apply_at = None
            raise CommandError("ufw apply failed")
        return Result(command, 0, "", "")


class FirewallStateRunner:
    def __init__(self) -> None:
        self.panel_ip = "203.0.113.10"
        self.foreign_ip = "198.51.100.99"
        self.rules: list[list[str]] = [
            [
                "allow",
                "from",
                self.panel_ip,
                "to",
                "any",
                "port",
                "2222",
                "proto",
                "tcp",
                "comment",
                "remnawave-manager:panel-api",
            ],
            [
                "deny",
                "to",
                "any",
                "port",
                "2222",
                "proto",
                "tcp",
                "comment",
                "remnawave-manager:node-api-deny",
            ],
            ["allow", "2222/tcp", "comment", "foreign:broad-node-api"],
        ]
        self.states: list[tuple[tuple[str, ...], bool, bool]] = []

    def run(self, args, **_kwargs):  # type: ignore[no-untyped-def]
        command = tuple(args)
        if command == ("ufw", "show", "added"):
            result = Result(command, 0, ufw_added_output(self.rules), "")
        else:
            apply_ufw_rule_command(self.rules, command)
            result = Result(command, 0, "", "")
        self.states.append(
            (
                command,
                self._allows_node_api(self.panel_ip),
                self._allows_node_api(self.foreign_ip),
            )
        )
        return result

    def _allows_node_api(self, source: str) -> bool:
        for rule in self.rules:
            if rule[0] not in {"allow", "deny"}:
                continue
            if "2222/tcp" not in rule:
                try:
                    port_index = rule.index("port")
                except ValueError:
                    continue
                if port_index + 1 >= len(rule) or rule[port_index + 1] != "2222":
                    continue
            if "from" in rule:
                source_index = rule.index("from")
                if source_index + 1 >= len(rule):
                    continue
                try:
                    source_matches = ipaddress.ip_address(source) in ipaddress.ip_network(
                        rule[source_index + 1], strict=False
                    )
                except ValueError:
                    source_matches = rule[source_index + 1] == source
                if not source_matches:
                    continue
            return rule[0] == "allow"
        return False


class FirewallPlanningTests(unittest.TestCase):
    def test_ufw_insert_duplicate_matching_ignores_action_log_and_comment(self) -> None:
        existing = [
            "allow",
            "from",
            "203.0.113.10",
            "to",
            "any",
            "port",
            "2222",
            "proto",
            "tcp",
            "comment",
            "remnawave-manager:panel-api",
        ]
        rules = [existing.copy()]

        apply_ufw_rule_command(
            rules,
            (
                "ufw",
                "insert",
                "1",
                "limit",
                "log",
                "from",
                "203.0.113.10",
                "to",
                "any",
                "port",
                "2222",
                "proto",
                "tcp",
                "comment",
                "remnawave-manager:transition-panel-api",
            ),
        )

        self.assertEqual(rules, [existing])

    def test_empty_ufw_is_seeded_before_first_positional_insert(self) -> None:
        runner = mock.Mock(spec=Runner)
        rules: list[list[str]] = []

        def run(args, **_kwargs):  # type: ignore[no-untyped-def]
            command = tuple(args)
            if command == ("ufw", "show", "added"):
                return Result(command, 0, ufw_added_output(rules), "")
            apply_ufw_rule_command(rules, command)
            return Result(command, 0, "", "")

        runner.run.side_effect = run

        apply_firewall(runner, firewall_plan())

        commands = [call.args[0] for call in runner.run.call_args_list]
        self.assertEqual(commands[0], ["ufw", "show", "added"])
        self.assertEqual(commands[1][:2], ["ufw", "deny"])
        self.assertEqual(commands[2][:3], ["ufw", "insert", "1"])
        self.assertEqual(commands[2][3], "deny")
        transition_commands = [
            command
            for command in commands
            if any("transition-node-api-deny" in token for token in command)
        ]
        self.assertEqual(len(transition_commands), 8)
        self.assertIn(list(firewall_plan().commands[-4]), commands)
        self.assertIn(list(firewall_plan().commands[-3]), commands)
        self.assertEqual(
            [rule[0] for rule in rules[:2]],
            ["allow", "deny"],
        )
        self.assertIn("remnawave-manager:panel-api", rules[0])
        self.assertIn("remnawave-manager:node-api-deny", rules[1])
        self.assertFalse(
            any("transition-" in token for rule in rules for token in rule)
        )

    def test_panel_plan_with_empty_ufw_uses_only_append_rules(self) -> None:
        runner = mock.Mock(spec=Runner)
        rules: list[list[str]] = []

        def run(args, **_kwargs):  # type: ignore[no-untyped-def]
            command = tuple(args)
            if command == ("ufw", "show", "added"):
                return Result(command, 0, ufw_added_output(rules), "")
            if command[:2] == ("ufw", "insert"):
                raise AssertionError("Panel firewall must not use positional inserts")
            apply_ufw_rule_command(rules, command)
            return Result(command, 0, "", "")

        runner.run.side_effect = run
        plan = FirewallPlan(
            role="panel",
            ssh_ports=(22,),
            commands=tuple(
                tuple(command)
                for command in build_firewall_commands("panel", (22,))
            ),
        )

        apply_firewall(runner, plan)

        commands = [call.args[0] for call in runner.run.call_args_list]
        self.assertEqual(commands[0], ["ufw", "show", "added"])
        self.assertFalse(
            any(command[:2] == ["ufw", "insert"] for command in commands)
        )
        self.assertIn(
            ["ufw", "allow", "22/tcp", "comment", "remnawave-manager:ssh"],
            commands,
        )

    def test_reapply_deletes_only_previous_manager_rules_before_new_rules(self) -> None:
        runner = mock.Mock(spec=Runner)
        rules = [
            ["allow", "22/tcp", "comment", "foreign:ssh"],
            ["allow", "22022/tcp", "comment", "remnawave-manager:ssh"],
            [
                "allow",
                "from",
                "198.51.100.7",
                "to",
                "any",
                "port",
                "2222",
                "proto",
                "tcp",
                "comment",
                "remnawave-manager:panel-api",
            ],
        ]

        def run(args, **_kwargs):  # type: ignore[no-untyped-def]
            command = tuple(args)
            if command == ("ufw", "show", "added"):
                return Result(command, 0, ufw_added_output(rules), "")
            apply_ufw_rule_command(rules, command)
            return Result(
                command,
                0,
                "",
                "",
            )

        runner.run.side_effect = run
        plan = firewall_plan()
        apply_firewall(runner, plan)

        commands = [call.args[0] for call in runner.run.call_args_list]
        deletions = [command for command in commands if "delete" in command]
        old_ssh_delete = [
            "ufw",
            "--force",
            "delete",
            "allow",
            "22022/tcp",
            "comment",
            "remnawave-manager:ssh",
        ]
        old_panel_delete = [
            "ufw",
            "--force",
            "delete",
            "allow",
            "from",
            "198.51.100.7",
            "to",
            "any",
            "port",
            "2222",
            "proto",
            "tcp",
            "comment",
            "remnawave-manager:panel-api",
        ]
        transition_specs = [
            [
                "deny",
                "from",
                network,
                "to",
                "any",
                "port",
                "2222",
                "proto",
                "tcp",
                "comment",
                f"remnawave-manager:transition-node-api-deny-{suffix}",
            ]
            for network, suffix in (
                ("0.0.0.0/1", "v4-low"),
                ("128.0.0.0/1", "v4-high"),
                ("::/1", "v6-low"),
                ("8000::/1", "v6-high"),
            )
        ]
        transition_setup = [
            ["ufw", "insert", "1", *specification]
            for specification in transition_specs
        ]
        transition_deletes = [
            ["ufw", "--force", "delete", *specification]
            for specification in reversed(transition_specs)
        ]
        self.assertEqual(
            deletions,
            [
                old_ssh_delete,
                old_panel_delete,
                *transition_deletes,
            ],
        )
        self.assertNotIn("foreign:ssh", " ".join(" ".join(item) for item in deletions))
        self.assertEqual(commands[0], ["ufw", "show", "added"])
        for command in transition_setup:
            self.assertIn(command, commands)
        self.assertLess(commands.index(transition_setup[-1]), commands.index(old_ssh_delete))
        self.assertLess(
            commands.index(old_panel_delete), commands.index(list(plan.commands[0]))
        )
        self.assertLess(
            commands.index(list(plan.commands[-1])),
            commands.index(transition_deletes[0]),
        )

    def test_node_api_rules_shadow_preexisting_broad_allow(self) -> None:
        commands = build_firewall_commands("node", (22022,), panel_ip="203.0.113.10")

        scoped_allow = [
            "ufw",
            "insert",
            "1",
            "allow",
            "from",
            "203.0.113.10",
            "to",
            "any",
            "port",
            "2222",
            "proto",
            "tcp",
            "comment",
            "remnawave-manager:panel-api",
        ]
        deny_other_sources = [
            "ufw",
            "insert",
            "2",
            "deny",
            "to",
            "any",
            "port",
            "2222",
            "proto",
            "tcp",
            "comment",
            "remnawave-manager:node-api-deny",
        ]
        self.assertIn(scoped_allow, commands)
        self.assertIn(deny_other_sources, commands)
        self.assertLess(
            commands.index(scoped_allow), commands.index(deny_other_sources)
        )

        rules = [["allow", "2222/tcp"]]
        for command in commands:
            if command[:2] == ["ufw", "insert"]:
                rules.insert(int(command[2]) - 1, command[3:])
        self.assertEqual(rules[:2], [scoped_allow[3:], deny_other_sources[3:]])
        self.assertEqual(rules[2], ["allow", "2222/tcp"])

    def test_node_reapply_never_exposes_api_during_any_ufw_command(self) -> None:
        runner = FirewallStateRunner()
        plan = firewall_plan()

        self.assertTrue(runner._allows_node_api(runner.panel_ip))
        self.assertFalse(runner._allows_node_api(runner.foreign_ip))

        apply_firewall(runner, plan)  # type: ignore[arg-type]

        for command, _panel_allowed, foreign_allowed in runner.states:
            with self.subTest(command=command):
                self.assertFalse(
                    foreign_allowed,
                    f"посторонний источник получил доступ после {shlex.join(command)}",
                )
        self.assertTrue(runner._allows_node_api(runner.panel_ip))
        self.assertFalse(runner._allows_node_api(runner.foreign_ip))
        comments = {
            rule[index + 1]
            for rule in runner.rules
            if "comment" in rule
            for index in (rule.index("comment"),)
        }
        self.assertIn("foreign:broad-node-api", comments)
        self.assertIn("remnawave-manager:panel-api", comments)
        self.assertIn("remnawave-manager:node-api-deny", comments)
        self.assertFalse(any("transition-" in comment for comment in comments))

    def test_node_rejects_ssh_on_reserved_api_port(self) -> None:
        with self.assertRaisesRegex(ValidationError, "зарезервирован для Node API"):
            build_firewall_commands("node", (2222,), panel_ip="203.0.113.10")

    def test_sshd_failure_never_falls_back_to_port_22_or_runs_ufw(self) -> None:
        runner = mock.Mock(spec=Runner)
        runner.run.return_value = Result(("sshd", "-T"), 1, "", "invalid config")

        with (
            mock.patch("remnawave_manager.firewall.command_exists", return_value=True),
            self.assertRaisesRegex(ValidationError, "UFW не изменён"),
        ):
            configure_firewall(runner, "panel")

        runner.run.assert_called_once()
        self.assertEqual(runner.run.call_args.args[0], ["sshd", "-T"])
        self.assertFalse(
            any(call.args[0][0] == "ufw" for call in runner.run.call_args_list)
        )

    def test_sshd_output_without_port_is_rejected(self) -> None:
        runner = mock.Mock(spec=Runner)
        runner.run.return_value = Result(
            ("sshd", "-T"),
            0,
            "permitrootlogin prohibit-password\n",
            "",
        )

        with self.assertRaisesRegex(ValidationError, "ни одного SSH-порта"):
            detect_ssh_ports(runner)

    def test_detects_every_valid_sshd_port_without_default(self) -> None:
        runner = mock.Mock(spec=Runner)
        runner.run.side_effect = (
            Result(
                ("sshd", "-T"),
                0,
                "port 22022\nport 22\nport 22022\n",
                "",
            ),
            Result(("systemctl", "is-active"), 3, "inactive\n", ""),
            Result(
                ("ss", "-H", "-ltn"),
                0,
                "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\nLISTEN 0 128 [::]:22022 [::]:*\n",
                "",
            ),
        )

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(detect_ssh_ports(runner), (22, 22022))
        self.assertEqual(runner.run.call_args_list[0].kwargs["env"]["LC_ALL"], "C")

    def test_auto_detection_rejects_unverified_current_session_port(
        self,
    ) -> None:
        runner = mock.Mock(spec=Runner)
        runner.run.side_effect = (
            Result(("sshd", "-T"), 0, "port 22\n", ""),
            Result(("systemctl", "is-active"), 0, "active\n", ""),
            Result(
                ("systemctl", "show"),
                0,
                "[::]:22022 (Stream) 0.0.0.0:22022 (Stream)\n",
                "",
            ),
            Result(
                ("ss", "-H", "-ltn"),
                0,
                "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n"
                "LISTEN 0 128 [::]:22022 [::]:*\n"
                "LISTEN 0 128 192.0.2.10:2222 0.0.0.0:*\n",
                "",
            ),
        )

        with (
            mock.patch.dict(
                os.environ,
                {"SSH_CONNECTION": "198.51.100.20 54321 192.0.2.10 2222"},
                clear=True,
            ),
            self.assertRaisesRegex(ValidationError, "нет в проверенной конфигурации"),
        ):
            detect_ssh_ports(runner)

    def test_auto_detection_rejects_configured_port_that_is_not_listening(self) -> None:
        runner = mock.Mock(spec=Runner)
        runner.run.side_effect = (
            Result(("sshd", "-T"), 0, "port 22022\n", ""),
            Result(("systemctl", "is-active"), 3, "inactive\n", ""),
            Result(
                ("ss", "-H", "-ltn"),
                0,
                "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n",
                "",
            ),
        )

        with (
            mock.patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(ValidationError, "не слушаются: 22022"),
        ):
            detect_ssh_ports(runner)

    def test_explicit_port_checks_listener_without_running_sshd(self) -> None:
        runner = mock.Mock(spec=Runner)
        runner.run.return_value = Result(
            ("ss", "-H", "-ltn"),
            0,
            "LISTEN 0 128 0.0.0.0:22022 0.0.0.0:*\n",
            "",
        )

        with (
            mock.patch(
                "remnawave_manager.firewall.command_exists",
                return_value=True,
            ),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            plan = plan_firewall(
                runner,
                "node",
                panel_ip="203.0.113.10",
                ssh_ports=(22022,),
            )

        runner.run.assert_called_once_with(
            ["ss", "-H", "-ltn"], check=False, timeout=30
        )
        self.assertEqual(plan.ssh_ports, (22022,))
        runner.reset_mock()
        rules: list[list[str]] = []

        def apply_run(args, **_kwargs):  # type: ignore[no-untyped-def]
            command = tuple(args)
            if command == ("ufw", "show", "added"):
                return Result(command, 0, ufw_added_output(rules), "")
            apply_ufw_rule_command(rules, command)
            return Result(command, 0, "", "")

        runner.run.side_effect = apply_run
        apply_firewall(runner, plan)
        commands = [call.args[0] for call in runner.run.call_args_list]
        self.assertEqual(commands[0], ["ufw", "show", "added"])
        self.assertEqual(commands[1][:2], ["ufw", "deny"])
        self.assertEqual(commands[2][:3], ["ufw", "insert", "1"])
        self.assertEqual(commands[5][:3], ["ufw", "allow", "22022/tcp"])
        self.assertEqual(commands[-7], ["ufw", "--force", "enable"])
        self.assertEqual(commands[-6], ["ufw", "reload"])
        for command in commands[-5:-1]:
            self.assertEqual(command[:4], ["ufw", "--force", "delete", "deny"])
        self.assertEqual(commands[-1], ["ufw", "show", "added"])

    def test_manual_node_plan_without_panel_ip_is_rejected_before_ufw(self) -> None:
        runner = mock.Mock(spec=Runner)
        plan = FirewallPlan(
            role="node",
            ssh_ports=(22022,),
            commands=tuple(
                tuple(command)
                for command in build_firewall_commands(
                    "node",
                    (22022,),
                    panel_ip="203.0.113.10",
                )
            ),
        )

        with self.assertRaisesRegex(ValidationError, "IP-адрес Panel"):
            apply_firewall(runner, plan)

        runner.run.assert_not_called()

    def test_explicit_port_must_already_be_listening(self) -> None:
        runner = mock.Mock(spec=Runner)
        runner.run.return_value = Result(
            ("ss", "-H", "-ltn"),
            0,
            "LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n",
            "",
        )

        with (
            mock.patch("remnawave_manager.firewall.command_exists", return_value=True),
            mock.patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(ValidationError, "не слушаются: 22022"),
        ):
            plan_firewall(runner, "panel", ssh_ports=(22022,))

        runner.run.assert_called_once_with(
            ["ss", "-H", "-ltn"], check=False, timeout=30
        )

    def test_explicit_ports_cannot_exclude_current_ssh_session(self) -> None:
        runner = mock.Mock(spec=Runner)

        with (
            mock.patch("remnawave_manager.firewall.command_exists", return_value=True),
            mock.patch.dict(
                os.environ,
                {"SSH_CONNECTION": "198.51.100.20 54321 192.0.2.10 22022"},
                clear=True,
            ),
            self.assertRaisesRegex(ValidationError, "не указан в --ssh-port"),
        ):
            plan_firewall(runner, "panel", ssh_ports=(22,))

        runner.run.assert_not_called()

    def test_manual_firewall_apply_uses_transaction_and_commits_it(self) -> None:
        runner = mock.Mock(spec=Runner)
        runner.run.return_value = Result(
            ("ss", "-H", "-ltn"),
            0,
            "LISTEN 0 128 0.0.0.0:22022 0.0.0.0:*\n",
            "",
        )
        transaction = mock.Mock()

        with (
            mock.patch("remnawave_manager.firewall.command_exists", return_value=True),
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch(
                "remnawave_manager.firewall.apply_firewall_transactional",
                return_value=transaction,
            ) as apply_transaction,
        ):
            ports = configure_firewall(
                runner,
                "panel",
                ssh_ports=(22022,),
                transaction_root=Path(
                    "/var/lib/remnawave-manager/firewall-transactions"
                ),
            )

        self.assertEqual(ports, (22022,))
        runner.run.assert_called_once_with(
            ["ss", "-H", "-ltn"], check=False, timeout=30
        )
        apply_transaction.assert_called_once()
        transaction.commit.assert_called_once_with()


class FirewallTransactionTests(unittest.TestCase):
    def test_panel_install_enables_empty_inactive_ufw_without_insert(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = firewall_paths(root, enabled=False)
            runner = UfwRunner(paths, active=False)
            plan = FirewallPlan(
                role="panel",
                ssh_ports=(22,),
                commands=tuple(
                    tuple(command)
                    for command in build_firewall_commands("panel", (22,))
                ),
            )

            transaction = apply_firewall_transactional(
                runner,  # type: ignore[arg-type]
                plan,
                transaction_root=root / "transactions",
                paths=paths,
            )

            self.assertTrue(runner.active)
            self.assertFalse(
                any(command[:2] == ("ufw", "insert") for command in runner.calls)
            )
            transaction.commit()
            self.assertEqual(list((root / "transactions").iterdir()), [])

    def test_every_apply_failure_restores_exact_files_modes_and_runtime(self) -> None:
        plan = firewall_plan()
        apply_command_count = len(plan.commands) + 4

        for active in (False, True):
            for fail_apply_at in range(1, apply_command_count + 1):
                with (
                    self.subTest(active=active, fail_apply_at=fail_apply_at),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    root = Path(temporary)
                    paths = firewall_paths(root, enabled=active)
                    original = {
                        path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                        for _, path in paths.items()
                    }
                    runner = UfwRunner(
                        paths,
                        active=active,
                        fail_apply_at=fail_apply_at,
                    )
                    transaction_root = root / "transactions"

                    with self.assertRaisesRegex(
                        TransactionError,
                        "точное исходное состояние восстановлено",
                    ):
                        apply_firewall_transactional(
                            runner,  # type: ignore[arg-type]
                            plan,
                            transaction_root=transaction_root,
                            paths=paths,
                        )

                    self.assertEqual(runner.active, active)
                    for path, (payload, mode) in original.items():
                        self.assertEqual(path.read_bytes(), payload)
                        self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
                    self.assertEqual(list(transaction_root.iterdir()), [])

    def test_partial_apply_restores_exact_inactive_files_and_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = firewall_paths(root)
            original = {
                path: (path.read_bytes(), path.stat().st_mode & 0o777)
                for _, path in paths.items()
            }
            runner = UfwRunner(paths, active=False, fail_apply_at=2)
            transaction_root = root / "transactions"

            with self.assertRaisesRegex(
                TransactionError, "точное исходное состояние восстановлено"
            ):
                apply_firewall_transactional(
                    runner,  # type: ignore[arg-type]
                    firewall_plan(),
                    transaction_root=transaction_root,
                    paths=paths,
                )

            self.assertFalse(runner.active)
            for path, (payload, mode) in original.items():
                self.assertEqual(path.read_bytes(), payload)
                self.assertEqual(path.stat().st_mode & 0o777, mode)
            self.assertEqual(list(transaction_root.glob("ufw-*")), [])

    def test_late_rollback_restores_active_rules_and_uses_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = firewall_paths(root, enabled=True)
            original = {path: path.read_bytes() for _, path in paths.items()}
            runner = UfwRunner(paths, active=True)

            transaction = apply_firewall_transactional(
                runner,  # type: ignore[arg-type]
                firewall_plan(),
                transaction_root=root / "transactions",
                paths=paths,
            )
            manifest = json.loads(
                (transaction.artifact_path / "manifest.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertTrue(manifest["active"])
            self.assertTrue(manifest["enabled"])
            self.assertEqual(
                {item["path"] for item in manifest["files"]},
                {str(path) for _, path in paths.items()},
            )
            transaction.rollback()

            self.assertTrue(runner.active)
            self.assertIn(("ufw", "reload"), runner.calls)
            for path, payload in original.items():
                self.assertEqual(path.read_bytes(), payload)
            self.assertFalse(transaction.artifact_path.exists())

    def test_runtime_and_ufw_conf_must_agree_before_apply(self) -> None:
        for active, enabled in ((True, False), (False, True)):
            with (
                self.subTest(active=active, enabled=enabled),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                paths = firewall_paths(root, enabled=enabled)
                runner = UfwRunner(paths, active=active)
                transaction_root = root / "transactions"

                with self.assertRaisesRegex(ValidationError, "не согласованы"):
                    apply_firewall_transactional(
                        runner,  # type: ignore[arg-type]
                        firewall_plan(),
                        transaction_root=transaction_root,
                        paths=paths,
                    )

                self.assertEqual(runner.apply_calls, 0)
                self.assertEqual(
                    runner.calls,
                    [("ufw", "status"), ("ufw", "status")],
                )
                self.assertEqual(list(transaction_root.glob("ufw-*")), [])

    def test_unsafe_ufw_file_type_fails_before_rule_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = firewall_paths(root)
            paths.user6_rules.unlink()
            paths.user6_rules.mkdir()
            runner = UfwRunner(paths, active=False)

            with self.assertRaisesRegex(ValidationError, "небезопасный тип"):
                apply_firewall_transactional(
                    runner,  # type: ignore[arg-type]
                    firewall_plan(),
                    transaction_root=root / "transactions",
                    paths=paths,
                )

            self.assertEqual(runner.apply_calls, 0)
            self.assertEqual(runner.calls, [("ufw", "status")])

    def test_hardlinked_ufw_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = firewall_paths(root)
            paths.user6_rules.unlink()
            try:
                os.link(paths.user_rules, paths.user6_rules)
            except OSError as error:
                self.skipTest(f"hardlinks unavailable: {error}")
            runner = UfwRunner(paths, active=False)

            with self.assertRaisesRegex(ValidationError, "hardlink"):
                apply_firewall_transactional(
                    runner,  # type: ignore[arg-type]
                    firewall_plan(),
                    transaction_root=root / "transactions",
                    paths=paths,
                )

            self.assertEqual(runner.apply_calls, 0)

    def test_symlinked_ufw_file_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = firewall_paths(root)
            paths.defaults.unlink()
            try:
                paths.defaults.symlink_to(paths.user_rules)
            except OSError as error:
                self.skipTest(f"symbolic links unavailable: {error}")
            runner = UfwRunner(paths, active=False)

            with self.assertRaisesRegex(ValidationError, "небезопасный тип"):
                apply_firewall_transactional(
                    runner,  # type: ignore[arg-type]
                    firewall_plan(),
                    transaction_root=root / "transactions",
                    paths=paths,
                )

            self.assertEqual(runner.apply_calls, 0)

    def test_incomplete_rollback_keeps_active_firewall_and_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = firewall_paths(root)
            runner = UfwRunner(paths, active=False)
            transaction = apply_firewall_transactional(
                runner,  # type: ignore[arg-type]
                firewall_plan(),
                transaction_root=root / "transactions",
                paths=paths,
            )
            paths.user_rules.unlink()
            paths.user_rules.mkdir()

            with self.assertRaisesRegex(ValidationError, "небезопасный тип"):
                transaction.rollback()

            self.assertTrue(runner.active)
            self.assertTrue(transaction.artifact_path.is_dir())
            self.assertTrue((transaction.artifact_path / "manifest.json").is_file())
            with self.assertRaisesRegex(
                ValidationError, "незавершённая UFW-транзакция"
            ):
                apply_firewall_transactional(
                    runner,  # type: ignore[arg-type]
                    firewall_plan(),
                    transaction_root=root / "transactions",
                    paths=paths,
                )


if __name__ == "__main__":
    unittest.main()
