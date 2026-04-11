"""长期记忆 — 分类持久化知识库（按需检索，不常驻 prompt）。

存储：
  ~/.lelamp/longterm_memory.json   记忆条目
  ~/.lelamp/longterm_embeddings.npy  向量索引（id → embedding）

写入路径：
  1. LLM 主动调用 save_memory 工具
  2. episodic 压缩时自动提取

检索：DashScope text-embedding-v4 向量 + cosine similarity
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

import numpy as np

logger = logging.getLogger(__name__)

# ── 路径 ──────────────────────────────────────────────────────────────────────
_MEMORY_DIR = Path.home() / ".lelamp"
_LTM_FILE = _MEMORY_DIR / "longterm_memory.json"
_EMB_FILE = _MEMORY_DIR / "longterm_embeddings.npy"

# ── 分类 ──────────────────────────────────────────────────────────────────────
CATEGORIES = {
    "hobby":      "爱好",
    "dislike":    "不喜欢",
    "experience": "经历",
    "person":     "人物",
    "knowledge":  "知识",
    "habit":      "习惯",
    "wish":       "愿望",
    "other":      "其他",
}

# ── Embedding ─────────────────────────────────────────────────────────────────
_EMBEDDING_MODEL = "text-embedding-v4"
_EMBEDDING_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
_EMBEDDING_DIM = 1024


@dataclass
class LongTermEntry:
    id: str                     # "ltm_" + 8-char hex
    category: str               # one of CATEGORIES
    title: str                  # 简短标题，<30 字
    content: str                # 详细描述，<500 字
    tags: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    source: str = "user_tool"   # "user_tool" | "compaction"


class LongTermMemory:
    """分类长期记忆，embedding 向量检索。"""

    def __init__(
        self,
        path: Path = _LTM_FILE,
        emb_path: Path = _EMB_FILE,
        api_key: str = "",
    ):
        self._path = path
        self._emb_path = emb_path
        self._api_key = api_key
        self._entries: list[LongTermEntry] = []
        self._embeddings: dict[str, np.ndarray] = {}  # id → vector
        self._client = None  # lazy init

        _MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        self._load()

    # ── 写入 ──────────────────────────────────────────────────────────────────

    async def save(
        self,
        category: str,
        title: str,
        content: str,
        tags: Optional[list[str]] = None,
        source: str = "user_tool",
    ) -> LongTermEntry:
        """保存或更新一条长期记忆。同 (category, title) 存在则更新。"""
        if category not in CATEGORIES:
            category = "other"

        now = time.time()
        existing = self._find(category, title)
        if existing:
            existing.content = content[:500]
            existing.tags = (tags or [])[:5]
            existing.updated_at = now
            existing.source = source
            entry = existing
        else:
            entry = LongTermEntry(
                id=f"ltm_{uuid.uuid4().hex[:8]}",
                category=category,
                title=title[:30],
                content=content[:500],
                tags=(tags or [])[:5],
                created_at=now,
                updated_at=now,
                source=source,
            )
            self._entries.append(entry)

        # 生成 embedding（HTTP 调用，offload 到线程池避免阻塞事件循环）
        emb = await asyncio.to_thread(
            self._get_embedding, f"{entry.title} {entry.content}"
        )
        if emb is not None:
            self._embeddings[entry.id] = emb

        self._flush()
        logger.info(
            "📝 长期记忆 %s: [%s] %s",
            "更新" if existing else "新增",
            category,
            title,
        )
        return entry

    def delete(self, memory_id: str) -> bool:
        """按 id 删除一条记忆。"""
        before = len(self._entries)
        self._entries = [e for e in self._entries if e.id != memory_id]
        self._embeddings.pop(memory_id, None)
        if len(self._entries) < before:
            self._flush()
            return True
        return False

    # ── 检索 ──────────────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        category: Optional[str] = None,
        limit: int = 5,
    ) -> list[LongTermEntry]:
        """向量语义搜索。可选按分类过滤。"""
        candidates = self._entries
        if category and category in CATEGORIES:
            candidates = [e for e in candidates if e.category == category]
        if not candidates:
            return []

        query_emb = await asyncio.to_thread(self._get_embedding, query)
        if query_emb is None:
            # embedding 不可用时回退到关键词匹配
            return self._keyword_search(query, candidates, limit)

        # cosine similarity
        scored: list[tuple[float, LongTermEntry]] = []
        for entry in candidates:
            emb = self._embeddings.get(entry.id)
            if emb is None:
                continue
            sim = float(np.dot(query_emb, emb) / (
                np.linalg.norm(query_emb) * np.linalg.norm(emb) + 1e-9
            ))
            scored.append((sim, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in scored[:limit]]

    def list_by_category(self, category: str) -> list[LongTermEntry]:
        return [e for e in self._entries if e.category == category]

    def categories_summary(self) -> str:
        """返回 "爱好(3) 经历(5)" 格式的摘要，用于 prompt hint。"""
        if not self._entries:
            return ""
        counts: dict[str, int] = {}
        for e in self._entries:
            label = CATEGORIES.get(e.category, e.category)
            counts[label] = counts.get(label, 0) + 1
        parts = [f"{label}({n})" for label, n in counts.items()]
        return " ".join(parts)

    # ── Embedding API ─────────────────────────────────────────────────────────

    def _ensure_client(self):
        if self._client is None:
            import openai
            self._client = openai.OpenAI(
                api_key=self._api_key,
                base_url=_EMBEDDING_BASE_URL,
            )

    def _get_embedding(self, text: str) -> Optional[np.ndarray]:
        """同步调用 DashScope embedding API。"""
        if not self._api_key:
            return None
        try:
            self._ensure_client()
            resp = self._client.embeddings.create(
                model=_EMBEDDING_MODEL,
                input=text[:512],
                dimensions=_EMBEDDING_DIM,
            )
            return np.array(resp.data[0].embedding, dtype=np.float32)
        except Exception as exc:
            logger.warning("embedding 生成失败: %s", exc)
            return None

    # ── 关键词回退 ────────────────────────────────────────────────────────────

    @staticmethod
    def _keyword_search(
        query: str,
        candidates: list[LongTermEntry],
        limit: int,
    ) -> list[LongTermEntry]:
        """embedding 不可用时的回退搜索。"""
        keywords = query.lower().split()
        if not keywords:
            return candidates[:limit]

        scored: list[tuple[int, LongTermEntry]] = []
        for entry in candidates:
            haystack = f"{entry.title} {entry.content} {' '.join(entry.tags)}".lower()
            hits = sum(1 for kw in keywords if kw in haystack)
            if hits > 0:
                scored.append((hits, entry))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [entry for _, entry in scored[:limit]]

    # ── 持久化 ────────────────────────────────────────────────────────────────

    def _find(self, category: str, title: str) -> Optional[LongTermEntry]:
        for e in self._entries:
            if e.category == category and e.title == title:
                return e
        return None

    def _load(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                memories = data.get("memories", [])
                self._entries = [LongTermEntry(**m) for m in memories]
                logger.info("📂 长期记忆已加载: %d 条", len(self._entries))
            except Exception as exc:
                logger.warning("长期记忆加载失败: %s", exc)
                self._entries = []

        if self._emb_path.exists():
            try:
                saved = np.load(self._emb_path, allow_pickle=True).item()
                self._embeddings = {
                    k: np.array(v, dtype=np.float32) for k, v in saved.items()
                }
            except Exception as exc:
                logger.warning("embedding 加载失败: %s", exc)
                self._embeddings = {}

    def _flush(self):
        """同步写盘（atomic write）。"""
        try:
            data = {
                "version": 1,
                "memories": [asdict(e) for e in self._entries],
            }
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.rename(self._path)
        except OSError as exc:
            logger.error("长期记忆写盘失败: %s", exc)

        if self._embeddings:
            try:
                np.save(self._emb_path, self._embeddings)
            except Exception as exc:
                logger.warning("embedding 写盘失败: %s", exc)
