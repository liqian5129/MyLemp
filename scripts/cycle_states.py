"""慢速 cycle 6 个 state,每个停 5 秒,验证设备各 state 视觉是否区分。

跟 test_display.py 区别:
  - test_display.py 验收 ack(快速 cycle,只看协议层)
  - cycle_states.py 验收**视觉**(慢速 cycle,你目视每个 state)

用法:
  uv run python scripts/cycle_states.py             # 默认每态 5 秒
  uv run python scripts/cycle_states.py --hold 10   # 每态 10 秒,慢慢看
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lelamp.transport import DisplayController, find_display_port  # noqa: E402


SEQUENCE = [
    ("idle",      "(基线)空闲,角色坐着"),
    ("busy",      "工作中,角色敲键盘 / 凑屏前倾"),
    ("attention", "等审批,Claude 抬头 + 警告橙边框慢呼吸"),
    ("failed",    "失败,沮丧低头 + 错误红边框 + 一次抖动"),
    ("celebrate", "庆祝,跳起 + 彩虹脉冲(1.5s 后设备会自动回 idle)"),
    ("sleep",     "睡眠,闭眼 + 屏暗"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", default=None)
    parser.add_argument("--hold", type=float, default=5.0, help="每个 state 停留秒数")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    log = logging.getLogger("cycle")

    try:
        port = args.port or find_display_port()
    except RuntimeError as e:
        log.error(str(e))
        return 1
    log.info("显示设备: %s", port)

    disp = DisplayController(port=port)
    with disp:
        time.sleep(0.5)
        log.info("准备开始,共 %d 个 state,每个停 %.1f 秒。看屏!", len(SEQUENCE), args.hold)
        time.sleep(2.0)

        for idx, (state, hint) in enumerate(SEQUENCE, 1):
            log.info("[%d/%d] set_state(%s) — %s", idx, len(SEQUENCE), state, hint)
            disp.set_state(state)
            # 给 attention 和 failed 推一行测试文字便于辨认
            if state == "busy":
                disp.set_text("subtitle", "Refactoring auth...")
            elif state == "attention":
                disp.show_prompt(id=f"cycle_{idx}", tool="Bash", command="rm -rf /tmp/x")
            elif state == "failed":
                disp.set_text("subtitle", "3 tests failed")
            time.sleep(args.hold)

        log.info("收尾:回 idle")
        disp.set_state("idle")
        time.sleep(0.5)

    log.info("完成。请记下:哪些 state 视觉跟 idle 明显不同?哪些跟 idle 看起来一样?")
    return 0


if __name__ == "__main__":
    sys.exit(main())
