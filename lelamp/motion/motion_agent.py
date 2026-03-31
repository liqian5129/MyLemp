"""
Motion Agent — 统一动作服务

提供两类动作：
  1. 情绪动作（play_emotion）  调用 MOTION_REGISTRY 中编程固定动作
  2. 关节动作（play_waypoint） 直接指定关节目标值

状态机（两态）：
  idle    → 完全静止，舵机维持当前位置
  playing → 逐帧播放动画，结束后自动衔接队列（深度 1）

帧格式：dict，键为关节名（不带 .pos 后缀）
  {"base_yaw": v, "base_pitch": v, "elbow_pitch": v, "wrist_roll": v, "wrist_pitch": v}
"""
from __future__ import annotations

import csv
import logging
import os
import threading
import time
from typing import Optional

from lelamp.service.motors.motion_scripts import MOTION_REGISTRY, _build_frames, HOME_POS

logger = logging.getLogger(__name__)

DEFAULT_FPS   = 30
IDLE_HOLD_SEC = 30.0   # 动作结束后静止保持时长

_JOINT_KEYS = frozenset({"base_yaw", "base_pitch", "elbow_pitch", "wrist_roll", "wrist_pitch"})


class MotionAgent:
    """
    统一运动服务。

    用法：
        agent = MotionAgent(port="/dev/ttyUSB0", lamp_id="lelamp", llm=llm_client)
        agent.start()
        agent.play_emotion("happy_wiggle")
        await agent.play_functional("鞠躬致谢")
        agent.stop()
    """

    def __init__(
        self,
        port:    str,
        lamp_id: str,
        fps:     int = DEFAULT_FPS,
    ):
        self.port    = port
        self.lamp_id = lamp_id
        self.fps     = fps
        self._dt     = 1.0 / fps

        # 状态机
        self._mode:        str            = "idle"
        self._frames:      list[dict]     = []
        self._frame_idx:   int            = 0
        self._next_frames: Optional[list[dict]] = None

        # Idle 状态
        self._idle_hold_until: float = 0.0   # 保持期截止时间戳（期间完全静止）

        self._attitude: float = 0.0

        # 轨迹录制
        self._csv_writer = None
        self._csv_file   = None
        self._csv_t0     = 0.0

        # 线程
        self._lock    = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.robot    = None

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    def start(self):
        from lelamp.follower import LeLampFollowerConfig, LeLampFollower
        config = LeLampFollowerConfig(port=self.port, id=self.lamp_id)
        self.robot = LeLampFollower(config)
        self.robot.connect(calibrate=False)

        current = self._get_current_pos()
        self._idle_hold_until = 0.0
        logger.info(
            "📐 启动角度  yaw=%.1f pitch=%.1f elbow=%.1f roll=%.1f wrist=%.1f",
            current["base_yaw"], current["base_pitch"], current["elbow_pitch"],
            current["wrist_roll"], current["wrist_pitch"],
        )

        self._running = True
        self._thread  = threading.Thread(
            target=self._run_loop, daemon=True, name="motion-agent"
        )
        self._thread.start()
        logger.info("✨ MotionAgent 已启动  port=%s  fps=%d", self.port, self.fps)

    def stop(self, timeout: float = 3.0):
        self._running = False
        if self._thread:
            self._thread.join(timeout=timeout)
        if self.robot:
            self.robot.disconnect()
            self.robot = None
        logger.info("🛑 MotionAgent 已停止")

    # ── 公共接口 ──────────────────────────────────────────────────────────────

    def play_emotion(self, name: str):
        """触发 MOTION_REGISTRY 动作（同步，立即返回）"""
        if name not in MOTION_REGISTRY:
            logger.warning("未知情绪动作: %s，可用: %s", name, list(MOTION_REGISTRY))
            return
        current = self._get_current_pos()
        frames  = MOTION_REGISTRY[name](current)
        self._enqueue_frames(frames)
        logger.info("🎭 情绪动作: %s  %d frames", name, len(frames))

    def is_playing(self) -> bool:
        """是否正在播放动作"""
        with self._lock:
            return self._mode == "playing"

    def get_status_str(self) -> str:
        """返回当前运动状态描述，注入 think prompt"""
        with self._lock:
            if self._mode == "playing":
                return "正在执行动作中（如无必要勿打断）"
            return "空闲，可以自由运动"

    def play_waypoint(self, joints: dict, duration: float = 1.0):
        """直接从关节目标值生成帧并入队，无需 LLM。
        若指定了 base_yaw，同步更新 _attention_target，使 idle 保持该方向直到下一个动作。
        """
        clean = {k: float(v) for k, v in joints.items() if k in _JOINT_KEYS and v is not None}
        if not clean:
            logger.warning("play_waypoint: 无有效关节")
            return
        current  = self._get_current_pos()
        duration = max(0.3, min(5.0, float(duration)))
        frames   = _build_frames(current, [(clean, duration)])
        if not frames:
            return
        self._enqueue_frames(frames)
        logger.info(
            "🎯 body_move: %s  dur=%.1fs  %d frames",
            list(clean.keys()), duration, len(frames),
        )

    def set_attitude(self, score: float):
        with self._lock:
            self._attitude = float(max(-1.0, min(1.0, score)))

    async def wait_done(self, timeout: float = 8.0) -> bool:
        """异步等待当前动作播放完毕。返回 True=正常完成，False=超时"""
        import asyncio
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if self._mode == "idle":
                    return True
            await asyncio.sleep(0.05)
        logger.warning("wait_done 超时 (%.1fs)", timeout)
        return False

    # ── 内部：帧入队 ──────────────────────────────────────────────────────────

    def _enqueue_frames(self, frames: list[dict]):
        with self._lock:
            if self._mode == "idle":
                self._frames      = frames
                self._frame_idx   = 0
                self._next_frames = None
                self._mode        = "playing"
            else:
                # 替换 pending（避免堆积，丢弃旧 pending）
                self._next_frames = frames

    # ── 30fps 控制循环 ────────────────────────────────────────────────────────

    def _run_loop(self):
        logger.info("🔄 控制循环启动 @ %d fps", self.fps)
        while self._running:
            t0     = time.perf_counter()
            t_wall = time.time()

            with self._lock:
                mode      = self._mode
                frame_idx = self._frame_idx
                total     = len(self._frames)

            if mode == "playing":
                if frame_idx < total:
                    with self._lock:
                        frame = self._frames[frame_idx]
                        self._frame_idx += 1
                    self._send_pos(frame)
                else:
                    # 动画结束，检查队列
                    with self._lock:
                        next_f            = self._next_frames
                        self._next_frames = None

                    if next_f is not None:
                        with self._lock:
                            self._frames     = next_f
                            self._frame_idx  = 0
                        logger.debug("▶ 衔接下一动作  %d frames", len(next_f))
                    else:
                        with self._lock:
                            self._mode            = "idle"
                            self._idle_hold_until = time.time() + IDLE_HOLD_SEC
                        logger.debug("▶ playing→idle  hold=%.0fs", IDLE_HOLD_SEC)
            else:
                self._run_idle_step(t_wall)

            elapsed = time.perf_counter() - t0
            sleep_t = self._dt - elapsed
            if sleep_t > 0:
                time.sleep(sleep_t)

    def _run_idle_step(self, t: float):
        """Idle 模式：完全静止，舵机维持当前位置，不发任何命令。"""
        pass

    # ── 工具方法 ──────────────────────────────────────────────────────────────

    def _get_current_pos(self) -> dict:
        """读取编码器当前位置；失败时返回 HOME_POS"""
        try:
            if self.robot is not None:
                obs = self.robot.get_observation()
                return {n: float(obs.get(f"{n}.pos", HOME_POS[n])) for n in HOME_POS}
        except Exception as e:
            logger.debug("读取关节位置失败（使用 HOME_POS）: %s", e)
        return dict(HOME_POS)

    def _send_pos(self, pos: dict):
        """将 dict 格式位置发送给机器人"""
        action = {f"{name}.pos": pos[name] for name in HOME_POS}
        try:
            if self.robot is not None:
                self.robot.send_action(action)
        except Exception as exc:
            logger.warning("send_action 失败: %s", exc)

        # 轨迹录制（环境变量 MOTION_RECORD=1 时启用）
        if self._csv_writer is not None:
            t = (time.perf_counter() - self._csv_t0) * 1000
            with self._lock:
                mode = self._mode
            self._csv_writer.writerow(
                [f"{t:.1f}", mode] + [f"{pos[n]:.2f}" for n in HOME_POS]
            )

    # ── 轨迹录制 ──────────────────────────────────────────────────────────────

    def start_recording(self, path: str = "motion_record.csv"):
        """开始记录每帧发送的关节指令到 CSV"""
        self._csv_file   = open(path, "w", newline="")
        self._csv_writer = csv.writer(self._csv_file)
        self._csv_t0     = time.perf_counter()
        self._csv_writer.writerow(["t_ms", "mode"] + list(HOME_POS.keys()))
        logger.info("🎬 轨迹录制开始 → %s", path)

    def stop_recording(self):
        """停止录制并关闭文件"""
        if self._csv_writer is not None:
            self._csv_writer = None
            self._csv_file.close()
            self._csv_file = None
            logger.info("🎬 轨迹录制结束")

