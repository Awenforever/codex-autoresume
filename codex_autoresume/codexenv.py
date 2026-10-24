"""Environment discovery and read-only access to Codex local state.

Layout notes (Codex app >= 26.x / codex-cli >= 0.15x, sqlite-backed):

    ~/.codex/
      state_*.sqlite         threads table (id, title, model, reasoning_effort,
                             updated_at_ms, archived, cwd, ...)
      thread_history_*.sqlite  thread_turns (status, error_json, completed_at)
                             per turn; status in
                             {completed, failed, interrupted, inProgress}
      goals_*.sqlite         thread_goals (status in {active, paused, blocked,
                             usage_limited, budget_limited, complete})
      sessions/YYYY/MM/DD/rollout-*.jsonl   legacy rollout files

Error payloads seen in thread_turns.error_json:

    {"message": "You've hit your usage limit. ... try again at 1:51 PM.",
     "codexErrorInfo": "usageLimitExceeded", ...}
    {"message": "Selected model is at capacity. ...",
     "codexErrorInfo": "serverOverloaded", ...}

All database connections are opened read-only (URI mode=ro) so WAL writers
are never blocked.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass, field
from typing import Any

WIN_APP_GLOBS = [
    os.path.join("~", "AppData", "Local", "OpenAI", "Codex", "bin", "*", "codex.exe"),
]


class CodexEnvError(Exception):
    pass


def find_codex_bin(cfg_codex_bin: str = "") -> str:
    """Locate the codex executable."""
    if cfg_codex_bin:
        expanded = os.path.expanduser(cfg_codex_bin)
        if os.path.isfile(expanded):
            return os.path.abspath(expanded)
        on_path = shutil.which(cfg_codex_bin)
        if on_path:
            return on_path
        raise CodexEnvError("configured codex_bin not found: %r" % cfg_codex_bin)
    on_path = shutil.which("codex")
    if on_path:
        return on_path
    for pattern in WIN_APP_GLOBS:
        hits = glob.glob(os.path.expanduser(pattern))
        if hits:
            return os.path.abspath(max(hits, key=os.path.getmtime))
    raise CodexEnvError(
        "codex executable not found; set general.codex_bin in config.toml")


def codex_version(codex_bin: str) -> str:
    try:
        out = subprocess.run([codex_bin, "--version"], capture_output=True,
                             text=True, timeout=30)
        return (out.stdout or out.stderr).strip()
    except Exception as e:  # pragma: no cover - environment dependent
        return "unknown: %s" % e


def _connect_ro(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        raise CodexEnvError("database not found: %s" % path)
    uri = "file:%s?mode=ro" % path.replace("\\", "/").replace("?", "%3f")
    return sqlite3.connect(uri, uri=True, timeout=10)


def _latest_db(codex_home: str, prefix: str) -> str:
    """Newest state_N.sqlite matching *prefix* (Codex bumps the number)."""
    pattern = os.path.join(codex_home, "%s*.sqlite" % prefix)
    hits = [p for p in glob.glob(pattern)
            if not p.endswith(("-wal", "-shm"))]
    if not hits:
        raise CodexEnvError(
            "no %s*.sqlite under %s (Codex >= 0.15x with sqlite state "
            "required)" % (prefix, codex_home))
    return max(hits, key=lambda p: (os.path.getmtime(p), p))


# ---------------------------------------------------------------- turn scans

@dataclass
class TurnFailure:
    thread_id: str
    turn_id: str
    completed_at: int | None          # unix seconds (may be None)
    codex_error_info: str            # usageLimitExceeded / serverOverloaded / ...
    message: str
    reset_at_iso: str | None = None  # filled by detect.py
    window: str = "unknown"          # primary / weekly / unknown

@dataclass
class ThreadInfo:
    id: str
    title: str = ""
    model: str | None = None
    reasoning_effort: str | None = None
    updated_at_ms: int = 0
    archived: int = 0
    cwd: str = ""
    name: str | None = None


class CodexState:
    """Read-only view over the Codex state databases."""

    def __init__(self, codex_home: str):
        self.home = codex_home
        self._state_db = _latest_db(codex_home, "state_")
        self._history_db = _latest_db(codex_home, "thread_history_")
        self._goals_db: str | None = None
        for p in glob.glob(os.path.join(codex_home, "goals_*.sqlite")):
            if not p.endswith(("-wal", "-shm")):
                if self._goals_db is None or os.path.getmtime(p) > os.path.getmtime(self._goals_db):
                    self._goals_db = p

    @property
    def state_db(self) -> str:
        return self._state_db

    @property
    def history_db(self) -> str:
        return self._history_db

    @property
    def goals_db(self) -> str | None:
        return self._goals_db

    def failed_turns(self, since_completed_at: int = 0,
                     limit: int = 200) -> list[TurnFailure]:
        """Turns with a non-empty error_json, newest first.

        *since_completed_at* is a unix-seconds watermark; rows without a
        completed_at are always included (the caller dedups by turn_id).
        """
        out: list[TurnFailure] = []
        con = _connect_ro(self._history_db)
        try:
            rows = con.execute(
                "SELECT thread_id, turn_id, status, error_json, completed_at "
                "FROM thread_turns "
                "WHERE error_json IS NOT NULL AND error_json != '' "
                "AND (completed_at IS NULL OR completed_at > ?) "
                "ORDER BY completed_at DESC LIMIT ?", (since_completed_at, limit))
            for thread_id, turn_id, status, error_json, completed_at in rows:
                try:
                    payload = json.loads(error_json)
                except (ValueError, TypeError):
                    payload = {"message": str(error_json)[:500]}
                message = str(payload.get("message") or "")
                info = str(payload.get("codexErrorInfo") or
                           payload.get("codex_error_info") or "")
                out.append(TurnFailure(
                    thread_id=thread_id, turn_id=turn_id,
                    completed_at=completed_at, codex_error_info=info,
                    message=message))
        finally:
            con.close()
        return out

    def busy_threads(self, item_fresh_sec: int = 900) -> set[str]:
        """Threads with a turn genuinely in progress.

        Crashed/restarted sessions leave zombie ``inProgress`` rows behind,
        so a live turn additionally requires recent thread activity: a new
        thread_item written within *item_fresh_sec*. Threads whose turn only
        started recently (no items yet) also count as busy.
        """
        import time as _time
        con = _connect_ro(self._history_db)
        try:
            now_ms = _time.time() * 1000
            rows = con.execute(
                "SELECT t.thread_id, t.started_at, "
                "(SELECT MAX(i.created_at_ms) FROM thread_items i "
                " WHERE i.thread_id = t.thread_id) AS last_item_ms "
                "FROM thread_turns t WHERE t.status = 'inProgress'")
            busy: set[str] = set()
            for thread_id, started_at, last_item_ms in rows:
                if last_item_ms:
                    if now_ms - last_item_ms <= item_fresh_sec * 1000:
                        busy.add(thread_id)
                elif started_at and now_ms - started_at * 1000 <= 4 * 3600 * 1000:
                    busy.add(thread_id)
            return busy
        finally:
            con.close()

    def thread(self, thread_id: str) -> ThreadInfo | None:
        con = _connect_ro(self._state_db)
        try:
            row = con.execute(
                "SELECT id, title, model, reasoning_effort, updated_at_ms, "
                "archived, cwd, name FROM threads WHERE id = ?",
                (thread_id,)).fetchone()
        finally:
            con.close()
        if not row:
            return None
        return ThreadInfo(*row)

    def list_threads(self, include_archived: bool = False,
                     limit: int = 100) -> list[ThreadInfo]:
        con = _connect_ro(self._state_db)
        try:
            where = "" if include_archived else "WHERE archived = 0"
            rows = con.execute(
                "SELECT id, title, model, reasoning_effort, updated_at_ms, "
                "archived, cwd, name FROM threads %s "
                "ORDER BY updated_at_ms DESC LIMIT %d" % (where, limit)).fetchall()
        finally:
            con.close()
        return [ThreadInfo(*r) for r in rows]

    def latest_thread_id(self) -> str | None:
        threads = self.list_threads(limit=1)
        return threads[0].id if threads else None

    # ------------------------------------------------------------------ goals

    def goals(self) -> list[dict[str, Any]]:
        if not self._goals_db:
            return []
        con = _connect_ro(self._goals_db)
        try:
            rows = con.execute(
                "SELECT thread_id, goal_id, objective, status, tokens_used, "
                "updated_at_ms FROM thread_goals").fetchall()
        finally:
            con.close()
        keys = ("thread_id", "goal_id", "objective", "status", "tokens_used",
                "updated_at_ms")
        return [dict(zip(keys, r)) for r in rows]
