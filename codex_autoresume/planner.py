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
from .timeparse import WEEKLY_THRESHOLD

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
        # guard disabled across weeks: stop permanently -- even when the
        # weekly window recovers, this thread must not be continued
        return Decision("untrack", event.thread_id,
                        "weekly guard off; stopping permanently after "
                        "weekly-limit interruption "
                        "(general.weekly_resume=%s)" % g["weekly_resume"],
                        kind=event.kind)

    # --- goal guards fire only with real headroom in both windows ----------
    if (event.kind == "goal_limited" and snapshot is not None
            and not _window_headroom(snapshot)):
        wake = _blocked_until(snapshot) or (now + timedelta(minutes=15))
        reason = ("goal parked but 5h window exhausted"
                  if (snapshot.primary.used_percent or 0) >= 100
                  else "goal parked but weekly window exhausted")
        return Decision("wait", event.thread_id, reason,
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

    # --- interruption evidence beats a trailing snapshot -------------------
    # A usage-limit interruption is first-hand proof that the window was
    # empty at that moment. The cached snapshot may still read 99% (poll
    # cadence, backend lag); resuming on that number is exactly what
    # appended duplicate "usage limit" turns to the conversation. Wait for
    # the reset time carried by the interruption itself; trust the snapshot
    # again only once it describes a clearly NEWER window.
    jitter = random.uniform(0, g["jitter_sec"])
    if (event.kind in ("usage_primary", "usage_weekly", "goal_limited")
            and event.reset_at is not None and event.reset_at > now
            and not _snapshot_is_newer_window(event, snapshot, now)):
        return Decision("wait", event.thread_id,
                        "interruption reset %s not reached yet"
                        % event.reset_at.isoformat(timespec="seconds"),
                        wake_at=event.reset_at + timedelta(seconds=jitter),
                        kind=event.kind)

    # --- compute the earliest wake time ------------------------------------
    candidates: list[datetime] = []
    # both windows must have headroom; a refilled 5h block cannot carry a
    # weekly-exhausted account
    quota_ok_now = _window_headroom(snapshot) if snapshot else False
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
    # A window still marked empty with a reset time ahead of us is NOT
    # recoverable yet: attempting early appends another limit message to the
    # conversation. Only once the deadline has passed do we try (the reset
    # boundary is not a whole minute and flags can lag; a still-limited
    # resume fails instantly and reschedules itself).
    if snapshot is not None and now >= wake and not quota_ok_now:
        deadline = event.reset_at
        blocked = _blocked_until(snapshot)
        if blocked is not None:
            deadline = max(deadline, blocked) if deadline else blocked
        elif deadline is None:
            nxt = snapshot.next_reset_epoch()
            deadline = (datetime.fromtimestamp(nxt).astimezone()
                        if nxt else None)
        if deadline is None or deadline <= now:
            pass                     # reset reached: attempt below
        else:
            recheck = now + timedelta(seconds=cfg["limits_probe"]["poll_sec"])
            return Decision("wait", event.thread_id,
                            "quota gate: window empty "
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


def _window_headroom(snapshot: QuotaSnapshot | None) -> bool:
    """Quota available in BOTH windows.

    A non-empty 5h block is useless when the weekly quota is gone (the
    resume fails instantly and appends another limit message).
    """
    if snapshot is None:
        return False
    for w in (snapshot.primary, snapshot.secondary):
        if w.used_percent is not None and w.used_percent >= 100:
            return False
    return True


def _blocked_until(snapshot: QuotaSnapshot) -> datetime | None:
    """Latest reset among exhausted windows: every one of them must refill."""
    resets = []
    for w in (snapshot.primary, snapshot.secondary):
        if (w.used_percent is not None and w.used_percent >= 100
                and w.resets_at):
            resets.append(datetime.fromtimestamp(w.resets_at).astimezone())
    return max(resets) if resets else None


def _snapshot_is_newer_window(event: InterruptionEvent,
                              snapshot: QuotaSnapshot | None,
                              now: datetime) -> bool:
    """True when the snapshot's reset lies clearly after the event's reset.

    That means the snapshot describes a window which STARTED after the
    interruption, so its headroom is genuine and the interruption is stale.
    """
    if snapshot is None or event.reset_at is None:
        return False
    weekly = (event.kind == "usage_weekly" or
              (event.kind == "goal_limited" and
               event.reset_at - now > WEEKLY_THRESHOLD))
    w = snapshot.secondary if weekly else snapshot.primary
    if not w.resets_at:
        return False
    snap_reset = datetime.fromtimestamp(w.resets_at).astimezone()
    return snap_reset > event.reset_at + timedelta(minutes=5)
