"""ReminderService — 定时提醒工具层。

设计原理：日程/提醒是"工具"，不是"记忆"。
  - 人脑记的是"我是个守约的人"（identity fact），具体"几点做什么"靠闹钟（tool）。
  - Push 通道：每条 reminder 自带 asyncio.create_task 到点回调。
  - Pull 通道：LLM 任何时候可调 list_reminders() 主动查询。
  - 跨重启：pending reminders 持久化到 ~/.lelamp/reminders.json，启动时重建 task；
    过期的直接丢弃（闹钟睡过头就过了）。

存储：~/.lelamp/reminders.json
  {"reminders": [{"id": "rem_...", "text": "...", "due_at": 1234.0, "created_at": 1234.0}]}

只存 active reminders。fired / cancelled 立即从文件删除，不保留历史。
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Coroutine, Optional

logger = logging.getLogger(__name__)

_REMINDERS_FILE = Path.home() / ".lelamp" / "reminders.json"
_TEXT_MAX_LEN = 200


def _fmt_ts(ts: float) -> str:
    """unix ts → 'YYYY-MM-DD HH:MM:SS'。"""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Reminder:
    id: str
    text: str
    due_at: float       # unix timestamp（系统算，不让 LLM 算）
    created_at: float


class ReminderService:
    """定时提醒服务。由 SoulAgent 持有，通过回调往事件队列推 ReminderFired。"""

    def __init__(self, path: Path = _REMINDERS_FILE):
        self._path = path
        self._reminders: dict[str, Reminder] = {}   # 持久化数据
        self._tasks: dict[str, "asyncio.Task"] = {}  # 运行时 sleep task
        self._on_fire: Optional[Callable[[str, str], Coroutine]] = None
        self._load()

    def set_on_fire(self, callback: Callable[[str, str], Coroutine]) -> None:
        """注册 fire 回调。签名: async def on_fire(reminder_id, text)。"""
        self._on_fire = callback

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """启动时为所有 pending 重建 asyncio task；过期的丢弃 + log。"""
        import asyncio  # noqa: local import（模块级不强制依赖 asyncio）

        now = time.time()
        expired_ids: list[str] = []
        for r in list(self._reminders.values()):
            if r.due_at <= now:
                expired_ids.append(r.id)
                logger.warning(
                    "🗑️ 错过的 reminder（重启后丢弃）: %s @ %s (%.1f 小时前到期)",
                    r.text, _fmt_ts(r.due_at), (now - r.due_at) / 3600,
                )
            else:
                self._schedule(r)

        if expired_ids:
            for rid in expired_ids:
                self._reminders.pop(rid, None)
            self._save()
            logger.info("启动时丢弃了 %d 条过期 reminder", len(expired_ids))

        active = len(self._reminders)
        if active:
            logger.info(
                "⏰ ReminderService 启动：%d 条 pending reminder 已重新调度",
                active,
            )

    async def stop(self) -> None:
        """关闭时取消所有 sleep task（不删 reminders.json 数据——下次启动恢复）。"""
        for task in self._tasks.values():
            task.cancel()
        self._tasks.clear()

    # ── 公共 API ──────────────────────────────────────────────────────────────

    def add(
        self,
        text: str,
        delay_seconds: float | None = None,
        at_time: float | None = None,
    ) -> Reminder:
        """创建一条 reminder。delay_seconds / at_time 二选一。

        delay_seconds: 相对延迟（秒），系统算绝对时间。LLM 不做时间算术。
        at_time: 绝对 unix 时间戳（由 dispatch handler 从 ISO 字符串转换而来）。
        """
        if (delay_seconds is None) == (at_time is None):
            raise ValueError("delay_seconds 和 at_time 必须恰好提供一个")

        now = time.time()
        if delay_seconds is not None:
            due_at = now + float(delay_seconds)
        else:
            due_at = float(at_time)  # type: ignore[arg-type]

        if due_at <= now:
            raise ValueError(
                f"due_at 必须在未来（收到 {_fmt_ts(due_at)}，"
                f"当前 {_fmt_ts(now)}）"
            )

        r = Reminder(
            id=f"rem_{uuid.uuid4().hex[:8]}",
            text=text.strip()[:_TEXT_MAX_LEN],
            due_at=due_at,
            created_at=now,
        )
        self._reminders[r.id] = r
        self._schedule(r)
        self._save()
        logger.info("⏰ 新 reminder: %s @ %s (%s)", r.text, _fmt_ts(r.due_at), r.id)
        return r

    def cancel(self, reminder_id: str) -> bool:
        """取消一条 pending reminder。返回是否成功。"""
        if reminder_id not in self._reminders:
            return False
        self._reminders.pop(reminder_id)
        task = self._tasks.pop(reminder_id, None)
        if task is not None and not task.done():
            task.cancel()
        self._save()
        logger.info("❎ 取消 reminder: %s", reminder_id)
        return True

    def find_by_text(self, keyword: str) -> list[Reminder]:
        """按关键词搜索 active reminders（子串匹配，大小写不敏感）。"""
        kw = keyword.lower()
        return [r for r in self._reminders.values() if kw in r.text.lower()]

    def list_active(self, within_seconds: float | None = None) -> list[Reminder]:
        """返回当前 pending reminders，按 due_at 升序。

        within_seconds: 只返回 due_at 在 now + within_seconds 之内的。
        """
        items = list(self._reminders.values())
        if within_seconds is not None:
            cutoff = time.time() + within_seconds
            items = [r for r in items if r.due_at <= cutoff]
        items.sort(key=lambda r: r.due_at)
        return items

    # ── 内部调度 ──────────────────────────────────────────────────────────────

    def _schedule(self, r: Reminder) -> None:
        """为单条 reminder 启动 asyncio sleep task。"""
        import asyncio

        self._tasks[r.id] = asyncio.create_task(
            self._fire_at(r), name=f"reminder-{r.id}",
        )

    async def _fire_at(self, r: Reminder) -> None:
        """睡到 due_at，唤醒后调回调，删除自身。"""
        import asyncio

        delay = max(0.0, r.due_at - time.time())
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            return  # 被 cancel() 取消，安静退出

        # 防御：sleep 期间可能被 cancel 删掉
        if r.id not in self._reminders:
            return

        if self._on_fire is not None:
            try:
                await self._on_fire(r.id, r.text)
            except Exception as exc:
                logger.error("reminder %s on_fire 回调异常: %s", r.id, exc)

        # fire 后删除：数据 + 运行时
        self._reminders.pop(r.id, None)
        self._tasks.pop(r.id, None)
        self._save()

    # ── 持久化 ────────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            with self._path.open("r", encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("ReminderService 读盘失败，按空启动: %s", exc)
            return
        for item in raw.get("reminders", []):
            try:
                r = Reminder(**item)
                self._reminders[r.id] = r
            except TypeError as exc:
                logger.warning("跳过损坏 reminder: %s (%s)", item, exc)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        payload = {"reminders": [asdict(r) for r in self._reminders.values()]}
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path)
