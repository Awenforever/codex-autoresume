# codex-autoresume（中文说明）

**Codex 无人值守自动继续器。** 当 Codex 会话或 goal 线程因 ChatGPT 额度耗尽
（5 小时窗口 / 周额度）被中断时，`codex-autoresume` 等待额度窗口重置，然后以
无头模式自动恢复该线程——并支持每次恢复时单独设定 model / reasoning。

解决的问题：

- 5h 额度耗尽 → 额度恢复时**自动继续任务**（支持 goal 式长任务，也支持普通
  被中断的对话）；
- 每次**自动继续时的模型 / reasoning 设定**（全局默认 + 按线程覆盖 + 模型
  过载时的 fallback 链）；
- **周额度**：窗口重置时间带日期（如 "Oct 27th, 2026 3:38 PM"）时可识别为
  周额度耗尽，按配置决定是否等待多日后自动继续；
- `codex-autoresume limits` 可查看两个窗口用量与**重置赠送 credits**。

桌面版 Codex（Windows/macOS）与 Codex CLI 通用——只要状态在 `~/.codex`
（sqlite 布局，Codex ≥ 0.15x / app 26.x）。

完整文档见 [英文 README](README.md)（命令表、配置、工作原理）。

## 快速上手

```bash
git clone <repo> codex-autoresume && cd codex-autoresume
python -m codex_autoresume doctor        # 环境自检

codex-autoresume install                 # 注册登录自启（无需管理员）
codex-autoresume watch goal              # 盯住被额度卡住的 goal 线程
codex-autoresume status                  # 查看跟踪状态与额度快照
```

之后任何（近期活跃的）线程撞上额度耗尽都会被自动跟踪，窗口重置后自动恢复，
无需手动 `watch`。

## 常用配置（~/.codex-autoresume/config.toml）

```toml
[resume]
mode = "exec"              # "queue" 则投递到桌面 app 内
model = ""                 # 留空 = 沿用线程自身模型
reasoning_effort = ""

[general]
weekly_resume = "off"      # off | per-thread | all

[threads."01a05ddb"]       # UUID 前缀即可
model = "gpt-6.1-sol"
reasoning_effort = "high"
weekly_resume = true
```

## 安全设计

- 对 Codex 数据库**只读**（`mode=ro`），绝不写入；
- 恢复前经 `codex app-server` 的 `ordinaryUsageAllowed` 权威门控；
- 线程正有 turn 在跑时绝不注入（busy guard）；
- 每线程/全局每日恢复预算、冷却时间、连续失败自动停用（parking）；
- 单实例锁；全部动作记录在 `state.json` 与 `daemon.log`。

MIT 开源。
