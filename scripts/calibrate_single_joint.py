"""单关节重新标定 — 只重设指定关节的 zero offset + range,其他关节 calibration 保留。

适用于:更换/重装单个舵机,或某关节零位漂了但其他关节没动。
不重做整套 calibrate,避免影响其他正常关节。

用法:
    uv run python scripts/calibrate_single_joint.py \\
        --port /dev/cu.usbmodem5B141150361 \\
        --motor wrist_pitch

流程(按提示按 Enter 推进):
  1. 把目标关节**手动**拨到机械中位 → Enter 设零点
  2. 把该关节拨到正负极限往复几次 → Enter 记录 range
  3. 脚本自动 merge 进 calibration file 并 enable torque

可选 motor 名(对应 lelamp follower):
    base_yaw / base_pitch / elbow_pitch / wrist_roll / wrist_pitch
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lelamp.follower import LeLampFollowerConfig, LeLampFollower  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True, help="舵机串口")
    parser.add_argument("--id", default="lelamp", help="机器人 id")
    parser.add_argument("--motor", required=True,
                        help="要标定的关节名(如 wrist_pitch)")
    args = parser.parse_args()

    robot = LeLampFollower(LeLampFollowerConfig(port=args.port, id=args.id))
    # 用 calibrate=False 进入,保留现有 calibration file 加载
    robot.connect(calibrate=False)
    bus = robot.bus

    if args.motor not in bus.motors:
        print(f"✗ 未知 motor {args.motor!r}")
        print(f"  可选: {list(bus.motors)}")
        robot.disconnect()
        return 1

    # 检查是否有 calibration file 加载(connect 时 robot 基类自动 load file 到 self.calibration)
    if not bus.calibration:
        print("✗ 没有 calibration file,请先跑全套 calibrate(走 robot.calibrate())")
        print(f"  期待路径: ~/.cache/huggingface/lerobot/calibration/robots/lelamp_follower/{args.id}.json")
        robot.disconnect()
        return 1

    # 把 file 里的 calibration 写回电机寄存器(lelamp_follower.calibrate 标准做法)
    # 这样 is_calibrated 后续比较一致,read_calibration 拿到正确 baseline
    bus.write_calibration(bus.calibration)
    print(f"已把 calibration file 写回电机寄存器")
    print(f"现有 calibration 关节: {list(bus.calibration.keys())}")
    print(f"本次只重标定: {args.motor!r}\n")

    # ⚠️ 注意:set_half_turn_homings 内部会调 reset_calibration,**清空整个**
    # bus.calibration dict(不止单关节项)。所以这里先备份,后面用备份做 merge
    # 而非读 bus.calibration(那时已空)。
    backup_cal = dict(bus.calibration)

    # ── 1. 设零点 ────────────────────────────────────────────────────────
    bus.disable_torque(args.motor)
    print(f"[1/2] 把 {args.motor!r} 手动拨到**机械中位**(物理上正中间)")
    input("    完成后按 Enter 设零点...")
    bus.set_half_turn_homings(args.motor)
    print(f"    ✓ 已设 {args.motor!r} 的 Homing_Offset\n")

    # ── 2. 记 range ───────────────────────────────────────────────────────
    print(f"[2/2] 把 {args.motor!r} 拨到正反两个极限往复几次,实时位置会显示")
    print("    完成后按 Enter 结束记录")
    range_mins, range_maxes = bus.record_ranges_of_motion(args.motor)
    print()
    print(f"    range_min={range_mins[args.motor]} / range_max={range_maxes[args.motor]}\n")

    # ── 3. merge 进 calibration + 持久化到 file ──────────────────────────
    # 用 backup_cal(set_half_turn_homings 把 bus.calibration 清空了),只 update 这一项
    new_cal = dict(backup_cal)
    old = new_cal[args.motor]
    from lerobot.motors.motors_bus import MotorCalibration
    new_cal[args.motor] = MotorCalibration(
        id=old.id,
        drive_mode=old.drive_mode,
        homing_offset=bus.read("Homing_Offset", args.motor, normalize=False),
        range_min=range_mins[args.motor],
        range_max=range_maxes[args.motor],
    )
    # 写回电机寄存器(同步内存里 bus.calibration)
    bus.write_calibration(new_cal)
    # 同步给 robot 实例并持久化到 file(其他 4 个关节原值保留)
    robot.calibration = new_cal
    robot._save_calibration()
    print(f"    ✓ calibration 已写入电机 + 文件: {robot.calibration_fpath}")

    bus.enable_torque(args.motor)
    robot.disconnect()
    print("\n完成。重启 main_persona 验证 wake_up,或先 record_home_pos 调新站姿。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
