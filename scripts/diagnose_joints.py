"""
关节角度诊断脚本

模式 1（默认）：实时显示，用手移动关节观察读数
  uv run python diagnose_joints.py

模式 2：录制模式，采样并保存到 CSV，用于分析运动平滑度
  uv run python diagnose_joints.py record
  → 生成 joint_record.csv，同时在 main_soul.py 运行期间采样
  → 先启动本脚本，再另一个终端 uv run python main_soul.py
  → Ctrl-C 停止录制
"""
import csv
import sys
import time

from dotenv import load_dotenv
from lelamp.follower import LeLampFollowerConfig, LeLampFollower
from lelamp.service.motors.motion_scripts import HOME_POS
from lelamp.utils import find_serial_port

load_dotenv()

JOINTS = ["base_yaw", "base_pitch", "elbow_pitch", "wrist_roll", "wrist_pitch"]
LABELS = {
    "base_yaw":    "底座左右 (yaw)  ",
    "base_pitch":  "整体俯仰 (pitch)",
    "elbow_pitch": "臂弯曲  (elbow)",
    "wrist_roll":  "灯头歪斜 (roll) ",
    "wrist_pitch": "灯头俯仰 (wrist)",
}


def read_pos(robot) -> dict | None:
    try:
        obs = robot.get_observation()
        return {n: float(obs.get(f"{n}.pos", float("nan"))) for n in JOINTS}
    except Exception as e:
        print(f"\r读取失败: {e}", end="")
        return None


def mode_watch(robot):
    """实时显示模式"""
    print("✅ 已连接，实时显示（Ctrl-C 退出）\n")
    printed = False
    try:
        while True:
            pos = read_pos(robot)
            if pos is None:
                time.sleep(0.2)
                continue

            lines = []
            for n in JOINTS:
                v    = pos[n]
                home = HOME_POS[n]
                flag = " ⚠️  超限!" if abs(v) > 92.0 else ""
                lines.append(
                    f"  {LABELS[n]}  当前={v:+7.1f}  HOME={home:+7.1f}  偏差={v-home:+7.1f}{flag}"
                )
            out = "\n".join(lines)
            if printed:
                sys.stdout.write(f"\033[{len(lines)+1}A\r")
            sys.stdout.write(out + "\n\n")
            sys.stdout.flush()
            printed = True
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\n👋 退出")


def mode_record(robot, path="data/joint_record.csv", interval=0.05):
    """录制模式：以 ~20Hz 采样，写入 CSV"""
    print(f"✅ 录制中 → {path}  (采样率 ~{1/interval:.0f}Hz，Ctrl-C 停止)\n")
    t0 = time.perf_counter()
    rows = 0
    try:
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["t_ms"] + JOINTS)
            while True:
                t = (time.perf_counter() - t0) * 1000
                pos = read_pos(robot)
                if pos:
                    writer.writerow([f"{t:.1f}"] + [f"{pos[n]:.2f}" for n in JOINTS])
                    rows += 1
                    if rows % 100 == 0:
                        print(f"\r已录制 {rows} 行 ({t/1000:.1f}s)...", end="")
                time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\n✅ 录制完成：{rows} 行，保存至 {path}")
        print("\n前20行预览：")
        with open(path) as f:
            for i, line in enumerate(f):
                print(" ", line.rstrip())
                if i >= 20:
                    break


def main():
    port = find_serial_port()
    print(f"🔌 连接串口: {port}")
    config = LeLampFollowerConfig(port=port, id="lelamp")
    robot  = LeLampFollower(config)
    robot.connect(calibrate=False)

    mode = sys.argv[1] if len(sys.argv) > 1 else "watch"
    try:
        if mode == "record":
            mode_record(robot)
        else:
            mode_watch(robot)
    finally:
        robot.disconnect()
        print("🔌 已断开")


if __name__ == "__main__":
    main()
