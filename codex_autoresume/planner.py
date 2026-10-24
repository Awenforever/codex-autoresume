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
        return Decision("skip", event.thread_id,
                        "weekly reset not enabled (general.weekly_resume=%s)"
                        % g["weekly_resume"], kind=event.kind)

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
    grace = g["grace_after_reset_sec"]
    jitter = random.uniform(0, g["jitter_sec"])
    candidates: list[datetime] = []
    if event.reset_at is not None:
        candidates.append(event.reset_at + timedelta(seconds=grace + jitter))
    if snapshot is not None:
        nxt = snapshot.next_reset_epoch()
        if nxt:
            candidates.append(
                datetime.fromtimestamp(nxt).astimezone()
                + timedelta(seconds=grace + jitter))
    wake = min(candidates) if candidates else now

    # --- quota gate ---------------------------------------------------------
    if snapshot is not None and now >= wake:
        ok, why = snapshot.recovery_ok(
            cfg["limits_probe"]["recovery_used_percent_max"])
        if not ok:
            recheck = now + timedelta(seconds=cfg["limits_probe"]["poll_sec"])
            return Decision("wait", event.thread_id,
                            "quota gate: %s" % why, wake_at=recheck,
                            kind=event.kind)

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
