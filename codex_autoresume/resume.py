"""Build and run resume invocations, and classify their outcomes."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field

CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

USAGE_LIMIT_PATTERNS = [
    re.compile(r"usageLimitExceeded", re.IGNORECASE),
    re.compile(r"You'?ve hit your usage limit", re.IGNORECASE),
    re.compile(r"usage limit", re.IGNORECASE),
    re.compile(r"try again (?:at|by)\s+", re.IGNORECASE),
]
OVERLOADED_PATTERNS = [
    re.compile(r"serverOverloaded", re.IGNORECASE),
    re.compile(r"Selected model is at capacity", re.IGNORECASE),
    re.compile(r"model is at capacity", re.IGNORECASE),
]
ARCHIVED_PATTERN = re.compile(r"is archived", re.IGNORECASE)
NO_ROLLOUT_PATTERN = re.compile(r"no rollout found", re.IGNORECASE)
WRITER_CONFLICT_PATTERNS = [
    re.compile(r"already has an active writer", re.IGNORECASE),
    re.compile(r"thread-store conflict", re.IGNORECASE),
]


@dataclass
class ResumeOutcome:
    ok: bool
    kind: str                  # success | usage_limit | overloaded | archived |
                               # no_rollout | not_found | other_error
    reset_at_iso: str | None = None
    exit_code: int | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    duration_sec: float = 0.0


def build_exec_resume_command(codex_bin: str, thread_id: str, prompt: str,
                              model: str = "", reasoning_effort: str = "",
                              extra_config: list[str] | None = None,
                              codex_extra_args: list[str] | None = None,
                              cwd: str | None = None) -> list[str]:
    """codex exec resume <thread> <prompt> with optional overrides."""
    argv = [codex_bin, "exec", "resume", thread_id, prompt,
            "--skip-git-repo-check"]
    if cwd:
        argv += ["-C", cwd]
    if model:
        argv += ["-m", model]
    if reasoning_effort:
        argv += ["-c", "model_reasoning_effort=%s" % reasoning_effort]
    for kv in extra_config or []:
        argv += ["-c", kv]
    argv += list(codex_extra_args or [])
    return argv


def build_queue_command(codex_bin: str, thread_id: str, prompt: str) -> list[str]:
    """codex queue --thread <id> --message <text> (in-app delivery)."""
    return [codex_bin, "queue", "--thread", thread_id, "--message", prompt]


def build_probe_command(codex_bin: str, thread_id: str,
                        model: str = "") -> list[str]:
    """Minimal-cost probe turn (only used when resume.probe_first is true)."""
    argv = [codex_bin, "exec", "resume", thread_id,
            "Reply with exactly: OK", "--skip-git-repo-check"]
    if model:
        argv += ["-m", model]
    argv += ["-c", "model_reasoning_effort=none"]
    return argv


def _tail(text: str, n: int = 2000) -> str:
    return text[-n:].strip() if text else ""


# --------------------------------------------------------------------------
# Windows job object: children (codex exec resume) die with this process.
# Prevents orphaned codex processes from continuing to operate threads after
# the daemon is killed (Task Scheduler /End, logoff).
# --------------------------------------------------------------------------

_job_handle = None


def _ensure_job():
    global _job_handle
    if _job_handle is not None or os.name != "nt":
        return _job_handle
    try:
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount",
                "OtherOperationCount", "ReadTransferCount",
                "WriteTransferCount", "OtherTransferCount")]

        class BASIC_LIMITS(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_void_p),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED_LIMITS(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMITS),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]

        k32 = ctypes.windll.kernel32
        h = k32.CreateJobObjectW(None, None)
        info = EXTENDED_LIMITS()
        info.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(
                ctypes.c_void_p(h), 9, ctypes.byref(info),
                ctypes.sizeof(info)):
            raise OSError("SetInformationJobObject failed")
        _job_handle = h
    except Exception:
        _job_handle = None
    return _job_handle


def _assign_job(pid: int) -> None:
    h = _ensure_job()
    if h is None:
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001
        hp = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if hp:
            k32.AssignProcessToJobObject(ctypes.c_void_p(h), ctypes.c_void_p(hp))
            k32.CloseHandle(hp)
    except Exception:
        pass


def classify_output(exit_code: int, stdout: str, stderr: str) -> ResumeOutcome:
    """Classify a finished codex invocation from exit code + output text."""
    blob = "\n".join((stdout, stderr))
    from .timeparse import parse_reset_time
    if exit_code == 0:
        return ResumeOutcome(ok=True, kind="success", exit_code=exit_code,
                             stdout_tail=_tail(stdout), stderr_tail=_tail(stderr))
    reset = parse_reset_time(blob)
    for pat in USAGE_LIMIT_PATTERNS:
        if pat.search(blob):
            return ResumeOutcome(
                ok=False, kind="usage_limit", exit_code=exit_code,
                reset_at_iso=reset.isoformat() if reset else None,
                stdout_tail=_tail(stdout), stderr_tail=_tail(stderr))
    for pat in OVERLOADED_PATTERNS:
        if pat.search(blob):
            return ResumeOutcome(
                ok=False, kind="overloaded", exit_code=exit_code,
                stdout_tail=_tail(stdout), stderr_tail=_tail(stderr))
    for pat in WRITER_CONFLICT_PATTERNS:
        if pat.search(blob):
            return ResumeOutcome(
                ok=False, kind="writer_conflict", exit_code=exit_code,
                stdout_tail=_tail(stdout), stderr_tail=_tail(stderr))
    if ARCHIVED_PATTERN.search(blob):
        return ResumeOutcome(ok=False, kind="archived", exit_code=exit_code,
                             stdout_tail=_tail(stdout), stderr_tail=_tail(stderr))
    if NO_ROLLOUT_PATTERN.search(blob):
        return ResumeOutcome(ok=False, kind="no_rollout", exit_code=exit_code,
                             stdout_tail=_tail(stdout), stderr_tail=_tail(stderr))
    if re.search(r"(thread|session).*(not found|does not exist)", blob, re.IGNORECASE):
        return ResumeOutcome(ok=False, kind="not_found", exit_code=exit_code,
                             stdout_tail=_tail(stdout), stderr_tail=_tail(stderr))
    return ResumeOutcome(ok=False, kind="other_error", exit_code=exit_code,
                         stdout_tail=_tail(stdout), stderr_tail=_tail(stderr))


def run_command(argv: list[str], timeout: float = 21600.0,
                env: dict | None = None) -> ResumeOutcome:
    """Run a codex invocation and classify the result."""
    import time
    start = time.monotonic()
    merged_env = None
    if env:
        merged_env = dict(os.environ)
        merged_env.update(env)
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", env=merged_env,
            creationflags=CREATE_NO_WINDOW)
        if os.name == "nt":
            _assign_job(proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            outcome = classify_output(proc.returncode, stdout or "", stderr or "")
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                stdout, stderr = proc.communicate(timeout=10)
            except Exception:
                stdout = stderr = ""
            outcome = ResumeOutcome(ok=False, kind="timeout", exit_code=None,
                                    stdout_tail=_tail(stdout or ""),
                                    stderr_tail=_tail(stderr or ""))
    except OSError as e:
        outcome = ResumeOutcome(ok=False, kind="spawn_error", exit_code=None,
                                stderr_tail=str(e))
    outcome.duration_sec = time.monotonic() - start
    return outcome
