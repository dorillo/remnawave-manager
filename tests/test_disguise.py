from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remnawave_manager.disguise import _refresh_inventory, apply_template
from remnawave_manager.errors import TransactionError, ValidationError
from remnawave_manager.models import Component, Inventory, ManagedFile
from remnawave_manager.runner import Result, Runner, sha256_file


class DisguiseInventoryTests(unittest.TestCase):
    def test_refresh_replaces_only_site_hashes_and_preserves_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            site = root / "site"
            site.mkdir()
            old = site / "old.txt"
            old.write_text("old", encoding="utf-8")
            secret = root / ".cloudflare.ini"
            secret.write_text("token", encoding="utf-8")
            inventory = Inventory(
                schema_version=1,
                role="node",
                install_dir=str(root),
                compose_file=str(root / "docker-compose.yml"),
                env_file=str(root / ".env"),
                webserver="nginx",
                site_dirs=[str(site)],
                components={"node": Component("node", "remnanode")},
                managed_files=[
                    ManagedFile(str(old), sha256_file(old), "site"),
                    ManagedFile(str(secret), sha256_file(secret), "secret"),
                ],
            )
            old.unlink()
            current = site / "index.html"
            current.write_text("new", encoding="utf-8")
            store = mock.Mock()

            _refresh_inventory(store, inventory, site)

            files = {Path(item.path).name: item for item in inventory.managed_files}
            self.assertEqual(set(files), {"index.html", ".cloudflare.ini"})
            self.assertEqual(files["index.html"].sha256, sha256_file(current))
            self.assertEqual(files[".cloudflare.ini"].kind, "secret")
            store.save_inventory.assert_called_once_with(inventory)

    def test_failed_second_rename_restores_original_site(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            site = root / "site"
            site.mkdir()
            (site / "index.html").write_text("original\n", encoding="utf-8")
            inventory = Inventory(
                schema_version=1,
                role="node",
                install_dir=str(root),
                compose_file=str(root / "docker-compose.yml"),
                env_file=None,
                webserver="nginx",
                site_dirs=[str(site)],
                components={"nginx": Component("nginx", "remnawave-nginx")},
                managed_files=[
                    ManagedFile(
                        str(site / "index.html"),
                        sha256_file(site / "index.html"),
                        "config",
                    )
                ],
            )
            store = mock.Mock()
            store.load_inventory.return_value = inventory
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = Result(("docker", "compose"), 0, "", "")
            original_replace = __import__("os").replace

            def fail_install(source: Path, destination: Path) -> None:
                if (
                    Path(destination).name == site.name
                    and Path(source).name.startswith(".site.rwm-new-")
                ):
                    raise OSError("rename failed")
                original_replace(source, destination)

            with (
                mock.patch("remnawave_manager.disguise.create_backup"),
                mock.patch(
                    "remnawave_manager.disguise.nginx_is_running",
                    return_value=True,
                ),
                mock.patch(
                    "remnawave_manager.disguise.os.replace",
                    side_effect=fail_install,
                ),
                self.assertRaisesRegex(TransactionError, "прежний сайт восстановлен"),
            ):
                apply_template(runner, store, "01-northline")

            self.assertEqual(
                (site / "index.html").read_text(encoding="utf-8"),
                "original\n",
            )
            self.assertEqual(
                list(root.glob(".site.rwm-old-*")),
                [],
            )
            self.assertEqual(
                list(root.glob(".site.rwm-new-*")),
                [],
            )

    def test_template_copy_failure_cleans_staging_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            site = root / "site"
            site.mkdir()
            (site / "index.html").write_text("original\n", encoding="utf-8")
            inventory = Inventory(
                schema_version=1,
                role="node",
                install_dir=str(root),
                compose_file=str(root / "docker-compose.yml"),
                env_file=None,
                webserver="nginx",
                site_dirs=[str(site)],
                components={"nginx": Component("nginx", "remnawave-nginx")},
                managed_files=[
                    ManagedFile(
                        str(site / "index.html"),
                        sha256_file(site / "index.html"),
                        "config",
                    )
                ],
            )
            store = mock.Mock()
            store.load_inventory.return_value = inventory

            def fail_copy(_template: str, staging: Path) -> None:
                staging.mkdir()
                (staging / "partial.txt").write_text("partial", encoding="utf-8")
                raise TransactionError("copy failed")

            with (
                mock.patch("remnawave_manager.disguise.create_backup"),
                mock.patch(
                    "remnawave_manager.disguise.copy_template",
                    side_effect=fail_copy,
                ),
                self.assertRaisesRegex(TransactionError, "copy failed"),
            ):
                apply_template(mock.Mock(spec=Runner), store, "01-northline")

            self.assertEqual(
                (site / "index.html").read_text(encoding="utf-8"),
                "original\n",
            )
            self.assertEqual(list(root.glob(".site.rwm-new-*")), [])

    def test_inventory_save_failure_restores_site_and_previous_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            site = root / "site"
            site.mkdir()
            index = site / "index.html"
            index.write_text("original\n", encoding="utf-8")
            compose = root / "docker-compose.yml"
            compose.write_text("services: {}\n", encoding="utf-8")
            inventory = Inventory(
                schema_version=1,
                role="node",
                install_dir=str(root),
                compose_file=str(compose),
                env_file=None,
                webserver="nginx",
                site_dirs=[str(site)],
                components={"nginx": Component("nginx", "remnawave-nginx")},
                managed_files=[
                    ManagedFile(str(index), sha256_file(index), "site"),
                ],
            )
            store = mock.Mock()
            store.load_inventory.return_value = inventory
            store.save_inventory.side_effect = [OSError("state fsync failed"), None]
            runner = mock.Mock(spec=Runner)
            runner.run.return_value = Result(("docker",), 0, "", "")

            with (
                mock.patch("remnawave_manager.disguise.create_backup"),
                self.assertRaisesRegex(TransactionError, "прежний сайт восстановлен"),
            ):
                apply_template(runner, store, "01-northline")

            self.assertEqual(index.read_text(encoding="utf-8"), "original\n")
            self.assertEqual(store.save_inventory.call_count, 2)
            restored = store.save_inventory.call_args_list[-1].args[0]
            self.assertEqual(restored.managed_files[0].sha256, sha256_file(index))
            recreate_calls = [
                call
                for call in runner.run.call_args_list
                if "--force-recreate" in call.args[0]
            ]
            self.assertEqual(len(recreate_calls), 2)
            state_checks = [
                call
                for call in runner.run.call_args_list
                if "ps" in call.args[0]
            ]
            self.assertEqual(len(state_checks), 1)

    def test_refresh_removes_legacy_config_kind_below_site_root(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            site = root / "site"
            site.mkdir()
            index = site / "index.html"
            index.write_text("new", encoding="utf-8")
            inventory = Inventory(
                schema_version=1,
                role="node",
                install_dir=str(root),
                compose_file=str(root / "docker-compose.yml"),
                env_file=None,
                webserver="nginx",
                site_dirs=[str(site)],
                managed_files=[
                    ManagedFile(str(index), "0" * 64, "config"),
                ],
            )
            store = mock.Mock()

            _refresh_inventory(store, inventory, site)

            matches = [item for item in inventory.managed_files if item.path == str(index.resolve())]
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].kind, "site")
            self.assertEqual(matches[0].sha256, sha256_file(index))
            store.save_inventory.assert_called_once_with(inventory)

    def test_untracked_site_file_blocks_destructive_replacement_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            site = root / "site"
            site.mkdir()
            tracked = site / "index.html"
            tracked.write_text("tracked", encoding="utf-8")
            (site / "untracked.txt").write_text("do not lose", encoding="utf-8")
            inventory = Inventory(
                schema_version=1,
                role="node",
                install_dir=str(root),
                compose_file=str(root / "docker-compose.yml"),
                env_file=None,
                webserver="nginx",
                site_dirs=[str(site)],
                components={"nginx": Component("nginx", "remnawave-nginx")},
                managed_files=[
                    ManagedFile(str(tracked), sha256_file(tracked), "config"),
                ],
            )
            store = mock.Mock()
            store.load_inventory.return_value = inventory

            with (
                mock.patch("remnawave_manager.disguise.create_backup") as backup,
                self.assertRaisesRegex(ValidationError, "неучтённый файл"),
            ):
                apply_template(mock.Mock(spec=Runner), store, "01-northline")

            backup.assert_not_called()
            self.assertEqual((site / "untracked.txt").read_text(encoding="utf-8"), "do not lose")

    def test_modified_site_file_blocks_replacement_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            site = root / "site"
            site.mkdir()
            index = site / "index.html"
            index.write_text("before", encoding="utf-8")
            recorded = sha256_file(index)
            index.write_text("after", encoding="utf-8")
            inventory = Inventory(
                schema_version=1,
                role="node",
                install_dir=str(root),
                compose_file=str(root / "docker-compose.yml"),
                env_file=None,
                webserver="nginx",
                site_dirs=[str(site)],
                components={"nginx": Component("nginx", "remnawave-nginx")},
                managed_files=[ManagedFile(str(index), recorded, "site")],
            )
            store = mock.Mock()
            store.load_inventory.return_value = inventory

            with (
                mock.patch("remnawave_manager.disguise.create_backup") as backup,
                self.assertRaisesRegex(ValidationError, "изменён после adoption"),
            ):
                apply_template(mock.Mock(spec=Runner), store, "01-northline")

            backup.assert_not_called()
            self.assertEqual(index.read_text(encoding="utf-8"), "after")

    def test_site_change_during_backup_is_not_discarded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            site = root / "site"
            site.mkdir()
            index = site / "index.html"
            index.write_text("before", encoding="utf-8")
            inventory = Inventory(
                schema_version=1,
                role="node",
                install_dir=str(root),
                compose_file=str(root / "docker-compose.yml"),
                env_file=None,
                webserver="nginx",
                site_dirs=[str(site)],
                components={"nginx": Component("nginx", "remnawave-nginx")},
                managed_files=[ManagedFile(str(index), sha256_file(index), "site")],
            )
            store = mock.Mock()
            store.load_inventory.return_value = inventory

            def edit_during_backup(*_args, **_kwargs) -> None:  # type: ignore[no-untyped-def]
                index.write_text("operator edit", encoding="utf-8")

            with (
                mock.patch(
                    "remnawave_manager.disguise.create_backup",
                    side_effect=edit_during_backup,
                ),
                self.assertRaisesRegex(ValidationError, "изменён после adoption"),
            ):
                apply_template(mock.Mock(spec=Runner), store, "01-northline")

            self.assertEqual(index.read_text(encoding="utf-8"), "operator edit")
            self.assertEqual(list(root.glob(".site.rwm-old-*")), [])
            self.assertEqual(list(root.glob(".site.rwm-new-*")), [])


if __name__ == "__main__":
    unittest.main()
