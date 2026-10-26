"""Guard: the panel's embedded <script> must be valid JavaScript.

A Python-side syntax check passes even when the JS inside PANEL_HTML is
broken (the page then sits at "加载中" forever with no visible error), so
parse the script with node whenever it is installed.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest


class PanelJsSyntaxTest(unittest.TestCase):
    def test_script_parses(self):
        from codex_autoresume.control import PANEL_HTML
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        m = re.search(r"<script>(.*)</script>", PANEL_HTML, re.S)
        self.assertTrue(m and m.group(1).strip(), "no <script> in panel")
        fd, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(m.group(1))
            r = subprocess.run([node, "--check", path],
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(
                r.returncode, 0,
                "panel JS syntax error:\n%s" % (r.stderr or "")[:2000])
        finally:
            os.unlink(path)


class PanelNewsHighlightTest(unittest.TestCase):
    """The news lexicon must give "reset" per-letter color spans."""

    @staticmethod
    def _rainbow(word: str) -> str:
        return ('<span class="hl-reset">'
                + "".join('<span class="rl">%s</span>' % ch for ch in word)
                + "</span>")

    def test_reset_keyword_gets_per_letter_spans(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        from codex_autoresume.control import PANEL_HTML
        js = re.search(r"<script>(.*)</script>", PANEL_HTML, re.S).group(1)
        m = re.search(r"function hlNews\(t\)\{.*?\}\)\}", js, re.S)
        self.assertTrue(m, "hlNews not found in panel script")
        harness = m.group(0) + r"""
const out = [
  hlNews("Day 20/ Rest and reset"),
  hlNews("Landed as announced reset at 15:00"),
  hlNews("Preset values keep their tone"),
  hlNews("Two resets happened")
];
console.log(JSON.stringify(out));
"""
        fd, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(harness)
            r = subprocess.run([node, path], capture_output=True, text=True,
                               timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr[:2000])
            plain, landed, preset, plural = json.loads(
                r.stdout.strip().splitlines()[-1])
        finally:
            os.unlink(path)
        self.assertIn(self._rainbow("reset"), plain)
        self.assertIn('class="hl-ok"', landed)
        self.assertIn(self._rainbow("reset"), landed)
        self.assertNotIn("hl-reset", preset)
        self.assertIn(self._rainbow("resets"), plural)

    def test_reset_highlight_has_no_pill_background(self):
        from codex_autoresume.control import PANEL_HTML
        self.assertNotIn("--resetbg", PANEL_HTML)
        self.assertRegex(PANEL_HTML, r"\.hl-reset\{font-weight:700\}")


if __name__ == "__main__":
    unittest.main()


class PanelI18nCoverageTest(unittest.TestCase):
    """Every L("key") used in the panel must exist in BOTH language dicts."""

    def test_all_keys_covered(self):
        import re as _re
        from codex_autoresume.control import PANEL_HTML
        m = _re.search(r"<script>(.*)</script>", PANEL_HTML, _re.S)
        js = m.group(1)
        used = set(_re.findall(r'L\("([A-Za-z0-9_]+)"\)', js))
        # parse each language dict
        missing = []
        for lang in ("zh", "en"):
            m2 = _re.search(lang + r':\{(.*?)\}\}', js, _re.S)
            assert m2, lang + " dict not found"
            keys = set(_re.findall(r'([A-Za-z0-9_]+):"', m2.group(1)))
            miss = sorted(used - keys - {"g_" + x for x in
                          ("usage_limited", "budget_limited", "active",
                           "paused", "blocked", "complete")}
                          - {"ocMap"} if False else used - keys)
            # g_* status keys are looked up dynamically (g_+status)
            miss = [k for k in miss if not k.startswith("g_")]
            missing += [lang + ":" + k for k in miss]
        self.assertEqual(missing, [],
                         "i18n keys used but not defined: %s" % missing)
