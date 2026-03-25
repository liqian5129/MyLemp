"""
运动执行器

下游处理链路（与 LLM 无关，纯数学）：
  关键帧 (4, N_JOINTS) → 插值器 → 安全裁剪 → 密集轨迹

基于设计文档 expressive_robot_motion_design_v2.md Section 5
"""
from __future__ import annotations

import numpy as np
from dataclasses import dataclass
from typing import Optional

# ── 硬件常量 ──────────────────────────────────────────────────────────────────

N_JOINTS = 5
K_FRAMES  = 4   # 固定 4 帧：f0（当前）+ f1 + f2（LLM生成）+ f3（结束）

JOINT_NAMES = ["base_yaw", "base_pitch", "elbow_pitch", "wrist_roll", "wrist_pitch"]

Q_MIN = np.array([ -5.0, -68.0,  50.0, -30.0,  -5.0], dtype=np.float32)
Q_MAX = np.array([ 14.0, -20.0, 100.0,  15.0,  68.0], dtype=np.float32)
Q_REST = np.array([  0.0, -44.0,  75.0,   0.0,  25.0], dtype=np.float32)

# 各关节最大速度 (deg/s)
DQ_MAX = np.array([60.0, 80.0, 80.0, 60.0, 80.0], dtype=np.float32)

# 相邻帧最大角度差
MAX_FRAME_DELTA = 55.0  # degrees


# ── 速度曲线 ──────────────────────────────────────────────────────────────────

def build_speed_profile(t_norm: float, accel_ratio: float, asymmetry: float) -> float:
    """
    三段梯形速度曲线，输入归一化时间 [0, 1]，输出运动进度 [0, 1]。

    asymmetry:
      +1 → 快起慢停（开心弹跳感）
      -1 → 慢起快停（悲伤拖沓感）
       0 → 对称钟形（平静）
    """
    decel_ratio = float(np.clip(accel_ratio * (1.0 + asymmetry), 0.05, 0.90 - accel_ratio))
    plateau = 1.0 - accel_ratio - decel_ratio

    if t_norm < accel_ratio:
        s = 0.5 * (t_norm / accel_ratio) ** 2
    elif t_norm < accel_ratio + plateau:
        s = 0.5 * accel_ratio + (t_norm - accel_ratio)
    else:
        d = t_norm - accel_ratio - plateau
        s = (0.5 * accel_ratio + plateau
             + decel_ratio
             - 0.5 * ((decel_ratio - d) / decel_ratio) ** 2 * decel_ratio)

    norm = 0.5 * accel_ratio + plateau + 0.5 * decel_ratio
    return float(np.clip(s / norm, 0.0, 1.0))


# ── 关键帧安全裁剪 ────────────────────────────────────────────────────────────

def clamp_frame_deltas(q_frames: np.ndarray) -> np.ndarray:
    """
    相邻帧角度差超出 MAX_FRAME_DELTA 时，对超出帧做线性收缩（保形，不截断）。
    输入/输出: (K_FRAMES, N_JOINTS)
    """
    q = q_frames.copy()
    for i in range(1, K_FRAMES):
        delta = q[i] - q[i - 1]
        max_delta = np.abs(delta).max()
        if max_delta > MAX_FRAME_DELTA:
            scale = MAX_FRAME_DELTA / max_delta
            q[i] = q[i - 1] + delta * scale
    return q


# ── 插值器 ────────────────────────────────────────────────────────────────────

def interpolate(
    q_frames: np.ndarray,
    duration: float,
    accel_ratio: float,
    asymmetry: float,
    dt: float = 0.033,
) -> tuple[np.ndarray, np.ndarray]:
    """
    (K_FRAMES, N_JOINTS) 关键帧 → 密集轨迹。

    返回:
        t_traj: (N,) 时间戳，秒
        q_traj: (N, N_JOINTS) 关节角度，度
    """
    q_frames = np.asarray(q_frames, dtype=np.float32)
    n = max(2, int(duration / dt))
    t_norm_arr = np.linspace(0.0, 1.0, n)

    q_traj = np.zeros((n, N_JOINTS), dtype=np.float32)
    for idx, t_norm in enumerate(t_norm_arr):
        s = build_speed_profile(t_norm, accel_ratio, asymmetry)
        seg_f = s * (K_FRAMES - 1)
        seg_i = int(np.clip(np.floor(seg_f), 0, K_FRAMES - 2))
        alpha  = seg_f - seg_i
        q_traj[idx] = (1 - alpha) * q_frames[seg_i] + alpha * q_frames[seg_i + 1]

    t_traj = np.linspace(0.0, duration, n)
    return t_traj, q_traj


# ── 速度安全裁剪（时间拉伸，保角度形状）────────────────────────────────────────

def velocity_safe_stretch(
    t_traj: np.ndarray,
    q_traj: np.ndarray,
    dq_max: Optional[np.ndarray] = None,
) -> tuple[np.ndarray, np.ndarray]:
    """
    超速时拉伸时间轴，角度完全不变（保形）。
    不会截断运动幅度，只会让动作变慢。
    """
    if dq_max is None:
        dq_max = DQ_MAX

    dt_arr  = np.diff(t_traj)
    dq_arr  = np.abs(np.diff(q_traj, axis=0))             # (N-1, N_JOINTS)
    dt_min  = (dq_arr / (dq_max + 1e-6)).max(axis=1)      # (N-1,)
    dt_safe = np.maximum(dt_arr, dt_min * 1.05)            # 5% margin

    t_safe = np.concatenate([[0.0], np.cumsum(dt_safe)])
    return t_safe, q_traj  # 角度完全不变


# ── 完整组装 ──────────────────────────────────────────────────────────────────

def assemble_trajectory(
    f0: np.ndarray,
    f1: np.ndarray,
    f2: np.ndarray,
    f3: np.ndarray,
    duration: float,
    accel_ratio: float,
    asymmetry: float,
    dt: float = 0.033,
) -> tuple[np.ndarray, np.ndarray]:
    """
    从 4 帧组装完整密集轨迹。

    f0: 当前姿态（编码器读数）
    f1, f2: LLM 或模板生成的关键帧
    f3: 结束回归姿态（通常为 Q_REST）

    返回:
        t_traj: (N,) 时间戳
        q_traj: (N, N_JOINTS) 轨迹，夹紧到限位
    """
    q_raw = np.stack([
        np.asarray(f0, dtype=np.float32),
        np.asarray(f1, dtype=np.float32),
        np.asarray(f2, dtype=np.float32),
        np.asarray(f3, dtype=np.float32),
    ], axis=0)  # (4, N_JOINTS)

    # 相邻帧约束
    q_raw = clamp_frame_deltas(q_raw)

    # 插值
    t_traj, q_traj = interpolate(q_raw, duration, accel_ratio, asymmetry, dt)

    # 速度安全拉伸
    t_traj, q_traj = velocity_safe_stretch(t_traj, q_traj)

    # 位置限位
    q_traj = np.clip(q_traj, Q_MIN, Q_MAX)

    return t_traj, q_traj


# ── 控制器 ────────────────────────────────────────────────────────────────────

class MotionExecutor:
    """
    持有预计算密集轨迹，每帧按时间插值返回目标关节角度。

    设计为"编译期"与"运行期"分离：
    - 编译期（动作触发时，一次性）：assemble_trajectory → MotionExecutor
    - 运行期（每 ~33ms）：executor.step(t_elapsed) → action_dict
    """

    def __init__(self, t_traj: np.ndarray, q_traj: np.ndarray):
        self.t = t_traj
        self.q = q_traj

    @classmethod
    def from_frames(
        cls,
        f0: np.ndarray,
        f1: np.ndarray,
        f2: np.ndarray,
        f3: Optional[np.ndarray] = None,
        duration: float = 2.0,
        accel_ratio: float = 0.2,
        asymmetry: float = 0.0,
        dt: float = 0.033,
    ) -> "MotionExecutor":
        if f3 is None:
            f3 = Q_REST
        t_traj, q_traj = assemble_trajectory(f0, f1, f2, f3, duration, accel_ratio, asymmetry, dt)
        return cls(t_traj, q_traj)

    @property
    def total_duration(self) -> float:
        return float(self.t[-1])

    def step(self, t_elapsed: float) -> Optional[dict]:
        """
        按经过时间返回目标关节角度字典。
        轨迹结束返回 None。
        """
        if t_elapsed >= self.t[-1]:
            return None

        action = {}
        for j, name in enumerate(JOINT_NAMES):
            val = float(np.interp(t_elapsed, self.t, self.q[:, j]))
            action[f"{name}.pos"] = val
        return action
