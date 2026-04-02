"""
说话预算 — 只统计心跳触发的主动说话，用户对话中的回复不计入

每日上限 8 次，最小间隔 5 分钟，午夜自动重置。
"""
import time
import logging
from datetime import date

logger = logging.getLogger(__name__)


class SpeechBudget:
    def __init__(self, daily_limit: int = 8, min_interval: float = 300.0):
        self.daily_limit = daily_limit
        self.min_interval = min_interval
        self._today = date.today()
        self._count = 0
        self._last_time = 0.0

    def record(self):
        """记录一次主动说话"""
        self._rollover()
        self._count += 1
        self._last_time = time.time()
        logger.info("🗣️ 主动说话 %d/%d", self._count, self.daily_limit)

    def get_context(self) -> str:
        """返回注入心跳 prompt 的预算上下文，无内容时返回空字符串"""
        self._rollover()
        parts = []
        if self._count > 0:
            parts.append(f"今天你已经主动说了 {self._count} 次。")
        if self._count >= 6:
            parts.append("说得够多了，除非非常重要否则保持安静。")
        if self._last_time and time.time() - self._last_time < self.min_interval:
            parts.append("你刚说过话不久，不要太频繁。")
        return "\n".join(parts)

    def _rollover(self):
        today = date.today()
        if today != self._today:
            self._today = today
            self._count = 0
