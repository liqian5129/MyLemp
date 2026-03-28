"""
LLM 关键帧生成器

调用 Kimi（可换其他 OpenAI 兼容模型）生成情绪运动关键帧。
输出与 template_generate() 完全兼容的 MotionKeyframes 对象。

设计原则：
  - generate() 是 async，支持 asyncio.gather 并行调用（Best-of-N）
  - 解析/校验失败返回 None，由调用方 fallback 到模板
  - f3 软约束：偏离 Q_REST 不超过 30°（逐关节 clip）
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

import numpy as np

from lelamp.agent.ai_client import AIClient
from lelamp.motion.motion_executor import Q_MIN, Q_MAX, Q_REST
from lelamp.motion.templates import EMOTION_INDEX, MotionKeyframes

logger = logging.getLogger(__name__)

# ── 系统提示词 ────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """你是专业的机器人运动设计师，为5关节台灯机器人设计情绪运动关键帧。

关节定义（绝对角度，单位：度）：
  q0 base_yaw    [-5,  +14]°   正=右转，负=左转
  q1 base_pitch  [-68, -20]°   更负=更高，-68最高，-20最低
  q2 elbow_pitch [50,  100]°   正=弯曲，100=最弯
  q3 wrist_roll  [-30, +15]°   正=右歪，负=左歪
  q4 wrist_pitch [-5,  +68]°   正=抬头，-5=低头

中性姿态（Q_REST）：[0, -44, 75, 0, 25]

动作关键帧说明：
  f1 情绪高峰帧  — 最能表达情绪的姿态，动作约 25-35% 时到达
  f2 回落缓冲帧  — 从高峰稍退，给动作层次感
  f3 收束停留帧  — 动作结束后的停留姿态，各关节偏离Q_REST不超过30°

运动参数：
  duration    动作总时长（秒），范围 0.8~4.0
  accel_ratio 加速比例，0.10=快起（爆发），0.35=慢起（平滑）
  asymmetry   不对称度，+1.0=快去慢回（弹跳），-1.0=慢去快收（下坠），0=对称

**输出严格JSON，不加任何其他文字：**
{"f1":[q0,q1,q2,q3,q4],"f2":[q0,q1,q2,q3,q4],"f3":[q0,q1,q2,q3,q4],"duration":数字,"accel_ratio":数字,"asymmetry":数字}"""

# ── 各情绪的运动风格提示 ──────────────────────────────────────────────────────

_EMOTION_HINTS: dict[str, str] = {
    "happy":   "快速上扬弹跳，轻盈晃头，asymmetry=+0.6~0.8，duration短(1.2~2.0s)",
    "sad":     "缓缓低垂，沉重，asymmetry=-0.5~-0.8，duration长(2.5~4.0s)，幅度偏小",
    "angry":   "快速直接，大幅冲击，duration短(0.8~1.5s)，asymmetry近0或稍负",
    "curious": "歪头前探，左右张望，asymmetry=+0.3，持续偏移不完全回中",
    "calm":    "慢而对称，幅度极小，duration长(2.5~4.0s)，f1/f2接近Q_REST",
}

# ── 解析与校验 ────────────────────────────────────────────────────────────────

def _clip_f3(f3_raw: list) -> np.ndarray:
    """确保 f3 各关节偏离 Q_REST ≤ 30°"""
    f3 = np.array(f3_raw, dtype=np.float32)
    delta = np.clip(f3 - Q_REST, -30.0, 30.0)
    return np.clip(Q_REST + delta, Q_MIN, Q_MAX)


def _parse(raw: str, emotion: str) -> Optional[MotionKeyframes]:
    """解析 LLM JSON 输出，校验维度和范围，失败返回 None"""
    # 提取 JSON 片段（防止模型在前后加了说明文字）
    start = raw.find("{")
    end   = raw.rfind("}") + 1
    if start < 0 or end <= start:
        logger.warning("LLM 输出无 JSON: %s", raw[:100])
        return None

    try:
        data = json.loads(raw[start:end])

        f1 = np.array(data["f1"], dtype=np.float32)
        f2 = np.array(data["f2"], dtype=np.float32)
        f3 = _clip_f3(data["f3"])

        if f1.shape != (5,) or f2.shape != (5,):
            logger.warning("LLM 输出维度错误: f1=%s f2=%s", f1.shape, f2.shape)
            return None

        f1 = np.clip(f1, Q_MIN, Q_MAX)
        f2 = np.clip(f2, Q_MIN, Q_MAX)

        duration    = float(np.clip(data["duration"],    0.8, 4.0))
        accel_ratio = float(np.clip(data["accel_ratio"], 0.05, 0.50))
        asymmetry   = float(np.clip(data["asymmetry"],  -1.0, 1.0))

        return MotionKeyframes(
            f1=f1, f2=f2, f3=f3,
            duration=duration,
            accel_ratio=accel_ratio,
            asymmetry=asymmetry,
            intent=f"[LLM] {emotion}",
        )

    except (KeyError, ValueError, json.JSONDecodeError) as e:
        logger.warning("LLM 输出解析失败: %s | 原文: %s", e, raw[:200])
        return None


# ── 生成器类 ──────────────────────────────────────────────────────────────────

class LLMGenerator:
    """
    异步 LLM 关键帧生成器。

    用法：
        gen = LLMGenerator()
        kf = await gen.generate("happy", 0.8)

    并行 Best-of-N：
        results = await asyncio.gather(*[gen.generate("happy", 0.8) for _ in range(8)])
        valid = [r for r in results if r is not None]
    """

    def __init__(
        self,
        api_key:  str   = "",
        model:    str   = "moonshot-v1-8k",
        base_url: str   = "https://api.moonshot.cn/v1",
        timeout:  float = 20.0,
    ):
        api_key = api_key or os.environ.get("KIMI_API_KEY", "")
        self._client = AIClient(
            provider  = "kimi",
            api_key   = api_key,
            model     = model,
            base_url  = base_url,
            max_retries = 1,
            timeout   = timeout,
        )

    async def generate(
        self,
        emotion:          str,
        intensity:        float,
        few_shot_examples: list[MotionKeyframes] | None = None,
    ) -> Optional[MotionKeyframes]:
        """
        生成单条 MotionKeyframes。失败返回 None。

        Args:
            emotion:           情绪名（必须在 EMOTION_INDEX 中）
            intensity:         强度 [0, 1]
            few_shot_examples: 高分参考样本（来自 ExampleDB），注入 prompt
        """
        if emotion not in EMOTION_INDEX:
            return None

        user_msg = self._build_user_msg(emotion, intensity, few_shot_examples)

        for attempt in range(2):
            try:
                resp = await self._client.chat(
                    user_message  = user_msg,
                    system_prompt = _SYSTEM_PROMPT,
                    max_tokens    = 200,
                )
                if resp.stop_reason == "error":
                    continue

                kf = _parse(resp.text, emotion)
                if kf is not None:
                    logger.debug(
                        "LLM 生成 ✓ %s @ %.1f  dur=%.1fs asym=%.2f",
                        emotion, intensity, kf.duration, kf.asymmetry,
                    )
                    return kf

            except Exception as e:
                logger.warning("LLM generate 异常 (attempt %d): %s", attempt + 1, e)

        logger.warning("LLM 生成失败: %s @ %.1f", emotion, intensity)
        return None

    def _build_user_msg(
        self,
        emotion:   str,
        intensity: float,
        few_shot:  list[MotionKeyframes] | None,
    ) -> str:
        hint = _EMOTION_HINTS.get(emotion, "")
        lines = [
            f"情绪：{emotion}  强度：{intensity:.1f}",
            f"风格提示：{hint}",
            f"intensity={intensity:.1f}，f1 偏移幅度约为最大幅度的 {intensity:.0%}。",
        ]

        if few_shot:
            lines.append("参考示例（高分动作风格）：")
            for i, ex in enumerate(few_shot[:2]):
                lines.append(
                    f"  示例{i+1}: f1={[round(v,1) for v in ex.f1.tolist()]} "
                    f"f2={[round(v,1) for v in ex.f2.tolist()]} "
                    f"dur={ex.duration:.1f} asym={ex.asymmetry:.2f}"
                )

        lines.append("输出JSON：")
        return "\n".join(lines)
