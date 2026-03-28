"""
录制 HOME 站立位置
用法: uv run python record_home_pos.py --port /dev/cu.usbmodem5B141150361

脚本会关闭扭矩，让你手动把灯调到合适的站立姿势，
按回车后读取当前位置，打印出可直接粘贴到 motion_scripts.py 的 HOME_POS。
"""
import argparse
import time
from lelamp.follower import LeLampFollowerConfig, LeLampFollower


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    parser.add_argument("--id", default="lelamp")
    args = parser.parse_args()

    robot = LeLampFollower(LeLampFollowerConfig(port=args.port, id=args.id))
    robot.connect(calibrate=False)

    # 关闭扭矩，让用户自由摆放
    robot.bus.disable_torque()
    print("\n扭矩已关闭，请手动把灯调到你想要的站立姿势。")
    print("调好后按 Enter 记录位置...")
    input()

    pos = robot.bus.sync_read("Present_Position")

    print("\n当前位置：")
    for k, v in pos.items():
        print(f"  {k}: {v:.1f}")

    print("\n可直接粘贴到 motion_scripts.py 的 HOME_POS：")
    print("HOME_POS = {")
    for k, v in pos.items():
        print(f'    "{k}": {round(v, 1)},')
    print("}")

    robot.bus.enable_torque()
    robot.disconnect()
    print("\n已断开。")


if __name__ == "__main__":
    main()
