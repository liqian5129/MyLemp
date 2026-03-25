"""
舞蹈执行层

BaseOscillation  — 持续正弦律动，叠加在所有动作之上（永不静止）
DanceExecutor    — 增量动作执行器，含 body wave 插值 + soft homing
"""
from __future__ import annotations

import logging
import time

import numpy as np

from .choreography import DUR_MAP, ScheduledMove
from .groove import apply_body_wave
from .primitives import JOINT_NAMES, N_JOINTS, Q_MIN, Q_MAX, Q_REST

logger = logging.getLogger(__name__)

# 中性姿态（关节限位中点，用于 soft homing 目标）
Q_NEUTRAL = (Q_MIN + Q_MAX) / 2.0

# 空闲时回归中性的速率（度/控制步）
SOFT_HOMING_RATE = 0.05


# ── 基础律动层 ────────────────────────────────────────────────────────────────

class BaseOscillation:
    """
    持续正弦律动，与 BPM 同频。
    振幅 ~4°，主要作用于 base_pitch（上下呼吸）和 wrist_pitch（头部跟随）。
    让机器人在不执行任何原语的间隙也不完全静止。
    """

    def __init__(self, amplitude: float = 4.0):
        self.amplitude = amplitude
        self.enabled   = True

    def get_offset(self, t: float, bpm: float) -> np.ndarray:
        """
        t:   当前时间（秒，建议用 time.time()）
        bpm: 当前 BPM
        返回 (5,) 各关节正弦偏移量（度）
        """
        if not self.enabled:
            return np.zeros(N_JOINTS, dtype=np.float32)

        freq  = bpm / 60.0
        phase = 2.0 * np.pi * freq * t
        a     = self.amplitude

        return np.array([
            a * 0.15 * np.sin(phase * 0.5),   # base_yaw:    半频微晃
            a * 1.0  * np.sin(phase),           # base_pitch:  主呼吸
            0.0,                                 # elbow_pitch: 静止
            a * 0.3  * np.sin(phase),           # wrist_roll:  轻微跟随
            a * 0.6  * np.sin(phase),           # wrist_pitch: 头部跟随
        ], dtype=np.float32)


# ── 执行器 ────────────────────────────────────────────────────────────────────

class DanceExecutor:
    """
    维护当前关节位置，将增量原语转换为绝对轨迹并发送给机器人。

    每 10ms（100Hz）调用一次 step()。

    关键机制：
      - 增量角度：动作总从当前实际位置出发，无需回中性
      - BaseOscillation：持续叠加基础律动
      - Body wave：各关节延迟插值（底部到头部的波浪传导）
      - Soft homing：空闲 1 秒后缓慢向中性漂回
    """

    def __init__(
        self,
        robot,
        dt:          float = 0.01,
        bpm:         float = 120.0,
        groove_style: str  = "tight",
    ):
        self.robot        = robot
        self.dt           = dt
        self.bpm          = bpm
        self.groove_style = groove_style

        self.active_move:     ScheduledMove | None = None
        self.move_start_time: float = 0.0
        self.q_base:          np.ndarray = Q_REST.copy()

        self.oscillation = BaseOscillation(amplitude=4.0)
        self.idle_frames = 0

    def update_bpm(self, bpm: float) -> None:
        self.bpm = bpm

    def start_move(self, scheduled: ScheduledMove) -> None:
        """开始执行一个调度动作（直接从当前关节位置出发）"""
        self.active_move     = scheduled
        self.move_start_time = time.time()
        self.q_base          = self._get_q()
        self.idle_frames     = 0
        logger.debug(
            "▶ 执行 %s [%s] energy=%s",
            scheduled.primitive.id,
            scheduled.primitive.type.value,
            scheduled.primitive.energy_level,
        )

    def step(self) -> None:
        """每 10ms 调用一次，计算并发送目标关节角度。"""
        now        = time.time()
        osc_offset = self.oscillation.get_offset(now, self.bpm)

        if self.active_move is not None:
            prim      = self.active_move.primitive
            beat_dur  = self.active_move.beat_dur
            total_dur = DUR_MAP[prim.duration] * beat_dur

            elapsed = now - self.move_start_time
            t_norm  = min(1.0, elapsed / total_dur)

            # Body wave 插值：每关节错峰延迟
            q_delta  = apply_body_wave(
                t_norm, total_dur,
                prim.frame_times, prim.delta_frames,
                self.bpm,
            )
            q_target = self.q_base + q_delta + osc_offset

            if t_norm >= 1.0:
                self.active_move = None

        else:
            # 空闲：基础律动 + soft homing
            self.idle_frames += 1
            q_current = self._get_q()

            homing = np.zeros(N_JOINTS, dtype=np.float32)
            if self.idle_frames > 100:   # 约 1 秒后开始回归
                diff   = Q_NEUTRAL - q_current
                homing = np.clip(diff, -SOFT_HOMING_RATE, SOFT_HOMING_RATE)

            q_target = q_current + osc_offset + homing

        q_target = np.clip(q_target, Q_MIN, Q_MAX)
        self._send(q_target)

    # ── 私有方法 ──────────────────────────────────────────────────────────────

    def _get_q(self) -> np.ndarray:
        """从机器人读取当前关节角度，失败时返回 Q_REST。"""
        try:
            obs = self.robot.get_observation()
            return np.array([
                obs.get("base_yaw.pos",    Q_REST[0]),
                obs.get("base_pitch.pos",  Q_REST[1]),
                obs.get("elbow_pitch.pos", Q_REST[2]),
                obs.get("wrist_roll.pos",  Q_REST[3]),
                obs.get("wrist_pitch.pos", Q_REST[4]),
            ], dtype=np.float32)
        except Exception as e:
            logger.debug("读取关节角度失败: %s", e)
            return Q_REST.copy()

    def _send(self, q: np.ndarray) -> None:
        """发送绝对目标角度到机器人。"""
        try:
            action = {f"{name}.pos": float(q[i]) for i, name in enumerate(JOINT_NAMES)}
            self.robot.send_action(action)
        except Exception as e:
            logger.debug("send_action 失败: %s", e)
