import argparse
import csv
import time
import os

from .follower import LeLampFollowerConfig, LeLampFollower


def smoothstep(t: float) -> float:
    return t * t * (3 - 2 * t)


def main():
    parser = argparse.ArgumentParser(description="Replay recorded actions from CSV file")
    parser.add_argument('--name', type=str, required=True, help='Name of the recording to replay')
    parser.add_argument('--port', type=str, required=True, help='Serial port for the robot')
    parser.add_argument('--id', type=str, required=True, help='ID of the robot')
    parser.add_argument('--fps', type=int, default=30, help='Frames per second for replay (default: 30)')
    parser.add_argument('--warmup', type=float, default=2.0, help='软启动过渡时长（秒，默认2.0）')
    args = parser.parse_args()

    robot_config = LeLampFollowerConfig(port=args.port, id=args.id)
    robot = LeLampFollower(robot_config)
    robot.connect(calibrate=False)

    recordings_dir = os.path.join(os.path.dirname(__file__), "recordings")
    csv_path = os.path.join(recordings_dir, f"{args.name}.csv")

    with open(csv_path, 'r') as csvfile:
        actions = list(csv.DictReader(csvfile))

    print(f"Replaying {len(actions)} actions from {csv_path}")

    # 软启动：从当前位置插值到第一帧，smoothstep 平滑过渡
    current = robot.bus.sync_read("Present_Position")
    first = {k.removesuffix(".pos"): float(v) for k, v in actions[0].items() if k != 'timestamp'}
    warmup_frames = max(1, int(args.warmup * args.fps))
    print(f"软启动：{args.warmup}s 平滑过渡到起始位置...")
    for i in range(warmup_frames):
        t = smoothstep((i + 1) / warmup_frames)
        interp = {m: current[m] + (first[m] - current[m]) * t for m in first}
        robot.send_action({f"{m}.pos": v for m, v in interp.items()})
        time.sleep(1.0 / args.fps)

    print("开始回放...")
    replay_start = time.perf_counter()
    record_start = float(actions[0]['timestamp'])

    for i, row in enumerate(actions):
        action = {key: float(value) for key, value in row.items() if key != 'timestamp'}
        robot.send_action(action)

        elapsed_record = float(row['timestamp']) - record_start
        elapsed_replay = time.perf_counter() - replay_start
        wait = elapsed_record - elapsed_replay
        if wait > 0:
            time.sleep(wait)

    # 保持最后姿势直到 Ctrl+C
    last_action = {key: float(value) for key, value in actions[-1].items() if key != 'timestamp'}
    print("回放完成，保持最后姿势（Ctrl+C 退出）...")
    try:
        while True:
            robot.send_action(last_action)
            time.sleep(0.1)
    except KeyboardInterrupt:
        pass

    robot.disconnect()
    print("已退出")


if __name__ == "__main__":
    main()
