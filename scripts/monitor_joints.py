"""
关节角度实时监视器 — 手动拖动关节时查看角度值

运行：uv run python monitor_joints.py
退出：Ctrl+C

启动后自动关闭力矩，可自由拖动各关节。
终端每 0.1s 刷新一次当前角度。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lelamp.follower import LeLampFollower, LeLampFollowerConfig  # noqa: E402
from lelamp.service.motors.motion_scripts import HOME_POS  # noqa: E402
from lelamp.utils import find_serial_port  # noqa: E402


JOINT_NAMES = {
    "base_yaw":    "底座水平",
    "base_pitch":  "底座俯仰",
    "elbow_pitch": "肘部弯曲",
    "wrist_roll":  "灯头横滚",
    "wrist_pitch": "灯头俯仰",
}

REFRESH_HZ = 10  # 刷新频率


def main():
    port = find_serial_port()
    print(f"串口: {port}")

    config = LeLampFollowerConfig(port=port, id="lelamp")
    robot = LeLampFollower(config)
    robot.connect(calibrate=False)

    # 关闭力矩，允许自由拖动
    robot.bus.disable_torque()
    print("力矩已关闭，可以自由拖动各关节。按 Ctrl+C 退出。\n")

    dt = 1.0 / REFRESH_HZ
    joints = list(robot.bus.motors.keys())

    # 打印表头（只打印一次，后续用 ANSI 转义覆盖）
    header_lines = 2  # 标题 + 分隔线
    n_joints = len(joints)
    total_lines = header_lines + n_joints + 1  # +1 底部空行

    try:
        while True:
            pos = robot.bus.sync_read("Present_Position")

            # 构建显示内容
            lines = []
            lines.append(f"  {'关节':<15} {'中文名':<10} {'当前角度':>10} {'HOME':>10} {'偏差':>10}")
            lines.append("  " + "-" * 60)
            for j in joints:
                angle = pos.get(j, 0.0)
                home = HOME_POS.get(j, 0.0)
                diff = angle - home
                cn = JOINT_NAMES.get(j, "")
                lines.append(f"  {j:<15} {cn:<10} {angle:>10.1f} {home:>10.1f} {diff:>+10.1f}")
            lines.append("")

            # 用 ANSI 转义移到区域开头并覆盖
            sys.stdout.write(f"\033[{total_lines}A")  # 光标上移
            for line in lines:
                sys.stdout.write(f"\033[2K{line}\n")  # 清行 + 写入
            sys.stdout.flush()

            time.sleep(dt)
    except KeyboardInterrupt:
        print("\n\n正在断开...")
    finally:
        robot.disconnect()
        print("已断开。")


if __name__ == "__main__":
    # 先输出占位行，确保第一次 ANSI 上移不会超出屏幕
    joints = list(JOINT_NAMES.keys())
    total_lines = 2 + len(joints) + 1
    for _ in range(total_lines):
        print()
    main()
