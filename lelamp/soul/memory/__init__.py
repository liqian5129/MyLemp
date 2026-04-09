"""小Q 记忆系统六层模型。

子模块：
  episodic  — append-only 事件流（heard/said + 兼容旧类型）
  scene     — 文件式场景记忆
  identity  — 声纹/人脸 embedding 库
  state     — WorldState 视图层（read-through 到 SoulAgent）
  render    — 把各层渲染成 prompt 段
"""
from .episodic import MemoryStream, MemoryEntry
from .scene import SceneMemory
from .identity import IdentityMemory
from .state import WorldState
from .render import render_context_packet
from .facts import FactStore, Fact
from .consolidate import (
    FactCandidate,
    PendingFactBuffer,
    extract_facts,
    today_narrative,
)

__all__ = [
    "MemoryStream",
    "MemoryEntry",
    "SceneMemory",
    "IdentityMemory",
    "WorldState",
    "render_context_packet",
    "FactStore",
    "Fact",
    "FactCandidate",
    "PendingFactBuffer",
    "extract_facts",
    "today_narrative",
]
