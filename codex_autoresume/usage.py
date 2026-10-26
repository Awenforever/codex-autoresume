"""Usage-rate estimation from quota snapshots (pure functions).

Percentages come back as integers, so short baselines produce phantom rates
(a 1-point jump over two minutes reads as 30%/h). Rates are only quoted
after a minimum sample span, and the weekly-window estimate is capped by
the structural maximum: weekly percentage can never rise faster than
``(100% / 5h) * ratio`` where ``ratio = weekly% rise per primary% rise`` —
the 5-hour window is a hard gate on how fast the weekly budget drains.

The ratio is measured only inside one 5h block (``delta weekly`` tracks
``delta primary`` there) and pooled over completed blocks. A block that
ended exhausted still contributes a valid pair; a block's flat exhausted
tail does not dilute it because the pair is baseline-to-last, not a rate.
"""

from __future__ import annotations

import math
import time

# resets_at is not stable to the second: the backend may correct it by a
# couple of minutes within the same block. Window identity therefore uses
# a tolerance instead of exact epoch equality.
WINDOW_EPOCH_TOLERANCE = 300


def add_sample(samples: list[tuple], primary: int | None,
               secondary: int | None,
               primary_reset: int | None = None,
               secondary_reset: int | None = None,
               keep_sec: float = 6 * 3600) -> list[tuple]:
    """Append (timestamp, primary%, secondary%, reset epochs) and trim.

    History resets when a window rolls over, so no rate or ratio is ever
    computed across a refill. Two independent signals trigger this: a large
    downward jump in either percentage, and a change of either window's
    ``resets_at`` epoch. The epoch check also catches light blocks whose
    used-% drop is below the jump threshold — the boundary must not rely on
    magnitudes. Samples from before a 5h reset are useless for the weekly
    ratio (part of that block was spent exhausted, so weekly stayed flat).
    """
    now = time.time()
    if samples:
        last = samples[-1]
        rolled = False
        for prev, cur in ((last[1], primary), (last[2], secondary)):
            if prev is not None and cur is not None and prev - cur > 10:
                rolled = True
                break
        if not rolled:
            for idx, cur in ((3, primary_reset), (4, secondary_reset)):
                prev = last[idx] if len(last) > idx else None
                # tolerance: do not wipe history on an epoch correction that
                # still identifies the same window
                if (prev is not None and cur is not None
                        and abs(prev - cur) > WINDOW_EPOCH_TOLERANCE):
                    rolled = True
                    break
        if rolled:
            samples = []
    samples.append((now, primary, secondary, primary_reset,
                    secondary_reset))
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


def simulate_weekly_eta(now: float, primary_used, secondary_used, rate_pct_h,
                        ratio, block_end, weekly_end=None,
                        win_sec: float = None
                        ) -> tuple[float | None, bool, str]:
    """Rate-aware weekly-exhaustion epoch: (eta, capped, reason).

    The 5h window gates the weekly drain: within the current block only
    ``x0 = min(100-P, r*(block_end-now))`` primary points can still burn,
    costing ``ratio*x0`` weekly. If that covers the remainder, the weekly
    quota runs out at ``now + W/(ratio*r)``. Otherwise the remainder is
    covered by whole blocks, each refilling 100 primary and burning at most
    ``Cb = min(100, r*L)`` of it (a low rate leaves a block unused), until
    a fractional last block.

    ``capped`` means the weekly window resets before the projected
    exhaustion; there is no exhaustion ETA to show. No rate or ratio means
    no ETA — nothing is fabricated from the structural maximum alone.
    """
    if (primary_used is None or secondary_used is None or ratio is None
            or not ratio or ratio <= 0 or rate_pct_h is None
            or rate_pct_h <= 0 or block_end is None):
        return None, False, "insufficient"
    if secondary_used >= 100:
        return None, False, "exhausted"
    L_h = (win_sec or 5 * 3600) / 3600.0
    W = 100.0 - secondary_used
    x0 = min(max(100.0 - primary_used, 0.0),
             max(rate_pct_h * (block_end - now) / 3600.0, 0.0))
    if W <= ratio * x0:
        eta = now + W / (ratio * rate_pct_h) * 3600.0
    else:
        w1 = W - ratio * x0
        cb = min(100.0, rate_pct_h * L_h)
        q = w1 / (ratio * cb)
        j = max(1, math.ceil(q - 1e-9))
        f = q - (j - 1)
        eta = (block_end + (j - 1) * L_h * 3600.0
               + f * (cb / rate_pct_h) * 3600.0)
    if weekly_end is not None and weekly_end > now and eta >= weekly_end:
        return None, True, "capped"
    return eta, False, "ok"


def pooled_ratio(windows: list[dict], current: dict | None = None,
                 min_completed_dp: int = 20, min_current_dp: int = 5,
                 current_weight_dp: int = 10) -> float | None:
    """Weekly-% per primary-% pooled over completed 5h windows.

    Each completed window contributes its ``dp`` as the weight, so long
    blocks dominate short ones and integer rounding of ``ds`` averages out
    across blocks. The open window is quantized and may be truncated
    mid-block, so it only nudges the pool with at most *current_weight_dp*
    of weight, and only once a completed window exists. With no completed
    window yet (fresh install), the open window stands alone once it has
    the same minimum span a completed window needs — below that, one
    integer step in ``ds`` would swing ``t`` by tens of percent.
    """
    completed = [w for w in windows
                 if w.get("dp", 0) >= min_completed_dp and w.get("ds", 0) > 0]
    dp = sum(w["dp"] for w in completed)
    ds = sum(w["ds"] for w in completed)
    cur = current or {}
    cdp, cds = cur.get("dp", 0), cur.get("ds", 0)
    if completed:
        if cdp >= min_current_dp and cds > 0:
            w = min(cdp, current_weight_dp)
            dp += w
            ds += w * cds / cdp
        return ds / dp if dp > 0 else None
    if cdp >= min_completed_dp and cds > 0:
        return cds / cdp
    return None


def window_ratio(samples: list[tuple],
                 primary_reset: int | None = None) -> float | None:
    """Weekly-% per primary-%, measured within one 5h window.

    The weekly quota accumulates across 5h blocks, so a ratio computed
    across a block boundary is meaningless (primary drops to 0, weekly
    does not). Two guards apply:

    * every sample must belong to the window identified by
      *primary_reset* (the snapshot's ``resets_at``, matched within
      ``WINDOW_EPOCH_TOLERANCE``) when it is known; otherwise the delta
      pair spans a refill and pairs one block's weekly growth with another
      block's primary consumption;
    * the segment is cut at the newest rollover of either column, and a
      primary rise of >= 3 points inside it is required.

    Used for per-window inspection; the panel pools several windows via
    :func:`pooled_ratio` so one integer-rounded block cannot swing ``t``.
    """
    if len(samples) < 2:
        return None
    if primary_reset is not None:
        samples = [s for s in samples
                   if len(s) > 3 and s[3] is not None
                   and abs(s[3] - primary_reset) <= WINDOW_EPOCH_TOLERANCE]
        if len(samples) < 2:
            return None
    # segment at the last rollover of EITHER column: the 5h block refill
    # drops primary, the weekly reset drops secondary — both poison the
    # delta pair, so the measurement window starts after the newest one
    start = 0
    for i in range(1, len(samples)):
        p_prev, p_cur = samples[i - 1][1] or 0, samples[i][1] or 0
        s_prev, s_cur = samples[i - 1][2] or 0, samples[i][2] or 0
        if p_prev - p_cur >= 10 or s_prev - s_cur >= 10:
            start = i
    first, last = samples[start], samples[-1]
    dp = (last[1] or 0) - (first[1] or 0)
    ds = (last[2] or 0) - (first[2] or 0)
    if dp >= 3 and ds > 0:
        return ds / dp
    return None


def simulate_primary_eta(now: float, used, rate_pct_h: float,
                         reset_epoch=None, win_sec: float = None):
    """When the current 5h quota exhausts at the CURRENT speed.

    Pure projection: (100 - P) / rate. The 5h reset is NOT part of this
    answer's semantics. None without a measured rate or when already at
    100%.
    """
    if used is None or not rate_pct_h:
        return None
    if used >= 100:
        return None
    return now + (100.0 - used) / rate_pct_h * 3600.0
