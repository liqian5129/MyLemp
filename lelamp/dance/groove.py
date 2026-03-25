"""
Groove 机制

两个独立组件：
  1. apply_groove      — 对重音帧施加随机微时值偏移（±4% 一拍）
  2. apply_body_wave   — 各关节错峰延迟，模拟能量从底部向上传导

关节顺序（5 维）：base_yaw, base_pitch, elbow_pitch, wrist_roll, wrist_pitch
"""
from __future__ import annotations

from typing import List

import numpy as np

# ── 微时值扰动 ────────────────────────────────────────────────────────────────

GROOVE_AMT = 0.04   # 最大扰动量（一拍的 4%）


def apply_groove(
    frame_times: List[float],
    groove_style: str = "tight",
) -> List[float]:
    """
    对重音帧（frame_times[1]）施加微小时值偏移。

    groove_style:
      "tight"     → 略微提前（-），街舞风格，紧张感
      "laid_back" → 略微延迟（+），R&B 风格，放松感
      "neutral"   → 随机双向
    """
    if groove_style == "tight":
        jitter = float(np.random.uniform(-GROOVE_AMT, -GROOVE_AMT * 0.1))
    elif groove_style == "laid_back":
        jitter = float(np.random.uniform(GROOVE_AMT * 0.1, GROOVE_AMT))
    else:
        jitter = float(np.random.uniform(-GROOVE_AMT, GROOVE_AMT * 0.5))

    new_times = list(frame_times)
    new_times[1] = float(np.clip(frame_times[1] + jitter, 0.10, 0.50))
    return new_times


# ── 身体波 ────────────────────────────────────────────────────────────────────

# 各关节相对 q0 的基准延迟（毫秒，BPM=100 时）
# 顺序：base_yaw, base_pitch, elbow_pitch, wrist_roll, wrist_pitch
BODY_WAVE_BASE_DELAYS_MS = np.array([0.0, 15.0, 30.0, 45.0, 60.0], dtype=np.float32)


def get_body_wave_delays(bpm: float) -> np.ndarray:
    """根据 BPM 动态缩放延迟量，快节拍时自动减弱。"""
    scale = min(1.0, 100.0 / max(bpm, 60.0))
    return BODY_WAVE_BASE_DELAYS_MS * scale


def apply_body_wave(
    t_norm: float,
    total_dur: float,
    frame_times: List[float],
    delta_frames: List[List[float]],
    bpm: float = 120.0,
) -> np.ndarray:
    """
    对每个关节使用略有不同的等效时间做插值，产生"波浪从底向上传导"效果。

    Args:
        t_norm:       当前动作进度 [0, 1]
        total_dur:    动作总时长（秒）
        frame_times:  关键帧归一化时间列表
        delta_frames: 关键帧增量角度列表 (K, 5)
        bpm:          当前 BPM

    Returns:
        (5,) 增量角度数组
    """
    delays_ms   = get_body_wave_delays(bpm)
    delays_norm = delays_ms / 1000.0 / max(total_dur, 0.1)
    frames_arr  = np.array(delta_frames, dtype=np.float32)  # (K, 5)
    result      = np.zeros(5, dtype=np.float32)

    for j in range(5):
        t_j = float(np.clip(t_norm - delays_norm[j], 0.0, 1.0))

        interpolated = False
        for i in range(len(frame_times) - 1):
            if frame_times[i] <= t_j <= frame_times[i + 1]:
                denom = frame_times[i + 1] - frame_times[i] + 1e-9
                alpha = (t_j - frame_times[i]) / denom
                result[j] = (1.0 - alpha) * frames_arr[i, j] + alpha * frames_arr[i + 1, j]
                interpolated = True
                break

        if not interpolated:
            result[j] = frames_arr[-1, j]

    return result
