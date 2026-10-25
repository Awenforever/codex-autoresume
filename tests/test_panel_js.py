"""Guard: the panel's embedded <script> must be valid JavaScript.

A Python-side syntax check passes even when the JS inside PANEL_HTML is
broken (the page then sits at "加载中" forever with no visible error), so
parse the script with node whenever it is installed.
"""
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


if __name__ == "__main__":
    unittest.main()
