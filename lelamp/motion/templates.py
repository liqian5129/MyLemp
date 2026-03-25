"""
参数化模板库

提供 5 种情绪的手工设计动作变体，无需 LLM，零延迟。
作用：
  1. Day 0 即可 demo
  2. LLM 不可用时的 fallback
  3. 示例数据库的冷启动种子

关节顺序（5维）：[base_yaw, base_pitch, elbow_pitch, wrist_roll, wrist_pitch]
关节限位：
  base_yaw    [-5,  14]   0=正前，正=右转
  base_pitch  [-68, -20]  负值越大越抬起（-68最高，-20最低）
  elbow_pitch [50, 100]   正=弯曲增加
  wrist_roll  [-30,  15]  正=右歪
  wrist_pitch [-5,  68]   正=抬起

基准姿态 Q_REST = [0, -44, 75, 0, 25]

基于 expressive_robot_motion_design_v2.md Section 6，关节适配为 5 维。
"""
from __future__ import annotations

import random
from dataclasses import dataclass

import numpy as np

from lelamp.motion.motion_executor import Q_REST, Q_MIN, Q_MAX, N_JOINTS

# ── 情绪目标枚举 ──────────────────────────────────────────────────────────────

EMOTION_INDEX = ["happy", "sad", "angry", "curious", "calm"]

# ── LLMOutput 兼容数据结构 ────────────────────────────────────────────────────

@dataclass
class MotionKeyframes:
    """
    与 LLM 输出接口兼容的动作关键帧结构。
    f1, f2 为中间两帧（5维），f0/f3 由调用方注入。
    lma_* 和 intent 字段仅用于调试日志，不进入下游计算。
    """
    f1: np.ndarray          # (N_JOINTS,)
    f2: np.ndarray          # (N_JOINTS,)
    duration: float         # 秒 [0.5, 5.0]
    accel_ratio: float      # [0.05, 0.50]
    asymmetry: float        # [-1.0, +1.0]
    lma_time: str = ""
    lma_weight: str = ""
    lma_flow: str = ""
    lma_space: str = ""
    intent: str = ""


# ── 模板库（5 情绪 × 3~5 变体）────────────────────────────────────────────────
# 每个变体: f1, f2 均为 [base_yaw, base_pitch, elbow_pitch, wrist_roll, wrist_pitch]
# 所有角度均在关节限位范围内，基于 Q_REST=[0,-44,75,0,25] 设计

_MOTION_TEMPLATES: dict[str, list[dict]] = {

    # ── 开心：快起慢停，弹跳感，asymmetry 高正值 ──────────────────────────────
    "happy": [
        {   # 弹跳抬起 + 左右晃头
            "f1": [ 8, -56,  85, -8, 42],
            "f2": [-6, -38,  70, 10, 16],
            "timing": {"duration": 1.8, "accel_ratio": 0.12, "asymmetry": 0.65},
            "lma": {"time": "sudden", "weight": "light", "flow": "free", "space": "indirect"},
            "intent": "快速弹跳抬起，头部左右轻晃",
        },
        {   # 快速抬起 + 歪头
            "f1": [11, -60,  88, 12, 50],
            "f2": [-9, -34,  68, -18, 18],
            "timing": {"duration": 1.5, "accel_ratio": 0.15, "asymmetry": 0.80},
            "lma": {"time": "sudden", "weight": "light", "flow": "free", "space": "indirect"},
            "intent": "快速抬高后歪头摇摆",
        },
        {   # 小幅点头 + 左右摆
            "f1": [12, -52,  80, -12, 38],
            "f2": [-10, -36, 72, 10, 22],
            "timing": {"duration": 1.2, "accel_ratio": 0.10, "asymmetry": 0.70},
            "lma": {"time": "sudden", "weight": "light", "flow": "free", "space": "indirect"},
            "intent": "小幅欢快点头",
        },
        {   # 开合身体 + 歪头
            "f1": [ 5, -60,  90, -15, 55],
            "f2": [-4, -32,  66, 14, 15],
            "timing": {"duration": 2.0, "accel_ratio": 0.12, "asymmetry": 0.60},
            "lma": {"time": "sudden", "weight": "light", "flow": "free", "space": "indirect"},
            "intent": "张开身体然后收拢，表达喜悦",
        },
    ],

    # ── 伤心：慢起快停，拖沓感，asymmetry 低负值 ─────────────────────────────
    "sad": [
        {   # 缓慢低头
            "f1": [-3, -28,  62,  -4, 12],
            "f2": [ 2, -24,  60,   4, 8],
            "timing": {"duration": 3.5, "accel_ratio": 0.35, "asymmetry": -0.70},
            "lma": {"time": "sustained", "weight": "strong", "flow": "bound", "space": "indirect"},
            "intent": "缓缓低头，沉重地落下",
        },
        {   # 侧倾 + 下垂
            "f1": [-6, -26,  60,  -16, 10],
            "f2": [ 4, -22,  58,   8, 6],
            "timing": {"duration": 4.0, "accel_ratio": 0.40, "asymmetry": -0.80},
            "lma": {"time": "sustained", "weight": "strong", "flow": "bound", "space": "indirect"},
            "intent": "微微侧倾，无力地垂下",
        },
        {   # 向前弓身
            "f1": [ 0, -25,  92,   0, 5],
            "f2": [ 0, -22,  96,   0, 3],
            "timing": {"duration": 3.8, "accel_ratio": 0.38, "asymmetry": -0.75},
            "lma": {"time": "sustained", "weight": "strong", "flow": "bound", "space": "direct"},
            "intent": "沉重弓身，表达沮丧",
        },
    ],

    # ── 愤怒：快速直接，accel 短，asymmetry 近零 ─────────────────────────────
    "angry": [
        {   # 快速前冲低头
            "f1": [ 0, -32,  90,  0, 12],
            "f2": [ 0, -28,  94,  0, 8],
            "timing": {"duration": 0.8, "accel_ratio": 0.08, "asymmetry": -0.20},
            "lma": {"time": "sudden", "weight": "strong", "flow": "bound", "space": "direct"},
            "intent": "猛烈前冲，直接攻击性姿态",
        },
        {   # 猛烈左右扫视
            "f1": [13, -38,  78,  0, 20],
            "f2": [-5, -38,  78,  0, 20],
            "timing": {"duration": 1.0, "accel_ratio": 0.06, "asymmetry": 0.0},
            "lma": {"time": "sudden", "weight": "strong", "flow": "bound", "space": "direct"},
            "intent": "愤怒地左右扫视",
        },
        {   # 急速抬起 + 俯冲
            "f1": [ 0, -65,  75,  0, 60],
            "f2": [ 0, -30,  90,  0, 10],
            "timing": {"duration": 0.9, "accel_ratio": 0.07, "asymmetry": -0.15},
            "lma": {"time": "sudden", "weight": "strong", "flow": "bound", "space": "direct"},
            "intent": "急速抬起后猛然俯冲",
        },
    ],

    # ── 好奇：中等速度，探索性弧线，asymmetry 小正值 ─────────────────────────
    "curious": [
        {   # 歪头 + 前探
            "f1": [10, -50,  82,  18, 30],
            "f2": [-8, -47,  80, -14, 28],
            "timing": {"duration": 2.2, "accel_ratio": 0.20, "asymmetry": 0.30},
            "lma": {"time": "sustained", "weight": "light", "flow": "free", "space": "indirect"},
            "intent": "歪头前探，好奇地审视",
        },
        {   # 左右张望
            "f1": [12, -46,  78,  12, 25],
            "f2": [-4, -48,  80, -16, 28],
            "timing": {"duration": 2.5, "accel_ratio": 0.25, "asymmetry": 0.25},
            "lma": {"time": "sustained", "weight": "light", "flow": "free", "space": "indirect"},
            "intent": "左右张望，探索性地转头",
        },
        {   # 缓慢俯仰探视
            "f1": [ 4, -58,  82,  8, 45],
            "f2": [-3, -38,  72, -6, 20],
            "timing": {"duration": 2.8, "accel_ratio": 0.22, "asymmetry": 0.35},
            "lma": {"time": "sustained", "weight": "light", "flow": "free", "space": "indirect"},
            "intent": "上下俯仰，多角度观察",
        },
    ],

    # ── 平静：对称慢动作，呼吸感 ──────────────────────────────────────────────
    "calm": [
        {   # 轻微起伏呼吸感
            "f1": [ 0, -46,  76,  0, 26],
            "f2": [ 0, -42,  74,  0, 24],
            "timing": {"duration": 4.0, "accel_ratio": 0.40, "asymmetry": 0.0},
            "lma": {"time": "sustained", "weight": "light", "flow": "free", "space": "direct"},
            "intent": "缓慢深呼吸般的轻微起伏",
        },
        {   # 轻柔侧倾
            "f1": [ 3, -44,  75,  5, 25],
            "f2": [-3, -44,  75, -5, 25],
            "timing": {"duration": 5.0, "accel_ratio": 0.45, "asymmetry": 0.0},
            "lma": {"time": "sustained", "weight": "light", "flow": "free", "space": "direct"},
            "intent": "极轻柔的左右微摆，如静止的水波",
        },
    ],
}


# ── 模板生成函数 ───────────────────────────────────────────────────────────────

def template_generate(emotion: str, intensity: float) -> MotionKeyframes:
    """
    从模板库随机选取一个变体，用 intensity 缩放幅度，加随机扰动。

    Args:
        emotion:   情绪名，必须在 EMOTION_INDEX 中
        intensity: 强度 [0.0, 1.0]

    Returns:
        MotionKeyframes，与 LLM 输出接口兼容
    """
    if emotion not in _MOTION_TEMPLATES:
        emotion = "calm"

    template = random.choice(_MOTION_TEMPLATES[emotion])
    lma = template.get("lma", {})
    t = template["timing"]

    # intensity 缩放：偏离中性姿态的幅度与 intensity 成正比
    intensity_clipped = float(np.clip(intensity, 0.2, 1.0))
    f1_base = np.array(template["f1"], dtype=np.float32)
    f2_base = np.array(template["f2"], dtype=np.float32)

    f1 = Q_REST + (f1_base - Q_REST) * intensity_clipped
    f2 = Q_REST + (f2_base - Q_REST) * intensity_clipped

    # 随机扰动（低 intensity 时扰动小）
    noise_scale = 2.0 * intensity_clipped
    f1 += np.random.randn(N_JOINTS).astype(np.float32) * noise_scale
    f2 += np.random.randn(N_JOINTS).astype(np.float32) * noise_scale

    # 裁剪到关节限位
    f1 = np.clip(f1, Q_MIN, Q_MAX)
    f2 = np.clip(f2, Q_MIN, Q_MAX)

    # timing 随 intensity 微调（强度高 → 稍快）
    duration    = float(np.clip(t["duration"] * (1.1 - 0.3 * intensity_clipped), 0.5, 5.0))
    accel_ratio = float(np.clip(t["accel_ratio"], 0.05, 0.50))
    asymmetry   = float(np.clip(t["asymmetry"] * intensity_clipped, -1.0, 1.0))

    return MotionKeyframes(
        f1=f1,
        f2=f2,
        duration=duration,
        accel_ratio=accel_ratio,
        asymmetry=asymmetry,
        lma_time=lma.get("time", ""),
        lma_weight=lma.get("weight", ""),
        lma_flow=lma.get("flow", ""),
        lma_space=lma.get("space", ""),
        intent=f"[模板] {template.get('intent', emotion)} @ {intensity:.1f}",
    )


def get_template_variants(emotion: str) -> list[dict]:
    """返回指定情绪的所有模板变体（用于调试或种子生成）"""
    return _MOTION_TEMPLATES.get(emotion, [])
