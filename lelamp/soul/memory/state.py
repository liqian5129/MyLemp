"""WorldState — SoulAgent 的只读视图层。

设计原则（来自 dev-memory-system.md Phase 1）：
- 不拥有任何状态。所有字段都是 @property，read-through 到 SoulAgent 的实例字段。
- 视图层只读 + read-through 避免双份状态在异步系统里因为 sync 不及时而打架。
- 字段硬长度上限（防膨胀），不写运行时优先级裁剪。
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from lelamp.soul.soul_agent import SoulAgent


# ── 字段硬长度上限 ────────────────────────────────────────────────────────
_FIELD_MAX_LEN = {
    "body_status": 80,
    "last_audio_env": 40,
    "last_user_activity": 40,
    "last_user_speaker": 20,
}


def _truncate(value: Optional[str], max_len: int) -> Optional[str]:
    if value is None:
        return None
    if len(value) <= max_len:
        return value
    return value[: max_len - 1] + "…"


def _phase_of(hour: int) -> str:
    """把 24 小时映射到口语化的时段标签。

    与原 _process_event 中 TimerTick 时段判断保持一致。
    """
    if hour < 7:
        return "凌晨"
    if hour < 9:
        return "早上"
    if hour < 12:
        return "上午"
    if hour < 14:
        return "中午"
    if hour < 18:
        return "下午"
    if hour < 21:
        return "晚上"
    return "深夜"


class WorldState:
    """SoulAgent 的只读视图层 —— 不拥有任何状态。

    所有字段都是 @property，read-through 到 SoulAgent 的实例字段。
    SoulAgent 是事实的唯一所有者；WorldState 只是它们的对外观察接口。
    """

    def __init__(self, agent: "SoulAgent"):
        self._agent = agent

    # ── 时间 ────────────────────────────────────────────────────────────
    @property
    def now(self) -> datetime:
        return datetime.now()

    @property
    def time_phase(self) -> str:
        return _phase_of(self.now.hour)

    # ── 在场感知 ────────────────────────────────────────────────────────
    @property
    def last_user_speech_at(self) -> Optional[float]:
        ts = getattr(self._agent, "_last_activity", 0.0)
        return ts if ts else None

    @property
    def last_user_speaker(self) -> Optional[str]:
        return getattr(self._agent, "_last_recognized_speaker", None)

    # ── 自我状态 ────────────────────────────────────────────────────────
    @property
    def last_self_speech_at(self) -> Optional[float]:
        return getattr(self._agent, "_last_self_speech_at", None)

    @property
    def self_speech_count_today(self) -> int:
        return getattr(self._agent, "_self_speech_count_today", 0)

    @property
    def body_status(self) -> str:
        try:
            return self._agent._motion_agent.get_status_str()
        except Exception:
            return ""

    # ── 视觉 / 环境 ─────────────────────────────────────────────────────
    @property
    def ticks_since_photo(self) -> int:
        return getattr(self._agent, "_ticks_since_photo", 0)

    @property
    def last_audio_env(self) -> Optional[str]:
        v = getattr(self._agent, "_last_env_audio_env", "") or ""
        return v or None

    @property
    def last_user_activity(self) -> Optional[str]:
        v = getattr(self._agent, "_last_env_user_activity", "") or ""
        return v or None

    @property
    def last_env_event_at(self) -> Optional[float]:
        ts = getattr(self._agent, "_last_env_trigger_time", 0.0)
        return ts if ts else None

    # ── 触发上下文 ──────────────────────────────────────────────────────
    @property
    def current_trigger(self) -> str:
        return getattr(self._agent, "_event_source", None) or "user"

    # ── 渲染入口 ────────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        """返回扁平字段字典，供 render 使用。

        - None 字段会在 render 时省略
        - 字符串字段按 _FIELD_MAX_LEN 截断
        """
        snap: dict[str, Any] = {
            "now": self.now,
            "time_phase": self.time_phase,
            "last_user_speech_at": self.last_user_speech_at,
            "last_user_speaker": _truncate(self.last_user_speaker, _FIELD_MAX_LEN["last_user_speaker"]),
            "last_self_speech_at": self.last_self_speech_at,
            "self_speech_count_today": self.self_speech_count_today,
            "body_status": _truncate(self.body_status or None, _FIELD_MAX_LEN["body_status"]),
            "ticks_since_photo": self.ticks_since_photo,
            "last_audio_env": _truncate(self.last_audio_env, _FIELD_MAX_LEN["last_audio_env"]),
            "last_user_activity": _truncate(self.last_user_activity, _FIELD_MAX_LEN["last_user_activity"]),
            "last_env_event_at": self.last_env_event_at,
            "current_trigger": self.current_trigger,
        }
        return snap
