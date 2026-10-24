"""Official reset announcements (best-effort external monitor).

Polls a community tracker (default: opentherank.com/codex-reset, which
logs Thibault "Tibo" Sottiaux's X posts about global Codex usage-limit
resets and banked credits). The page is server-rendered static HTML, so we
extract two things:

- the latest recorded global reset ("Latest recorded reset: <date>")
- any advance landing notice ("Landing tomorrow before 10am PST ...")

Arrival (到账) is measured locally by the daemon's credit-grant detection,
not by this feed. All parsing is tolerant: any change in page wording just
yields empty fields, never errors.
"""

from __future__ import annotations

import re
import time
import urllib.request

DEFAULT_URL = "https://opentherank.com/codex-reset"
UA = "codex-autoresume/0.2 (+https://github.com/codex-autoresume)"

_LATEST_RE = re.compile(
    r"Latest recorded reset:\s*([^<]{5,120}?)(?:<|\s{2,}|$)", re.IGNORECASE)
_LANDING_RE = re.compile(r"Landing[^<>.]{5,140}", re.IGNORECASE)


def fetch_announcement(url: str = DEFAULT_URL,
                       timeout: float = 15) -> dict:
    """Fetch and parse the tracker page. Never raises."""
    out = {"url": url, "latest_reset": None, "landing": None,
           "fetched_at": None}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                   "Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read(400_000).decode("utf-8", errors="replace")
        m = _LATEST_RE.search(html)
        if m:
            out["latest_reset"] = m.group(1).strip()
        m = _LANDING_RE.search(html)
        if m:
            out["landing"] = m.group(0).strip()
        out["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        pass
    return out
