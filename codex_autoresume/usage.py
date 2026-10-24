"""Usage-rate estimation from quota snapshots (pure functions)."""

from __future__ import annotations

import time


def add_sample(samples: list[tuple[float, int | None, int | None]],
               primary: int | None, secondary: int | None,
               keep_sec: float = 6 * 3600) -> list[tuple]:
    """Append a (timestamp, primary%, secondary%) sample and trim old ones.

    A large downward jump means a window rolled over: history resets so the
    rate is not poisoned by the drop.
    """
    now = time.time()
    if samples:
        last = samples[-1]
        for prev in (last[1], last[2]):
            cur = primary if prev is last[1] else secondary
            if prev is not None and cur is not None and prev - cur > 10:
                samples = []
                break
    samples.append((now, primary, secondary))
    cutoff = now - keep_sec
    return [s for s in samples if s[0] >= cutoff]


def window_rate(samples: list[tuple], idx: int, window_sec: float,
                now: float | None = None) -> float | None:
    """Percent-per-hour consumption rate over the recent *window_sec*.

    *idx* selects the column (1 = primary, 2 = secondary). Uses the earliest
    sample inside the window vs. the newest one. Returns None when the rate
    is not measurable (single sample, missing data, or no growth).
    """
    if not samples:
        return None
    now = now or time.time()
    newest = samples[-1]
    baseline = None
    for s in samples:
        if now - s[0] <= window_sec and s[idx] is not None:
            baseline = s
            break
    if baseline is None or baseline is newest:
        return None
    dv = (newest[idx] or 0) - (baseline[idx] or 0)
    dt_h = (newest[0] - baseline[0]) / 3600.0
    if dt_h <= 0 or dv <= 0:
        return None
    return dv / dt_h


def eta_epoch(samples: list[tuple], idx: int, window_sec: float,
              now: float | None = None) -> tuple[int | None, float | None]:
    """(eta_unix_seconds, rate_pct_per_hour) until the window hits 100%."""
    now = now or time.time()
    if not samples or samples[-1][idx] is None:
        return None, None
    used = samples[-1][idx]
    if used >= 100:
        return None, None
    rate = window_rate(samples, idx, window_sec, now=now)
    if rate is None:
        return None, None
    hours = (100 - used) / rate
    return int(now + hours * 3600), rate
