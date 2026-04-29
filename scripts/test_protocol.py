"""最小协议联调脚本(Step 1 验收用)。

用法:
  uv run python scripts/test_protocol.py --mock     # 不连真硬件,跑通发送+ack 路径
  uv run python scripts/test_protocol.py            # 自动找 ESP32-S3,跑真链路
  uv run python scripts/test_protocol.py --port /dev/cu.usbmodemXXXX

预期输出(mock 模式):
  - 看到 [mock send] {"cmd":"ping","seq":1}
  - 收到 ack {"ack":"ping","ok":true,"seq":1}
  - 测试通过

预期输出(真硬件,Agent B 烧好固件后):
  - 启动后短时间内收到 evt:ready
  - ping 后收到对应 ack
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

# 项目无 editable install,确保 lelamp 包在 import 路径上
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lelamp.transport import DisplayController, find_display_port  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default=None, help="USB CDC 设备路径,默认按 VID 自动查找")
    parser.add_argument("--mock", action="store_true", help="不连真硬件,走 mock 模式")
    parser.add_argument("--timeout", type=float, default=2.0, help="等 ack 的超时秒数")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    log = logging.getLogger("test_protocol")

    if args.mock:
        port = None
        log.info("mock 模式,不连真硬件")
    else:
        try:
            port = args.port or find_display_port()
            log.info("显示设备端口: %s", port)
        except RuntimeError as e:
            log.error("自动查找失败: %s", e)
            log.error("→ 用 --port 显式指定,或加 --mock 跑离线测试")
            return 1

    disp = DisplayController(port=port, mock=args.mock)

    # 注册回调,验证事件分发
    disp.on("ready", lambda m: log.info("[evt:ready] %s", m))
    disp.on("ping", lambda m: log.info("[ack:ping] %s", m))
    disp.on("*", lambda m: log.debug("[any] %s", m))

    with disp:
        # 真硬件:等设备的 ready 事件(开机自发)
        if not args.mock:
            log.info("等待设备 ready 事件(最多 1 秒)...")
            ready = disp.wait_for(lambda m: m.get("evt") == "ready", timeout=1.0)
            if ready is None:
                log.warning("未收到 ready 事件;设备可能已经启动过,继续 ping 测试")

        # 发 ping 并同步等 ack
        log.info("发送 ping(seq=1)")
        disp.ping(seq=1)
        ack = disp.wait_for(
            lambda m: m.get("ack") == "ping" and m.get("seq") == 1,
            timeout=args.timeout,
        )
        if ack is None:
            log.error("ping 超时(%.1fs 无 ack)", args.timeout)
            return 2
        if not ack.get("ok"):
            log.error("ping ack 返回失败: %s", ack)
            return 3
        log.info("ping ok,链路通畅 ✓")

        # 给回调一点时间打印(异步分发)
        time.sleep(0.05)

    log.info("测试通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
