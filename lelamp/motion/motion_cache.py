"""
离线运动缓存池

策略：
  - 每个 (emotion, intensity_bucket) 维护一个 deque（目标 8 条）
  - get() 弹出一条，库存 < 3 时后台异步补充
  - 补充使用 asyncio.gather 并行发 N 个请求，总延迟 ≈ 单次调用
  - warmup() 启动时并行预热所有桶
  - 无缓存时返回 None，由调用方 fallback 到模板（零中断）
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from typing import Optional

from lelamp.motion.llm_generator import LLMGenerator
from lelamp.motion.templates import EMOTION_INDEX, MotionKeyframes

logger = logging.getLogger(__name__)

_INTENSITY_BUCKETS = {
    "low":  0.30,   # intensity 代表值（用于 generate 调用）
    "mid":  0.60,
    "high": 0.90,
}

_BUCKET_INTENSITY_RANGES = {
    "low":  (0.0,  0.45),
    "mid":  (0.45, 0.72),
    "high": (0.72, 1.01),
}


def _intensity_bucket(intensity: float) -> str:
    for name, (lo, hi) in _BUCKET_INTENSITY_RANGES.items():
        if lo <= intensity < hi:
            return name
    return "high"


class MotionCache:
    """
    线程安全的 MotionKeyframes 缓存池。

    用法：
        cache = MotionCache(generator)
        cache.warmup()          # 阻塞直到首批缓存就绪
        kf = cache.get("happy", 0.8)  # 零延迟，None = 回退模板
    """

    TARGET_SIZE       = 8    # 每个桶的目标容量
    REFILL_THRESHOLD  = 3    # 低于此值时触发后台补充

    def __init__(
        self,
        generator:  LLMGenerator,
        example_db = None,      # Optional[ExampleDB]，用于 few-shot 注入
    ):
        self._generator  = generator
        self._db         = example_db

        # 缓存桶：key=(emotion, bucket_name), value=deque[MotionKeyframes]
        self._buckets: dict[tuple, deque[MotionKeyframes]] = {
            (emotion, bucket): deque()
            for emotion in EMOTION_INDEX
            for bucket in _INTENSITY_BUCKETS
        }
        self._lock       = threading.Lock()
        self._refilling: set[tuple] = set()
        self._refill_lock = threading.Lock()

    # ── 运行时接口 ────────────────────────────────────────────────────────────

    def get(self, emotion: str, intensity: float) -> Optional[MotionKeyframes]:
        """
        弹出一条缓存。
        返回 None 表示该桶为空，调用方应 fallback 到 template_generate()。
        同时在后台异步补充。
        """
        key = (emotion, _intensity_bucket(intensity))
        with self._lock:
            bucket = self._buckets.get(key)
            if not bucket:
                remaining = 0
                kf = None
            else:
                kf        = bucket.popleft()
                remaining = len(bucket)

        if remaining < self.REFILL_THRESHOLD:
            self._trigger_refill(key, intensity)

        return kf

    def bucket_size(self, emotion: str, intensity: float) -> int:
        key = (emotion, _intensity_bucket(intensity))
        with self._lock:
            return len(self._buckets.get(key, []))

    # ── 预热 ──────────────────────────────────────────────────────────────────

    def warmup(self):
        """
        阻塞式预热所有桶（推荐在服务启动时调用）。
        所有情绪 × 3 强度桶 = 15 个桶并行预热。
        """
        logger.info("🔥 开始预热运动缓存（LLM 并行）...")
        t0 = time.time()

        async def _all():
            tasks = [
                self._refill_async(
                    key=(emotion, bucket),
                    intensity=rep_intensity,
                    n_needed=self.TARGET_SIZE,
                )
                for emotion in EMOTION_INDEX
                for bucket, rep_intensity in _INTENSITY_BUCKETS.items()
            ]
            await asyncio.gather(*tasks, return_exceptions=True)

        asyncio.run(_all())
        total = sum(len(b) for b in self._buckets.values())
        logger.info("✅ 缓存预热完成: %d 条, %.1fs", total, time.time() - t0)

    # ── 后台补充 ──────────────────────────────────────────────────────────────

    def _trigger_refill(self, key: tuple, intensity: float):
        with self._refill_lock:
            if key in self._refilling:
                return
            self._refilling.add(key)

        with self._lock:
            n_needed = self.TARGET_SIZE - len(self._buckets.get(key, []))
        if n_needed <= 0:
            with self._refill_lock:
                self._refilling.discard(key)
            return

        emotion        = key[0]
        rep_intensity  = _INTENSITY_BUCKETS[key[1]]  # 桶的代表强度

        def _run():
            try:
                asyncio.run(self._refill_async(key, rep_intensity, n_needed))
            finally:
                with self._refill_lock:
                    self._refilling.discard(key)

        threading.Thread(
            target=_run, daemon=True,
            name=f"cache-refill-{emotion}-{key[1]}",
        ).start()

    async def _refill_async(
        self,
        key:         tuple,
        intensity:   float,
        n_needed:    int,
    ):
        """
        并行发 n_needed × 3 个 LLM 请求，取有效结果填入桶。
        总延迟 ≈ 单次调用时间（asyncio.gather 并行）。
        """
        emotion   = key[0]
        n_total   = max(n_needed * 3, 6)

        # few-shot 注入（如果有 ExampleDB）
        few_shot = None
        if self._db is not None:
            try:
                few_shot = self._db.retrieve(emotion, intensity, k=2)
            except Exception:
                pass

        tasks   = [self._generator.generate(emotion, intensity, few_shot) for _ in range(n_total)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        valid   = [r for r in results if isinstance(r, MotionKeyframes)]

        added = 0
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is not None:
                for kf in valid[:n_needed]:
                    bucket.append(kf)
                    added += 1

        logger.info(
            "缓存补充 %s/%s: +%d 条  (请求%d / 有效%d)",
            emotion, key[1], added, n_total, len(valid),
        )
