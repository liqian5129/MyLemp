"""render_context_packet — 把六层记忆渲染成 ReAct initial user message 的正文。

段顺序固定：
  [STATE]    当前世界快照（state.snapshot()）
  [FACTS]    facts.format()        ← Phase 2 起非空
  [TODAY]    today                 ← Phase 3 起非空
  [SCENE]    scene.read()
  [RECENT]   episodic.format_for_prompt()
  [TRIGGER]  本次触发原因
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from .episodic import MemoryStream
    from .scene import SceneMemory
    from .state import WorldState


def sanitize_names_in_text(content: str, names: set[str]) -> str:
    """将已知人名替换为中性称呼，消除 few-shot 示范效应。"""
    for name in sorted(names, key=len, reverse=True):
        content = content.replace(f"{name}，", "")
        content = content.replace(f"{name},", "")
        content = content.replace(name, "用户")
    return content.strip()


def _fmt_relative(ts: Optional[float], now: float) -> Optional[str]:
    """把 unix timestamp 转换成 '12 秒前' / '3 分钟前' / '2 小时前' 的相对时间。"""
    if ts is None or ts <= 0:
        return None
    delta = max(0.0, now - ts)
    if delta < 60:
        return f"{int(delta)} 秒前"
    if delta < 3600:
        return f"{int(delta / 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta / 3600)} 小时前"
    return f"{int(delta / 86400)} 天前"


def _render_state(snap: dict[str, Any]) -> str:
    """把 WorldState.snapshot() 渲染成 [STATE] 段文本。

    None 字段省略；不打印 'None'。
    """
    now: datetime = snap["now"]
    now_ts = now.timestamp()
    lines: list[str] = ["[STATE]"]

    # 显示秒级精度：让 LLM 在算 due_at 时（如"30 秒后叫我"）有精确参考点。
    # 之前只到分钟，导致 LLM 把 13:30:52 当成 13:30 算，30 秒后变成 13:30:30 → 落到过去。
    lines.append(f"时间: {now.strftime('%Y-%m-%d %H:%M:%S')} ({snap['time_phase']})")

    body = snap.get("body_status")
    if body:
        lines.append(f"身体: {body}")

    speech_at = snap.get("last_user_speech_at")
    if speech_at:
        rel = _fmt_relative(speech_at, now_ts)
        speaker = snap.get("last_user_speaker")
        if speaker:
            lines.append(f"在场: {speaker} ({rel}说话)")
        else:
            lines.append(f"在场: 用户 ({rel}说话)")

    self_speech_at = snap.get("last_self_speech_at")
    self_count = snap.get("self_speech_count_today", 0)
    if self_speech_at or self_count:
        if self_speech_at:
            rel = _fmt_relative(self_speech_at, now_ts)
            lines.append(f"上次主动说话: {rel} (今天累计 {self_count} 次)")
        else:
            lines.append(f"今天主动说话: {self_count} 次")

    activity = snap.get("last_user_activity")
    if activity:
        lines.append(f"用户行为: {activity}")

    ticks = snap.get("ticks_since_photo", 0)
    if ticks > 0:
        lines.append(f"视觉: 已 {ticks} 次心跳没看了")

    env_at = snap.get("last_env_event_at")
    env_audio = snap.get("last_audio_env")
    if env_at and env_audio:
        rel = _fmt_relative(env_at, now_ts)
        lines.append(f"上次环境事件: {rel} ({env_audio})")
    elif env_audio:
        lines.append(f"环境音: {env_audio}")

    return "\n".join(lines)


def _render_section(header: str, body: Optional[str]) -> Optional[str]:
    """通用段渲染：空内容返回 None（render_context_packet 会跳过）。"""
    if body is None:
        return None
    body = body.strip()
    if not body:
        return None
    return f"[{header}]\n{body}"


def render_context_packet(
    state: "WorldState",
    episodic: "MemoryStream",
    scene: "SceneMemory",
    facts: Any = None,    # Phase 2 注入 FactStore
    today: Optional[str] = None,   # Phase 3 注入 today_narrative
    ltm_hint: Optional[str] = None,  # 长期记忆摘要提示
    trigger: str = "",
    confirmed_speaker: Optional[str] = None,  # 当前声纹确认的身份
    known_names: Optional[set[str]] = None,    # 所有已知人名（用于脱敏）
) -> str:
    """构造 ReAct initial user message 的正文。

    见模块 docstring 的段顺序。空段省略。

    confirmed_speaker: 当前触发由声纹确认的说话人。为 None 时，
                       [RECENT] 和 [FACTS] 中的人名会被脱敏，
                       防止 LLM 在心跳/环境触发时凭记忆猜测身份。
    """
    sections: list[str] = []

    # 脱敏策略：无确认身份时隐藏人名
    sanitize = known_names if (confirmed_speaker is None and known_names) else None

    # [STATE]
    sections.append(_render_state(state.snapshot()))

    # [FACTS]  — Phase 2 起
    if facts is not None:
        try:
            facts_text = facts.format(confirmed_speaker=confirmed_speaker)
        except (AttributeError, TypeError):
            facts_text = None
        sec = _render_section("FACTS", facts_text)
        if sec:
            sections.append(sec)

    # [LONGTERM]  — 长期记忆摘要提示
    if ltm_hint:
        sec = _render_section(
            "LONGTERM",
            f"你的长期记忆库：{ltm_hint} — 用 recall_memory 搜索详情",
        )
        if sec:
            sections.append(sec)

    # [TODAY]  — Phase 3 起
    today_text = today
    if today_text and sanitize:
        today_text = sanitize_names_in_text(today_text, sanitize)
    sec = _render_section("TODAY", today_text)
    if sec:
        sections.append(sec)

    # [SCENE]
    try:
        scene_text = scene.read()
    except Exception:
        scene_text = None
    sec = _render_section("SCENE", scene_text)
    if sec:
        sections.append(sec)

    # [RECENT]
    try:
        recent_text = episodic.format_for_prompt(sanitize_names=sanitize)
    except Exception:
        recent_text = None
    sec = _render_section("RECENT", recent_text)
    if sec:
        sections.append(sec)

    # [TRIGGER]
    sec = _render_section("TRIGGER", trigger)
    if sec:
        sections.append(sec)

    return "\n\n".join(sections)
