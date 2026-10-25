"""Usage-rate estimation from quota snapshots (pure functions).

Percentages come back as integers, so short baselines produce phantom rates
(a 1-point jump over two minutes reads as 30%/h). Rates are only quoted
after a minimum sample span, and the weekly-window estimate is capped by
the structural maximum: weekly percentage can never rise faster than
``(100% / 5h) * ratio`` where ``ratio = weekly% rise per primary% rise`` —
the 5-hour window is a hard gate on how fast the weekly budget drains.
"""

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
        for prev, cur in ((last[1], primary), (last[2], secondary)):
            if prev is not None and cur is not None and prev - cur > 10:
                samples = []
                break
    samples.append((now, primary, secondary))
    cutoff = now - keep_sec
    return [s for s in samples if s[0] >= cutoff]


def _baseline(samples: list[tuple], idx: int, window_sec: float,
              min_span_sec: float, now: float) -> tuple | None:
    """Earliest sample inside the window with usable data (None otherwise)."""
    newest = samples[-1]
    if newest[idx] is None:
        return None
    baseline = None
    for s in samples:
        if now - s[0] <= window_sec and s[idx] is not None:
            baseline = s
            break
    if baseline is None or baseline is newest:
        return None
    if newest[0] - baseline[0] < min_span_sec:
        return None                      # too little span -> phantom rate
    return baseline


def window_rate(samples: list[tuple], idx: int, window_sec: float,
                now: float | None = None,
                min_span_sec: float = 600) -> float | None:
    """Percent-per-hour consumption rate over the recent *window_sec*.

    *idx* selects the column (1 = primary, 2 = secondary). None when the
    rate is not measurable (insufficient span, missing data, no growth).
    """
    if not samples:
        return None
    now = now or time.time()
    baseline = _baseline(samples, idx, window_sec, min_span_sec, now)
    if baseline is None:
        return None
    newest = samples[-1]
    dv = (newest[idx] or 0) - (baseline[idx] or 0)
    dt_h = (newest[0] - baseline[0]) / 3600.0
    if dt_h <= 0 or dv <= 0:
        return None
    return dv / dt_h


def estimate_ratio(samples: list[tuple]) -> float | None:
    """weekly% rise per primary% rise over the full sample history.

    Guards against integer granularity by requiring a primary delta of at
    least 3 points. None when not estimable yet.
    """
    if len(samples) < 2:
        return None
    first, last = samples[0], samples[-1]
    dp = (last[1] or 0) - (first[1] or 0)
    ds = (last[2] or 0) - (first[2] or 0)
    if dp >= 3 and ds > 0:
        return ds / dp
    return None


def eta_epoch(samples: list[tuple], idx: int, window_sec: float,
              now: float | None = None, min_span_sec: float = 600,
              cap_pct_per_h: float | None = None
              ) -> tuple[int | None, float | None, bool]:
    """(eta_unix_seconds, effective_rate_pct_per_hour, rate_was_capped).

    ``cap_pct_per_h`` applies the structural limit (weekly window gated by
    the 5h window): the measured rate is clamped to the cap and flagged.
    """
    now = now or time.time()
    if not samples or samples[-1][idx] is None:
        return None, None, False
    used = samples[-1][idx]
    if used >= 100:
        return None, None, False
    rate = window_rate(samples, idx, window_sec, now=now,
                       min_span_sec=min_span_sec)
    if rate is None:
        return None, None, False
    capped = False
    if cap_pct_per_h is not None and rate > cap_pct_per_h:
        rate = cap_pct_per_h
        capped = True
    hours = (100 - used) / rate
    return int(now + hours * 3600), rate, capped


def simulate_weekly_eta(now: float, primary_used, secondary_used,
                        rate_p_pct_h: float, ratio: float,
                        primary_reset_epoch, primary_window_sec,
                        weekly_reset_epoch=None, max_windows: int = 200):
    """Weekly-exhaustion epoch by stepping through 5h windows.

    Model: the 5h window is a hard gate. Within the current (partial)
    window the primary can still drain at most (100 - P)% before its
    reset; every reset opens a fresh window worth at most min(rate*5h,
    100)% of primary. Weekly percent rises ``ratio`` per primary percent.

    Always returns a concrete epoch: the moment the current trajectory
    exhausts the weekly quota; if that pace will not exhaust it within
    this weekly window, the window's own end (the weekly refresh) is the
    answer. The caller never has to show "--".
    """
    if (primary_used is None or secondary_used is None
            or not rate_p_pct_h or not ratio):
        return None
    if not primary_reset_epoch:
        primary_reset_epoch = now + max(primary_window_sec or 5 * 3600, 900)
    win_h = max(primary_window_sec or 5 * 3600, 900) / 3600.0
    wr = float(weekly_reset_epoch) if weekly_reset_epoch else None
    t = float(now)
    # primary-% still to burn before weekly hits 100
    need = (100.0 - secondary_used) / ratio
    # first partial window: capped by remaining headroom AND reset time
    span_h = max(primary_reset_epoch - t, 0.0) / 3600.0
    avail = min(rate_p_pct_h * span_h, 100.0 - primary_used)
    if avail > 0:
        if need <= avail:
            eta = t + need / rate_p_pct_h * 3600.0
            return wr if (wr and eta > wr) else eta
        need -= avail
    t = max(primary_reset_epoch, t)
    for _ in range(max_windows):
        avail = min(rate_p_pct_h * win_h, 100.0)
        if need <= avail:
            eta = t + need / rate_p_pct_h * 3600.0
            return wr if (wr and eta > wr) else eta
        need -= avail
        t += win_h * 3600.0
        if wr and t >= wr:
            return wr
    return t


def window_ratio(samples: list[tuple]) -> float | None:
    """Weekly-% per primary-%, measured within the CURRENT 5h window.

    The weekly quota accumulates across 5h blocks, so a ratio computed
    across a block boundary is meaningless (primary drops to 0, weekly
    does not). Segment the samples at the last primary rollover (a drop
    of >= 10 points) and require a primary rise of >= 3 points inside the
    segment. Empirically ~0.18 on a Plus plan (weekly cap ~5x the 5h cap).
    """
    if len(samples) < 2:
        return None
    start = 0
    for i in range(1, len(samples)):
        p_prev = samples[i - 1][1] or 0
        p_cur = samples[i][1] or 0
        if p_prev - p_cur >= 10:
            start = i
    first, last = samples[start], samples[-1]
    dp = (last[1] or 0) - (first[1] or 0)
    ds = (last[2] or 0) - (first[2] or 0)
    if dp >= 3 and ds > 0:
        return ds / dp
    return None


def simulate_primary_eta(now: float, used, rate_pct_h: float,
                         reset_epoch, win_sec: float):
    """When the CURRENT 5h window hits 100%, per the block model.

    A block that cannot reach 100 before its end refills instead — the
    next exhaustion is then a full block of max burn after the reset.
    """
    if used is None or not rate_pct_h:
        return None
    if used >= 100:
        return None
    span = max(reset_epoch - now, 0.0)
    if (100.0 - used) <= rate_pct_h * span / 3600.0:
        return now + (100.0 - used) / rate_pct_h * 3600.0
    if not reset_epoch:
        return None
    return reset_epoch + (100.0 / rate_pct_h) * 3600.0
