"""验证 Mac 真的把 desc 字段发出去 + 设备到底渲不渲染。

直接用 DisplayController 发一组 set_state attention + show_prompt(带 desc),
绕过 daemon。屏现在应该显示 attention,我们看 NEEDS YOU 下方有没有 desc 文本。

用法:
  curl -X POST http://127.0.0.1:9000/shutdown -d '{}'  # 先关 daemon
  uv run python scripts/probe_show_prompt_desc.py
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

    disp = DisplayController(port=port)
    with disp:
        time.sleep(0.5)

        # 1) 切 attention
        print("\n[1] set_state attention")
        disp.set_state("attention")
        time.sleep(0.5)

        # 2) 发 show_prompt 带 desc(英文,排除 CJK 字体问题)
        print("[2] show_prompt with desc='HELLO_FROM_MAC_DESC_FIELD'")
        disp.show_prompt(
            id="probe_desc_1",
            tool="Bash",
            command="rm -rf /tmp/test",
            desc="HELLO_FROM_MAC_DESC_FIELD",
        )
        print("\n→ 看屏 10 秒:")
        print("   - NEEDS YOU 下应有 'rm -rf /tmp/test'(command 字段)")
        print("   - 还应有 'HELLO_FROM_MAC_DESC_FIELD'(desc 字段)")
        print("   - 后者没出现 → Agent B 没渲染 desc")
        time.sleep(10)

        # 3) 收尾回 idle
        print("\n[3] 收尾:set_state idle")
        disp.set_state("idle")
        time.sleep(0.5)

    print("完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
