"""Step 3 验收脚本:依次切到 6 个 buddy 状态,目视检查机械臂动作。

用法:
  uv run python scripts/test_arm.py --mock           # 离线 dry-run,只走 API 路径
  uv run python scripts/test_arm.py                  # 真臂硬件,自动找端口
  uv run python scripts/test_arm.py --port /dev/cu.usbmodemXXXX --hold 3.0

序列:idle → busy → attention → failed → idle → celebrate → idle → sleep
末态停在 sleep(臂垂下),停止时舵机断电不会因重力坠落。

每个状态停留 --hold 秒(默认 2.5),给眼睛验收时间。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lelamp.transport import ArmController, find_arm_port  # noqa: E402


STATE_SEQUENCE = [
    "idle",
    "busy",
    "attention",
    "failed",
    "idle",
    "celebrate",
    "idle",
    "sleep",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default=None, help="舵机串口,不填则自动查找")
    parser.add_argument("--mock", action="store_true", help="不连舵机,只走 API 路径")
    parser.add_argument("--lamp-id", default="lelamp")
    parser.add_argument("--hold", type=float, default=2.5, help="每个状态停留秒数")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    log = logging.getLogger("test_arm")

    if args.mock:
        port = None
        log.info("mock 模式,不连真硬件")
    else:
        try:
            port = args.port or find_arm_port()
            log.info("舵机串口: %s", port)
        except Exception as e:
            log.error("找不到舵机串口: %s", e)
            return 1

    arm = ArmController(port=port, lamp_id=args.lamp_id, mock=args.mock)

    with arm:
        # 给 motion_agent 控制循环一点时间起来
        time.sleep(0.3 if not args.mock else 0)

        for state in STATE_SEQUENCE:
            log.info("→ set_state(%s)", state)
            arm.set_state(state)
            time.sleep(args.hold)

        log.info("序列发完,等末段动作收尾...")
        deadline = time.time() + 5.0
        while arm.is_busy() and time.time() < deadline:
            time.sleep(0.1)

    log.info("测试完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
