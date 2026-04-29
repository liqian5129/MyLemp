"""验证 busy→celebrate 过渡的视觉问题。

模拟真实 Claude Code flow:
  1. set_state busy + set_text subtitle "Working on task..."
  2. 停 3 秒
  3. set_state celebrate(不清 subtitle)—— 这就是 daemon 真实下发顺序
  4. 停 5 秒(让 celebrate 充分展示,排除时长问题)
  5. set_state idle

如果 step 3-4 期间看不到 celebrate 视觉,验证:**busy 状态的 subtitle 残留遮住了 celebrate**,
是 Agent B 渲染层问题(set_state 应清旧 state 的 UI 元素)。

如果看得到,说明 daemon 真实 flow 时长(1.5s)太短,Mac 侧得调整或加 padding。

用法:
  uv run python scripts/probe_celebrate.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lelamp.transport import DisplayController, find_display_port  # noqa: E402


def main() -> int:
    port = find_display_port()
    print(f"显示设备: {port}")
    print("准备开始 — 现在屏应该是任意之前状态,不重要")

    disp = DisplayController(port=port)
    with disp:
        time.sleep(0.5)

        print("\n[1] set_state busy + subtitle 'Working on task...'")
        disp.set_state("busy")
        disp.set_text("subtitle", "Working on task...")
        time.sleep(3)

        print("\n[2] set_state celebrate(不清 subtitle,模拟真实 daemon 下发顺序)")
        disp.set_state("celebrate")
        print("   → 接下来 5 秒看屏:看到 celebrate 视觉了吗?")
        time.sleep(5)

        print("\n[3] set_state idle 收尾")
        disp.set_state("idle")
        time.sleep(1)

    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
