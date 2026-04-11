"""Phase 1 + Phase 2 单元测试（不接舵机）。

覆盖：
  - 程序化动作 intensity 缩放（幅度 + 速度）
  - 抖动随机性（不同 seed 产出不同序列）
  - validate_segments 各类合法/非法输入
  - _build_frames 对 LLM 风格 segments 的容错

用法：
    uv run python tests/test_intensity_and_compose.py
"""
from __future__ import annotations

import sys
import traceback

from lelamp.motion.compose_motion import (
    JOINT_RANGE,
    MAX_SEG_COUNT,
    MAX_TOTAL_DUR,
    MIN_SEG_COUNT,
    validate_segments,
)
from lelamp.service.motors.motion_scripts import (
    HOME_POS,
    _build_frames,
    curious,
    happy_wiggle,
    nod,
    sad,
)


def _max_dev(frames: list[dict], joint: str, baseline: float) -> float:
    return max(abs(f[joint] - baseline) for f in frames)


def _passed(name: str):
    print(f"  ✓ {name}")


def test_intensity_scales_amplitude():
    """同一 seed 下，intensity=1.5 的幅度应明显大于 intensity=0.5"""
    pos = dict(HOME_POS)
    f_low  = nod(pos, intensity=0.5, jitter=0.0, seed=42)
    f_high = nod(pos, intensity=1.5, jitter=0.0, seed=42)
    dev_low  = _max_dev(f_low,  "wrist_pitch", pos["wrist_pitch"])
    dev_high = _max_dev(f_high, "wrist_pitch", pos["wrist_pitch"])
    assert dev_high > dev_low * 2.5, f"幅度比例不对: low={dev_low:.1f} high={dev_high:.1f}"
    _passed(f"intensity 缩放幅度  低={dev_low:.1f}° 高={dev_high:.1f}°")


def test_intensity_scales_duration():
    """intensity=0.5 的总帧数应大于 intensity=1.5（更慢）"""
    pos = dict(HOME_POS)
    f_slow = nod(pos, intensity=0.5, jitter=0.0, seed=42)
    f_fast = nod(pos, intensity=1.5, jitter=0.0, seed=42)
    assert len(f_slow) > len(f_fast), f"慢动作帧数应更多: slow={len(f_slow)} fast={len(f_fast)}"
    _passed(f"intensity 影响速度  慢={len(f_slow)}帧 快={len(f_fast)}帧")


def test_jitter_randomness():
    """不同 seed 应产出不同序列；jitter=0 时幂等"""
    pos = dict(HOME_POS)
    a = nod(pos, intensity=1.0, jitter=0.2, seed=1)
    b = nod(pos, intensity=1.0, jitter=0.2, seed=2)
    c = nod(pos, intensity=1.0, jitter=0.0, seed=1)
    d = nod(pos, intensity=1.0, jitter=0.0, seed=2)
    # 不同 seed + 有 jitter → 不同
    assert a != b, "不同 seed 应产出不同序列"
    # jitter=0 → 与 seed 无关
    assert c == d, "jitter=0 时不同 seed 应产出相同序列"
    _passed("jitter seed 行为正确")


def test_curious_with_pause_segments():
    """curious 包含空 dict 停顿段，参数化后仍能跑通"""
    pos = dict(HOME_POS)
    frames = curious(pos, intensity=0.4, seed=7)
    assert len(frames) > 0
    # 所有帧都应包含 5 个关节
    for f in frames:
        assert set(f.keys()) == set(HOME_POS.keys()), f"帧关节集不全: {f.keys()}"
    _passed(f"curious 含停顿段  {len(frames)} 帧")


def test_intensity_clamp_in_motion_scripts():
    """函数内 _scaled 不会自己 clamp intensity，但 _clamp 会保证角度安全。
    极端 intensity=2.0 时，幅度应被关节限位拦下。"""
    pos = dict(HOME_POS)
    frames = sad(pos, intensity=2.0, jitter=0.0, seed=0)
    for f in frames:
        for v in f.values():
            assert -100.0 <= v <= 100.0, f"_build_frames 内部 clamp 失效: {v}"
    _passed("极端 intensity 角度仍被限位")


def test_happy_wiggle_full_joint_set():
    """happy_wiggle 同时控制 yaw + roll，参数化后两关节都应被缩放"""
    pos = dict(HOME_POS)
    f_low  = happy_wiggle(pos, intensity=0.5, jitter=0.0, seed=3)
    f_high = happy_wiggle(pos, intensity=1.5, jitter=0.0, seed=3)
    yaw_low  = _max_dev(f_low,  "base_yaw",   pos["base_yaw"])
    yaw_high = _max_dev(f_high, "base_yaw",   pos["base_yaw"])
    roll_low  = _max_dev(f_low,  "wrist_roll", pos["wrist_roll"])
    roll_high = _max_dev(f_high, "wrist_roll", pos["wrist_roll"])
    assert yaw_high  > yaw_low  * 2.5
    assert roll_high > roll_low * 2.5
    _passed(f"happy_wiggle 双关节缩放  yaw {yaw_low:.1f}→{yaw_high:.1f}°")


def test_validate_ok():
    seg = [
        {"joints": {"wrist_pitch": -69}, "duration": 0.35},
        {"joints": {"wrist_pitch": -47}, "duration": 0.45},
    ]
    out, err = validate_segments(seg)
    assert err is None, err
    assert out is not None
    assert len(out) == 2
    assert out[0] == ({"wrist_pitch": -69}, 0.35)
    _passed("validate_segments 合法输入")


def test_validate_too_few():
    seg = [{"joints": {"wrist_pitch": -69}, "duration": 0.35}]
    _, err = validate_segments(seg)
    assert err and str(MIN_SEG_COUNT) in err
    _passed("拒绝段数过少")


def test_validate_too_many():
    seg = [{"joints": {"wrist_pitch": -47}, "duration": 0.2}] * (MAX_SEG_COUNT + 1)
    _, err = validate_segments(seg)
    assert err and str(MAX_SEG_COUNT) in err
    _passed("拒绝段数过多")


def test_validate_unknown_joint():
    seg = [
        {"joints": {"shoulder": 30}, "duration": 0.4},
        {"joints": {"wrist_pitch": -47}, "duration": 0.4},
    ]
    _, err = validate_segments(seg)
    assert err and "shoulder" in err
    _passed("拒绝未知关节")


def test_validate_out_of_range_value():
    seg = [
        {"joints": {"wrist_pitch": 200}, "duration": 0.4},
        {"joints": {"wrist_pitch": -47}, "duration": 0.4},
    ]
    _, err = validate_segments(seg)
    assert err and "200" in err
    _passed("拒绝越界关节值")


def test_validate_out_of_range_duration():
    seg = [
        {"joints": {"wrist_pitch": -47}, "duration": 0.05},  # 太短
        {"joints": {"wrist_pitch": -47}, "duration": 0.4},
    ]
    _, err = validate_segments(seg)
    assert err and "duration" in err
    _passed("拒绝越界 duration")


def test_validate_total_duration():
    seg = [{"joints": {"wrist_pitch": -47}, "duration": 2.0}] * 5  # 10.0s 总时长
    _, err = validate_segments(seg)
    assert err and "总时长" in err
    _passed(f"拒绝总时长 > {MAX_TOTAL_DUR}s")


def test_validate_empty_joints():
    seg = [
        {"joints": {}, "duration": 0.4},
        {"joints": {"wrist_pitch": -47}, "duration": 0.4},
    ]
    _, err = validate_segments(seg)
    assert err and "joints" in err
    _passed("拒绝空 joints dict")


def test_compose_to_build_frames_pipeline():
    """从 LLM 风格 segments → validate → _build_frames 全链路"""
    seg = [
        {"joints": {"wrist_pitch": -69}, "duration": 0.35},
        {"joints": {"wrist_pitch": -39}, "duration": 0.35},
        {"joints": {"wrist_pitch": -69}, "duration": 0.35},
        {"joints": {"wrist_pitch": -47}, "duration": 0.45},
    ]
    out, err = validate_segments(seg)
    assert err is None
    frames = _build_frames(dict(HOME_POS), out)
    assert len(frames) > 0
    for f in frames:
        assert set(f.keys()) == set(HOME_POS.keys())
    _passed(f"compose 全链路：4 段 → {len(frames)} 帧")


def main():
    tests = [
        ("Phase 1: motion_scripts intensity scaling", [
            test_intensity_scales_amplitude,
            test_intensity_scales_duration,
            test_jitter_randomness,
            test_curious_with_pause_segments,
            test_intensity_clamp_in_motion_scripts,
            test_happy_wiggle_full_joint_set,
        ]),
        ("Phase 2: compose_motion validate_segments", [
            test_validate_ok,
            test_validate_too_few,
            test_validate_too_many,
            test_validate_unknown_joint,
            test_validate_out_of_range_value,
            test_validate_out_of_range_duration,
            test_validate_total_duration,
            test_validate_empty_joints,
            test_compose_to_build_frames_pipeline,
        ]),
    ]

    passed = 0
    failed = 0
    for group_name, group_tests in tests:
        print(f"\n{group_name}")
        for fn in group_tests:
            try:
                fn()
                passed += 1
            except AssertionError as e:
                print(f"  ✗ {fn.__name__}: {e}")
                failed += 1
            except Exception as e:
                print(f"  ✗ {fn.__name__}: {type(e).__name__}: {e}")
                traceback.print_exc()
                failed += 1

    print(f"\n{'─' * 40}")
    print(f"通过 {passed}  失败 {failed}")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
