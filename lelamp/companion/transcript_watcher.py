"""TranscriptWatcher — 轮询本项目 Claude Code transcript JSONL,累加今日 tokens。

Claude Code 把会话 transcript 落到:
  ~/.claude/projects/<encoded-cwd>/<session-id>.jsonl

每行 JSON;`type=assistant` 的 message 带 `usage` 字段(input/output/cache 各项)。
本 watcher:
  - 启动时全量扫历史,过滤"今日"(local)的 assistant 行求 token 之和作初值
  - 1Hz 轮询每个 jsonl 文件 stat(),发现 size 增大 → 从上次 offset 增量读
  - 跨 local 0:00 自动清零
  - 节流交给 callback(SessionStateMachine.set_tokens_today 内部已节流)

不引入 watchdog 依赖 —— stat() 在数 KB-MB 文件上极轻,1Hz polling 完全不是瓶颈。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 1.0


def encode_cwd_to_project_dir(cwd: str) -> str:
    """Claude Code 把路径所有非字母数字字符替换为 `-` 作目录名。

    例:`/Users/foo/lelamp_runtime` → `-Users-foo-lelamp-runtime`(`_` 也变 `-`)。
    实测验证:实际目录是 `-Users-qliau-playground-lelamp-runtime`。
    """
    return re.sub(r"[^a-zA-Z0-9]+", "-", cwd)


def _local_today_start_epoch() -> float:
    """本地 0:00 对应的 epoch 秒。"""
    now = time.localtime()
    today_start = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, now.tm_isdst))
    return today_start


def _parse_iso_to_epoch(ts: str) -> Optional[float]:
    """`2026-05-02T10:35:15.412Z` → epoch 秒。失败返回 None。"""
    if not ts or not isinstance(ts, str):
        return None
    try:
        # Python 3.11+ fromisoformat 支持 Z
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


def _sum_usage(usage: dict) -> int:
    """对 input + cache_creation + cache_read + output 求和。"""
    if not isinstance(usage, dict):
        return 0
    return (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("cache_creation_input_tokens", 0) or 0)
        + int(usage.get("cache_read_input_tokens", 0) or 0)
        + int(usage.get("output_tokens", 0) or 0)
    )


class TranscriptWatcher:
    """监听 ~/.claude/projects/<encoded-cwd>/*.jsonl 累加今日 tokens。"""

    def __init__(
        self,
        project_dir: Path,
        on_tokens_change: Callable[[int], None],
    ):
        self._dir = project_dir
        self._on_change = on_tokens_change

        self._lock = threading.Lock()
        self._offsets: dict[Path, int] = {}  # 文件 → 已读到的 byte offset
        self._today_tokens: int = 0
        self._today_start: float = _local_today_start_epoch()

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ---------- lifecycle ----------

    def start(self) -> None:
        if not self._dir.exists():
            logger.warning("transcript 目录不存在,watcher 不启动: %s", self._dir)
            return
        self._bootstrap()
        self._on_change(self._today_tokens)
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, name="transcript-watcher", daemon=True
        )
        self._thread.start()
        logger.info(
            "TranscriptWatcher 已启动 dir=%s 今日初值=%d tokens",
            self._dir, self._today_tokens,
        )

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None

    # ---------- 内部 ----------

    def _bootstrap(self) -> None:
        """启动时扫所有 jsonl,统计今日初值,并记录每个文件的 byte offset。"""
        for f in sorted(self._dir.glob("*.jsonl")):
            try:
                with open(f, "rb") as fp:
                    for raw in fp:
                        self._maybe_count(raw)
                    self._offsets[f] = fp.tell()
            except OSError:
                logger.exception("扫描 transcript 失败: %s", f)

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            self._stop_event.wait(POLL_INTERVAL_SEC)
            if self._stop_event.is_set():
                return
            try:
                self._check_date_rollover()
                self._poll_files()
            except Exception:
                logger.exception("transcript watcher tick 异常")

    def _check_date_rollover(self) -> None:
        new_today_start = _local_today_start_epoch()
        if new_today_start > self._today_start:
            # 跨日 → 立即清零并通知
            with self._lock:
                self._today_start = new_today_start
                self._today_tokens = 0
            logger.info("transcript watcher 跨日清零")
            self._on_change(0)

    def _poll_files(self) -> None:
        """扫目录,每个 jsonl 比对 size,增量读尾部新增字节。"""
        try:
            files = list(self._dir.glob("*.jsonl"))
        except OSError:
            return

        gained = 0
        for f in files:
            try:
                size = f.stat().st_size
            except OSError:
                continue
            offset = self._offsets.get(f, 0)
            if size <= offset:
                # size < offset:文件被替换/truncate(理论上不会,Claude Code append-only)
                # 不重读避免重复计数,直接对齐到 size
                if size < offset:
                    self._offsets[f] = size
                continue
            try:
                with open(f, "rb") as fp:
                    fp.seek(offset)
                    chunk = fp.read(size - offset)
                    new_offset = fp.tell()
            except OSError:
                continue
            # 按行切;最后一行可能不完整,留到下次
            tail_complete = chunk.endswith(b"\n")
            lines = chunk.split(b"\n")
            if not tail_complete:
                # 最后片段不完整 —— 回退 offset 让下次重读
                incomplete = lines.pop()
                new_offset -= len(incomplete)
            for raw in lines:
                gained += self._maybe_count(raw, return_n=True) or 0
            self._offsets[f] = new_offset

        if gained > 0:
            self._on_change(self._today_tokens)

    def _maybe_count(self, raw: bytes, return_n: bool = False):
        """解析单行,若是今日 assistant message 则累加 usage。

        return_n=True 时返回本行贡献的 token 数(供 _poll_files 判断是否触发回调)。
        """
        if not raw or not raw.strip():
            return 0 if return_n else None
        try:
            obj = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return 0 if return_n else None
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            return 0 if return_n else None
        # 按 local 今日过滤
        ts = obj.get("timestamp")
        epoch = _parse_iso_to_epoch(ts) if ts else None
        if epoch is not None and epoch < self._today_start:
            return 0 if return_n else None
        msg = obj.get("message")
        if not isinstance(msg, dict):
            return 0 if return_n else None
        n = _sum_usage(msg.get("usage"))
        if n > 0:
            with self._lock:
                self._today_tokens += n
        return n if return_n else None
