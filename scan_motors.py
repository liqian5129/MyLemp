"""
扫描总线上所有舵机 ID
用法: uv run python scan_motors.py --port /dev/cu.usbmodem5B141150361
"""
import argparse
from lerobot.motors import Motor, MotorNormMode
from lerobot.motors.feetech import FeetechMotorsBus

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    args = parser.parse_args()

    # 把 ID 1~10 都注册进去扫描
    motors = {f"motor_{i}": Motor(i, "sts3215", MotorNormMode.RANGE_M100_100) for i in range(1, 11)}
    bus = FeetechMotorsBus(port=args.port, motors=motors)
    bus.connect(handshake=False)

    print("扫描总线上的舵机 ID...")
    found = []
    for i in range(1, 11):
        model, comm, _ = bus.packet_handler.ping(bus.port_handler, i)
        if comm == 0:  # COMM_SUCCESS = 0
            print(f"  ID {i}: 响应 ✓  model={model}")
            found.append(i)
        else:
            print(f"  ID {i}: 无响应")

    bus.port_handler.closePort()
    print(f"\n发现 {len(found)} 个舵机，ID: {found}")
    if len(found) != len(set(found)):
        print("⚠️  有重复 ID！")
    expected = {1, 2, 3, 4, 5}
    if set(found) != expected:
        missing = expected - set(found)
        extra = set(found) - expected
        if missing:
            print(f"⚠️  缺少 ID: {missing}")
        if extra:
            print(f"⚠️  多余 ID: {extra}")
    else:
        print("✓ ID 1~5 全部正常")

if __name__ == "__main__":
    main()
