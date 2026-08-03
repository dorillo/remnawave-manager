from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from remnawave_manager.errors import ValidationError
from remnawave_manager.journal import TransactionJournal
from remnawave_manager.paths import RuntimePaths
from remnawave_manager.state import StateStore


class TransactionJournalTests(unittest.TestCase):
    def test_existing_transaction_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(RuntimePaths(Path(temporary) / "runtime"))
            store.initialize()
            path = store.paths.state / "active-transaction.json"
            original = '{"operation":"previous","phase":"migrating"}\n'
            path.write_text(original, encoding="utf-8")

            with self.assertRaisesRegex(ValidationError, "незавершённая транзакция"):
                TransactionJournal(
                    store,
                    "node-update",
                    Path(temporary) / "backup/new.tar.gz",
                )

            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_journal_refuses_to_delete_a_replaced_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(RuntimePaths(Path(temporary) / "runtime"))
            journal = TransactionJournal(
                store,
                "panel-update",
                Path(temporary) / "backup/panel.tar.gz",
            )
            path = store.paths.state / "active-transaction.json"
            replacement = {
                "transaction_id": "another-operation",
                "operation": "node-update",
                "backup": "/backup/node.tar.gz",
                "phase": "started",
            }
            path.write_text(json.dumps(replacement), encoding="utf-8")

            with self.assertRaisesRegex(ValidationError, "заменён"):
                journal.complete()

            self.assertTrue(path.exists())

    def test_journal_rejects_non_object_invalid_utf8_and_oversized_payloads(
        self,
    ) -> None:
        replacements = (
            b"[]\n",
            b"\xff\xfe\n",
            b"{" + b" " * (64 * 1024) + b"}\n",
        )
        for replacement in replacements:
            with (
                self.subTest(size=len(replacement)),
                tempfile.TemporaryDirectory() as temporary,
            ):
                store = StateStore(RuntimePaths(Path(temporary) / "runtime"))
                journal = TransactionJournal(store, "panel-update")
                journal.path.write_bytes(replacement)
                if os.name == "posix":
                    journal.path.chmod(0o600)

                with self.assertRaisesRegex(ValidationError, "повреждён или подменён"):
                    journal.phase("stopping-applications")

    @unittest.skipUnless(hasattr(os, "link"), "hard links are unavailable")
    def test_journal_rejects_hardlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(RuntimePaths(Path(temporary) / "runtime"))
            journal = TransactionJournal(store, "node-update")
            try:
                os.link(journal.path, journal.path.with_name("journal-hardlink.json"))
            except OSError as error:
                self.skipTest(f"hard links unavailable: {error}")

            with self.assertRaisesRegex(ValidationError, "повреждён или подменён"):
                journal.complete()

    @unittest.skipUnless(os.name == "posix", "POSIX modes are unavailable")
    def test_journal_rejects_insecure_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(RuntimePaths(Path(temporary) / "runtime"))
            journal = TransactionJournal(store, "node-update")
            journal.path.chmod(0o644)

            with self.assertRaisesRegex(ValidationError, "повреждён или подменён"):
                journal.complete()

    def test_backup_can_be_bound_once_after_transaction_start(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(RuntimePaths(Path(temporary) / "runtime"))
            journal = TransactionJournal(store, "panel-update")
            self.assertNotIn(
                "backup", json.loads(journal.path.read_text(encoding="utf-8"))
            )

            backup = Path(temporary) / "backup.tar.gz"
            journal.set_backup(backup)

            saved = json.loads(journal.path.read_text(encoding="utf-8"))
            self.assertEqual(saved["backup"], str(backup))
            with self.assertRaisesRegex(ValidationError, "уже привязан"):
                journal.set_backup(Path(temporary) / "another.tar.gz")

    def test_running_services_snapshot_is_validated_sorted_and_bound_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(RuntimePaths(Path(temporary) / "runtime"))
            journal = TransactionJournal(store, "panel-update")

            journal.set_running_services({"remnawave-subscription", "remnawave"})

            saved = json.loads(journal.path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved["running_services"],
                ["remnawave", "remnawave-subscription"],
            )
            with self.assertRaisesRegex(ValidationError, "уже записан"):
                journal.set_running_services(set())

    def test_running_services_snapshot_rejects_untrusted_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            store = StateStore(RuntimePaths(Path(temporary) / "runtime"))
            journal = TransactionJournal(store, "panel-update")

            with self.assertRaisesRegex(ValidationError, "некорректные имена"):
                journal.set_running_services({"--project-directory"})

    def test_archive_metadata_is_validated_sorted_and_bound_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(RuntimePaths(root / "runtime"))
            journal = TransactionJournal(store, "stack-archive")
            install = root / "opt/remnanode"
            archived_install = root / "opt/remnanode.removed-fixed"
            inventory = store.paths.inventory
            archived_inventory = store.paths.state / "inventory.removed-fixed.json"
            secrets = store.paths.secrets
            archived_secrets = store.paths.etc / "secrets.removed-fixed.json"

            journal.set_archive_metadata(
                install_directory=(install, archived_install),
                inventory=(inventory, archived_inventory),
                secrets=(secrets, archived_secrets),
                created_services=frozenset({"stopped-worker", "remnanode"}),
                running_services=frozenset({"remnanode"}),
            )

            saved = json.loads(journal.path.read_text(encoding="utf-8"))
            self.assertEqual(
                saved["archive_targets"],
                {
                    "install_directory": {
                        "original": str(install),
                        "archive": str(archived_install),
                    },
                    "inventory": {
                        "original": str(inventory),
                        "archive": str(archived_inventory),
                    },
                    "secrets": {
                        "original": str(secrets),
                        "archive": str(archived_secrets),
                    },
                },
            )
            self.assertEqual(
                saved["created_services"],
                ["remnanode", "stopped-worker"],
            )
            self.assertEqual(saved["running_services"], ["remnanode"])
            with self.assertRaisesRegex(ValidationError, "уже записаны"):
                journal.set_archive_metadata(
                    install_directory=(install, archived_install),
                    inventory=(inventory, archived_inventory),
                    secrets=None,
                    created_services=set(),
                    running_services=set(),
                )

    def test_archive_metadata_rejects_unsafe_or_inconsistent_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(RuntimePaths(root / "runtime"))
            journal = TransactionJournal(store, "stack-archive")
            install = root / "opt/remnanode"
            archived_install = root / "opt/remnanode.removed-fixed"
            inventory = store.paths.inventory
            archived_inventory = store.paths.state / "inventory.removed-fixed.json"

            with self.assertRaisesRegex(ValidationError, "безопасным абсолютным"):
                journal.set_archive_metadata(
                    install_directory=(Path("relative"), archived_install),
                    inventory=(inventory, archived_inventory),
                    secrets=None,
                    created_services=set(),
                    running_services=set(),
                )
            with self.assertRaisesRegex(ValidationError, "некорректные имена"):
                journal.set_archive_metadata(
                    install_directory=(install, archived_install),
                    inventory=(inventory, archived_inventory),
                    secrets=None,
                    created_services={"node\nservice"},
                    running_services=set(),
                )
            with self.assertRaisesRegex(ValidationError, "подмножеством"):
                journal.set_archive_metadata(
                    install_directory=(install, archived_install),
                    inventory=(inventory, archived_inventory),
                    secrets=None,
                    created_services={"node"},
                    running_services={"node", "nginx"},
                )
            saved = json.loads(journal.path.read_text(encoding="utf-8"))
            self.assertNotIn("archive_targets", saved)
            self.assertNotIn("created_services", saved)

    def test_journal_rejects_unbounded_or_control_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = StateStore(RuntimePaths(root / "runtime"))

            with self.assertRaisesRegex(ValidationError, "Operation journal"):
                TransactionJournal(store, "panel-update\nspoofed")
            self.assertFalse(
                (store.paths.state / "active-transaction.json").exists()
            )

            journal = TransactionJournal(store, "panel-update")
            with self.assertRaisesRegex(ValidationError, "Phase journal"):
                journal.phase("stopping\napplications")
            with self.assertRaisesRegex(ValidationError, "безопасным абсолютным"):
                journal.set_backup(root / "backup\nspoofed.tar.gz")


if __name__ == "__main__":
    unittest.main()
