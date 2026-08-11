from __future__ import annotations

import json
import unittest

from remnawave_manager.compat import (
    component_target,
    detect_component_version,
    require_supported_source,
)
from remnawave_manager.errors import ValidationError
from remnawave_manager.models import Component
from remnawave_manager.runner import Result


class FakeRunner:
    def __init__(
        self,
        repo_digests: list[str] | None,
        *,
        returncode: int = 0,
        container_data: dict[str, object] | None = None,
        daemon_returncode: int = 0,
    ) -> None:
        self.repo_digests = repo_digests
        self.returncode = returncode
        self.container_data = container_data
        self.daemon_returncode = daemon_returncode
        self.calls: list[tuple[str, ...]] = []

    def run(self, args, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(tuple(args))
        if args[:2] == ["docker", "inspect"]:
            if self.container_data is None:
                return Result(tuple(args), 1, "", "No such container")
            return Result(tuple(args), 0, json.dumps(self.container_data), "")
        if args[:2] == ["docker", "info"]:
            return Result(
                tuple(args),
                self.daemon_returncode,
                json.dumps("27.5.1") if self.daemon_returncode == 0 else "",
                "",
            )
        stdout = json.dumps(self.repo_digests) if self.repo_digests is not None else ""
        return Result(tuple(args), self.returncode, stdout, "")


class CompatibilityTests(unittest.TestCase):
    def test_legacy_postgres_manifest_digest_remains_an_approved_source(self) -> None:
        digest = "3a82e1f56c8f0f5616a11103ac3d47e632c3938698946a7ad26da0df1334744a"
        component = Component(
            name="database",
            service="remnawave-db",
            configured_image="postgres:18.4@sha256:" + digest,
            running_image="postgres:18.4@sha256:" + digest,
            running_image_id="sha256:" + "d" * 64,
        )
        runner = FakeRunner(
            [],
            container_data={
                "Config": {"Image": component.running_image},
                "Image": component.running_image_id,
            },
        )

        self.assertEqual(require_supported_source(runner, "database", component), "18.4")

    def test_panel_target_is_3_2_3_and_identical_in_both_registries(self) -> None:
        expected_digest = (
            "sha256:bee71b9c3974e24007de4c13efd4aa6d5ec04b7fbf97cbe81095faac075a41b4"
        )

        docker_hub = component_target("panel", "docker-hub")
        ghcr = component_target("panel", "ghcr")

        self.assertEqual(
            docker_hub,
            {
                "version": "3.2.3",
                "image": "remnawave/backend:3.2.3",
                "digest": expected_digest,
            },
        )
        self.assertEqual(
            ghcr,
            {
                "version": "3.2.3",
                "image": "ghcr.io/remnawave/backend:3.2.3",
                "digest": expected_digest,
            },
        )

    def test_node_target_is_3_1_1_and_identical_in_both_registries(self) -> None:
        expected_digest = (
            "sha256:85849e3255250b5b60000ecffc1470a7bee7edf634497ee2c91d531b194fa8eb"
        )

        self.assertEqual(
            component_target("node", "docker-hub"),
            {
                "version": "3.1.1",
                "image": "remnawave/node:3.1.1",
                "digest": expected_digest,
            },
        )
        self.assertEqual(
            component_target("node", "ghcr"),
            {
                "version": "3.1.1",
                "image": "ghcr.io/remnawave/node:3.1.1",
                "digest": expected_digest,
            },
        )

    def test_managed_panel_3_1_0_is_an_approved_update_source(self) -> None:
        component = Component(
            name="panel",
            service="remnawave",
            configured_image="remnawave/backend:3.1.0",
            running_image="remnawave/backend:3.1.0",
            running_image_id="sha256:" + "3" * 64,
        )
        runner = FakeRunner(
            [
                (
                    "remnawave/backend@sha256:"
                    "a9b5bf76a136d552f72e953baee549b2f3dc6bd30c2ff2936f64cb72db9a2587"
                )
            ],
            container_data={
                "Config": {"Image": "remnawave/backend:3.1.0"},
                "Image": "sha256:" + "3" * 64,
            },
        )

        self.assertEqual(require_supported_source(runner, "panel", component), "3.1.0")

    def test_managed_panel_3_2_0_is_an_approved_update_source(self) -> None:
        component = Component(
            name="panel",
            service="remnawave",
            configured_image="remnawave/backend:3.2.0",
            running_image="remnawave/backend:3.2.0",
            running_image_id="sha256:" + "4" * 64,
        )
        runner = FakeRunner(
            [
                (
                    "remnawave/backend@sha256:"
                    "72af06c106111db7cd11f67da63cee591a4a1c60fc1d1ad57384c498c1e9fa40"
                )
            ],
            container_data={
                "Config": {"Image": "remnawave/backend:3.2.0"},
                "Image": "sha256:" + "4" * 64,
            },
        )

        self.assertEqual(require_supported_source(runner, "panel", component), "3.2.0")

    def test_managed_panel_3_2_2_is_an_approved_update_source(self) -> None:
        component = Component(
            name="panel",
            service="remnawave",
            configured_image="remnawave/backend:3.2.2",
            running_image="remnawave/backend:3.2.2",
            running_image_id="sha256:" + "5" * 64,
        )
        runner = FakeRunner(
            [
                (
                    "remnawave/backend@sha256:"
                    "44607a941eb1343a3975e5cc77b65207c597c3af4d00b80e4e32ebd48e73abd5"
                )
            ],
            container_data={
                "Config": {"Image": "remnawave/backend:3.2.2"},
                "Image": "sha256:" + "5" * 64,
            },
        )

        self.assertEqual(require_supported_source(runner, "panel", component), "3.2.2")

    def test_managed_node_3_1_0_is_an_approved_update_source(self) -> None:
        component = Component(
            name="node",
            service="remnanode",
            configured_image="remnawave/node:3.1.0",
            running_image="remnawave/node:3.1.0",
            running_image_id="sha256:" + "6" * 64,
        )
        runner = FakeRunner(
            [
                (
                    "remnawave/node@sha256:"
                    "7a71bebdd18fd25a7035ad67d83f56ea660904bb1b8da8767f4f8ebc2b05870e"
                )
            ],
            container_data={
                "Config": {"Image": "remnawave/node:3.1.0"},
                "Image": "sha256:" + "6" * 64,
            },
        )

        self.assertEqual(require_supported_source(runner, "node", component), "3.1.0")

    def test_detects_old_panel_behind_mutable_major_tag_by_digest(self) -> None:
        component = Component(
            name="panel",
            service="remnawave",
            configured_image="remnawave/backend:2",
            running_image="remnawave/backend:2",
            running_image_id="sha256:" + "1" * 64,
        )
        runner = FakeRunner(
            [
                (
                    "remnawave/backend@sha256:"
                    "361f9bb0b183d4fcefea2f1f7163db490e2aa1ec3b4bdde016a9ab9229ce956b"
                )
            ],
            container_data={
                "Config": {"Image": "remnawave/backend:2"},
                "Image": "sha256:" + "1" * 64,
            },
        )

        self.assertEqual(detect_component_version(runner, "panel", component), "2.8.1")

    def test_exact_supported_tag_is_not_proof_for_offline_import(self) -> None:
        component = Component(
            name="node",
            service="remnanode",
            configured_image="remnawave/node:2.8.0",
        )
        with self.assertRaises(ValidationError):
            require_supported_source(
                FakeRunner(None, returncode=1),
                "node",
                component,
            )
        self.assertIsNone(
            require_supported_source(
                FakeRunner(None, returncode=1),
                "node",
                component,
                accept_unknown=True,
            )
        )

    def test_unknown_source_requires_explicit_override(self) -> None:
        component = Component(
            name="node",
            service="remnanode",
            configured_image="remnawave/node:2",
        )
        runner = FakeRunner(["remnawave/node@sha256:" + "f" * 64])

        with self.assertRaises(ValidationError):
            require_supported_source(runner, "node", component)
        self.assertIsNone(
            require_supported_source(runner, "node", component, accept_unknown=True)
        )

    def test_supported_tag_from_untrusted_repository_is_not_accepted(self) -> None:
        component = Component(
            name="node",
            service="remnanode",
            configured_image="example.invalid/remnawave/node:2.8.0",
        )

        with self.assertRaises(ValidationError):
            require_supported_source(
                FakeRunner(None, returncode=1),
                "node",
                component,
            )

    def test_unknown_running_image_is_not_hidden_by_supported_compose_image(self) -> None:
        component = Component(
            name="node",
            service="remnanode",
            configured_image="remnawave/node:2.8.0",
            running_image="example.invalid/custom-node:unknown",
            running_image_id="sha256:" + "2" * 64,
        )

        with self.assertRaises(ValidationError):
            require_supported_source(
                FakeRunner(
                    ["example.invalid/custom-node@sha256:" + "f" * 64],
                    container_data={
                        "Config": {"Image": "example.invalid/custom-node:unknown"},
                        "Image": "sha256:" + "2" * 64,
                    },
                ),
                "node",
                component,
            )

    def test_unknown_running_digest_is_not_accepted_from_supported_runtime_tag(self) -> None:
        component = Component(
            name="node",
            service="remnanode",
            configured_image="remnawave/node:2.8.0",
            running_image="remnawave/node:2.8.0",
            running_image_id="sha256:" + "2" * 64,
        )

        with self.assertRaises(ValidationError):
            require_supported_source(
                FakeRunner(
                    ["remnawave/node@sha256:" + "f" * 64],
                    container_data={
                        "Config": {"Image": "remnawave/node:2.8.0"},
                        "Image": "sha256:" + "2" * 64,
                    },
                ),
                "node",
                component,
            )

    def test_unknown_explicit_digest_is_not_accepted_from_its_supported_tag(self) -> None:
        component = Component(
            name="node",
            service="remnanode",
            configured_image="remnawave/node:2.8.0@sha256:" + "f" * 64,
        )

        with self.assertRaises(ValidationError):
            require_supported_source(
                FakeRunner(["remnawave/node@sha256:" + "f" * 64]),
                "node",
                component,
            )

    def test_changed_live_image_id_is_rejected_before_digest_lookup(self) -> None:
        component = Component(
            name="node",
            service="remnanode",
            container="remnanode",
            configured_image="remnawave/node:2.8.0",
            running_image="remnawave/node:2.8.0",
            running_image_id="sha256:" + "1" * 64,
            status="running",
        )
        runner = FakeRunner(
            [
                "remnawave/node@sha256:03f14935751b4ab565181e2b1766ccd1a9ac349d6839acd3ee49014e543fa232"
            ],
            container_data={
                "Config": {"Image": "remnawave/node:2.8.0"},
                "Image": "sha256:" + "2" * 64,
            },
        )

        with self.assertRaisesRegex(ValidationError, "изменился после adoption"):
            require_supported_source(runner, "node", component)

        self.assertFalse(any(call[:3] == ("docker", "image", "inspect") for call in runner.calls))

    def test_live_container_is_used_when_inventory_was_adopted_while_stopped(self) -> None:
        digest = "03f14935751b4ab565181e2b1766ccd1a9ac349d6839acd3ee49014e543fa232"
        component = Component(
            name="node",
            service="remnanode",
            configured_image="remnawave/node:2.8.0",
            status="not-created",
        )
        runner = FakeRunner(
            [f"remnawave/node@sha256:{digest}"],
            container_data={
                "Config": {"Image": "remnawave/node:2.8.0"},
                "Image": "sha256:" + "3" * 64,
            },
        )

        self.assertEqual(require_supported_source(runner, "node", component), "2.8.0")
        self.assertIn(
            ("docker", "image", "inspect", "--format", "{{json .RepoDigests}}", "sha256:" + "3" * 64),
            runner.calls,
        )

    def test_missing_container_does_not_hide_daemon_outage(self) -> None:
        component = Component(
            name="node",
            service="remnanode",
            configured_image=(
                "remnawave/node:2.8.0@sha256:"
                "03f14935751b4ab565181e2b1766ccd1a9ac349d6839acd3ee49014e543fa232"
            ),
            status="not-created",
        )

        with self.assertRaisesRegex(ValidationError, "Docker daemon недоступен"):
            require_supported_source(
                FakeRunner(None, daemon_returncode=1),
                "node",
                component,
            )


if __name__ == "__main__":
    unittest.main()
