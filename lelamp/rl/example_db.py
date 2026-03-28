"""
示例数据库 — few-shot 检索 + 高分样本持久化

每条记录：
  emotion, intensity, f1, f2, f3, duration, accel_ratio, asymmetry, reward, source, timestamp

用途：
  1. 从 LLM/模板生成的高分样本中积累 few-shot 种子
  2. generate() 调用前按 (emotion, intensity) 检索最近邻高分样本注入 prompt
  3. Stage 4：CMA-ES 训练数据来源
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional

import numpy as np

from lelamp.motion.templates import MotionKeyframes

logger = logging.getLogger(__name__)

_DEFAULT_PATH = Path("lelamp/recordings/examples.json")


class ExampleDB:
    """
    线程安全的 JSON 示例数据库。

    用法：
        db = ExampleDB()
        db.store("happy", 0.8, kf, reward=0.75)
        examples = db.retrieve("happy", 0.8, k=2)
    """

    REWARD_THRESHOLD = 0.50   # 只存储 reward 超过此值的样本
    MAX_PER_BUCKET   = 50     # 每个 (emotion, intensity±0.25) 桶最多保留条数

    def __init__(self, db_path: Path | str = _DEFAULT_PATH):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock  = threading.Lock()
        self._data: list[dict] = self._load()
        logger.info("ExampleDB: %d 条样本  path=%s", len(self._data), self._path)

    # ── 读写 ──────────────────────────────────────────────────────────────────

    def _load(self) -> list[dict]:
        if self._path.exists():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning("ExampleDB 加载失败，重置: %s", e)
        return []

    def _save(self):
        try:
            self._path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.warning("ExampleDB 保存失败: %s", e)

    # ── 公共接口 ──────────────────────────────────────────────────────────────

    def store(
        self,
        emotion:   str,
        intensity: float,
        kf:        MotionKeyframes,
        reward:    float,
        source:    str = "llm",
    ) -> bool:
        """
        存储一条样本。

        Returns:
            True 表示已存储，False 表示 reward 不达标跳过
        """
        if reward < self.REWARD_THRESHOLD:
            return False

        entry = {
            "emotion":     emotion,
            "intensity":   round(float(intensity), 2),
            "f1":          [round(v, 2) for v in kf.f1.tolist()],
            "f2":          [round(v, 2) for v in kf.f2.tolist()],
            "f3":          [round(v, 2) for v in kf.f3.tolist()],
            "duration":    round(float(kf.duration), 2),
            "accel_ratio": round(float(kf.accel_ratio), 3),
            "asymmetry":   round(float(kf.asymmetry), 3),
            "reward":      round(float(reward), 4),
            "source":      source,
            "timestamp":   time.time(),
        }

        with self._lock:
            self._data.append(entry)
            self._trim()
            self._save()

        return True

    def retrieve(
        self,
        emotion:   str,
        intensity: float,
        k:         int = 3,
    ) -> list[MotionKeyframes]:
        """
        检索最相近的 k 条高分样本（按 reward 降序）。
        intensity 窗口：±0.35
        """
        with self._lock:
            candidates = [
                e for e in self._data
                if e["emotion"] == emotion
                and abs(e["intensity"] - intensity) < 0.35
            ]

        if not candidates:
            return []

        candidates.sort(key=lambda e: e["reward"], reverse=True)

        results: list[MotionKeyframes] = []
        for entry in candidates[:k]:
            try:
                results.append(MotionKeyframes(
                    f1         = np.array(entry["f1"], dtype=np.float32),
                    f2         = np.array(entry["f2"], dtype=np.float32),
                    f3         = np.array(entry["f3"], dtype=np.float32),
                    duration   = entry["duration"],
                    accel_ratio= entry["accel_ratio"],
                    asymmetry  = entry["asymmetry"],
                    intent     = f"[DB] {entry['source']} reward={entry['reward']:.2f}",
                ))
            except Exception:
                continue
        return results

    def size(self, emotion: Optional[str] = None) -> int:
        with self._lock:
            if emotion is None:
                return len(self._data)
            return sum(1 for e in self._data if e["emotion"] == emotion)

    def all_entries(self) -> list[dict]:
        """返回全部原始记录（供 RL 训练读取）"""
        with self._lock:
            return list(self._data)

    # ── 内部维护 ──────────────────────────────────────────────────────────────

    def _trim(self):
        """每个桶最多保留 MAX_PER_BUCKET 条，按 reward 保留最高分"""
        buckets: dict[tuple, list] = defaultdict(list)
        for e in self._data:
            # 以 0.5 为粒度分桶
            bucket_key = (e["emotion"], round(e["intensity"] * 2) / 2)
            buckets[bucket_key].append(e)

        trimmed: list[dict] = []
        for entries in buckets.values():
            entries.sort(key=lambda x: x["reward"], reverse=True)
            trimmed.extend(entries[: self.MAX_PER_BUCKET])

        self._data = trimmed
