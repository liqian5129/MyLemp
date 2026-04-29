"""AI Working Companion daemon。

把 coding agent 的事件(Claude Code hooks 等)翻译为 SessionSnapshot,
派生 buddy state,经 v0.2.0 协议下发到设备。

入口:`uv run python -m lelamp.companion`
契约:`dev-agent-events.md`
"""
from lelamp.companion.snapshot import (
    SessionSnapshot,
    Prompt,
    Error,
    derive_state,
    CELEBRATE_DURATION,
    ERROR_DISPLAY_TTL,
    SLEEP_THRESHOLD,
)
from lelamp.companion.state_machine import SessionStateMachine

__all__ = [
    "SessionSnapshot",
    "Prompt",
    "Error",
    "derive_state",
    "SessionStateMachine",
    "CELEBRATE_DURATION",
    "ERROR_DISPLAY_TTL",
    "SLEEP_THRESHOLD",
]
