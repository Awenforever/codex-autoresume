"""Self-update: release check + detached update/restart helper.

Checking is a plain GET of the latest GitHub release. The update itself runs
in a separate helper process AFTER the daemon exits: a running process must
never replace its own code, and on Windows a daemon cannot reliably restart
itself from a worker thread. The helper pulls (or pip-installs) the new code,
refreshes the Codex plugin cache, starts the daemon again and leaves
``update_last.json`` for the new process to report in the panel.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

DEFAULT_RELEASE_URL = ("https://api.github.com/repos/Awenforever/"
                       "codex-autoresume/releases/latest")
DEFAULT_PIP_SOURCE = ("git+https://github.com/Awenforever/"
                      "codex-autoresume")
DEFAULT_PLUGIN_SPEC = "codex-autoresume-guard@codex-autoresume"
HELPER_NAME = "update_helper.py"
JOB_NAME = "update_job.json"
RESULT_NAME = "update_last.json"
LOG_NAME = "update.log"

_VER_RE = re.compile(r"^[vV]?(\d+(?:\.\d+)*)")


def parse_version(text: str):
    """``v0.3.50`` / ``0.3.50`` -> (0, 3, 50); unparseable -> None.

    A pre-release suffix is dropped for ordering (0.3.50-rc1 == 0.3.50),
    which also keeps a pre-release from re-flagging an installed version.
    """
    m = _VER_RE.match(str(text or "").strip())
    if not m:
        return None
    return tuple(int(x) for x in m.group(1).split("."))


def version_text(ver) -> str:
    return ".".join(str(x) for x in ver)


def is_newer(latest: str, current: str) -> bool:
    lv, cv = parse_version(latest), parse_version(current)
    if lv is None or cv is None:
        return False
    return lv > cv


def fetch_latest_release(url: str, timeout: float = 8.0) -> dict:
    """Latest published release, from a GitHub-compatible JSON endpoint."""
    from .announcements import _http_get
    raw = _http_get(url, "application/vnd.github+json", timeout,
                    limit=300_000)
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise ValueError("release feed is not JSON: %s" % e) from e
    if not isinstance(data, dict):
        raise ValueError("release feed is not an object")
    tag = str(data.get("tag_name") or data.get("name") or "").strip()
    ver = parse_version(tag)
    if ver is None:
        raise ValueError("release has no parseable version: %r" % tag)
    return {"tag": tag, "version": version_text(ver),
            "url": str(data.get("html_url") or ""),
            "published_at": str(data.get("published_at") or ""),
            "name": str(data.get("name") or tag)}


# --------------------------------------------------------------- preflight

def _run(argv: list[str], cwd: str | None = None,
         timeout: float = 15.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
        creationflags=0x08000000 if os.name == "nt" else 0)


def _tail(text: str | None, limit: int = 300) -> str:
    return " ".join((text or "").split())[-limit:]


def source_info(cfg: dict) -> dict:
    """Where the running code came from.

    ``source``: a git checkout (import path inside the tree, or the
    configured ``[update].repo``). ``pip``: a site-packages install; the
    upgrade source is ``[update].pip_source``.
    """
    ucfg = cfg.get("update") or {}
    forced = str(ucfg.get("repo") or "").strip()
    if forced:
        return {"kind": "source",
                "root": os.path.abspath(os.path.expanduser(forced)),
                "forced": True}
    from .installer import _repo_root
    root = _repo_root()
    if root:
        return {"kind": "source", "root": os.path.abspath(root),
                "forced": False}
    return {"kind": "pip",
            "pip_source": str(ucfg.get("pip_source")
                              or DEFAULT_PIP_SOURCE).strip()}


def preflight(cfg: dict) -> tuple[bool, str, dict | None]:
    """Can this install update itself right now? (ok, reason, source)."""
    ucfg = cfg.get("update") or {}
    if ucfg.get("enabled") is False:
        return False, "update checks are disabled", None
    src = source_info(cfg)
    if src["kind"] == "pip":
        if not src.get("pip_source"):
            return False, "no pip upgrade source configured", src
        return True, "", src
    root = src["root"]
    if not os.path.isfile(os.path.join(root, "pyproject.toml")):
        return False, "checkout not found at %s" % root, src
    if not os.path.isdir(os.path.join(root, ".git")):
        return False, "%s is not a git checkout" % root, src
    try:
        st = _run(["git", "-C", root, "status", "--porcelain"])
        if st.returncode != 0:
            return False, "git status failed: %s" % _tail(st.stderr), src
        if (st.stdout or "").strip():
            return (False, "the checkout has uncommitted changes; "
                    "commit or stash them before updating", src)
        up = _run(["git", "-C", root, "rev-parse", "--abbrev-ref",
                   "--symbolic-full-name", "@{u}"])
        if up.returncode != 0:
            return False, "the checkout has no upstream branch", src
    except (OSError, subprocess.SubprocessError) as e:
        return False, "git is unavailable: %s" % e, src
    return True, "", src


def resolve_pip_spec(pip_source: str, tag: str) -> str:
    """Pin a git source to the release tag (reproducible upgrade)."""
    src = (pip_source or "").strip()
    if not src or not tag:
        return src
    if src.startswith("git+") and "@" not in src.rsplit("/", 1)[-1]:
        return src + "@" + tag
    return src


def restart_command(state_dir: str) -> list[str]:
    """Manager-specific restart, only for the canonical instance.

    A sandbox / second instance must never drive the machine-wide task or
    service, or it would start (and take over) the production guard.
    """
    from .config import default_state_dir
    from .installer import TASK_NAME
    try:
        same = (os.path.abspath(state_dir)
                == os.path.abspath(default_state_dir()))
    except OSError:
        same = False
    if not same:
        return []
    if os.name == "nt":
        try:
            r = _run(["schtasks", "/Query", "/TN", TASK_NAME])
            if r.returncode == 0:
                return ["schtasks", "/Run", "/TN", TASK_NAME]
        except (OSError, subprocess.SubprocessError):
            pass
        return []
    if sys.platform == "darwin":
        return ["launchctl", "kickstart", "-k",
                "gui/%d/com.codex-autoresume" % os.getuid()]
    if sys.platform.startswith("linux"):
        return ["systemctl", "--user", "restart", TASK_NAME]
    return []


def build_job(cfg: dict, state_dir: str, codex_bin: str, src: dict,
              release: dict, current: str) -> dict:
    """Job description consumed by the detached helper."""
    from .installer import _launcher_path, _pythonw, _write_launcher
    launcher = _write_launcher(state_dir)
    ucfg = cfg.get("update") or {}
    tag = release.get("tag") or release.get("version") or ""
    return {
        "pid": os.getpid(),
        "state_dir": state_dir,
        "log_path": os.path.join(state_dir, LOG_NAME),
        "result_path": os.path.join(state_dir, RESULT_NAME),
        "job_path": os.path.join(state_dir, JOB_NAME),
        "kind": src["kind"],
        "repo": src.get("root") or "",
        "pip_source": resolve_pip_spec(
            src.get("pip_source") or DEFAULT_PIP_SOURCE, tag),
        "python_exe": sys.executable,
        "codex_bin": codex_bin or "",
        "plugin_spec": str(ucfg.get("plugin_spec", DEFAULT_PLUGIN_SPEC)
                           or ""),
        "relaunch_cmd": [_pythonw(), str(_launcher_path(state_dir))],
        "restart_cmd": restart_command(state_dir),
        "from": current,
        "to": release.get("version") or "",
        "tag": tag,
        "wait_sec": 180,
    }


def read_last(state_dir: str) -> dict | None:
    try:
        with open(os.path.join(state_dir, RESULT_NAME),
                  encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def write_helper(state_dir: str) -> str:
    os.makedirs(state_dir, exist_ok=True)
    path = os.path.join(state_dir, HELPER_NAME)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(_HELPER_SRC)
    os.replace(tmp, path)
    return path


def launch_helper(helper_path: str, job: dict) -> None:
    """Write the job and start the helper fully detached."""
    os.makedirs(job["state_dir"], exist_ok=True)
    job_path = job["job_path"]
    tmp = job_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False, indent=2)
    os.replace(tmp, job_path)
    kwargs = {
        "cwd": job["state_dir"],
        "close_fds": True,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = 0x00000008 | 0x08000000
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([job["python_exe"], helper_path, job_path], **kwargs)


# The helper is standalone on purpose: it must run after this package's
# files were replaced, so it imports nothing from codex_autoresume.
_HELPER_SRC = r'''"""codex-autoresume self-update helper (generated; safe to delete).

Waits for the old daemon to exit, updates the code, refreshes the Codex
plugin cache, starts the daemon again and records update_last.json.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def _log(path, msg):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def _flags():
    return 0x08000000 if os.name == "nt" else 0


def _proxy_env():
    """os.environ plus the live Windows system proxy when one is enabled.

    The local proxy app rotates its port; git keeps whatever ``http.proxy``
    it was configured with, so a stale value makes pip/git upgrades fail
    even though the network works. Explicit proxy env vars always win.
    """
    env = dict(os.environ)
    if os.name != "nt":
        return env
    if any(env.get(k) for k in ("HTTPS_PROXY", "https_proxy",
                                "HTTP_PROXY", "http_proxy")):
        return env
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
    except Exception:
        return env
    if not enabled or not server:
        return env
    if "=" in server:            # per-scheme form "http=...;https=..."
        pick = dict(p.split("=", 1) for p in server.split(";") if "=" in p)
        server = pick.get("https") or pick.get("http") or ""
    if not server:
        return env
    if "://" not in server:
        server = "http://" + server
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env[k] = server
    if "GIT_CONFIG_COUNT" not in os.environ:
        # like -c key=value on the command line: beats a stale config file
        env["GIT_CONFIG_COUNT"] = "1"
        env["GIT_CONFIG_KEY_0"] = "http.proxy"
        env["GIT_CONFIG_VALUE_0"] = server
    return env


def _wait_exit(pid, timeout):
    if not pid:
        return True
    if os.name == "nt":
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x00100000, False,
                                                   int(pid))
            if not h:
                return True
            try:
                return ctypes.windll.kernel32.WaitForSingleObject(
                    h, int(timeout * 1000)) == 0
            finally:
                ctypes.windll.kernel32.CloseHandle(h)
        except Exception:
            return True
    end = time.time() + timeout
    while time.time() < end:
        try:
            os.kill(int(pid), 0)
        except OSError:
            return True
        time.sleep(0.5)
    return False


def _run(name, argv, cwd, timeout):
    step = {"name": name, "cmd": argv, "rc": None, "out": "", "err": ""}
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout, creationflags=_flags(),
                           env=_proxy_env())
        step["rc"] = p.returncode
        step["out"] = (p.stdout or "")[-4000:]
        step["err"] = (p.stderr or "")[-4000:]
    except Exception as e:
        step["rc"] = -1
        step["err"] = str(e)[-4000:]
    return step


def _version(repo, python_exe):
    if repo:
        try:
            text = Path(repo, "codex_autoresume", "__init__.py").read_text(
                encoding="utf-8")
            for line in text.splitlines():
                if line.startswith("__version__"):
                    return line.split("=", 1)[1].strip().strip("\"'")[:40]
        except OSError:
            pass
    try:
        p = subprocess.run(
            [python_exe, "-c",
             "import codex_autoresume as m;print(m.__version__)"],
            capture_output=True, text=True, timeout=60,
            creationflags=_flags())
        if p.returncode == 0:
            return (p.stdout or "").strip()[:40]
    except Exception:
        pass
    return ""


def _write_result(job, result):
    try:
        p = Path(job["result_path"])
        tmp = Path(str(p) + ".tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(str(tmp), str(p))
    except (OSError, KeyError):
        pass


def _finish(job, result):
    logp = job.get("log_path") or ""
    _write_result(job, result)
    _log(logp, "finished: %s %s -> %s%s" % (
        result.get("status"), result.get("from"), result.get("to"),
        (" (%s)" % result["error"]) if result.get("error") else ""))
    try:
        os.unlink(sys.argv[1])
    except OSError:
        pass
    return 0


def main():
    job = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    logp = job.get("log_path") or ""
    result = {"status": "failed", "from": job.get("from"),
              "to": job.get("to"),
              "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
              "steps": [], "error": "", "relaunched": False, "version": ""}
    steps = result["steps"]

    def log(msg):
        _log(logp, msg)

    log("update %s -> %s starting (pid %s)" % (
        job.get("from"), job.get("to"), job.get("pid")))
    try:
        if not _wait_exit(job.get("pid") or 0,
                          float(job.get("wait_sec") or 180)):
            result["error"] = "daemon did not exit; nothing was changed"
            log(result["error"])
            return _finish(job, result)
        ok = True
        kind = job.get("kind")
        if kind == "source":
            repo = job.get("repo") or ""
            st = _run("git pull", ["git", "-C", repo, "pull", "--ff-only"],
                      None, 180)
            steps.append(st)
            ok = st["rc"] == 0
            if not ok:
                result["error"] = "git pull failed"
            if job.get("python_exe"):
                # the daemon imports the checkout directly; refreshing the
                # console entry point is a convenience, not the update
                steps.append(_run(
                    "pip install",
                    [job["python_exe"], "-m", "pip", "install",
                     "--upgrade", "--no-input",
                     "--disable-pip-version-check", repo],
                    repo, 600))
        elif kind == "pip":
            st = _run(
                "pip install",
                [job["python_exe"], "-m", "pip", "install", "--upgrade",
                 "--no-input", "--disable-pip-version-check",
                 job.get("pip_source") or ""],
                None, 900)
            steps.append(st)
            ok = st["rc"] == 0
            if not ok:
                result["error"] = "pip install failed"
        if ok and job.get("codex_bin") and job.get("plugin_spec"):
            # best effort: the daemon runs without the plugin cache
            steps.append(_run(
                "plugin refresh",
                [job["codex_bin"], "plugin", "add", job["plugin_spec"]],
                job.get("state_dir"), 300))
        result["version"] = _version(job.get("repo") or "",
                                     job.get("python_exe") or
                                     sys.executable)
        if ok:
            result["status"] = "ok"
        # write BEFORE starting: the new daemon reads this file at startup
        result["relaunched"] = True
        _write_result(job, result)
        started = False
        if job.get("restart_cmd"):
            st = _run("restart", job["restart_cmd"], None, 60)
            steps.append(st)
            started = st["rc"] == 0
        if not started and job.get("relaunch_cmd"):
            try:
                kwargs = {"cwd": str(Path.home()), "close_fds": True,
                          "stdin": subprocess.DEVNULL,
                          "stdout": subprocess.DEVNULL,
                          "stderr": subprocess.DEVNULL}
                if os.name == "nt":
                    kwargs["creationflags"] = 0x00000008 | 0x08000000
                else:
                    kwargs["start_new_session"] = True
                subprocess.Popen(job["relaunch_cmd"], **kwargs)
                started = True
                steps.append({"name": "relaunch",
                              "cmd": job["relaunch_cmd"], "rc": 0,
                              "out": "", "err": ""})
            except Exception as e:
                steps.append({"name": "relaunch",
                              "cmd": job.get("relaunch_cmd"), "rc": -1,
                              "out": "", "err": str(e)[-4000:]})
        result["relaunched"] = bool(started)
        if not started:
            result["status"] = "failed"
            result["error"] = result["error"] or (
                "could not restart the daemon")
        log("update steps: %s" % ", ".join(
            "%s=%s" % (s.get("name"), s.get("rc")) for s in steps))
    except Exception as e:
        result["error"] = str(e)[:500]
        log("helper failed: %r" % e)
    _finish(job, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''
