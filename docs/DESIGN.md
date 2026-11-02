# Design notes & prior-art survey

Date: 2026-10-24. Updated 2026-11-02 for 0.4.0 (guard lifecycle, resume
prompt, panel order). Target: OpenAI Codex with the sqlite state layout
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
  continue prompt — with exponential backoff capped at 3 minutes and seeded
  by `general.server_overloaded_cooldown_sec`, rotating
  `general.fallback_models` away from the model that just failed. These
  retries never consume the daily resume budgets nor the failure streak
  that parks a thread.
- **Quota-limit interruptions are first-hand evidence**: a turn that failed
  with `usageLimitExceeded` proves the window was empty at that moment, so
  its reset time overrides a trailing snapshot (which may still read 99%)
  until a clearly newer window is observed. The opposite also holds: the
  interruption only owns the thread while it is still the **newest** turn,
  and only for the lifetime of the window it happened in (6h primary / 7d
  weekly). A later completed or interrupted turn means the user moved on,
  and a stop from days ago is finished work — neither may receive an
  injected prompt. State records `last_turn_id` so this can be rechecked at
  send time, not just at scheduling time.
- **Arming after the fact adopts real evidence**: enabling the guard for a
  conversation that was interrupted *before* the guard existed cannot rely
  on a detection event (cold start seeds that history as seen). Instead the
  armed thread adopts its newest failed turn — kind, reset time and original
  message — on the next heartbeat. Arming also clears `fail_streak` /
  `overload_streak` and any wake time, so a previously parked thread is
  eligible again; no interruption kind is ever fabricated.
- **Cold-start seeding is bounded by the persisted watermark**: failures at
  or below the watermark are history (already ingested by an earlier run),
  while anything newer — recorded while the daemon was down — still flows
  through normal detection.
- **Weekly policy is opt-in** because a multi-day parked watcher silently
  burning a fresh weekly window is a bigger decision than a 5-hour one;
  with `weekly_resume = "off"` a weekly-limit interruption disarms the
  thread instead of waiting days for a resume that will not happen, and
  `weekly_resume = "per-thread"` + `threads.<id>.weekly_resume = true`
  matches "根据用户需要在周额度用光并且重置到账时自动继续".

## 5. Deliberate non-features

- **No `rateLimitResetCredit/consume`** — redeeming paid/bonus credits is an
  account-level financial action; the tool only *displays* credits.
- **No writes to Codex databases** — the app owns that state; a corrupted
  thread_history is catastrophic vs. a missed resume.
- **No auto `codex login`** — auth refresh stays with Codex itself.

## 6. Guard lifecycle (0.4.0)

The panel must answer "is this conversation finished, interrupted, or only
between stages?" without reading the model's prose. The phase is a
deterministic fold over machine facts, evaluated every heartbeat:

| Source | Signal | Phase |
|---|---|---|
| thread_goals | `status = complete` | ✓ Completed |
| thread_goals | `usage_limited` / `budget_limited` / `blocked` | Waiting (quota / budget / blocked) |
| thread_turns + state | newest `failed`/`interrupted` with persisted limit evidence | Waiting (quota / capacity) |
| thread_turns + goal | newest turn `completed` while the goal is still active | Stage done |
| resume floor | first completed final-answer turn after a guard resume on an ordinary thread | Resumed |
| none of the above | — | Watching |

Lifecycle overrides sit above the fold: guard off → Off (a persisted
`stopped` phase survives guard-off so the guard's own weekly decision stays
visible), paused → Paused, in-flight → Running.

Design rules:

- **Ordinary conversations are never marked complete.** A turn boundary only
  returns control to the user; declaring "done" would require guessing from
  prose, which is exactly the failure mode being removed. Only the goal
  database's own status is authoritative.
- **Completion is sticky** until a newer turn or re-arm. The app may clear
  the goal row after completion; without stickiness a stale "stage" would
  silently undo the flip the user just saw.
- **Delivery uses a captured floor turn.** Before every resume the daemon
  records the newest turn; the first completed turn with a final answer above
  it is the guard's delivery. `resume_floor_open` covers the Continue path,
  where the floor turn itself completes in place and still counts.
- **The flip is durable and observable.** `phase`, `phase_at`, and
  `phase_turn_id` persist in `state.json`; `goal_complete` / `delivered`
  history entries are appended; `goal_complete` fires `notify_command`.
- **Completion does not switch the guard off.** The user owns that decision;
  the pill's tooltip says the guard is still on and how to stop it.

## 7. Resume prompt (0.4.0)

Community-tested resume instructions converge on the same points: do not say
"continue" into a stale transcript — first read the current files, git state,
and last output; do not recap or redo finished work; keep working until
blocked; stop for destructive/external actions or a real human decision; and
do not trust a self-reported "done". The built-in prompt compresses this to
~90 words with three explicit exits (continue / already done / report the
failing check).

The prompt is not the verification mechanism. Goal completion comes from the
goal database, and ordinary threads can only ever report "delivered"; an
asynchronous harness must check artifacts, not rely on the model's claim.
Per-thread prompt overrides remain for users with special tasks.

## 8. Panel order (0.4.0)

- `ui_pin` and `ui_order` live in `state.json` (they are UI state, not
  config); both survive daemon restarts.
- Sort: pinned first; inside a group, threads never placed by the user float
  above manual ranks by recency; placed threads follow `ui_order`.
- The client sends the complete visible order plus the pinned set to
  `/api/threads/reorder`; the server writes rank and group in one atomic
  state save, so a drop across the divider can pin/unpin and reorder together.
- Affordances are hover-only: a grip and a pin in the card header, a single
  hairline between groups, and an accent insertion bar while dragging.
  Dropping exactly on the boundary keeps the previous pin state, so a near
  miss cannot flip a card between groups.
