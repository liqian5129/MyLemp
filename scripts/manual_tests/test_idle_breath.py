"""
测试 idle_breath 呼吸动作

流程：wake_up → 依次播放 5 种呼吸变体 → 结束
每个变体之间停顿 2 秒，方便观察。

运行：
    uv run python test_idle_breath.py
"""
import time

from lelamp.motion.motion_agent import MotionAgent
from lelamp.utils import find_serial_port

# seed → 变体映射（预计算）
SEED_MAP = [
    (0, "stretch"),
    (1, "tilt_l"),
    (2, "sigh"),
    (5, "drift"),
    (7, "tilt_r"),
]


def main():
    port = find_serial_port()
    print(f"串口: {port}")

    agent = MotionAgent(port=port, lamp_id="lelamp", fps=30)
    agent.start()

    # wake up
    print("\n=== wake_up ===")
    agent.play_emotion("wake_up")
    time.sleep(3.0)

    # 依次播放 5 种变体（用 seed 固定）
    for seed, name in SEED_MAP:
        print(f"\n=== idle_breath: {name} (seed={seed}) ===")
        agent.play_emotion("idle_breath", intensity=0.8, seed=seed)
        time.sleep(4.0)

    # 再来 3 次随机（不固定 seed）
    for i in range(3):
        print(f"\n=== idle_breath 随机 #{i+1} ===")
        agent.play_emotion("idle_breath", intensity=0.8)
        time.sleep(4.0)

    print("\n测试结束")
    agent.stop()


if __name__ == "__main__":
    main()
