"""
逐个测试程序化动作
用法: uv run python test_motions.py --port /dev/cu.usbmodem5B141150361
"""
import argparse
from lelamp.service.motors.motors_service import MotorsService
from lelamp.service.motors.motion_scripts import MOTION_REGISTRY

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    parser.add_argument("--id", default="lelamp")
    parser.add_argument("--only", help="只测试某个动作，如 --only nod")
    args = parser.parse_args()

    svc = MotorsService(port=args.port, lamp_id=args.id)
    svc.start()

    motions = [args.only] if args.only else list(MOTION_REGISTRY.keys())

    print(f"\n共 {len(motions)} 个动作待测试")
    try:
        for name in motions:
            user = input(f"\n[{name}] 按回车播放，输入 s 跳过，输入 q 退出: ").strip().lower()
            if user == "q":
                break
            if user == "s":
                print(f"  跳过 {name}")
                continue
            print(f"  播放 {name}...")
            svc.dispatch("play", name)
            # wait_until_idle 让动作播完再继续
            svc.wait_until_idle(timeout=15)
            print(f"  完成")
    except KeyboardInterrupt:
        pass

    print("\n所有动作测试完毕，保持当前位置（Ctrl+C 退出）...")
    try:
        while True:
            import time
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        svc.stop()
        print("已退出")

if __name__ == "__main__":
    main()
