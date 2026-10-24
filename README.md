# codex-autoresume

**Unattended auto-continue for OpenAI Codex.** When a Codex conversation or
goal thread is interrupted because the ChatGPT usage limit (5-hour window or
weekly quota) is exhausted, `codex-autoresume` waits for the quota window to
reset and resumes the thread headlessly — with per-resume model and reasoning
settings.

```
codex  ──▶ "You've hit your usage limit ... try again at 1:51 PM."
                                                        │
codex-autoresume  ──  sleeps until 1:51 PM (+grace)  ───┘
        │  verifies quota via `codex app-server` (ordinaryUsageAllowed)
        ▼
codex exec resume <thread> "Continue the task..." -m <model> -c model_reasoning_effort=<effort>
```

Works with both the **Codex desktop app** (Windows/macOS) and the **Codex
CLI** — anything that stores state under `~/.codex` (sqlite layout, Codex
>= 0.15x / app 26.x).

[中文说明](README.zh-CN.md)

## Features

- **Detects** usage-limit interruptions by watching Codex's local state
  databases (read-only): failed turns carrying `codexErrorInfo:
  "usageLimitExceeded"` / `"serverOverloaded"`, and goal threads parked in
  `usage_limited` / `budget_limited` state.
- **Parses the reset time** embedded in the error ("try again at 1:51 PM"
  for the 5-hour window, "Oct 27th, 2026 3:38 PM" for the weekly window) and
  schedules the resume for `reset + grace + jitter`.
- **Verifies recovery** through `codex app-server` → `account/rateLimits/read`
  and gates on the documented `ordinaryUsageAllowed` flag (never resumes into
  a still-exhausted window).
- **Resumes headlessly** via `codex exec resume <thread-id>` (or
  `codex queue` to deliver into the open desktop app), applying **per-resume
  model / reasoning-effort overrides** — globally, per thread, or rotated
  from a fallback chain when the model is at capacity.
- **Weekly quota support (opt-in)**: when the weekly window is the thing that
  ran out, the watcher can wait the multi-day reset out (`weekly_resume`).
- **Reset-credit visibility**: `codex-autoresume limits` prints both windows
  plus any pending rate-limit reset credits (the "bonus" grants).
- **Safety rails**: per-thread and global daily resume budgets, cooldowns,
  busy-thread guard (never injects into a turn that is still running),
  single-instance lock, parking after repeated failures, and full history in
  a JSON state file.
- **Cross-platform**: pure Python 3.11+ stdlib, no dependencies. Installs as
  a Windows scheduled task (logon), macOS LaunchAgent, or systemd user unit —
  no administrator rights needed.

## Quick start

```bash
git clone https://github.com/<you>/codex-autoresume.git
cd codex-autoresume

# run from the checkout...
python -m codex_autoresume doctor        # environment self-check

# ...or install the CLI entry point
pip install .
codex-autoresume doctor
```

Typical unattended setup:

```bash
codex-autoresume install                 # register the logon runner + start it
codex-autoresume watch goal              # auto-continue the usage-limited goal thread
codex-autoresume watch --last            # ...or the most recent conversation
codex-autoresume status                  # what is tracked, next wake, quota snapshot
```

From then on, any thread that dies on a usage limit (while recent enough)
is auto-tracked and resumed when the window resets — no `watch` needed.

## Commands

| Command | What it does |
|---|---|
| `daemon [--once] [-v]` | run the watcher loop (foreground) |
| `status` | tracked threads, next wake times, quota snapshot |
| `watch <uuid\|goal> [--last]` | arm a thread for auto-continue |
| `unwatch <uuid>` | stop auto-continuing a thread |
| `pause` / `resume` | pause / unpause all auto-resumes |
| `resume-now <uuid\|goal> [--model M] [--effort E]` | one resume attempt immediately |
| `goals` | list Codex goal threads and their status |
| `limits` | quota snapshot JSON (both windows + reset credits) |
| `install` / `uninstall` | register/remove the OS-native unattended runner |
| `doctor` | environment self-check |

## Configuration

Config lives at `~/.codex-autoresume/config.toml` (create it from
[`config.example.toml`](config.example.toml); every key is optional). The
most useful bits:

```toml
[resume]
mode = "exec"              # "queue" delivers into the desktop app instead
model = ""                 # "" keeps each thread's own model
reasoning_effort = ""

[general]
weekly_resume = "off"      # off | per-thread | all

[threads."01a05ddb-0483-7ea1-bf01-b298b386bff8"]   # UUID prefix is enough
model = "gpt-6.1-sol"
reasoning_effort = "high"
weekly_resume = true       # also resume this thread after weekly resets
```

## Guard semantics (v0.2)

Guarding is **opt-in per conversation** — nothing is ever resumed unless you
armed that thread:

- Arm it via the **control panel** (`http://127.0.0.1:8765`, served by the
  daemon), the `watch` command, or the bundled **Codex plugin** (composer
  icon → "为当前对话开启自动继续守护" / natural language via its MCP tools).
- A guarded conversation is continued **only when** it has no running turn,
  its goal (if any) is not user-paused, and the 5-hour window actually has
  quota left.
- **Goal threads** are continued via the app's own goal-continue action
  (`thread/goal/set`, origin `user`) — the programmatic equivalent of the
  Continue button on the goal status bar. No message is injected. Optional
  per-resume model/effort are applied first via `thread/settings/update`.
- **Ordinary threads** resume via headless `codex exec resume` (falls back
  to `codex queue` when the desktop app holds the thread; a live turn is
  never injected into).
- **Weekly window**: per-thread `weekly` toggle (default off). Off means
  the weekly quota running out stops guarding **permanently** — even after
  the weekly window recovers. On means the watcher waits out the multi-day
  reset and continues.
- Per-resume **model / reasoning effort** can be set globally, per thread in
  config, or per thread from the control panel / plugin tools.

> Note: the Codex plugin API currently allows composer icons, skills and
> bundled MCP servers, but not arbitrary composer-bottom widgets — the
> bundled MCP tools + panel deliver the same controls through supported
> surfaces.

## How it works

1. **Detect** — every poll it opens `~/.codex/thread_history_*.sqlite` and
   `~/.codex/goals_*.sqlite` in read-only mode and looks for new failed
   turns / parked goals. Error payloads carry both a machine code
   (`usageLimitExceeded`, `serverOverloaded`) and the human reset time.
2. **Plan** — for each interruption it decides: wait until
   `reset + grace + jitter`, respect weekly policy, budget caps, cooldowns,
   and the busy guard.
3. **Gate** — near the wake time it asks `codex app-server`
   (`account/rateLimits/read`) whether ordinary usage is allowed again; the
   protocol schema explicitly says recovery must be judged from
   `ordinaryUsageAllowed`, not from percentages.
4. **Resume** — `codex exec resume <thread> "<continuation prompt>"` with the
   configured `-m` / `-c model_reasoning_effort=...` overrides. If the model
   is at capacity it rotates through `fallback_models`; if the usage limit
   is somehow still active it re-parses the new reset time and reschedules.

Everything the watcher does is recorded in `~/.codex-autoresume/state.json`
and `daemon.log`. The tool **never writes** to Codex's own databases.

## Design decisions & prior art

The problem is well known but not solved end-to-end by existing tools
(survey in [`docs/DESIGN.md`](docs/DESIGN.md)): desktop-app watchdogs exist
(`codex-auto-resume-windows`), `--last`-only CLI resumers exist
(`codex-auto-resume`), and `unsnooze` covers interactive panes. None combine
multi-thread tracking, goal awareness, weekly-window scheduling, verified
quota recovery and per-resume model overrides. This project borrows its
proven mechanisms — reset-time parsing with a failure-time anchor, the
app-server `account/rateLimits/read` gate, probe-verified exec resume, and
the no-admin logon-task runner — and adds the missing pieces.

## Requirements

- Python 3.11+ (stdlib only)
- Codex with the sqlite state layout (`state_*.sqlite`,
  `thread_history_*.sqlite` under `~/.codex`) — Codex app 26.x /
  codex-cli 0.15x+
- Logged-in Codex account (`codex login`)

## License

[MIT](LICENSE)
