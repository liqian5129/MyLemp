"""
关节物理极限测试 — 手动拖动版

流程：关掉力矩 → 你手动把关节掰到极限 → 按回车读取当前角度
每个关节测两次（正极限、负极限），最后输出汇总表。

运行：uv run python test_joint_limits.py
"""
from lelamp.follower import LeLampFollower, LeLampFollowerConfig
from lelamp.service.motors.motion_scripts import HOME_POS
from lelamp.utils import find_serial_port
from lerobot.motors.feetech import OperatingMode

JOINTS = [
    ("base_yaw",    "水平旋转，负=左，正=右"),
    ("base_pitch",  "俯仰，负=直立/后仰，正=前倾"),
    ("elbow_pitch", "臂弯曲，小=伸直，大=弯曲"),
    ("wrist_roll",  "灯头左右歪，负=左歪，正=右歪"),
    ("wrist_pitch", "灯头俯仰，小=抬头，大=低垂"),
]


def read_positions(robot) -> dict[str, float]:
    raw = robot.bus.sync_read("Present_Position")
    return {k.replace(".pos", ""): v for k, v in raw.items()}


def main():
    port = find_serial_port()
    print(f"串口: {port}")

    config = LeLampFollowerConfig(port=port, id="lelamp")
    robot = LeLampFollower(config)
    robot.connect(calibrate=False)

    # 关闭力矩
    robot.bus.disable_torque()
    for motor in robot.bus.motors:
        robot.bus.write("Operating_Mode", motor, OperatingMode.POSITION.value)

    print("\n力矩已关闭，可以自由拖动。")

    # 先读一下当前位置
    pos = read_positions(robot)
    print(f"当前位置: { {k: f'{v:.1f}' for k, v in pos.items()} }")
    print(f"HOME参考: { {k: f'{v:.1f}' for k, v in HOME_POS.items()} }\n")

    results = {}

    for joint, desc in JOINTS:
        print(f"{'='*50}")
        print(f"  {joint} ({desc})")
        print(f"  HOME = {HOME_POS[joint]:.1f}")
        print(f"{'='*50}")

        # 负极限
        input(f"  把 {joint} 掰到【负极限】方向，然后按回车...")
        pos = read_positions(robot)
        neg = pos.get(joint, 0)
        print(f"  读到: {neg:.1f}")

        # 正极限
        input(f"  把 {joint} 掰到【正极限】方向，然后按回车...")
        pos = read_positions(robot)
        pos_val = pos.get(joint, 0)
        print(f"  读到: {pos_val:.1f}")

        # 确保 neg < pos
        if neg > pos_val:
            neg, pos_val = pos_val, neg

        results[joint] = (neg, pos_val)
        print(f"  → {joint}: [{neg:.1f}, {pos_val:.1f}]\n")

    # 汇总
    print(f"\n{'='*60}")
    print("测试结果汇总")
    print(f"{'='*60}")
    print(f"{'关节':<15} {'负极限':>8} {'HOME':>8} {'正极限':>8}")
    print("-" * 45)
    for joint, _ in JOINTS:
        neg, pos_val = results[joint]
        home = HOME_POS[joint]
        print(f"{joint:<15} {neg:>8.1f} {home:>8.1f} {pos_val:>8.1f}")

    print(f"\n可直接粘贴到代码：")
    print("JOINT_LIMITS = {")
    for joint, _ in JOINTS:
        neg, pos_val = results[joint]
        print(f'    "{joint}": ({neg:.1f}, {pos_val:.1f}),')
    print("}")

    robot.disconnect()


if __name__ == "__main__":
    main()
