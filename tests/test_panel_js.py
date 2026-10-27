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


class PanelThemeTest(unittest.TestCase):
    """One theme button cycles auto/light/dark; auto follows the live media query."""

    def test_single_button_has_three_modes(self):
        from codex_autoresume.control import PANEL_HTML
        self.assertIn('id="themeBtn"', PANEL_HTML)
        self.assertIn('data-mode="auto"', PANEL_HTML)
        self.assertIn('onclick="cycleTheme()"', PANEL_HTML)
        self.assertNotIn("themebar", PANEL_HTML)
        self.assertNotIn('data-theme="auto"', PANEL_HTML)
        self.assertIn('var THEME="auto"', PANEL_HTML)
        self.assertNotIn("toggleTheme", PANEL_HTML)
        self.assertIn('addEventListener("change",_themeFollow)', PANEL_HTML)

    def test_cycle_order_auto_light_dark(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        from codex_autoresume.control import PANEL_HTML
        js = re.search(r"<script>(.*)</script>", PANEL_HTML, re.S).group(1)
        m = re.search(r"function cycleTheme\(\)\{[^\n]*\}", js)
        self.assertTrue(m, "cycleTheme not found in panel script")
        harness = m.group(0) + r"""
const out = [THEME];
for (let i = 0; i < 4; i++) { cycleTheme(); out.push(THEME); }
console.log(JSON.stringify(out));
"""
        fd, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write('var THEME="auto";\nfunction setTheme(m){THEME=m}\n' + harness)
            r = subprocess.run([node, path], capture_output=True, text=True,
                               timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr[:2000])
            got = json.loads(r.stdout.strip().splitlines()[-1])
        finally:
            os.unlink(path)
        self.assertEqual(got, ["auto", "light", "dark", "auto", "light"])

    def test_auto_resolver_tracks_media_query(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        from codex_autoresume.control import PANEL_HTML
        js = re.search(r"<script>(.*)</script>", PANEL_HTML, re.S).group(1)
        m = re.search(r"function themeIsLight\(\)\{[^\n]*\}", js)
        self.assertTrue(m, "themeIsLight not found in panel script")
        harness = m.group(0) + r"""
const cases = [
  ["auto", true], ["auto", false],
  ["dark", true], ["dark", false],
  ["light", true], ["light", false]
];
const out = cases.map(([mode, mq]) => {
  THEME = mode; MQL = {matches: mq};
  return themeIsLight();
});
console.log(JSON.stringify(out));
"""
        fd, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("var THEME, MQL;\n" + harness)
            r = subprocess.run([node, path], capture_output=True, text=True,
                               timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr[:2000])
            got = json.loads(r.stdout.strip().splitlines()[-1])
        finally:
            os.unlink(path)
        self.assertEqual(got, [True, False, False, False, True, True])


class PanelWeeklyCapTest(unittest.TestCase):
    """A capped weekly ETA says "resets first" and drops the %/h tail."""

    def test_capped_row(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        from codex_autoresume.control import PANEL_HTML
        js = re.search(r"<script>(.*)</script>", PANEL_HTML, re.S).group(1)
        m = re.search(r"function etaRow\(.*?\}", js, re.S)
        self.assertTrue(m, "etaRow not found in panel script")
        harness = m.group(0) + r"""
var I18N = {en: {weeklyfirst: "Weekly resets first",
                 exhausted: "Exhausted"}};
function L(k){ return I18N.en[k] || k }
function fmtMDHM(e){ return "T" + Math.round(e) }
const q = {eta_secondary: 1234, rate_secondary: 6.2,
           eta_secondary_capped: true, secondary_used: 34,
           secondary_reset: 9999};
const capped = etaRow("w", q, "eta_secondary", "rate_secondary",
                      "secondary_reset", "eta_secondary_capped");
const plain = etaRow("w", {eta_secondary: 1234, rate_secondary: 6.2,
                           secondary_used: 34, eta_secondary_capped: false},
                     "eta_secondary", "rate_secondary",
                     "secondary_reset", "eta_secondary_capped");
console.log(JSON.stringify([capped, plain]));
"""
        fd, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(harness)
            r = subprocess.run([node, path], capture_output=True, text=True,
                               timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr[:2000])
            capped, plain = json.loads(r.stdout.strip().splitlines()[-1])
        finally:
            os.unlink(path)
        self.assertIn("Weekly resets first", capped)
        self.assertNotIn("%/h", capped)
        self.assertIn("T1234", plain)
        self.assertIn("6.2%/h", plain)


class PanelVersionTest(unittest.TestCase):
    """The running daemon's version sits next to the title in muted gray."""

    def test_version_next_to_title(self):
        import codex_autoresume
        from codex_autoresume.control import PANEL_HTML
        self.assertIn('<span class="ver">v%s</span>'
                      % codex_autoresume.__version__, PANEL_HTML)
        self.assertNotIn("__CXR_VERSION__", PANEL_HTML)
        self.assertRegex(PANEL_HTML,
                         r"\.ver\{[^}]*font-size:11px[^}]*color:var\(--dim\)")

    def test_version_survives_language_switch(self):
        from codex_autoresume.control import PANEL_HTML
        # applyLang() rewrites textContent of [data-i18n]; the version span
        # must stay OUTSIDE the h1 or switching language would erase it
        self.assertIn('<h1 data-i18n="title">Codex Guard</h1>'
                      '<span class="ver">', PANEL_HTML)


if __name__ == "__main__":
    unittest.main()


class PanelHeaderTest(unittest.TestCase):
    """Header chrome: theme + language icons only, English default, no chips."""

    def test_header_has_theme_and_language_icons_only(self):
        from codex_autoresume.control import PANEL_HTML
        self.assertRegex(
            PANEL_HTML,
            r'<button class="btn-mini icon" id="langBtn"[^>]*>\s*<svg')
        self.assertIn('id="themeBtn"', PANEL_HTML)
        self.assertNotIn('id="refreshBtn"', PANEL_HTML)
        self.assertNotIn('L("refresh")', PANEL_HTML)
        self.assertNotIn('refreshed', PANEL_HTML)
        self.assertIn('setInterval(refresh,15000)', PANEL_HTML)

    def test_english_is_default_language(self):
        from codex_autoresume.control import PANEL_HTML
        self.assertIn('<html lang="en">', PANEL_HTML)
        self.assertIn('<title>Codex Guard Console</title>', PANEL_HTML)
        self.assertIn('<h1 data-i18n="title">Codex Guard</h1>', PANEL_HTML)
        self.assertIn('var LNG="en"', PANEL_HTML)
        self.assertIn('localStorage.getItem("cglang")||"en"', PANEL_HTML)

    def test_status_chips_reduced_to_dry_run(self):
        from codex_autoresume.control import PANEL_HTML
        self.assertNotIn('L("running")', PANEL_HTML)
        self.assertNotIn('L("pausedC")', PANEL_HTML)
        self.assertNotIn('"LIVE"', PANEL_HTML)
        self.assertIn('s.dry_run?\'<span class="chip dry">DRY-RUN</span>\':""',
                      PANEL_HTML)


class PanelEscapingTest(unittest.TestCase):
    """Dynamic text reaches innerHTML only through esc()/jsq()."""

    def test_news_text_and_thread_ids_go_through_escapers(self):
        from codex_autoresume.control import PANEL_HTML
        self.assertIn("hlNews(esc(e.text))", PANEL_HTML)
        self.assertNotIn("hlNews(e.text)", PANEL_HTML)
        self.assertIn('title="\'+L("postAt")+\'">\'+\n esc(fmtPostAt(e.at))',
                      PANEL_HTML)
        self.assertIn("setT(\\''+jsq(t.id)", PANEL_HTML)
        self.assertNotIn("setT(\\''+t.id", PANEL_HTML)

    def test_esc_and_jsq_neutralise_markup(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        from codex_autoresume.control import PANEL_HTML
        js = re.search(r"<script>(.*)</script>", PANEL_HTML, re.S).group(1)
        m = re.search(r"(function esc\(s\)\{.*?)(?=function newsBox)", js,
                      re.S)
        self.assertTrue(m, "esc/hlNews not found in panel script")
        harness = m.group(1) + r"""
const out = {
  esc: esc("<img src=x onerror='alert(1)'>&\""),
  news: hlNews(esc("<img src=x onerror=alert(1)> reset at 15:00")),
  jsq: jsq("a'b\\c<d\"e&f")
};
console.log(JSON.stringify(out));
"""
        fd, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(harness)
            r = subprocess.run([node, path], capture_output=True, text=True,
                               timeout=30)
            self.assertEqual(r.returncode, 0, r.stderr[:2000])
            got = json.loads(r.stdout.strip().splitlines()[-1])
        finally:
            os.unlink(path)
        self.assertIn("&lt;img", got["esc"])
        self.assertIn("&#39;", got["esc"])
        self.assertIn("&quot;", got["esc"])
        self.assertIn("&amp;", got["esc"])
        self.assertNotIn("<img", got["esc"])
        self.assertNotIn("<img", got["news"])
        self.assertIn("&lt;img", got["news"])
        self.assertIn("hl-reset", got["news"])
        self.assertEqual(got["jsq"], "a\\&#39;b\\\\c&lt;d&quot;e&amp;f")


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


class PanelUpdateUiTest(unittest.TestCase):
    """Red dot on the logo + confirmation modal for self-update."""

    def test_logo_is_the_update_button_with_a_dot(self):
        from codex_autoresume.control import PANEL_HTML
        self.assertIn('id="logoWrap"', PANEL_HTML)
        self.assertIn('id="logoBtn"', PANEL_HTML)
        self.assertIn('onclick="openUpdate()"', PANEL_HTML)
        self.assertIn('id="updDot"', PANEL_HTML)
        self.assertIn('<div id="updOverlay"', PANEL_HTML)
        self.assertIn('id="updGo"', PANEL_HTML)
        self.assertRegex(PANEL_HTML, r"\.upddot\{[^}]*background:#ff4d4f")
        self.assertRegex(PANEL_HTML, r"\.logowrap\.upd \.upddot\{display:block\}")

    def test_update_requires_the_confirmed_api_call(self):
        from codex_autoresume.control import PANEL_HTML
        self.assertIn('api("/api/update",{confirm:true})', PANEL_HTML)
        self.assertIn("syncUpdateUi(s)", PANEL_HTML)

    def test_update_strings_are_bilingual(self):
        from codex_autoresume.control import PANEL_HTML
        for key in ("updTitle", "updNote", "updGo", "updRestarting",
                    "updFailed", "updSlow"):
            self.assertEqual(PANEL_HTML.count(key + ':'), 2, key)
