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
import urllib.error
import urllib.request
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

    def has_primary_headroom(self) -> bool:
        """True when the 5-hour window still has quota (nonzero).

        usedPercent < 100 is the ground truth; ordinaryUsageAllowed can lag
        behind reality, so it does not veto a window that shows headroom.
        """
        return self.primary.used_percent is None or self.primary.used_percent < 100

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
            # thread/settings/update, thread/goal/set, thread/queue/* are
            # gated behind the experimentalApi capability
            "capabilities": {"experimentalApi": True},
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


def fetch_quota_snapshot(codex_bin: str, attempts: int = 2) -> QuotaSnapshot:
    """One-shot convenience: spawn, read, shut down (with retry).

    The backend call can fail transiently (proxy/network blips); one retry
    avoids noisy failures for one-shot CLI usage.
    """
    last: Exception | None = None
    for _ in range(max(1, attempts)):
        client = AppServerClient(codex_bin)
        try:
            return client.read_rate_limits()
        except (RateLimitError, OSError) as e:
            last = e
            time.sleep(1.5)
        finally:
            client.close()
    raise last if last else RateLimitError("unreachable")


WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
WHAM_CREDITS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"


def _wininet_proxy() -> str | None:
    """System (WinINET) proxy from the registry, if enabled.

    urllib's auto-detection is unreliable here: an environment NO_PROXY
    var alone makes getproxies() return without the registry proxy, which
    left wham/usage timing out on machines where chatgpt.com is only
    reachable through the local proxy app.
    """
    if os.name != "nt":
        return None
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
        try:
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            server = winreg.QueryValueEx(key, "ProxyServer")[0]
        finally:
            winreg.CloseKey(key)
        if not enabled or not server:
            return None
        if "=" in server:            # per-scheme form "http=...;https=..."
            parts = [p.split("=", 1) for p in server.split(";") if "=" in p]
            pick = dict(parts).get("https") or dict(parts).get("http")
            server = pick or ""
        if not server:
            return None
        if "://" not in server:
            server = "http://" + server
        return server
    except Exception:
        return None


def fetch_quota_http(codex_home: str, timeout: float = 20) -> QuotaSnapshot:
    """Quota snapshot via direct backend HTTP (NO app-server spawn).

    Spawning ``codex app-server`` per poll makes each child connect to the
    remote-control websocket with the machine's installation id, which can
    fight the desktop app's own connection (visible as "reconnecting to
    network" in the app). This path reads ``auth.json`` (fresh, read-only)
    and calls the usage endpoints directly instead.
    """
    import json as _json
    import urllib.request as _rq
    auth_path = os.path.join(codex_home, "auth.json")
    try:
        with open(auth_path, encoding="utf-8") as f:
            auth = _json.load(f)
        tokens = auth.get("tokens") or {}
        token = tokens.get("access_token")
        account = tokens.get("account_id")
    except (OSError, ValueError) as e:
        raise RateLimitError("auth.json unreadable: %s" % e)
    if not token:
        raise RateLimitError("auth.json has no access_token (logged out?)")

    headers = {"Authorization": "Bearer " + token,
               "User-Agent": "codex-autoresume/0.3",
               "Accept": "application/json"}
    if account:
        headers["chatgpt-account-id"] = account

    proxy = _wininet_proxy()
    opener = _rq.build_opener(
        _rq.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}))

    def _get(url: str):
        req = _rq.Request(url, headers=headers)
        with opener.open(req, timeout=timeout) as resp:
            return _json.loads(resp.read().decode("utf-8", "replace"))

    try:
        body = _get(WHAM_USAGE_URL)
    except urllib.error.HTTPError as e:
        raise RateLimitError("wham/usage HTTP %d" % e.code)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise RateLimitError("wham/usage unreachable: %s" % e)

    rl = body.get("rate_limit") or {}
    def _win(d: dict) -> Window:
        d = d or {}
        return Window(
            used_percent=(int(d["used_percent"])
                          if d.get("used_percent") is not None else None),
            resets_at=(int(d["reset_at"]) if d.get("reset_at") is not None
                       else None),
            window_duration_mins=(int(d["limit_window_seconds"] // 60)
                                  if d.get("limit_window_seconds") else None),
        )
    snap = QuotaSnapshot(
        ordinary_usage_allowed=bool(rl.get("allowed")) and
        not bool(rl.get("limit_reached")),
        primary=_win(rl.get("primary_window")),
        secondary=_win(rl.get("secondary_window")),
        plan_type=body.get("plan_type"),
    )
    # reset credits (best-effort separate endpoint)
    try:
        credits = _get(WHAM_CREDITS_URL)
        items = None
        if isinstance(credits, dict):
            items = (credits.get("credits") or credits.get("data")
                     or credits.get("items"))
            if items is None and credits.get("available_count") == 0:
                items = []
        elif isinstance(credits, list):
            items = credits
        for c in (items or []):
            if not isinstance(c, dict):
                continue
            snap.reset_credits.append(ResetCredit(
                id=str(c.get("id") or c.get("credit_id") or ""),
                granted_at=int(c.get("granted_at") or c.get("grantedAt") or 0),
                expires_at=(int(c["expires_at"] if c.get("expires_at") is not
                                None else c.get("expiresAt", 0)) or None),
                reset_type=str(c.get("reset_type") or c.get("resetType")
                               or "unknown"),
                status=str(c.get("status") or "available"),
                title=c.get("title"),
            ))
    except Exception:
        pass                       # credits stay empty on any failure
    return snap


def models_from_cache(codex_home: str) -> list[dict] | None:
    """Model catalog from the app-maintained models_cache.json.

    Zero network: the desktop app keeps this file fresh. Returns None when
    the cache is missing or unreadable (caller falls back to app-server).
    """
    import json as _json
    path = os.path.join(codex_home, "models_cache.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = _json.load(f)
        out = []
        for m in (data.get("models") or []):
            vis = str(m.get("visibility") or "").lower()
            if vis in ("hidden", "internal", "private"):
                continue
            slug = m.get("slug") or m.get("id") or ""
            if not slug:
                continue
            out.append({"id": slug,
                        "name": m.get("display_name") or slug})
        return sorted(out, key=lambda x: x["name"].lower()) or None
    except (OSError, ValueError):
        return None


def list_models(codex_bin: str) -> list[dict]:
    """Available models via app-server ``model/list``.

    Returns [{"id", "name"}] sorted by display name, hidden models excluded.
    """
    client = AppServerClient(codex_bin)
    try:
        result = client._rpc("model/list", {"limit": 100})
        out = []
        for m in (result.get("data") or []):
            if m.get("hidden"):
                continue
            out.append({"id": m.get("id") or "",
                        "name": m.get("displayName") or m.get("id") or ""})
        return sorted(out, key=lambda m: m["name"].lower())
    finally:
        client.close()


def consume_reset_credit(codex_bin: str, idempotency_key: str,
                         credit_id: str | None = None) -> dict:
    """Redeem one official rate-limit reset credit.

    Financial-ish action: callers must gate this behind explicit per-thread
    opt-in. *idempotency_key* identifies one logical redemption attempt
    (reuse the same value when retrying); *credit_id* picks a specific
    credit, or let the backend choose the next available one.
    """
    client = AppServerClient(codex_bin)
    try:
        params: dict = {"idempotencyKey": idempotency_key}
        if credit_id:
            params["creditId"] = credit_id
        return client._rpc("account/rateLimitResetCredit/consume", params)
    finally:
        client.close()


class GoalBusyError(RateLimitError):
    """The thread already has an active writer (app is running/holding it).

    Not a failure: the guard must stand down and retry later instead of
    counting an error or ever falling back to message injection.
    """


def goal_continue(codex_bin: str, thread_id: str, model: str | None = None,
                  effort: str | None = None, origin: str = "user") -> dict:
    """Resume a parked goal exactly like the app's Continue button.

    Optionally updates the thread's model / reasoning effort for subsequent
    turns first, then flips the goal back to ``active`` via
    ``thread/goal/set`` with user-origin authorization. This never injects
    a message into the conversation: the goal engine drives its own
    continuation turns. A failed settings update is not fatal — the goal
    continue still proceeds with the thread's current settings.
    """
    client = AppServerClient(codex_bin)
    try:
        if model or effort:
            params: dict = {"threadId": thread_id}
            if model:
                params["model"] = model
            if effort:
                params["effort"] = effort
            try:
                client._rpc("thread/settings/update", params)
            except RateLimitError as e:
                import logging
                logging.getLogger("codex-autoresume").warning(
                    "thread/settings/update failed (%s); continuing goal "
                    "with existing thread settings", e)
        try:
            result = client._rpc("thread/goal/set", {
                "threadId": thread_id, "status": "active", "origin": origin})
            return result
        except RateLimitError as e:
            if "active writer" in str(e).lower():
                raise GoalBusyError(str(e)) from None
            raise
    finally:
        client.close()
