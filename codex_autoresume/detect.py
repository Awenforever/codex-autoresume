"""Event detection: raw DB rows -> classified interruption events."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .codexenv import CodexState, TurnFailure
from .timeparse import classify_window, parse_reset_time

# codexErrorInfo values that mean "quota exhausted"
USAGE_LIMIT_CODES = {"usageLimitExceeded", "usage_limit_exceeded",
                     "UsageLimitExceeded"}
# retryable with a different model
OVERLOADED_CODES = {"serverOverloaded", "server_overloaded", "ServerOverloaded"}
# goal statuses that park a goal thread
# "blocked" is included: goals blocked by e.g. "selected model at capacity"
# need the same auto-continue as quota-parked ones (the app shows Resume and
# waits; nothing else recovers them unattended)
LIMITED_GOAL_STATUSES = {"usage_limited", "budget_limited", "blocked"}


@dataclass
class InterruptionEvent:
    thread_id: str
    kind: str                 # usage_primary | usage_weekly | server_overloaded | goal_limited
    reset_at: datetime | None  # timezone-aware, None = unknown
    source: str               # turn:<turn_id> | goal
    detail: str = ""
    occurred_at: datetime | None = None   # when the failure was recorded
    observed_at: datetime = field(default_factory=lambda: datetime.now().astimezone())

    @property
    def is_usage_limit(self) -> bool:
        return self.kind in ("usage_primary", "usage_weekly", "goal_limited")


def classify_failures(failures: list[TurnFailure],
                      now: datetime | None = None) -> list[InterruptionEvent]:
    """Map failed turns to interruption events (one per failed turn)."""
    from datetime import datetime as _dt
    events: list[InterruptionEvent] = []
    for f in failures:
        occurred = (_dt.fromtimestamp(f.completed_at).astimezone()
                    if f.completed_at else None)
        if f.codex_error_info in USAGE_LIMIT_CODES:
            base = occurred
            reset = parse_reset_time(f.message, now=now, base=base)
            window = classify_window(reset, now=now) if reset else "unknown"
            events.append(InterruptionEvent(
                thread_id=f.thread_id,
                kind="usage_weekly" if window == "weekly" else "usage_primary",
                reset_at=reset,
                source="turn:%s" % f.turn_id,
                detail=f.message[:300],
                occurred_at=occurred,
            ))
        elif f.codex_error_info in OVERLOADED_CODES:
            events.append(InterruptionEvent(
                thread_id=f.thread_id, kind="server_overloaded",
                reset_at=None, source="turn:%s" % f.turn_id,
                detail=f.message[:300], occurred_at=occurred))
    return events


def goal_events(goals: list[dict],
                now: datetime | None = None) -> list[InterruptionEvent]:
    """Threads whose goal is parked by a usage/budget limit.

    Goal status is current state (not history), so these carry no
    occurred_at and are never treated as stale.
    """
    from datetime import datetime as _dt
    out = []
    for g in goals:
        if g.get("status") in LIMITED_GOAL_STATUSES:
            occurred = None
            if g.get("updated_at_ms"):
                try:
                    occurred = _dt.fromtimestamp(
                        g["updated_at_ms"] / 1000).astimezone()
                except (ValueError, OSError, OverflowError):
                    occurred = None
            out.append(InterruptionEvent(
                thread_id=g["thread_id"],
                kind="goal_limited",
                reset_at=None,       # goal rows carry no reset time; turns do
                source="goal",
                detail="goal status=%s objective=%.120s" % (
                    g.get("status"), str(g.get("objective") or "")),
                occurred_at=occurred,
            ))
    return out


def dedupe(events: list[InterruptionEvent],
           seen_sources: set[str]) -> list[InterruptionEvent]:
    return [e for e in events if e.source not in seen_sources]


def detect(state: CodexState, seen_sources: set[str],
           since_completed_at: int = 0,
           now: datetime | None = None) -> list[InterruptionEvent]:
    """Scan both sources and return new (unseen) interruption events."""
    failures = state.failed_turns(since_completed_at=since_completed_at)
    events = classify_failures(failures, now=now) + goal_events(state.goals())
    return dedupe(events, seen_sources)


def seed_seen(state: CodexState, seen_sources: set[str],
              watermark: int) -> tuple[set[str], int]:
    """Cold start: treat every failure recorded before installation as seen.

    Without this, a fresh watcher would ingest months of historical
    usage-limit failures and try to resume everything at once.
    """
    failures = state.failed_turns(since_completed_at=0, limit=100000)
    for f in failures:
        seen_sources.add("turn:%s" % f.turn_id)
        if f.completed_at and f.completed_at > watermark:
            watermark = f.completed_at
    return seen_sources, watermark
