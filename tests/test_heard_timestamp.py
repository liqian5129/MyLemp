"""验证 _pending_heard 时间戳修复：heard 条目应在 said 条目之前。

模拟场景：
  1. 用户说话 (ts=T0)
  2. _think 耗时 5 秒，LLM 调用 speak → mem.add("said") (ts=T0+5)
  3. _think 结束后延迟写入 mem.add("heard", timestamp=T0)
  4. format_for_prompt() 应输出 HEA 在 SAI 前面

用法：
    python tests/test_heard_timestamp.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lelamp.soul.memory.episodic import MemoryStream


def test_heard_before_said():
    """heard 传入原始时间戳后，应排在 said 之前。"""
    import tempfile
    tmp = tempfile.mkdtemp()
    mem = MemoryStream(
        active_file=Path(tmp) / "mem.json",
        archive_file=Path(tmp) / "archive.json",
    )

    T0 = time.time()

    # 模拟 _think 过程：先 speak (ts=T0+5)，再延迟写 heard (ts=T0)
    mem.add("said", "我只会发光助威呀！")  # ts = now ~ T0
    time.sleep(0.01)  # 确保 said 的 time.time() 比 T0 大一点

    # 关键：heard 用原始时间戳 T0，早于 said
    mem.add("heard", "小Q小Q，你会打篮球吗？", timestamp=T0 - 1.0)

    # 验证时间戳
    entries = mem.retrieve()
    heard = [e for e in entries if e.type == "heard"][0]
    said = [e for e in entries if e.type == "said"][0]

    assert heard.timestamp < said.timestamp, (
        f"heard.ts={heard.timestamp:.3f} should < said.ts={said.timestamp:.3f}"
    )
    print(f"  ✅ 时间戳正确: heard({heard.timestamp:.3f}) < said({said.timestamp:.3f})")

    # 验证 format_for_prompt 输出顺序
    prompt = mem.format_for_prompt()
    hea_pos = prompt.find("HEA")
    sai_pos = prompt.find("SAI")
    assert hea_pos < sai_pos, (
        f"HEA(pos={hea_pos}) should appear before SAI(pos={sai_pos})\n{prompt}"
    )
    print(f"  ✅ [RECENT] 顺序正确: HEA(pos={hea_pos}) 在 SAI(pos={sai_pos}) 前面")
    print(f"\n  [RECENT] 输出:\n{prompt}")


def test_default_timestamp():
    """不传 timestamp 时仍使用 time.time()。"""
    import tempfile
    tmp = tempfile.mkdtemp()
    mem = MemoryStream(
        active_file=Path(tmp) / "mem.json",
        archive_file=Path(tmp) / "archive.json",
    )

    before = time.time()
    entry = mem.add("heard", "测试")
    after = time.time()

    assert before <= entry.timestamp <= after, (
        f"默认时间戳应在 [{before}, {after}] 范围内，实际={entry.timestamp}"
    )
    print("  ✅ 默认时间戳正确（使用 time.time()）")


if __name__ == "__main__":
    print("=" * 50)
    print("heard 时间戳修复验证")
    print("=" * 50)

    print("\n--- 默认时间戳 ---")
    test_default_timestamp()

    print("\n--- heard 在 said 之前 ---")
    test_heard_before_said()

    print("\n" + "=" * 50)
    print("全部通过 ✅")
    print("=" * 50)
