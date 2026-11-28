"""Offline checks for the public source and container build context."""
import fnmatch
import json
import re
import shlex
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ReleaseTests(unittest.TestCase):
    def test_docker_copy_sources_are_in_the_build_allowlist(self):
        rules = (ROOT / ".dockerignore").read_text().splitlines()
        allowed = {line[1:].rstrip("/") for line in rules if line.startswith("!")}
        self.assertIn("**", rules)
        for line in (ROOT / "Dockerfile").read_text().splitlines():
            if not line.startswith("COPY "):
                continue
            words = shlex.split(line)
            if not words or words[0] != "COPY" or words[1].startswith("--from="):
                continue
            for source in words[1:-1]:
                with self.subTest(source=source):
                    self.assertTrue((ROOT / source).exists())
                    self.assertIn(source.rstrip("/"), allowed)

    def test_private_paths_are_excluded_from_git_and_build_context(self):
        gitignore = (ROOT / ".gitignore").read_text().splitlines()
        allowed = [line[1:] for line in (ROOT / ".dockerignore").read_text().splitlines()
                   if line.startswith("!")]
        for private, rule in [
            ("config.json", "config.json"),
            (".ma_token", ".ma_token"),
            (".env", ".env"),
            (".state", ".state/"),
            ("ma_youtube_cast", "/ma_youtube_cast/"),
        ]:
            with self.subTest(path=private):
                self.assertIn(rule, gitignore)
                self.assertFalse(any(fnmatch.fnmatch(private, pattern) for pattern in allowed))
        self.assertIn("*.log", gitignore)
        self.assertIn("*.jsonl", gitignore)

    def test_manifest_and_lockfile_agree(self):
        package = json.loads((ROOT / "package.json").read_text())
        lock = json.loads((ROOT / "package-lock.json").read_text())
        self.assertEqual(package["name"], "cast2sendspin")
        self.assertEqual(package["name"], lock["name"])
        self.assertEqual(package["dependencies"], lock["packages"][""]["dependencies"])
        self.assertEqual(package["license"], "Apache-2.0")
        self.assertEqual(lock["packages"][""]["license"], "Apache-2.0")

    def test_icon_is_valid_svg_and_served_by_the_app(self):
        icon = ET.parse(ROOT / "src/web/icon.svg").getroot()
        self.assertEqual(icon.tag, "{http://www.w3.org/2000/svg}svg")
        self.assertIn('href="/icon.svg"', (ROOT / "src/web/index.html").read_text())
        self.assertIn("['/icon.svg'", (ROOT / "src/manager.mjs").read_text())

    def test_icon_uses_the_web_ui_theme_palette(self):
        css = (ROOT / "src/web/style.css").read_text()
        theme = re.search(r":root\s*\{([^}]+)\}", css, re.S).group(1)
        palette = set(re.findall(r"#[0-9a-fA-F]{6}", theme))
        primary = re.search(r"--primary:\s*(#[0-9a-fA-F]{6})", theme).group(1)
        icon = ET.parse(ROOT / "src/web/icon.svg").getroot()
        colors = {
            value for element in icon.iter() for name, value in element.attrib.items()
            if name in ("fill", "stroke") and value.startswith("#")
        }
        self.assertTrue(colors <= palette)
        self.assertIn(primary, colors)

    def test_entry_points_and_subprocess_paths_exist(self):
        package = json.loads((ROOT / "package.json").read_text())
        self.assertTrue((ROOT / shlex.split(package["scripts"]["start"])[1]).is_file())
        sources = [*ROOT.glob("src/*.mjs"), ROOT / "src/worker.py"]
        for source in sources:
            for path in re.findall(r"""['"](src/[^'"\s]+)['"]""", source.read_text()):
                with self.subTest(source=source.name, path=path):
                    self.assertTrue((ROOT / path).is_file())


if __name__ == "__main__":
    unittest.main()
