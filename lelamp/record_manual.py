"""
手动拖动录制脚本（无需 leader）
用法: uv run python -m lelamp.record_manual --id lelamp --port /dev/cu.usbmodem5B141150361 --name nod
"""
import argparse
import csv
import os
import time

from .follower import LeLampFollower, LeLampFollowerConfig
from lerobot.motors.feetech import OperatingMode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--name", required=True, help="录制名称，如 nod / shake / curious")
    parser.add_argument("--fps", type=int, default=30)
    args = parser.parse_args()

    config = LeLampFollowerConfig(port=args.port, id=args.id)
    robot = LeLampFollower(config)
    robot.connect(calibrate=False)

    # 关闭力矩，允许手动拖动
    robot.bus.disable_torque()
    for motor in robot.bus.motors:
        robot.bus.write("Operating_Mode", motor, OperatingMode.POSITION.value)

    recordings_dir = os.path.join(os.path.dirname(__file__), "recordings")
    os.makedirs(recordings_dir, exist_ok=True)
    csv_path = os.path.join(recordings_dir, f"{args.name}.csv")

    input(f"\n力矩已关闭，可以手动摆好起始姿势。\n准备好后按回车开始录制（Ctrl+C 停止）...")

    print(f"录制中... -> {csv_path}")
    frame = 0
    with open(csv_path, "w", newline="") as f:
        writer = None
        try:
            while True:
                t0 = time.perf_counter()
                obs = robot.bus.sync_read("Present_Position")

                if writer is None:
                    fieldnames = ["timestamp"] + [f"{k}.pos" for k in obs.keys()]
                    writer = csv.DictWriter(f, fieldnames=fieldnames)
                    writer.writeheader()

                writer.writerow({"timestamp": t0, **{f"{k}.pos": v for k, v in obs.items()}})
                f.flush()
                frame += 1

                sleep = 1.0 / args.fps - (time.perf_counter() - t0)
                if sleep > 0:
                    time.sleep(sleep)

        except KeyboardInterrupt:
            pass

    robot.disconnect()
    print(f"\n录制完成，共 {frame} 帧，保存至 {csv_path}")


if __name__ == "__main__":
    main()
