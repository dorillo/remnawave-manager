from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from remnawave_manager.errors import ValidationError
from remnawave_manager.models import Inventory
from remnawave_manager.paths import RuntimePaths
from remnawave_manager.state import StateStore


def valid_inventory() -> dict[str, object]:
    return {
        "schema_version": 1,
        "role": "node",
        "install_dir": "/opt/remnanode",
        "compose_file": "/opt/remnanode/docker-compose.yml",
        "env_file": "/opt/remnanode/.env",
        "webserver": "nginx",
        "components": {
            "node": {
                "name": "node",
                "service": "remnanode",
                "container": "remnanode",
            }
        },
        "managed_files": [],
    }


class InventoryValidationTests(unittest.TestCase):
    def test_rejects_unsupported_schema_version(self) -> None:
        data = valid_inventory()
        data["schema_version"] = 2

        with self.assertRaises(ValueError):
            Inventory.from_dict(data)

    def test_component_collection_must_be_an_object(self) -> None:
        payload = valid_inventory()
        payload["components"] = []

        with self.assertRaises(TypeError):
            Inventory.from_dict(payload)

    def test_component_key_and_embedded_name_must_match(self) -> None:
        payload = valid_inventory()
        payload["components"] = {"node": {"name": "panel", "service": "remnanode"}}

        with self.assertRaises(ValueError):
            Inventory.from_dict(payload)

    def test_state_store_wraps_invalid_role_as_corrupt_inventory(self) -> None:
        payload = valid_inventory()
        payload["role"] = "combined"
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(RuntimePaths(Path(temporary) / "runtime"))
            store.save_settings({})
            store.paths.inventory.write_text(
                json.dumps(payload),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValidationError, "инвентаризации повреждён"):
                store.load_inventory()

    def test_rejects_relative_traversing_and_out_of_tree_core_paths(self) -> None:
        invalid_paths = (
            ("install_dir", "opt/remnanode"),
            ("install_dir", "/opt/../remnanode"),
            ("install_dir", "/"),
            ("compose_file", "/srv/docker-compose.yml"),
            ("compose_file", "/opt/remnanode/../docker-compose.yml"),
            ("env_file", "/srv/.env"),
        )
        for field, value in invalid_paths:
            payload = valid_inventory()
            payload[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                Inventory.from_dict(payload)

    def test_rejects_duplicate_and_relative_inventory_paths(self) -> None:
        for field, value in (
            ("nginx_files", ["/opt/remnanode/nginx.conf", "/opt/remnanode/nginx.conf"]),
            ("site_dirs", ["relative/site"]),
            ("xhttp_sockets", ["/run/xhttp.sock", "/run/./xhttp.sock"]),
            (
                "managed_files",
                [
                    {"path": "/opt/remnanode/.env", "sha256": "a" * 64, "kind": "env"},
                    {"path": "/opt/remnanode/.env", "sha256": "b" * 64, "kind": "env"},
                ],
            ),
        ):
            payload = valid_inventory()
            payload[field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                Inventory.from_dict(payload)

    def test_rejects_unsafe_component_and_warp_interface_names(self) -> None:
        payload = valid_inventory()
        payload["components"] = {
            "node": {"name": "node", "service": "--project-directory"}
        }
        with self.assertRaises(ValueError):
            Inventory.from_dict(payload)

        payload = valid_inventory()
        payload["warp_interfaces"] = ["../../etc"]
        with self.assertRaises(ValueError):
            Inventory.from_dict(payload)

    def test_rejects_unicode_control_and_format_characters(self) -> None:
        for field, value in (
            ("adopted_at", "2026-08-03\u0085spoof"),
            ("adopted_at", "2026-08-03\u202espoof"),
            ("features", {"warp\u2066spoof": True}),
        ):
            payload = valid_inventory()
            payload[field] = value

            with self.subTest(field=field, value=value), self.assertRaises(TypeError):
                Inventory.from_dict(payload)

    def test_state_store_refuses_to_persist_invalid_inventory(self) -> None:
        payload = valid_inventory()
        inventory = Inventory.from_dict(payload)
        inventory.compose_file = "relative-compose.yml"
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(RuntimePaths(Path(temporary) / "runtime"))
            with self.assertRaisesRegex(ValidationError, "некорректную инвентаризацию"):
                store.save_inventory(inventory)
            self.assertFalse(store.paths.inventory.exists())


if __name__ == "__main__":
    unittest.main()
