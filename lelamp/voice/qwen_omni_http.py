"""Qwen Omni HTTP 客户端 — 通过 function calling 获取结构化音频分析。

调用 DashScope OpenAI 兼容接口，发送音频段，返回转录 + 情绪 + 意图 + 环境音。
"""
from __future__ import annotations

import base64
import io
import json
import logging
import time
import wave
from typing import Optional

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "你是小Q的耳朵。小Q是一盏桌面台灯机器人。分析麦克风采集到的音频，调用 report_event 报告你听到的内容。\n"
    "判断：\n"
    "- text: 转录人类说的话（仅限人类语音内容；如果没有人说话，填空字符串 ''）\n"
    "- emotion: 从语气判断说话者情绪：neutral/happy/tired/frustrated/curious/sad\n"
    "- intent: 判断意图：new_request（新指令）/ supplement（补充）/ cancel（取消）/ chat（闲聊）/ none（没有人说话，只有环境音）\n"
    "- directed: 判断这段语音是否是对小Q说的：\n"
    "    to_robot — 明确对小Q说话（叫了名字、语气像在跟身边的人/物对话、内容是指令或提问）\n"
    "    not_to_robot — 明确不是对小Q说的（电视/视频/播客的声音、电话对话、自言自语、\n"
    "                   两人互相聊天、演讲/讲座录音、内容是在叙述故事或观点而非对话）\n"
    "    uncertain — 无法确定\n"
    "  判断技巧：视频/播客的声音通常语速平稳、内容连贯像在讲述，不会停顿等回应；\n"
    "  对小Q说话通常短句、口语化、有称呼或祈使语气。\n"
    "- audio_env: 描述背景环境音（安静、键盘声、多人说话、敲门声等）\n"
    "- user_activity: 判断用户正在做什么（唱歌、敲键盘、吃东西、走路、与他人交谈、看视频、打电话等，无法判断则填'未知'）\n"
    "\n"
    "重要：text 只填人类说的话。敲门声、敲击声、脚步声等非语音声音不要填在 text 里，应填在 audio_env 里。\n"
)

_REPORT_TOOL = {
    "type": "function",
    "function": {
        "name": "report_event",
        "description": "报告音频分析结果",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "用户说的话（仅人类语音转录，非语音时填空字符串）"},
                "emotion": {
                    "type": "string",
                    "enum": ["neutral", "happy", "tired", "frustrated", "curious", "sad"],
                    "description": "说话者情绪",
                },
                "intent": {
                    "type": "string",
                    "enum": ["new_request", "supplement", "cancel", "chat", "none"],
                    "description": "用户意图（没有人说话时填 none）",
                },
                "directed": {
                    "type": "string",
                    "enum": ["to_robot", "not_to_robot", "uncertain"],
                    "description": "语音是否对小Q说的（视频/播客/自言自语/与他人交谈=not_to_robot）",
                },
                "audio_env": {"type": "string", "description": "环境音描述"},
                "user_activity": {"type": "string", "description": "用户正在做的事（唱歌、敲键盘、吃东西、走路、安静坐着、与他人交谈等）"},
            },
            "required": ["text", "emotion", "intent", "directed", "audio_env", "user_activity"],
        },
    },
}

_DEFAULT_RESULT = {"text": "", "emotion": "neutral", "intent": "none", "directed": "uncertain", "audio_env": "", "user_activity": "未知"}


def _pcm_to_wav_base64(pcm_bytes: bytes, sample_rate: int) -> str:
    """int16 PCM → WAV → base64 字符串。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return base64.b64encode(buf.getvalue()).decode("ascii")


class QwenOmniHTTPClient:
    """通过 HTTP API 调用 Qwen Omni，分析音频段。"""

    def __init__(
        self,
        api_key: str,
        model: str = "qwen3-omni-flash",
        base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ):
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        self._model = model

    async def analyze_audio(
        self, pcm_bytes: bytes, sample_rate: int = 16000
    ) -> dict:
        """分析一段 int16 PCM 音频，返回结构化结果。

        Returns:
            {"text": str, "emotion": str, "intent": str, "audio_env": str}
        """
        wav_b64 = _pcm_to_wav_base64(pcm_bytes, sample_rate)
        duration = len(pcm_bytes) / (sample_rate * 2)
        logger.info("HTTP Omni 请求 (%.1fs, %.1fKB)", duration, len(pcm_bytes) / 1024)

        t0 = time.monotonic()
        resp = await self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_audio",
                            "input_audio": {
                                "data": f"data:audio/wav;base64,{wav_b64}",
                                "format": "wav",
                            },
                        },
                    ],
                },
            ],
            modalities=["text"],
            tools=[_REPORT_TOOL],
            tool_choice={"type": "function", "function": {"name": "report_event"}},
        )
        elapsed = time.monotonic() - t0
        logger.info("HTTP Omni 响应耗时: %.2fs", elapsed)

        msg = resp.choices[0].message
        if msg.tool_calls:
            return json.loads(msg.tool_calls[0].function.arguments)

        logger.warning("未返回 function call，丢弃原始回复: %s", msg.content)
        return {**_DEFAULT_RESULT}
