"""Demo：台灯机器人叫醒趴桌睡觉的用户。

完整序列：
  1. wake_up           回到 HOME，起点一致
  2. curious           环顾发现你在桌上趴着
  3. pause 1.5s        打量
  4. wake_me_up.json   手动录制的探头 — 灯头伸到你手臂旁
  5. 两次慢点头        programmatically：wrist_pitch 慢下→抬→慢下→抬（触碰手臂）
  6. pause 2.5s        等你反应
  7. 慢速抬起          其余关节回 HOME，base_yaw 转到 +30°（朝你这边偏）
  8. shy               害羞偏头，缓缓回正；结束后保持姿态直到 Ctrl+C

前置录制：
  uv run python -m lelamp.record_keyframe --name wake_me_up --intent "探头到用户手臂旁"
"""
import logging
import time

from lelamp.motion.motion_agent import MotionAgent
from lelamp.motion.play_keyframes import load_and_play, load_keyframes
from lelamp.service.motors.motion_scripts import HOME_POS
from lelamp.utils import find_serial_port


# ── 可调参数 ──────────────────────────────────────────────────────────
NOTICE_PAUSE    = 1.5   # curious 后打量用户的停顿
AFTER_REACH     = 0.3   # 探头到位后稍顿再点头
AFTER_TAP_PAUSE = 2.5   # 两次点头后等反应
RISE_BASE_YAW   = 50.0  # 抬起后 base_yaw 目标（其余关节回 HOME）
RISE_DUR        = 2.5   # 抬起过渡时长（秒）
BEFORE_SHY      = 5.0   # 抬起后停留多久再做害羞动作

# 点头触碰的 wrist_pitch 偏移（正值=头朝下）
# 以录制末尾的 wrist_pitch 为基准，±DELTA 做往复
TAP_DOWN_DELTA  = 22.0  # 低头幅度 — 太浅够不到，太深会顶手臂，按实际手感调
TAP_UP_DELTA    = -3.0  # 抬起比基准再微抬一点（给下一次低头留余量）
TAP_DOWN_DUR    = 1.0   # 慢慢低下（慢=温柔）
TAP_UP_DUR      = 0.6   # 抬起稍快


def _wait_idle(agent: MotionAgent, settle: float = 1.0):
    while agent.is_playing():
        time.sleep(0.1)
    time.sleep(settle)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    port = find_serial_port()
    print(f"串口: {port}")

    # 读录制 JSON，从其末尾姿态取 wrist_pitch 作为点头基准
    data = load_keyframes("wake_me_up")
    rest_wp = float(data["segments"][-1]["joints"]["wrist_pitch"])
    tap_down = rest_wp + TAP_DOWN_DELTA
    tap_up   = rest_wp + TAP_UP_DELTA
    print(f"点头基准 wrist_pitch={rest_wp:.1f} → down={tap_down:.1f} up={tap_up:.1f}")

    agent = MotionAgent(port=port, lamp_id="lelamp", fps=30)
    agent.start()

    try:
        print("\n=== 1/5 wake_up → HOME ===")
        agent.play_emotion("wake_up")
        _wait_idle(agent)

        print("\n=== 2/5 curious（发现你在趴着）===")
        agent.play_emotion("curious")
        _wait_idle(agent, settle=NOTICE_PAUSE)

        print("\n=== 3/5 探头到你手臂旁 ===")
        err = load_and_play(agent, "wake_me_up")
        if err:
            print(f"播放失败: {err}")
            return
        _wait_idle(agent, settle=AFTER_REACH)

        print("\n=== 4/5 两次慢点头触碰 ===")
        tap_segments = [
            {"joints": {"wrist_pitch": tap_down}, "duration": TAP_DOWN_DUR},
            {"joints": {"wrist_pitch": tap_up},   "duration": TAP_UP_DUR},
            {"joints": {"wrist_pitch": tap_down}, "duration": TAP_DOWN_DUR},
            {"joints": {"wrist_pitch": tap_up},   "duration": TAP_UP_DUR},
        ]
        err = agent.play_keyframes(tap_segments, intent="两次慢点头触碰手臂")
        if err:
            print(f"点头失败: {err}")
            return
        _wait_idle(agent, settle=AFTER_TAP_PAUSE)

        print(f"\n=== 5/6 慢慢抬起（base_yaw→{RISE_BASE_YAW}°，其余回 HOME）===")
        rise_target = {**HOME_POS, "base_yaw": RISE_BASE_YAW}
        err = agent.play_keyframes(
            [{"joints": rise_target, "duration": RISE_DUR}],
            intent="抬起到偏转 home 姿态",
        )
        if err:
            print(f"抬起失败: {err}")
            return
        _wait_idle(agent, settle=BEFORE_SHY)

        print("\n=== 6/6 shy（害羞）===")
        agent.play_emotion("shy")
        _wait_idle(agent)

        print("\n=== Demo 完成，保持姿态  Ctrl+C 退出 ===")
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n收到 Ctrl+C，退出中...")
    finally:
        agent.stop()


if __name__ == "__main__":
    main()
