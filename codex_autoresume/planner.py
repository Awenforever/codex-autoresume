"""Decision logic: interruption events + config + state + quota -> actions.

Pure functions (no I/O) so every rule is unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import random

from .detect import InterruptionEvent
from .ratelimits import QuotaSnapshot
from .state import ThreadState

MAX_ATTEMPTS_BEFORE_PARK = 6
# When the quota gate says "not recovered" but the reset deadline is closer
# than this, attempt anyway (snapshot flags lag reality; a still-limited
# resume fails instantly and reschedules itself). Farther out, wait quietly
# so threads do not accumulate junk failed turns.
GATE_ATTEMPT_WINDOW = timedelta(hours=1)


@dataclass
class Decision:
    action: str                  # track | due | wait | skip | untrack
    thread_id: str
    reason: str
    wake_at: datetime | None = None
    kind: str = ""               # interruption kind being handled
    model: str | None = None
    reasoning_effort: str | None = None
    prompt: str | None = None
    mode: str | None = None

@dataclass
class EffectiveSettings:
    """Merged resume settings for one thread (config precedence applied)."""
    model: str | None = None
    reasoning_effort: str | None = None
    prompt_goal: str = ""
    prompt_generic: str = ""
    mode: str = "exec"
    probe_first: bool = False
    weekly_enabled: bool = False
    per_thread_cap: int = 12


def merge_settings(cfg: dict, tcfg: dict) -> EffectiveSettings:
    g = cfg["general"]
    r = cfg["resume"]
    weekly_mode = g["weekly_resume"]
    weekly_for_thread = tcfg.get("weekly_resume")
    if weekly_mode == "all":
        weekly_enabled = True
    elif weekly_mode == "per-thread":
        weekly_enabled = bool(weekly_for_thread)
    else:
        weekly_enabled = False
    per_thread_cap = tcfg.get("max_resumes_per_day") or g["max_resumes_per_thread_per_day"]
    return EffectiveSettings(
        model=tcfg.get("model") or (r["model"] or None),
        reasoning_effort=tcfg.get("reasoning_effort") or (r["reasoning_effort"] or None),
        prompt_goal=tcfg.get("prompt") or r["prompt_goal"],
        prompt_generic=tcfg.get("prompt") or r["prompt_generic"],
        mode=tcfg.get("mode") or r["mode"],
        probe_first=bool(r["probe_first"]),
        weekly_enabled=weekly_enabled,
        per_thread_cap=int(per_thread_cap),
    )


def plan_for_thread(event: InterruptionEvent,
                    tstate: ThreadState,
                    cfg: dict,
                    tc: dict,
                    thread_last_update_ms: int,
                    busy: bool,
                    now: datetime,
                    snapshot: QuotaSnapshot | None,
                    today: str) -> Decision:
    """Decide what to do about one interruption event for one thread."""
    from .config import thread_config
    g = cfg["general"]

    if tc.get("enabled") is False:
        return Decision("skip", event.thread_id, "thread disabled in config")
    if not g["enabled"]:
        return Decision("skip", event.thread_id, "watcher disabled in config")

    settings = merge_settings(cfg, tc)

    # --- weekly window policy -------------------------------------------
    if event.kind == "usage_weekly" and not settings.weekly_enabled:
        # guard disabled across weeks: stop permanently -- even when the
        # weekly window recovers, this thread must not be continued
        return Decision("untrack", event.thread_id,
                        "weekly guard off; stopping permanently after "
                        "weekly-limit interruption "
                        "(general.weekly_resume=%s)" % g["weekly_resume"],
                        kind=event.kind)

    # --- goal guards fire only with real 5h headroom ----------------------
    if (event.kind == "goal_limited" and snapshot is not None
            and not snapshot.has_primary_headroom()):
        nxt = snapshot.next_reset_epoch()
        wake = (datetime.fromtimestamp(nxt).astimezone() if nxt
                else now + timedelta(minutes=15))
        return Decision("wait", event.thread_id,
                        "goal parked but 5h window exhausted",
                        wake_at=wake, kind=event.kind)

    # --- thread sanity ----------------------------------------------------
    age_hours = max(0.0, (now.timestamp() * 1000 - thread_last_update_ms) / 3600000)
    if event.kind == "server_overloaded":
        # capacity errors are short-lived: retry soon with the fallback chain
        wake = now + timedelta(seconds=g["server_overloaded_cooldown_sec"])
        return Decision("track", event.thread_id,
                        "server overloaded; retry after cooldown",
                        wake_at=wake, kind=event.kind)

    # --- tracking gate ----------------------------------------------------
    if not (tstate.tracked or tstate.manual):
        auto = g["auto_track"]
        if not auto:
            return Decision("skip", event.thread_id,
                            "not tracked and general.auto_track is off",
                            kind=event.kind)
        # goal status is current state (never stale); turn failures must be
        # recent or they belong to conversations the user already moved on
        # from
        if event.kind != "goal_limited" and event.occurred_at is not None:
            age_h = (now - event.occurred_at).total_seconds() / 3600
            if age_h > float(g["auto_track_max_age_hours"]):
                return Decision("skip", event.thread_id,
                                "interruption too old (%.1fh) for auto-track"
                                % age_h, kind=event.kind)
        if event.occurred_at is None and event.kind != "goal_limited":
            # unknown age (e.g. turn without completed_at): fall back to the
            # thread's last-update recency
            if age_hours > float(g["auto_track_max_age_hours"]):
                return Decision("skip", event.thread_id,
                                "thread stale (%.1fh old) vs auto_track_max_age_hours"
                                % age_hours, kind=event.kind)

    # --- budget / cooldown guards -----------------------------------------
    if tstate.resume_count_day != today:
        tstate.resume_count_day = today
        tstate.resume_count_today = 0
    if tstate.resume_count_today >= settings.per_thread_cap:
        return Decision("skip", event.thread_id,
                        "per-thread daily resume cap reached (%d)"
                        % settings.per_thread_cap, kind=event.kind)
    if tstate.fail_streak >= MAX_ATTEMPTS_BEFORE_PARK:
        return Decision("skip", event.thread_id,
                        "parked after %d consecutive failures" % tstate.fail_streak,
                        kind=event.kind)
    if tstate.last_resume_iso:
        last_resume = datetime.fromisoformat(tstate.last_resume_iso)
        since = (now - last_resume).total_seconds()
        if since < g["min_resume_interval_sec"]:
            wait = g["min_resume_interval_sec"] - since
            return Decision("wait", event.thread_id,
                            "resume cooldown (%.0fs left)" % wait,
                            wake_at=now + timedelta(seconds=wait),
                            kind=event.kind)

    # --- compute the earliest wake time ------------------------------------
    jitter = random.uniform(0, g["jitter_sec"])
    candidates: list[datetime] = []
    # primary quota headroom is the ground-truth "5h 额度不为零" signal;
    # ordinaryUsageAllowed can lag reality and only informs logging
    quota_ok_now = snapshot.has_primary_headroom() if snapshot else False
    if quota_ok_now:
        # usage is already available: nothing to wait for
        candidates.append(now)
    else:
        if event.reset_at is not None:
            candidates.append(event.reset_at + timedelta(seconds=jitter))
        if snapshot is not None:
            nxt = snapshot.next_reset_epoch()
            if nxt:
                candidates.append(
                    datetime.fromtimestamp(nxt).astimezone()
                    + timedelta(seconds=jitter))
    wake = min(candidates) if candidates else now

    # --- quota gate ---------------------------------------------------------
    # Primary-window headroom is the real signal. Without headroom we wait
    # quietly; within the attempt window near the reset deadline we still
    # try (a still-limited resume fails instantly and reschedules itself).
    if snapshot is not None and now >= wake and not quota_ok_now:
        deadline = event.reset_at
        if deadline is not None:
            snap_reset = snapshot.next_reset_epoch()
            if snap_reset:
                snap_dt = datetime.fromtimestamp(snap_reset).astimezone()
                deadline = max(deadline, snap_dt)
        else:
            nxt = snapshot.next_reset_epoch()
            deadline = (datetime.fromtimestamp(nxt).astimezone()
                        if nxt else None)
        if deadline is None or deadline - now <= GATE_ATTEMPT_WINDOW:
            pass                     # near/at deadline: attempt below
        else:
            recheck = now + timedelta(seconds=cfg["limits_probe"]["poll_sec"])
            return Decision("wait", event.thread_id,
                            "quota gate: 5h window exhausted "
                            "(reset %s away)" % deadline.isoformat(),
                            wake_at=recheck, kind=event.kind)

    # --- busy guard ----------------------------------------------------------
    if busy:
        return Decision("wait", event.thread_id,
                        "thread has a turn in progress",
                        wake_at=now + timedelta(seconds=g["poll_interval_sec"] * 2),
                        kind=event.kind)

    if now >= wake:
        return Decision("due", event.thread_id, "reset window passed; resuming",
                        kind=event.kind,
                        model=settings.model, reasoning_effort=settings.reasoning_effort,
                        prompt=(settings.prompt_goal if tstate.is_goal
                                else settings.prompt_generic),
                        mode=settings.mode)
    return Decision("wait", event.thread_id, "waiting for reset",
                    wake_at=wake, kind=event.kind)


def pick_fallback_model(cfg: dict, tried: list[str]) -> str | None:
    chain = list(cfg["general"]["fallback_models"])
    for m in chain:
        if m not in tried:
            return m
    return None
