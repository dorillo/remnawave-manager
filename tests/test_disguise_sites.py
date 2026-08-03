from __future__ import annotations

import json
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SITES_ROOT = PROJECT_ROOT / "src/remnawave_manager/data/disguises"
EXPECTED_IDS = (
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
)


class SiteParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.assets: list[str] = []
        self.inline_scripts = 0
        self.titles: list[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in {"img", "script", "link"}:
            candidate = values.get("src") or values.get("href")
            if candidate:
                self.assets.append(candidate)
        if tag == "script" and not values.get("src"):
            self.inline_scripts += 1
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.titles.append(data.strip())


class DisguiseSiteTests(unittest.TestCase):
    def test_catalog_and_directories_define_exactly_ten_sites(self) -> None:
        catalog = json.loads((SITES_ROOT / "catalog.json").read_text(encoding="utf-8"))
        ids = tuple(item["id"] for item in catalog["templates"])
        directories = tuple(sorted(item.name for item in SITES_ROOT.iterdir() if item.is_dir()))

        self.assertEqual(ids, EXPECTED_IDS)
        self.assertEqual(directories, EXPECTED_IDS)
        self.assertEqual(len({item["name"] for item in catalog["templates"]}), 10)
        self.assertEqual(len({item["description"] for item in catalog["templates"]}), 10)

    def test_sites_are_self_contained_and_hardened(self) -> None:
        titles: set[str] = set()
        for template_id in EXPECTED_IDS:
            with self.subTest(template=template_id):
                site = SITES_ROOT / template_id
                html = (site / "index.html").read_text(encoding="utf-8")
                css = (site / "styles.css").read_text(encoding="utf-8")
                javascript = (site / "app.js").read_text(encoding="utf-8")
                parser = SiteParser()
                parser.feed(html)

                self.assertIn('<html lang="ru">', html)
                self.assertIn('name="viewport"', html)
                self.assertIn('name="referrer" content="no-referrer"', html)
                self.assertIn('http-equiv="Content-Security-Policy"', html)
                self.assertIn("connect-src 'none'", html)
                self.assertEqual(parser.inline_scripts, 0)
                self.assertEqual(parser.assets.count("styles.css"), 1)
                self.assertEqual(parser.assets.count("app.js"), 1)
                self.assertGreater(len(javascript), 500)
                self.assertNotRegex(css, r"@import|url\([\"']?https?://")
                self.assertNotRegex(javascript, r"\bfetch\s*\(|XMLHttpRequest|WebSocket")
                self.assertLess(sum(path.stat().st_size for path in site.rglob("*") if path.is_file()), 5 * 1024 * 1024)

                for asset in parser.assets:
                    self.assertFalse(asset.startswith(("http://", "https://", "//")), asset)
                    if asset.startswith(("#", "data:")):
                        continue
                    self.assertTrue((site / asset.split("?", 1)[0]).is_file(), asset)

                title = "".join(parser.titles)
                self.assertTrue(title)
                self.assertNotIn(title, titles)
                titles.add(title)

    def test_sites_have_distinct_document_structures(self) -> None:
        fingerprints: set[tuple[int, ...]] = set()
        tags = ("aside", "article", "section", "table", "figure", "form", "nav")
        for template_id in EXPECTED_IDS:
            html = (SITES_ROOT / template_id / "index.html").read_text(encoding="utf-8")
            fingerprint = tuple(len(re.findall(fr"<{tag}(?:\s|>)", html)) for tag in tags)
            self.assertNotIn(fingerprint, fingerprints, template_id)
            fingerprints.add(fingerprint)

    def test_sites_start_anonymous_and_define_restricted_login(self) -> None:
        authenticated_state_markers = (
            "data-login",
            "data-gate",
            "Orbit Systems",
            "Мария Ильина",
            "Моя лента",
            "Для вас",
            "Продолжить просмотр",
        )
        for template_id in EXPECTED_IDS:
            with self.subTest(template=template_id):
                html = (SITES_ROOT / template_id / "index.html").read_text(encoding="utf-8")

                self.assertIn("data-auth", html)
                self.assertIn('type="email"', html)
                self.assertIn('type="password"', html)
                self.assertIn("Войти", html)
                for marker in authenticated_state_markers:
                    self.assertNotIn(marker, html)


if __name__ == "__main__":
    unittest.main()
