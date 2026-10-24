"""The watcher daemon: detect -> plan -> (wait) -> resume -> record."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from . import __version__
from .codexenv import CodexEnvError, CodexState, find_codex_bin
from .config import (ConfigError, load_config, resolve_codex_home,
                     resolve_state_dir, thread_config)
from .detect import detect
from .planner import plan_for_thread, pick_fallback_model, merge_settings
from .ratelimits import AppServerClient, QuotaSnapshot, RateLimitError
from .resume import (ResumeOutcome, build_exec_resume_command,
                     build_probe_command, build_queue_command, run_command)
from .state import InstanceLock, WatcherState, load_state, save_state

log = logging.getLogger("codex-autoresume")


def _now() -> datetime:
    return datetime.now().astimezone()


def _today(now: datetime | None = None) -> str:
    return (now or _now()).strftime("%Y-%m-%d")


class Daemon:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.codex_bin = find_codex_bin(cfg["general"]["codex_bin"])
        self.codex_home = resolve_codex_home(cfg)
        self.state_dir = resolve_state_dir(cfg)
        self.state_path = os.path.join(self.state_dir, "state.json")
        self.lock_path = os.path.join(self.state_dir, "daemon.lock")
        self.log_path = os.path.join(self.state_dir, "daemon.log")
        self.state: WatcherState | None = None
        self.apiserver: AppServerClient | None = None
        self._snapshot: QuotaSnapshot | None = None
        self._snapshot_at: float = 0.0
        self._resume_pool = ThreadPoolExecutor(
            max_workers=cfg["general"]["max_concurrent_resumes"],
            thread_name_prefix="resume")
        self._inflight: dict[str, float] = {}   # thread_id -> start ts
        self._inflight_lock = threading.Lock()

    # ------------------------------------------------------------ lifecycle

    def setup_logging(self, verbose: bool = False) -> None:
        os.makedirs(self.state_dir, exist_ok=True)
        fmt = "%(asctime)s %(levelname)s %(message)s"
        handlers: list[logging.Handler] = [
            logging.FileHandler(self.log_path, encoding="utf-8")]
        if verbose:
            handlers.append(logging.StreamHandler())
        logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                            format=fmt, handlers=handlers, force=True)

    def run(self, once: bool = False) -> int:
        self.setup_logging()
        lock = InstanceLock(self.lock_path)
        if not lock.acquire():
            log.error("another daemon instance holds %s", self.lock_path)
            return 3
        log.info("codex-autoresume %s starting (codex=%s home=%s)",
                 __version__, self.codex_bin, self.codex_home)
        try:
            self.state = load_state(self.state_path)
            while True:
                try:
                    self.tick()
                except Exception:
                    log.exception("tick failed")
                save_state(self.state)
                if once:
                    break
                time.sleep(self._sleep_seconds())
        except KeyboardInterrupt:
            log.info("interrupted; shutting down")
        finally:
            try:
                save_state(self.state)
            except Exception:
                log.exception("final state save failed")
            lock.release()
            self._resume_pool.shutdown(wait=False)
            if self.apiserver:
                self.apiserver.close()
        return 0

    def _sleep_seconds(self) -> float:
        interval = float(self.cfg["general"]["poll_interval_sec"])
        now = _now()
        for t in self.state.threads.values():
            if t.next_wake_iso:
                try:
                    wake = datetime.fromisoformat(t.next_wake_iso)
                except ValueError:
                    continue
                if now < wake:
                    interval = min(interval, max(5.0, (wake - now).total_seconds() + 2))
        return interval

    # ------------------------------------------------------------- one tick

    def tick(self) -> None:
        assert self.state is not None
        now = _now()
        if self.state.paused:
            log.debug("paused; skipping tick")
            return

        codex = CodexState(self.codex_home)
        events = detect(codex, self.state.seen_sources,
                        since_completed_at=self.state.turns_watermark, now=now)
        for e in events:
            self.state.seen_sources.add(e.source)
            if e.source.startswith("turn:") and e.thread_id:
                pass
        self._bump_watermark(codex)
        if events:
            log.info("detected %d new interruption event(s): %s", len(events),
                     [(e.thread_id[:8], e.kind) for e in events])

        goal_threads = {g["thread_id"] for g in codex.goals()
                        if g.get("status") in ("usage_limited", "budget_limited")}
        busy = codex.busy_threads()
        snapshot = self._quota_snapshot(force=bool(self._need_snapshot()))

        # fold new events into thread states
        for e in events:
            t = self.state.thread(e.thread_id)
            if e.kind == "goal_limited" or e.thread_id in goal_threads:
                t.is_goal = True
            t.last_kind = e.kind
            if e.reset_at is not None:
                t.last_reset_at = e.reset_at.isoformat()
            self._maybe_track(t, e)

        # re-derive wake times for tracked threads every tick (config may
        # change; snapshot reset times may arrive later)
        for thread_id, t in list(self.state.threads.items()):
            if not t.tracked or t.last_kind == "":
                continue
            if thread_id in busy:
                log.debug("%s busy (turn in progress); waiting", thread_id[:8])
                t.next_wake_iso = (now + datetime.timedelta(
                    seconds=self.cfg["general"]["poll_interval_sec"])).isoformat()
                continue
            event = self._synthetic_event(thread_id, t, snapshot)
            if event is None:
                continue
            try:
                info = codex.thread(thread_id)
            except CodexEnvError:
                info = None
            tc = thread_config(self.cfg, thread_id)
            decision = plan_for_thread(
                event, t, self.cfg, tc,
                thread_last_update_ms=(info.updated_at_ms if info else 0),
                busy=thread_id in busy, now=now, snapshot=snapshot,
                today=_today(now))
            t.next_wake_iso = (decision.wake_at.isoformat()
                               if decision.wake_at else None)
            if decision.action == "due":
                self._launch_resume(thread_id, t, decision, info)

        self._reap_finished_resumes()

    def _maybe_track(self, t, e) -> None:
        if t.manual:
            t.tracked = True
            return
        if e.kind == "goal_limited":
            t.tracked = True       # goals are always worth resuming
            log.info("tracking goal thread %s (goal parked)", e.thread_id[:8])
            return
        if self.cfg["general"]["auto_track"]:
            t.tracked = True
            log.info("tracking thread %s (%s)", e.thread_id[:8], e.kind)

    def _synthetic_event(self, thread_id: str, t, snapshot):
        from .detect import InterruptionEvent
        from .timeparse import classify_window
        reset = None
        if t.last_reset_at:
            try:
                reset = datetime.fromisoformat(t.last_reset_at)
            except ValueError:
                reset = None
        kind = t.last_kind or "usage_primary"
        if kind == "usage_primary" and reset is not None:
            window = classify_window(reset)
            if window == "weekly":
                kind = "usage_weekly"
        return InterruptionEvent(
            thread_id=thread_id, kind=kind,
            reset_at=reset, source="synthetic")

    def _bump_watermark(self, codex: CodexState) -> None:
        con_max = 0
        try:
            import sqlite3
            from .codexenv import _connect_ro
            con = _connect_ro(codex.history_db)
            try:
                row = con.execute(
                    "SELECT MAX(completed_at) FROM thread_turns "
                    "WHERE error_json IS NOT NULL AND error_json != ''").fetchone()
                con_max = int(row[0] or 0)
            finally:
                con.close()
        except Exception:
            pass
        if con_max > self.state.turns_watermark:
            self.state.turns_watermark = con_max

    # --------------------------------------------------------------- quota

    def _need_snapshot(self) -> bool:
        refresh = float(self.cfg["limits_probe"]["poll_sec"])
        return (time.monotonic() - self._snapshot_at) >= refresh

    def _quota_snapshot(self, force: bool = False) -> QuotaSnapshot | None:
        if not self.cfg["limits_probe"]["use_app_server"]:
            return None
        if not force and self._snapshot and \
                (time.monotonic() - self._snapshot_at) < self.cfg["limits_probe"]["poll_sec"]:
            return self._snapshot
        try:
            if self.apiserver is None:
                self.apiserver = AppServerClient(self.codex_bin)
            self._snapshot = self.apiserver.read_rate_limits()
            self._snapshot_at = time.monotonic()
            p, s = self._snapshot.primary, self._snapshot.secondary
            log.info("quota snapshot: ordinaryUsageAllowed=%s primary=%s%% "
                     "secondary=%s%% credits=%d", self._snapshot.ordinary_usage_allowed,
                     p.used_percent, s.used_percent, len(self._snapshot.reset_credits))
        except (RateLimitError, OSError) as e:
            log.warning("quota snapshot unavailable: %s", e)
            self.apiserver = None      # respawn next time
        return self._snapshot

    # -------------------------------------------------------------- resumes

    def _launch_resume(self, thread_id: str, t, decision, info) -> None:
        with self._inflight_lock:
            if thread_id in self._inflight:
                log.debug("%s resume already in flight", thread_id[:8])
                return
            self._inflight[thread_id] = time.monotonic()
        g = self.cfg["general"]
        if not self.state.bump_global_counter(_today(), g["max_resumes_per_day"]):
            log.warning("global daily resume cap reached; not resuming %s",
                        thread_id[:8])
            with self._inflight_lock:
                self._inflight.pop(thread_id, None)
            return
        if not t.bump_resume_counter(_today(), self._thread_cap(thread_id)):
            log.warning("per-thread daily cap reached for %s", thread_id[:8])
            with self._inflight_lock:
                self._inflight.pop(thread_id, None)
            return
        t.last_resume_iso = _now().isoformat()
        log.info("resuming %s (%s, model=%s effort=%s mode=%s)", thread_id[:8],
                 decision.reason, decision.model, decision.reasoning_effort,
                 decision.mode)
        self._resume_pool.submit(self._do_resume, thread_id, decision)

    def _thread_cap(self, thread_id: str) -> int:
        tc = thread_config(self.cfg, thread_id)
        settings = merge_settings(self.cfg, tc)
        return settings.per_thread_cap

    def _do_resume(self, thread_id: str, decision) -> None:
        try:
            r = self.cfg["resume"]
            prompt = decision.prompt or r["prompt_generic"]
            tried: list[str] = []
            model = decision.model
            for attempt in range(
                    max(1, int(self.cfg["general"]["server_overloaded_retries"]) + 1)):
                if decision.mode == "queue":
                    argv = build_queue_command(self.codex_bin, thread_id, prompt)
                else:
                    argv = build_exec_resume_command(
                        self.codex_bin, thread_id, prompt,
                        model=model or "",
                        reasoning_effort=decision.reasoning_effort or "",
                        extra_config=list(r["extra_config"]),
                        codex_extra_args=list(r["codex_extra_args"]))
                if r["probe_first"] and decision.mode != "queue":
                    probe = run_command(
                        build_probe_command(self.codex_bin, thread_id, model=model or ""),
                        timeout=300)
                    if probe.kind == "usage_limit":
                        outcome = probe
                        self._record(thread_id, outcome, model)
                        return
                outcome = run_command(
                    argv, timeout=float(self.cfg["general"]["resume_timeout_sec"]))
                self._record(thread_id, outcome, model)
                if outcome.kind != "overloaded":
                    return
                if model:
                    tried.append(model)
                nxt = pick_fallback_model(self.cfg, tried)
                if nxt is None:
                    return
                log.info("%s overloaded; retrying with fallback model %s",
                         thread_id[:8], nxt)
                model = nxt
                time.sleep(min(60, self.cfg["general"]["server_overloaded_cooldown_sec"]))
        except Exception:
            log.exception("resume worker crashed for %s", thread_id)
        finally:
            with self._inflight_lock:
                self._inflight.pop(thread_id, None)

    def _record(self, thread_id: str, outcome: ResumeOutcome, model: str | None) -> None:
        t = self.state.thread(thread_id)
        entry = {
            "at": _now().isoformat(), "kind": outcome.kind,
            "ok": outcome.ok, "exit_code": outcome.exit_code,
            "model": model, "duration_sec": round(outcome.duration_sec, 1),
            "reset_at": outcome.reset_at_iso,
            "error_tail": (outcome.stderr_tail or outcome.stdout_tail)[-400:],
        }
        t.append_history(entry)
        t.last_outcome = outcome.kind
        if outcome.ok:
            t.fail_streak = 0
            t.last_kind = ""            # interruption handled
            t.last_reset_at = None
            t.next_wake_iso = None
            log.info("resume %s -> success (%.0fs)", thread_id[:8],
                     outcome.duration_sec)
        else:
            t.fail_streak += 1
            t.last_error = outcome.kind
            if outcome.kind == "usage_limit" and outcome.reset_at_iso:
                from .timeparse import classify_window
                reset = datetime.fromisoformat(outcome.reset_at_iso)
                t.last_kind = ("usage_weekly"
                               if classify_window(reset) == "weekly"
                               else "usage_primary")
                t.last_reset_at = outcome.reset_at_iso
            elif outcome.kind == "overloaded":
                t.last_kind = "server_overloaded"
            log.warning("resume %s -> %s%s", thread_id[:8], outcome.kind,
                        (" (reset %s)" % outcome.reset_at_iso)
                        if outcome.reset_at_iso else "")
        self._notify(thread_id, outcome)
        save_state(self.state)

    def _notify(self, thread_id: str, outcome: ResumeOutcome) -> None:
        cmd = self.cfg["general"]["notify_command"]
        if not cmd:
            return
        sub = (cmd.replace("{event}", "resume_" + outcome.kind)
                  .replace("{thread_id}", thread_id)
                  .replace("{reason}", outcome.kind)
                  .replace("{detail}", (outcome.stderr_tail or "")[:200]))
        try:
            subprocess.run(sub, shell=True, timeout=30,
                           creationflags=0x08000000 if os.name == "nt" else 0)
        except Exception as e:
            log.warning("notify_command failed: %s", e)

    def _reap_finished_resumes(self) -> None:   # placeholder for future stats
        pass


def main_daemon(once: bool = False, config_path: str | None = None,
                verbose: bool = False) -> int:
    try:
        cfg = load_config(config_path)
    except ConfigError as e:
        print("config error: %s" % e, flush=True)
        return 2
    daemon = Daemon(cfg)
    return daemon.run(once=once)
