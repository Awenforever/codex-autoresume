"""Release-facing plugin text must stay bilingual (English + Chinese).

The panel itself switches languages at runtime; shipped docs, skill text,
manifest descriptions, MCP tool descriptions and CLI help are static and must
carry both languages.
"""
import json
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CJK = re.compile(r"[\u4e00-\u9fff]")
LATIN_WORD = re.compile(r"[A-Za-z]{4,}")


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as f:
        return f.read()


class BilingualTextTest(unittest.TestCase):
    def assert_bilingual(self, text, label):
        self.assertRegex(text, CJK, "%s has no Chinese text" % label)
        self.assertRegex(text, LATIN_WORD, "%s has no English text" % label)

    def test_root_readme_has_both_languages(self):
        self.assert_bilingual(_read("README.md"), "README.md")

    def test_plugin_readme_has_both_languages(self):
        self.assert_bilingual(_read("plugin", "README.md"), "plugin/README.md")

    def test_skill_is_bilingual(self):
        self.assert_bilingual(
            _read("plugin", "skills", "autoresume-guard", "SKILL.md"),
            "autoresume-guard/SKILL.md")

    def test_manifests_are_bilingual(self):
        for rel in ("plugin/plugin.json", "plugin/.codex-plugin/plugin.json"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                data = json.load(f)
            self.assert_bilingual(data["description"], rel + " description")
            ui = data["interface"]
            self.assert_bilingual(ui["displayName"], rel + " displayName")
            self.assert_bilingual(ui["shortDescription"], rel + " shortDescription")
            if "longDescription" in ui:
                self.assert_bilingual(ui["longDescription"], rel + " longDescription")
            for prompt in ui.get("defaultPrompt", []):
                self.assert_bilingual(prompt, rel + " defaultPrompt item")

    def test_mcp_tool_descriptions_are_bilingual(self):
        from codex_autoresume import mcp_server
        for tool in mcp_server.TOOLS:
            self.assert_bilingual(tool["description"], "MCP " + tool["name"])
            for prop, schema in tool["inputSchema"].get("properties", {}).items():
                if "description" in schema:
                    self.assert_bilingual(
                        schema["description"],
                        "MCP %s.%s description" % (tool["name"], prop))

    def test_cli_help_is_bilingual(self):
        from codex_autoresume.cli import build_parser
        self.assert_bilingual(build_parser().format_help(), "CLI help")


class VersionAlignmentTest(unittest.TestCase):
    def test_version_matches_every_manifest(self):
        from codex_autoresume import __version__
        pyproject = _read("pyproject.toml")
        self.assertIn('version = "%s"' % __version__, pyproject)
        for rel in ("plugin/plugin.json", "plugin/.codex-plugin/plugin.json"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                self.assertEqual(json.load(f)["version"], __version__, rel)
