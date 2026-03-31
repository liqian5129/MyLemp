"""
摄像头采集模块

在背景线程中持续采集帧，缓存最新帧供 take_snapshot() 主动抓取。
帧差检测已移除——智能体通过 take_photo 工具自主决定何时观察。

设计原则：
  - 独立线程，不阻塞 asyncio 主循环
  - take_snapshot() 可在任意时刻调用，返回临时 jpg 路径（调用方负责删除）
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_FPS = 1.0   # 采集帧率（1 fps 足够保持最新帧）


class CameraCapture:
    """
    后台摄像头采集，提供 take_snapshot() 供智能体主动抓帧。

    用法：
        camera = CameraCapture(device_id=0, flip=False)
        camera.start()
        path = camera.take_snapshot()   # 返回临时 jpg 路径
        ...
        camera.stop()
    """

    def __init__(
        self,
        device_id:   int   = 0,
        capture_fps: float = DEFAULT_FPS,
        flip:        bool  = False,
    ):
        self._device_id  = device_id
        self._interval   = 1.0 / max(capture_fps, 0.1)
        self._flip       = flip
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

        self._latest_frame: np.ndarray | None = None
        self._frame_lock   = threading.Lock()

    def start(self):
        self._thread = threading.Thread(
            target=self._run, name="camera-capture", daemon=True
        )
        self._thread.start()
        logger.info("📷 CameraCapture 启动 (device=%d)", self._device_id)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        logger.info("📷 CameraCapture 已停止")

    # ── 采集线程 ──────────────────────────────────────────────────────────────

    def _run(self):
        cap = cv2.VideoCapture(self._device_id)
        if not cap.isOpened():
            logger.error("❌ 无法打开摄像头 device_id=%d", self._device_id)
            return

        logger.info("📷 摄像头已打开 device_id=%d", self._device_id)
        try:
            while not self._stop_event.is_set():
                t0 = time.perf_counter()

                ret, frame = cap.read()
                if not ret:
                    logger.warning("摄像头读取失败，等待重试...")
                    time.sleep(1.0)
                    continue

                if self._flip:
                    frame = cv2.flip(frame, -1)

                with self._frame_lock:
                    self._latest_frame = frame.copy()

                elapsed = time.perf_counter() - t0
                wait = self._interval - elapsed
                if wait > 0:
                    self._stop_event.wait(timeout=wait)

        finally:
            cap.release()
            logger.info("📷 摄像头已释放")

    # ── 主动抓帧 ──────────────────────────────────────────────────────────────

    def take_snapshot(self) -> str | None:
        """主动抓取当前最新帧，保存为临时 jpg，返回路径（调用方负责删除）。
        需等摄像头线程已启动且至少采集过一帧，否则返回 None。
        """
        with self._frame_lock:
            frame = self._latest_frame
        if frame is None:
            logger.warning("take_snapshot: 尚无可用帧")
            return None
        try:
            h, w = frame.shape[:2]
            if w > 640:
                scale = 640.0 / w
                frame = cv2.resize(frame, (640, int(h * scale)), interpolation=cv2.INTER_AREA)
            ok, jpg_buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
            if not ok:
                return None
            snap_dir = Path("snapshots")
            snap_dir.mkdir(exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")[:-3]  # 毫秒精度
            path = snap_dir / f"{ts}.jpg"
            path.write_bytes(jpg_buf.tobytes())
            logger.debug("📸 take_snapshot → %s", path)
            return str(path)
        except Exception as e:
            logger.error("take_snapshot 异常: %s", e)
            return None
