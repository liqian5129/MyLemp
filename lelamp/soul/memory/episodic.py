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

# Phase 3: episodic 收紧到 heard/said/thought/action。
# thought 是内部 compaction 写入的反思条目，不属于"事件"但留在流里作为叙事压缩产物。
# action 是工具执行记录（set_reminder/cancel_reminder 等），与 heard/said 同权进保底槽位。
# 其他类型（did/saw/felt）一律拒绝。
ALLOWED_TYPES = {"heard", "said", "thought", "action"}


@dataclass
class MemoryEntry:
    id: str
    timestamp: float
    type: str           # heard | said | thought | action
    content: str
    importance: float   # 1-10
    archived: bool = False


def _format_relative(seconds_ago: float) -> str:
    """把秒差转成"X 秒前 / X 分钟前 / X 小时前 / X 天前"。"""
    s = max(0.0, seconds_ago)
    if s < 60:
        return f"{int(s)} 秒前"
    if s < 3600:
        return f"{int(s / 60)} 分钟前"
    if s < 86400:
        return f"{int(s / 3600)} 小时前"
    return f"{int(s / 86400)} 天前"


def _score_importance(type_: str, content: str) -> float:
    """基于类型 + 简单启发式计算重要性（无需 LLM）。

    Phase 3 收紧后只对 heard/said 评分；thought 由 compaction 显式传入 importance。
    """
    base = {"heard": 6, "said": 6, "action": 7}.get(type_, 5)
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
        mem.start()            # 启动后台防抖写盘协程
        mem.add("heard", "你好小Q")
        print(mem.format_for_prompt())
    """

    def __init__(
        self,
        active_file: Path = _ACTIVE_FILE,
        archive_file: Path = _ARCHIVE_FILE,
        history_db=None,
    ):
        self._active_file  = active_file
        self._archive_file = archive_file
        self._history_db   = history_db   # Optional[HistoryDB]，双写 SQLite
        self._entries: List[MemoryEntry] = []
        self._compacting   = asyncio.Lock()
        self._dirty        = False
        self._flush_event  = asyncio.Event()
        self._flush_task   = None

        _MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        self._load()

    def start(self):
        """启动后台防抖写盘协程（在 asyncio 事件循环中调用）。"""
        if self._flush_task is None:
            self._flush_task = asyncio.create_task(
                self._flush_loop(), name="mem-flush"
            )

    # ── 公共接口 ──────────────────────────────────────────────────────────────

    def add(
        self,
        type_: str,
        content: str,
        importance: Optional[float] = None,
        timestamp: Optional[float] = None,
    ) -> MemoryEntry:
        """添加一条记忆，通知后台防抖写盘（不阻塞调用方）。

        Phase 3：只接受 heard/said/thought。其他类型（did/saw/felt）一律拒绝，
        从源头杜绝事件流被状态/控制信号污染。

        timestamp: 可选，指定条目的时间戳（用于延迟写入时保留原始事件时间）。
        """
        if type_ not in ALLOWED_TYPES:
            raise ValueError(
                f"episodic 不接受类型 {type_!r}（允许: {sorted(ALLOWED_TYPES)}）"
            )
        entry = MemoryEntry(
            id=str(uuid.uuid4())[:8],
            timestamp=timestamp or time.time(),
            type=type_,
            content=content[:500],
            importance=(
                importance if importance is not None
                else _score_importance(type_, content)
            ),
        )
        self._entries.append(entry)
        self._dirty = True
        self._flush_event.set()

        # 双写 SQLite（best-effort，失败不影响主流程）
        if self._history_db is not None:
            try:
                self._history_db.insert_one(entry)
            except Exception as exc:
                logger.debug("HistoryDB 实时写入失败: %s", exc)

        return entry

    def retrieve(self, n: int = RETRIEVE_TOP_N) -> List[MemoryEntry]:
        """
        检索 top-n 活跃条目。
        - thought 类型使用更快的衰减（λ=0.1，半衰期≈7h）
        - 保证最近 RECENT_SLOTS 条非 thought 条目必然入选
        """
        now    = time.time()
        active = [e for e in self._entries if not e.archived]

        def _score(e: MemoryEntry) -> float:
            hours_ago = (now - e.timestamp) / 3600
            lam = 0.1 if e.type == "thought" else RECENCY_LAMBDA
            return e.importance * math.exp(-lam * hours_ago)

        # 保留最近的非 thought 条目，确保当前对话不被摘要挤掉
        RECENT_SLOTS = 5
        non_thoughts = [e for e in active if e.type != "thought"]
        recent = sorted(non_thoughts, key=lambda e: e.timestamp, reverse=True)[:RECENT_SLOTS]
        recent_ids = {e.id for e in recent}

        # 剩余位按分数填充
        rest = [e for e in active if e.id not in recent_ids]
        rest_sorted = sorted(rest, key=_score, reverse=True)[:n - len(recent)]

        return recent + rest_sorted

    def format_for_prompt(self, sanitize_names: Optional[set[str]] = None) -> str:
        """
        紧凑格式，带日期、会话间隔分隔和"距今 X 分钟前"相对时间。

        sanitize_names: 当前无确认身份时，传入已知人名集合，
                        历史条目中的名字会被替换为中性称呼，
                        防止 LLM 在心跳时凭记忆猜测身份。
        """
        entries = self.retrieve()
        if not entries:
            return "（暂无记忆）"

        lines = []
        today = datetime.now().date()
        now = time.time()
        prev_ts = None

        for e in sorted(entries, key=lambda x: x.timestamp):
            dt = datetime.fromtimestamp(e.timestamp)

            # 会话间隔标记（>30 分钟视为不同会话）
            if prev_ts is not None:
                gap_h = (e.timestamp - prev_ts) / 3600
                if gap_h >= 1.0:
                    lines.append(f"--- (间隔 {gap_h:.0f} 小时) ---")
                elif gap_h > 0.5:
                    lines.append(f"--- (间隔 {gap_h * 60:.0f} 分钟) ---")

            # 非今天的条目显示日期
            if dt.date() == today:
                t = dt.strftime("%H:%M")
            else:
                t = dt.strftime("%m-%d %H:%M")

            tag = e.type[:3].upper()
            rel = _format_relative(now - e.timestamp)
            content = e.content
            if sanitize_names:
                from .render import sanitize_names_in_text
                content = sanitize_names_in_text(content, sanitize_names)
            lines.append(f"[{t} {tag} · {rel}] {content}")
            prev_ts = e.timestamp

        return "\n".join(lines)

    def should_compact(self) -> bool:
        return sum(1 for e in self._entries if not e.archived) > COMPACT_THRESHOLD

    def events_since(
        self,
        ts: float,
        types: Optional[set[str]] = None,
    ) -> List[MemoryEntry]:
        """返回 timestamp > ts 的活跃事件。types 默认 {heard, said}。"""
        types = types if types is not None else {"heard", "said"}
        return [
            e for e in self._entries
            if not e.archived
            and e.timestamp > ts
            and e.type in types
        ]

    async def compact_if_needed(self, llm, longterm=None) -> None:
        """压缩旧记忆为 reflection 条目。并发安全，重复调用无害。

        longterm: 可选的 LongTermMemory，压缩前提取长期记忆候选。
        """
        if not self.should_compact():
            return
        async with self._compacting:
            # 双重检查（获得锁后再判断一次）
            active = [e for e in self._entries if not e.archived]
            if len(active) <= COMPACT_THRESHOLD:
                return

            to_compact = sorted(active, key=lambda e: e.timestamp)[:COMPACT_COUNT]
            ids_to_compact = {e.id for e in to_compact}

            # 压缩前提取长期记忆（best-effort，失败不影响压缩）
            if longterm is not None:
                try:
                    from .consolidate import extract_longterm_memories
                    ltm_candidates = await extract_longterm_memories(
                        llm, to_compact, longterm,
                    )
                    for cand in ltm_candidates:
                        await longterm.save(
                            category=cand["category"],
                            title=cand["title"],
                            content=cand["content"],
                            tags=cand.get("tags", []),
                            source="compaction",
                        )
                except Exception as exc:
                    logger.warning("长期记忆提取失败，不影响压缩: %s", exc)

            # 先总结，失败则保留原始条目（不归档）
            try:
                summary = await _summarize(llm, to_compact)
            except Exception as exc:
                logger.warning("记忆压缩总结失败，保留原始条目: %s", exc)
                return

            # 总结成功后才归档
            for e in self._entries:
                if e.id in ids_to_compact:
                    e.archived = True
            self.add("thought", summary, importance=5)
            await asyncio.to_thread(self._flush_sync)
            self._append_to_archive(to_compact)
            logger.info("✅ 记忆压缩完成，归档 %d 条", len(to_compact))

    # ── 内部 ──────────────────────────────────────────────────────────────────

    async def _flush_loop(self):
        """后台防抖写盘：等 event → 0.5s 防抖 → 异步写盘"""
        while True:
            await self._flush_event.wait()
            self._flush_event.clear()
            await asyncio.sleep(0.5)       # 防抖：合并 0.5s 内的多次 add
            try:
                await asyncio.to_thread(self._flush_sync)
            except Exception as exc:
                logger.error("后台写盘失败: %s", exc)

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
        """同步写盘（由 _flush_loop 通过 asyncio.to_thread 调度，不阻塞事件循环）"""
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
        # JSON 备份（保留，向后兼容）
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

        # SQLite 双写（归档条目可能已在 add() 时写入，INSERT OR IGNORE 防重复）
        if self._history_db is not None:
            try:
                self._history_db.insert(entries)
            except Exception as exc:
                logger.warning("HistoryDB 归档写入失败: %s", exc)


_TYPE_LABEL_ZH = {
    "heard": "用户说",
    "said": "我说",
    "thought": "总结",
}


async def _summarize(llm, entries: List[MemoryEntry]) -> str:
    """用 LLM 将一批记忆总结为一条 thought（叙事压缩，留在 episodic 流）。

    Phase 3 改进：
    - 字数 80 → 150
    - 类型翻译为自然语言（heard → "用户说"，said → "我说"）
    """
    lines = []
    for e in sorted(entries, key=lambda x: x.timestamp):
        t = datetime.fromtimestamp(e.timestamp).strftime("%H:%M")
        label = _TYPE_LABEL_ZH.get(e.type, e.type)
        lines.append(f"[{t}] {label}: {e.content}")
    bulk = "\n".join(lines)

    resp = await llm.chat(
        user_message=(
            f"以下是小Q 最近的部分对话记录，请用 2-3 句话总结这一段时间里发生了什么、"
            f"对话主题、用户和小Q 的状态：\n{bulk}"
        ),
        system_prompt="你是记忆整理助手，用简短中文叙事性总结。不超过 150 字，不加任何前缀。",
    )
    return (resp.text or "").strip() or "（一段时光悄悄过去了）"
