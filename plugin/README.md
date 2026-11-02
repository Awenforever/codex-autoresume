# AutoResume Guard — usage guide / 使用说明

Guard Codex conversations: stop when the quota runs out, continue
automatically when it resets. Goal threads use the app's official Continue
button; ordinary threads receive one safety-first resume prompt (re-verify
state, no recap/redo, three exits: continue / already done / needs a human).
Per-thread model, reasoning, cross-week and reset-credit settings are
supported.

守护 Codex 会话：额度耗尽自动停、额度恢复自动继续（goal 对话走官方“继续”
按钮，普通对话发送一条安全优先的继续指令：先核验真实状态、不复述不重做、
三个出口——继续 / 已完成 / 需要人），支持逐会话的模型 / reasoning / 跨周 /
重置卡设置。

## Open the control panel / 打开控制面板（recommended, no quota cost / 推荐，不消耗额度）

In the Codex sidebar browser (in-app browser), open:

在 Codex 侧边栏（应用内浏览器）输入：

```
http://127.0.0.1:8765
```

The panel shows quota rings (drain ETAs simulated from the measured 5h burn
rate and the pooled within-block weekly ratio, gated by the 5h block and the
weekly reset — “Weekly resets first” when the weekly window would reset before
running out), reset-credit counts, and every recent conversation with its
guard / weekly / credit switches and model / reasoning / resume-prompt
settings.

面板里有额度环形仪表（耗尽
时间按实测 5h 燃烧速度与跨块汇总的块内周额度比例推算，受 5h 分块与周
重置约束——周额度会先重置时显示“周额度先重置”）、重置赠送计数、每个
近期会话的守护/跨周/重置卡开关和模型/Reasoning/继续指令设置。

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
| Resume prompt / 继续指令 | Prompt sent when an ordinary (non-goal) thread resumes (empty = the built-in safety-first prompt; “Reset” restores it). | 非 goal 会话自动继续时发送的提示词（留空 = 内置的安全优先指令；“恢复默认”一键还原）。 |

## When guarding fires / 守护触发条件

Only when the thread is not running, the goal (if any) is not paused by you,
the 5h window has quota left, and the thread was interrupted by a usage limit.
There is an extra 3-minute tolerance wait after the reset moment. The
interruption must still be the conversation's **newest** turn: once you have
continued or finished it yourself, Guard drops the stale evidence instead of
prompting into the thread. Enabling guarding *after* an interruption works
too — Guard adopts the newest failed turn (kind, reset time, message) instead
of guessing a window.

Goal conversations are restored via `thread/goal/set` (the programmatic
equivalent of the status-bar **Continue** button); Guard never injects chat
messages.

仅当「没有正在运行的任务 · 未被你暂停（goal）· 5h 额度有余量」且该会话
曾被额度打断时才会自动继续；重置时刻后额外等待 3 分钟容错。该中断还必须
仍是会话的**最新回合**：一旦你已自行继续或结束对话，守护会丢弃旧证据，
不会往对话里注入 prompt。中断之后才开启守护同样有效——守护会读取最新的
失败回合（类型、重置时间、原始消息），不靠猜窗口。goal 对话通过
`thread/goal/set`（即状态栏“继续”按钮的程序化等价）恢复，绝不注入消息。

## Card states / 卡片状态

The badge on each guarded thread comes from machine facts — the goal database
status and the turn database — not from the assistant's prose. So “done”,
“interrupted” and “finished one stage” are different states, and you can see
them flip. A completed guard stays on: the user decides whether to leave it or
switch it off.

每张卡片的徽标来自机器事实（goal 数据库状态 + 回合数据库），不靠模型自述，
因此“已完成”“被中断”“只是完成了一个阶段”是不同状态，且翻转可见。会话
完成后守护不会自动关闭，由用户决定是留着还是关掉。

| State / 状态 | Meaning / 含义 |
|---|---|
| Watching / 观察中 | Guard is on; nothing interrupted yet. / 守护已开，尚无中断。 |
| Running / 进行中 | A turn is currently running. / 当前有回合在运行。 |
| Waiting for quota / 等待额度 | Interrupted by a usage limit; waiting for the 5h reset. / 因额度中断，等待 5h 重置。 |
| Waiting for capacity · budget · Blocked / 等待容量 · 预算 · 受阻 | Interrupted for another reason; Guard retries as appropriate. / 因其他原因停住，守护会按情况重试。 |
| Stage done / 阶段完成 | A finished turn while the goal is still active — a milestone, not the end. / goal 仍 active 时的完成回合——是里程碑，不是结束。 |
| Resumed / 已续跑 | Guard delivered a resume turn; its result is pending. / 守护已投递续跑回合，结果待定。 |
| ✓ Completed / 已完成 | The goal database says complete, or the thread was completed after Guard resumed it. Guard stays on. / goal 数据库为 complete，或守护续跑后该会话完成。守护保持开启。 |
| Paused · Stopped / 暂停 · 已停止 | The goal is paused, or the user stopped the turn. / goal 被暂停，或用户停止了回合。 |

Ordinary (goal-less) threads never show “Completed”: Guard can only know that
its own resume was delivered, so they stop at “Resumed”, and you close the
conversation. / 普通（无 goal）会话不会显示“已完成”：守护只知道自己投递的
续跑已生效，所以停在“已续跑”，由你收尾。

## Pin and reorder / 置顶与排序

Hover a card: a grip and a pin appear on the right of its title, nothing else
changes. Drag the grip to reorder within a section, or drop the card above the
section divider to pin it (drop it below the divider to unpin). While you drag,
the card follows the pointer and the other cards slide aside to open its slot;
it settles into place on drop, and the pin icon pops when a card changes
groups. A card you place by hand keeps its slot for one hour; the rest of its
group still auto-sorts by recent activity. A conversation created while the
panel is open starts at the top of its group. Pinned cards float first; the
arrangement is stored locally and survives restarts.

悬停卡片：标题右侧出现拖动柄与置顶按钮，其余不变。拖动柄可在分组内排序，
把卡片拖过分隔线上方即置顶（拖到下方即取消置顶）。拖动时卡片跟随指针，
其余卡片会滑动让出位置，松手后卡片动画落位，切换分组时置顶图标弹跳。
手动拖过的卡片 1 小时内保持位置，同级其余卡片仍按最近活跃时间自动排序；
新建对话创建时排在所在分组第一位。置顶卡片排在最前，顺序保存在本地，
重启后保持。

## Using it from a conversation / 对话内用法（costs a little quota / 会消耗少量额度）

In any Codex conversation:

在任意 Codex 对话里说：

- “Enable guarding for this conversation” / “为当前对话开启守护”
- “This task should also survive the weekly reset” / “这个任务跨周也要继续”
- “Continue with gpt-6.1-sol high” / “用 gpt-6.1-sol high 继续”
- “Show my quota” / “看下额度”
- “Open the guard panel” / “打开守护面板”

## Process management / 进程管理

- Panel unreachable = the daemon is not running. Run `codex-autoresume
  doctor` first, then `codex-autoresume install` (this also clears a legacy
  Windows logon task's 72-hour execution cap) or `schtasks /Run /TN
  codex-autoresume` (Windows).
- 面板连不上 = 守护进程未运行：先跑 `codex-autoresume doctor`，再执行
  `codex-autoresume install`（同时清除旧版 Windows 登录任务默认的 72 小时
  执行上限），或 `schtasks /Run /TN codex-autoresume`（Windows）。
- If `codex-autoresume` itself is missing, only the plugin is installed:
  `pip install .` in the repository, then `codex-autoresume install`. /
  若 `codex-autoresume` 命令不存在，说明只装了插件：在仓库里 `pip install .`，
  再 `codex-autoresume install`。

## Full documentation / 完整文档

See the repository [README.md](https://github.com/Awenforever/codex-autoresume)
(bilingual).

完整文档见仓库
[README.md](https://github.com/Awenforever/codex-autoresume)（中英双语）。
