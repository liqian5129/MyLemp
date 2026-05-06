"""persona 模式辅助模块(协议 v0.5.0+)。

- face_director:把对话语义/情绪映射到设备表情(set_face / play_arc)
  通过 buddy daemon 的 HTTP /face /arc 路由下发,避免争 USB。
- buddy_awareness(v0.5.1):buddy 模式下 poll daemon /state,
  state 变化时驱动 lamp motion 配合 cc 状态。同时同步 device_mode
  让 SoulAgent 的 motion gate 知道当前模式做互斥。
"""
from lelamp.persona.face_director import FaceDirector
from lelamp.persona.buddy_awareness import BuddyAwareness

__all__ = ["FaceDirector", "BuddyAwareness"]
