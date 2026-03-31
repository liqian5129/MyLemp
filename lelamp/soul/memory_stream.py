"""
持久化记忆流

小Q 的所有感知和行动记录，跨重启保留。

存储：
  ~/.lelamp/memories.json          活跃条目
  ~/.lelamp/memories.archive.json  已压缩旧条目

检索策略：importance × recency_decay（λ=0.05，半衰期≈14h）
压缩策略：活跃条目 > 40 时，压缩最老 25 条 → reflection 条目
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

# ── 路径 ──────────────────────────────────────────────────────────────────────
_MEMORY_DIR   = Path.home() / ".lelamp"
_ACTIVE_FILE  = _MEMORY_DIR / "memories.json"
_ARCHIVE_FILE = _MEMORY_DIR / "memories.archive.json"

# ── 参数 ──────────────────────────────────────────────────────────────────────
COMPACT_THRESHOLD = 40     # 活跃条目超过此数触发压缩
COMPACT_COUNT     = 25     # 每次压缩最老的 N 条
RECENCY_LAMBDA    = 0.05   # 时间衰减系数（半衰期≈14h）
RETRIEVE_TOP_N    = 15

_IMPORTANCE_KEYWORDS = [
    "记住", "再见", "明天", "不", "帮我", "喜欢", "谢谢",
    "名字", "出门", "回来", "一起", "好吗", "可以", "走了",
]


@dataclass
class MemoryEntry:
    id: str
    timestamp: float
    type: str           # heard | saw | said | did | felt | thought
    content: str
    importance: float   # 1-10
    archived: bool = False


def _score_importance(type_: str, content: str) -> float:
    """基于类型 + 简单启发式计算重要性（无需 LLM）"""
    base = {
        "heard": 6, "saw": 4, "said": 6,
        "did": 5, "felt": 5, "thought": 8,
    }.get(type_, 5)
    if type_ == "heard":
        if len(content) > 20:
            base += 1
        if any(k in content for k in _IMPORTANCE_KEYWORDS):
            base += 1
    return min(float(base), 10.0)


class MemoryStream:
    """
    小Q 的持久化记忆流。

    用法：
        mem = MemoryStream()
        mem.add("heard", "你好小Q")
        print(mem.format_for_prompt())
    """

    def __init__(
        self,
        active_file: Path = _ACTIVE_FILE,
        archive_file: Path = _ARCHIVE_FILE,
    ):
        self._active_file  = active_file
        self._archive_file = archive_file
        self._entries: List[MemoryEntry] = []
        self._compacting   = asyncio.Lock()
        self._dirty        = False

        _MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        self._load()

    # ── 公共接口 ──────────────────────────────────────────────────────────────

    def add(
        self,
        type_: str,
        content: str,
        importance: Optional[float] = None,
    ) -> MemoryEntry:
        """添加一条记忆，异步调度写盘（不阻塞调用方）。"""
        entry = MemoryEntry(
            id=str(uuid.uuid4())[:8],
            timestamp=time.time(),
            type=type_,
            content=content[:500],
            importance=(
                importance if importance is not None
                else _score_importance(type_, content)
            ),
        )
        self._entries.append(entry)
        self._dirty = True
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.call_soon(self._flush_sync)
        except RuntimeError:
            self._flush_sync()
        return entry

    def retrieve(self, n: int = RETRIEVE_TOP_N) -> List[MemoryEntry]:
        """按 importance × recency_decay 排序，返回 top-n 活跃条目。"""
        now    = time.time()
        active = [e for e in self._entries if not e.archived]

        def _score(e: MemoryEntry) -> float:
            hours_ago = (now - e.timestamp) / 3600
            return e.importance * math.exp(-RECENCY_LAMBDA * hours_ago)

        return sorted(active, key=_score, reverse=True)[:n]

    def format_for_prompt(self) -> str:
        """
        紧凑格式，每条约 30-50 字符。
        示例：
          [09:15 HEA] 有人说：小Q 你好
          [09:15 SAI] 哒！你好你好！
        """
        entries = self.retrieve()
        if not entries:
            return "（暂无记忆）"
        lines = []
        for e in sorted(entries, key=lambda x: x.timestamp):
            t   = datetime.fromtimestamp(e.timestamp).strftime("%H:%M")
            tag = e.type[:3].upper()
            lines.append(f"[{t} {tag}] {e.content}")
        return "\n".join(lines)

    def should_compact(self) -> bool:
        return sum(1 for e in self._entries if not e.archived) > COMPACT_THRESHOLD

    async def compact_if_needed(self, llm) -> None:
        """压缩旧记忆为 reflection 条目。并发安全，重复调用无害。"""
        if not self.should_compact():
            return
        async with self._compacting:
            # 双重检查（获得锁后再判断一次）
            active = [e for e in self._entries if not e.archived]
            if len(active) <= COMPACT_THRESHOLD:
                return

            to_compact = sorted(active, key=lambda e: e.timestamp)[:COMPACT_COUNT]
            ids_to_compact = {e.id for e in to_compact}

            # 先标记 archived，防止重复压缩
            for e in self._entries:
                if e.id in ids_to_compact:
                    e.archived = True
            self._flush_sync()

            # 异步 LLM 总结
            try:
                summary = await _summarize(llm, to_compact)
                self.add("thought", summary, importance=8)
            except Exception as exc:
                logger.warning("记忆压缩总结失败: %s", exc)

            self._append_to_archive(to_compact)
            logger.info("✅ 记忆压缩完成，归档 %d 条", len(to_compact))

    # ── 内部 ──────────────────────────────────────────────────────────────────

    def _load(self):
        if not self._active_file.exists():
            return
        try:
            data = json.loads(self._active_file.read_text(encoding="utf-8"))
            self._entries = [MemoryEntry(**d) for d in data]
            logger.info("📂 加载记忆 %d 条", len(self._entries))
        except Exception as exc:
            logger.warning("加载记忆失败，重置: %s", exc)
            self._entries = []

    def _flush_sync(self):
        """同步写盘（由 call_soon 调度，在事件循环线程中执行）"""
        if not self._dirty:
            return
        try:
            data = [asdict(e) for e in self._entries if not e.archived]
            self._active_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._dirty = False
        except OSError as exc:
            logger.error("写盘失败（磁盘可能已满）: %s", exc)

    def _append_to_archive(self, entries: List[MemoryEntry]):
        try:
            existing: list = []
            if self._archive_file.exists():
                existing = json.loads(
                    self._archive_file.read_text(encoding="utf-8")
                )
            existing.extend(asdict(e) for e in entries)
            self._archive_file.write_text(
                json.dumps(existing, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.warning("写 archive 失败: %s", exc)


async def _summarize(llm, entries: List[MemoryEntry]) -> str:
    """用 LLM 将一批记忆总结为一条 thought"""
    lines = []
    for e in sorted(entries, key=lambda x: x.timestamp):
        t = datetime.fromtimestamp(e.timestamp).strftime("%H:%M")
        lines.append(f"[{t}] {e.type}: {e.content}")
    bulk = "\n".join(lines)

    resp = await llm.chat(
        user_message=(
            f"以下是小Q 最近的部分记忆记录，请用 1-2 句话总结发生了什么：\n{bulk}"
        ),
        system_prompt="你是记忆整理助手，用简短中文总结。不超过 80 字，不加任何前缀。",
    )
    return (resp.text or "").strip() or "（一段时光悄悄过去了）"
