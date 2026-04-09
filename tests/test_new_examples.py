"""测试新 compose_motion 示例候选动作（真机）。

目的：扩充 few-shot 示例库的运动词汇多样性。
现有示例覆盖：左右摇、上下点、前后仰、弹跳、缓降、歪头。
候选新示例填补：鞠躬、蛇形传导、渐强摇摆、环视扫描。

用法：
    uv run python tests/test_new_examples.py

HOME: base_yaw=7  base_pitch=-38  elbow_pitch=49  wrist_roll=0  wrist_pitch=-47
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lelamp.motion.motion_agent import MotionAgent  # noqa: E402
from lelamp.utils import find_serial_port  # noqa: E402

GAP_SEC = 2.5


def _wait_idle(agent: MotionAgent, timeout: float = 12.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not agent.is_playing():
            return
        time.sleep(0.05)
    print(f"  ⚠ wait_idle 超时 ({timeout}s)")


def _section(title: str):
    print()
    print("─" * 60)
    print(f"  {title}")
    print("─" * 60)


def _case(label: str):
    print(f"\n▶ {label}")


def _wake_home(agent: MotionAgent):
    _section("归位 wake_up → HOME")
    agent.play_emotion("wake_up", intensity=1.0, jitter=0.0, seed=0)
    _wait_idle(agent)
    time.sleep(GAP_SEC)


# ─────────────────────────────────────────────────────────────────────────────
# 候选示例
# ─────────────────────────────────────────────────────────────────────────────

def run_bow(agent: MotionAgent):
    """鞠躬：elbow + base_pitch 前倾，wrist_pitch 深低头，停顿后回正。
    填补词汇：elbow_pitch 大幅运动 + 深度前倾姿态。
    """
    _section("N1: bow 鞠躬")
    segments = [
        {"joints": {"elbow_pitch": 65, "wrist_pitch": -35},                     "duration": 0.7},   # 先挺起来
        {"joints": {"elbow_pitch": 10, "base_pitch": -45, "wrist_pitch": -75},  "duration": 1.0},   # 往下弯
        {"joints": {"wrist_pitch": -86},                                         "duration": 0.35},  # 弯着点头
        {"joints": {"wrist_pitch": -75},                                         "duration": 0.35},  # 头回（仍弯着）
        {"joints": {"elbow_pitch": 49, "base_pitch": -38, "wrist_pitch": -47},  "duration": 1.0},   # 起身
    ]
    err = agent.play_compose("鞠躬：先挺身，再弯腰，弯着点头，起身", segments)
    print(f"  返回: {err or 'OK'}")
    print("  → 观察：先挺起来一点，再往下弯，弯着时头往下点一下，然后起身")
    _wait_idle(agent)
    time.sleep(GAP_SEC)


def run_wave(agent: MotionAgent):
    """蛇形传导：每段只新增一个关节运动，前一个关节开始回收，强调相位差。
    填补词汇：多关节相位错开，清晰的顺序传导感。
    """
    _section("N2: wave 蛇形传导")
    # 波浪沿臂纵向传导：base_pitch → elbow_pitch → wrist_pitch，不转 base_yaw
    # 每拍一个关节做脉冲（偏离→回收），下一拍传给下一个关节
    segments = [
        {"joints": {"base_pitch": -48},                                          "duration": 0.35},  # 拍1: base 前倾
        {"joints": {"base_pitch": -36, "elbow_pitch": 62},                       "duration": 0.35},  # 拍2: base 回，elbow 伸
        {"joints": {"elbow_pitch": 47, "wrist_pitch": -65},                      "duration": 0.35},  # 拍3: elbow 回，wrist 垂
        {"joints": {"wrist_pitch": -45, "base_pitch": -48},                      "duration": 0.35},  # 拍4: wrist 回，base 前倾（第2轮）
        {"joints": {"base_pitch": -36, "elbow_pitch": 60},                       "duration": 0.35},  # 拍5: base 回，elbow 伸
        {"joints": {"elbow_pitch": 48, "wrist_pitch": -62, "wrist_roll": 15},    "duration": 0.35},  # 拍6: elbow 回，wrist 垂+roll
        {"joints": {"wrist_pitch": -47, "wrist_roll": 0, "base_pitch": -45},     "duration": 0.35},  # 拍7: wrist 回，base 小倾
        {"joints": {"base_pitch": -38, "elbow_pitch": 49},                       "duration": 0.4},   # 收
    ]
    err = agent.play_compose("蛇形传导：波浪沿臂纵向传导，两轮半", segments)
    print(f"  返回: {err or 'OK'}")
    print("  → 观察：波浪从底座→肘→腕纵向传导，像能量沿手臂向上涌")
    _wait_idle(agent)
    time.sleep(GAP_SEC)


def run_stretch(agent: MotionAgent):
    """伸懒腰：用到全部 5 个关节，三阶段编排（伸展→扭转→收回叹气）。
    填补词汇：全关节协调、多阶段叙事、非对称运动。
    """
    _section("N3: stretch 伸懒腰")
    segments = [
        # 阶段1: 伸展——臂往上撑开，头仰起
        {"joints": {"elbow_pitch": 68, "base_pitch": -32, "wrist_pitch": -30}, "duration": 1.0},
        {"joints": {"elbow_pitch": 68, "base_pitch": -32, "wrist_pitch": -30}, "duration": 0.4},   # hold
        # 阶段2: 扭转——保持伸展，身体左右扭
        {"joints": {"base_yaw": 25, "wrist_roll": 22},                          "duration": 0.6},
        {"joints": {"base_yaw": -15, "wrist_roll": -20},                        "duration": 0.7},
        # 阶段3: 收回——慢慢缩回来，头低下"叹口气"
        {"joints": {"elbow_pitch": 45, "base_pitch": -40, "wrist_pitch": -60},  "duration": 0.8},
        {"joints": {"elbow_pitch": 49, "base_pitch": -38, "wrist_pitch": -47,
                     "base_yaw": 7, "wrist_roll": 0},                           "duration": 0.7},
    ]
    err = agent.play_compose("伸懒腰：伸展→扭转→叹气收回，全 5 关节", segments)
    print(f"  返回: {err or 'OK'}")
    print("  → 观察：先撑开身体仰头，左右扭一下，再慢慢缩回来低头")
    _wait_idle(agent)
    time.sleep(GAP_SEC)


def run_scan(agent: MotionAgent):
    """环视扫描：两轮慢扫，每个停靠点低头"看一看"再继续，6 段长动作。
    填补词汇：大幅度 + 长 duration 慢动作 + 停靠凝视，多段编排。
    """
    _section("N4: scan 环视扫描")
    segments = [
        {"joints": {"base_yaw": -30, "wrist_pitch": -58}, "duration": 1.0},   # 扫左 + 低头看
        {"joints": {"base_yaw": -30, "wrist_pitch": -58}, "duration": 0.4},   # 停靠凝视
        {"joints": {"base_yaw": 30,  "wrist_pitch": -38}, "duration": 1.2},   # 扫右 + 抬头看
        {"joints": {"base_yaw": 30,  "wrist_pitch": -38}, "duration": 0.4},   # 停靠凝视
        {"joints": {"base_yaw": -20, "wrist_pitch": -55}, "duration": 1.0},   # 再扫左
        {"joints": {"base_yaw": 7,   "wrist_pitch": -47}, "duration": 0.8},   # 回正
    ]
    err = agent.play_compose("环视扫描：两轮慢扫，停靠点凝视", segments)
    print(f"  返回: {err or 'OK'}")
    print("  → 观察：慢扫到左侧停一下'看看'，再扫到右侧停一下，再回来")
    _wait_idle(agent)
    time.sleep(GAP_SEC)


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    port = find_serial_port()
    agent = MotionAgent(port=port, lamp_id="lelamp", fps=30)
    agent.start()

    try:
        time.sleep(1.0)
        _wake_home(agent)

        run_bow(agent)
        _wake_home(agent)

        run_wave(agent)
        _wake_home(agent)

        run_stretch(agent)
        _wake_home(agent)

        run_scan(agent)
        _wake_home(agent)

        _section("全部候选示例测试完毕")
        print("  N1 bow 鞠躬 | N2 wave 蛇形 | N3 stretch 伸懒腰 | N4 scan 环视")
        print("  请记录每个动作的观感，确认后将加入 compose_motion few-shot 示例库")
    finally:
        agent.stop()


if __name__ == "__main__":
    main()
