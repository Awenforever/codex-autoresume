# AutoResume Guard — usage guide / 使用说明

Guard Codex conversations: stop when the quota runs out, continue
automatically when it resets. Goal threads use the app's official Continue
button; ordinary threads receive one minimal resume prompt. Per-thread model,
reasoning, cross-week and reset-credit settings are supported.

守护 Codex 会话：额度耗尽自动停、额度恢复自动继续（goal 对话走官方“继续”
按钮，普通对话发送一条极简继续指令），支持逐会话的模型 / reasoning / 跨周 /
重置卡设置。

## Open the control panel / 打开控制面板（recommended, no quota cost / 推荐，不消耗额度）

In the Codex sidebar browser (in-app browser), open:

在 Codex 侧边栏（应用内浏览器）输入：

```
http://127.0.0.1:8765
```

You can also double-click the desktop shortcut **CodexGuardPanel**. The panel
shows quota rings (with drain ETAs estimated from the last 1h / 5h rates),
reset-credit counts, and every recent conversation with its guard / weekly /
credit switches and model / reasoning / resume-prompt settings.

也可以双击桌面快捷方式 **CodexGuardPanel**。面板里有额度环形仪表（含按
近 1h / 5h 消耗速度预估的耗尽时间）、重置赠送计数、每个近期会话的
守护/跨周/重置卡开关和模型/Reasoning/继续指令设置。

> Asking Codex to call this plugin from a conversation works too, but the
> conversation itself costs quota — use the panel for day-to-day control.
>
> 通过对话让 Codex 调用本插件同样可以，但对话本身会消耗额度——日常
> 管理建议直接用面板。

## Switch meanings / 开关含义

| Switch / 开关 | English | 中文 |
|---|---|---|
| Auto-resume / 守护 | Automatically continue this thread after the 5h quota runs out. | 打开则表示 5h 额度耗尽后自动继续任务。 |
| Weekly / 跨周 | With guarding on, also continue after the weekly quota resets. Off: the thread stops permanently once the weekly quota is exhausted. | 打开则表示守护开启时周额度重置后也自动继续任务（关闭：周额度耗尽即永久停止，即使周额度恢复也不再继续）。 |
| Credits / 重置卡 | When the weekly quota runs out and the account has official reset credits, spend one to continue. | 周额度耗尽且账户有官方重置卡时，自动消耗一张以继续任务。 |
| Model / Reasoning / 模型 / Reasoning | Model and reasoning effort used for every automatic resume (empty = inherit the thread's own settings). | 每次自动继续所用的模型与推理档位（留空 = 继承会话自身设置）。 |
| Resume prompt / 继续指令 | Prompt sent when an ordinary (non-goal) thread resumes (empty = default minimal prompt; “Reset” restores it). | 非 goal 会话自动继续时发送的提示词（留空 = 默认极简指令；“恢复默认”一键还原）。 |

## When guarding fires / 守护触发条件

Only when the thread is not running, the goal (if any) is not paused by you,
the 5h window has quota left, and the thread was interrupted by a usage limit.
There is an extra 3-minute tolerance wait after the reset moment.

Goal conversations are restored via `thread/goal/set` (the programmatic
equivalent of the status-bar **Continue** button); Guard never injects chat
messages.

仅当「没有正在运行的任务 · 未被你暂停（goal）· 5h 额度有余量」且该会话
曾被额度打断时才会自动继续；重置时刻后额外等待 3 分钟容错。goal 对话通过
`thread/goal/set`（即状态栏“继续”按钮的程序化等价）恢复，绝不注入消息。

## Using it from a conversation / 对话内用法（costs a little quota / 会消耗少量额度）

In any Codex conversation:

在任意 Codex 对话里说：

- “Enable guarding for this conversation” / “为当前对话开启守护”
- “This task should also survive the weekly reset” / “这个任务跨周也要继续”
- “Continue with gpt-6.1-sol high” / “用 gpt-6.1-sol high 继续”
- “Show my quota” / “看下额度”
- “Open the guard panel” / “打开守护面板”

## Process management / 进程管理

- Panel unreachable = the daemon is not running: `schtasks /Run /TN codex-autoresume`
  (Windows) or `codex-autoresume daemon` in any terminal.
- 面板连不上 = 守护进程未运行：`schtasks /Run /TN codex-autoresume`（Windows）
  或任意终端 `codex-autoresume daemon`。

## Full documentation / 完整文档

See the repository [README.md](https://github.com/Awenforever/codex-autoresume)
(bilingual).

完整文档见仓库
[README.md](https://github.com/Awenforever/codex-autoresume)（中英双语）。
