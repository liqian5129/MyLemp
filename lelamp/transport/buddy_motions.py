"""buddy_* 状态对应的程序化关节轨迹生成。

每个函数返回 `motion_agent.play_keyframes` 期望的 segment 列表:
    [{"joints": {"base_yaw": v, ...}, "duration": seconds}, ...]
joints 可只包含部分关节,未指定的关节由 `_build_frames` 内部保持当前值插值。

**Step 3 占位实现**:用最简单的"目标姿态 + 单段时长"凑出可视化区分,
保证 ArmController 路由链路通畅,所有偏移都在 HOME_POS 附近 ±30° 内。

**Step 5 完整实现**(待写):
- idle:base_pitch ±2° 正弦呼吸循环
- failed:进入时一次 emphatic 抖动(~0.3 秒,与屏角色抖动同步),之后保持 busy 姿态静止
- celebrate:多段点头 + 横向晃动复合脚本

舵机速度安全由下游 `motion_agent._velocity_audit` 兜底,这里的 duration 给宽松值即可。
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
    """归位休息(Step 5 加 ±2° 正弦呼吸循环)"""
    return [{"joints": dict(HOME_POS), "duration": 1.0}]


def buddy_busy() -> list[dict]:
    """凑屏前倾:base_pitch +20°(向 Mac 屏方向)"""
    return [{"joints": _from_home(base_pitch=+20.0), "duration": 1.0}]


def buddy_attention() -> list[dict]:
    """从 busy 慢转向用户:base_yaw 归 0,1.5 秒平滑过渡"""
    return [{"joints": {"base_yaw": 0.0}, "duration": 1.5}]


def buddy_failed() -> list[dict]:
    """进入时一次 emphatic 抖动 + 保持 busy 姿态。
    Step 5 改为程序化高频小幅抖动(~0.3 秒一次)。"""
    busy_pose  = _from_home(base_pitch=+20.0)
    shake_left  = dict(busy_pose); shake_left["base_yaw"]  = HOME_POS["base_yaw"] - 5.0
    shake_right = dict(busy_pose); shake_right["base_yaw"] = HOME_POS["base_yaw"] + 5.0
    return [
        {"joints": shake_left,  "duration": 0.15},
        {"joints": shake_right, "duration": 0.15},
        {"joints": busy_pose,   "duration": 0.3},   # 之后保持 busy 姿态静止
    ]


def buddy_celebrate() -> list[dict]:
    """点头两次(Step 5 加左右晃动复合)"""
    return [
        {"joints": _from_home(base_pitch=+15.0), "duration": 0.4},
        {"joints": _from_home(base_pitch=-5.0),  "duration": 0.3},
        {"joints": _from_home(base_pitch=+15.0), "duration": 0.4},
        {"joints": dict(HOME_POS),               "duration": 0.4},
    ]


STATE_TO_GENERATOR = {
    "sleep":     buddy_sleep,
    "idle":      buddy_idle,
    "busy":      buddy_busy,
    "attention": buddy_attention,
    "failed":    buddy_failed,
    "celebrate": buddy_celebrate,
}
