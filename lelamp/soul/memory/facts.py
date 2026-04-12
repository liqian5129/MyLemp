"""FactStore — 结构化语义事实层（Phase 2）。

API：upsert / forget / list_by_kind（last-write-wins 语义）

持久化：~/.lelamp/facts.json（顶层 {facts: [...]}）
写盘策略：临时文件 → atomic rename

注：commitment（定时提醒）已迁移到 lelamp.soul.reminder.ReminderService，
不再由 FactStore 管理。facts.json 的 "commitments" key 被加载时静默忽略。
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_FACTS_FILE = Path.home() / ".lelamp" / "facts.json"

ALLOWED_FACT_KINDS = {"identity", "preference", "calling", "daily_reflection"}


@dataclass
class Fact:
    """普通 fact：identity / preference / calling / daily_reflection。

    last-write-wins 语义：同 (kind, key) 的新写入直接覆盖旧值。
    历史变化由 episodic 流自然记录，facts 只回答"此刻是什么"。
    """
    id: str
    kind: str
    key: str
    value: str
    confidence: float = 1.0
    first_seen: float = 0.0
    last_confirmed: float = 0.0
    source_event_ids: list[str] = field(default_factory=list)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


class FactStore:
    """L3 结构化事实。JSON 文件持久化。

    总条目数预期 < 100，全量进 prompt（按 kind 分组）。
    """

    def __init__(self, path: Path = _FACTS_FILE):
        self._path = path
        self._facts: dict[str, Fact] = {}   # id -> Fact
        self._load()

    # ── 持久化 ────────────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("FactStore 读盘失败，按空库启动: %s", exc)
            return

        for item in raw.get("facts", []):
            try:
                fact = Fact(**item)
                self._facts[fact.id] = fact
            except TypeError as exc:
                logger.warning("跳过损坏 fact 条目: %s (%s)", item, exc)
        # 注：旧版 facts.json 可能含 "commitments" key（已迁移到 ReminderService），
        # 这里静默忽略，下次 _save() 时不再写回。

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {
            "facts": [asdict(f) for f in self._facts.values()],
        }
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)

    # ── 普通 fact API ─────────────────────────────────────────────────────
    def upsert(
        self,
        kind: str,
        key: str,
        value: str,
        confidence: float = 1.0,
        source_event_ids: Optional[list[str]] = None,
    ) -> Fact:
        """last-write-wins 写入。"""
        if kind not in ALLOWED_FACT_KINDS:
            raise ValueError(
                f"unknown fact kind: {kind}（允许: {sorted(ALLOWED_FACT_KINDS)}）"
            )

        now = time.time()
        # 同 (kind, key) 直接覆盖
        existing = self._find_by_kind_key(kind, key)
        if existing is not None:
            existing.value = value
            existing.confidence = confidence
            existing.last_confirmed = now
            if source_event_ids:
                # 累积来源（保留历史链回 episodic）
                merged = list(dict.fromkeys(existing.source_event_ids + source_event_ids))
                existing.source_event_ids = merged
            self._save()
            return existing

        fact = Fact(
            id=_new_id("fact"),
            kind=kind,
            key=key,
            value=value,
            confidence=confidence,
            first_seen=now,
            last_confirmed=now,
            source_event_ids=list(source_event_ids or []),
        )
        self._facts[fact.id] = fact
        self._save()
        return fact

    def forget(self, kind: str, key: str) -> bool:
        existing = self._find_by_kind_key(kind, key)
        if existing is None:
            return False
        del self._facts[existing.id]
        self._save()
        return True

    def list_by_kind(self, kind: str) -> list[Fact]:
        return [f for f in self._facts.values() if f.kind == kind]

    def all_active_facts(self) -> list[Fact]:
        return list(self._facts.values())

    def _find_by_kind_key(self, kind: str, key: str) -> Optional[Fact]:
        for f in self._facts.values():
            if f.kind == kind and f.key == key:
                return f
        return None

    # ── 渲染 ──────────────────────────────────────────────────────────────
    def format(self, confirmed_speaker: str | None = None) -> str:
        """渲染为 [FACTS] 段正文。

        分组顺序：identity → calling → preference → daily_reflection
        空分组省略。无任何条目时返回空串。

        confirmed_speaker: 当前声纹确认的说话人。为 None 时跳过
        identity/calling 分组，防止 LLM 在心跳时凭身份信息猜名字。
        """
        sections: list[str] = []
        order = ["identity", "calling", "preference", "daily_reflection"]
        labels = {
            "identity": "身份",
            "calling": "称谓",
            "preference": "偏好",
            "daily_reflection": "历史日反思",
        }
        for kind in order:
            # 无确认身份时不暴露 identity/calling
            if confirmed_speaker is None and kind in ("identity", "calling"):
                continue
            items = self.list_by_kind(kind)
            if not items:
                continue
            lines = [f"{labels[kind]}:"]
            for f in sorted(items, key=lambda x: x.key):
                lines.append(f"  {f.key}: {f.value}")
            sections.append("\n".join(lines))

        return "\n".join(sections)
