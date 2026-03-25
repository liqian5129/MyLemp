"""
动作原语库（5 关节适配版）

关节顺序：[base_yaw, base_pitch, elbow_pitch, wrist_roll, wrist_pitch]
限位：
  base_yaw    [-5,  14]   正=右转
  base_pitch  [-68, -20]  负=抬起（越负越高）
  elbow_pitch [50, 100]   正=弯曲
  wrist_roll  [-30,  15]  正=右歪
  wrist_pitch [-5,  68]   正=抬起

delta_frames 为增量角度（相对动作起始位置的 Δq），不是绝对角度。
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import List

import numpy as np

# ── 硬件常量 ──────────────────────────────────────────────────────────────────

N_JOINTS = 5
JOINT_NAMES = ["base_yaw", "base_pitch", "elbow_pitch", "wrist_roll", "wrist_pitch"]

Q_MIN  = np.array([ -5.0, -68.0,  50.0, -30.0,  -5.0], dtype=np.float32)
Q_MAX  = np.array([ 14.0, -20.0, 100.0,  15.0,  68.0], dtype=np.float32)
Q_REST = np.array([  0.0, -44.0,  75.0,   0.0,  25.0], dtype=np.float32)

# 安全增量上限（deg）：增量绝对值不超过此值
MAX_DELTA = np.array([10.0, 18.0, 20.0, 12.0, 30.0], dtype=np.float32)


# ── 类型定义 ──────────────────────────────────────────────────────────────────

class PrimitiveType(str, Enum):
    HIT    = "HIT"
    GROOVE = "GROOVE"
    FLOW   = "FLOW"
    FREEZE = "FREEZE"


@dataclass
class MotionPrimitive:
    id:           str
    type:         PrimitiveType
    delta_frames: List[List[float]]  # (K, 5) 增量关键帧
    frame_times:  List[float]        # (K,) 归一化时间 [0, 1]
    duration:     str                # "half" | "one" | "two"
    energy_level: str                # "low" | "mid" | "high"
    tags:         List[str] = field(default_factory=list)
    weight:       float = 1.0


# ── 方向模板（归一化，各分量 ∈ [-1, 1]）────────────────────────────────────────
# 乘以振幅后得到增量 deg

DIRECTION_PROFILES: dict[str, np.ndarray] = {
    # 上抬：base_pitch 负向（抬起）+ wrist_pitch 正向（头抬起）
    "up":          np.array([ 0.0, -1.0,  0.0,  0.0,  0.8], dtype=np.float32),
    # 低垂：base_pitch 正向（下沉）+ wrist_pitch 负向
    "down":        np.array([ 0.0,  0.6,  0.3,  0.0, -0.5], dtype=np.float32),
    # 前探：elbow 弯曲 + 轻微低头
    "forward":     np.array([ 0.0,  0.3,  1.0,  0.0,  0.3], dtype=np.float32),
    # 后仰：elbow 伸展 + 轻微仰头
    "backward":    np.array([ 0.0, -0.4, -1.0,  0.0, -0.2], dtype=np.float32),
    # 左转：base_yaw 负（左）+ wrist_roll 轻微
    "left":        np.array([-1.0,  0.0,  0.0,  0.2,  0.0], dtype=np.float32),
    # 右转：base_yaw 正（右）
    "right":       np.array([ 1.0,  0.0,  0.0, -0.2,  0.0], dtype=np.float32),
    # 左歪：wrist_roll 负（左倾）
    "twist_left":  np.array([-0.3,  0.0,  0.0, -1.0,  0.0], dtype=np.float32),
    # 右歪：wrist_roll 正（右倾）
    "twist_right": np.array([ 0.3,  0.0,  0.0,  1.0,  0.0], dtype=np.float32),
    # 弹跳：base_pitch 快速上下 + head 跟随
    "bounce":      np.array([ 0.0, -0.9,  0.2,  0.0,  0.7], dtype=np.float32),
    # 点头：主要靠 wrist_pitch
    "nod":         np.array([ 0.0,  0.2,  0.2,  0.0,  1.0], dtype=np.float32),
}

_ZERO = [0.0] * N_JOINTS


def _clip_delta(delta: np.ndarray) -> List[float]:
    """裁剪增量到安全范围"""
    return np.clip(delta, -MAX_DELTA, MAX_DELTA).tolist()


# ── 参数化生成器 ───────────────────────────────────────────────────────────────

def generate_hit(
    direction: str,
    amplitude: float,
    energy: str = "mid",
    accent_pos: float = 0.30,
    uid: str | None = None,
) -> MotionPrimitive:
    """
    HIT：单拍冲击，快速到达重音帧后回归。
    accent_pos: 重音帧位置（归一化，~0.30 = 突然感）
    """
    d = DIRECTION_PROFILES.get(direction, DIRECTION_PROFILES["up"]) * amplitude
    partial = d * 0.25

    return MotionPrimitive(
        id=uid or f"hit_{direction}_{amplitude:.0f}",
        type=PrimitiveType.HIT,
        delta_frames=[_ZERO, _clip_delta(d), _clip_delta(partial), _ZERO],
        frame_times=[0.0, accent_pos, 0.65, 1.0],
        duration="one",
        energy_level=energy,
        tags=[direction, "impact"],
        weight=1.0,
    )


def generate_groove(
    direction: str,
    amplitude: float,
    energy: str = "mid",
    uid: str | None = None,
) -> MotionPrimitive:
    """
    GROOVE：双拍振荡，来回摆动。
    """
    d    = DIRECTION_PROFILES.get(direction, DIRECTION_PROFILES["up"]) * amplitude
    neg  = -d * 0.5

    return MotionPrimitive(
        id=uid or f"groove_{direction}_{amplitude:.0f}",
        type=PrimitiveType.GROOVE,
        delta_frames=[_ZERO, _clip_delta(d), _ZERO, _clip_delta(neg), _ZERO],
        frame_times=[0.0, 0.25, 0.50, 0.75, 1.0],
        duration="two",
        energy_level=energy,
        tags=[direction, "oscillation"],
        weight=1.0,
    )


def generate_freeze(
    energy: str = "low",
    uid: str | None = None,
) -> MotionPrimitive:
    """
    FREEZE：静止保持，仅有极微小位移。
    """
    tiny = [0.5, 0.0, 0.0, 0.0, 0.5]

    return MotionPrimitive(
        id=uid or f"freeze_{energy}",
        type=PrimitiveType.FREEZE,
        delta_frames=[_ZERO, tiny, tiny, _ZERO],
        frame_times=[0.0, 0.30, 0.70, 1.0],
        duration="two",
        energy_level=energy,
        tags=["hold", "static"],
        weight=1.0,
    )


def generate_flow(
    dir1: str,
    dir2: str,
    amplitude: float,
    energy: str = "mid",
    uid: str | None = None,
) -> MotionPrimitive:
    """
    FLOW：双拍流畅弧线，两个方向平滑过渡。
    """
    d1 = DIRECTION_PROFILES.get(dir1, DIRECTION_PROFILES["up"]) * amplitude
    d2 = DIRECTION_PROFILES.get(dir2, DIRECTION_PROFILES["nod"]) * amplitude * 0.7

    return MotionPrimitive(
        id=uid or f"flow_{dir1}_{dir2}_{amplitude:.0f}",
        type=PrimitiveType.FLOW,
        delta_frames=[_ZERO, _clip_delta(d1), _clip_delta(d2), _ZERO],
        frame_times=[0.0, 0.35, 0.70, 1.0],
        duration="two",
        energy_level=energy,
        tags=[dir1, dir2, "smooth"],
        weight=1.0,
    )


# ── 批量生成库 ─────────────────────────────────────────────────────────────────

def batch_generate_library() -> list[MotionPrimitive]:
    """
    生成涵盖三种能量等级的完整原语库（约 30 条）。
    可用于实机筛选前的默认库。
    """
    lib: list[MotionPrimitive] = []

    # ── HIT：高能 ──
    for direction, amp in [("bounce", 14), ("up", 16), ("forward", 12)]:
        lib.append(generate_hit(direction, amp, energy="high", accent_pos=0.28))

    # ── HIT：中能 ──
    for direction, amp in [("up", 10), ("left", 9), ("right", 9), ("nod", 12), ("bounce", 10)]:
        lib.append(generate_hit(direction, amp, energy="mid", accent_pos=0.30))

    # ── HIT：低能 ──
    for direction, amp in [("nod", 7), ("up", 7), ("twist_left", 8)]:
        lib.append(generate_hit(direction, amp, energy="low", accent_pos=0.32))

    # ── GROOVE：高能 ──
    for direction, amp in [("bounce", 12), ("left", 8)]:
        p = generate_groove(direction, amp, energy="high")
        p.weight = 1.2
        lib.append(p)

    # ── GROOVE：中能 ──
    for direction, amp in [("left", 7), ("up", 9), ("bounce", 8), ("nod", 10)]:
        lib.append(generate_groove(direction, amp, energy="mid"))

    # ── GROOVE：低能 ──
    for direction, amp in [("up", 5), ("nod", 6)]:
        p = generate_groove(direction, amp, energy="low")
        p.weight = 0.8
        lib.append(p)

    # ── FLOW ──
    for dir1, dir2, amp, energy in [
        ("up",      "nod",     10, "mid"),
        ("forward", "up",       9, "mid"),
        ("left",    "right",    8, "mid"),
        ("up",      "forward", 12, "high"),
        ("nod",     "up",       6, "low"),
    ]:
        lib.append(generate_flow(dir1, dir2, amp, energy=energy))

    # ── FREEZE ──
    lib.append(generate_freeze(energy="low",  uid="freeze_low"))
    lib.append(generate_freeze(energy="mid",  uid="freeze_mid"))

    return lib
