"""compose_motion 工具的服务端逻辑：示例库、校验、生成 prompt 片段。

LLM 通过 compose_motion 工具直接输出关键帧 segments，本模块负责：
1. validate_segments  — 校验 LLM 输出的合法性，转成 _build_frames 接受的格式
2. MOTION_EXAMPLES    — few-shot 示例库（注入 system prompt，给 LLM 看怎么写动作）
3. get_few_shot_prompt — 暴露给 soul_agent 的入口
"""
from __future__ import annotations

from typing import Optional

# ── 校验常量 ─────────────────────────────────────────────────────────────────

VALID_JOINTS = {"base_yaw", "base_pitch", "elbow_pitch", "wrist_roll", "wrist_pitch"}
MIN_SEG_COUNT = 2
MAX_SEG_COUNT = 8
MIN_DUR       = 0.15
MAX_DUR       = 2.0
MAX_TOTAL_DUR = 6.0
JOINT_RANGE   = (-92.0, 92.0)


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
            if not (JOINT_RANGE[0] <= vf <= JOINT_RANGE[1]):
                return None, f"段 {idx} 关节 {k}={vf} 超出 {JOINT_RANGE}"
            clean_joints[k] = vf

        total += dur
        out.append((clean_joints, dur))

    if total > MAX_TOTAL_DUR:
        return None, f"总时长 {total:.1f}s 超过 {MAX_TOTAL_DUR}s"
    return out, None


# ── Few-shot 示例库 ──────────────────────────────────────────────────────────
# 写法故意贴近 _build_frames 的输入格式（绝对角度，非 delta），
# 让 LLM 看到：关节值是绝对的，起点是当前姿态。
# HOME 值参考：base_yaw=7  base_pitch=-38  elbow_pitch=49  wrist_roll=0  wrist_pitch=-47

MOTION_EXAMPLES = """
<motion_examples>
以下是 6 个标准动作的关键帧定义，供你 compose_motion 时参考。
所有数值是绝对关节角度（不是相对当前位置），最后一段都回到 HOME 附近。

示例 1 — nod 点头两次
intent: 点头表示肯定
segments:
  [{"joints": {"wrist_pitch": -69}, "duration": 0.35},
   {"joints": {"wrist_pitch": -39}, "duration": 0.35},
   {"joints": {"wrist_pitch": -69}, "duration": 0.35},
   {"joints": {"wrist_pitch": -47}, "duration": 0.45}]

示例 2 — headshake 摇头
intent: 摇头表示否定
segments:
  [{"joints": {"base_yaw": -15}, "duration": 0.35},
   {"joints": {"base_yaw":  29}, "duration": 0.40},
   {"joints": {"base_yaw": -15}, "duration": 0.35},
   {"joints": {"base_yaw":   7}, "duration": 0.40}]

示例 3 — curious 歪头打量
intent: 好奇地歪头看右边，停顿后回正
segments:
  [{"joints": {"base_yaw": 25, "wrist_roll":  22}, "duration": 0.80},
   {"joints": {"base_yaw": 25, "wrist_roll":  22}, "duration": 0.40},
   {"joints": {"base_yaw": -5, "wrist_roll": -10}, "duration": 0.70},
   {"joints": {"base_yaw":  7, "wrist_roll":   0}, "duration": 0.80}]

示例 4 — sad 沮丧低头
intent: 灯头缓缓垂下，停留，再慢慢回来
segments:
  [{"joints": {"wrist_pitch": -75}, "duration": 1.50},
   {"joints": {"wrist_pitch": -75}, "duration": 1.00},
   {"joints": {"wrist_pitch": -47}, "duration": 1.80}]

示例 5 — shock 震惊后仰
intent: 灯头快速后仰，停顿，再回正
segments:
  [{"joints": {"wrist_pitch": -33, "base_pitch": -46}, "duration": 0.30},
   {"joints": {"wrist_pitch": -33, "base_pitch": -46}, "duration": 0.35},
   {"joints": {"wrist_pitch": -47, "base_pitch": -38}, "duration": 0.50}]

示例 6 — excited 兴奋弹跳两次
intent: 整臂上下弹跳两次表示兴奋
segments:
  [{"joints": {"base_pitch": -30, "elbow_pitch": 42, "wrist_pitch": -40}, "duration": 0.45},
   {"joints": {"base_pitch": -38, "elbow_pitch": 49, "wrist_pitch": -47}, "duration": 0.40},
   {"joints": {"base_pitch": -30, "elbow_pitch": 42, "wrist_pitch": -40}, "duration": 0.40},
   {"joints": {"base_pitch": -38, "elbow_pitch": 49, "wrist_pitch": -47}, "duration": 0.50}]

构造原则：
1. 起点是当前姿态（不需要写），最后一段建议回到 HOME 附近：
   base_yaw≈7  base_pitch≈-38  elbow_pitch≈49  wrist_roll≈0  wrist_pitch≈-47
2. 想表达"两次/三次"循环时，4-5 段比 2 段更生动（有回弹感）
3. 想表达"停顿/凝视"时，重复一段同样的 joints 即可
4. 单段 duration 建议 0.3-1.5s；总时长不超过 5s
5. 只写要变的关节，未指定的关节自动维持上一段的值
6. intent 字段必填，用一句中文描述要表达的动作和情感
</motion_examples>
"""


def get_few_shot_prompt() -> str:
    """返回注入 system prompt 的 few-shot 段。"""
    return MOTION_EXAMPLES
