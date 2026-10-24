---
name: autoresume-guard
description: Configure codex-autoresume guarding for conversations - enable/disable auto-continue after usage-limit resets, weekly-window behavior, and per-resume model/reasoning settings. Use when the user asks to "守护当前对话", enable auto-continue, or check quota/guard status.
---

# AutoResume Guard control

## 怎么用（用户指南）

用户可以在任何 Codex 对话里直接说这些话，你来调用对应工具完成：

- **"为当前对话开启守护"** / "守护这个任务" → `autoresume_set_guard(guard=true, title_contains=<本对话主题里最有辨识度的短语>)`
- **"关闭守护"** → 同上，`guard=false`
- **"这个任务跨周也要继续"** → 追加 `weekly=true`；**"周额度用完就算了"** → `weekly=false`
- **"用 <模型> <档位> 继续"** → 追加 `model` / `effort`
- **"看下额度/守护状态"** → `autoresume_status()`，把 5h/周窗口百分比和各会话的守护状态讲给用户
- **"打开守护面板"** → 在应用内浏览器打开 **http://127.0.0.1:8765**（若可用浏览器/导航工具则直接打开，否则告诉用户点开该链接，Codex 默认在应用内打开）

找不到当前对话时，把 `autoresume_status()` 返回的会话列表（标题+时间）展示给用户选择。

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
