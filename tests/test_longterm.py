"""长期记忆单元测试。

用法：
    EMBEDDING_API_KEY=sk-xxx uv run python tests/test_longterm.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

from lelamp.soul.memory.longterm import LongTermMemory


@pytest.mark.asyncio
async def test_basic():
    """基础 CRUD，不依赖 embedding API。"""
    with tempfile.TemporaryDirectory() as tmp:
        ltm_path = Path(tmp) / "ltm.json"
        emb_path = Path(tmp) / "ltm_emb.npy"
        ltm = LongTermMemory(path=ltm_path, emb_path=emb_path, api_key="")

        # save
        e1 = await ltm.save("hobby", "弹吉他", "用户大学开始学吉他，喜欢弹民谣。", tags=["音乐", "吉他"])
        e2 = await ltm.save("person", "同事小王", "用户提到同事小王，一起做项目。", tags=["同事"])
        e3 = await ltm.save("dislike", "蜘蛛", "用户很怕蜘蛛。", tags=["恐惧"])
        assert len(ltm._entries) == 3

        # update (same category+title)
        await ltm.save("hobby", "弹吉他", "用户大学开始学吉他，最近在练指弹。", tags=["音乐", "吉他", "指弹"])
        assert len(ltm._entries) == 3  # 没有新增
        assert "指弹" in ltm._entries[0].content

        # keyword search (fallback, no embedding)
        results = await ltm.search("音乐")
        assert len(results) >= 1
        assert results[0].title == "弹吉他"

        # list_by_category
        hobbies = ltm.list_by_category("hobby")
        assert len(hobbies) == 1

        # categories_summary
        summary = ltm.categories_summary()
        assert "爱好" in summary

        # delete
        ok = ltm.delete(e3.id)
        assert ok
        assert len(ltm._entries) == 2

        # persistence: reload
        ltm2 = LongTermMemory(path=ltm_path, emb_path=emb_path, api_key="")
        assert len(ltm2._entries) == 2


@pytest.mark.asyncio
async def test_embedding_search():
    """向量语义搜索，需要 EMBEDDING_API_KEY。"""
    api_key = os.environ.get("EMBEDDING_API_KEY", "")
    if not api_key:
        pytest.skip("未设置 EMBEDDING_API_KEY")

    with tempfile.TemporaryDirectory() as tmp:
        ltm_path = Path(tmp) / "ltm.json"
        emb_path = Path(tmp) / "ltm_emb.npy"
        ltm = LongTermMemory(path=ltm_path, emb_path=emb_path, api_key=api_key)

        # 写入几条记忆
        await ltm.save("hobby", "弹吉他", "用户大学开始学吉他，现在偶尔在家弹民谣。", tags=["音乐"])
        await ltm.save("experience", "日本旅行", "用户去年夏天去了东京和京都旅行，很喜欢日本料理。", tags=["旅行", "日本"])
        await ltm.save("person", "女朋友小美", "用户的女朋友叫小美，在银行工作。", tags=["感情"])
        await ltm.save("habit", "跑步", "用户每周三和周六早上去公园跑步。", tags=["运动"])

        # 语义搜索测试
        test_cases = [
            ("音乐", "弹吉他"),       # 直接关键词
            ("乐器", "弹吉他"),       # 语义关联
            ("出国玩", "日本旅行"),    # 语义关联
            ("锻炼身体", "跑步"),     # 语义关联
            ("另一半", "女朋友小美"),  # 语义关联
        ]
        for query, expected_title in test_cases:
            results = await ltm.search(query, limit=3)
            assert results, f"'{query}' 应有结果"

        # 分类过滤
        results = await ltm.search("运动", category="habit")
        assert len(results) >= 1

        # persistence: reload with embeddings
        ltm2 = LongTermMemory(path=ltm_path, emb_path=emb_path, api_key=api_key)
        assert len(ltm2._entries) == 4
        assert len(ltm2._embeddings) == 4
        results = await ltm2.search("乐器", limit=1)
        assert results[0].title == "弹吉他"


if __name__ == "__main__":
    print("=" * 50)
    print("长期记忆测试")
    print("=" * 50)
    asyncio.run(test_basic())
    print("基础测试通过")
    asyncio.run(test_embedding_search())
    print("Embedding 测试通过")
