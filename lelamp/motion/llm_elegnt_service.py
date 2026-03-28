"""
LLM ELEGNT 运动服务（第 2 阶段）

架构：
  dispatch("emotion", {...})
      ↓ MotionService.generate()（缓存 → 模板，零延迟）
      ↓ MotionExecutor.from_frames(f0, f1, f2, f3)
      ↓ 30fps 控制循环

三态状态机：
  playing   → 执行关键帧动画（支持执行队列，实现 hint → 正式动作衔接）
  lingering → 停留在 f3 位置（最多 LINGER_TIMEOUT 秒）
  idle      → 多频正弦叠加呼吸微动

执行队列：
  _executor_queue 允许提前排入下一个 MotionExecutor。
  当前动作结束时自动无缝衔接（用于 hint + 正式动作连续播放）。

与 ELEGNTService 的 dispatch 接口完全兼容。
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from typing import Any, Optional

import numpy as np

from lelamp.motion.motion_executor import (
    MotionExecutor, Q_REST, Q_MIN, Q_MAX, JOINT_NAMES,
)
from lelamp.motion.templates import template_generate, EMOTION_INDEX, MotionKeyframes
from lelamp.motion.motion_service import MotionService

logger = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────────────

DEFAULT_FPS    = 30
LINGER_TIMEOUT = 4.0     # 秒：lingering 超过此时间后漂回 idle
MAX_ATTN_SPEED = 20.0    # deg/s：attention 追踪最大速度
IDLE_DRIFT_RATE = 0.03   # deg/帧：idle 时向 Q_REST 漂移速率

# 呼吸微动振幅（度）—— 叠加 4 个正弦，产生有机不规律感
# 各项: (振幅, 频率Hz, 相位rad)
_BREATH_PARAMS: list[tuple[int, float, float, float]] = [
    # joint_idx, amplitude, freq_Hz, phase_rad
    (1, 2.5,  0.18, 0.0),             # base_pitch:  主呼吸
    (4, 1.5,  0.13, math.pi / 4),     # wrist_pitch: 头微俯仰
    (3, 1.0,  0.23, math.pi / 2),     # wrist_roll:  轻微歪头
    # base_yaw (0) 由 attention 单独管理，不参与呼吸
    # elbow_pitch (2) 保持不动
]


class LLMELEGNTService:
    """
    LLM 驱动的情绪运动服务。

    用法与 ELEGNTService 完全一致：
        service = LLMELEGNTService(port="...", lamp_id="...")
        service.start()
        service.dispatch("emotion", {"emotion": "happy", "intensity": 0.8})
        service.stop()
    """

    def __init__(
        self,
        port:           str,
        lamp_id:        str,
        fps:            int              = DEFAULT_FPS,
        motion_service: Optional[MotionService] = None,
        **kwargs,
    ):
        self.port    = port
        self.lamp_id = lamp_id
        self.fps     = fps
        self._dt     = 1.0 / fps

        # MotionService：缓存 → 模板（可选，None 时直接用模板）
        self._motion_service = motion_service or MotionService()

        # ── 运动状态机 ──────────────────────────────────────────────────────
        self._executor:       Optional[MotionExecutor]   = None
        self._exec_start:     float                      = 0.0
        self._executor_queue: deque[MotionKeyframes]     = deque()  # hint → 正式动作队列
        self._mode:           str                        = "idle"   # "idle"|"playing"|"lingering"
        self._q_linger:       np.ndarray                 = Q_REST.copy()
        self._linger_start:   float                      = 0.0
        self._q_idle_base:    np.ndarray                 = Q_REST.copy()

        # ── attention / attitude ────────────────────────────────────────────
        self._attention_target:  float = 0.0
        self._attention_current: float = 0.0
        self._attitude:          float = 0.0

        # ── 线程 ───────────────────────────────────────────────────────────
        self._lock    = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.robot = None

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    def start(self):
        from lelamp.follower import LeLampFollowerConfig, LeLampFollower
        config = LeLampFollowerConfig(port=self.port, id=self.lamp_id)
        self.robot = LeLampFollower(config)
        self.robot.connect(calibrate=False)

        self._running = True
        self._thread  = threading.Thread(
            target=self._run_loop, daemon=True, name="llm-elegnt-motion"
        )
        self._thread.start()
        logger.info("✨ LLMELEGNTService 已启动  port=%s  fps=%d", self.port, self.fps)

    def stop(self, timeout: float = 3.0):
        self._running = False
        if self._thread:
            self._thread.join(timeout=timeout)
        if self.robot:
            self.robot.disconnect()
            self.robot = None
        logger.info("🛑 LLMELEGNTService 已停止")

    # ── 公共接口（与 ELEGNTService 完全兼容）─────────────────────────────────

    def dispatch(self, event_type: str, payload: Any):
        if event_type == "emotion":
            if isinstance(payload, dict):
                emotion   = payload.get("emotion", "calm")
                intensity = float(payload.get("intensity", 0.8))
                attn      = payload.get("attention_yaw", None)
            else:
                emotion, intensity, attn = str(payload), 0.8, None

            self._trigger_emotion(emotion, intensity)
            if attn is not None:
                self.set_attention(float(attn))

        elif event_type == "attention":
            self.set_attention(float(payload))
        elif event_type == "attitude":
            self.set_attitude(float(payload))

    def set_attention(self, yaw: float):
        with self._lock:
            self._attention_target = float(np.clip(yaw, -5.0, 14.0))

    def set_attitude(self, score: float):
        with self._lock:
            self._attitude = float(np.clip(score, -1.0, 1.0))

    def get_available_emotions(self) -> list[str]:
        return list(EMOTION_INDEX)

    # ── 内部：情绪触发 ────────────────────────────────────────────────────────

    def _trigger_emotion(self, emotion: str, intensity: float):
        """
        生成关键帧，立刻切换到 playing 模式（无论当前状态）。

        流程：
          1. 立即播放 hint 微动（curious @ 低强度，约 0.5s），给用户"注意到了"的反馈
          2. 从 MotionService 获取正式动作（缓存命中时零延迟）
          3. 将正式动作排入队列，hint 结束后自动衔接
        """
        if emotion not in EMOTION_INDEX:
            logger.warning("未知情绪 '%s'，可用: %s", emotion, EMOTION_INDEX)
            return

        f0 = self._get_current_q()

        # ── hint 微动：立刻给用户反馈 ────────────────────────────────────
        hint_kf = template_generate("curious", 0.25)
        hint_executor = MotionExecutor.from_frames(
            f0=f0,
            f1=hint_kf.f1,
            f2=hint_kf.f2,
            f3=hint_kf.f3,
            duration=min(hint_kf.duration, 0.6),   # 强制短时
            accel_ratio=0.20,
            asymmetry=0.3,
        )

        # ── 正式动作：MotionService（缓存 → 模板） ───────────────────────
        kf: MotionKeyframes = self._motion_service.generate(emotion, intensity)
        logger.info("🎭 %s @ %.1f  [%s]  cache=%s",
                    emotion, intensity, kf.intent, self._motion_service.has_cache)

        with self._lock:
            self._executor_queue.clear()            # 清空旧队列
            self._executor       = hint_executor    # hint 先播
            self._exec_start     = time.perf_counter()
            self._mode           = "playing"
            self._executor_queue.append(kf)         # 正式动作（MotionKeyframes）排队

    # ── 30fps 控制循环 ────────────────────────────────────────────────────────

    def _run_loop(self):
        logger.info("🔄 控制循环启动 @ %d fps", self.fps)
        while self._running:
            t0     = time.perf_counter()
            t_wall = time.time()

            with self._lock:
                mode         = self._mode
                executor     = self._executor
                exec_start   = self._exec_start
                q_linger     = self._q_linger.copy()
                linger_start = self._linger_start

            # ── playing ───────────────────────────────────────────────────
            if mode == "playing" and executor is not None:
                t_elapsed = time.perf_counter() - exec_start
                action    = executor.step(t_elapsed)

                if action is None:
                    # 当前动作结束，检查队列
                    with self._lock:
                        next_kf = self._executor_queue.popleft() if self._executor_queue else None

                    if next_kf is not None:
                        # 从当前实际位置无缝衔接下一个动作
                        q_now      = self._get_current_q()
                        next_exec  = MotionExecutor.from_frames(
                            f0=q_now,
                            f1=next_kf.f1,
                            f2=next_kf.f2,
                            f3=next_kf.f3,
                            duration=next_kf.duration,
                            accel_ratio=next_kf.accel_ratio,
                            asymmetry=next_kf.asymmetry,
                        )
                        with self._lock:
                            self._executor   = next_exec
                            self._exec_start = time.perf_counter()
                        logger.debug("▶ 队列衔接：hint → 正式动作")
                    else:
                        # 队列空 → 进入 lingering
                        q_end = self._get_current_q()
                        with self._lock:
                            self._mode         = "lingering"
                            self._q_linger     = q_end
                            self._linger_start = t_wall
                            self._executor     = None
                        logger.debug("▶ playing 结束 → lingering")
                else:
                    self._send(action)

            # ── lingering ─────────────────────────────────────────────────
            elif mode == "lingering":
                if t_wall - linger_start >= LINGER_TIMEOUT:
                    with self._lock:
                        self._mode         = "idle"
                        self._q_idle_base  = q_linger.copy()  # 从停留位置开始漂
                    logger.debug("▶ lingering 超时 → idle")
                else:
                    self._run_lingering_step(q_linger, t_wall)

            # ── idle ──────────────────────────────────────────────────────
            else:
                self._run_idle_step(t_wall)

            elapsed = time.perf_counter() - t0
            sleep_t = self._dt - elapsed
            if sleep_t > 0:
                time.sleep(sleep_t)

    # ── 状态步进 ──────────────────────────────────────────────────────────────

    def _run_lingering_step(self, q_linger: np.ndarray, t: float):
        """在 f3 停留位置叠加轻微呼吸，等待下一条指令"""
        q = q_linger.copy()
        q += self._breathing_offset(t)
        q[0] = self._step_attention()   # base_yaw 由 attention 单独管理
        q    = np.clip(q, Q_MIN, Q_MAX)
        self._send_q(q)

    def _run_idle_step(self, t: float):
        """向 Q_REST 缓慢漂移 + 呼吸微动 + attitude 偏置"""
        with self._lock:
            q_base   = self._q_idle_base.copy()
            attitude = self._attitude

        # 每帧向 Q_REST 漂一小步
        diff     = Q_REST - q_base
        max_step = IDLE_DRIFT_RATE
        q_base  += np.clip(diff, -max_step, max_step)

        with self._lock:
            self._q_idle_base = q_base.copy()

        q  = q_base + self._breathing_offset(t) + self._attitude_offset(attitude)
        q[0] = self._step_attention()
        q    = np.clip(q, Q_MIN, Q_MAX)
        self._send_q(q)

    # ── 工具方法 ──────────────────────────────────────────────────────────────

    def _breathing_offset(self, t: float) -> np.ndarray:
        """4 个叠加正弦产生有机呼吸感"""
        result = np.zeros(5, dtype=np.float32)
        for joint_idx, amp, freq, phase in _BREATH_PARAMS:
            result[joint_idx] += amp * math.sin(2.0 * math.pi * freq * t + phase)
        return result

    def _attitude_offset(self, attitude: float) -> np.ndarray:
        """attitude 分数 [-1,1] 映射为姿态偏置"""
        result = np.zeros(5, dtype=np.float32)
        result[1] = attitude * (-5.0)   # base_pitch: 正=昂扬（更负=更高）
        result[4] = attitude * 8.0      # wrist_pitch: 正=头抬起
        return result

    def _step_attention(self) -> float:
        """平滑追踪 attention_target，返回当前 base_yaw 绝对值"""
        with self._lock:
            target  = self._attention_target
            current = self._attention_current

        diff       = target - current
        max_change = MAX_ATTN_SPEED * self._dt
        new_val    = current + float(np.clip(diff, -max_change, max_change))

        with self._lock:
            self._attention_current = new_val
        return new_val

    def _get_current_q(self) -> np.ndarray:
        """读取编码器当前角度，失败时返回 Q_REST"""
        try:
            if self.robot is not None:
                obs = self.robot.get_observation()
                return np.array([
                    obs.get("base_yaw.pos",    Q_REST[0]),
                    obs.get("base_pitch.pos",  Q_REST[1]),
                    obs.get("elbow_pitch.pos", Q_REST[2]),
                    obs.get("wrist_roll.pos",  Q_REST[3]),
                    obs.get("wrist_pitch.pos", Q_REST[4]),
                ], dtype=np.float32)
        except Exception as e:
            logger.debug("读取关节角度失败（使用 Q_REST）: %s", e)
        return Q_REST.copy()

    def _send_q(self, q: np.ndarray):
        """将 (5,) 角度数组发送给机器人"""
        action = {f"{name}.pos": float(q[i]) for i, name in enumerate(JOINT_NAMES)}
        self._send(action)

    def _send(self, action: dict):
        try:
            if self.robot is not None:
                self.robot.send_action(action)
        except Exception as exc:
            logger.warning("send_action 失败: %s", exc)
