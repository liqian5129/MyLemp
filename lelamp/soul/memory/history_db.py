"""SQLite + FTS5 会话历史存储。

替代 memories.archive.json（写了不读的死数据），让所有对话记录可搜索。

存储位置：~/.lelamp/history.db

用法::

    db = HistoryDB()
    db.insert_one(entry)                      # 实时写入
    db.insert(entries)                        # 批量写入（归档）
    results = db.search("篮球", days_back=7)  # FTS5 全文搜索
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .episodic import MemoryEntry

logger = logging.getLogger(__name__)

_MEMORY_DIR = Path.home() / ".lelamp"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS episodes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id  TEXT UNIQUE,
    timestamp REAL NOT NULL,
    type      TEXT NOT NULL,
    content   TEXT NOT NULL,
    importance REAL DEFAULT 5.0
);

CREATE INDEX IF NOT EXISTS idx_episodes_ts ON episodes(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_episodes_type ON episodes(type);
"""

_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
    content,
    content='episodes',
    content_rowid='id',
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS episodes_fts_insert AFTER INSERT ON episodes BEGIN
    INSERT INTO episodes_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS episodes_fts_delete AFTER DELETE ON episodes BEGIN
    INSERT INTO episodes_fts(episodes_fts, rowid, content)
        VALUES('delete', old.id, old.content);
END;
"""


def _sanitize_fts_query(raw: str) -> str:
    """清理用户输入，使其成为安全的 FTS5 查询。

    参考 Hermes hermes_state.py:1005-1055。
    """
    q = raw.strip()
    if not q:
        return ""
    # 移除 FTS5 特殊字符（保留中文和基本标点）
    for ch in '+-{}()"^*':
        q = q.replace(ch, " ")
    # 合并多余空格
    q = " ".join(q.split())
    return q


class HistoryDB:
    """SQLite + FTS5 会话历史。持久连接，主线程使用。"""

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = db_path or (_MEMORY_DIR / "history.db")
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = self._open()
        self._init_schema()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.executescript(_FTS_SQL)
        self._migrate_unique_entry_id(self._conn)
        self._conn.commit()
        logger.info("📦 HistoryDB 初始化: %s", self._db_path)

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _migrate_unique_entry_id(conn: sqlite3.Connection) -> None:
        """给 entry_id 补 UNIQUE 索引（兼容已有数据库）。"""
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='idx_episodes_entry_id'"
        ).fetchall()
        if rows:
            return  # 已有索引，跳过
        try:
            conn.execute(
                "CREATE UNIQUE INDEX idx_episodes_entry_id ON episodes(entry_id)"
            )
        except sqlite3.IntegrityError:
            # 已有重复 entry_id — 先去重再建索引
            logger.warning("发现重复 entry_id，去重后重建索引")
            conn.execute("""
                DELETE FROM episodes WHERE id NOT IN (
                    SELECT MIN(id) FROM episodes GROUP BY entry_id
                )
            """)
            conn.execute(
                "CREATE UNIQUE INDEX idx_episodes_entry_id ON episodes(entry_id)"
            )

    # ── 写入 ─────────────────────────────────────────────────────────────────

    def insert_one(self, entry: "MemoryEntry") -> None:
        """写入单条记录（实时写入，heard/said 事件到达时调用）。"""
        try:
            self._conn.execute(
                "INSERT INTO episodes (entry_id, timestamp, type, content, importance) "
                "VALUES (?, ?, ?, ?, ?)",
                (entry.id, entry.timestamp, entry.type, entry.content, entry.importance),
            )
            self._conn.commit()
        except Exception as exc:
            logger.warning("HistoryDB insert_one 失败: %s", exc)

    def insert(self, entries: list["MemoryEntry"]) -> None:
        """批量写入（归档时调用）。"""
        if not entries:
            return
        try:
            self._conn.executemany(
                "INSERT OR IGNORE INTO episodes "
                "(entry_id, timestamp, type, content, importance) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (e.id, e.timestamp, e.type, e.content, e.importance)
                    for e in entries
                ],
            )
            self._conn.commit()
            logger.info("📦 HistoryDB 批量写入 %d 条", len(entries))
        except Exception as exc:
            logger.warning("HistoryDB insert 失败: %s", exc)

    # ── 搜索 ─────────────────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        days_back: int = 7,
        limit: int = 10,
    ) -> list[dict]:
        """FTS5 全文搜索，按时间间隔分组返回对话片段。

        返回格式::
            [
                {
                    "time_range": "04-10 09:15 ~ 09:32",
                    "matches": [
                        {"time": "09:20", "type": "heard", "content": "..."},
                        ...
                    ]
                },
                ...
            ]
        """
        fts_query = _sanitize_fts_query(query)
        if not fts_query:
            return []

        since_ts = time.time() - days_back * 86400
        try:
            # trigram 分词器要求 query >= 3 字符；短 query 用 LIKE 兜底
            if len(fts_query) >= 3:
                rows = self._conn.execute(
                    """
                    SELECT e.id, e.timestamp, e.type, e.content, e.importance
                    FROM episodes e
                    JOIN episodes_fts f ON f.rowid = e.id
                    WHERE episodes_fts MATCH ?
                      AND e.timestamp > ?
                    ORDER BY e.timestamp DESC
                    LIMIT ?
                    """,
                    (fts_query, since_ts, limit * 3),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT id, timestamp, type, content, importance
                    FROM episodes
                    WHERE content LIKE ?
                      AND timestamp > ?
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (f"%{fts_query}%", since_ts, limit * 3),
                ).fetchall()

            if not rows:
                return []

            # 收集匹配行的 ID，查前后 2 条上下文
            match_ids = {r["id"] for r in rows}
            all_ts = [r["timestamp"] for r in rows]
            min_ts = min(all_ts) - 300   # 前 5 分钟
            max_ts = max(all_ts) + 300   # 后 5 分钟

            context_rows = self._conn.execute(
                """
                SELECT id, timestamp, type, content
                FROM episodes
                WHERE timestamp BETWEEN ? AND ?
                ORDER BY timestamp
                """,
                (min_ts, max_ts),
            ).fetchall()

            # 按时间间隔分组（>30 分钟 = 不同对话段）
            segments = self._group_by_gap(context_rows, match_ids)
            return segments[:limit]

        except Exception as exc:
            logger.warning("HistoryDB search 失败: %s", exc)
            return []

    @staticmethod
    def _group_by_gap(
        rows: list[sqlite3.Row],
        match_ids: set[int],
        gap_minutes: float = 30.0,
    ) -> list[dict]:
        """按时间间隔分组，只保留包含匹配行的段。"""
        from datetime import datetime

        if not rows:
            return []

        segments: list[dict] = []
        current_segment: list[dict] = []
        current_has_match = False
        prev_ts = None

        for r in rows:
            ts = r["timestamp"]
            # 新段判定
            if prev_ts is not None and (ts - prev_ts) > gap_minutes * 60:
                if current_has_match and current_segment:
                    segments.append(_format_segment(current_segment))
                current_segment = []
                current_has_match = False

            entry = {
                "time": datetime.fromtimestamp(ts).strftime("%H:%M"),
                "type": r["type"],
                "content": r["content"],
                "is_match": r["id"] in match_ids,
            }
            current_segment.append(entry)
            if r["id"] in match_ids:
                current_has_match = True
            prev_ts = ts

        # 最后一段
        if current_has_match and current_segment:
            segments.append(_format_segment(current_segment))

        return segments

    # ── 迁移 ─────────────────────────────────────────────────────────────────

    def migrate_from_archive(self, archive_path: Path) -> int:
        """从旧的 archive JSON 导入全部条目。返回导入数量。

        已有数据时跳过（避免每次启动都读 archive JSON）。
        """
        if not archive_path.exists():
            return 0
        if self.count > 0:
            return 0

        try:
            data = json.loads(archive_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("读取 archive 失败: %s", exc)
            return 0

        if not data:
            return 0

        count = 0
        for item in data:
            try:
                self._conn.execute(
                    "INSERT OR IGNORE INTO episodes "
                    "(entry_id, timestamp, type, content, importance) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        item.get("id", ""),
                        item["timestamp"],
                        item["type"],
                        item["content"],
                        item.get("importance", 5.0),
                    ),
                )
                count += 1
            except (KeyError, Exception) as exc:
                logger.debug("跳过无效 archive 条目: %s", exc)
        self._conn.commit()
        logger.info("📦 从 archive 迁移 %d 条到 HistoryDB", count)
        return count

    # ── 统计 ─────────────────────────────────────────────────────────────────

    @property
    def count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM episodes").fetchone()
        return row[0]


def _format_segment(entries: list[dict]) -> dict:
    """格式化一个对话段为输出格式。"""
    times = [e["time"] for e in entries]
    return {
        "time_range": f"{times[0]} ~ {times[-1]}" if len(times) > 1 else times[0],
        "matches": entries,
    }
