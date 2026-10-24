"""Parse reset times embedded in Codex usage-limit error messages.

Observed message shapes (machine-local wall time, e.g. Asia/Shanghai):

    "You've hit your usage limit. ... or try again at 1:51 PM."
    "You've hit your usage limit. ... or try again at Oct 27th, 2026 3:38 PM."

The first form is a 5-hour primary window reset (at most ~5h away); the date
form is the weekly/secondary window reset (up to ~7 days away). All parsing
is defensive: any failure returns None and the caller falls back to polling.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_TRY_AGAIN_RE = re.compile(r"try again (?:at|by)\s+([0-9]{1,2}:[0-9]{2}\s*(?:AM|PM))"
                           r"[.,]?", re.IGNORECASE)
_TRY_AGAIN_DATE_RE = re.compile(
    r"try again (?:at|by)\s+([A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+"
    r"\d{4},?\s+[0-9]{1,2}:[0-9]{2}\s*(?:AM|PM))[.,]?", re.IGNORECASE)
_ORDINAL_RE = re.compile(r"(\d{1,2})(st|nd|rd|th)", re.IGNORECASE)

# If the parsed reset is farther away than this, treat it as a weekly reset.
WEEKLY_THRESHOLD = timedelta(hours=12)


def _strip_ordinal(s: str) -> str:
    return _ORDINAL_RE.sub(r"\1", s)


def parse_reset_time(message: str, now: datetime | None = None,
                     base: datetime | None = None) -> datetime | None:
    """Extract the reset time from a usage-limit message.

    Returns a timezone-aware datetime in the machine's local timezone, or
    None when no recognizable time is present.

    *base* is the moment the failure was recorded (e.g. the failed turn's
    completion time). A 5-hour window reset always falls within
    (base, base + ~5.5h], which disambiguates "1:51 PM" between today and
    tomorrow. Without *base* the nearest future occurrence is returned; a
    clock time that already passed today resolves to *now* ("due
    immediately"), leaving the quota-snapshot gate to double-check.
    """
    if not message:
        return None
    now = now or datetime.now().astimezone()

    m = _TRY_AGAIN_DATE_RE.search(message)
    if m:
        raw = _strip_ordinal(m.group(1))
        for fmt in ("%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p",
                    "%b %d %Y %I:%M %p", "%B %d %Y %I:%M %p"):
            try:
                dt = datetime.strptime(raw, fmt)
                return dt.replace(tzinfo=now.tzinfo)
            except ValueError:
                continue

    m = _TRY_AGAIN_RE.search(message)
    if m:
        raw = m.group(1).replace("  ", " ").strip()
        for fmt in ("%I:%M %p", "%H:%M %p"):
            try:
                parsed = datetime.strptime(raw, fmt)
            except ValueError:
                continue
            anchor = base or now
            day_candidates = [anchor.date()]
            from datetime import timedelta as _td
            day_candidates.append(anchor.date() + _td(days=1))
            if (now.date() != anchor.date()
                    and now.date() not in day_candidates):
                day_candidates.append(now.date())
            candidates = sorted(
                now.replace(year=d.year, month=d.month, day=d.day,
                            hour=parsed.hour, minute=parsed.minute,
                            second=0, microsecond=0)
                for d in day_candidates)
            if base is not None:
                # the true reset lies within (base, base + 6h]
                for c in candidates:
                    if base < c <= base + _td(hours=6):
                        return c
                return None
            for c in candidates:
                if c > now:
                    return c
            return now          # already due; quota gate re-verifies
    return None


def classify_window(reset_at: datetime | None, now: datetime | None = None) -> str:
    """Classify a reset time as 'primary' (5h) or 'weekly'."""
    if reset_at is None:
        return "unknown"
    now = now or datetime.now().astimezone()
    if reset_at - now > WEEKLY_THRESHOLD:
        return "weekly"
    return "primary"
