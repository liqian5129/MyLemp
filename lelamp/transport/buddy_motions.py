"""buddy_* 状态对应的程序化关节轨迹生成。

每个函数返回 `motion_agent.play_keyframes` 期望的 segment 列表:
    [{"joints": {"base_yaw": v, ...}, "duration": seconds}, ...]
joints 可只包含部分关节,未指定的关节由 `_build_frames` 内部保持当前值插值。

设计原则(跟 motion_scripts.py 已有动作风格一致):
- 多关节联动 + hold@端点 + 末段锚点
- 5 个状态在**主导关节** + **几何形状** + **节奏**三维度上互相区分,
  确保用户余光也能分辨

state 区分总览:
| state     | 主导关节            | 几何       | 节奏    | 末段锚点         |
|-----------|---------------------|------------|---------|------------------|
| idle      | yaw + roll 微动     | 平面小漂移 | 慢 1.7s | 略低垂(非 HOME) |
| busy      | wrist + elbow       | 抬起前倾   | 中 1.3s | HOME            |
| attention | yaw 归 0 + 大幅抬头 | 急抬凑近   | 急 1.15s| 抬头凑近(非 HOME)|
| celebrate | wrist_pitch 上下    | 纵向波形   | 快 1.85s| HOME            |
| failed    | wrist_pitch 下垂 + yaw 横摇 | 横+沉 | 慢 1.9s | 低垂(非 HOME)  |

舵机速度安全由下游 `motion_agent._velocity_audit` 兜底。所有偏移在 ±25° 内。
"""
from __future__ import annotations

from lelamp.service.motors.motion_scripts import HOME_POS


def _from_home(**deltas: float) -> dict:
    """以 HOME_POS 为基础,叠加给定关节的偏移。"""
    pose = dict(HOME_POS)
    for k, dv in deltas.items():
        pose[k] = HOME_POS[k] + dv
    return pose


def buddy_sleep() -> list[dict]:
    """完全垂下:base_pitch 向下 30°,2 秒过渡到位"""
    return [{"joints": _from_home(base_pitch=-30.0), "duration": 2.0}]


def buddy_idle() -> list[dict]:
    """idle:略低头 + 极微幅左右漂(±4°)+ 微歪头,营造"活着但安静"。
    末段锚点是"略低于 HOME 9° 的 wrist_pitch",不是 HOME。
    总时长 ~1.7s。
    """
    rest    = _from_home(wrist_pitch=+9.0)
    drift_l = _from_home(wrist_pitch=+9.0, base_yaw=-4.0, wrist_roll=-3.0)
    drift_r = _from_home(wrist_pitch=+9.0, base_yaw=+4.0, wrist_roll=+3.0)
    return [
        {"joints": drift_l, "duration": 0.7},
        {"joints": drift_r, "duration": 0.6},
        {"joints": rest,    "duration": 0.4},
    ]


def buddy_busy() -> list[dict]:
    """busy:从 idle 低头姿态抬起到 HOME,中间一个微前倾 beat 像"凑屏看"。
    总时长 1.3s,节奏快表达"利落开始干活"。
    """
    return [
        {"joints": dict(HOME_POS),                                 "duration": 0.6},  # 抬到 HOME
        {"joints": _from_home(wrist_pitch=+5.0, elbow_pitch=-4.0), "duration": 0.4},  # 微前倾凑屏
        {"joints": dict(HOME_POS),                                 "duration": 0.3},  # 回正端坐
    ]


def buddy_attention() -> list[dict]:
    """attention:急速抬头转向用户 + 顿一下 + 微凑近,保持警觉姿。
    动作语言:0.35s 急抬(对比 idle 的 1.2s 慢漂),用速度差强调"出事了"。
    末段不回 HOME,attention 期间维持 lean_in 直到下一个 state 切换。
    总时长 1.15s。
    """
    alert   = {"base_yaw": 0.0,
               "base_pitch":  HOME_POS["base_pitch"]  + 8.0,
               "wrist_pitch": HOME_POS["wrist_pitch"] - 18.0}
    stare   = {"base_yaw": 0.0,
               "base_pitch":  HOME_POS["base_pitch"]  + 8.0,
               "wrist_pitch": HOME_POS["wrist_pitch"] - 22.0}
    lean_in = {"base_yaw": 0.0,
               "base_pitch":  HOME_POS["base_pitch"]  + 12.0,
               "wrist_pitch": HOME_POS["wrist_pitch"] - 20.0}
    return [
        {"joints": alert,   "duration": 0.35},  # 急抬转向
        {"joints": stare,   "duration": 0.30},  # 顿一下盯人
        {"joints": lean_in, "duration": 0.50},  # 凑近 + 保持
    ]


def buddy_celebrate() -> list[dict]:
    """celebrate:wrist_pitch 双段点头(参考 nod 风格,hold@底)+ 末尾微摆余兴。
    每次到底 hold 0.15s 让点头"深度"被眼睛看见。
    末段反向 wrist_roll 微摆,让庆祝有"开心晃头"的尾韵。
    总时长 1.85s。
    """
    return [
        {"joints": _from_home(wrist_pitch=+18.0),                 "duration": 0.30},  # 第 1 次低头
        {"joints": _from_home(wrist_pitch=+18.0),                 "duration": 0.15},  # hold@底
        {"joints": _from_home(wrist_pitch=-6.0),                  "duration": 0.25},  # 抬起
        {"joints": _from_home(wrist_pitch=+18.0),                 "duration": 0.30},  # 第 2 次低头
        {"joints": _from_home(wrist_pitch=+18.0),                 "duration": 0.15},  # hold@底
        {"joints": _from_home(wrist_pitch=-3.0, wrist_roll=+8.0), "duration": 0.30},  # 抬起 + 摆
        {"joints": dict(HOME_POS),                                "duration": 0.40},  # 回 HOME
    ]


def buddy_failed() -> list[dict]:
    """failed:大幅低垂 + 慢摇头一次 + 维持沮丧姿(不回 HOME)。
    动作语言反向 celebrate:纵向点头 → 横向摇头,快节奏 → 慢节奏。
    末段保持垂头,直到下一个 state 切换才回 HOME(避免"刚沮丧立刻开心")。
    总时长 1.9s。
    """
    droop       = _from_home(wrist_pitch=+20.0, elbow_pitch=-8.0)
    droop_left  = dict(droop); droop_left["base_yaw"]  = HOME_POS["base_yaw"] - 10.0
    droop_right = dict(droop); droop_right["base_yaw"] = HOME_POS["base_yaw"] + 10.0
    return [
        {"joints": droop,       "duration": 0.7},   # 慢慢垮下去
        {"joints": droop_left,  "duration": 0.4},   # 摇头叹气向左
        {"joints": droop_right, "duration": 0.4},   # 向右
        {"joints": droop,       "duration": 0.4},   # 停在低垂
    ]


STATE_TO_GENERATOR = {
    "sleep":     buddy_sleep,
    "idle":      buddy_idle,
    "busy":      buddy_busy,
    "attention": buddy_attention,
    "failed":    buddy_failed,
    "celebrate": buddy_celebrate,
}
