from __future__ import annotations

import unittest
from pathlib import Path

from remnawave_manager.errors import TransactionError, ValidationError
from remnawave_manager.manager_update import update_manager
from remnawave_manager.runner import Result


class FakeRunner:
    def __init__(self, installer: str) -> None:
        self.installer = installer
        self.download: tuple[list[str], Path] | None = None
        self.interactive_args: list[str] | None = None
        self.version_args: list[str] | None = None
        self.downloaded_path: Path | None = None

    def run_to_file(self, args: list[str], target: Path, **_: object) -> None:
        self.download = (args, target)
        target.write_bytes(self.installer.encode("utf-8"))
        self.downloaded_path = target

    def interactive(self, args: list[str], **_: object) -> None:
        self.interactive_args = args

    def run(self, args: list[str], **_: object) -> Result:
        self.version_args = args
        return Result(tuple(args), 0, "rwm 0.1.3\n", "")


VALID_INSTALLER = """#!/bin/bash
readonly DEFAULT_MANAGER_REPOSITORY='dorillo/remnawave-manager'
readonly DEFAULT_MANAGER_REF='main'
bootstrap_manager() {
    :
}
"""


class ManagerUpdateTests(unittest.TestCase):
    def test_downloads_validated_installer_runs_it_and_checks_new_entrypoint(self) -> None:
        runner = FakeRunner(VALID_INSTALLER)

        version = update_manager(runner)  # type: ignore[arg-type]

        self.assertEqual(version, "rwm 0.1.3")
        self.assertIsNotNone(runner.download)
        assert runner.download is not None
        self.assertIn("--proto", runner.download[0])
        self.assertTrue(
            any(
                item.startswith("https://raw.githubusercontent.com/")
                for item in runner.download[0]
            )
        )
        self.assertEqual(
            runner.interactive_args[0:2],
            ["/bin/bash", str(runner.download[1])],
        )
        self.assertEqual(runner.interactive_args[-1], "install")
        self.assertEqual(runner.version_args, ["/usr/local/bin/rwm", "--version"])
        self.assertIsNotNone(runner.downloaded_path)
        self.assertFalse(runner.downloaded_path.exists())

    def test_rejects_unexpected_download_before_execution(self) -> None:
        runner = FakeRunner("#!/bin/bash\necho untrusted\n")

        with self.assertRaises(ValidationError):
            update_manager(runner)  # type: ignore[arg-type]

        self.assertIsNone(runner.interactive_args)
        self.assertIsNone(runner.version_args)

    def test_rejects_invalid_new_entrypoint_version(self) -> None:
        runner = FakeRunner(VALID_INSTALLER)
        runner.run = lambda args, **kwargs: Result(  # type: ignore[method-assign]
            tuple(args), 0, "unexpected output\n", ""
        )

        with self.assertRaises(TransactionError):
            update_manager(runner)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
