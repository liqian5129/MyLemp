# Agent Events 抽象层

> Mac daemon 与 coding agent 之间的契约文档。
>
> **什么是 agent**:Claude Code(MVP),将来 Cursor / Devin / 其他。
> **设计哲学**:借鉴 [Anthropic Hardware Buddy](https://github.com/anthropics/claude-desktop-buddy) 的 `TamaState` 数据模型,但 daemon 在 Mac 侧做状态派生,**不重做** v0.2.0 设备协议。
> **配套文档**:协议规范 → `dev-protocol.md`(Agent B 渲染层);整体计划 → `dev-implementation-plan.md`。

---

## 0. 整体架构

```
[Claude Code] ──hooks──▶ [Mac Daemon (lelamp/companion/)] ──v0.2.0 USB CDC──▶ [Display + Arm]
                              │
                              ├── SessionSnapshot       (抽象层数据模型)
                              ├── State Derivation      (snapshot → buddy state,纯函数)
                              └── Effect Dispatch       (经 DisplayController / ArmController 下发)
```

核心抽象:**`SessionSnapshot`**。任何 agent 产出的事件都翻译为 snapshot mutation;daemon 由 snapshot 派生 buddy state(sleep/idle/busy/attention/failed/celebrate),通过 v0.2.0 协议下发到设备。

---

## 1. SessionSnapshot 数据模型

```python
@dataclass
class Prompt:
    id: str                       # 唯一 id,与设备 evt:approval 的 id 一致
    tool: str                     # 工具名(Bash / Write / ...)
    command: str                  # 命令文本
    hint: Optional[str] = None    # 补充说明


@dataclass
class Error:
    msg: str                      # 错误摘要
    timestamp: float              # 错误发生时间(单调时钟)


@dataclass
class SessionSnapshot:
    # 生命特征
    last_updated: float                  # 单调时钟时间戳

    # 决定性信号(驱动 state 派生,见 §3)
    running: bool                        # 是否有任务进行中
    prompt: Optional[Prompt] = None      # 非 None 即 attention
    error: Optional[Error] = None        # 非 None 即 failed
    completed_at: Optional[float] = None # 最近 Stop 的时间戳(给 celebrate 1.5s 窗口)

    # 显示信息(下发到 set_text / set_progress)
    current_tool: Optional[str] = None
    elapsed_ms: int = 0
    tokens_today: int = 0
    msg: str = ""                        # 一行摘要 → set_text "subtitle"
    entries: list[str] = []              # 最近 transcript,新→旧,最多 8

    # 多源扩展(MVP 不使用,留接口)
    agent: str = "claude-code"
    session_id: Optional[str] = None
```

字段命名直接对齐 Anthropic 的 `TamaState`(`running` / `prompt` / `tokens_today` / `msg` / `entries` 同名同义),为将来"接 Anthropic 官方 BLE bridge"留兼容空间。

---

## 2. Daemon HTTP API

**地址**:`http://localhost:9000`(可配置)。

| Endpoint | 用途 |
|---|---|
| `POST /event` | 上游(hook 脚本)POST canonical event;daemon 翻译为 snapshot mutation。**部分 event 阻塞**(见下) |
| `POST /update` | 直接 merge partial snapshot(高级用法 / 调试 / 兼容 Anthropic snapshot push) |
| `GET /state` | 返回当前 snapshot + 派生 state(调试) |
| `POST /shutdown` | 优雅关闭:回 idle 后退出 |

### 2.1 POST /event 请求/响应

```json
// 请求
{
  "type": "task_started",
  "agent": "claude-code",
  "session_id": "abc123",
  "data": { /* event-specific */ }
}

// 响应
{ "ok": true, "decision": null }   // 大多数事件
{ "ok": true, "decision": "once" } // 仅 awaiting_approval 事件返回
{ "ok": true, "decision": "deny" }
```

### 2.2 阻塞语义

`awaiting_approval` 事件**阻塞 HTTP 请求**直到设备返回 `evt:approval` 或超时(默认 60s)。daemon 拿到 decision 后再回响应。其他事件立即返回(`{"ok":true}`),daemon 内部异步处理。

`decision` 取值借鉴 Anthropic:`once`(本次允许)/ `deny`(拒绝)。语义比 `yes/no` 更准 —— 强调"只这一次,不是永久授权"。

---

## 3. State 派生(纯函数)

```python
def derive_state(snap: SessionSnapshot, now: float) -> str:
    # 优先级从高到低
    if snap.error and (now - snap.error.timestamp) < ERROR_DISPLAY_TTL:
        return "failed"
    if snap.prompt is not None:
        return "attention"
    if snap.completed_at and (now - snap.completed_at) < CELEBRATE_DURATION:
        return "celebrate"
    if snap.running:
        return "busy"
    if (now - snap.last_updated) > SLEEP_THRESHOLD:
        return "sleep"
    return "idle"
```

| 常量 | 默认 | 含义 |
|---|---|---|
| `ERROR_DISPLAY_TTL` | 30 s | failed 状态展示时长,超时由 daemon 主动切回 idle |
| `CELEBRATE_DURATION` | 1.5 s | 与协议 §3.2 一致(设备自身 1.5s 后自动回 idle) |
| `SLEEP_THRESHOLD` | 5 min | 长时间无任何信号 → sleep |

### 派生触发时机

- 每次 snapshot mutation 后立即派生
- 1 Hz 定时器(覆盖时间窗口过期,如 celebrate→idle 转换)
- 派生结果与 `last_dispatched_state` 不同 → 通过 `DisplayController.set_state()` + `ArmController.set_state()` 下发
- 派生 == `last_dispatched_state` → 仅刷新 `set_text` / `set_progress`(避免重复 set_state 抖动屏)

---

## 4. Claude Code hook → 事件映射

### 4.1 settings.json hooks 配置

```json
{
  "hooks": {
    "SessionStart":     [{ "command": "$LELAMP_HOOKS/session_start.sh" }],
    "UserPromptSubmit": [{ "command": "$LELAMP_HOOKS/user_prompt_submit.sh" }],
    "PreToolUse":       [{ "command": "$LELAMP_HOOKS/pre_tool_use.sh" }],
    "PostToolUse":      [{ "command": "$LELAMP_HOOKS/post_tool_use.sh" }],
    "Notification":     [{ "command": "$LELAMP_HOOKS/notification.sh" }],
    "Stop":             [{ "command": "$LELAMP_HOOKS/stop.sh" }],
    "SessionEnd":       [{ "command": "$LELAMP_HOOKS/session_end.sh" }]
  }
}
```

`$LELAMP_HOOKS` 指向 `lelamp_runtime/lelamp/companion/hooks/claude_code/`。

### 4.2 事件映射表(MVP)

| Hook | canonical event | snapshot mutation | 阻塞? |
|---|---|---|---|
| `SessionStart` | `agent_session_start` | `running=false, last_updated=now` | 否 |
| `UserPromptSubmit` | `task_started` | `running=true, completed_at=None, current_tool=None, elapsed_ms=0` | 否 |
| `PreToolUse` | `tool_started` | `current_tool=<tool>, msg=<summary>` | 否(MVP) |
| `PostToolUse` | `tool_completed` | `current_tool=None, entries.push(...)` | 否 |
| `Notification` | `awaiting_approval` | `prompt={id, tool, command}` | **是**,见 §2.2 |
| `Stop` | `task_completed` | `running=false, completed_at=now` | 否 |
| `SessionEnd` | `agent_session_end` | `running=false` | 否 |

**阻塞 approval 注**:Claude Code 的 `Notification` hook 在 permission 等待时触发;hook 进程阻塞,daemon 阻塞 HTTP 响应直到设备 tap → daemon 回 hook → hook exit 0(允许)/ exit 2(拒绝)→ Claude Code 决定是否继续。

### 4.3 hook 脚本骨架

**最简事件(`stop.sh`)**:
```bash
#!/usr/bin/env bash
curl -s -X POST http://localhost:9000/event \
  -H "Content-Type: application/json" \
  --max-time 2 \
  -d "{\"type\":\"task_completed\",\"agent\":\"claude-code\",\"session_id\":\"$CLAUDE_SESSION_ID\"}" \
  >/dev/null || true
exit 0
```

**阻塞 approval(`notification.sh`)**:
```bash
#!/usr/bin/env bash
RESPONSE=$(curl -s -X POST http://localhost:9000/event \
  -H "Content-Type: application/json" --max-time 65 \
  -d "{
    \"type\":\"awaiting_approval\",
    \"agent\":\"claude-code\",
    \"data\":{\"id\":\"$CLAUDE_HOOK_ID\",\"tool\":\"$CLAUDE_TOOL_NAME\",\"command\":\"$CLAUDE_TOOL_INPUT\"}
  }")
DECISION=$(echo "$RESPONSE" | jq -r '.decision // "deny"')
[ "$DECISION" = "once" ] && exit 0 || exit 2
```

(具体 hook env var 名以 Claude Code 当前文档为准,实施时校对。)

---

## 5. 抽象层扩展(将来加 Cursor / Devin)

加新 agent 时,**只需写一个 translator**:从该 agent 的事件源(Cursor IPC / Devin webhook / Copilot extension API / ...)产生 canonical events,POST 到 daemon 同一 `/event` 端点。

§1 数据模型、§2 API、§3 派生逻辑**全部不变**。新增工作量:
- 写一份 `dev-agent-events-<name>.md`,只填 §4 等价的 hook → event 映射表
- 写对应的 event source 脚本 / 服务

### 多 agent 并存(Demo 2 Foreman)

届时 snapshot 升级为 `sessions: list[SessionSnapshot]`,§3 派生改为 aggregate:
- `any.error` → failed(高亮该 slot)
- `any.prompt` → attention(高亮该 slot)
- `any.running` → busy
- 协议加 `set_sessions [{slot, agent, pct, ...}]`、`evt:tap` 加 `slot` 字段

**MVP 不做**,先单 session。

---

## 6. 不在本契约范围(明确划界)

| 项 | 归属 |
|---|---|
| 通知聚合(Slack / Lark / Calendar)| 独立 macOS notification observer 模块,POST 自己的 event 到 daemon。Demo 3 Don't Look At Me 时再做 |
| Cursor / Copilot / Devin 接入 | v2,见 §5 |
| Anthropic 官方 BLE bridge 兼容 | 数据模型已对齐;加 BLE NUS 是 Agent B 的固件选项,非本契约范围 |
| 设备渲染细节(LVGL 布局 / 角色动画 / 颜色)| Agent B 工作,见 `dev-implementation-plan.md` §2 + §4 |

---

## 7. 版本

| Version | Date | Changes |
|---|---|---|
| 0.1.0 | 2026-04-28 | 初版,Claude Code hooks → daemon → 设备(MVP) |
