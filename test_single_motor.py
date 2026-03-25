"""
单舵机测试脚本
用法: uv run python test_single_motor.py --port /dev/cu.usbmodem5B141150361
"""
import argparse
import time
from lerobot.motors import Motor, MotorCalibration, MotorNormMode
from lerobot.motors.feetech import FeetechMotorsBus, OperatingMode

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    parser.add_argument("--id", type=int, default=1, help="舵机ID，默认1（出厂默认）")
    args = parser.parse_args()

    bus = FeetechMotorsBus(
        port=args.port,
        motors={"motor": Motor(args.id, "sts3215", MotorNormMode.DEGREES)},
    )

    print(f"连接串口 {args.port}，舵机 ID={args.id} ...")
    bus.connect()

    # STS3215 满量程 0~4095，中位 2048，手动注入校准让 sync_read/write 能用
    bus.calibration = {
        "motor": MotorCalibration(id=args.id, drive_mode=0, homing_offset=0, range_min=0, range_max=4095)
    }
    print("连接成功！")

    pos = bus.sync_read("Present_Position")
    print(f"当前位置: {pos['motor']:.1f}°")

    # 设为位置模式
    bus.write("Operating_Mode", "motor", OperatingMode.POSITION.value)
    bus.enable_torque()

    print("\n开始运动测试（小步渐进）...")
    waypoints = [0.0, 10.0, 20.0, 30.0, 45.0, 30.0, 15.0, 0.0, -15.0, -30.0, -45.0, -30.0, -15.0, 0.0]
    for target in waypoints:
        print(f"  → 目标角度: {target:+.0f}°")
        bus.sync_write("Goal_Position", {"motor": target})
        time.sleep(0.6)
        pos = bus.sync_read("Present_Position")
        print(f"    实际位置: {pos['motor']:+.1f}°")

    print("\n测试完成，断开连接")
    bus.disable_torque()
    bus.disconnect()

if __name__ == "__main__":
    main()
