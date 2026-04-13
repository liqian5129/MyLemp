"""
场景记忆 — LLM 通过工具读写的持久化环境描述

存储：~/.lelamp/scene_memory.md
覆盖式更新，保持简洁不膨胀。写入时自动添加时间戳。
"""
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_SCENE_FILE = Path.home() / ".lelamp" / "scene_memory.md"
_MAX_LEN = 500


class SceneMemory:
    def __init__(self, path: Path = _SCENE_FILE):
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def read(self) -> str:
        if self._path.exists():
            text = self._path.read_text(encoding="utf-8").strip()
            if text:
                return text
        return "（还没有场景记忆，第一次 look 后记录）"

    def write(self, content: str):
        ts = datetime.now().strftime("[更新于 %m-%d %H:%M]")
        body = content[:_MAX_LEN].rstrip()
        self._path.write_text(f"{ts}\n{body}\n", encoding="utf-8")
        logger.info("📝 场景记忆已更新 (%d 字)", len(body))
