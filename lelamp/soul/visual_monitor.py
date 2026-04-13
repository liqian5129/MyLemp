"""VisualMonitor — 帧差检测，感知画面中的显著变化。

后台 asyncio task，每 check_interval 秒取帧做灰度帧差：
  变化 ≥ change_threshold → 触发 on_change 回调 + 更新参考帧
  5%-threshold            → 仅 debug 日志
  < 5%                    → 每 30s 慢更新参考帧（适应光线渐变）

防环路：
  - motion_agent.is_playing() → 跳过
  - 运动结束首次循环 → 重置参考帧，不触发
  - resume() → 重置参考帧，跳过首次比较
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

import cv2
import numpy as np

if TYPE_CHECKING:
    from .camera_capture import CameraCapture

logger = logging.getLogger(__name__)

# 参考帧慢更新间隔（秒），低于 5% 变化时每隔这么久更新一次
_SLOW_UPDATE_INTERVAL = 30.0
# 缩放尺寸（灰度帧差用）
_RESIZE_W, _RESIZE_H = 320, 240


class VisualMonitor:
    """帧差视觉变化检测器。

    用法：
        monitor = VisualMonitor(camera, motion_agent, on_change=callback)
        monitor.start()   # 在 asyncio 事件循环中调用
        ...
        monitor.stop()
    """

    def __init__(
        self,
        camera: "CameraCapture",
        motion_agent,
        on_change: Optional[Callable[[str], Awaitable[None]]] = None,
        check_interval: float = 2.0,
        change_threshold: float = 0.25,
        pixel_threshold: int = 25,
        startup_delay: float = 3.0,
    ):
        self._camera = camera
        self._motion_agent = motion_agent
        self.on_change = on_change

        self._check_interval = check_interval
        self._change_threshold = change_threshold
        self._pixel_threshold = pixel_threshold
        self._startup_delay = startup_delay

        self._reference: Optional[np.ndarray] = None  # 灰度参考帧
        self._motion_dirty = False
        self._last_seen_motion_end: float = 0.0  # 上次已处理的 motion_end 时间戳
        self._skip_next = False  # resume/motion-end 后跳过一次比较
        self._last_slow_update: float = 0.0

        self._task: Optional[asyncio.Task] = None
        self._running = False

    # ── 公共接口 ──────────────────────────────────────────────────────────────

    def start(self):
        """启动后台检测（在 asyncio 事件循环中调用）。"""
        if self._task is None:
            self._running = True
            self._task = asyncio.create_task(self._loop(), name="visual-monitor")
            logger.info("👁 VisualMonitor 已启动 (interval=%.1fs, threshold=%.0f%%)",
                        self._check_interval, self._change_threshold * 100)

    def stop(self):
        """停止后台检测。"""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None
            logger.info("👁 VisualMonitor 已停止")

    def pause(self):
        """暂时挂起检测（外部可选调用）。"""
        self._skip_next = True

    def resume(self):
        """恢复检测，重置参考帧防误触。"""
        self._reference = None
        self._skip_next = True

    # ── 内部 ──────────────────────────────────────────────────────────────────

    def _prepare_gray(self, frame: np.ndarray) -> np.ndarray:
        """缩放 + 灰度 + 高斯模糊。"""
        small = cv2.resize(frame, (_RESIZE_W, _RESIZE_H))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        return cv2.GaussianBlur(gray, (5, 5), 0)

    async def _loop(self):
        """后台检测循环。"""
        await asyncio.sleep(self._startup_delay)

        # 等待首帧作为参考
        while self._running:
            frame = self._camera.get_latest_frame()
            if frame is not None:
                self._reference = self._prepare_gray(frame)
                self._last_slow_update = time.monotonic()
                logger.debug("👁 参考帧已初始化")
                break
            await asyncio.sleep(0.5)

        while self._running:
            await asyncio.sleep(self._check_interval)
            if not self._running:
                break

            try:
                await self._tick()
            except Exception as exc:
                logger.error("VisualMonitor tick 异常: %s", exc, exc_info=True)

    async def _tick(self):
        # 运动中 → 标记 dirty，跳过
        if self._motion_agent.is_playing():
            self._motion_dirty = True
            return

        # 检测短运动（在两个 tick 之间完整发生，轮询没看到 is_playing）
        motion_end_t = self._motion_agent.last_motion_end_time()
        if motion_end_t > self._last_seen_motion_end:
            self._last_seen_motion_end = motion_end_t
            self._motion_dirty = True

        # 运动刚结束 → 重置参考帧，不触发
        if self._motion_dirty:
            self._motion_dirty = False
            frame = self._camera.get_latest_frame()
            if frame is not None:
                self._reference = self._prepare_gray(frame)
                self._last_slow_update = time.monotonic()
            self._skip_next = True
            return

        # 外部要求跳过一次（resume/motion-end）
        if self._skip_next:
            self._skip_next = False
            frame = self._camera.get_latest_frame()
            if frame is not None:
                self._reference = self._prepare_gray(frame)
                self._last_slow_update = time.monotonic()
            return

        frame = self._camera.get_latest_frame()
        if frame is None or self._reference is None:
            return

        gray = self._prepare_gray(frame)

        # 帧差计算
        diff = cv2.absdiff(self._reference, gray)
        total = _RESIZE_W * _RESIZE_H
        changed = int(np.count_nonzero(diff > self._pixel_threshold))
        fraction = changed / total

        now = time.monotonic()

        if fraction >= self._change_threshold:
            # 显著变化 → 触发
            self._reference = gray
            self._last_slow_update = now

            if fraction >= 0.40:
                desc = "画面发生了很大变化（可能有人出现或离开）"
            else:
                desc = "画面有明显变化（可能有人走动或物体移动）"
            logger.info("👁 帧差触发: %.1f%% 像素变化 — %s", fraction * 100, desc)

            if self.on_change:
                # 立即拍快照（避免入队延迟导致画面过时）
                snapshot = self._camera.take_snapshot()
                if snapshot:
                    await self.on_change(snapshot)

        elif fraction >= 0.05:
            # 中等变化 → 仅日志，更新参考帧防累积
            logger.debug("👁 帧差 %.1f%%（未达触发门槛 %.0f%%）",
                         fraction * 100, self._change_threshold * 100)
            self._reference = gray
            self._last_slow_update = now

        else:
            # 微小变化 → 慢更新参考帧
            if now - self._last_slow_update >= _SLOW_UPDATE_INTERVAL:
                self._reference = gray
                self._last_slow_update = now
