"""Official reset announcements (best-effort external monitor).

Polls a community tracker (default: opentherank.com/codex-reset, which
logs Thibault "Tibo" Sottiaux's X posts about global Codex usage-limit
resets and banked credits). The page is server-rendered static HTML.

Extracted:
- the banner status: either ``Landed as announced, around HH:MM ...``
  (already landed -- never shown as a forecast) or an upcoming
  ``Landing tomorrow before ...`` phrase
- the latest recorded global reset ("Latest recorded reset: <date>")

All clock times in the text are converted to the machine's local timezone
(``HH:MM UTC`` mentions get an appended local reading; full timestamps are
reformatted). Parsing is tolerant: any wording change yields empty fields,
never errors. Arrival (到账) times come from the daemon's local credit
detection, not from this feed.
"""

from __future__ import annotations

import re
import time
import urllib.request
from datetime import datetime, timezone

DEFAULT_URL = "https://opentherank.com/codex-reset"
UA = "codex-autoresume/0.2 (+https://github.com/codex-autoresume)"

_LATEST_RE = re.compile(
    r"Latest recorded reset:\s*((?:[^<]|<[^>]+>){5,300}?)\bReset\b",
    re.IGNORECASE)
_LANDED_RE = re.compile(r"Landed as announced[^<]{0,150}", re.IGNORECASE)
_LANDING_RE = re.compile(
    r"Landing\s+(?:tomorrow|today|next\s+[a-z]+)?\s*"
    r"(?:before|at|around|by)\s*[^<>()!?.]{3,60}", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_UTC_CLOCK = re.compile(r"(\d{1,2}):(\d{2})\s*UTC")
_FULL_UTC = re.compile(
    r"([A-Z][a-z]{2}),\s*([A-Z][a-z]{2})\s+(\d{1,2}),\s*(\d{4}),"
    r"\s*(\d{1,2}):(\d{2})\s*UTC")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
           "Oct", "Nov", "Dec")


def _strip_tags(s: str) -> str:
    return _TAG_RE.sub("", s).replace("&amp;", "&").strip()


def _localize_clocks(text: str) -> str:
    """Append a local-time reading next to every ``HH:MM UTC`` mention."""
    def rep(m: "re.Match") -> str:
        h, mi = int(m.group(1)), int(m.group(2))
        try:
            now_local = datetime.now(timezone.utc).astimezone()
            naive = datetime.now(timezone.utc).replace(
                hour=min(h, 23), minute=min(mi, 59), second=0, microsecond=0)
            loc = naive.astimezone()
            return "%02d:%02d UTC（本机 %s）" % (h, mi, loc.strftime("%H:%M"))
        except Exception:
            return m.group(0)
    return _UTC_CLOCK.sub(rep, text)


def parse_full_utc(text: str) -> datetime | None:
    """Parse ``Fri, Oct 23, 2026, 17:00 UTC`` into an aware datetime."""
    m = _FULL_UTC.search(text)
    if not m:
        return None
    try:
        dt = datetime(int(m.group(4)), _MONTHS.index(m.group(2)) + 1,
                      int(m.group(3)), int(m.group(5)), int(m.group(6)),
                      tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def fetch_announcement(url: str = DEFAULT_URL,
                       timeout: float = 15) -> dict:
    """Fetch and parse the tracker page. Never raises."""
    out = {"url": url, "latest_reset": None, "latest_reset_local": None,
           "status": None, "status_kind": None, "landing": None,
           "fetched_at": None}
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                   "Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read(400_000).decode("utf-8", errors="replace")

        m = _LATEST_RE.search(html)
        if m:
            raw = _strip_tags(m.group(1))[:160]
            out["latest_reset"] = raw
            dt = parse_full_utc(raw)
            if dt:
                out["latest_reset_local"] = dt.astimezone().strftime(
                    "%m-%d %H:%M")

        landed = _LANDED_RE.search(html)
        upcoming = _LANDING_RE.search(html)
        if landed:
            out["status"] = _localize_clocks(_strip_tags(landed.group(0)))
            out["status_kind"] = "landed"
        elif upcoming:
            out["status"] = _localize_clocks(_strip_tags(upcoming.group(0)))
            out["status_kind"] = "upcoming"
            out["landing"] = out["status"]
        out["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        pass
    return out
