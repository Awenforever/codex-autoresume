"""Reset announcements and Tibo news timeline (best-effort external monitor).

Three public sources are merged into one chronological news log:
- opentherank.com/codex-reset (HTML): reset announcements and forecast banner
- codex-reset.com/api/feed (JSON): structured X timeline, including the
  28-day challenge's ship/feature days and selected replies
- willreset.com/tibo (HTML): full recent Tibo timeline, including ordinary
  posts that are not reset announcements

From the tracker pages we also extract:
- the banner status: either ``Landed as announced, around HH:MM ...``
  (already landed -- never shown as a forecast) or an upcoming
  ``Landing tomorrow before ...`` phrase
- the latest recorded global reset ("Latest recorded reset: <date>")

All clock times in the text are converted to the machine's local timezone
(``HH:MM UTC`` mentions get an appended local reading; full timestamps are
reformatted). Parsing is tolerant: any wording change yields empty fields,
never errors. Arrival (到账) times come from the daemon's local credit
detection, not from these feeds.
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone

DEFAULT_URL = "https://opentherank.com/codex-reset"
CODEX_RESET_URL = "https://codex-reset.com/api/feed"
WILLRESET_URL = "https://willreset.com/tibo"
UA = "codex-autoresume/0.3 (+https://github.com/Awenforever/codex-autoresume)"

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
    return html.unescape(_TAG_RE.sub("", s)).strip()


def _clean(text: str) -> str:
    """Collapse markup whitespace so the panel shows one tidy line."""
    return re.sub(r"\s+", " ", text or "").strip()


def _norm_iso(value) -> str | None:
    """Any ISO-8601 value -> ``YYYY-MM-DDTHH:MM:SSZ`` (UTC)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


_X_EPOCH_MS = 1288834974657


def _snowflake_iso(tweet_id) -> str | None:
    """X status id -> its publication instant (snowflake epoch)."""
    try:
        ms = (int(str(tweet_id)) >> 22) + _X_EPOCH_MS
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def _tweet_id(url: str) -> str:
    m = re.search(r"/status/(\d+)", url or "")
    return m.group(1) if m else ""


def _relative_iso(text: str, now: datetime | None = None) -> str | None:
    """willreset's "2 hours ago" -> an approximate absolute instant."""
    t = (text or "").lower()
    now = now or datetime.now(timezone.utc)
    if "just now" in t:
        delta = timedelta(0)
    elif "yesterday" in t:
        delta = timedelta(days=1)
    else:
        m = re.search(r"(\d+)\s*(minute|hour|day|week)", t)
        if not m:
            return None
        n = int(m.group(1))
        delta = {"minute": timedelta(minutes=n),
                 "hour": timedelta(hours=n),
                 "day": timedelta(days=n),
                 "week": timedelta(weeks=n)}[m.group(2)]
    return _norm_iso((now - delta).isoformat())


def _http_get(url: str, accept: str, timeout: float,
              limit: int = 600_000) -> str:
    """One GET with a FRESH opener.

    urllib's process-global opener caches the system proxy on first use, so
    when the local proxy app rotates its port every later fetch would keep
    dialing the dead port. Build the opener per request instead.
    """
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": accept})
    proxy = None
    try:
        from .ratelimits import _wininet_proxy
        proxy = _wininet_proxy()
    except Exception:
        proxy = None
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        opener = urllib.request.build_opener()
    with opener.open(req, timeout=timeout) as resp:
        return resp.read(limit).decode("utf-8", errors="replace")


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
    r'<blockquote class="cx-quote"[^>]*cite="([^"]*)"[^>]*>\s*<p[^>]*>(.*?)</p>',
    re.S)
_PENDING_ID_RE = re.compile(r'data-pending-id="([^"]+)"')

# willreset timeline: <li class="log-item"> with a pill label, relative
# time, full text and the canonical X status link (id -> exact timestamp)
_WR_ITEM_RE = re.compile(r'<li class="log-item">(.*?)</li>', re.S)
_WR_PILL_RE = re.compile(
    r'<span class="pill ([a-z-]+)"[^>]*>([^<]*)</span>', re.IGNORECASE)
_WR_TEXT_RE = re.compile(
    r'<p class="log-item-text[^"]*">(.*?)</p>', re.S)
_WR_LINK_RE = re.compile(
    r'href="(https://x\.com/[^"]*/status/(\d+))"')
_WR_TIME_RE = re.compile(r'<span class="log-item-time">([^<]+)</span>')

_LOCAL_NOTE = re.compile(r"（本机[^）]*）")


def forecast_epoch(text: str, posted_iso: str | None = None) -> int | None:
    """A Pacific clock in an announcement -> unix epoch.

    The poster's frame is Pacific time, so every anchor is Pacific:
    "tomorrow" means the day AFTER THE POST (in Pacific), and a bare
    clock time that has already passed when the post went out means its
    NEXT occurrence ("by midnight PST" posted 20:40 = 00:00 that night).
    """
    # midnight/noon carry no digits for the clock regex
    t = re.sub(r"(\bmidnight\b|midnight)", "12:00 am", text or "", flags=re.IGNORECASE)
    t = re.sub(r"(\bnoon\b|noon)", "12:00 pm", t, flags=re.IGNORECASE)
    m = _PACIFIC_CLOCK.search(t)
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
        if posted_iso:
            try:
                post_utc = datetime.fromisoformat(
                    posted_iso.replace("Z", "+00:00"))
            except ValueError:
                post_utc = None
        else:
            post_utc = None
        if post_utc is None:
            post_utc = datetime.now(timezone.utc)
        offset = _pacific_offset_hours(post_utc)
        post_pt = post_utc + timedelta(hours=offset)     # PT wall clock
        base_pt = post_pt.replace(hour=hour, minute=int(mi or 0),
                                  second=0, microsecond=0)
        if prefix and "tomorrow" in prefix.lower():
            base_pt += timedelta(days=1)
        elif not prefix and base_pt <= post_pt:
            base_pt += timedelta(days=1)                 # next occurrence
        # Pacific = UTC + offset (offset is negative): UTC = Pacific - offset
        target_utc = base_pt - timedelta(hours=offset)
        return int(target_utc.timestamp())
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
        page = _http_get(url, "text/html", timeout, 400_000)

        m = _LATEST_RE.search(page)
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
        for m in _POST_RE.finditer(page):
            cite = m.group(2)
            posts.append({
                "id": _tweet_id(cite), "at": _norm_iso(m.group(1)),
                "cite": cite, "url": cite,
                "text": _clean(_strip_tags(m.group(3)))[:600],
                "kind": "post", "source": "opentherank"})
        out["posts"] = posts[:40]

        landed = _LANDED_RE.search(page)
        upcoming = _LANDING_RE.search(page)
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
        # the forecast comes from the page's OWN pending marker
        # (data-pending-id -> the post it cites), never from the first
        # "Landing ..." text match, which can be a days-old post
        pend = _PENDING_ID_RE.search(page)
        if pend:
            pid = pend.group(1)
            for q in posts:
                if pid and pid in (q.get("cite") or ""):
                    out["forecast_epoch"] = forecast_epoch(q["text"], q["at"])
                    break
        if out["status"]:
            out["status_short"] = _status_short(out["status"])
        out["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    except Exception:
        pass
    return out


def fetch_codex_reset(url: str = CODEX_RESET_URL,
                      timeout: float = 10) -> dict:
    """Structured Tibo timeline (JSON). Never raises."""
    out = {"source": "codex-reset", "ok": False, "error": None,
           "posts": [], "fetched_at": None, "stale": None}
    try:
        data = json.loads(_http_get(url, "application/json", timeout, 800_000))
        out["fetched_at"] = _norm_iso(data.get("fetched_at"))
        out["stale"] = bool(data.get("stale"))
        kinds = {"boost": "feature", "candidate": "reset",
                 "signal": "reset", "limits": "limit"}
        for t in data.get("tweets") or []:
            at = _norm_iso(t.get("at") or t.get("declared_at"))
            text = _clean(t.get("text"))
            if not at or not text:
                continue
            out["posts"].append({
                "id": str(t.get("id") or ""),
                "at": at,
                "text": text[:600],
                "url": t.get("url") or "",
                "kind": kinds.get(t.get("kind") or "", "post"),
                "source": "codex-reset",
                "is_reply": bool(t.get("is_reply")),
            })
        out["ok"] = True
    except Exception as e:
        out["error"] = str(e)[:160]
    return out


def fetch_willreset(url: str = WILLRESET_URL,
                    timeout: float = 10) -> dict:
    """Full Tibo timeline (HTML). Never raises."""
    out = {"source": "willreset", "ok": False, "error": None,
           "posts": [], "fetched_at": None}
    try:
        page = _http_get(url, "text/html", timeout, 600_000)
        now = datetime.now(timezone.utc)
        for block in _WR_ITEM_RE.findall(page):
            mt = _WR_TEXT_RE.search(block)
            if not mt:
                continue
            text = _clean(_strip_tags(mt.group(1)))
            if not text:
                continue
            link = _WR_LINK_RE.search(block)
            tid = link.group(2) if link else ""
            at = _snowflake_iso(tid)
            if not at:
                tm = _WR_TIME_RE.search(block)
                at = _relative_iso(tm.group(1), now) if tm else None
            if not at:
                continue
            pill = _WR_PILL_RE.search(block)
            cls = (pill.group(1) if pill else "").lower()
            kind = {"reset": "reset", "codex": "feature",
                    "limits": "limit"}.get(cls, "post")
            is_reply = text.startswith("@")
            # the page mirrors replies too; keep ordinary posts and only
            # substantive/feature replies (codex-reset curates the rest)
            if is_reply and kind == "post" and len(text) < 80:
                continue
            out["posts"].append({
                "id": tid,
                "at": at,
                "text": text[:600],
                "url": link.group(1) if link else "",
                "kind": kind,
                "source": "willreset",
                "is_reply": is_reply,
            })
        out["ok"] = True
    except Exception as e:
        out["error"] = str(e)[:160]
    return out


_SOURCE_RANK = {"codex-reset": 0, "willreset": 1, "opentherank": 2}


def _text_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())[:180]


def _richer(a: dict, b: dict) -> dict:
    """Duplicate winner: longer text, then source priority."""
    la, lb = len(a.get("text") or ""), len(b.get("text") or "")
    if abs(la - lb) > 12:
        return a if la > lb else b
    ra = _SOURCE_RANK.get(a.get("source") or "", 9)
    rb = _SOURCE_RANK.get(b.get("source") or "", 9)
    if ra != rb:
        return a if ra < rb else b
    return a if la >= lb else b


def _merge_posts(*groups) -> list[dict]:
    """Merge source timelines, dedupe by X id/text, oldest -> newest."""
    by_id: dict[str, dict] = {}
    loose: list[dict] = []
    for posts in groups:
        for p in posts or []:
            at = _norm_iso(p.get("at"))
            text = _clean(p.get("text"))
            if not at or not text:
                continue
            rec = dict(p, at=at, text=text)
            pid = str(rec.get("id") or "")
            if pid:
                by_id[pid] = (rec if pid not in by_id
                              else _richer(by_id[pid], rec))
            else:
                loose.append(rec)
    by_text: dict[str, dict] = {}
    for rec in list(by_id.values()) + loose:
        key = _text_key(rec["text"])
        by_text[key] = (rec if key not in by_text
                        else _richer(by_text[key], rec))
    merged = sorted(by_text.values(), key=lambda p: p.get("at", ""))
    return merged[-80:]


def _future_result(future, source: str) -> dict:
    try:
        return future.result()
    except Exception as e:
        return {"source": source, "ok": False, "error": str(e)[:160],
                "posts": [], "fetched_at": None}


def fetch_news(opentherank_url: str = DEFAULT_URL,
               codex_reset_url: str = CODEX_RESET_URL,
               willreset_url: str = WILLRESET_URL,
               timeout: float = 10) -> dict:
    """Fetch and merge every source in parallel. Never raises."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=3,
                            thread_name_prefix="news") as pool:
        f_op = pool.submit(fetch_announcement, opentherank_url, timeout)
        f_cr = pool.submit(fetch_codex_reset, codex_reset_url, timeout)
        f_wr = pool.submit(fetch_willreset, willreset_url, timeout)
        op = _future_result(f_op, "opentherank")
        cr = _future_result(f_cr, "codex-reset")
        wr = _future_result(f_wr, "willreset")

    op = dict(op or {})
    op_ok = bool(op.get("fetched_at"))
    op_posts = op.get("posts") or []
    for key in ("latest_reset", "latest_reset_local", "status",
                "status_kind", "landing", "status_short",
                "forecast_epoch", "upcoming_local"):
        op.setdefault(key, None)

    op["posts"] = _merge_posts(cr.get("posts"), wr.get("posts"), op_posts)
    op["url"] = opentherank_url
    op["sources"] = {
        "opentherank": {"ok": op_ok, "posts": len(op_posts),
                        "error": None if op_ok else "unreachable"},
        "codex-reset": {"ok": bool(cr.get("ok")),
                        "posts": len(cr.get("posts") or []),
                        "error": cr.get("error")},
        "willreset": {"ok": bool(wr.get("ok")),
                      "posts": len(wr.get("posts") or []),
                      "error": wr.get("error")},
    }
    ok = any(s["ok"] for s in op["sources"].values())
    op["fetched_at"] = (time.strftime("%Y-%m-%dT%H:%M:%S") if ok else None)
    return op
