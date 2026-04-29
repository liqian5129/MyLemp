"""关键帧录制脚本：手动拖到姿势 → 按回车保存关键帧 → 写 JSON（播放时再插值）。

JSON 格式（与 compose_motion/play_keyframes 通用）:
{
  "name": "...",
  "intent": "...",
  "start_pose": {"base_yaw": ..., ...},     # 录制起点（调试用，播放不读）
  "segments": [
    {"joints": {...完整 5 关节...}, "duration": 1.5},
    ...
  ]
}

用法: uv run python -m lelamp.record_keyframe --id lelamp --port /dev/cu.usbmodem5B141150361 --name wake_me_up
"""
import argparse
import json
import os

from .follower import LeLampFollower, LeLampFollowerConfig
from .utils import find_serial_port
from lerobot.motors.feetech import OperatingMode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--id",     default="lelamp")
    parser.add_argument("--port",   default=None, help="不指定则自动查找")
    parser.add_argument("--name",   required=True, help="录制名称")
    parser.add_argument("--intent", default="", help="动作意图描述（可选，写入 JSON）")
    args = parser.parse_args()

    port = args.port or find_serial_port()
    print(f"串口: {port}")
    config = LeLampFollowerConfig(port=port, id=args.id)
    robot = LeLampFollower(config)
    robot.connect(calibrate=False)

    robot.bus.disable_torque()
    for motor in robot.bus.motors:
        robot.bus.write("Operating_Mode", motor, OperatingMode.POSITION.value)

    print("=" * 50)
    print("关键帧录制模式（输出 JSON，播放时插值）")
    print("  拖动到目标姿势后按回车保存关键帧")
    print("  输入 q 完成录制并生成 JSON")
    print("=" * 50)

    keyframes: list[dict] = []
    durations: list[float] = []

    while True:
        user_input = input(f"\n[帧{len(keyframes)}] 拖到目标姿势后按回车，或输入 q 完成: ").strip().lower()
        if user_input == "q":
            if len(keyframes) < 2:
                print("至少需要 2 个关键帧才能生成动作，继续添加。")
                continue
            break

        pos = robot.bus.sync_read("Present_Position")
        keyframes.append({k: float(v) for k, v in pos.items()})
        print(f"  已保存: { {k: round(v, 1) for k, v in pos.items()} }")

        if len(keyframes) >= 2:
            while True:
                try:
                    dur = float(input(f"  这段过渡时长（秒，默认1.0）: ").strip() or "1.0")
                    if dur <= 0:
                        raise ValueError
                    durations.append(dur)
                    break
                except ValueError:
                    print("  请输入正数")

    robot.disconnect()

    # keyframes[0] 是录制起点，play 时会接当前真实位姿；segments 从 keyframes[1] 起
    start_pose = {k: round(v, 2) for k, v in keyframes[0].items()}
    segments = [
        {
            "joints":   {k: round(v, 2) for k, v in keyframes[i].items()},
            "duration": durations[i - 1],
        }
        for i in range(1, len(keyframes))
    ]

    recordings_dir = os.path.join(os.path.dirname(__file__), "recordings")
    os.makedirs(recordings_dir, exist_ok=True)
    json_path = os.path.join(recordings_dir, f"{args.name}.json")

    total = sum(durations)
    data = {
        "name":       args.name,
        "intent":     args.intent,
        "start_pose": start_pose,
        "segments":   segments,
    }
    with open(json_path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"\n已保存至 {json_path}")
    print(f"  关键帧: {len(keyframes)}  段: {len(segments)}  总时长: {total:.2f}s")


if __name__ == "__main__":
    main()
