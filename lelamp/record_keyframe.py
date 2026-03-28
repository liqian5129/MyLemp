"""
关键帧录制脚本：手动拖到姿势 → 按回车保存关键帧 → 自动插值生成平滑 CSV
用法: uv run python -m lelamp.record_keyframe --id lelamp --port /dev/cu.usbmodem5B141150361 --name nod
"""
import argparse
import csv
import os
import time
import numpy as np

from .follower import LeLampFollower, LeLampFollowerConfig
from lerobot.motors.feetech import OperatingMode


def interpolate(kf_positions: list[dict], kf_durations: list[float], fps: int) -> list[dict]:
    """在关键帧之间做线性插值，返回每帧的位置列表"""
    frames = []
    motors = list(kf_positions[0].keys())

    for i in range(len(kf_positions) - 1):
        start = kf_positions[i]
        end = kf_positions[i + 1]
        n_frames = max(1, int(kf_durations[i] * fps))

        for f in range(n_frames):
            t = f / n_frames  # 0.0 ~ 1.0
            # 平滑插值（smoothstep）
            t_smooth = t * t * (3 - 2 * t)
            frame = {m: start[m] + (end[m] - start[m]) * t_smooth for m in motors}
            frames.append(frame)

    # 最后一帧
    frames.append(kf_positions[-1])
    return frames


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id", required=True)
    parser.add_argument("--port", required=True)
    parser.add_argument("--name", required=True, help="录制名称")
    parser.add_argument("--fps", type=int, default=30)
    args = parser.parse_args()

    config = LeLampFollowerConfig(port=args.port, id=args.id)
    robot = LeLampFollower(config)
    robot.connect(calibrate=False)

    robot.bus.disable_torque()
    for motor in robot.bus.motors:
        robot.bus.write("Operating_Mode", motor, OperatingMode.POSITION.value)

    print("=" * 50)
    print("关键帧录制模式")
    print("  拖动到目标姿势后按回车保存关键帧")
    print("  输入 q 完成录制并生成 CSV")
    print("=" * 50)

    keyframes = []    # 每个关键帧的位置
    durations = []    # 每段过渡时长（秒）

    while True:
        user_input = input(f"\n[帧{len(keyframes)}] 拖到目标姿势后按回车，或输入 q 完成: ").strip().lower()
        if user_input == "q":
            if len(keyframes) < 2:
                print("至少需要 2 个关键帧才能生成动作，继续添加。")
                continue
            break

        pos = robot.bus.sync_read("Present_Position")
        keyframes.append(dict(pos))
        print(f"  已保存: { {k: round(v, 1) for k, v in pos.items()} }")

        if len(keyframes) >= 2:
            while True:
                try:
                    dur = float(input(f"  这段过渡时长（秒，默认1.0）: ").strip() or "1.0")
                    durations.append(dur)
                    break
                except ValueError:
                    print("  请输入数字")

    robot.disconnect()

    # 插值生成所有帧
    print(f"\n生成插值帧（{len(keyframes)} 个关键帧，fps={args.fps}）...")
    frames = interpolate(keyframes, durations, args.fps)
    print(f"共生成 {len(frames)} 帧")

    # 保存 CSV
    recordings_dir = os.path.join(os.path.dirname(__file__), "recordings")
    os.makedirs(recordings_dir, exist_ok=True)
    csv_path = os.path.join(recordings_dir, f"{args.name}.csv")

    motors = list(keyframes[0].keys())
    t = 0.0
    dt = 1.0 / args.fps
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp"] + [f"{m}.pos" for m in motors])
        writer.writeheader()
        for frame in frames:
            writer.writerow({"timestamp": t, **{f"{m}.pos": v for m, v in frame.items()}})
            t += dt

    print(f"已保存至 {csv_path}")


if __name__ == "__main__":
    main()
