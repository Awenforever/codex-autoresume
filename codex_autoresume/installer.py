"""OS-native unattended runners (per-user, no administrator required).

Windows:  Task Scheduler job at logon, running pythonw.exe (windowless)
macOS:    LaunchAgent plist + launchctl
Linux:    systemd user unit
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

TASK_NAME = "codex-autoresume"
PLIST_ID = "com.codex-autoresume"


def _pythonw() -> str:
    """pythonw.exe next to the current interpreter when available."""
    exe = Path(sys.executable)
    if os.name == "nt":
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def _repo_root() -> str | None:
    """Checkout root when running from a source tree (not site-packages)."""
    here = Path(__file__).resolve().parent
    root = here.parent
    if (root / "pyproject.toml").exists() and here.name == "codex_autoresume":
        return str(root)
    return None


def _daemon_cmd(state_dir: str | None = None) -> list[str]:
    """Argument list for the runner.

    From a source checkout the runner must import the package from the repo
    without relying on the working directory (Task Scheduler starts in the
    profile dir). A ``python -c`` bootstrap keeps it a SINGLE process so
    that `schtasks /End` / service stop kills the actual daemon instead of
    leaving an orphan behind a ``cmd`` wrapper. Installed via pip, the
    console script is self-contained.
    """
    root = _repo_root()
    if root is None:
        argv = ["codex-autoresume", "daemon"]
    else:
        code = ("import sys; sys.path.insert(0, %r); "
                "from codex_autoresume.cli import main; "
                "sys.argv = ['codex-autoresume', 'daemon']; "
                "raise SystemExit(main())") % root
        argv = [_pythonw(), "-c", code]
    env = os.environ.get("CODEX_AUTORESUME_HOME")
    if state_dir:
        argv += ["--state-dir", state_dir]
    elif env:
        argv += ["--state-dir", env]
    return argv


def _runner_command(argv: list[str]) -> str:
    return _quote(argv)


def _quote(argv: list[str]) -> str:
    if os.name == "nt":
        return " ".join('"%s"' % a for a in argv)
    return " ".join(argv.replace('"', '\\"') if " " in a else a for a in argv)


def install(state_dir: str | None = None) -> str:
    system = platform.system()
    if system == "Windows":
        return _install_windows(state_dir)
    if system == "Darwin":
        return _install_macos(state_dir)
    return _install_linux(state_dir)


def uninstall() -> str:
    system = platform.system()
    if system == "Windows":
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"],
                       capture_output=True, text=True)
        return "scheduled task %s removed" % TASK_NAME
    if system == "Darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / (PLIST_ID + ".plist")
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        if plist.exists():
            plist.unlink()
        return "LaunchAgent removed"
    subprocess.run(["systemctl", "--user", "disable", "--now", TASK_NAME],
                   capture_output=True)
    unit = Path.home() / ".config" / "systemd" / "user" / (TASK_NAME + ".service")
    if unit.exists():
        unit.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    return "systemd user unit removed"


def status() -> str:
    system = platform.system()
    if system == "Windows":
        out = subprocess.run(
            ["schtasks", "/Query", "/TN", TASK_NAME],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        return "installed" if out.returncode == 0 else "not installed"
    if system == "Darwin":
        out = subprocess.run(["launchctl", "list", PLIST_ID],
                             capture_output=True, text=True)
        return "installed" if out.returncode == 0 else "not installed"
    out = subprocess.run(["systemctl", "--user", "is-enabled", TASK_NAME],
                         capture_output=True, text=True)
    return "installed" if out.returncode == 0 else "not installed"


# ------------------------------------------------------------------ windows

def _install_windows(state_dir: str | None) -> str:
    cmd = _runner_command(_daemon_cmd(state_dir))
    out = subprocess.run(
        ["schtasks", "/Create", "/TN", TASK_NAME, "/TR", cmd,
         "/SC", "ONLOGON", "/F"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if out.returncode != 0:
        raise RuntimeError("schtasks failed: %s%s" % (out.stdout, out.stderr))
    subprocess.run(["schtasks", "/Run", "/TN", TASK_NAME], capture_output=True)
    return ("scheduled task '%s' created at logon and started now "
            "(runs: %s)" % (TASK_NAME, cmd))


# -------------------------------------------------------------------- macos

def _install_macos(state_dir: str | None) -> str:
    import plistlib
    argv = _daemon_cmd(state_dir)
    agents = Path.home() / "Library" / "LaunchAgents"
    agents.mkdir(parents=True, exist_ok=True)
    plist = agents / (PLIST_ID + ".plist")
    payload = {
        "Label": PLIST_ID,
        "ProgramArguments": argv,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(Path.home() / ".codex-autoresume" / "launchd.out"),
        "StandardErrorPath": str(Path.home() / ".codex-autoresume" / "launchd.err"),
    }
    root = _repo_root()
    if root:
        payload["WorkingDirectory"] = root
    plist.write_bytes(plistlib.dumps(payload))
    subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
    out = subprocess.run(["launchctl", "load", str(plist)], capture_output=True,
                         text=True)
    if out.returncode != 0:
        raise RuntimeError("launchctl failed: %s" % out.stderr)
    return "LaunchAgent %s loaded" % plist


# -------------------------------------------------------------------- linux

def _install_linux(state_dir: str | None) -> str:
    argv = _daemon_cmd(state_dir)
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit = unit_dir / (TASK_NAME + ".service")
    root = _repo_root()
    body = "\n".join([
        "[Unit]",
        "Description=codex-autoresume unattended resume watcher",
        "",
        "[Service]",
        "ExecStart=%s" % _quote(argv),
        "Restart=always",
        "RestartSec=30",
    ] + (["WorkingDirectory=%s" % root] if root else []) + [
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])
    unit.write_text(body, encoding="utf-8")
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    out = subprocess.run(
        ["systemctl", "--user", "enable", "--now", TASK_NAME],
        capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError("systemctl failed: %s" % (out.stderr or out.stdout))
    return "systemd user unit enabled: %s" % unit
