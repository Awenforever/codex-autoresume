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


def _panel_js():
    from codex_autoresume.control import PANEL_HTML
    return re.search(r"<script>(.*)</script>", PANEL_HTML, re.S).group(1)


def _fn(js, name):
    """Extract one top-level function by brace matching."""
    i = js.index("function " + name + "(")
    j = js.index("{", i)
    depth = 0
    for k in range(j, len(js)):
        if js[k] == "{":
            depth += 1
        elif js[k] == "}":
            depth -= 1
            if depth == 0:
                return js[i:k + 1]
    raise AssertionError("unterminated function " + name)


def _run_node(harness):
    fd, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(harness)
        r = subprocess.run(["node", path], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30)
        assert r.returncode == 0, r.stderr[:2000]
        return json.loads(r.stdout.strip().splitlines()[-1])
    finally:
        os.unlink(path)


class PanelWeeklyEtaTest(unittest.TestCase):
    """Capped points at the weekly reset; exhausted at the real dry time."""

    def test_rows(self):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        js = _panel_js()
        harness = "\n".join([_fn(js, "etaTime"), _fn(js, "etaRow")]) + r"""
var I18N={en:{weeklyfirst:"Weekly resets first",exhausted:"Exhausted"}};
function L(k){return I18N.en[k]||k}
function fmtMDHM(e){return "T"+Math.round(e)}
function fmtISO(iso){return "I("+iso+")"}
var common={rate_secondary:6.2,eta_secondary_capped:false,
            secondary_used:34,secondary_reset:9999};
var capped=etaRow("w",Object.assign({},common,{eta_secondary:1234,
               eta_secondary_capped:true}),
               "eta_secondary","rate_secondary",
               "secondary_exhausted_at","eta_secondary_capped");
var plain=etaRow("w",Object.assign({},common,{eta_secondary:1234}),
               "eta_secondary","rate_secondary",
               "secondary_exhausted_at","eta_secondary_capped");
var dry=etaRow("w",Object.assign({},common,{eta_secondary:null,
               rate_secondary:null,secondary_used:100,
               secondary_exhausted_at:"2026-10-26T05:21:22Z"}),
               "eta_secondary","rate_secondary",
               "secondary_exhausted_at","eta_secondary_capped");
var dry0=etaRow("w",Object.assign({},common,{eta_secondary:null,
               rate_secondary:null,secondary_used:100,
               secondary_exhausted_at:null}),
               "eta_secondary","rate_secondary",
               "secondary_exhausted_at","eta_secondary_capped");
console.log(JSON.stringify([capped,plain,dry,dry0]));
"""
        capped, plain, dry, dry0 = _run_node(harness)
        self.assertIn("Weekly resets first", capped)
        self.assertNotIn("%/h", capped)
        self.assertIn("T1234", plain)
        self.assertIn("6.2%/h", plain)
        # the real exhaustion instant, never the weekly reset (T9999)
        self.assertIn("Exhausted \u00b7 I(2026-10-26T05:21:22Z)", dry)
        self.assertNotIn("T9999", dry)
        self.assertIn("Exhausted", dry0)
        self.assertNotIn("\u00b7", dry0)
        self.assertNotIn("T9999", dry0)


class PanelLandedTest(unittest.TestCase):
    """Landed is a full reset: no weekly label, and it grays with age."""

    def test_landed_has_no_weekly_label(self):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        js = _panel_js()
        harness = "\n".join([_fn(js, "hexRGB"), _fn(js, "cssRGB"),
                             _fn(js, "mixRGB"), _fn(js, "freshStyle"),
                             _fn(js, "annRowsFor")]) + r"""
var document={documentElement:{}};
function getComputedStyle(){return{getPropertyValue:function(n){
 return {"--warn":"#e0a341","--accent":"#10a37f",
         "--dim":"#87929a"}[n]||""}}}
var I18N={en:{forecast:"Forecast",landed:"Landed",card:"Credits",wk:"Weekly"}};
function L(k){return I18N.en[k]||k}
function kvRow(k,v){return k+"="+v+"\n"}
function newsBox(s){return ""}
function esc(s){return String(s)}
function fmtISO(iso){return "I("+iso+")"}
function fmtMDHM(e){return "T"+Math.round(e)}
var weeklyOnly={announcement:null,credit_events:[],
  reset_events:[{kind:"weekly",at:"2026-10-26T05:21:22Z"},
                {kind:"credit",at:"2026-10-20T01:00:00Z"}]};
var creditNew={announcement:null,reset_events:[],
  credit_events:[{landed_at:"2026-10-27T00:00:00Z",title:"Reset"}]};
console.log(JSON.stringify([annRowsFor(weeklyOnly),annRowsFor(creditNew)]));
"""
        weekly, credit = _run_node(harness)
        self.assertIn("Landed=", weekly)
        self.assertIn("I(2026-10-26T05:21:22Z)", weekly)
        self.assertNotIn("Weekly", weekly)
        self.assertNotIn("\u00b7", weekly)
        self.assertIn("Credits", credit)
        self.assertIn("I(2026-10-27T00:00:00Z)", credit)


class PanelFreshnessTest(unittest.TestCase):
    """Freshness is a color shift toward gray within a 24h horizon."""

    def test_style_decays_with_distance(self):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        js = _panel_js()
        harness = "\n".join([_fn(js, "hexRGB"), _fn(js, "cssRGB"),
                             _fn(js, "mixRGB"),
                             _fn(js, "freshStyle")]) + r"""
var STYLES={"--warn":"#e0a341","--accent":"#10a37f","--dim":"#87929a"};
var document={documentElement:{}};
function getComputedStyle(){return{getPropertyValue:function(n){return STYLES[n]||""}}}
var now=Date.now()/1000;
function rgb(s){var m=/color:rgb\((\d+),(\d+),(\d+)\)/.exec(s);
 return m?[+m[1],+m[2],+m[3]]:null}
console.log(JSON.stringify({
  nearF:rgb(freshStyle(now,"forecast")),
  halfF:rgb(freshStyle(now+12*3600,"forecast")),
  farF:rgb(freshStyle(now+24*3600,"forecast")),
  staleF:rgb(freshStyle(now-48*3600,"forecast")),
  nearL:rgb(freshStyle(now-60,"landed")),
  halfL:rgb(freshStyle(now-12*3600,"landed")),
  oldL:rgb(freshStyle(now-24*3600,"landed")),
  none:freshStyle(null,"landed"),
  rawF:freshStyle(now,"forecast")
}));
"""
        got = _run_node(harness)
        warn, accent, gray = [224, 163, 65], [16, 163, 127], [135, 146, 154]
        self.assertEqual(got["nearF"], warn)
        self.assertEqual(got["farF"], gray)
        self.assertEqual(got["staleF"], gray)
        self.assertEqual(got["nearL"], accent)
        self.assertEqual(got["oldL"], gray)
        for mid, hi, lo in ((got["halfF"], warn, gray),
                            (got["halfL"], accent, gray)):
            self.assertNotEqual(mid, hi)
            self.assertNotEqual(mid, lo)
            for c, a, b in zip(mid, hi, lo):
                self.assertGreaterEqual(c, min(a, b))
                self.assertLessEqual(c, max(a, b))
        self.assertEqual(got["none"], "")
        # color only: no opacity fade, no filter
        self.assertIn("color:rgb(", got["rawF"])
        self.assertNotIn("opacity", got["rawF"])
        self.assertNotIn("filter", got["rawF"])


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
        self.assertIn("hlNews(esc(stripTco(e.text)))", PANEL_HTML)
        self.assertNotIn("hlNews(esc(e.text))", PANEL_HTML)
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
  tco: stripTco("Terra is back now https://t.co/1ilnCkAPuy"),
  tcoLead: stripTco("https://t.co/Fgy0dBAHsJ has landed"),
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
        self.assertEqual(got["tco"], "Terra is back now")
        self.assertEqual(got["tcoLead"],
                         "https://t.co/Fgy0dBAHsJ has landed")
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
        self.assertIn('onclick="onLogo()"', PANEL_HTML)
        self.assertIn('id="updDot"', PANEL_HTML)
        self.assertIn('<div id="updOverlay"', PANEL_HTML)
        self.assertIn('id="updGo"', PANEL_HTML)
        self.assertRegex(PANEL_HTML, r"\.upddot\{[^}]*background:#ff4d4f")
        self.assertRegex(PANEL_HTML, r"\.logowrap\.upd \.upddot\{display:block\}")

    def test_update_requires_the_confirmed_api_call(self):
        from codex_autoresume.control import PANEL_HTML
        self.assertIn('api("/api/update",{confirm:true})', PANEL_HTML)
        self.assertIn("syncUpdateUi(s)", PANEL_HTML)

    def test_idle_logo_click_runs_a_forced_check(self):
        from codex_autoresume.control import PANEL_HTML
        self.assertIn('api("/api/update/check",{},15000)', PANEL_HTML)

    def test_update_strings_are_bilingual(self):
        from codex_autoresume.control import PANEL_HTML
        for key in ("updTitle", "updNote", "updGo", "updRestarting",
                    "updFailed", "updSlow", "logoTip", "updChecking",
                    "updCurrent", "updFound", "updCheckFail", "updOff"):
            self.assertEqual(PANEL_HTML.count(key + ':'), 2, key)


class PanelUpdateCheckTest(unittest.TestCase):
    """Logo click: red dot opens the modal, otherwise force a fresh check."""

    def test_logo_click_paths(self):
        if not shutil.which("node"):
            self.skipTest("node not installed")
        js = _panel_js()
        harness = _fn(js, "onLogo") + r"""
var calls=[];
var mode="update";
function L(k){return k+":"}
function toast(m,ms){calls.push(["toast",m])}
function openUpdate(){calls.push(["open"])}
function syncUpdateUi(s){calls.push(["sync",s.update.latest])}
function api(p,o,ms){
 calls.push(["api",p,ms]);
 var u={enabled:true,current:"0.3.53",latest:"0.3.54",available:true,
  can_update:true,in_progress:false,error:"",reason:""};
 if(mode==="current"){u.latest="0.3.53";u.available=false;u.can_update=false}
 if(mode==="error"){u.latest=null;u.available=false;u.can_update=false;
  u.error="offline"}
 return Promise.resolve({update:u})
}
var window={__s:{update:{enabled:true,available:false,can_update:false,
 in_progress:false}}};
function reset(){calls=[];
 window.__s.update={enabled:true,available:false,can_update:false,
  in_progress:false}}
onLogo();
setTimeout(function(){
 var found=calls.slice();
 reset();mode="current";onLogo();
 setTimeout(function(){
  var current=calls.slice();
  reset();mode="error";onLogo();
  setTimeout(function(){
   var failed=calls.slice();
   reset();
   window.__s.update={enabled:true,available:true,can_update:true,
    in_progress:false,latest:"9.9.9"};
   onLogo();
   console.log(JSON.stringify({found:found,current:current,
    failed:failed,reddot:calls.slice()}));
  },10);
 },10);
},10);
"""
        got = _run_node(harness)
        found = got["found"]
        self.assertIn(["api", "/api/update/check", 15000], found)
        self.assertTrue(any(c[0] == "toast" and c[1].startswith("updChecking:")
                            for c in found))
        self.assertIn(["sync", "0.3.54"], found)
        self.assertIn(["open"], found)
        current = got["current"]
        self.assertIn(["toast", "updCurrent:0.3.53"], current)
        self.assertNotIn(["open"], current)
        failed = got["failed"]
        self.assertIn(["toast", "updCheckFail:offline"], failed)
        self.assertNotIn(["open"], failed)
        # with a red dot the logo opens the modal without touching the network
        self.assertEqual(got["reddot"], [["open"]])
