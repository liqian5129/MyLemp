"""Omni 智能耳朵 → SoulAgent 的结构化音频事件。"""

from dataclasses import dataclass


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

    audio_env: str = ""
    """环境音描述（如 "键盘声停了""安静""多人说话"）。"""

    is_speech: bool = True
    """True=语音事件，False=纯环境音/情绪信号。"""
