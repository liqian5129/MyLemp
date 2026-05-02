"""SessionSnapshot 数据模型 + 状态派生(纯函数)。

跟 dev-agent-events.md §1 / §3 对应。
设计原则:
  - dataclass 不可变更新(`with_(**changes)` 返回新实例)
  - `derive_state` 纯函数,无副作用,易单元测
  - 字段命名跟 Anthropic Hardware Buddy 的 TamaState 对齐(running / prompt /
    tokens_today / msg / entries),为将来兼容他们 BLE bridge 留余地
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Optional


# 状态派生常量 ───────────────────────────────────────────────────────────────
ERROR_DISPLAY_TTL = 30.0   # failed 状态展示时长(秒);超时 daemon 主动回 idle
CELEBRATE_DURATION = 4.0   # celebrate 持续时长(秒);Mac 派生窗口,给设备动画充分播放
SLEEP_THRESHOLD = 300.0    # 5 分钟无信号 → sleep

# 多 session 聚合 ───────────────────────────────────────────────────────────
# 优先级:数值越大越占主导;winner 由此排序得出
STATE_PRIORITY = {
    "failed":    5,
    "attention": 4,
    "celebrate": 3,
    "busy":      2,
    "idle":      1,
    "sleep":     0,
}
SESSION_IDLE_TTL = 900.0   # 15 分钟无活动的 session 视为 abandoned 自动 GC
                           # (Claude Code 终端被 Ctrl-C / 关掉时 SessionEnd 不一定触发,
                           # 用绝对超时兜底,避免屏角 ghost 点)


@dataclass(frozen=True)
class Prompt:
    """待审批 prompt。非 None 即触发 attention。"""
    id: str
    tool: str
    command: str
    hint: Optional[str] = None


@dataclass(frozen=True)
class Error:
    """错误信息。非 None 且未超时即触发 failed。"""
    msg: str
    timestamp: float


@dataclass(frozen=True)
class SessionSnapshot:
    """coding agent 的当前状态快照。所有 mutation 通过 with_(**changes) 产生新实例。"""

    # 生命特征
    last_updated: float = 0.0  # 单调时钟;mutate 时自动刷新

    # 决定性信号(驱动 state 派生)
    running: bool = False
    prompt: Optional[Prompt] = None
    error: Optional[Error] = None
    completed_at: Optional[float] = None  # 最近 Stop 的时间戳,给 celebrate 1.5s 窗口

    # 显示信息
    current_tool: Optional[str] = None
    elapsed_ms: int = 0
    tokens_today: int = 0
    msg: str = ""
    entries: tuple[str, ...] = ()  # 用 tuple 因为 frozen dataclass

    # 多源扩展(MVP 不使用)
    agent: str = "claude-code"
    session_id: Optional[str] = None

    def with_(self, **changes) -> "SessionSnapshot":
        """merge 字段返回新 snapshot,自动刷新 last_updated。"""
        if "last_updated" not in changes:
            changes["last_updated"] = time.monotonic()
        # entries 如果传 list,转 tuple(保 frozen)
        if "entries" in changes and isinstance(changes["entries"], list):
            changes["entries"] = tuple(changes["entries"])
        return replace(self, **changes)


def derive_state(snap: SessionSnapshot, now: Optional[float] = None) -> str:
    """从 snapshot 派生 buddy state。纯函数。

    优先级:error > prompt > celebrate > busy > sleep > idle
    """
    if now is None:
        now = time.monotonic()

    if snap.error and (now - snap.error.timestamp) < ERROR_DISPLAY_TTL:
        return "failed"
    if snap.prompt is not None:
        return "attention"
    if snap.completed_at and (now - snap.completed_at) < CELEBRATE_DURATION:
        return "celebrate"
    if snap.running:
        return "busy"
    if snap.last_updated > 0 and (now - snap.last_updated) > SLEEP_THRESHOLD:
        return "sleep"
    return "idle"


def initial_snapshot() -> SessionSnapshot:
    """daemon 启动时的初始 snapshot:idle 状态。"""
    return SessionSnapshot(last_updated=time.monotonic())


def aggregate_state(
    sessions: dict[str, SessionSnapshot],
    now: Optional[float] = None,
) -> tuple[str, Optional[str]]:
    """聚合 N 个 session 派生 (winning_state, winning_session_id)。

    规则:
      - 每个 session 各自 derive_state,取**优先级最高**的那个
      - 同优先级时,**last_updated 最新者胜**(最近活跃)
      - 空 dict / 全 None → ("idle", None)

    用于 multi-terminal Claude Code:多个会话并发时,屏只能呈现一个状态,
    选最重要的(failed > attention > celebrate > busy > idle > sleep)。
    """
    if not sessions:
        return ("idle", None)
    if now is None:
        now = time.monotonic()

    best_sid: Optional[str] = None
    best_state: str = "idle"
    best_priority: int = -1
    best_last_updated: float = -1.0

    for sid, snap in sessions.items():
        state = derive_state(snap, now)
        priority = STATE_PRIORITY.get(state, 0)
        if priority > best_priority or (
            priority == best_priority and snap.last_updated > best_last_updated
        ):
            best_sid = sid
            best_state = state
            best_priority = priority
            best_last_updated = snap.last_updated

    return (best_state, best_sid)
