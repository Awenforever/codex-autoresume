# Design notes & prior-art survey

Date: 2026-10-24. Target: OpenAI Codex with the sqlite state layout
(codex-cli 0.15x+ / desktop app 26.x). This document records why the tool
exists, what was reused, and the ground-truth observations it is built on.

## 1. Problem

ChatGPT-plan Codex usage is metered in two windows: a **primary 5-hour
window** and a **secondary weekly window**. When a window is exhausted,
running turns fail and — crucially — nothing resumes them automatically:

- goal threads get parked (`thread_goals.status = 'usage_limited'`) and stay
  parked (an open-source Rust patch restoring goal auto-continuation exists,
  confirming this is by design);
- ordinary conversations just stop; the reset time is shown once in the
  error and forgotten.

Users who leave long tasks (or `/goal`-style goals) running overnight lose
the whole window between "limit hit" and "user comes back".

## 2. Ground truth (observed on a live install, codex-cli 0.163.0-alpha.11.2)

State layout under `~/.codex`:

- `thread_history_*.sqlite` → `thread_turns(thread_id, turn_id, status,
  error_json, completed_at, ...)`, `status ∈ {completed, failed,
  interrupted, inProgress}`. Failures carry a structured payload:
  `{"message": "You've hit your usage limit. ... try again at 1:51 PM.",
  "codexErrorInfo": "usageLimitExceeded", ...}` — the message embeds the
  reset time in **machine-local** time; weekly resets use a dated form
  ("try again at Oct 27th, 2026 3:38 PM"). Model capacity errors carry
  `"codexErrorInfo": "serverOverloaded"`.
- `goals_*.sqlite` → `thread_goals(..., status CHECK IN (active, paused,
  blocked, usage_limited, budget_limited, complete))` — first-class signal
  that a goal is parked by quota.
- `state_*.sqlite` → `threads(id, title, model, reasoning_effort,
  updated_at_ms, archived, ...)` — per-thread model settings and recency.

Resume paths:

- `codex exec resume <uuid|thread-name> [PROMPT]` — headless, accepts
  `--last`, `-m/--model`, `-c key=value` (incl.
  `model_reasoning_effort=...`), `-C/--cwd`, `--skip-git-repo-check`.
  Resolves desktop-app threads through the shared app-server (`thread/resume`
  RPC); requires the thread to have a rollout (all non-archived threads do)
  and to not be archived.
- `codex queue --thread <uuid> --message <text>` — official injection into a
  live session (ships since 0.149); keeps the thread's own settings.

Quota truth (via `codex app-server` JSON-RPC over stdio, schema dumped with
`codex app-server generate-json-schema --experimental`):

- `account/rateLimits/read` → `{ordinaryUsageAllowed, rateLimits:
  {primary: {usedPercent, resetsAt, windowDurationMins}, secondary: {...},
  planType, ...}, rateLimitResetCredits: {availableCount, credits:
  [{id, grantedAt, expiresAt, resetType, status}]}}`.
  The schema states clients **must not infer recovery from percentages or
  reset times** — `ordinaryUsageAllowed` is the gate. `resetsAt` is unix
  seconds.
- `account/rateLimitResetCredit/consume` exists for redeeming bonus credits
  (deliberately *not* invoked by this tool; surfaced read-only via `limits`).
- `turn/start` natively accepts `model` + `effort` overrides (future
  app-server-native mode).

Windows unattended hosting: Task Scheduler "at logon" running `pythonw.exe`
needs no admin and survives reboots; pipe/JSONL watchers work while the
desktop is locked (only UI-automation tools need an unlocked interactive
session).

## 3. Prior art (2026-10-24 survey)

| Tool | Mechanism | Missing for our goal |
|---|---|---|
| lutzkind/codex-auto-resume | rollout-JSONL scan, probe-verified `exec resume --last`, `-m/-c` overrides | `--last` only, single session; weekly not scheduled |
| songyb111/codex-auto-resume-windows (39★) | app-server pipe `account/rateLimits/read`, probe, `codex queue`, heavy safety engineering, Windows desktop | desktop-app-centric; no per-resume model override |
| 201224tlx-sketch/...-watchdog | spawns own app-server, resumes at `usedPercent<50`, `--last` | no model config, single session |
| Justin1491/codex-dashboard | undocumented `wham/usage` HTTP API + optional `--last` resume | no license, no model config |
| sybxxx/codex-auto-retry | IPC bounded-backoff retries | no reset-time strategy at all for 5h waits |
| unsnooze (204★) | multi-agent pane scanning, `codex resume` interactive | not headless exec, no overrides |
| zhh198903-ctrl/codex-cc-auto-continue | UIAutomation typing + per-session model/effort | fragile UIA; needs unlocked desktop |
| cc-auto-continue family (Claude) | Stop-hook block / probe loops | Claude-only mechanisms |

No tool combines: multi-thread tracking + goal awareness + weekly scheduling
+ verified recovery + `exec resume` with per-resume model/reasoning +
maintained cross-platform runner. Hence this project (per the "reuse first,
build if insufficient" mandate). Reused mechanisms (with credit): reset-time
parsing, app-server rateLimits gate, probe-verified resume, no-admin logon
task, budget/cap safety patterns.

## 4. Architecture

```
detect (read-only sqlite)  ──▶  planner (pure logic)  ──▶  daemon loop
   failed turns                   weekly policy               │  quota gate
   parked goals                   budgets/cooldowns           ▼ (app-server
   reset-time parse               busy guard            account/rateLimits)
                                                              │
                                                     resume (codex exec resume
                                                             / codex queue)
                                                              │
                                                     state.json + daemon.log
```

- **Pure-stdlib Python ≥3.11** (`sqlite3`, `tomllib`, `subprocess`,
  `concurrent.futures`): the target machine has Python via pixi and nothing
  else guaranteed; zero deps keeps `git clone && python -m ...` working.
- **Detection is polling, not hooks**: Codex's `notify` config currently
  fans out to the desktop app's computer-use helper; hooking it would couple
  us to app internals. Polling read-only sqlite every 30 s is cheap,
  survives app restarts, and sees both app and CLI threads.
- **Reset-time anchor**: a clock-only reset ("1:51 PM") is disambiguated
  against the *failure timestamp* — the true 5h-window reset lies within
  (failure, failure+6h]. Without the anchor (e.g. classifying a resume
  attempt's stderr) a passed clock time means "due now" and the quota gate
  arbitrates.
- **Queue vs exec modes**: `exec` is default (model overrides, headless);
  `queue` delivers into the running desktop app for users who want to watch
  it happen in the UI.
- **Capacity is transient, not a task blockage**: `serverOverloaded`
  ("Selected model is at capacity") parks goals and fails resume attempts,
  but says nothing about the task itself. Both paths keep retrying — goals
  via the official `thread/goal/set` continue, ordinary threads via the
  continue prompt — with exponential backoff (3m → 48m) seeded by
  `general.server_overloaded_cooldown_sec`, rotating
  `general.fallback_models` away from the model that just failed. These
  retries never consume the daily resume budgets nor the failure streak
  that parks a thread.
- **Quota-limit interruptions are first-hand evidence**: a turn that failed
  with `usageLimitExceeded` proves the window was empty at that moment, so
  its reset time overrides a trailing snapshot (which may still read 99%)
  until a clearly newer window is observed.
- **Weekly policy is opt-in** because a multi-day parked watcher silently
  burning a fresh weekly window is a bigger decision than a 5-hour one;
  `weekly_resume = "per-thread"` + `threads.<id>.weekly_resume = true`
  matches "根据用户需要在周额度用光并且重置到账时自动继续".

## 5. Deliberate non-features

- **No `rateLimitResetCredit/consume`** — redeeming paid/bonus credits is an
  account-level financial action; the tool only *displays* credits.
- **No writes to Codex databases** — the app owns that state; a corrupted
  thread_history is catastrophic vs. a missed resume.
- **No auto `codex login`** — auth refresh stays with Codex itself.
