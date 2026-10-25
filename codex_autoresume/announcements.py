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
from datetime import datetime, timedelta, timezone

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
_PACIFIC_CLOCK = re.compile(
    r"((?:tomorrow|today)[^0-9]{0,12})?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*"
    r"(PST|PDT|Pacific(?:\s+Time)?)\b", re.IGNORECASE)
_FULL_UTC = re.compile(
    r"([A-Z][a-z]{2}),\s*([A-Z][a-z]{2})\s+(\d{1,2}),\s*(\d{4}),"
    r"\s*(\d{1,2}):(\d{2})\s*UTC")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
           "Oct", "Nov", "Dec")


def _pacific_offset_hours(now: datetime) -> int:
    """PST (-8) or PDT (-7) by calendar approximation (2nd Sun Mar ..
    1st Sun Nov)."""
    import calendar
    year = now.year
    dst_start = datetime(year, 3, 1)
    while dst_start.weekday() != 6 or dst_start.day < 8:
        dst_start = dst_start.replace(day=dst_start.day + 1)
    dst_end = datetime(year, 11, 1)
    while dst_end.weekday() != 6:
        dst_end = dst_end.replace(day=dst_end.day + 1)
    return -7 if dst_start <= now.replace(tzinfo=None) < dst_end else -8


def _localize_clocks(text: str) -> str:
    """Append local-time readings next to UTC and Pacific clock times."""
    def rep_utc(m: "re.Match") -> str:
        h, mi = int(m.group(1)), int(m.group(2))
        try:
            loc = datetime.now(timezone.utc).replace(
                hour=min(h, 23), minute=min(mi, 59)).astimezone()
            return "%02d:%02d UTC（本机 %s）" % (h, mi, loc.strftime("%H:%M"))
        except Exception:
            return m.group(0)

    def rep_pac(m: "re.Match") -> str:
        prefix, h, mi, ampm, zone = m.groups()
        try:
            now = datetime.now(timezone.utc)
            hour = int(h)
            if ampm:
                ap = ampm.lower()
                if ap == "pm" and hour < 12:
                    hour += 12
                if ap == "am" and hour == 12:
                    hour = 0
            if hour > 23:
                return m.group(0)
            offset = _pacific_offset_hours(now)
            base = now.replace(hour=hour, minute=int(mi or 0), second=0,
                               microsecond=0)
            if prefix and "tomorrow" in prefix.lower():
                base += timedelta(days=1)
            loc = (base + timedelta(hours=offset)).astimezone()
            return "%s（本机 %s）" % (m.group(0).rstrip(),
                                     loc.strftime("%m-%d %H:%M"))
        except Exception:
            return m.group(0)

    text = _UTC_CLOCK.sub(rep_utc, text)
    return _PACIFIC_CLOCK.sub(rep_pac, text)


def _strip_tags(s: str) -> str:
    return _TAG_RE.sub("", s).replace("&amp;", "&").strip()


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


def _pacific_local(text: str) -> str | None:
    """First Pacific clock in *text* -> local "%m-%d %H:%M" (or None)."""
    m = _PACIFIC_CLOCK.search(text)
    if not m:
        return None
    prefix, h, mi, ampm, zone = m.groups()
    try:
        now = datetime.now(timezone.utc)
        hour = int(h)
        if ampm:
            ap = ampm.lower()
            if ap == "pm" and hour < 12:
                hour += 12
            if ap == "am" and hour == 12:
                hour = 0
        if hour > 23:
            return None
        base = now.replace(hour=hour, minute=int(mi or 0), second=0,
                           microsecond=0)
        if prefix and "tomorrow" in prefix.lower():
            base += timedelta(days=1)
        loc = (base + timedelta(hours=_pacific_offset_hours(now))).astimezone()
        return loc.strftime("%m-%d %H:%M")
    except Exception:
        return None


# post timeline: <time class="cx-post-t" datetime="ISO"> ... <blockquote
# class="cx-quote"><p>TEXT</p> — every reset-related post with its own
# publication timestamp (authoritative, straight from the tracker)
_POST_RE = re.compile(
    r'<time class="cx-post-t" datetime="([^"]+)"[^>]*>.*?'
    r'<blockquote class="cx-quote"[^>]*>\s*<p[^>]*>(.*?)</p>',
    re.S)

_LOCAL_NOTE = re.compile(r"（本机[^）]*）")


def forecast_epoch(text: str) -> int | None:
    """The Pacific clock in an upcoming announcement -> unix epoch.

    "Landing tomorrow before 10am PST" is the poster's own frame (Tibo
    posts in Pacific time); return the absolute instant so the panel can
    render it in the reader's machine timezone.
    """
    m = _PACIFIC_CLOCK.search(text or "")
    if not m:
        return None
    prefix, h, mi, ampm, zone = m.groups()
    try:
        hour = int(h)
        if ampm:
            ap = ampm.lower()
            if ap == "pm" and hour < 12:
                hour += 12
            if ap == "am" and hour == 12:
                hour = 0
        if hour > 23:
            return None
        base = datetime.now(timezone.utc).replace(
            hour=hour, minute=int(mi or 0), second=0, microsecond=0)
        if prefix and "tomorrow" in prefix.lower():
            base += timedelta(days=1)
        # Pacific = UTC + offset (offset is negative): UTC = Pacific - offset
        utc_instant = base - timedelta(hours=_pacific_offset_hours(base))
        return int(utc_instant.timestamp())
    except Exception:
        return None


def _status_short(text: str) -> str:
    """Compact one-liner: drop localized-time notes, trim hard at 70 chars."""
    t = _LOCAL_NOTE.sub("", text or "")
    t = t.strip()
    return t[:160] + ("…" if len(t) > 160 else "")


def fetch_announcement(url: str = DEFAULT_URL,
                       timeout: float = 15) -> dict:
    """Fetch and parse the tracker page. Never raises."""
    out = {"url": url, "latest_reset": None, "latest_reset_local": None,
           "status": None, "status_kind": None, "landing": None,
           "status_short": None, "forecast_epoch": None, "upcoming_local": None,
           "posts": [], "fetched_at": None}
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
                # machine timezone (datetime.astimezone reads the OS
                # setting); never IP-geolocated
                out["latest_reset_local"] = dt.astimezone().strftime(
                    "%m-%d %H:%M")

        posts = []
        for m in _POST_RE.finditer(html):
            posts.append({"at": m.group(1),
                          "text": _strip_tags(m.group(2))[:500]})
        out["posts"] = posts[:40]

        landed = _LANDED_RE.search(html)
        upcoming = _LANDING_RE.search(html)
        # display text stays VERBATIM (source timezone preserved): local
        # conversion proved ambiguous (proxy-shifted machine timezones),
        # so users read the original announcement exactly as published.
        if landed:
            out["status"] = _strip_tags(landed.group(0))
            out["status_kind"] = "landed"
        elif upcoming:
            raw = _strip_tags(upcoming.group(0))
            out["status"] = raw
            out["status_kind"] = "upcoming"
            out["landing"] = raw
            out["forecast_epoch"] = forecast_epoch(raw)
        if out["status"]:
            out["status_short"] = _status_short(out["status"])
        out["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        pass
    return out
