"""Background Review 单元测试。

mock LLM，验证：
  1. 触发条件（计数 + 节流）
  2. JSON 解析 + 记忆写入
  3. 边界情况处理

用法：
    python tests/test_background_review.py
"""
import asyncio
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@dataclass
class FakeLLMResponse:
    text: str
    tool_calls: list = None
    stop_reason: str = "stop"

    def __post_init__(self):
        if self.tool_calls is None:
            self.tool_calls = []


async def make_agent():
    """创建一个最小化的 SoulAgent，仅包含 review 所需的组件。"""
    from lelamp.soul.memory.episodic import MemoryStream, MemoryEntry
    from lelamp.soul.memory.longterm import LongTermMemory
    from lelamp.soul.memory.facts import FactStore
    from lelamp.soul.soul_agent import SoulAgent

    tmp = tempfile.mkdtemp()
    mem = MemoryStream(
        active_file=Path(tmp) / "mem.json",
        archive_file=Path(tmp) / "archive.json",
    )

    ltm = LongTermMemory(
        path=Path(tmp) / "ltm.json",
        emb_path=Path(tmp) / "ltm_emb.npy",
        api_key="",  # 无 embedding，用关键词 fallback
    )

    review_llm = AsyncMock()
    # mock SoulAgent with minimal deps (motion/rgb/tts not used in review)
    agent = SoulAgent.__new__(SoulAgent)
    agent._mem = mem
    agent._longterm = ltm
    agent._facts = FactStore()
    agent._review_llm = review_llm
    agent._review_engagement_count = 0
    agent._last_review_at = 0.0

    return agent, review_llm, mem, ltm, tmp


async def test_trigger_conditions():
    """测试触发条件：计数不够 / 节流期内 → 不触发。"""
    agent, mock_llm, mem, ltm, _ = await make_agent()

    # 计数不够 → 不触发
    agent._review_engagement_count = 2
    await agent._maybe_background_review()
    mock_llm.chat.assert_not_called()
    print("  ✅ 计数不够（2 < 3）→ 不触发")

    # 计数够但节流期内 → 不触发
    agent._review_engagement_count = 5
    agent._last_review_at = time.time() - 60  # 1 分钟前刚审查
    await agent._maybe_background_review()
    mock_llm.chat.assert_not_called()
    print("  ✅ 节流期内（< 5 分钟）→ 不触发")


async def test_no_dialogue():
    """测试没有对话事件时不触发。"""
    agent, mock_llm, mem, ltm, _ = await make_agent()
    agent._review_engagement_count = 5
    agent._last_review_at = 0.0  # 从未审查

    await agent._maybe_background_review()
    mock_llm.chat.assert_not_called()
    print("  ✅ 无对话事件 → 不触发")


async def test_successful_review():
    """测试成功的审查：LLM 返回记忆 → 写入 LongTermMemory + FactStore。"""
    agent, mock_llm, mem, ltm, _ = await make_agent()
    agent._review_engagement_count = 5
    agent._last_review_at = 0.0

    # 模拟对话
    mem.add("heard", "我很喜欢打篮球")
    mem.add("said", "哦你喜欢打篮球呀！")
    mem.add("heard", "对，我是打前锋的，喜欢投三分球")

    # mock LLM 返回
    mock_llm.chat = AsyncMock(return_value=FakeLLMResponse(
        text='[{"type": "ltm", "category": "hobby", "title": "打篮球", '
             '"content": "用户喜欢打篮球，打前锋，喜欢投三分球。", '
             '"tags": ["篮球", "运动", "三分球"]}]'
    ))

    await agent._maybe_background_review()

    # 验证 LLM 被调用
    mock_llm.chat.assert_called_once()
    call_kwargs = mock_llm.chat.call_args
    user_msg = call_kwargs.kwargs.get("user_message", "")
    assert "打篮球" in user_msg
    print("  ✅ LLM 被调用，对话内容传入")

    # 验证长期记忆写入
    results = ltm.search("篮球")
    assert len(results) >= 1
    assert results[0].title == "打篮球"
    assert "三分球" in results[0].content
    print(f"  ✅ 长期记忆写入: [{results[0].category}] {results[0].title}")

    # 验证计数器重置
    assert agent._review_engagement_count == 0
    assert agent._last_review_at > 0
    print("  ✅ 计数器已重置")


async def test_fact_extraction():
    """测试 fact 类型提取。"""
    agent, mock_llm, mem, ltm, _ = await make_agent()
    agent._review_engagement_count = 5
    agent._last_review_at = 0.0

    mem.add("heard", "你叫我阿千就好了")
    mem.add("said", "好的阿千！")

    mock_llm.chat = AsyncMock(return_value=FakeLLMResponse(
        text='[{"type": "fact", "kind": "calling", "key": "user_call_name", "value": "阿千"}]'
    ))

    await agent._maybe_background_review()

    facts = agent._facts.list_by_kind("calling")
    assert len(facts) >= 1
    assert facts[0].value == "阿千"
    print(f"  ✅ 事实写入: {facts[0].kind}/{facts[0].key}={facts[0].value}")


async def test_empty_result():
    """测试 LLM 返回空数组 → 不写入，计数器仍重置。"""
    agent, mock_llm, mem, ltm, _ = await make_agent()
    agent._review_engagement_count = 5
    agent._last_review_at = 0.0

    mem.add("heard", "今天天气不错")
    mem.add("said", "是呀，阳光明媚！")

    mock_llm.chat = AsyncMock(return_value=FakeLLMResponse(text="[]"))

    await agent._maybe_background_review()
    assert agent._review_engagement_count == 0
    assert len(ltm.search("天气")) == 0
    print("  ✅ 空结果 → 不写入，计数器重置")


async def test_malformed_json():
    """测试 LLM 返回非法 JSON → 不崩溃。"""
    agent, mock_llm, mem, ltm, _ = await make_agent()
    agent._review_engagement_count = 5
    agent._last_review_at = 0.0

    mem.add("heard", "你好")
    mem.add("said", "你好呀")

    mock_llm.chat = AsyncMock(return_value=FakeLLMResponse(text="这不是JSON"))

    await agent._maybe_background_review()
    print("  ✅ 非法 JSON → 不崩溃")


async def test_markdown_wrapped_json():
    """测试 LLM 返回 ```json``` 包裹的 JSON → 正常解析。"""
    agent, mock_llm, mem, ltm, _ = await make_agent()
    agent._review_engagement_count = 5
    agent._last_review_at = 0.0

    mem.add("heard", "我特别怕蜘蛛")
    mem.add("said", "我也怕！")

    mock_llm.chat = AsyncMock(return_value=FakeLLMResponse(
        text='```json\n[{"type": "ltm", "category": "dislike", "title": "怕蜘蛛", '
             '"content": "用户很怕蜘蛛。", "tags": ["恐惧"]}]\n```'
    ))

    await agent._maybe_background_review()
    results = ltm.search("蜘蛛")
    assert len(results) >= 1
    print(f"  ✅ Markdown 包裹 → 正常解析: {results[0].title}")


async def main():
    print("=" * 50)
    print("Background Review 单元测试")
    print("=" * 50)

    print("\n--- 触发条件 ---")
    await test_trigger_conditions()

    print("\n--- 无对话 ---")
    await test_no_dialogue()

    print("\n--- 成功审查 ---")
    await test_successful_review()

    print("\n--- Fact 提取 ---")
    await test_fact_extraction()

    print("\n--- 空结果 ---")
    await test_empty_result()

    print("\n--- 非法 JSON ---")
    await test_malformed_json()

    print("\n--- Markdown 包裹 ---")
    await test_markdown_wrapped_json()

    print("\n" + "=" * 50)
    print("全部通过 ✅")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(main())
