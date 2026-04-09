"""consolidate — 把 episodic 事件流"提炼"到更长期的层。

三个独立任务：
  - extract_facts(llm, events, existing_facts) → 候选 fact
      产出 facts.json 候选项；caller 通过 PendingFactBuffer 做"独立 session"双确认
      后才 promote 到 FactStore。
  - today_narrative(llm, events, prev_summary) → ~200 字当天叙事
      产出 [TODAY] 段，给 LLM 提供"今天大致发生了什么"的滚动摘要。
  - compress_episodic 与 episodic.MemoryStream.compact_if_needed() 同义，
      该路径已经在 MemoryStream 内部实现（_summarize），不在本模块重复。
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .episodic import MemoryEntry, MemoryStream
    from .facts import FactStore

logger = logging.getLogger(__name__)

# 抽取允许的 fact kind（与 facts.py 的 ALLOWED_FACT_KINDS 对齐，
# 但不包含 daily_reflection——那是 today_narrative 跨日时由 SoulAgent 写入的）
_EXTRACTABLE_KINDS = {"identity", "preference", "calling"}


@dataclass
class FactCandidate:
    """LLM 抽取的候选 fact，未确认前不会进 FactStore。"""
    kind: str
    key: str
    value: str
    confidence: float
    source_event_ids: list[str] = field(default_factory=list)
    proposed_at: float = field(default_factory=time.time)


# ── extract_facts ─────────────────────────────────────────────────────────

_EXTRACT_SYSTEM_PROMPT = """你是小Q 的长期记忆抽取助手。从一段对话中找出"对用户/世界长期成立"的稳定事实。

只在你 80% 以上确信这是稳定事实时才返回。判断标准：
- 玩笑、比喻、夸张 → 不返回
- 临时状态（"今天有点累"）→ 不返回
- 一次性事件（"刚才喝了一杯咖啡"）→ 不返回
- 模糊的、不确定的 → 不返回
- 用户明确表达的偏好/称谓/身份 → 返回
- 反复出现的、可被未来场景复用的事实 → 返回

允许的 kind：
- identity（对一个人的固定理解，例如 voice:张三 = "张三，软件工程师"）
- calling（用什么称呼用户，例如 user_call_name = "老板"）
- preference（用户偏好/习惯，例如 likes_cats = "喜欢猫"）

严格输出 JSON 数组，不要任何前后缀，不要 markdown。每个元素：
  {"kind": "...", "key": "...", "value": "...", "confidence": 0.0-1.0}

如果没有任何符合条件的 fact，返回 [] 。

如果某条 fact 在"现有已确认 fact"中已经存在且 value 一致，不要再返回它——
用户再次提及已知偏好不是新证据，重复返回会让 pending 缓冲反复消耗 source_event_ids。"""


def _events_to_dialogue(events: list["MemoryEntry"]) -> str:
    lines = []
    for e in events:
        t = datetime.fromtimestamp(e.timestamp).strftime("%H:%M")
        if e.type == "heard":
            lines.append(f"[{t}] 用户: {e.content}")
        elif e.type == "said":
            lines.append(f"[{t}] 小Q: {e.content}")
        # thought 不喂进抽取——它本身就是压缩产物，会污染抽取信噪比
    return "\n".join(lines)


def _format_existing_facts(fs: "FactStore") -> str:
    lines = []
    for kind in sorted(_EXTRACTABLE_KINDS):
        items = fs.list_by_kind(kind)
        if not items:
            continue
        for f in items:
            lines.append(f"- {kind}.{f.key} = {f.value}")
    return "\n".join(lines) if lines else "（暂无）"


def _parse_candidates(text: str, source_event_ids: list[str]) -> list[FactCandidate]:
    """从 LLM 输出中提取 JSON 数组。

    宽容处理：去掉可能的 ```json``` 包裹。
    """
    s = text.strip()
    # 剥掉 ```json ... ``` 或 ``` ... ```
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", s, re.DOTALL)
    if m:
        s = m.group(1).strip()
    if not s:
        return []
    try:
        data = json.loads(s)
    except json.JSONDecodeError as exc:
        logger.warning("extract_facts JSON 解析失败: %s | 原文: %s", exc, text[:200])
        return []
    if not isinstance(data, list):
        return []
    out: list[FactCandidate] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        kind = (item.get("kind") or "").strip()
        key = (item.get("key") or "").strip()
        value = (item.get("value") or "").strip()
        try:
            conf = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        if not (kind and key and value):
            continue
        if kind not in _EXTRACTABLE_KINDS:
            continue
        if conf < 0.8:
            continue
        out.append(FactCandidate(
            kind=kind,
            key=key,
            value=value,
            confidence=conf,
            source_event_ids=list(source_event_ids),
        ))
    return out


async def extract_facts(
    llm,
    events: list["MemoryEntry"],
    existing_facts: "FactStore",
    timeout: float = 60.0,
) -> list[FactCandidate]:
    """从一批 episodic 事件中抽取候选 fact。

    异步路径，timeout=60s（不在用户响应热路径上）。
    输出尚未写入 FactStore——caller 通过 PendingFactBuffer 二次确认后才 promote。
    """
    dialogue = _events_to_dialogue(events)
    if not dialogue.strip():
        return []
    existing = _format_existing_facts(existing_facts)
    user_msg = (
        f"现有已确认 fact:\n{existing}\n\n"
        f"最近的对话：\n{dialogue}\n\n"
        f"按 system prompt 的标准抽取候选 fact。"
    )
    try:
        resp = await llm.chat(
            user_message=user_msg,
            system_prompt=_EXTRACT_SYSTEM_PROMPT,
        )
    except Exception as exc:
        logger.warning("extract_facts LLM 调用失败: %s", exc)
        return []

    source_ids = [e.id for e in events]
    candidates = _parse_candidates(resp.text or "", source_ids)
    if candidates:
        logger.info("📤 extract_facts 抽出 %d 条候选 fact", len(candidates))
    return candidates


# ── PendingFactBuffer ─────────────────────────────────────────────────────

class PendingFactBuffer:
    """候选 fact 的"独立 session"二次确认缓冲。

    判定规则：候选 fact 进入 pending 后，需要由第二个独立的抽取轮次再次抽出
    本质上等价的 fact（同 kind+key），且两者的 source_event_ids 没有任何重叠，
    才 promote 到 FactStore。

    理由：避免"同一段对话被同一个 LLM 在同一 prompt 里反复确认自己"造成误 promote。
    """

    def __init__(self):
        self._pending: list[FactCandidate] = []

    def __len__(self) -> int:
        return len(self._pending)

    def consider(
        self,
        candidates: list[FactCandidate],
        fact_store: "FactStore",
    ) -> list[FactCandidate]:
        """喂入新的候选 fact，返回本轮被 promote 的 fact 列表。

        被 promote 的 fact 同时调用 fact_store.upsert() 写入。
        """
        promoted: list[FactCandidate] = []
        for cand in candidates:
            existing_pending = self._find_pending(cand.kind, cand.key)
            if existing_pending is None:
                # 第一次见到，进 pending
                self._pending.append(cand)
                continue
            # 再次见到 → 检查 source_event_ids 是否独立
            old_ids = set(existing_pending.source_event_ids)
            new_ids = set(cand.source_event_ids)
            if old_ids.isdisjoint(new_ids):
                # 独立 session 二次确认 → promote
                fact_store.upsert(
                    kind=cand.kind,
                    key=cand.key,
                    value=cand.value,
                    confidence=max(existing_pending.confidence, cand.confidence),
                    source_event_ids=sorted(old_ids | new_ids),
                )
                promoted.append(cand)
                self._pending.remove(existing_pending)
            else:
                # 同一 session 内重复出现 → 不 promote，但更新 value、合并 source_ids
                existing_pending.value = cand.value
                existing_pending.confidence = max(
                    existing_pending.confidence, cand.confidence
                )
                merged = list(dict.fromkeys(
                    existing_pending.source_event_ids + cand.source_event_ids
                ))
                existing_pending.source_event_ids = merged
        return promoted

    def _find_pending(self, kind: str, key: str) -> Optional[FactCandidate]:
        for c in self._pending:
            if c.kind == kind and c.key == key:
                return c
        return None

    def gc(self, max_age_sec: float = 7 * 86400) -> int:
        """清理超过 max_age 的 pending 候选（默认 7 天）。返回清理数量。"""
        now = time.time()
        before = len(self._pending)
        self._pending = [
            c for c in self._pending if (now - c.proposed_at) <= max_age_sec
        ]
        return before - len(self._pending)


# ── today_narrative ───────────────────────────────────────────────────────

_TODAY_SYSTEM_PROMPT = """你是小Q 的当天叙事整理员。把一段对话和上一版叙事融合，
更新为一段紧凑的"今天发生了什么"的中文叙事。

要求：
- 不超过 200 字
- 第三人称视角（"用户"和"小Q"）
- 只写发生过的事和明显的状态变化，不要评价、不要推测
- 保留最近的关键互动和已知约定，丢弃过时细节
- 不加任何前缀，不加 markdown，直接输出叙事文本"""


async def today_narrative(
    llm,
    events_today: list["MemoryEntry"],
    prev_summary: Optional[str] = None,
    timeout: float = 60.0,
) -> str:
    """根据今天的 episodic 事件 + 上一版叙事，生成新的当天叙事摘要。

    调用方负责：
      - 过滤出今天的 events（按 timestamp）
      - 节流（新 heard/said ≥5 且距上次 ≥10 分钟）
      - 跨日把昨天的 narrative 转存为 daily_reflection fact
    """
    dialogue = _events_to_dialogue(events_today)
    if not dialogue.strip():
        return prev_summary or ""

    user_msg = (
        (f"上一版叙事：\n{prev_summary}\n\n" if prev_summary else "")
        + f"今天的对话事件：\n{dialogue}\n\n"
        + "按 system prompt 的要求生成新的当天叙事。"
    )
    try:
        resp = await llm.chat(
            user_message=user_msg,
            system_prompt=_TODAY_SYSTEM_PROMPT,
        )
    except Exception as exc:
        logger.warning("today_narrative LLM 调用失败: %s", exc)
        return prev_summary or ""
    text = (resp.text or "").strip()
    return text or (prev_summary or "")
