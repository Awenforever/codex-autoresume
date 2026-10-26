---
name: autoresume-guard
description: Configure codex-autoresume guarding for conversations - enable/disable auto-continue after usage-limit resets, weekly-window behavior, and per-resume model/reasoning settings. / 配置 codex-autoresume 守护：开启/关闭额度重置后的自动继续、跨周行为，以及每次自动继续的模型与推理档位。Use when the user asks to "守护当前对话", enable auto-continue, or check quota/guard status. / 当用户要求“守护当前对话”、开启自动继续或查看额度/守护状态时使用。
---

# AutoResume Guard control / 守护控制

## How to use (user guide) / 怎么用（用户指南）

Users can say these things in any Codex conversation; call the matching tool.
用户可以在任何 Codex 对话里直接说这些话，你来调用对应工具完成：

- **"Enable guarding for this conversation" / “为当前对话开启守护”** /
  “守护这个任务” → `autoresume_set_guard(guard=true, title_contains=<the most
  distinctive phrase from this conversation's topic> / <本对话主题里最有辨识度的短语>)`
- **"Disable guarding" / “关闭守护”** → same tool with
  `guard=false` / 同上，`guard=false`
- **"This task should also survive the weekly reset" / “这个任务跨周也要继续”** →
  add `weekly=true` / 追加 `weekly=true`; **"Stop when the weekly quota is
  gone" / “周额度用完就算了”** → `weekly=false`
- **"Continue with <model> <effort>" / “用 <模型> <档位> 继续”** → add
  `model` / `effort` / 追加 `model` / `effort`
- **"Show quota / guard status" / “看下额度/守护状态”** → `autoresume_status()`,
  then tell the user the 5h/weekly percentages and each conversation's guard
  state / 把 5h/周窗口百分比和各会话的守护状态讲给用户
- **"Open the guard panel" / “打开守护面板”** → open **http://127.0.0.1:8765**
  in the in-app browser (use a browser/navigation tool if available, otherwise
  tell the user to open the link — Codex opens links in the in-app browser by
  default) / 在应用内浏览器打开 **http://127.0.0.1:8765**（若可用浏览器/导航工具则
  直接打开，否则告诉用户点开该链接，Codex 默认在应用内打开）

If the current conversation cannot be found, show the conversation list
(titles + times) returned by `autoresume_status()` and ask the user to pick one.
找不到当前对话时，把 `autoresume_status()` 返回的会话列表（标题+时间）展示给用户选择。

## Tools / 工具

1. **autoresume_status()** — quota windows, reset credits, and every recent
   conversation with its guard state. / 额度窗口、重置赠送，以及每个近期会话的守护状态。
2. **autoresume_set_guard(guard, thread_id? | title_contains?, weekly?, model?, effort?)**
   - `guard: true/false` — enable/disable guarding for that conversation. /
     开启/关闭该会话的守护。
   - Identifying the CURRENT conversation: pass `title_contains` with a
     distinctive phrase from this conversation's topic or first user message
     (the status list shows titles). If several match, the tool returns the
     candidates — show them and ask which one. / 识别“当前对话”：把本对话主题或
     首条用户消息里最有辨识度的短语传给 `title_contains`（状态列表会显示标题）。
     若匹配到多个，工具会返回候选，展示给用户并询问选哪个。
   - `weekly` — cross-week guarding. `false` (default) means: when the weekly
     quota runs out, guarding STOPS permanently, even after the weekly window
     recovers. `true` waits out the weekly reset and continues. / 跨周守护。
     `false`（默认）表示周额度耗尽即永久停止，即使周窗口恢复也不再继续；
     `true` 表示等周重置后继续。
   - `model` / `effort` — applied to every automatic resume of this
     conversation (empty string = inherit the conversation's own settings). /
     应用于该会话的每次自动继续（空字符串 = 继承会话自身设置）。
3. **autoresume_pause(paused)** — pause/resume the whole watcher. /
   暂停/恢复整个守护进程。

## Behavior contract (what the user expects) / 行为契约（用户预期）

- Resuming fires ONLY when: the conversation has no running turn AND the goal
  (if any) is not paused by the user AND the 5-hour window has quota. /
  仅当：会话没有正在运行的回合、goal（如有）未被用户暂停、且 5 小时窗口有余量时，
  才会触发继续。
- Goal conversations are continued via the app's official goal-continue action
  (`thread/goal/set`), never by injecting messages. / goal 对话通过应用的官方
  goal 继续动作（`thread/goal/set`）恢复，绝不注入消息。
- Ordinary conversations resume via a headless `codex exec resume` with the
  configured model/effort; if the desktop app holds the thread, delivery falls
  back to `codex queue`. / 普通会话通过无头 `codex exec resume` 以配置的
  模型/强度恢复；若桌面应用占用该线程，则回退为 `codex queue` 投递。
- The daemon may run in dry-run mode (observes, never sends). Tell the user
  when `dry_run` is on in the status output. / 守护进程可能运行在 dry-run 模式
  （只观察、不发送）。状态输出里 `dry_run` 为真时要告知用户。

## Typical requests / 常见请求

- "Enable guarding for this conversation" / “为当前对话开启守护” →
  set_guard(guard=true, title_contains=<distinctive phrase / 辨识短语>)
- "This task should also survive the weekly reset" / “这个任务跨周也要继续” →
  set_guard(guard=true, weekly=true, ...)
- "Continue with gpt-6.1-sol high" / “用 gpt-6.1-sol high 继续” →
  set_guard(guard=true, model="gpt-6.1-sol", effort="high")
- "Show quota" / “看下额度” → autoresume_status()
- "Open the guard panel / show the control panel" / “打开守护面板 / 显示控制面板”
  → open **http://127.0.0.1:8765** in the in-app browser (use your
  browser/navigation tool if available, or tell the user to click/open the URL
  — Codex opens links in the in-app browser by default). The panel shows every
  recent conversation with guard toggles, weekly switches, per-resume
  model/effort inputs and the quota windows. / 在应用内浏览器打开
  **http://127.0.0.1:8765**（有浏览器/导航工具就直接打开，否则让用户点开链接；
  Codex 默认在应用内打开）。面板展示每个近期会话的守护开关、跨周开关、
  逐会话模型/强度设置以及额度窗口。

If the tools report the control API is unreachable, the daemon is not running:
tell the user to run `schtasks /Run /TN codex-autoresume` (Windows) or
`codex-autoresume daemon` (any OS). / 若工具报告控制 API 不可达，说明守护进程
未运行：让用户运行 `schtasks /Run /TN codex-autoresume`（Windows）或
`codex-autoresume daemon`（任意系统）。
