"""Quota snapshot via ``codex app-server`` (JSON-RPC over stdio).

Spawns a short-lived ``codex app-server --listen stdio://`` child process and
calls ``account/rateLimits/read``. This is the authoritative recovery gate:
the schema explicitly documents ``ordinaryUsageAllowed`` as the permission
signal and warns clients not to infer recovery from percentages or reset
times.

Response shape (abridged, codex app-server protocol):

    result: {
      "ordinaryUsageAllowed": true | false | null,
      "rateLimits": {
        "primary":   {"usedPercent": int, "resetsAt": unix-sec|null, ...},
        "secondary": {...},
        "planType": "...", "rateLimitReachedType": "..." | null
      },
      "rateLimitResetCredits": {"availableCount": int,
                                "credits": [{id, grantedAt, expiresAt,
                                             resetType, status, title}]}
    }

The transport is newline-delimited JSON (one JSON-RPC message per line).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

INIT_TIMEOUT = 20
CALL_TIMEOUT = 30


class RateLimitError(Exception):
    pass


@dataclass
class Window:
    used_percent: int | None = None
    resets_at: int | None = None        # unix seconds
    window_duration_mins: int | None = None


@dataclass
class ResetCredit:
    id: str
    granted_at: int
    expires_at: int | None
    reset_type: str
    status: str
    title: str | None = None


@dataclass
class QuotaSnapshot:
    ordinary_usage_allowed: bool | None = None
    primary: Window = field(default_factory=Window)
    secondary: Window = field(default_factory=Window)
    plan_type: str | None = None
    rate_limit_reached_type: str | None = None
    reset_credits: list[ResetCredit] = field(default_factory=list)
    fetched_at: float = field(default_factory=time.time)
    raw: dict | None = None

    # ------------------------------------------------------------- helpers

    @classmethod
    def from_response(cls, resp: dict) -> "QuotaSnapshot":
        snap = cls(raw=resp)
        snap.ordinary_usage_allowed = resp.get("ordinaryUsageAllowed")
        limits = (resp.get("rateLimits") or {})
        snap.primary = _window(limits.get("primary"))
        snap.secondary = _window(limits.get("secondary"))
        snap.plan_type = limits.get("planType")
        snap.rate_limit_reached_type = limits.get("rateLimitReachedType")
        credits = (resp.get("rateLimitResetCredits") or {}).get("credits") or []
        for c in credits:
            try:
                snap.reset_credits.append(ResetCredit(
                    id=str(c.get("id")),
                    granted_at=int(c.get("grantedAt") or 0),
                    expires_at=(int(c["expiresAt"])
                                if c.get("expiresAt") is not None else None),
                    reset_type=str(c.get("resetType") or "unknown"),
                    status=str(c.get("status") or "unknown"),
                    title=c.get("title"),
                ))
            except (TypeError, ValueError):
                continue
        return snap

    def recovery_ok(self, fallback_used_percent_max: int = 95) -> tuple[bool, str]:
        """Decide whether usage is available again.

        Returns (ok, reason). Prefers ordinaryUsageAllowed; when unavailable
        (null), falls back to the primary window percentage gate.
        """
        if self.ordinary_usage_allowed is True:
            if self.primary.used_percent is not None and self.primary.used_percent >= 100:
                return False, "ordinaryUsageAllowed=true but primary window at 100%%"
            return True, "ordinaryUsageAllowed"
        if self.ordinary_usage_allowed is False:
            return False, "ordinaryUsageAllowed=false"
        if self.primary.used_percent is None:
            return True, "no quota data; allowing attempt"
        if self.primary.used_percent >= fallback_used_percent_max:
            return False, "primary window used %d%% >= %d%% (fallback gate)" % (
                self.primary.used_percent, fallback_used_percent_max)
        return True, "primary used %d%% below fallback gate" % self.primary.used_percent

    def next_reset_epoch(self) -> int | None:
        """Earliest known reset (primary, then secondary)."""
        for w in (self.primary, self.secondary):
            if w.resets_at:
                return w.resets_at
        return None


def _window(d: dict | None) -> Window:
    d = d or {}
    return Window(
        used_percent=(int(d["usedPercent"]) if d.get("usedPercent") is not None else None),
        resets_at=(int(d["resetsAt"]) if d.get("resetsAt") is not None else None),
        window_duration_mins=(int(d["windowDurationMins"])
                              if d.get("windowDurationMins") is not None else None),
    )


class AppServerClient:
    """Minimal newline-JSON-RPC client for a spawned codex app-server."""

    def __init__(self, codex_bin: str, client_name: str = "codex-autoresume",
                 client_version: str = "0.1.0"):
        self.codex_bin = codex_bin
        self.client_name = client_name
        self.client_version = client_version
        self._proc: subprocess.Popen | None = None
        self._next_id = 1

    # ------------------------------------------------------------ lifecycle

    def _ensure_proc(self) -> subprocess.Popen:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        self.close()
        creationflags = CREATE_NO_WINDOW
        self._proc = subprocess.Popen(
            [self.codex_bin, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
            errors="replace", creationflags=creationflags)
        self._rpc("initialize", {
            "clientInfo": {
                "name": self.client_name,
                "title": self.client_name,
                "version": self.client_version,
            },
        }, timeout=INIT_TIMEOUT)
        return self._proc

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        except Exception:
            pass

    # ---------------------------------------------------------------- rpc

    def _rpc(self, method: str, params: dict | None = None,
             timeout: float = CALL_TIMEOUT) -> dict:
        proc = self._ensure_proc()
        assert proc.stdin is not None and proc.stdout is not None
        rid = self._next_id
        self._next_id += 1
        msg: dict = {"id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                raise RateLimitError("app-server closed stdout")
            line = line.strip()
            if not line:
                continue
            try:
                resp = json.loads(line)
            except ValueError:
                continue
            if resp.get("id") != rid:
                continue          # notification or someone else's response
            if "error" in resp:
                err = resp["error"]
                raise RateLimitError("%s failed: %s" % (method, err))
            return resp.get("result") or {}
        raise RateLimitError("%s timed out after %.0fs" % (method, timeout))

    # ------------------------------------------------------------- public

    def read_rate_limits(self) -> QuotaSnapshot:
        result = self._rpc("account/rateLimits/read", None)
        return QuotaSnapshot.from_response(result)


def fetch_quota_snapshot(codex_bin: str) -> QuotaSnapshot:
    """One-shot convenience: spawn, read, shut down."""
    client = AppServerClient(codex_bin)
    try:
        return client.read_rate_limits()
    finally:
        client.close()
