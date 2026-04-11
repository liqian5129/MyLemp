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

import numpy as np

from lelamp.motion.motion_executor import JOINT_NAMES, velocity_safe_stretch
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
        self._io_lock = threading.Lock()  # 串口访问串行化（sync_read / sync_write）
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

    def play_emotion(self, name: str, intensity: float = 1.0,
                     jitter: float = 0.10, seed: Optional[int] = None):
        """触发 MOTION_REGISTRY 动作（同步，立即返回）。
        intensity: 0.3-1.5，控制幅度 + 速度（默认 1.0）
        jitter:    0.0-0.3，每段随机抖动比例（默认 0.10）
        seed:      可选，固定随机种子用于复现/测试
        """
        if name not in MOTION_REGISTRY:
            logger.warning("未知情绪动作: %s，可用: %s", name, list(MOTION_REGISTRY))
            return
        intensity = max(0.3, min(1.5, float(intensity)))
        jitter    = max(0.0, min(0.3, float(jitter)))
        current   = self._predicted_start_pos()
        frames    = MOTION_REGISTRY[name](current, intensity=intensity,
                                          jitter=jitter, seed=seed)
        frames    = self._velocity_audit(frames)
        self._enqueue_frames(frames)
        logger.info(
            "🎭 %s  intensity=%.2f  %d frames",
            name, intensity, len(frames),
        )

    def play_compose(self, intent: str, segments: list) -> Optional[str]:
        """compose_motion 工具入口：LLM 直接给出 segments 列表。
        返回错误字符串（用于回报 LLM）或 None（成功）。
        """
        from lelamp.motion.compose_motion import validate_segments

        seg_list, err = validate_segments(segments)
        if err:
            logger.warning("compose_motion 校验失败: %s", err)
            return f"segments 不合法：{err}"
        if seg_list is None:
            return "segments 校验返回空"

        current = self._predicted_start_pos()
        frames  = _build_frames(current, seg_list)
        if not frames:
            return "生成的帧序列为空"
        frames  = self._velocity_audit(frames)
        self._enqueue_frames(frames)
        logger.info(
            "🎨 compose_motion: %s  %d segs → %d frames",
            (intent or "")[:30], len(seg_list), len(frames),
        )
        return None

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
        current  = self._predicted_start_pos()
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

    def _velocity_audit(self, frames: list[dict]) -> list[dict]:
        """对 30fps 帧列表做速度安全检查。
        超速时通过 velocity_safe_stretch 拉伸时间轴并按 fps 重采样回来；
        正常情况直接返回原列表。
        A 路径（play_emotion）和 C 路径（play_compose）共用此方法。
        """
        if len(frames) < 2:
            return frames
        n = len(frames)
        t_arr = np.linspace(0.0, (n - 1) / self.fps, n, dtype=np.float32)
        q_arr = np.array(
            [[float(f.get(j, HOME_POS[j])) for j in JOINT_NAMES] for f in frames],
            dtype=np.float32,
        )
        # 注意：不能用 motion_executor 的 Q_MIN/Q_MAX 二次 clip，那是另一套
        # 废弃管道的坐标系（wrist_pitch 范围与本项目差 40°+）。
        # _build_frames 内部已用 ±92° 限位，这里只做时间拉伸即可。
        t_safe, q_safe = velocity_safe_stretch(t_arr, q_arr)

        # 没触发拉伸 → 直通
        if abs(float(t_safe[-1]) - float(t_arr[-1])) < 1e-3:
            return frames

        # 重采样回 fps
        total = float(t_safe[-1])
        n2 = max(2, int(total * self.fps))
        t_uniform = np.linspace(0.0, total, n2)
        out: list[dict] = []
        for i in range(n2):
            d = {}
            for j_idx, j_name in enumerate(JOINT_NAMES):
                d[j_name] = float(np.interp(t_uniform[i], t_safe, q_safe[:, j_idx]))
            out.append(d)
        logger.info("⚙️ velocity_audit  %d→%d frames  %.2fs→%.2fs",
                    n, n2, float(t_arr[-1]), total)
        return out

    def _get_current_pos(self) -> dict:
        """读取编码器当前位置；失败时返回 HOME_POS"""
        try:
            if self.robot is not None:
                with self._io_lock:
                    obs = self.robot.get_observation()
                return {n: float(obs.get(f"{n}.pos", HOME_POS[n])) for n in HOME_POS}
        except Exception as e:
            logger.debug("读取关节位置失败（使用 HOME_POS）: %s", e)
        return dict(HOME_POS)

    def _predicted_start_pos(self) -> dict:
        """预测下一个新动作起播时的位置（供 play_emotion / play_compose / play_waypoint 用）。

        - 若当前在 playing：返回当前 frames 的最后一帧
          （新动作会替换 pending，最终接在当前 frames 之后；
          以 frames 末尾为起点才能保证位置连续，避免动作衔接处跳变）
        - 若 idle：读真实编码器（保留漂移恢复路径）

        历史 bug：曾经直接用 _get_current_pos()，主线程读到的是入队"那一刻"的
        编码器值，而下一动作真正播放时编码器已被前一动作改变，导致前一动作 →
        新动作衔接处出现明显的位置跳变。
        """
        with self._lock:
            if self._mode == "playing" and self._frames:
                return dict(self._frames[-1])
        # 锁外做 IO，避免和 _io_lock 嵌套且不阻塞控制循环
        return self._get_current_pos()

    def _send_pos(self, pos: dict):
        """将 dict 格式位置发送给机器人"""
        action = {f"{name}.pos": pos[name] for name in HOME_POS}
        try:
            if self.robot is not None:
                with self._io_lock:
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

