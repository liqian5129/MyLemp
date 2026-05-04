"""persona 模式表情驱动模块(协议 v0.5.0)。

face_director 负责把对话语义/情绪映射到设备表情:
  - 关键词触发 arc(早安/晚安/难过等触发预定义剧本)
  - LLM 输出尾部 <face>name</face> 标签解析为 set_face

通过 buddy daemon 的 HTTP /face / /arc 路由下发命令(daemon 持 USB 端口),
避免 main_persona 跟 daemon 争抢 USB。
"""
from lelamp.persona.face_director import FaceDirector

__all__ = ["FaceDirector"]
