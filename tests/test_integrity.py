from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from remnawave_manager.errors import ValidationError
from remnawave_manager.integrity import configuration_drift, snapshot_hashes
from remnawave_manager.models import Inventory, ManagedFile


def inventory(files: list[ManagedFile]) -> Inventory:
    return Inventory(
        schema_version=1,
        role="node",
        install_dir="/opt/remnanode",
        compose_file="/opt/remnanode/docker-compose.yml",
        env_file=None,
        webserver=None,
        managed_files=files,
    )


class IntegritySecurityTests(unittest.TestCase):
    def test_duplicate_managed_target_is_reported_and_snapshot_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config"
            path.write_bytes(b"value")
            checksum = hashlib.sha256(b"value").hexdigest()
            current = inventory(
                [
                    ManagedFile(str(path), checksum, "config"),
                    ManagedFile(str(path), checksum, "config"),
                ]
            )

            self.assertTrue(
                any("повторяется" in item for item in configuration_drift(current))
            )
            with self.assertRaises(ValidationError):
                snapshot_hashes(current)

    def test_relative_path_is_drift_without_reading_cwd(self) -> None:
        current = inventory([ManagedFile("relative.env", "0" * 64, "env")])
        self.assertEqual(
            configuration_drift(current),
            ["относительный managed-путь: relative.env"],
        )

    def test_snapshot_fails_closed_for_missing_managed_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            missing = Path(temporary) / "missing.conf"
            current = inventory(
                [ManagedFile(str(missing), "a" * 64, "nginx")]
            )

            with self.assertRaises(ValidationError):
                snapshot_hashes(current)


if __name__ == "__main__":
    unittest.main()
