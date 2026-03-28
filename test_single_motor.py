"""
单舵机逐个测试脚本（出厂 ID=1，一次只接一个）
用法: uv run python test_single_motor.py --port /dev/cu.usbmodem5B141150361
"""
import argparse
import time
from lerobot.motors import Motor, MotorCalibration, MotorNormMode
from lerobot.motors.feetech import FeetechMotorsBus, OperatingMode

MOTORS = [
    "base_yaw",
    "base_pitch",
    "elbow_pitch",
    "wrist_roll",
    "wrist_pitch",
]

WAYPOINTS = [0.0, 10.0, 20.0, 30.0, 45.0, 30.0, 15.0, 0.0, -15.0, -30.0, -45.0, -30.0, -15.0, 0.0]


def test_motor(port: str, name: str):
    bus = FeetechMotorsBus(
        port=port,
        motors={"motor": Motor(1, "sts3215", MotorNormMode.DEGREES)},
    )
    bus.connect()
    bus.calibration = {
        "motor": MotorCalibration(id=1, drive_mode=0, homing_offset=0, range_min=0, range_max=4095)
    }

    pos = bus.sync_read("Present_Position")
    print(f"  当前位置: {pos['motor']:.1f}°")

    bus.write("Operating_Mode", "motor", OperatingMode.POSITION.value)
    bus.enable_torque()

    for target in WAYPOINTS:
        print(f"  → {target:+.0f}°", end="  ", flush=True)
        bus.sync_write("Goal_Position", {"motor": target})
        time.sleep(0.6)
        pos = bus.sync_read("Present_Position")
        print(f"实际 {pos['motor']:+.1f}°")

    bus.disable_torque()
    bus.disconnect()
    print(f"  ✓ {name} 测试完成\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    args = parser.parse_args()

    print("=" * 50)
    print("舵机逐个测试（每次只接一个，出厂 ID=1）")
    print("=" * 50)

    for name in MOTORS:
        input(f"\n[{name}] 只接这一个舵机到驱动板，准备好后按回车...")
        print(f"  正在测试 {name} ...")
        try:
            test_motor(args.port, name)
        except Exception as e:
            print(f"  ✗ 错误: {e}")
            retry = input("  重试? (y/n): ")
            if retry.strip().lower() == "y":
                try:
                    test_motor(args.port, name)
                except Exception as e2:
                    print(f"  ✗ 再次失败: {e2}")

    print("=" * 50)
    print("所有舵机测试完成！")
    print("=" * 50)


if __name__ == "__main__":
    main()
