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
    "你是小Q的耳朵。分析用户的语音，调用 report_event 报告你听到的内容。\n"
    "判断：\n"
    "- text: 转录用户说的话\n"
    "- emotion: 从语气判断说话者情绪：neutral/happy/tired/frustrated/curious/sad\n"
    "- intent: 判断意图：new_request（新指令）/ supplement（补充）/ cancel（取消）/ chat（闲聊）\n"
    "- audio_env: 描述背景环境音（安静、键盘声、多人说话等）\n"
)

_REPORT_TOOL = {
    "type": "function",
    "function": {
        "name": "report_event",
        "description": "报告音频分析结果",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "用户说的话（转录）"},
                "emotion": {
                    "type": "string",
                    "enum": ["neutral", "happy", "tired", "frustrated", "curious", "sad"],
                    "description": "说话者情绪",
                },
                "intent": {
                    "type": "string",
                    "enum": ["new_request", "supplement", "cancel", "chat"],
                    "description": "用户意图",
                },
                "audio_env": {"type": "string", "description": "环境音描述"},
            },
            "required": ["text", "emotion", "intent", "audio_env"],
        },
    },
}

_DEFAULT_RESULT = {"text": "", "emotion": "neutral", "intent": "chat", "audio_env": ""}


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
