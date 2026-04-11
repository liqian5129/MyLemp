"""真机测试：Phase 1 (intensity 参数化) + Phase 2 (compose_motion) 全量回归。

直接驱动 MotionAgent，不经过 LLM。每个 case 之间留 2 秒静止间隔。

用法：
    uv run python tests/test_motion_real.py

观察重点：
  Phase 1 程序化动作 intensity：
    A1 nod         — 三档幅度/速度差异（已加 hold）
    A2 curious     — 含停顿段动作，三档节奏变化
    A3 happy_wiggle — 双关节联动幅度对比
    A4 jitter      — 同 intensity 三次播放略有差异（无 seed）
    A5 headshake   — 三档（已加 hold）
    A6 excited     — 双档，多关节弹跳
    A7 sad         — 双档，慢动作 + 长停留
    A8 scanning    — 双档（仅 0.5/1.0 防越限）
    A9 shock       — 双档，快速后仰 + 停顿
    A10 shy        — 双档，三关节复合躲避

  Phase 2 compose_motion：
    C1 复刻 nod     — 与 A1 中档对照
    C2 复合三关节   — 歪头 + 低头
    C3 速度安全网   — 90° 跨度 / 0.2s，期待 velocity_audit 拉伸
    C4 非法输入     — 5 种校验失败路径，舵机不应动
    C5 边界值合法   — 2 段 / 8 段 / 总时长 6.0s
    C6 partial 关节继承 — seg 间未指定的关节自动保持
    C7 hold via 重复 — 同 joints 连续两段 = 凝视
    C8 队列衔接     — 长 compose + pending compose 顺序播放（不抢占）
    C9 链式连发     — 3 个 compose 无间隔，第 1 → 第 3 衔接，第 2 被丢弃
    C10 A↔C 队列   — express 与 compose 互相进入 pending 队列
    C11 漂移恢复    — compose 不回 HOME → 下一动作从漂移位起 → wake_up 收尾
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

# 允许直接 `uv run python tests/test_motion_real.py` 不需要设置 PYTHONPATH
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lelamp.motion.motion_agent import MotionAgent  # noqa: E402
from lelamp.utils import find_serial_port  # noqa: E402


GAP_SEC = 2.0


def _wait_idle(agent: MotionAgent, timeout: float = 10.0):
    """轮询等待动作播放完毕。"""
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


def _wake_home(agent: MotionAgent, label: str = "归位 wake_up → HOME"):
    """主动归位到 HOME，避免动作累积漂移。"""
    _section(label)
    agent.play_emotion("wake_up", intensity=1.0, jitter=0.0, seed=0)
    _wait_idle(agent, timeout=8.0)
    time.sleep(GAP_SEC)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1：A 路径 — 程序化动作 intensity
# ─────────────────────────────────────────────────────────────────────────────

def run_a1_nod_intensity(agent: MotionAgent):
    _section("A1: nod 三档 intensity（幅度 + 速度对比，含 hold）")
    for level in (0.4, 1.0, 1.4):
        _case(f"nod intensity={level}")
        agent.play_emotion("nod", intensity=level, jitter=0.0, seed=42)
        _wait_idle(agent)
        time.sleep(GAP_SEC)


def run_a2_curious_intensity(agent: MotionAgent):
    _section("A2: curious 三档（含停顿段，验证节奏缩放）")
    for level in (0.4, 1.0, 1.4):
        _case(f"curious intensity={level}")
        agent.play_emotion("curious", intensity=level, jitter=0.0, seed=7)
        _wait_idle(agent)
        time.sleep(GAP_SEC)


def run_a3_happy_wiggle(agent: MotionAgent):
    _section("A3: happy_wiggle 双关节联动（base_yaw + wrist_roll）")
    for level in (0.5, 1.4):
        _case(f"happy_wiggle intensity={level}")
        agent.play_emotion("happy_wiggle", intensity=level, jitter=0.0, seed=3)
        _wait_idle(agent)
        time.sleep(GAP_SEC)


def run_a4_jitter(agent: MotionAgent):
    _section("A4: jitter 微抖动可观察性（同 intensity，无 seed，三次）")
    for i in range(3):
        _case(f"nod intensity=1.0 jitter=0.2 (run {i + 1}/3)")
        agent.play_emotion("nod", intensity=1.0, jitter=0.2, seed=None)
        _wait_idle(agent)
        time.sleep(GAP_SEC)


def run_a5_headshake(agent: MotionAgent):
    _section("A5: headshake 三档（已加 hold，对比左右极限可见性）")
    for level in (0.4, 1.0, 1.4):
        _case(f"headshake intensity={level}")
        agent.play_emotion("headshake", intensity=level, jitter=0.0, seed=11)
        _wait_idle(agent)
        time.sleep(GAP_SEC)


def run_a6_excited(agent: MotionAgent):
    _section("A6: excited 双档（多关节弹跳：base_pitch + elbow + wrist）")
    for level in (0.5, 1.4):
        _case(f"excited intensity={level}")
        agent.play_emotion("excited", intensity=level, jitter=0.0, seed=4)
        _wait_idle(agent)
        time.sleep(GAP_SEC)


def run_a7_sad(agent: MotionAgent):
    _section("A7: sad 双档（慢动作 + 长停留，验证 _scaled_dur 在长 base 上正确）")
    for level in (0.5, 1.4):
        _case(f"sad intensity={level}")
        agent.play_emotion("sad", intensity=level, jitter=0.0, seed=5)
        _wait_idle(agent, timeout=15.0)  # sad 本身就 4s+
        time.sleep(GAP_SEC)


def run_a8_scanning(agent: MotionAgent):
    _section("A8: scanning 双档（仅 0.5/1.0 防 base_yaw 越限）")
    for level in (0.5, 1.0):
        _case(f"scanning intensity={level}")
        agent.play_emotion("scanning", intensity=level, jitter=0.0, seed=6)
        _wait_idle(agent, timeout=12.0)  # scanning 5s
        time.sleep(GAP_SEC)


def run_a9_shock(agent: MotionAgent):
    _section("A9: shock 双档（快速后仰 + 停顿，验证 base_pitch + wrist_pitch 联动）")
    for level in (0.5, 1.4):
        _case(f"shock intensity={level}")
        agent.play_emotion("shock", intensity=level, jitter=0.0, seed=8)
        _wait_idle(agent)
        time.sleep(GAP_SEC)


def run_a10_shy(agent: MotionAgent):
    _section("A10: shy 双档（三关节复合躲避：yaw + roll + wrist）")
    for level in (0.5, 1.4):
        _case(f"shy intensity={level}")
        agent.play_emotion("shy", intensity=level, jitter=0.0, seed=9)
        _wait_idle(agent, timeout=12.0)
        time.sleep(GAP_SEC)


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2：C 路径 — compose_motion
# ─────────────────────────────────────────────────────────────────────────────

def run_c1_compose_nod(agent: MotionAgent):
    _section("C1: compose_motion 复刻 nod（基线对照 A1 中档）")
    segments = [
        {"joints": {"wrist_pitch": -69}, "duration": 0.35},
        {"joints": {"wrist_pitch": -39}, "duration": 0.35},
        {"joints": {"wrist_pitch": -69}, "duration": 0.35},
        {"joints": {"wrist_pitch": -47}, "duration": 0.45},
    ]
    err = agent.play_compose("点头表示肯定（compose 复刻）", segments)
    print(f"  返回: {err or 'OK'}")
    _wait_idle(agent)
    time.sleep(GAP_SEC)


def run_c2_compose_compound(agent: MotionAgent):
    _section("C2: compose_motion 复合新动作（歪头 + 低头，三关节同时变）")
    segments = [
        {"joints": {"base_yaw": 22, "wrist_roll": 18, "wrist_pitch": -62}, "duration": 0.70},
        {"joints": {"base_yaw": 22, "wrist_roll": 18, "wrist_pitch": -62}, "duration": 0.40},
        {"joints": {"base_yaw":  7, "wrist_roll":  0, "wrist_pitch": -47}, "duration": 0.80},
    ]
    err = agent.play_compose("歪头同时低头打量（复合三关节）", segments)
    print(f"  返回: {err or 'OK'}")
    _wait_idle(agent)
    time.sleep(GAP_SEC)


def run_c3_velocity_audit(agent: MotionAgent):
    _section("C3: 速度安全网触发（90° 跨度 / 0.2s，期待 velocity_audit 拉伸）")
    segments = [
        {"joints": {"wrist_pitch":  20}, "duration": 0.20},
        {"joints": {"wrist_pitch": -75}, "duration": 0.20},
        {"joints": {"wrist_pitch": -47}, "duration": 0.40},
    ]
    err = agent.play_compose("极端跨度测试速度安全网", segments)
    print(f"  返回: {err or 'OK'}")
    print("  → 检查日志中是否出现 '⚙️ velocity_audit ... frames' 一行")
    _wait_idle(agent, timeout=15.0)
    time.sleep(GAP_SEC)


def run_c4_invalid_inputs(agent: MotionAgent):
    """5 种校验失败路径，逐个验证：返回 err 字符串、舵机不动、agent 仍在 idle。"""
    _section("C4: 非法输入（5 种），舵机应完全不动")
    bad_cases = [
        ("段数过少（1 段）",
         [{"joints": {"wrist_pitch": -50}, "duration": 0.4}]),
        ("未知关节",
         [{"joints": {"shoulder": 10}, "duration": 0.4},
          {"joints": {"wrist_pitch": -47}, "duration": 0.4}]),
        ("关节值越界（200°）",
         [{"joints": {"wrist_pitch": 200}, "duration": 0.4},
          {"joints": {"wrist_pitch": -47}, "duration": 0.4}]),
        ("duration 太短（0.05）",
         [{"joints": {"wrist_pitch": -50}, "duration": 0.05},
          {"joints": {"wrist_pitch": -47}, "duration": 0.4}]),
        ("空 joints dict",
         [{"joints": {}, "duration": 0.4},
          {"joints": {"wrist_pitch": -47}, "duration": 0.4}]),
    ]
    for label, segs in bad_cases:
        _case(label)
        err = agent.play_compose(f"故意非法：{label}", segs)
        playing = agent.is_playing()
        ok = (err is not None) and (not playing)
        print(f"  返回 err={err!r}  playing={playing}  {'✓' if ok else '✗'}")
    time.sleep(GAP_SEC)


def run_c5_boundary(agent: MotionAgent):
    _section("C5: 边界值合法输入（最少段 / 最大段 / 总时长 6.0s）")

    _case("2 段（最小数量）")
    err = agent.play_compose("最少 2 段", [
        {"joints": {"wrist_pitch": -55}, "duration": 0.50},
        {"joints": {"wrist_pitch": -47}, "duration": 0.50},
    ])
    print(f"  返回: {err or 'OK'}")
    _wait_idle(agent)
    time.sleep(GAP_SEC)

    _case("8 段（最大数量）")
    yaw_seq = [-12, 12, -8, 8, -5, 5, -2, 7]
    err = agent.play_compose("最多 8 段：base_yaw 渐进收敛", [
        {"joints": {"base_yaw": v}, "duration": 0.30} for v in yaw_seq
    ])
    print(f"  返回: {err or 'OK'}")
    _wait_idle(agent)
    time.sleep(GAP_SEC)

    _case("总时长正好 6.0s（边界）")
    err = agent.play_compose("总时长 6.0s", [
        {"joints": {"wrist_pitch": -65}, "duration": 1.5},
        {"joints": {"wrist_pitch": -30}, "duration": 1.5},
        {"joints": {"wrist_pitch": -65}, "duration": 1.5},
        {"joints": {"wrist_pitch": -47}, "duration": 1.5},
    ])
    print(f"  返回: {err or 'OK'}")
    _wait_idle(agent, timeout=12.0)
    time.sleep(GAP_SEC)


def run_c6_partial_joints(agent: MotionAgent):
    _section("C6: partial 关节继承（seg 间未指定的关节应保持上段值）")
    # 视觉预期：先低头 → 仍低着头转向左 → 抬头并回正
    segments = [
        {"joints": {"wrist_pitch": -65}, "duration": 0.60},                       # 仅低头
        {"joints": {"base_yaw": -18}, "duration": 0.60},                          # 仅转头（低头应保持）
        {"joints": {"base_yaw":  7, "wrist_pitch": -47}, "duration": 0.70},       # 同时回正
    ]
    err = agent.play_compose("低头→仍低着头转向左→回正（验证 partial 继承）", segments)
    print(f"  返回: {err or 'OK'}")
    print("  → 视觉确认：转向左时灯头应仍是低着的，没有先抬起再转")
    _wait_idle(agent)
    time.sleep(GAP_SEC)


def run_c7_hold_via_repeat(agent: MotionAgent):
    _section("C7: hold via 重复段（同 joints 连续两段 = 凝视）")
    segments = [
        {"joints": {"base_yaw": -22, "wrist_roll": 15}, "duration": 0.60},
        {"joints": {"base_yaw": -22, "wrist_roll": 15}, "duration": 0.80},   # 凝视
        {"joints": {"base_yaw":  7,  "wrist_roll":  0}, "duration": 0.70},
    ]
    err = agent.play_compose("歪头凝视后回正", segments)
    print(f"  返回: {err or 'OK'}")
    print("  → 视觉确认：歪头后应有明显凝视停顿（约 0.8s）")
    _wait_idle(agent)
    time.sleep(GAP_SEC)


def run_c8_queue(agent: MotionAgent):
    """当前设计：play_compose 在 playing 状态下进入 pending 队列，长动作播完才衔接。
    LLM 推理时延通常 > 单次动作时长，真实 main loop 不会出现"中途打断"场景。
    """
    _section("C8: 队列衔接（长 compose + pending compose 顺序播放）")
    long_segs = [
        {"joints": {"wrist_pitch": -75}, "duration": 1.5},
        {"joints": {"wrist_pitch":  10}, "duration": 1.5},
        {"joints": {"wrist_pitch": -47}, "duration": 1.0},
    ]
    short_segs = [
        {"joints": {"base_yaw": -18}, "duration": 0.50},
        {"joints": {"base_yaw":   7}, "duration": 0.50},
    ]
    _case("起 4s 长动作")
    err1 = agent.play_compose("长 compose", long_segs)
    print(f"  返回: {err1 or 'OK'}")
    time.sleep(0.8)  # 让长动作播一会儿

    _case("0.8s 后入队短动作（pending）")
    err2 = agent.play_compose("队列短 compose", short_segs)
    print(f"  返回: {err2 or 'OK'}")
    print("  → 期待：长动作完整播完（约 4s）→ 自动衔接短动作（约 1s）")
    _wait_idle(agent, timeout=8.0)
    time.sleep(GAP_SEC)


def run_c9_chain(agent: MotionAgent):
    _section("C9: 链式连发（3 个 compose 无间隔，验证 pending 不堆积）")
    chain = [
        [{"joints": {"wrist_pitch": -65}, "duration": 0.50},
         {"joints": {"wrist_pitch": -47}, "duration": 0.50}],
        [{"joints": {"base_yaw": -15}, "duration": 0.50},
         {"joints": {"base_yaw":   7}, "duration": 0.50}],
        [{"joints": {"wrist_roll": 18}, "duration": 0.50},
         {"joints": {"wrist_roll":  0}, "duration": 0.50}],
    ]
    for i, segs in enumerate(chain):
        _case(f"chain {i + 1}/3")
        err = agent.play_compose(f"chain step {i}", segs)
        print(f"  返回: {err or 'OK'}")
        time.sleep(0.05)  # 故意几乎不间隔
    print("  → 期待：第 1 个开始播 → 第 3 个替换为 pending → 第 1 播完自动衔接第 3")
    print("  → （第 2 个会被第 3 个替换掉，永远不播）")
    _wait_idle(agent, timeout=8.0)
    time.sleep(GAP_SEC)


def run_c10_mixed_a_c(agent: MotionAgent):
    """A↔C 路径互相进入对方的 pending 队列：先到的播完后衔接后入队的。"""
    _section("C10: A↔C 队列衔接（express ↔ compose 顺序播放）")

    _case("起 express_emotion(curious) 长动作")
    agent.play_emotion("curious", intensity=1.0, jitter=0.0, seed=7)
    time.sleep(0.5)

    _case("0.5s 后入队 compose（pending）")
    err = agent.play_compose("compose 跟在 express 后", [
        {"joints": {"wrist_pitch": -55}, "duration": 0.50},
        {"joints": {"wrist_pitch": -47}, "duration": 0.50},
    ])
    print(f"  返回: {err or 'OK'}")
    print("  → 期待：curious 完整播完 → 自动衔接 compose")
    _wait_idle(agent)
    time.sleep(GAP_SEC)

    _case("起 compose 长动作")
    agent.play_compose("compose 长", [
        {"joints": {"base_yaw": -20}, "duration": 1.2},
        {"joints": {"base_yaw":   7}, "duration": 1.0},
    ])
    time.sleep(0.5)

    _case("0.5s 后入队 express_emotion(nod)")
    agent.play_emotion("nod", intensity=1.0, jitter=0.0, seed=42)
    print("  → 期待：compose 长动作播完 → 自动衔接 nod")
    _wait_idle(agent)
    time.sleep(GAP_SEC)


def run_c11_drift_then_recover(agent: MotionAgent):
    _section("C11: compose 不回 HOME → 漂移 → 下个动作从漂移位起 → wake_up 收尾")
    drift_segs = [
        {"joints": {"base_yaw": -25, "wrist_pitch": -65}, "duration": 0.80},
        {"joints": {"base_yaw": -25, "wrist_pitch": -65}, "duration": 0.40},  # 结束于此（非 HOME）
    ]
    err = agent.play_compose("漂移：歪左低头并停留", drift_segs)
    print(f"  返回: {err or 'OK'}")
    _wait_idle(agent)
    pos = agent._get_current_pos()
    print(f"  漂移后位置: yaw={pos['base_yaw']:.1f} wrist_pitch={pos['wrist_pitch']:.1f}")
    time.sleep(GAP_SEC)

    _case("从漂移位起播 nod（应从当前位置出发，而不是从 HOME）")
    agent.play_emotion("nod", intensity=1.0, jitter=0.0, seed=42)
    _wait_idle(agent)
    pos = agent._get_current_pos()
    print(f"  nod 后位置: yaw={pos['base_yaw']:.1f} wrist_pitch={pos['wrist_pitch']:.1f}")
    print("  → 视觉确认：nod 在歪着头的位置上下点头，没有先回正")
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
        time.sleep(1.0)  # 等舵机就位
        _wake_home(agent)

        # ── Phase 1 老 case ────────────────────────────────────────────
        run_a1_nod_intensity(agent)
        run_a2_curious_intensity(agent)
        run_a3_happy_wiggle(agent)
        run_a4_jitter(agent)
        _wake_home(agent, "归位（A1-A4 后）")

        # ── Phase 1 新 case ────────────────────────────────────────────
        run_a5_headshake(agent)
        run_a6_excited(agent)
        run_a7_sad(agent)
        run_a8_scanning(agent)
        run_a9_shock(agent)
        run_a10_shy(agent)
        _wake_home(agent, "归位（A5-A10 后）")

        # ── Phase 2 老 case ────────────────────────────────────────────
        run_c1_compose_nod(agent)
        run_c2_compose_compound(agent)
        run_c3_velocity_audit(agent)
        _wake_home(agent, "归位（C1-C3 后）")

        # ── Phase 2 新 case ────────────────────────────────────────────
        run_c4_invalid_inputs(agent)
        run_c5_boundary(agent)
        run_c6_partial_joints(agent)
        run_c7_hold_via_repeat(agent)
        run_c8_queue(agent)
        run_c9_chain(agent)
        run_c10_mixed_a_c(agent)
        run_c11_drift_then_recover(agent)
        _wake_home(agent, "归位（C4-C11 后，收尾）")

        _section("全部 case 跑完")
        print("  Phase 1 ✓ (10 actions)  Phase 2 ✓ (11 cases)")
    finally:
        agent.stop()


if __name__ == "__main__":
    main()
