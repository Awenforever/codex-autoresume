---
name: autoresume-guard
description: Configure codex-autoresume guarding for conversations - enable/disable auto-continue after usage-limit resets, weekly-window behavior, and per-resume model/reasoning settings. Use when the user asks to "守护当前对话", enable auto-continue, or check quota/guard status.
---

# AutoResume Guard control

You control the codex-autoresume watcher through its MCP tools (preferred)
or its local HTTP API at `http://127.0.0.1:8765` (fallback: `curl`).

## Tools

1. **autoresume_status()** — quota windows, reset credits, and every recent
   conversation with its guard state.
2. **autoresume_set_guard(guard, thread_id? | title_contains?, weekly?, model?, effort?)**
   - `guard: true/false` — enable/disable guarding for that conversation.
   - Identifying the CURRENT conversation: pass `title_contains` with a
     distinctive phrase from this conversation's topic or first user
     message (the status list shows titles). If several match, the tool
     returns the candidates — show them and ask which one.
   - `weekly` — cross-week guarding. `false` (default) means: when the
     weekly quota runs out, guarding STOPS permanently, even after the
     weekly window recovers. `true` waits out the weekly reset and
     continues.
   - `model` / `effort` — applied to every automatic resume of this
     conversation (empty string = inherit the conversation's own settings).
3. **autoresume_pause(paused)** — pause/resume the whole watcher.

## Behavior contract (what the user expects)

- Resuming fires ONLY when: the conversation has no running turn AND the
  goal (if any) is not paused by the user AND the 5-hour window has quota.
- Goal conversations are continued via the app's official goal-continue
  action (`thread/goal/set`), never by injecting messages.
- Ordinary conversations resume via a headless `codex exec resume` with the
  configured model/effort; if the desktop app holds the thread, delivery
  falls back to `codex queue`.
- The daemon may run in dry-run mode (observes, never sends). Tell the user
  when `dry_run` is on in the status output.

## Typical requests

- "为当前对话开启守护" → set_guard(guard=true, title_contains=<distinctive
  phrase from this conversation>)
- "这个任务跨周也要继续" → set_guard(guard=true, weekly=true, ...)
- "用 gpt-6.1-sol high 继续" → set_guard(guard=true, model="gpt-6.1-sol",
  effort="high")
- "看下额度" → autoresume_status()
- "打开守护面板 / 显示控制面板" → open **http://127.0.0.1:8765** in the
  in-app browser (use your browser/navigation tool if available, or tell
  the user to click/open the URL — Codex opens links in the in-app browser
  by default). The panel shows every recent conversation with guard
  toggles, weekly switches, per-resume model/effort inputs and the quota
  windows.

If the tools report the control API is unreachable, the daemon is not
running: tell the user to run `schtasks /Run /TN codex-autoresume`
(Windows) or `codex-autoresume daemon` (any OS).
