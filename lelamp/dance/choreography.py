"""
编舞调度层

包含：
  DanceState          — 枚举：HIT / GROOVE / FLOW / FREEZE
  PHRASE_TEMPLATES    — 5 种 8 拍短句模板
  ChoreographyStateMachine — 状态机，每拍返回 DanceState
  MotionSelector      — 从原语库选择，含防重复逻辑
  ScheduledMove       — 已调度动作的时间信息
  MotionScheduler     — 计算提前启动时间并管理执行队列
"""
from __future__ import annotations

import random
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import List

from .primitives import MotionPrimitive

# ── 状态枚举 ──────────────────────────────────────────────────────────────────

class DanceState(str, Enum):
    HIT    = "HIT"
    GROOVE = "GROOVE"
    FLOW   = "FLOW"
    FREEZE = "FREEZE"


# ── Phrase Templates（5 种 × 8 拍）────────────────────────────────────────────

PHRASE_TEMPLATES: dict[str, List[str]] = {
    "buildup": [
        "GROOVE", "GROOVE", "HIT",    "HIT",
        "GROOVE", "HIT",    "HIT",    "HIT",
    ],
    "chill": [
        "GROOVE", "FLOW",   "GROOVE", "FREEZE",
        "GROOVE", "FLOW",   "GROOVE", "GROOVE",
    ],
    "hype": [
        "HIT",    "HIT",    "HIT",    "GROOVE",
        "HIT",    "HIT",    "HIT",    "HIT",
    ],
    "contrast": [
        "FREEZE", "FREEZE", "HIT",    "HIT",
        "GROOVE", "GROOVE", "HIT",    "FLOW",
    ],
    "groove_focus": [
        "GROOVE", "GROOVE", "GROOVE", "HIT",
        "GROOVE", "GROOVE", "FLOW",   "HIT",
    ],
}

# 模板播放顺序
TEMPLATE_ORDER = [
    "buildup", "chill", "hype", "groove_focus",
    "contrast", "hype", "chill", "groove_focus",
]


# ── 状态机 ────────────────────────────────────────────────────────────────────

class ChoreographyStateMachine:
    """
    基于 Phrase Template 的编舞状态机。
    每拍调用 tick()，返回当前目标 DanceState。
    能量极高/极低时强制覆盖。
    """

    def __init__(self):
        self._template_idx   = 0
        self._beat_in_phrase = 0

    def tick(
        self,
        bar_position: int,
        energy: float,
        section_changed: bool,
    ) -> DanceState:
        # 段落变化时切换模板
        if section_changed:
            self._template_idx   = (self._template_idx + 1) % len(TEMPLATE_ORDER)
            self._beat_in_phrase = 0

        template_name = TEMPLATE_ORDER[self._template_idx]
        phrase        = PHRASE_TEMPLATES[template_name]
        beat_in_8     = self._beat_in_phrase % 8
        state         = DanceState(phrase[beat_in_8])

        self._beat_in_phrase += 1

        # 能量覆盖规则
        if energy > 0.82:
            state = DanceState.HIT
        elif energy < 0.22:
            state = DanceState.FREEZE

        return state


# ── 动作选择器 ────────────────────────────────────────────────────────────────

class MotionSelector:
    """
    从原语库中选择匹配 DanceState 和能量级别的原语。
    维护最近 6 条记录，防止短时间内重复。
    """

    def __init__(self, library: List[MotionPrimitive], recent_size: int = 6):
        self.library = library
        self.recent: deque[str] = deque(maxlen=recent_size)

    def select(
        self,
        state: DanceState,
        bar_position: int,
        energy: float,
    ) -> MotionPrimitive:
        # 第一轮：类型 + 能量 + 防重复
        candidates = [
            p for p in self.library
            if p.type.value == state.value
            and self._energy_match(p.energy_level, energy)
            and p.id not in self.recent
        ]

        # 第二轮：类型 + 防重复（放宽能量约束）
        if not candidates:
            candidates = [
                p for p in self.library
                if p.type.value == state.value
                and p.id not in self.recent
            ]

        # 兜底：全库
        if not candidates:
            candidates = list(self.library)

        weights = [p.weight for p in candidates]
        chosen  = random.choices(candidates, weights=weights)[0]
        self.recent.append(chosen.id)
        return chosen

    @staticmethod
    def _energy_match(prim_energy: str, actual: float) -> bool:
        return (
            (prim_energy == "low"  and actual < 0.40) or
            (prim_energy == "mid"  and 0.30 < actual < 0.75) or
            (prim_energy == "high" and actual > 0.60)
        )


# ── 时间调度器 ────────────────────────────────────────────────────────────────

PREP_TIME = 0.12   # 提前 120ms 开始（根据实测舵机响应调整）

DUR_MAP = {"half": 0.5, "one": 1.0, "two": 2.0}


@dataclass
class ScheduledMove:
    primitive:  MotionPrimitive
    start_time: float   # 何时开始执行（绝对 wall time）
    beat_time:  float   # 对应节拍时刻（调试用）
    beat_dur:   float   # 一拍时长（秒）


class MotionScheduler:
    """
    计算每个动作的提前启动时间，并在到时后返还给执行层。

    卡点公式：
        start_time = beat_time - accent_norm × total_dur - PREP_TIME
    """

    def __init__(self):
        self.queue: List[ScheduledMove] = []

    def schedule(
        self,
        beat_time: float,
        beat_dur:  float,
        primitive: MotionPrimitive,
    ) -> None:
        total_dur   = DUR_MAP[primitive.duration] * beat_dur
        accent_norm = primitive.frame_times[1]          # 重音帧归一化位置

        start_time = beat_time - accent_norm * total_dur - PREP_TIME
        start_time = max(start_time, time.time() + 0.005)  # 不往过去调度

        self.queue.append(ScheduledMove(
            primitive  = primitive,
            start_time = start_time,
            beat_time  = beat_time,
            beat_dur   = beat_dur,
        ))
        self.queue.sort(key=lambda m: m.start_time)

    def pop_ready(self) -> List[ScheduledMove]:
        """弹出所有已到执行时间的动作"""
        now     = time.time()
        ready   = [m for m in self.queue if m.start_time <= now]
        pending = [m for m in self.queue if m.start_time >  now]
        self.queue = pending
        return ready
