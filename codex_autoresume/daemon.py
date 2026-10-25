"""The watcher daemon: detect -> plan -> (wait) -> resume -> record."""

from __future__ import annotations

import json
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


def _post_age_hours(iso: str) -> float:
    """Age of a tracker post (ISO UTC) in hours; unknown age -> large."""
    try:
        from datetime import datetime as _dt, timezone as _tz
        return (datetime.now(_tz.utc)
                - _dt.fromisoformat(iso.replace("Z", "+00:00"))
                ).total_seconds() / 3600.0
    except Exception:
        return 1e9


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
        self._snapshot_wall: str | None = None
        self._models: list[dict] | None = None
        self._models_at: float = 0.0
        self._usage_samples: list[tuple] = []
        self._seen_credit_ids: set[str] = set()
        self._notified_credits = False
        self._announcement: dict | None = None
        self._announcement_at: float = 0.0
        self._last_spawn_fallback: float = 0.0
        self._state_mtime: float = 0.0
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
            self._sync_external_edits(force=True)
            control = None
            if self.cfg["control_api"]["enabled"]:
                from .control import ControlApi
                control = ControlApi(self)
                try:
                    control.start(self.cfg["control_api"]["host"],
                                  int(self.cfg["control_api"]["port"]))
                    log.info("control api on http://%s:%d/ (panel ready)",
                             self.cfg["control_api"]["host"],
                             self.cfg["control_api"]["port"])
                except OSError as e:
                    log.warning("control api failed to start: %s", e)
                    control = None
            first_run = not self.state.seen_sources and not self.state.turns_watermark
            if first_run:
                from .detect import seed_seen
                codex = CodexState(self.codex_home)
                self.state.seen_sources, self.state.turns_watermark = seed_seen(
                    codex, self.state.seen_sources, self.state.turns_watermark)
                log.info("cold start: seeded %d pre-existing failures as seen "
                         "(watermark=%d)", len(self.state.seen_sources),
                         self.state.turns_watermark)
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
            if control:
                control.stop()
            lock.release()
            self._resume_pool.shutdown(wait=False)
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
        self._sync_external_edits()
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
                        if g.get("status") in ("usage_limited", "budget_limited",
                                               "blocked")}
        busy = codex.busy_threads()
        snapshot = self._quota_snapshot(force=bool(self._need_snapshot()))
        self._maybe_consume_reset_card(snapshot)

        # fold new events into thread states
        for e in events:
            t = self.state.thread(e.thread_id)
            if e.kind == "goal_limited" or e.thread_id in goal_threads:
                t.is_goal = True
            t.last_kind = e.kind
            if e.reset_at is not None:
                t.last_reset_at = e.reset_at.isoformat()
            self._maybe_track(t, e)

        # goal status is current state: a USER-GUARDED goal that is (again)
        # parked by a limit must be re-armed every tick so it keeps being
        # guarded across interruptions. Goals never guarded stay untouched
        # (guarding is opt-in per conversation).
        for gid in goal_threads:
            t = self.state.thread(gid)
            t.is_goal = True
            if t.manual and (not t.tracked or t.last_kind == ""):
                t.tracked = True
                t.last_kind = "goal_limited"
                # a stale success marker ("Continue clicked") is wrong while
                # the goal is parked waiting for the quota again
                if t.last_outcome in ("goal_continue", "uia_continue"):
                    t.last_outcome = "quota_wait"
                log.info("re-armed user-guarded goal thread %s (status "
                         "parked again)", gid[:8])

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
            if decision.action == "untrack":
                t.tracked = False
                log.info("untracked %s: %s", thread_id[:8], decision.reason)
                continue
            if decision.action == "due":
                # exec resume does NOT inherit the thread's model/effort by
                # itself (it falls back to config defaults); pass the
                # thread's recorded settings through for fidelity
                if decision.mode != "queue" and info is not None:
                    if not decision.model and info.model:
                        decision.model = info.model
                    if not decision.reasoning_effort and info.reasoning_effort:
                        decision.reasoning_effort = info.reasoning_effort
                self._launch_resume(thread_id, t, decision, info)

        # warm slow external caches off the HTTP path: /api/status serves
        # cache-only, so the panel can never hang on external fetches
        self.model_catalog()
        self._log_announcement(self.announcement())

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

    def cached_models(self) -> list[dict]:
        """Cache-only read for HTTP handlers (never spawns)."""
        return self._models or []

    def cached_announcement(self) -> dict | None:
        """Cache-only read for HTTP handlers (never fetches)."""
        return self._announcement

    def model_catalog(self) -> list[dict]:
        """Model list for the control UI.

        Primary source is the app-maintained models_cache.json (zero
        network, no app-server spawn); the app-server path is only a
        fallback when that file is missing. Refreshed ~10 min in the tick.
        """
        if (self._models is not None
                and time.monotonic() - self._models_at < 600):
            return self._models
        from .ratelimits import models_from_cache, list_models
        cached = models_from_cache(self.codex_home)
        if cached:
            self._models = cached
            self._models_at = time.monotonic()
        else:
            try:
                self._models = list_models(self.codex_bin)
                self._models_at = time.monotonic()
                log.info("model catalog via app-server: %d models",
                         len(self._models))
            except Exception as e:
                log.warning("model/list unavailable: %s", e)
                if self._models is None:
                    self._models = []
        return self._models or []

    def _watch_reset_credits(self) -> None:
        """Monitor officially granted reset credits and announce new ones.

        Landing time = local detection wall time (persisted to state as
        credit_events); the external tracker feed only provides 预告.
        """
        snap = self._snapshot
        if snap is None:
            return
        ids = {c.id for c in snap.reset_credits}
        if self._seen_credit_ids:
            fresh = ids - self._seen_credit_ids
            for c in snap.reset_credits:
                if c.id in fresh:
                    landed = _now().isoformat(timespec="seconds")
                    log.info("reset credit granted: %s (%s, expires %s) "
                             "landed %s", c.title or c.id, c.status,
                             c.expires_at or "never", landed)
                    self.state.credit_events.append({
                        "id": c.id, "title": c.title or c.id,
                        "status": c.status, "expires_at": c.expires_at,
                        "landed_at": landed})
                    self.state.credit_events = self.state.credit_events[-20:]
                    save_state(self.state)
                    self._notify("account", "reset_credit_granted",
                                 "%s landed=%s expires=%s" % (
                                     c.title or c.id, landed, c.expires_at))
        self._seen_credit_ids = ids

    def announcement(self) -> dict | None:
        """External reset-announcement feed (cached ~5 min, best-effort)."""
        if (self._announcement is not None
                and time.monotonic() - self._announcement_at < 300):
            return self._announcement
        from .announcements import fetch_announcement, DEFAULT_URL
        acfg = self.cfg.get("announcements", {}) or {}
        if acfg.get("enabled") is False:
            return None
        url = acfg.get("url", DEFAULT_URL)
        self._announcement = fetch_announcement(url)
        self._announcement_at = time.monotonic()
        if self._announcement.get("latest_reset"):
            log.info("reset tracker: latest=%s landing=%s",
                     self._announcement.get("latest_reset"),
                     self._announcement.get("landing"))
        return self._announcement

    def _maybe_consume_reset_card(self, snapshot) -> None:
        """Auto-redeem a reset credit when the weekly window is exhausted.

        Only fires when (a) some guarded thread opted in via the panel,
        (b) the secondary window is at 100%, and (c) a credit is actually
        available. One redemption frees the whole account; a 30-minute
        cooldown prevents runaway loops.
        """
        if snapshot is None or not snapshot.reset_credits:
            return
        if snapshot.secondary.used_percent is None \
                or snapshot.secondary.used_percent < 100:
            return
        qualifying = [
            (tid, t) for tid, t in self.state.threads.items()
            if t.tracked and t.guard_reset_card
            and t.last_kind == "usage_weekly"
        ]
        if not qualifying:
            return
        last = None
        for tid, t in self.state.threads.items():
            for h in t.history[-3:]:
                if h.get("kind") == "reset_card_consumed":
                    ts = h.get("at")
                    if ts and ts > (last or ""):
                        last = ts
        if last:
            try:
                age_min = (_now() - datetime.fromisoformat(last)).total_seconds() / 60
                if age_min < 30:
                    return
            except ValueError:
                pass
        import uuid
        from .ratelimits import consume_reset_credit, RateLimitError
        try:
            result = consume_reset_credit(self.codex_bin,
                                          str(uuid.uuid4()))
            log.info("reset credit consumed for weekly-exhausted guarded "
                     "threads (%s): %s", [tid[:8] for tid, _ in qualifying],
                     json.dumps(result, ensure_ascii=False)[:200])
            for tid, t in qualifying:
                t.append_history({"at": _now().isoformat(),
                                  "kind": "reset_card_consumed", "ok": True})
                t.next_wake_iso = None       # re-evaluate next tick
                self._notify(tid, "reset_card_consumed")
            save_state(self.state)
        except (RateLimitError, OSError) as e:
            log.warning("reset credit consumption failed: %s", e)

    def _quota_snapshot(self, force: bool = False) -> QuotaSnapshot | None:
        """Quota snapshot with NO app-server spawns in the normal path.

        Direct backend HTTP (wham/usage with the auth.json token) is the
        primary source: spawning ``codex app-server`` per poll made every
        child fight the desktop app's remote-control websocket (the app
        showed constant "reconnecting to network"). The app-server path is
        kept only as a rare fallback (at most once per 15 minutes).
        """
        if not self.cfg["limits_probe"]["use_app_server"]:
            return None
        if not force and self._snapshot and \
                (time.monotonic() - self._snapshot_at) < self.cfg["limits_probe"]["poll_sec"]:
            return self._snapshot
        from .ratelimits import fetch_quota_http, fetch_quota_snapshot
        try:
            self._snapshot = fetch_quota_http(self.codex_home)
            self._snapshot_at = time.monotonic()
            self._snapshot_wall = _now().isoformat(timespec="seconds")
            self._last_spawn_fallback = 0.0     # http healthy again
        except RateLimitError as e:
            now_mono = time.monotonic()
            last = getattr(self, "_last_spawn_fallback", 0.0)
            if now_mono - last >= 300:
                # rare fallback; guarded so it cannot fight the app's
                # websocket on a regular cadence
                self._last_spawn_fallback = now_mono
                try:
                    self._snapshot = fetch_quota_snapshot(self.codex_bin,
                                                          attempts=1)
                    self._snapshot_at = time.monotonic()
                    self._snapshot_wall = _now().isoformat(timespec="seconds")
                    log.warning("quota http failed (%s); app-server fallback "
                                "used (throttled to 1/5min)", e)
                except (RateLimitError, OSError) as e2:
                    log.warning("quota snapshot unavailable (http: %s; "
                                "fallback: %s)", e, e2)
            else:
                log.warning("quota snapshot unavailable (http: %s)", e)
        if self._snapshot is not None:
            from .usage import add_sample
            self._usage_samples = add_sample(
                self._usage_samples,
                self._snapshot.primary.used_percent,
                self._snapshot.secondary.used_percent)
            self._watch_reset_credits()
            self._record_reset_events()
            p, s = self._snapshot.primary, self._snapshot.secondary
            log.info("quota snapshot: allowed=%s primary=%s%% "
                     "secondary=%s%% credits=%d",
                     self._snapshot.ordinary_usage_allowed,
                     p.used_percent, s.used_percent,
                     len(self._snapshot.reset_credits))
        return self._snapshot

    def _log_announcement(self, ann: dict | None) -> None:
        """Keep news_log as a normalized mirror of the tracker's timeline.

        The PAGE is the source of truth: rebuild the log from its posts
        (last 48h, oldest to newest) on every sync. Entries carry each
        post's own publication time. When the page cannot be parsed, the
        stored log is left untouched — never append fetch-time entries.
        """
        try:
            from datetime import datetime as _dt, timedelta as _td, timezone
            posts = [p for p in ((ann or {}).get("posts") or [])
                     if _post_age_hours(p.get("at", "")) <= 48]
            if not posts:
                return
            posts.sort(key=lambda q: q.get("at", ""))
            normalized = [{"at": q["at"], "text": q["text"],
                           "kind": "post", "key": q["at"]}
                          for q in posts]
            st = self.state
            if st.news_log != normalized:
                st.news_log = normalized
                save_state(st)
                self._state_mtime = os.path.getmtime(st.state_path)
        except Exception:
            pass

    def _record_reset_events(self) -> None:
        """Record quota windows ACTUALLY resetting (used% big drop).

        The panel shows these as observed landing times, distinct from the
        tracker site's announcements (which are forecasts/global data).
        The previous percentages live in the PERSISTED state: a daemon
        restart must not blind the detector to a reset that happened
        across it (that is exactly how the weekly reset went unrecorded).
        """
        try:
            snap = self._snapshot
            if snap is None:
                return
            st = self.state
            prev_p, prev_s = st.prev_primary_pct, st.prev_secondary_pct
            cur_p, cur_s = snap.primary.used_percent, snap.secondary.used_percent
            events = []
            if prev_p is not None and cur_p is not None and prev_p - cur_p >= 10:
                events.append({"kind": "primary", "at":
                               _now().isoformat(timespec="seconds")})
            if prev_s is not None and cur_s is not None and prev_s - cur_s >= 10:
                events.append({"kind": "weekly", "at":
                               _now().isoformat(timespec="seconds")})
            if cur_p is not None:
                st.prev_primary_pct = cur_p
            if cur_s is not None:
                st.prev_secondary_pct = cur_s
            if events:
                st.reset_events = (st.reset_events or [])[-19:] + events
                save_state(st)
                self._state_mtime = os.path.getmtime(st.state_path)
                for ev in events:
                    log.info("reset observed: %s window rolled over at %s",
                             ev["kind"], ev["at"])
            elif cur_p is not None or cur_s is not None:
                # persist the moved baseline even without events
                save_state(st)
                self._state_mtime = os.path.getmtime(st.state_path)
        except Exception:
            pass

    def _effective_thread_config(self, thread_id: str, t) -> dict:
        """Config [threads] merged with state-level control-UI overrides."""
        tc = dict(thread_config(self.cfg, thread_id))
        if t is not None:
            if t.guard_weekly is not None:
                tc["weekly_resume"] = t.guard_weekly
            if t.guard_model:
                tc["model"] = t.guard_model
            if t.guard_effort:
                tc["reasoning_effort"] = t.guard_effort
            if t.guard_prompt:
                tc["prompt"] = t.guard_prompt
            if t.guard_reset_card is not None:
                tc["reset_card"] = t.guard_reset_card
        return tc

    def _sync_external_edits(self, force: bool = False) -> None:
        """Pick up pause/unwatch/watch edits made via the CLI while running."""
        try:
            mtime = os.path.getmtime(self.state_path)
        except OSError:
            return
        if not force and mtime <= self._state_mtime:
            return
        self._state_mtime = mtime
        try:
            fresh = load_state(self.state_path)
        except Exception:
            return
        st = self.state
        st.paused = fresh.paused
        for tid, ts in fresh.threads.items():
            cur = st.threads.get(tid)
            if cur is None:
                st.threads[tid] = ts
            else:
                cur.manual = ts.manual
                cur.guard_weekly = ts.guard_weekly
                cur.guard_model = ts.guard_model
                cur.guard_effort = ts.guard_effort
                cur.guard_prompt = ts.guard_prompt
                cur.guard_reset_card = ts.guard_reset_card
                if not ts.tracked and cur.tracked:
                    log.info("unwatched %s via CLI", tid[:8])
                cur.tracked = ts.tracked

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
            if self.cfg["general"]["dry_run"]:
                log.info("[dry-run] would resume %s (model=%s effort=%s "
                         "mode=%s) prompt=%.80s", thread_id[:8],
                         decision.model, decision.reasoning_effort,
                         decision.mode, prompt)
                t = self.state.thread(thread_id)
                t.append_history({"at": _now().isoformat(),
                                  "kind": "dry_run_skipped", "ok": None,
                                  "model": decision.model})
                t.last_outcome = "dry_run_skipped"
                save_state(self.state)
                return
            t = self.state.thread(thread_id)
            if t.is_goal:
                # goal threads are ONLY ever continued via the official
                # goal-continue action -- never via injected messages, even
                # if the RPC path fails (retry later instead)
                self._goal_resume(thread_id, t, decision)
                return
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
                if (outcome.kind == "writer_conflict"
                        and decision.mode != "queue"):
                    # the desktop app (or another session) holds the thread's
                    # writer: deliver through codex queue instead, which goes
                    # via the app's own daemon and preserves thread settings.
                    # Re-check busy first: never inject into a live turn.
                    from .codexenv import CodexState
                    if thread_id in CodexState(self.codex_home).busy_threads():
                        log.info("%s turn went live; skipping queue fallback",
                                 thread_id[:8])
                        self._record(thread_id, ResumeOutcome(
                            ok=False, kind="other_error", exit_code=None,
                            stderr_tail="skipped: turn became live"), model)
                        return
                    log.info("%s writer conflict; falling back to codex queue",
                             thread_id[:8])
                    argv = build_queue_command(self.codex_bin, thread_id, prompt)
                    outcome = run_command(
                        argv,
                        timeout=float(self.cfg["general"]["resume_timeout_sec"]))
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

    def _goal_resume(self, thread_id: str, t, decision) -> bool:
        """Resume a parked goal like the app's Continue button.

        Preconditions re-verified at send time: the goal must still be
        parked by a usage/budget limit (not running, not user-paused) and
        the thread must not have a live turn. Then: optional per-resume
        model/effort via thread/settings/update, then
        thread/goal/set {status: active, origin: user}. No message is ever
        injected. Always returns True: goal threads never fall back to the
        exec/queue message path (failures retry later instead).
        """
        from .codexenv import CodexState
        from .ratelimits import goal_continue, RateLimitError, GoalBusyError
        try:
            floor = float(self.cfg["general"]["server_overloaded_cooldown_sec"])
            last = t.history[-1] if t.history else None
            if isinstance(last, dict) and last.get("at"):
                try:
                    from datetime import datetime as _dt
                    age = (_now() - _dt.fromisoformat(last["at"])).total_seconds()
                    if age < floor and last.get("kind") in (
                            "goal_continue", "uia_continue",
                            "goal_continue_error"):
                        t.next_wake_iso = (_dt.fromisoformat(last["at"])
                                           + timedelta(seconds=floor)
                                           ).isoformat()
                        save_state(self.state)
                        return True
                except ValueError:
                    pass
            goals = {g["thread_id"]: g["status"]
                     for g in CodexState(self.codex_home).goals()}
            status = goals.get(thread_id)
            if status not in ("usage_limited", "budget_limited", "blocked"):
                log.info("goal %s state is %r (running/paused/done); "
                         "not touching it", thread_id[:8], status)
                t.last_kind = ""
                t.next_wake_iso = None
                save_state(self.state)
                return True
            if thread_id in CodexState(self.codex_home).busy_threads():
                log.info("goal %s has a live turn; deferring", thread_id[:8])
                t.next_wake_iso = (_now() + timedelta(
                    seconds=self.cfg["general"]["poll_interval_sec"])).isoformat()
                save_state(self.state)
                return True
            result = goal_continue(self.codex_bin, thread_id,
                                   model=decision.model,
                                   effort=decision.reasoning_effort)
            new_status = (result.get("goal") or {}).get("status")
            entry = {"at": _now().isoformat(), "kind": "goal_continue",
                     "ok": True, "model": decision.model,
                     "effort": decision.reasoning_effort,
                     "goal_status": new_status}
            t.append_history(entry)
            t.success_resumes += 1
            t.last_outcome = "goal_continue"
            t.fail_streak = 0
            t.last_kind = ""          # handed back to the goal engine
            t.next_wake_iso = None
            getattr(self, "_goal_busy_backoff", {}).pop(thread_id, None)
            log.info("goal %s continued via thread/goal/set (status=%s, "
                     "model=%s effort=%s)", thread_id[:8], new_status,
                     decision.model, decision.reasoning_effort)
            self._notify(thread_id, "goal_continue")
            save_state(self.state)
            return True
        except GoalBusyError:
            # the app owns the thread: the ONLY safe move left is clicking
            # the app's own Continue button (user-approved UI automation)
            if self._uia_goal_continue(thread_id, t):
                return True
            # stand down quietly. Back off exponentially so a held writer
            # does not keep spawning app-servers every poll.
            bo = getattr(self, "_goal_busy_backoff", None)
            if bo is None:
                bo = self._goal_busy_backoff = {}
            prev = bo.get(thread_id, 0)
            wait = 300 if prev < 300 else min(1800, prev * 2)
            bo[thread_id] = wait
            t.last_outcome = "writer_busy"
            t.next_wake_iso = (_now() + timedelta(seconds=wait)).isoformat()
            log.info("goal %s has an active writer (app owns it); "
                     "standing down, rechecking in %d min", thread_id[:8],
                     wait // 60)
            save_state(self.state)
            return True
        except (RateLimitError, OSError) as e:
            t.fail_streak += 1
            t.last_error = "goal_continue"
            t.append_history({"at": _now().isoformat(),
                              "kind": "goal_continue_error", "ok": False,
                              "error_tail": str(e)[:400]})
            t.next_wake_iso = (_now() + timedelta(minutes=10)).isoformat()
            log.warning("goal continue %s failed (%s); NO message fallback "
                        "for goal threads, retrying in 10 min",
                        thread_id[:8], e)
            save_state(self.state)
            return True   # handled: caller must not fall back to exec/queue

    def _uia_goal_continue(self, thread_id: str, t) -> bool:
        """Writer held by the app: click its own Continue button (UIA).

        Only fires when exactly ONE goal is usage/budget-limited, so the
        visible button cannot belong to a different goal. Success is
        VERIFIED by the goal status actually flipping; a click that changes
        nothing is treated as busy, not as a resume.
        """
        if not self.cfg["general"].get("uia_resume", True):
            return False
        try:
            from .codexenv import CodexState
            goals = CodexState(self.codex_home).goals()
            limited = [g["thread_id"] for g in goals
                       if g.get("status") in ("usage_limited", "budget_limited",
                                              "blocked")]
            if limited != [thread_id]:
                log.info("uia resume skipped: %d limited goals (ambiguous)",
                         len(limited))
                return False
            from .uia import click_continue
            res = click_continue()
            if not res.get("clicked"):
                log.info("uia Continue not clickable: %s", res.get("detail"))
                return False
            time.sleep(3)
            status = {g["thread_id"]: g.get("status")
                      for g in CodexState(self.codex_home).goals()}.get(thread_id)
            if status in ("usage_limited", "budget_limited", "blocked"):
                log.info("uia click did not flip goal %s (%s); busy",
                         thread_id[:8], status)
                return False
            t.append_history({"at": _now().isoformat(), "kind": "uia_continue",
                              "ok": True, "detail": str(res.get("detail"))[:200]})
            t.success_resumes += 1
            t.last_outcome = "uia_continue"
            t.fail_streak = 0
            t.last_kind = ""
            t.next_wake_iso = None
            getattr(self, "_goal_busy_backoff", {}).pop(thread_id, None)
            log.info("goal %s resumed via the app's own Continue button (%s)",
                     thread_id[:8], res.get("detail"))
            self._notify(thread_id, "uia_continue")
            save_state(self.state)
            return True
        except Exception:
            log.exception("uia continue failed")
            return False

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
            t.success_resumes += 1
            t.fail_streak = 0
            t.last_kind = ""            # interruption handled
            t.last_reset_at = None
            t.next_wake_iso = None
            log.info("resume %s -> success (%.0fs)", thread_id[:8],
                     outcome.duration_sec)
        else:
            t.last_error = outcome.kind
            if outcome.kind == "writer_conflict":
                # contention with the desktop app, not a real failure:
                # retry later without counting toward the park threshold
                t.next_wake_iso = (_now() + timedelta(minutes=10)).isoformat()
                log.warning("resume %s -> writer conflict (app holds thread); "
                            "recheck in 10 min", thread_id[:8])
                self._notify(thread_id, "writer_conflict")
                save_state(self.state)
                return
            t.fail_streak += 1
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
        self._notify(thread_id, outcome.kind,
                     outcome.stderr_tail or outcome.stdout_tail)
        save_state(self.state)

    def _notify(self, thread_id: str, kind: str, detail: str = "") -> None:
        cmd = self.cfg["general"]["notify_command"]
        if not cmd:
            return
        sub = (cmd.replace("{event}", "resume_" + kind)
                  .replace("{thread_id}", thread_id)
                  .replace("{reason}", kind)
                  .replace("{detail}", detail[:200]))
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
        msg = "config error: %s" % e
        print(msg, flush=True)
        # scheduled tasks swallow stdout: make fatal config errors visible
        # in the daemon log instead of dying silently
        try:
            from .config import resolve_state_dir, load_config as _lc
            fallback = _lc(config_path) if config_path and \
                os.path.exists(config_path) else None
            state_dir = resolve_state_dir(fallback)
        except Exception:
            state_dir = os.path.join(os.path.expanduser("~"),
                                     ".codex-autoresume")
        try:
            os.makedirs(state_dir, exist_ok=True)
            with open(os.path.join(state_dir, "daemon.log"), "a",
                      encoding="utf-8") as f:
                f.write("%s FATAL %s\n" % (_now().isoformat(), msg))
        except OSError:
            pass
        return 2
    daemon = Daemon(cfg)
    return daemon.run(once=once)
