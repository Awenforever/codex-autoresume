"""Persistent watcher state (atomic JSON) and single-instance lock."""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any

STATE_VERSION = 1
LOCK_STALE_SEC = 600


class StateError(Exception):
    pass


@dataclass
class ThreadState:
    tracked: bool = False
    manual: bool = False                # armed explicitly via `watch`/UI
    is_goal: bool = False
    # per-thread overrides set from the control UI (state-level; merged over
    # config threads table)
    guard_weekly: bool | None = None
    guard_model: str | None = None
    guard_effort: str | None = None
    # custom continue prompt for non-goal threads (None = config default)
    guard_prompt: str | None = None
    # allow auto-consuming an official reset credit when the weekly window
    # is exhausted (financial-ish action: strictly opt-in per thread)
    guard_reset_card: bool | None = None
    last_kind: str = ""                 # last interruption kind
    last_reset_at: str | None = None    # ISO
    next_wake_iso: str | None = None
    last_resume_iso: str | None = None
    resume_count_today: int = 0
    resume_count_day: str = ""          # YYYY-MM-DD bucket
    total_resumes: int = 0             # attempts (budget cap counter)
    success_resumes: int = 0           # resumes that actually continued work
    fail_streak: int = 0
    last_outcome: str = ""
    last_error: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)

    def append_history(self, entry: dict[str, Any], keep: int = 50) -> None:
        self.history.append(entry)
        if len(self.history) > keep:
            self.history = self.history[-keep:]

    def bump_resume_counter(self, day: str, cap: int) -> bool:
        if self.resume_count_day != day:
            self.resume_count_day = day
            self.resume_count_today = 0
        if self.resume_count_today >= cap:
            return False
        self.resume_count_today += 1
        self.total_resumes += 1
        return True


@dataclass
class WatcherState:
    threads: dict[str, ThreadState] = field(default_factory=dict)
    seen_sources: set[str] = field(default_factory=set)
    turns_watermark: int = 0            # max completed_at ingested
    global_resume_count_today: int = 0
    global_resume_day: str = ""
    paused: bool = False
    lock_path: str = ""
    state_path: str = ""
    # official reset credits observed arriving (landing times)
    credit_events: list[dict[str, Any]] = field(default_factory=list)
    # quota windows actually observed resetting ({kind, at}; from snapshots)
    reset_events: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------ io

    def thread(self, thread_id: str) -> ThreadState:
        if thread_id not in self.threads:
            self.threads[thread_id] = ThreadState()
        return self.threads[thread_id]

    def bump_global_counter(self, day: str, cap: int) -> bool:
        if self.global_resume_day != day:
            self.global_resume_day = day
            self.global_resume_count_today = 0
        if self.global_resume_count_today >= cap:
            return False
        self.global_resume_count_today += 1
        return True

    def prune_seen_sources(self, keep: int = 5000) -> None:
        if len(self.seen_sources) > keep:
            self.seen_sources = set(sorted(self.seen_sources)[-keep:])


def load_state(state_path: str) -> WatcherState:
    st = WatcherState(state_path=state_path)
    if os.path.exists(state_path):
        try:
            with open(state_path, encoding="utf-8") as f:
                raw = json.load(f)
            if raw.get("version", STATE_VERSION) != STATE_VERSION:
                raise StateError("state version mismatch: %r" % raw.get("version"))
            for tid, t in raw.get("threads", {}).items():
                ts = ThreadState(**{k: v for k, v in t.items()
                                    if k in ThreadState.__dataclass_fields__})
                if not ts.success_resumes:
                    # migrate older state (no counter yet): derive successes
                    # from the recorded history
                    ts.success_resumes = sum(
                        1 for h in ts.history if h.get("ok") is True)
                st.threads[tid] = ts
            st.seen_sources = set(raw.get("seen_sources", []))
            st.turns_watermark = int(raw.get("turns_watermark", 0))
            st.global_resume_count_today = int(raw.get("global_resume_count_today", 0))
            st.global_resume_day = raw.get("global_resume_day", "")
            st.paused = bool(raw.get("paused", False))
            st.credit_events = list(raw.get("credit_events", []))[-20:]
            st.reset_events = list(raw.get("reset_events", []))[-20:]
        except (ValueError, TypeError) as e:
            raise StateError("corrupt state file %s: %s" % (state_path, e)) from e
    return st


def save_state(st: WatcherState) -> None:
    os.makedirs(os.path.dirname(st.state_path), exist_ok=True)
    payload = {
        "version": STATE_VERSION,
        "saved_at": time.time(),
        "threads": {tid: asdict(t) for tid, t in st.threads.items()},
        "seen_sources": sorted(st.seen_sources),
        "turns_watermark": st.turns_watermark,
        "global_resume_count_today": st.global_resume_count_today,
        "global_resume_day": st.global_resume_day,
        "paused": st.paused,
        "credit_events": st.credit_events,
        "reset_events": st.reset_events,
    }
    tmp = st.state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, st.state_path)


class InstanceLock:
    """Cross-process single-instance lock via an exclusive lock file."""

    def __init__(self, path: str):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> bool:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        if os.path.exists(self.path):
            try:
                age = time.time() - os.path.getmtime(self.path)
                with open(self.path, encoding="utf-8") as f:
                    pid = f.read().strip()
                if age > LOCK_STALE_SEC:
                    os.unlink(self.path)      # stale holder died
                elif pid and not _pid_alive(pid):
                    os.unlink(self.path)
            except OSError:
                pass
        try:
            self._fd = os.open(self.path,
                               os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        os.write(self._fd, str(os.getpid()).encode())
        return True

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
            try:
                os.unlink(self.path)
            except OSError:
                pass


def _pid_alive(pid_s: str) -> bool:
    try:
        pid = int(pid_s)
    except ValueError:
        return True   # unreadable -> assume alive; stale timer will reap
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            k32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            WAIT_TIMEOUT = 0x102
            h = k32.OpenProcess(SYNCHRONIZE, False, pid)
            if not h:
                return False
            try:
                return k32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
            finally:
                k32.CloseHandle(h)
        else:
            os.kill(pid, 0)
            return True
    except OSError:
        return False
