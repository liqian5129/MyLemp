"""
wrist_pitch 方向验证脚本

流程：
1. 连接舵机，读取当前位置
2. 移到 HOME
3. 移到 wrist_pitch=80 → 让用户观察灯头是低垂还是抬起
4. 回 HOME
5. 移到 wrist_pitch=20 → 让用户观察
6. 回 HOME

运行：uv run python test_wrist_pitch.py
"""
import time

from lelamp.motion.motion_agent import MotionAgent
from lelamp.service.motors.motion_scripts import HOME_POS
from lelamp.utils import find_serial_port


def main():
    port = find_serial_port()
    print(f"串口: {port}")

    agent = MotionAgent(port=port, lamp_id="lelamp", fps=30)
    agent.start()
    time.sleep(0.5)

    print(f"\nHOME_POS = {HOME_POS}")
    print(f"wrist_pitch HOME = {HOME_POS['wrist_pitch']}")

    # ── Step 1: 回 HOME ──
    input("\n按回车 → 移到 HOME 位置...")
    agent.play_waypoint({k: v for k, v in HOME_POS.items()}, duration=2.0)
    time.sleep(2.5)
    print("✅ 已到 HOME")

    # ── Step 2: wrist_pitch = 80 ──
    input("\n按回车 → wrist_pitch 设为 80（HOME=55，比 HOME 大 25°）...")
    agent.play_waypoint({"wrist_pitch": 80}, duration=1.5)
    time.sleep(2.0)

    answer = input("👀 灯头现在是【低垂/朝下】还是【抬起/朝上】？(输入 '下' 或 '上'): ").strip()
    if "下" in answer:
        print("→ 正值=低垂 ✓  方向假设正确！")
        direction_confirmed = True
    elif "上" in answer:
        print("→ 正值=抬起 ✗  方向假设错误，需要翻转回来！")
        direction_confirmed = False
    else:
        print(f"→ 未识别 '{answer}'，继续测试...")
        direction_confirmed = None

    # ── Step 3: 回 HOME ──
    input("\n按回车 → 回 HOME...")
    agent.play_waypoint({"wrist_pitch": HOME_POS["wrist_pitch"]}, duration=1.5)
    time.sleep(2.0)

    # ── Step 4: wrist_pitch = 20 ──
    input("\n按回车 → wrist_pitch 设为 20（HOME=55，比 HOME 小 35°）...")
    agent.play_waypoint({"wrist_pitch": 20}, duration=1.5)
    time.sleep(2.0)

    answer2 = input("👀 灯头现在是【低垂/朝下】还是【抬起/朝上】？(输入 '下' 或 '上'): ").strip()
    if "上" in answer2:
        print("→ 负方向=抬起 ✓  方向假设正确！")
    elif "下" in answer2:
        print("→ 负方向=低垂 ✗  方向假设错误！")

    # ── Step 5: 回 HOME ──
    input("\n按回车 → 回 HOME...")
    agent.play_waypoint({k: v for k, v in HOME_POS.items()}, duration=1.5)
    time.sleep(2.0)

    # ── 结论 ──
    print("\n" + "=" * 50)
    if direction_confirmed is True:
        print("结论：wrist_pitch 方向反转假设正确")
        print("  正值 = 低垂（头朝下）")
        print("  负值 = 抬起（头朝上）")
        print("  当前代码无需修改。")
    elif direction_confirmed is False:
        print("结论：wrist_pitch 方向反转假设错误！")
        print("  正值 = 抬起（头朝上）— 与旧版相同")
        print("  负值 = 低垂（头朝下）— 与旧版相同")
        print("  需要撤销所有 wrist_pitch 方向修改！")
    else:
        print("结论：未确认，请手动观察重新测试。")
    print("=" * 50)

    agent.stop()


if __name__ == "__main__":
    main()
