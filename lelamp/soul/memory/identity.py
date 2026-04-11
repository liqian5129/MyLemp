"""统一身份识别 — 声纹 + 人脸 embedding 存储与比对。

存储位置：~/.lelamp/identities/
  registry.json      — 元数据索引
  voice_<name>_<n>.npy  — 声纹 embedding
  face_<name>_<n>.npy   — 人脸 embedding

用法::

    mem = IdentityMemory()
    mem.register_voice("李谦", embedding)
    speaker = mem.identify_voice(embedding)  # → "李谦" or None
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

_DEFAULT_DIR = os.path.expanduser("~/.lelamp/identities")

# 相似度阈值（余弦相似度，0-1）
_VOICE_THRESHOLD = 0.65
_FACE_THRESHOLD = 0.45


def _cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    norm = np.linalg.norm(a) * np.linalg.norm(b)
    if norm < 1e-8:
        return 0.0
    return float(np.dot(a, b) / norm)


class IdentityMemory:
    """统一身份存储：声纹 + 人脸 embedding。

    启动时从磁盘加载全部 embedding 到内存，运行时纯内存比对。
    注册时写磁盘 + 更新内存。
    """

    def __init__(self, data_dir: str = _DEFAULT_DIR):
        self._dir = Path(data_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._registry_path = self._dir / "registry.json"

        # 内存中的 embedding 库：{name: [embedding, ...]}
        self._voice_embs: dict[str, list[np.ndarray]] = {}
        self._face_embs: dict[str, list[np.ndarray]] = {}

        self._load()

    # ── 加载 ─────────────────────────────────────────────────────────────────

    def _load(self) -> None:
        """从磁盘加载全部 embedding 到内存。"""
        if not self._registry_path.exists():
            logger.info("身份库为空，将在首次注册时创建")
            return

        try:
            registry = json.loads(self._registry_path.read_text("utf-8"))
        except Exception as exc:
            logger.warning("加载 registry.json 失败: %s", exc)
            return

        for name, info in registry.items():
            # 加载声纹
            for f in info.get("voice_files", []):
                path = self._dir / f
                if path.exists():
                    emb = np.load(path)
                    self._voice_embs.setdefault(name, []).append(emb)

            # 加载人脸
            for f in info.get("face_files", []):
                path = self._dir / f
                if path.exists():
                    emb = np.load(path)
                    self._face_embs.setdefault(name, []).append(emb)

        total_v = sum(len(v) for v in self._voice_embs.values())
        total_f = sum(len(v) for v in self._face_embs.values())
        logger.info(
            "身份库已加载：%d 人，%d 条声纹，%d 条人脸",
            len(registry), total_v, total_f,
        )

    def _save_registry(self) -> None:
        """将当前内存状态写回 registry.json。"""
        registry: dict[str, dict] = {}
        all_names = set(self._voice_embs.keys()) | set(self._face_embs.keys())
        for name in all_names:
            voice_files = [
                f"voice_{name}_{i}.npy"
                for i in range(len(self._voice_embs.get(name, [])))
            ]
            face_files = [
                f"face_{name}_{i}.npy"
                for i in range(len(self._face_embs.get(name, [])))
            ]
            registry[name] = {
                "voice_files": voice_files,
                "face_files": face_files,
            }
        self._registry_path.write_text(
            json.dumps(registry, ensure_ascii=False, indent=2), "utf-8"
        )

    # ── 注册 ─────────────────────────────────────────────────────────────────

    def register_voice(self, name: str, embedding: np.ndarray) -> int:
        """注册一条声纹 embedding，返回该用户当前声纹总数。"""
        embs = self._voice_embs.setdefault(name, [])
        idx = len(embs)
        embs.append(embedding)

        filename = f"voice_{name}_{idx}.npy"
        np.save(self._dir / filename, embedding)
        self._save_registry()
        logger.info("已注册声纹：%s（第 %d 条）", name, idx + 1)
        return idx + 1

    def register_face(self, name: str, embedding: np.ndarray) -> int:
        """注册一条人脸 embedding，返回该用户当前人脸总数。"""
        embs = self._face_embs.setdefault(name, [])
        idx = len(embs)
        embs.append(embedding)

        filename = f"face_{name}_{idx}.npy"
        np.save(self._dir / filename, embedding)
        self._save_registry()
        logger.info("已注册人脸：%s（第 %d 条）", name, idx + 1)
        return idx + 1

    # ── 识别 ─────────────────────────────────────────────────────────────────

    def identify_voice(
        self, embedding: np.ndarray, threshold: float = _VOICE_THRESHOLD
    ) -> Optional[str]:
        """比对声纹，返回最匹配的名字或 None。"""
        return self._identify(embedding, self._voice_embs, threshold)

    def identify_face(
        self, embedding: np.ndarray, threshold: float = _FACE_THRESHOLD
    ) -> Optional[str]:
        """比对单张人脸，返回最匹配的名字或 None。"""
        return self._identify(embedding, self._face_embs, threshold)

    def identify_faces(
        self, embeddings: list[np.ndarray], threshold: float = _FACE_THRESHOLD
    ) -> list[Optional[str]]:
        """批量比对多张人脸。"""
        return [self._identify(emb, self._face_embs, threshold) for emb in embeddings]

    def _identify(
        self,
        embedding: np.ndarray,
        db: dict[str, list[np.ndarray]],
        threshold: float,
    ) -> Optional[str]:
        best_name = None
        best_score = threshold  # 低于阈值的不要

        for name, embs in db.items():
            for ref in embs:
                score = _cosine_similarity(embedding, ref)
                if score > best_score:
                    best_score = score
                    best_name = name

        if best_name:
            logger.info("🔊 身份匹配: %s (score=%.3f, threshold=%.2f)", best_name, best_score, threshold)
        else:
            # 输出最高 score 帮助排查阈值问题
            top_name = None
            top_score = -1.0
            for name, embs in db.items():
                for ref in embs:
                    score = _cosine_similarity(embedding, ref)
                    if score > top_score:
                        top_score = score
                        top_name = name
            if top_name is not None:
                logger.info("🔇 身份未匹配: 最高 %s (score=%.3f, threshold=%.2f)", top_name, top_score, threshold)
        return best_name

    # ── 查询 ─────────────────────────────────────────────────────────────────

    def list_identities(self) -> list[dict]:
        """列出所有已注册身份。"""
        all_names = set(self._voice_embs.keys()) | set(self._face_embs.keys())
        return [
            {
                "name": name,
                "voice_count": len(self._voice_embs.get(name, [])),
                "face_count": len(self._face_embs.get(name, [])),
            }
            for name in sorted(all_names)
        ]

    @property
    def is_empty(self) -> bool:
        return not self._voice_embs and not self._face_embs
