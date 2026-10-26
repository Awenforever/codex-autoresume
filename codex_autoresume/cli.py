"""Command line interface."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

from . import __version__
from .codexenv import CodexEnvError, CodexState, codex_version, find_codex_bin
from .config import (ConfigError, default_config_path, load_config,
                     resolve_codex_home, resolve_state_dir)


def _load(args):
    return load_config(getattr(args, "config", None))


def _state_dir_from(args, cfg):
    d = getattr(args, "state_dir", None)
    return d or resolve_state_dir(cfg)


# ---------------------------------------------------------------- commands

def cmd_daemon(args) -> int:
    from .daemon import main_daemon
    if getattr(args, "state_dir", None):
        os.environ["CODEX_AUTORESUME_HOME"] = args.state_dir
    return main_daemon(once=getattr(args, "once", False),
                       config_path=getattr(args, "config", None),
                       verbose=getattr(args, "verbose", False))


def cmd_status(args) -> int:
    cfg = _load(args)
    state_dir = _state_dir_from(args, cfg)
    from .state import load_state
    state = load_state(os.path.join(state_dir, "state.json"))
    print("state dir : %s" % state_dir)
    print("paused    : %s" % state.paused)
    print("threads:")
    now = datetime.now().astimezone()
    for tid, t in sorted(state.threads.items(),
                         key=lambda kv: str(kv[1].last_resume_iso or "")):
        if not (t.tracked or t.manual or t.history):
            continue
        last = t.last_resume_iso or "-"
        wake = t.next_wake_iso or "-"
        print("  %s  tracked=%-5s goal=%-5s kind=%-16s resumes=%-3d "
              "streak=%-2d last=%s next_wake=%s" %
              (tid, t.tracked, t.is_goal, t.last_kind or "-",
               t.total_resumes, t.fail_streak, last[:19], wake[:19]))
    # quota snapshot
    try:
        from .ratelimits import fetch_quota_snapshot
        snap = fetch_quota_snapshot(find_codex_bin(cfg["general"]["codex_bin"]))
        print("quota     : ordinaryUsageAllowed=%s primary=%s%% secondary=%s%% "
              "resetCredits=%d" % (snap.ordinary_usage_allowed,
                                   snap.primary.used_percent,
                                   snap.secondary.used_percent,
                                   len(snap.reset_credits)))
    except Exception as e:
        print("quota     : unavailable (%s)" % e)
    return 0


def cmd_watch(args) -> int:
    cfg = _load(args)
    codex_home = resolve_codex_home(cfg)
    state = CodexState(codex_home)
    if args.thread == "goal":
        goals = [g for g in state.goals()
                 if g.get("status") in ("usage_limited", "budget_limited")]
        if not goals:
            print("no usage-limited goal found")
            return 1
        if len(goals) > 1 and not args.all:
            for g in goals:
                print("goal thread:", g["thread_id"],
                      "(%.80s)" % g.get("objective", ""))
            print("use --all to watch all of them")
            return 1
        ids = [g["thread_id"] for g in goals] if args.all else [goals[0]["thread_id"]]
    elif args.last:
        tid = state.latest_thread_id()
        if not tid:
            print("no threads found")
            return 1
        ids = [tid]
    else:
        ids = [args.thread]

    from .state import load_state, save_state, ThreadState
    state_dir = _state_dir_from(args, cfg)
    st = load_state(os.path.join(state_dir, "state.json"))
    applied = []
    for tid in ids:
        info = state.thread(tid)
        if info is None:
            print("thread not found: %s" % tid)
            continue
        ts = st.thread(info.id)
        ts.manual = True
        ts.tracked = True
        ts.is_goal = bool(args.goal or ts.is_goal)
        if not ts.last_kind:
            ts.last_kind = "usage_primary"   # assume interrupted by quota
        applied.append(info.id)
        title = (info.name or info.title or "").replace("\n", " ")[:50]
        print("watching %s  %s" % (info.id, title))
    if applied:
        save_state(st)
        return 0
    return 1


def cmd_unwatch(args) -> int:
    cfg = _load(args)
    from .state import load_state, save_state
    state_dir = _state_dir_from(args, cfg)
    st = load_state(os.path.join(state_dir, "state.json"))
    ts = st.threads.get(args.thread)
    if ts is None:
        print("thread not tracked: %s" % args.thread)
        return 1
    ts.tracked = False
    ts.manual = False
    ts.next_wake_iso = None
    save_state(st)
    print("unwatched %s" % args.thread)
    return 0


def cmd_pause(args) -> int:
    cfg = _load(args)
    from .state import load_state, save_state
    state_dir = _state_dir_from(args, cfg)
    st = load_state(os.path.join(state_dir, "state.json"))
    st.paused = True
    save_state(st)
    print("paused (daemon will observe only; resume with `resume`)")
    return 0


def cmd_resume_pause(args) -> int:
    cfg = _load(args)
    from .state import load_state, save_state
    state_dir = _state_dir_from(args, cfg)
    st = load_state(os.path.join(state_dir, "state.json"))
    st.paused = False
    save_state(st)
    print("resumed")
    return 0


def cmd_resume_now(args) -> int:
    """Immediately run one resume attempt (bypasses wake scheduling)."""
    cfg = _load(args)
    from .planner import merge_settings
    from .config import thread_config
    from .resume import build_exec_resume_command, build_queue_command, run_command
    codex_home = resolve_codex_home(cfg)
    tid = args.thread
    if tid in ("goal", ""):
        goals = [g for g in CodexState(codex_home).goals()
                 if g.get("status") in ("usage_limited", "budget_limited")]
        if not goals:
            print("no usage-limited goal thread")
            return 1
        tid = goals[0]["thread_id"]
    tc = thread_config(cfg, tid)
    settings = merge_settings(cfg, tc)
    cstate = CodexState(codex_home)
    is_goal = any(g["thread_id"] == tid for g in cstate.goals())
    prompt = (settings.prompt_goal if is_goal else settings.prompt_generic) \
        if not args.prompt else args.prompt
    # inherit the thread's recorded model/effort unless overridden (exec
    # resume otherwise falls back to config.toml defaults)
    info = cstate.thread(tid)
    model = args.model or settings.model or (info.model if info else "") or ""
    effort = (args.effort or settings.reasoning_effort
              or (info.reasoning_effort if info else "") or "")
    codex_bin = find_codex_bin(cfg["general"]["codex_bin"])
    if settings.mode == "queue" and not args.model:
        argv = build_queue_command(codex_bin, tid, prompt)
    else:
        argv = build_exec_resume_command(
            codex_bin, tid, prompt, model=model, reasoning_effort=effort,
            extra_config=list(cfg["resume"]["extra_config"]),
            codex_extra_args=list(cfg["resume"]["codex_extra_args"]))
    print("running: %s" % " ".join(argv))
    out = run_command(argv, timeout=float(cfg["general"]["resume_timeout_sec"]))
    print("outcome : %s (exit=%s, %.0fs)" % (out.kind, out.exit_code,
                                             out.duration_sec))
    if out.reset_at_iso:
        print("reset at: %s" % out.reset_at_iso)
    if out.stdout_tail:
        print("---- stdout tail ----\n%s" % out.stdout_tail[-1500:])
    if out.stderr_tail:
        print("---- stderr tail ----\n%s" % out.stderr_tail[-1500:])
    return 0 if out.ok else 1


def cmd_goals(args) -> int:
    cfg = _load(args)
    state = CodexState(resolve_codex_home(cfg))
    goals = state.goals()
    if not goals:
        print("no goals")
        return 0
    for g in goals:
        print("%s  status=%-14s tokens=%-10d %.100s" %
              (g["thread_id"], g["status"], g["tokens_used"],
               str(g.get("objective") or "")))
    return 0


def cmd_limits(args) -> int:
    cfg = _load(args)
    from .ratelimits import fetch_quota_snapshot
    try:
        snap = fetch_quota_snapshot(find_codex_bin(cfg["general"]["codex_bin"]))
    except Exception as e:
        print("quota snapshot unavailable: %s" % e, file=sys.stderr)
        return 1
    print(json.dumps({
        "ordinaryUsageAllowed": snap.ordinary_usage_allowed,
        "primary": {"usedPercent": snap.primary.used_percent,
                    "resetsAt": snap.primary.resets_at,
                    "windowDurationMins": snap.primary.window_duration_mins},
        "secondary": {"usedPercent": snap.secondary.used_percent,
                      "resetsAt": snap.secondary.resets_at,
                      "windowDurationMins": snap.secondary.window_duration_mins},
        "planType": snap.plan_type,
        "rateLimitReachedType": snap.rate_limit_reached_type,
        "resetCredits": [{"id": c.id, "grantedAt": c.granted_at,
                          "expiresAt": c.expires_at, "status": c.status,
                          "title": c.title} for c in snap.reset_credits],
    }, ensure_ascii=False, indent=2))
    return 0


def cmd_install(args) -> int:
    from .installer import install, status as inst_status
    print(install(_state_dir_from(args, _load(args)) if not args.no_state_dir else None))
    print("runner status: %s" % inst_status())
    return 0


def cmd_uninstall(args) -> int:
    from .installer import uninstall
    print(uninstall())
    return 0


def cmd_doctor(args) -> int:
    cfg = _load(args)
    ok = True
    print("codex-autoresume %s doctor" % __version__)
    try:
        codex_bin = find_codex_bin(cfg["general"]["codex_bin"])
        print("[ok] codex binary : %s (%s)" % (codex_bin, codex_version(codex_bin)))
    except CodexEnvError as e:
        print("[FAIL] codex binary: %s" % e); ok = False
    codex_home = resolve_codex_home(cfg)
    print("      codex home   : %s" % codex_home)
    try:
        st = CodexState(codex_home)
        print("[ok] state db     : %s" % st.state_db)
        print("[ok] history db   : %s" % st.history_db)
        goals = st.goals()
        print("[ok] goals db     : %s (%d goals)" %
              (st.goals_db or "none", len(goals)))
        busy = st.busy_threads()
        print("[ok] busy threads : %d" % len(busy))
    except CodexEnvError as e:
        print("[FAIL] state dbs  : %s" % e); ok = False
    if cfg["limits_probe"]["use_app_server"]:
        try:
            from .ratelimits import fetch_quota_snapshot
            snap = fetch_quota_snapshot(codex_bin)
            print("[ok] app-server   : primary=%s%% secondary=%s%% allowed=%s" %
                  (snap.primary.used_percent, snap.secondary.used_percent,
                   snap.ordinary_usage_allowed))
        except Exception as e:
            print("[warn] app-server : %s (quota gating falls back to parsed "
                  "reset times)" % e)
    print("      config      : %s" % cfg.get("_config_path"))
    print("      state dir   : %s" % _state_dir_from(args, cfg))
    print("[ok] all checks passed" if ok else "[FAIL] some checks failed")
    return 0 if ok else 1


# ------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="codex-autoresume",
        description="Unattended auto-continue for OpenAI Codex after usage-limit resets. / 额度重置后无人值守地自动继续 Codex 对话。")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-c", "--config", help="config TOML path / 配置文件 TOML 路径")
    p.add_argument("--state-dir", help="state directory override / 覆盖状态目录")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", help="config TOML path / 配置文件 TOML 路径")
    common.add_argument("--state-dir", help="state directory override / 覆盖状态目录")
    sub = p.add_subparsers(dest="cmd")

    d = sub.add_parser("daemon", help="run the watcher (foreground) / 运行守护循环（前台）",
                       parents=[common])
    d.add_argument("--once", action="store_true", help="one tick then exit / 只执行一轮后退出")
    d.add_argument("-v", "--verbose", action="store_true",
                   help="verbose logging / 输出详细日志")
    d.set_defaults(fn=cmd_daemon)

    s = sub.add_parser("status", help="show tracked threads and quota / 显示已守护线程与额度",
                       parents=[common])
    s.set_defaults(fn=cmd_status)

    w = sub.add_parser("watch", help="arm a thread for auto-continue / 为一个线程开启自动继续",
                       parents=[common])
    w.add_argument("thread", nargs="?", default="",
                   help="thread UUID, 'goal', or omit with --last / 线程 UUID、'goal'，或配合 --last 省略")
    w.add_argument("--last", action="store_true", help="most recent thread / 最近的线程")
    w.add_argument("--all", action="store_true", help="with 'goal': all goals / 配合 'goal'：所有 goal")
    w.add_argument("--goal", action="store_true", help="treat as goal thread / 按 goal 线程处理")
    w.set_defaults(fn=cmd_watch)

    u = sub.add_parser("unwatch", help="stop auto-continuing a thread / 停止守护一个线程", parents=[common])
    u.add_argument("thread")
    u.set_defaults(fn=cmd_unwatch)

    pp = sub.add_parser("pause", help="pause auto-resumes / 暂停全部自动继续", parents=[common])
    pp.set_defaults(fn=cmd_pause)
    pr = sub.add_parser("resume", help="unpause the watcher / 恢复守护进程", parents=[common])
    pr.set_defaults(fn=cmd_resume_pause)

    rn = sub.add_parser("resume-now", help="run one resume attempt now / 立即尝试一次继续", parents=[common])
    rn.add_argument("thread", nargs="?", default="goal",
                    help="thread UUID or 'goal' / 线程 UUID 或 'goal'")
    rn.add_argument("--prompt", help="resume prompt / 继续指令")
    rn.add_argument("--model", help="model for this resume / 本次继续使用的模型")
    rn.add_argument("--effort", help="reasoning effort / 推理强度")
    rn.set_defaults(fn=cmd_resume_now)

    g = sub.add_parser("goals", help="list Codex goals / 列出 Codex goal", parents=[common])
    g.set_defaults(fn=cmd_goals)

    l = sub.add_parser("limits", help="print quota snapshot (JSON) / 输出额度快照（JSON）", parents=[common])
    l.set_defaults(fn=cmd_limits)

    i = sub.add_parser("install", help="install the unattended runner / 安装无人值守运行器", parents=[common])
    i.add_argument("--no-state-dir", action="store_true",
                   help="do not bake the state dir into the runner command / 不把状态目录写入运行命令")
    i.set_defaults(fn=cmd_install)
    un = sub.add_parser("uninstall", help="remove the unattended runner / 移除无人值守运行器", parents=[common])
    un.set_defaults(fn=cmd_uninstall)

    doc = sub.add_parser("doctor", help="environment self-check / 环境自检", parents=[common])
    doc.set_defaults(fn=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 2
    try:
        return args.fn(args) or 0
    except ConfigError as e:
        print("config error: %s" % e, file=sys.stderr)
        return 2
    except CodexEnvError as e:
        print("environment error: %s" % e, file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
