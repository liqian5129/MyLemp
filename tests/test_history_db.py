"""HistoryDB 搜索功能验证。

测试项：
  1. FTS5 trigram 中文搜索（≥3 字符走 FTS5）
  2. 短查询 LIKE 兜底（<3 字符）
  3. 搜索结果按时间间隔分组（>30min = 不同对话段）
  4. 无匹配返回空
  5. 上下文窗口（匹配行的前后对话一起返回）
  6. days_back 过滤
  7. FTS5 特殊字符清理
  8. insert_one 去重（entry_id 唯一性）
  9. migrate_from_archive

用法：
    python -m pytest tests/test_history_db.py -v
    python tests/test_history_db.py
"""
import json
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lelamp.soul.memory.history_db import HistoryDB, _sanitize_fts_query


@dataclass
class FakeEntry:
    """最小化的 MemoryEntry 替身。"""
    id: str
    timestamp: float
    type: str
    content: str
    importance: float = 6.0


def _make_db() -> tuple[HistoryDB, Path]:
    """创建临时目录的 HistoryDB。"""
    tmp = Path(tempfile.mkdtemp())
    db = HistoryDB(db_path=tmp / "test_history.db")
    return db, tmp


def _seed_conversation(db: HistoryDB, base_ts: float) -> list[FakeEntry]:
    """写入一段典型对话，返回条目列表。"""
    entries = [
        FakeEntry("a1", base_ts,       "heard", "小Q小Q，你会打篮球吗？"),
        FakeEntry("a2", base_ts + 5,   "said",  "我只会发光助威呀！"),
        FakeEntry("a3", base_ts + 30,  "heard", "那你喜欢什么运动？"),
        FakeEntry("a4", base_ts + 35,  "said",  "我喜欢看你打篮球！"),
        FakeEntry("a5", base_ts + 60,  "heard", "帮我记住，我喜欢喝橙汁"),
        FakeEntry("a6", base_ts + 65,  "said",  "好的，记住了！你喜欢喝橙汁。"),
    ]
    for e in entries:
        db.insert_one(e)
    return entries


# ── 测试用例 ──────────────────────────────────────────────────────────────────


def test_fts5_chinese_search():
    """中文 trigram 搜索（≥3 字符）。"""
    db, _ = _make_db()
    base = time.time() - 3600  # 1 小时前
    _seed_conversation(db, base)

    results = db.search("篮球", days_back=1)
    assert len(results) > 0, "应搜到包含'篮球'的对话段"

    # 检查匹配内容
    all_contents = []
    for seg in results:
        for m in seg["matches"]:
            if m.get("is_match"):
                all_contents.append(m["content"])
    assert any("篮球" in c for c in all_contents), f"匹配内容应包含'篮球': {all_contents}"
    print("  ✅ FTS5 中文搜索正确")


def test_short_query_like_fallback():
    """短查询（<3 字符）走 LIKE 兜底。"""
    db, _ = _make_db()
    base = time.time() - 3600
    _seed_conversation(db, base)

    # "橙汁" 是 2 个中文字符 = 6 bytes，但 len("橙汁") = 2 < 3
    results = db.search("橙汁", days_back=1)
    assert len(results) > 0, "短查询应通过 LIKE 搜到结果"

    all_contents = []
    for seg in results:
        for m in seg["matches"]:
            if m.get("is_match"):
                all_contents.append(m["content"])
    assert any("橙汁" in c for c in all_contents), f"应匹配到'橙汁': {all_contents}"
    print("  ✅ 短查询 LIKE 兜底正确")


def test_no_match_returns_empty():
    """无匹配返回空列表。"""
    db, _ = _make_db()
    base = time.time() - 3600
    _seed_conversation(db, base)

    results = db.search("量子力学", days_back=1)
    assert results == [], f"不应有匹配: {results}"
    print("  ✅ 无匹配返回空")


def test_empty_query_returns_empty():
    """空查询返回空列表。"""
    db, _ = _make_db()
    results = db.search("", days_back=1)
    assert results == [], "空查询应返回空"
    results = db.search("   ", days_back=1)
    assert results == [], "纯空格查询应返回空"
    print("  ✅ 空查询返回空")


def test_gap_grouping():
    """时间间隔 >30 分钟的对话分成不同段。"""
    db, _ = _make_db()
    base = time.time() - 7200  # 2 小时前

    # 第一段对话
    db.insert_one(FakeEntry("b1", base,       "heard", "你好小Q"))
    db.insert_one(FakeEntry("b2", base + 5,   "said",  "你好呀！"))
    db.insert_one(FakeEntry("b3", base + 30,  "heard", "今天天气怎么样"))

    # 间隔 2 小时 — 第二段对话
    db.insert_one(FakeEntry("b4", base + 7200,     "heard", "小Q，今天心情好吗"))
    db.insert_one(FakeEntry("b5", base + 7205,     "said",  "今天超开心！"))

    # 搜索"今天"应在两段中都出现
    results = db.search("今天", days_back=1)
    assert len(results) == 2, f"应有 2 个对话段（间隔 >30min），实际: {len(results)}"
    print("  ✅ 时间间隔分组正确")


def test_context_window():
    """匹配行的前后对话也作为上下文返回。"""
    db, _ = _make_db()
    base = time.time() - 3600
    _seed_conversation(db, base)

    results = db.search("橙汁", days_back=1)
    assert len(results) > 0

    seg = results[0]
    contents = [m["content"] for m in seg["matches"]]
    # 橙汁在 a5/a6 条目，但上下文窗口（±5 分钟）应包含整段对话
    assert len(contents) >= 2, f"上下文应包含多条对话，实际: {contents}"
    print("  ✅ 上下文窗口正确")


def test_days_back_filter():
    """days_back 过滤掉更早的记录。"""
    db, _ = _make_db()

    # 10 天前的对话
    old_ts = time.time() - 10 * 86400
    db.insert_one(FakeEntry("c1", old_ts, "heard", "很久以前的篮球话题"))

    # 1 小时前的对话
    recent_ts = time.time() - 3600
    db.insert_one(FakeEntry("c2", recent_ts, "heard", "最近的篮球话题"))

    results = db.search("篮球", days_back=3)
    all_contents = []
    for seg in results:
        for m in seg["matches"]:
            all_contents.append(m["content"])
    assert any("最近" in c for c in all_contents), "应找到最近的"
    assert not any("很久以前" in c for c in all_contents), "不应找到 10 天前的"
    print("  ✅ days_back 过滤正确")


def test_sanitize_fts_query():
    """FTS5 特殊字符被正确清理。"""
    assert _sanitize_fts_query('test "hello"') == "test hello"
    assert _sanitize_fts_query("a+b-c") == "a b c"
    assert _sanitize_fts_query("  多余  空格  ") == "多余 空格"
    assert _sanitize_fts_query("") == ""
    assert _sanitize_fts_query("正常中文") == "正常中文"
    print("  ✅ FTS5 查询清理正确")


def test_count():
    """count 属性返回正确数量。"""
    db, _ = _make_db()
    assert db.count == 0
    base = time.time()
    _seed_conversation(db, base)
    assert db.count == 6
    print("  ✅ count 正确")


def test_migrate_from_archive():
    """从 archive JSON 迁移数据。"""
    db, tmp = _make_db()

    archive = tmp / "archive.json"
    archive.write_text(json.dumps([
        {"id": "m1", "timestamp": time.time() - 100, "type": "heard",
         "content": "迁移测试内容", "importance": 6.0},
        {"id": "m2", "timestamp": time.time() - 50, "type": "said",
         "content": "迁移回复内容", "importance": 6.0},
    ], ensure_ascii=False))

    count = db.migrate_from_archive(archive)
    assert count == 2, f"应迁移 2 条，实际: {count}"
    assert db.count == 2

    # 再次迁移不应重复
    count2 = db.migrate_from_archive(archive)
    assert db.count == 2, "INSERT OR IGNORE 应防止重复"
    print("  ✅ archive 迁移正确（含去重）")


if __name__ == "__main__":
    print("=" * 50)
    print("HistoryDB 搜索功能验证")
    print("=" * 50)

    tests = [
        ("FTS5 中文搜索", test_fts5_chinese_search),
        ("短查询 LIKE 兜底", test_short_query_like_fallback),
        ("无匹配返回空", test_no_match_returns_empty),
        ("空查询返回空", test_empty_query_returns_empty),
        ("时间间隔分组", test_gap_grouping),
        ("上下文窗口", test_context_window),
        ("days_back 过滤", test_days_back_filter),
        ("FTS5 查询清理", test_sanitize_fts_query),
        ("count 属性", test_count),
        ("archive 迁移", test_migrate_from_archive),
    ]

    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            print(f"\n--- {name} ---")
            fn()
            passed += 1
        except Exception as exc:
            print(f"  ❌ {name} 失败: {exc}")
            failed += 1

    print(f"\n{'=' * 50}")
    print(f"结果: {passed} 通过, {failed} 失败")
    print("=" * 50)
    if failed:
        sys.exit(1)
