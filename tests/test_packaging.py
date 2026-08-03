from __future__ import annotations

import json
import re
import tomllib
import unittest
from importlib.resources import files
from pathlib import Path
from urllib.parse import unquote, urlsplit

from remnawave_manager import __version__

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_project_and_runtime_versions_match(self) -> None:
        config = tomllib.loads(
            (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
        )

        self.assertEqual(config["project"]["version"], __version__)

    def test_readme_compatibility_matrix_matches_manifest(self) -> None:
        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        manifest = json.loads(
            (
                PROJECT_ROOT / "src/remnawave_manager/data/compatibility.json"
            ).read_text(encoding="utf-8")
        )
        rows = {
            name: (sources, target)
            for name, sources, target in re.findall(
                r"^\| (.+?) \| (.+?) \| (.+?) \|$",
                readme,
                flags=re.MULTILINE,
            )
        }
        components = manifest["components"]

        for label, key in (
            ("Remnawave Panel", "panel"),
            ("Subscription Page", "subscription"),
            ("Remnawave Node", "node"),
            ("PostgreSQL", "database"),
        ):
            with self.subTest(component=key):
                self.assertEqual(
                    rows[label],
                    (
                        ", ".join(components[key]["upgrade_from"]),
                        components[key]["version"],
                    ),
                )
        self.assertEqual(
            rows["`wgcf`"],
            (
                "не применяется",
                f"{manifest['tools']['wgcf']['version']} с фиксированным SHA-256",
            ),
        )

    def test_internal_markdown_links_resolve(self) -> None:
        markdown_files = [PROJECT_ROOT / "README.md"]
        markdown_files.extend((PROJECT_ROOT / "docs").rglob("*.md"))
        markdown_files.extend((PROJECT_ROOT / "src").rglob("*.md"))
        failures: list[str] = []
        link_pattern = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")

        for document in markdown_files:
            content = document.read_text(encoding="utf-8")
            for raw_target in link_pattern.findall(content):
                target = raw_target.strip()
                if target.startswith("<") and target.endswith(">"):
                    target = target[1:-1]
                parsed = urlsplit(target)
                if parsed.scheme or parsed.netloc or not parsed.path:
                    continue
                resolved = (document.parent / unquote(parsed.path)).resolve()
                if not resolved.exists():
                    failures.append(
                        f"{document.relative_to(PROJECT_ROOT)} -> {target}"
                    )

        self.assertEqual(failures, [])

    def test_source_distribution_includes_root_installer(self) -> None:
        manifest = (PROJECT_ROOT / "MANIFEST.in").read_text(encoding="utf-8")

        self.assertIn("include install.sh", manifest.splitlines())
        self.assertIn("prune tests", manifest.splitlines())
        self.assertIn("global-exclude *.py[cod]", manifest.splitlines())
        self.assertTrue((PROJECT_ROOT / "install.sh").is_file())

    def test_distribution_declares_license_and_notice_files(self) -> None:
        config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual(
            config["tool"]["setuptools"]["license-files"],
            ["LICENSE", "NOTICE"],
        )
        self.assertTrue((PROJECT_ROOT / "LICENSE").is_file())
        self.assertTrue((PROJECT_ROOT / "NOTICE").is_file())

    def test_all_runtime_data_has_an_explicit_package_pattern(self) -> None:
        config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        patterns = set(config["tool"]["setuptools"]["package-data"]["remnawave_manager"])

        self.assertEqual(
            patterns,
            {
                "data/*.json",
                "data/licenses/*.txt",
                "data/disguises/*.json",
                "data/disguises/*.md",
                "data/disguises/*/*.html",
                "data/disguises/*/*.css",
                "data/disguises/*/*.js",
                "data/disguises/*/*.jpg",
                "data/disguises/*/*.svg",
            },
        )

    def test_all_markdown_guides_are_included_as_distribution_docs(self) -> None:
        config = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

        self.assertEqual(
            config["tool"]["setuptools"]["data-files"]["share/doc/remnawave-manager"],
            ["docs/*.md"],
        )
        self.assertTrue((PROJECT_ROOT / "docs/certificates.md").is_file())

    def test_packaged_resources_cover_manifest_notices_and_ten_templates(self) -> None:
        package = files("remnawave_manager")
        self.assertTrue(package.joinpath("data/compatibility.json").is_file())
        self.assertTrue(package.joinpath("data/licenses/wgcf-MIT.txt").is_file())
        self.assertTrue(package.joinpath("data/disguises/catalog.json").is_file())
        self.assertTrue(package.joinpath("data/disguises/CREDITS.md").is_file())

        templates = sorted(
            item.name
            for item in package.joinpath("data/disguises").iterdir()
            if item.is_dir()
        )
        self.assertEqual(
            templates,
            [
                "01-northline",
                "02-aster-observatory",
                "03-morrow-coffee",
                "04-signal-works",
                "05-field-notes",
                "06-loop-archive",
                "07-fokus-news",
                "08-vector-docs",
                "09-pulse-monitor",
                "10-dev-circle",
            ],
        )
        for template in templates:
            resource = package.joinpath(f"data/disguises/{template}")
            for asset in ("index.html", "styles.css", "app.js"):
                with self.subTest(template=template, asset=asset):
                    self.assertTrue(resource.joinpath(asset).is_file())


if __name__ == "__main__":
    unittest.main()
