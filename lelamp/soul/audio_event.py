"""Omni 智能耳朵 → SoulAgent 的结构化音频事件。"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class AudioEvent:
    """一次音频事件（语音或环境音变化）。

    由 OmniEar（Qwen Omni RT）生成，传递给 SoulAgent。
    """

    text: str
    """转录文本（非语音时为空字符串）。"""

    emotion: str = "neutral"
    """说话者情绪：neutral / happy / tired / frustrated / curious / sad。"""

    intent: str = "none"
    """意图分类（由 Omni LLM 判断）：
    - new_request: 新指令
    - supplement: 对正在执行任务的补充
    - cancel: 取消当前任务
    - chat: 与当前任务无关的闲聊
    - none: 非语音事件（环境音变化、叹气等）
    """

    directed: str = "uncertain"
    """语音是否对小Q说的：
    - to_robot: 明确对小Q说话
    - not_to_robot: 明确不是（视频/播客/自言自语/与他人交谈）
    - uncertain: 无法确定
    """

    audio_env: str = ""
    """环境音描述（如 "键盘声停了""安静""多人说话"）。"""

    user_activity: str = "未知"
    """用户行为（唱歌、敲键盘、吃东西、走路、安静坐着等）。"""

    is_speech: bool = True
    """True=语音事件，False=纯环境音/情绪信号。"""

    speaker: Optional[str] = None
    """声纹识别出的说话者名字，未识别时为 None。"""

    voice_embedding: object = field(default=None, compare=False, repr=False, hash=False)
    """当前语音段的声纹 embedding（np.ndarray），供 register_voice 工具使用。"""
