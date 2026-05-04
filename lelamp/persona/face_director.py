"""FaceDirector — 把对话语义 / 情绪 映射到设备表情。

输入:用户/助手文本 + 关键事件(IMU tap / 摄像头检测人脸)
输出:HTTP POST 到 buddy daemon /face 或 /arc(daemon 持 USB 出口)

设计:
  - 不直接持 DisplayController(避免跟 buddy daemon 争 USB)
  - 走 daemon HTTP 路由(协议 v0.5.0 增加的 /face /arc 端点)
  - 节流:1Hz 上限,避免 set_face 风暴

两层决策:
  1. arc 触发 — 关键词匹配(粗粒度,启动剧本)
  2. face hint — LLM 回复尾部 <face>name</face> 标签解析(细粒度)
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

DAEMON_BASE_URL = "http://127.0.0.1:9000"
SET_FACE_MIN_INTERVAL_SEC = 1.0  # 1Hz 节流上限

# 16 个 face(协议 v0.5.0 §3.10,跟 xq_face.h 对齐)
VALID_FACES = frozenset({
    "neutral", "focus", "idle_watch", "sleep", "content", "warm_smile",
    "listen", "comfort", "wink", "smirk", "side_eye", "peek",
    "surprised", "blush", "sleepy", "love",
})

# 6 个 arc 剧本(协议 v0.5.0 §3.11)
VALID_ARCS = frozenset({
    "morning", "pat", "tease", "goodnight", "noticed", "comfort",
})

# 关键词 → arc 触发映射(在用户语音里识别到这些词时,直接触发剧本)
ARC_KEYWORDS: dict[str, list[str]] = {
    "morning":   ["早安", "早上好", "good morning", "醒了"],
    "goodnight": ["晚安", "good night", "睡了", "睡觉了"],
    "comfort":   ["难过", "不开心", "好累", "想哭"],
    "tease":     ["逗你", "调皮", "坏蛋"],
    # pat / noticed 由 IMU / 摄像头事件触发,不走关键词
}

# face 标签正则:LLM 输出 <face>warm_smile</face> 风格
FACE_TAG_RE = re.compile(r"<face>\s*([a-z_]+)\s*</face>", re.IGNORECASE)


class FaceDirector:
    """把对话语义映射到设备表情。线程安全(节流锁)。"""

    def __init__(
        self,
        daemon_url: str = DAEMON_BASE_URL,
        timeout: float = 1.0,
    ):
        self._url = daemon_url.rstrip("/")
        self._timeout = timeout
        self._lock = threading.Lock()
        self._last_face_at: float = 0.0
        self._last_face: Optional[str] = None

    # ---------- 公共接口 ----------

    def set_face(self, face: str) -> bool:
        """直接设置 face,带节流。返回 True/False 表示是否真发送。

        节流:同一 face 1 秒内不重发;不同 face 间隔无下限(用户期待立即反应)。
        """
        face = face.strip().lower()
        if face not in VALID_FACES:
            logger.warning("set_face: 未知 face %r,忽略", face)
            return False
        now = time.monotonic()
        with self._lock:
            if face == self._last_face and (now - self._last_face_at) < SET_FACE_MIN_INTERVAL_SEC:
                return False
            self._last_face = face
            self._last_face_at = now
        return self._post("/face", {"face": face})

    def play_arc(self, arc: str) -> bool:
        """触发预定义 arc 剧本。"""
        arc = arc.strip().lower()
        if arc not in VALID_ARCS:
            logger.warning("play_arc: 未知 arc %r,忽略", arc)
            return False
        # arc 不节流(频率本来就低,通常事件触发)
        return self._post("/arc", {"arc": arc})

    def maybe_arc_from_user_text(self, text: str) -> Optional[str]:
        """用户语音文本里识别 arc 关键词;命中触发并返回 arc 名,否则 None。

        在 SoulAgent.on_speech 入口处调,粗粒度先于 LLM 处理。
        """
        if not text:
            return None
        low = text.lower()
        for arc, keywords in ARC_KEYWORDS.items():
            for kw in keywords:
                if kw in low:
                    self.play_arc(arc)
                    logger.info("arc 触发 %s(关键词 %r)", arc, kw)
                    return arc
        return None

    def maybe_face_from_assistant_text(self, text: str) -> Optional[str]:
        """LLM 回复尾部 <face>name</face> 标签解析,命中调 set_face 并返回名。

        识别到合法 face 就返回名(不管 daemon 是否真发送成功);没标签返回 None。
        发送失败由内部 log 处理。
        """
        if not text:
            return None
        match = FACE_TAG_RE.search(text)
        if not match:
            return None
        name = match.group(1).strip().lower()
        if name not in VALID_FACES:
            logger.warning("LLM 输出未知 face %r,忽略", name)
            return None
        self.set_face(name)
        return name

    @staticmethod
    def strip_face_tag(text: str) -> str:
        """从 LLM 输出移除 <face>...</face> 标签(避免 TTS 朗读)。"""
        if not text:
            return text
        return FACE_TAG_RE.sub("", text).strip()

    # ---------- HTTP 转发 ----------

    def _post(self, path: str, payload: dict) -> bool:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self._url + path,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as r:
                if r.status != 200:
                    logger.warning("daemon %s 返回 %d", path, r.status)
                    return False
            return True
        except urllib.error.URLError as e:
            logger.warning("daemon %s 调用失败(daemon 可能没启): %s", path, e)
            return False
        except Exception as e:
            logger.warning("daemon %s 异常: %s", path, e)
            return False

    def close(self) -> None:
        # urllib 无连接池,nothing to close
        pass
