"""
统一动作生成入口

优先级（零延迟路径）：
  1. MotionCache（LLM 离线预生成，质量最高）
  2. template_generate（参数化模板，Day-0 兜底）

LLM 仅在离线 prefill/warmup 时调用，不在运行时阻塞。
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from lelamp.motion.motion_cache import MotionCache
from lelamp.motion.templates import EMOTION_INDEX, MotionKeyframes, template_generate

logger = logging.getLogger(__name__)


class MotionService:
    """
    运行时动作生成服务。

    构造示例（纯模板模式，无需 API key）：
        svc = MotionService()

    构造示例（带 LLM 缓存）：
        from lelamp.motion.llm_generator import LLMGenerator
        from lelamp.motion.motion_cache import MotionCache
        gen   = LLMGenerator(api_key=os.environ["KIMI_API_KEY"])
        cache = MotionCache(gen)
        cache.warmup()      # 阻塞约 5~10s，等首批缓存就绪
        svc   = MotionService(cache=cache)
    """

    def __init__(self, cache: Optional[MotionCache] = None):
        self._cache = cache
        mode = "缓存+模板" if cache is not None else "纯模板"
        logger.info("MotionService 初始化: %s", mode)

    # ── 核心接口 ──────────────────────────────────────────────────────────────

    def generate(self, emotion: str, intensity: float) -> MotionKeyframes:
        """
        同步、零延迟生成 MotionKeyframes。
        永远不会阻塞：缓存命中则返回 LLM 高质量帧，否则立即返回模板帧。
        """
        if self._cache is not None:
            kf = self._cache.get(emotion, intensity)
            if kf is not None:
                logger.debug("缓存命中: %s @ %.1f", emotion, intensity)
                return kf
            logger.debug("缓存未命中: %s @ %.1f，使用模板", emotion, intensity)

        return template_generate(emotion, intensity)

    @property
    def has_cache(self) -> bool:
        return self._cache is not None

    def cache_status(self) -> dict[str, int]:
        """返回各桶剩余数量（调试用）"""
        if self._cache is None:
            return {}
        return {
            f"{e}/{b}": self._cache.bucket_size(e, v)
            for e in EMOTION_INDEX
            for b, v in [("low", 0.3), ("mid", 0.6), ("high", 0.9)]
        }


# ── 工厂函数 ──────────────────────────────────────────────────────────────────

def create_motion_service(
    use_llm:     bool  = True,
    api_key:     str   = "",
    warmup:      bool  = True,
    example_db          = None,   # Optional[ExampleDB]
) -> MotionService:
    """
    便捷工厂函数。

    Args:
        use_llm:    是否启用 LLM 缓存（需要 KIMI_API_KEY）
        api_key:    API key，默认读 KIMI_API_KEY 环境变量
        warmup:     是否在返回前阻塞预热缓存
        example_db: ExampleDB 实例，用于 few-shot 注入
    """
    if not use_llm:
        return MotionService()

    api_key = api_key or os.environ.get("KIMI_API_KEY", "")
    if not api_key:
        logger.warning("KIMI_API_KEY 未设置，降级为纯模板模式")
        return MotionService()

    from lelamp.motion.llm_generator import LLMGenerator
    gen   = LLMGenerator(api_key=api_key)
    cache = MotionCache(gen, example_db=example_db)

    if warmup:
        cache.warmup()

    return MotionService(cache=cache)
