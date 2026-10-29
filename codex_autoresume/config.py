"""Configuration loading and defaults.

Config is a TOML file (default ``~/.codex-autoresume/config.toml``). Every key
has a built-in default, so an empty (or missing) config file yields a working
setup. Unknown keys are rejected with a clear error to catch typos.
"""

from __future__ import annotations

import copy
import os
import sys
import tomllib
from dataclasses import dataclass, field
from typing import Any

try:  # Python >= 3.11
    import tomllib as _toml_load_mod
except ImportError:  # pragma: no cover
    _toml_load_mod = None  # type: ignore[assignment]

APP_DIR_NAME = ".codex-autoresume"

DEFAULT_PROMPT_GOAL = (
    "Your previous turn was interrupted because the account usage limit was "
    "exhausted. The quota window has now reset. Continue pursuing the thread "
    "goal from where it stopped."
)

# Kept deliberately minimal: the resumed thread re-reads its whole context
# anyway, so the prompt must only (1) tell the model to continue exactly
# where it stopped and (2) suppress the expensive failure modes -- progress
# recaps, re-confirmation questions, redoing finished steps.
DEFAULT_PROMPT_GENERIC = (
    "Continue the unfinished task from exactly where it stopped. Do not "
    "summarize or restate progress, do not ask for confirmation, and do "
    "not redo completed steps."
)

DEFAULTS: dict[str, Any] = {
    "general": {
        # master switch; when false the daemon only observes and logs
        "enabled": True,
        # empty -> auto-detect: `codex` on PATH, then desktop-app install dirs
        "codex_bin": "",
        # empty -> ~/.codex (respects CODEX_HOME)
        "codex_home": "",
        # empty -> ~/.codex-autoresume
        "state_dir": "",
        "poll_interval_sec": 30,
        # extra wait after a parsed reset time before resuming: quota
        # windows do not roll on the exact minute, give them room
        # random extra wait (0..N) to avoid thundering-herd on shared resets
        "jitter_sec": 45,
        # per-thread minimum spacing between two resume attempts (kept at
        # the same 3-minute ceiling as the transient retry backoff)
        "min_resume_interval_sec": 180,
        # global safety budget
        "max_resumes_per_day": 60,
        "max_resumes_per_thread_per_day": 12,
        # weekly/secondary-window handling:
        #   "off"         - never wait for weekly resets
        #   "per-thread"  - only threads with weekly_resume=true in [threads]
        #   "all"         - wait for weekly resets for every tracked thread
        "weekly_resume": "off",
        # when a resume fails with "model at capacity", rotate through these
        "fallback_models": [],
        "server_overloaded_retries": 3,
        "server_overloaded_cooldown_sec": 180,
        # when the app holds a goal thread's writer (all RPC rejected),
        # click the app's own Continue button via UI automation
        "uia_resume": True,
        # when true the daemon detects and plans but never invokes codex:
        # no prompts are ever sent (safe first-deployment mode)
        "dry_run": False,
        # dry_run etc. above; auto_track defaults OFF: guarding is opt-in
        # per conversation (control UI / `watch`), never automatic
        "auto_track": False,
        # only auto-track threads whose last update is within this window
        "auto_track_max_age_hours": 36,
        # optional shell command run on events; {} placeholders:
        #   {event} {thread_id} {reason} {detail}
        "notify_command": "",
        # hard timeout for one resume invocation (a resumed turn may run long)
        "resume_timeout_sec": 21600,
        # concurrent resume invocations
        "max_concurrent_resumes": 2,
        # a guarded conversation whose newest turn was interrupted (Stop
        # button / app quit) is continued like the app's Continue button
        # once the turn has been idle this long (0 disables nothing; it is
        # a settle window so a just-pressed Stop is not fought instantly)
        "stopped_continue_sec": 60,
    },
    "resume": {
        # "exec"  -> codex exec resume <id> "<prompt>" (supports model overrides)
        # "queue" -> codex queue --thread <id> --message "<prompt>" (in-app,
        #            preserves the thread's own settings, no model override)
        "mode": "exec",
        # run a minimal-cost probe turn before the real resume (adds a tiny
        # "reply OK" turn to the thread; off by default because the
        # rate-limit snapshot gate already verifies recovery)
        "probe_first": False,
        "prompt_goal": DEFAULT_PROMPT_GOAL,
        "prompt_generic": DEFAULT_PROMPT_GENERIC,
        # empty -> inherit the thread's own model / effort
        "model": "",
        "reasoning_effort": "",
        # extra -c key=value overrides applied to every exec resume
        "extra_config": [],
        # extra raw args appended to the codex invocation
        "codex_extra_args": [],
    },
    "limits_probe": {
        # use `codex app-server` JSON-RPC account/rateLimits/read as the
        # recovery gate; disable to rely on parsed reset times only
        "use_app_server": True,
        # while a thread is waiting for its reset, refresh limits this often
        "poll_sec": 60,
        # fallback gate when ordinaryUsageAllowed is unavailable:
        # resume only if primary.usedPercent is below this
        "recovery_used_percent_max": 95,
    },
    # local control API (127.0.0.1) serving the built-in panel and the
    # in-app guard toggles
    "control_api": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8765,
    },
    # external Tibo news timeline (reset announcements + ordinary posts from
    # several public trackers); fetched by the daemon in the background,
    # never from HTTP handlers
    "announcements": {
        "enabled": True,
        # how often the three sources are re-fetched (seconds)
        "refresh_sec": 120,
        "timeout_sec": 10,
        # reset announcements / forecast banner (HTML)
        # "url": "https://opentherank.com/codex-reset",
        # structured X timeline incl. ship/feature days (JSON)
        # "codex_reset_url": "https://codex-reset.com/api/feed",
        # full recent Tibo timeline incl. ordinary posts (HTML)
        # "willreset_url": "https://willreset.com/tibo",
    },
    # self-update: check the latest GitHub release and let the panel offer a
    # one-click update (detached helper; the daemon restarts itself)
    "update": {
        "enabled": True,
        # how often the release feed is re-checked (seconds)
        "check_sec": 86400,
        "timeout_sec": 8,
        # latest-release JSON (GitHub-compatible)
        "release_url": ("https://api.github.com/repos/Awenforever/"
                        "codex-autoresume/releases/latest"),
        # force a checkout path when the running package is not inside it
        "repo": "",
        # pip installs: package source used by the helper
        "pip_source": "git+https://github.com/Awenforever/codex-autoresume",
        # refresh this Codex plugin after a successful update; "" disables
        "plugin_spec": "codex-autoresume-guard@codex-autoresume",
    },
    # per-thread overrides, keyed by thread UUID (full or prefix):
    # [threads."01a05ddb-0483-..."]
    # enabled = true
    # model = "gpt-6.1-sol"
    # reasoning_effort = "high"
    # prompt = "..."
    # mode = "exec"
    # weekly_resume = true
    "threads": {},
}

_THREAD_KEYS = {
    "enabled", "model", "reasoning_effort", "prompt", "mode", "weekly_resume",
    "max_resumes_per_day", "priority",
}


class ConfigError(Exception):
    pass


def _merge(base: dict, override: dict, path: str) -> dict:
    out = copy.deepcopy(base)
    thread_table = path == "threads" or path.startswith("threads.")
    for k, v in override.items():
        if k not in out:
            if thread_table:
                # [threads."<uuid>"] tables accept arbitrary thread keys;
                # they are validated against _THREAD_KEYS afterwards
                out[k] = {}
            else:
                raise ConfigError("unknown config key: %s.%s" % (path, k))
        cur = out[k]
        if isinstance(cur, dict) and isinstance(v, dict):
            child = (path + "." + k) if path else k
            out[k] = _merge(cur, v, child)
        else:
            out[k] = copy.deepcopy(v)
    return out


def default_state_dir() -> str:
    override = os.environ.get("CODEX_AUTORESUME_HOME")
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), APP_DIR_NAME)


def default_config_path() -> str:
    return os.path.join(default_state_dir(), "config.toml")


def load_config(path: str | None = None) -> dict:
    """Load and validate config from *path* (or the default location)."""
    cfg = copy.deepcopy(DEFAULTS)
    if path is None:
        path = default_config_path()
    if os.path.exists(path):
        try:
            with open(path, "rb") as f:
                user = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError("failed to parse %s: %s" % (path, e)) from e
        cfg = _merge(cfg, user, "")
    _validate(cfg)
    cfg["_config_path"] = os.path.abspath(path)
    return cfg


def _validate(cfg: dict) -> None:
    g = cfg["general"]
    if g["weekly_resume"] not in ("off", "per-thread", "all"):
        raise ConfigError("general.weekly_resume must be off|per-thread|all")
    if cfg["resume"]["mode"] not in ("exec", "queue"):
        raise ConfigError("resume.mode must be exec|queue")
    if g["poll_interval_sec"] < 5:
        raise ConfigError("general.poll_interval_sec must be >= 5")
    if g["max_concurrent_resumes"] < 1:
        raise ConfigError("general.max_concurrent_resumes must be >= 1")
    for tid, t in cfg["threads"].items():
        if not isinstance(t, dict):
            raise ConfigError("threads.%s must be a table" % tid)
        bad = set(t) - _THREAD_KEYS
        if bad:
            raise ConfigError(
                "unknown key(s) in threads.%s: %s" % (tid, ", ".join(sorted(bad)))
            )
        if "mode" in t and t["mode"] not in ("exec", "queue"):
            raise ConfigError("threads.%s.mode must be exec|queue" % tid)
        prio = t.get("priority")
        if prio is not None and (isinstance(prio, bool)
                                 or not isinstance(prio, int) or prio < 0):
            raise ConfigError(
                "threads.%s.priority must be a non-negative integer" % tid)


def thread_config(cfg: dict, thread_id: str) -> dict:
    """Effective per-thread settings for *thread_id* (longest UUID prefix wins)."""
    tid = thread_id.lower()
    match = None
    for key in cfg["threads"]:
        k = key.lower()
        if tid == k or tid.startswith(k) or k.startswith(tid):
            if match is None or len(k) > len(match):
                match = k
    out = {
        "enabled": None,
        "model": None,
        "reasoning_effort": None,
        "prompt": None,
        "mode": None,
        "weekly_resume": None,
        "max_resumes_per_day": None,
        "priority": None,
    }
    if match is not None:
        for k, v in cfg["threads"][match].items():
            out[k] = v
    return out


def resolve_codex_home(cfg: dict) -> str:
    if cfg["general"]["codex_home"]:
        return os.path.abspath(cfg["general"]["codex_home"])
    env = os.environ.get("CODEX_HOME")
    if env:
        return os.path.abspath(env)
    return os.path.join(os.path.expanduser("~"), ".codex")


def resolve_state_dir(cfg: dict) -> str:
    if cfg["general"]["state_dir"]:
        return os.path.abspath(cfg["general"]["state_dir"])
    return default_state_dir()
