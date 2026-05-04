"""compose_motion 工具的服务端逻辑：示例库、校验、生成 prompt 片段。

LLM 通过 compose_motion 工具直接输出关键帧 segments，本模块负责：
1. validate_segments  — 校验 LLM 输出的合法性，转成 _build_frames 接受的格式
2. MOTION_EXAMPLES    — few-shot 示例库（注入 system prompt，给 LLM 看怎么写动作）
3. get_few_shot_prompt — 暴露给 soul_agent 的入口
"""
from __future__ import annotations

from typing import Optional

from lelamp.service.motors.motion_scripts import JOINT_LIMITS

# ── 校验常量 ─────────────────────────────────────────────────────────────────

VALID_JOINTS = {"base_yaw", "base_pitch", "elbow_pitch", "wrist_roll", "wrist_pitch"}
MIN_SEG_COUNT = 2
MAX_SEG_COUNT = 12
MIN_DUR       = 0.15
MAX_DUR       = 2.0
MAX_TOTAL_DUR = 8.0
_DEFAULT_RANGE = (-92.0, 92.0)


# ── 校验器 ───────────────────────────────────────────────────────────────────

def validate_segments(segments) -> tuple[Optional[list], Optional[str]]:
    """校验 LLM 输出。

    输入格式（LLM 风格）:
        [{"joints": {"wrist_pitch": -69}, "duration": 0.35}, ...]

    输出格式（_build_frames 风格）:
        [({"wrist_pitch": -69}, 0.35), ...]

    返回 (转换后的段列表, error_msg)。
    成功时 error_msg=None；失败时段列表=None。
    """
    if not isinstance(segments, list):
        return None, "segments 必须是数组"
    if not (MIN_SEG_COUNT <= len(segments) <= MAX_SEG_COUNT):
        return None, f"segments 数量需在 {MIN_SEG_COUNT}-{MAX_SEG_COUNT}（当前 {len(segments)}）"

    out: list[tuple[dict, float]] = []
    total = 0.0
    for idx, seg in enumerate(segments):
        if not isinstance(seg, dict):
            return None, f"段 {idx} 不是 dict"
        joints = seg.get("joints", {})
        dur    = seg.get("duration")
        if not isinstance(joints, dict) or not joints:
            return None, f"段 {idx} joints 缺失或为空"
        if dur is None:
            return None, f"段 {idx} 缺 duration"
        try:
            dur = float(dur)
        except (TypeError, ValueError):
            return None, f"段 {idx} duration 不是数字"
        if not (MIN_DUR <= dur <= MAX_DUR):
            return None, f"段 {idx} duration={dur} 超出 [{MIN_DUR}, {MAX_DUR}]"

        clean_joints: dict[str, float] = {}
        for k, v in joints.items():
            if k not in VALID_JOINTS:
                return None, f"段 {idx} 未知关节 '{k}'，可用：{sorted(VALID_JOINTS)}"
            try:
                vf = float(v)
            except (TypeError, ValueError):
                return None, f"段 {idx} 关节 {k} 值不是数字"
            lo, hi = JOINT_LIMITS.get(k, _DEFAULT_RANGE)
            if not (lo <= vf <= hi):
                return None, f"段 {idx} 关节 {k}={vf} 超出 [{lo}, {hi}]"
            clean_joints[k] = vf

        total += dur
        out.append((clean_joints, dur))

    if total > MAX_TOTAL_DUR:
        return None, f"总时长 {total:.1f}s 超过 {MAX_TOTAL_DUR}s"
    return out, None


# ── Few-shot 示例库 ──────────────────────────────────────────────────────────
# 写法故意贴近 _build_frames 的输入格式（绝对角度，非 delta），
# 让 LLM 看到：关节值是绝对的，起点是当前姿态。
# HOME 值参考：base_yaw=14  base_pitch=-38  elbow_pitch=61  wrist_roll=-6  wrist_pitch=5
# ⚠️ wrist_pitch 方向：正值=低垂，负值=抬起（与直觉相反）

MOTION_EXAMPLES = """
<motion_examples>
以下是 6 个标准动作的关键帧定义，供你 compose_motion 时参考。
所有数值是绝对关节角度（不是相对当前位置），最后一段都回到 HOME 附近。
⚠️ wrist_pitch 方向注意：正值=低垂，负值=抬起

示例 1 — nod 点头两次
intent: wrist_pitch 两次快速下探回弹，节奏均匀
segments:
  [{"joints": {"wrist_pitch": 25}, "duration": 0.35},
   {"joints": {"wrist_pitch": -5}, "duration": 0.35},
   {"joints": {"wrist_pitch": 25}, "duration": 0.35},
   {"joints": {"wrist_pitch": 5}, "duration": 0.45}]

示例 2 — headshake 摇头
intent: base_yaw 左右摆动一个来回，幅度对称
segments:
  [{"joints": {"base_yaw": -8}, "duration": 0.35},
   {"joints": {"base_yaw": 36}, "duration": 0.40},
   {"joints": {"base_yaw": -8}, "duration": 0.35},
   {"joints": {"base_yaw": 14}, "duration": 0.40}]

示例 3 — curious 歪头打量
intent: base_yaw 转右 + wrist_roll 同向歪头，停顿一拍后回正
segments:
  [{"joints": {"base_yaw": 31, "wrist_roll":  17}, "duration": 0.80},
   {"joints": {"base_yaw": 31, "wrist_roll":  17}, "duration": 0.40},
   {"joints": {"base_yaw":  1, "wrist_roll": -15}, "duration": 0.70},
   {"joints": {"base_yaw": 14, "wrist_roll":  -6}, "duration": 0.80}]

示例 4 — sad 沮丧低头
intent: wrist_pitch 缓慢升到最高（低垂），长停顿，再慢速回正
segments:
  [{"joints": {"wrist_pitch": 25}, "duration": 1.50},
   {"joints": {"wrist_pitch": 25}, "duration": 1.00},
   {"joints": {"wrist_pitch": 5}, "duration": 1.80}]

示例 5 — shock 震惊后仰
intent: wrist_pitch 下降（抬头）+ base_pitch 后仰同时快弹，短停顿后回正
segments:
  [{"joints": {"wrist_pitch": -10, "base_pitch": -50}, "duration": 0.30},
   {"joints": {"wrist_pitch": -10, "base_pitch": -50}, "duration": 0.35},
   {"joints": {"wrist_pitch": 5, "base_pitch": -38}, "duration": 0.50}]

示例 6 — excited 兴奋弹跳两次
intent: base_pitch + elbow_pitch + wrist_pitch 三关节联动，上下弹跳两次
segments:
  [{"joints": {"base_pitch": -30, "elbow_pitch": 54, "wrist_pitch": -2}, "duration": 0.45},
   {"joints": {"base_pitch": -38, "elbow_pitch": 61, "wrist_pitch": 5}, "duration": 0.40},
   {"joints": {"base_pitch": -30, "elbow_pitch": 54, "wrist_pitch": -2}, "duration": 0.40},
   {"joints": {"base_pitch": -38, "elbow_pitch": 61, "wrist_pitch": 5}, "duration": 0.50}]

构造原则：
1. 起点是当前姿态（不需要写），最后一段建议回到 HOME 附近：
   base_yaw≈14  base_pitch≈-38  elbow_pitch≈61  wrist_roll≈-6  wrist_pitch≈5
2. 想表达"两次/三次"循环时，4-5 段比 2 段更生动（有回弹感）
3. 想表达"停顿/凝视"时，重复一段同样的 joints 即可
4. duration 要和角度变化幅度匹配：
   - 小幅度（<30°）：0.3-0.5s
   - 中幅度（30-50°）：0.5-0.8s
   - 大幅度（>50°）：0.8-1.2s
   ⚠️ 角度变化大但 duration 太短会被速度安全网强制拉伸，动作会比预期慢且失去节奏感。
   总时长不超过 8s（多阶段串联表演可用满）
5. 只写要变的关节，未指定的关节自动维持上一段的值
6. intent 字段必填，写出关节级的动作分解（见各示例的 intent 写法）
7. intent 要写机械分解：点名用哪几个关节、运动模式（摆动/脉冲/渐变/弹跳）、节奏（快慢/停顿），不要只写诗意描述
</motion_examples>
"""


def get_few_shot_prompt() -> str:
    """返回注入 system prompt 的 few-shot 段。"""
    return MOTION_EXAMPLES
