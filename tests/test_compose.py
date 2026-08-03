from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from remnawave_manager.compose import ComposeDocument, validate_rendered_compose
from remnawave_manager.errors import TransactionError, ValidationError


class ComposeDocumentTests(unittest.TestCase):
    def test_rendered_validation_reports_primary_and_cleanup_failures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            compose = root / "compose.yml"
            compose.write_text("services: {}\n", encoding="utf-8")
            runner = mock.Mock()
            runner.run.side_effect = ValidationError("invalid rendered compose")
            original_unlink = Path.unlink

            def reject_temporary(path: Path, missing_ok: bool = False) -> None:
                if path.name.startswith(".rwm-compose-"):
                    raise OSError("cleanup denied")
                original_unlink(path, missing_ok=missing_ok)

            with (
                mock.patch.object(Path, "unlink", new=reject_temporary),
                self.assertRaisesRegex(
                    TransactionError,
                    "cleanup denied.*invalid rendered compose",
                ),
            ):
                validate_rendered_compose(
                    runner,
                    compose,
                    "services: {}\n",
                    None,
                )

    def test_save_preserves_parallel_operator_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "compose.yml"
            original = "services:\n  panel:\n    image: old/panel:2\n"
            path.write_text(original, encoding="utf-8")
            document = ComposeDocument.load(path)
            document.set_image("panel", "new/panel:3")
            operator_edit = original + "# operator edit\n"
            path.write_text(operator_edit, encoding="utf-8")
            before_write = mock.Mock()

            with self.assertRaisesRegex(ValidationError, "изменился после загрузки"):
                document.save(path, before_write=before_write)

            self.assertEqual(path.read_text(encoding="utf-8"), operator_edit)
            before_write.assert_not_called()

    def test_rejects_nested_duplicate_or_repeated_services(self) -> None:
        for payload in (
            "extension:\n  services:\n    panel:\n      image: old/panel:2\n",
            "services:\n  panel:\n    image: old/panel:2\nservices:\n  node:\n    image: old/node:2\n",
            "services:\n  panel:\n    image: old/panel:2\n  panel:\n    image: shadow/panel:2\n",
        ):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                ComposeDocument(payload).service_blocks()

    def test_changes_only_direct_image_and_preserves_comments_and_anchors(self) -> None:
        original = (
            "x-runtime: &runtime\r\n"
            "  restart: unless-stopped\r\n"
            "services:\r\n"
            "  remnawave: &panel-service\r\n"
            "    <<: *runtime\r\n"
            '    image: "ghcr.io/remnawave/backend:2" # keep deployment note\r\n'
            "    environment:\r\n"
            "      APP_PORT: 3000\r\n"
            "  postgres:\r\n"
            "    image: postgres:17 # database is unrelated\r\n"
        )
        document = ComposeDocument(original)

        previous = document.set_image("remnawave", "ghcr.io/remnawave/backend:3.1.0")

        self.assertEqual(previous, "ghcr.io/remnawave/backend:2")
        self.assertEqual(
            document.render(),
            original.replace(
                '    image: "ghcr.io/remnawave/backend:2" # keep deployment note',
                "    image: ghcr.io/remnawave/backend:3.1.0 # keep deployment note",
                1,
            ),
        )
        self.assertIn("x-runtime: &runtime\r\n", document.render())
        self.assertIn("  remnawave: &panel-service\r\n", document.render())
        self.assertIn("    <<: *runtime\r\n", document.render())
        self.assertIn("    image: postgres:17 # database is unrelated\r\n", document.render())

    def test_nested_image_does_not_make_direct_image_ambiguous(self) -> None:
        original = (
            "services:\n"
            "  panel:\n"
            "    image: old/panel:2\n"
            "    extension:\n"
            "      image: nested/value:1\n"
        )
        document = ComposeDocument(original)

        document.set_image("panel", "new/panel:3")

        self.assertEqual(
            document.render(),
            original.replace("    image: old/panel:2", "    image: new/panel:3", 1),
        )

    def test_preserves_anchor_attached_to_image_scalar(self) -> None:
        original = (
            "services:\n"
            "  panel:\n"
            "    image: &panel-image old/panel:2 # aliases depend on this anchor\n"
            "  worker:\n"
            "    image: *panel-image\n"
        )
        document = ComposeDocument(original)

        document.set_image("panel", "new/panel:3")

        self.assertEqual(
            document.render(),
            original.replace(
                "image: &panel-image old/panel:2",
                "image: &panel-image new/panel:3",
                1,
            ),
        )

    def test_rejects_multiple_direct_image_keys_as_ambiguous(self) -> None:
        document = ComposeDocument(
            "services:\n"
            "  panel:\n"
            "    image: old/panel:2\n"
            "    image: shadow/panel:2\n"
        )

        with self.assertRaises(ValidationError):
            document.set_image("panel", "new/panel:3")

    def test_rejects_tabs_before_attempting_a_rewrite(self) -> None:
        with self.assertRaises(ValidationError):
            ComposeDocument("services:\n\tpanel:\n\t\timage: old/panel:2\n")

    def test_replaces_only_selected_service_long_volume_target(self) -> None:
        original = (
            "services:\r\n"
            "  remnawave-nginx:\r\n"
            "    volumes:\r\n"
            "      - type: bind\r\n"
            "        source: ./other\r\n"
            "        target: /var/log/remnanode\r\n"
            "  remnanode:\r\n"
            "    volumes:\r\n"
            "      - type: bind\r\n"
            "        source: ./logs\r\n"
            '        target: "/var/log/remnanode" # old manager target\r\n'
        )
        document = ComposeDocument(original)

        changed = document.replace_volume_target(
            "remnanode", "/var/log/remnanode", "/var/log/xray"
        )

        self.assertTrue(changed)
        self.assertEqual(
            document.render(),
            original.replace(
                '        target: "/var/log/remnanode" # old manager target',
                '        target: "/var/log/xray" # old manager target',
            ),
        )
        self.assertIn("source: ./logs\r\n", document.render())
        self.assertIn("        target: /var/log/remnanode\r\n", document.render())

    def test_replaces_short_volume_target_without_reformatting(self) -> None:
        original = (
            "services:\n"
            "  remnanode:\n"
            "    image: remnawave/node:2.8.0\n"
            "    volumes:\n"
            "      - './logs:/var/log/remnanode:rw' # preserve this comment\n"
        )
        document = ComposeDocument(original)

        changed = document.replace_volume_target(
            "remnanode", "/var/log/remnanode", "/var/log/xray"
        )

        self.assertTrue(changed)
        self.assertEqual(
            document.render(),
            original.replace("./logs:/var/log/remnanode:rw", "./logs:/var/log/xray:rw"),
        )


if __name__ == "__main__":
    unittest.main()
