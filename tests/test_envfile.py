from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from remnawave_manager.envfile import EnvDocument
from remnawave_manager.errors import ValidationError


class EnvDocumentPanelV3MigrationTests(unittest.TestCase):
    def test_save_preserves_parallel_operator_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / ".env"
            path.write_text("APP_SECRET=original\n", encoding="utf-8")
            document = EnvDocument.load(path)
            document.set("APP_SECRET", "manager-change")
            operator_edit = "APP_SECRET=operator-change\n"
            path.write_text(operator_edit, encoding="utf-8")
            before_write = mock.Mock()

            with self.assertRaisesRegex(ValidationError, "изменился после загрузки"):
                document.save(path, before_write=before_write)

            self.assertEqual(path.read_text(encoding="utf-8"), operator_edit)
            before_write.assert_not_called()

    def test_effective_value_handles_dotenv_inline_comments_after_quotes(self) -> None:
        document = EnvDocument(
            "EMPTY='' # intentionally empty\n"
            "VALUE=secret # deployment note\n"
            "HASH=value#part-of-value\n"
        )

        self.assertEqual(document.effective_value("EMPTY"), "")
        self.assertEqual(document.effective_value("VALUE"), "secret")
        self.assertEqual(document.effective_value("HASH"), "value#part-of-value")

    def test_renames_jwt_secret_without_changing_right_hand_side_bytes(self) -> None:
        original = (
            "# surrounding text must stay untouched\r\n"
            "export JWT_AUTH_SECRET \t= \t'  value=with # spaces  '\r\n"
            "KEEP=this\r\n"
        )
        document = EnvDocument(original)

        result = document.migrate_panel_v3()

        expected = original.replace("JWT_AUTH_SECRET", "APP_SECRET", 1)
        self.assertEqual(document.render().encode("utf-8"), expected.encode("utf-8"))
        self.assertEqual(result["secret_source"], "JWT_AUTH_SECRET")

    def test_existing_app_secret_wins_and_all_jwt_secrets_are_removed(self) -> None:
        original = (
            "JWT_AUTH_SECRET=obsolete-first\n"
            "APP_SECRET = 'keep this exact value'\n"
            "JWT_AUTH_SECRET=obsolete-last\n"
            "UNCHANGED=yes\n"
        )
        document = EnvDocument(original)

        result = document.migrate_panel_v3()

        self.assertEqual(
            document.render(),
            "APP_SECRET = 'keep this exact value'\nUNCHANGED=yes\n",
        )
        self.assertEqual(result["secret_source"], "APP_SECRET")

    def test_duplicate_jwt_secret_uses_effective_last_assignment(self) -> None:
        document = EnvDocument(
            "JWT_AUTH_SECRET=obsolete\n"
            "KEEP=between\n"
            "export JWT_AUTH_SECRET  =  'effective secret'\n"
        )

        document.migrate_panel_v3()

        self.assertEqual(
            document.render(),
            "KEEP=between\nexport APP_SECRET  =  'effective secret'\n",
        )

    def test_duplicate_app_secret_is_collapsed_to_effective_last_assignment(self) -> None:
        document = EnvDocument(
            "APP_SECRET=obsolete\n"
            "KEEP=between\n"
            "export APP_SECRET  =  'effective secret'\n"
        )

        document.migrate_panel_v3()

        self.assertEqual(
            document.render(),
            "KEEP=between\nexport APP_SECRET  =  'effective secret'\n",
        )

    def test_missing_or_empty_secret_is_rejected(self) -> None:
        inputs = (
            "KEEP=yes\n",
            "JWT_AUTH_SECRET=\n",
            "JWT_AUTH_SECRET=   \n",
            'JWT_AUTH_SECRET=""\n',
            "JWT_AUTH_SECRET=''\n",
            "APP_SECRET=\nJWT_AUTH_SECRET=usable-but-must-not-replace-app\n",
        )
        for text in inputs:
            with self.subTest(text=text), self.assertRaises(ValidationError):
                EnvDocument(text).migrate_panel_v3()

    def test_removes_every_deprecated_assignment_and_preserves_other_lines(self) -> None:
        document = EnvDocument(
            "APP_SECRET=stable\n"
            "JWT_API_TOKENS_SECRET=one\n"
            "# SWAGGER_PATH=/commented-is-not-an-assignment\n"
            "SWAGGER_PATH=/docs\n"
            "SCALAR_PATH=/scalar\n"
            "IS_DOCS_ENABLED=true\n"
            "JWT_API_TOKENS_SECRET=two\n"
            "OTHER=value\n"
        )

        result = document.migrate_panel_v3()

        self.assertEqual(
            document.render(),
            "APP_SECRET=stable\n"
            "# SWAGGER_PATH=/commented-is-not-an-assignment\n"
            "OTHER=value\n",
        )
        self.assertEqual(
            result["removed"],
            ["JWT_API_TOKENS_SECRET", "SWAGGER_PATH", "SCALAR_PATH", "IS_DOCS_ENABLED"],
        )

    def test_panel_v3_preflight_accepts_complete_legacy_environment_after_migration(self) -> None:
        document = EnvDocument(
            "DATABASE_URL=postgresql://postgres:secret@remnawave-db/postgres\n"
            "JWT_AUTH_SECRET=stable-secret\n"
            "JWT_AUTH_LIFETIME=168\n"
            "FRONT_END_DOMAIN=panel.example.com\n"
            "SUB_PUBLIC_DOMAIN=sub.example.com\n"
            "METRICS_USER=metrics\n"
            "METRICS_PASS=secret\n"
            "REDIS_SOCKET=/var/run/valkey/valkey.sock\n"
            "WEBHOOK_ENABLED=false\n"
        )

        document.migrate_panel_v3()
        document.validate_panel_v3()

    def test_panel_v3_preflight_rejects_missing_contract_and_ambiguous_redis(self) -> None:
        base = (
            "DATABASE_URL=postgresql://postgres:secret@remnawave-db/postgres\n"
            "APP_SECRET=stable-secret\n"
            "FRONT_END_DOMAIN=panel.example.com\n"
            "SUB_PUBLIC_DOMAIN=sub.example.com\n"
            "METRICS_USER=metrics\n"
            "METRICS_PASS=secret\n"
        )
        invalid = (
            base.replace("METRICS_PASS=secret\n", ""),
            base,
            base
            + "REDIS_SOCKET=/var/run/valkey/valkey.sock\n"
            + "REDIS_HOST=redis\nREDIS_PORT=6379\n",
            base + "REDIS_HOST=redis\nREDIS_PORT=invalid\n",
            base + "REDIS_SOCKET=/run/redis.sock\nREDIS_HOST=redis\n",
            base + "REDIS_SOCKET=/run/redis.sock\nREDIS_PORT=6379\n",
            base + "REDIS_SOCKET=/run/redis.sock\nWEBHOOK_ENABLED=yes\n",
        )
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(ValidationError):
                EnvDocument(text).validate_panel_v3()


if __name__ == "__main__":
    unittest.main()
