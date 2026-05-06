"""BuddyAwareness — 后台 asyncio task,2s 一次 poll daemon /state,
state 字段变化时驱动 lamp motion 配合 cc 工作状态(协议 v0.5.1)。

职责边界:
  - 只看 state 字段变化,winner_sid 切换不触发(避免两 cc 来回 grab winner 时动作风暴)
  - 不管 face/arc(由 FaceDirector 走 daemon HTTP 路由处理)
  - 启动时记初值不触发(避免开机时跟 wake_up 撞)
  - daemon 不可达时静默(避免 log 风暴)
  - **只在 device_mode == "buddy" 时触发 motion**(persona 模式让位 SoulAgent)

mode 同步双职责:
  每次 poll 同时拿 `state` 和 `device_mode`,把 device_mode 写回 mode_box[0],
  让 main_persona 的 motion_svc gate 间接读到。
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.error
import urllib.request
from typing import Callable, Optional

from lelamp.transport.buddy_motions import STATE_TO_GENERATOR

logger = logging.getLogger(__name__)

POLL_INTERVAL_SEC = 2.0
HTTP_TIMEOUT_SEC = 1.0
_VALID_STATES = frozenset({"idle", "busy", "attention", "celebrate", "failed", "sleep"})
_VALID_MODES = frozenset({"persona", "buddy"})


class BuddyAwareness:
    """轮询 daemon /state,buddy 模式下根据 cc state 变化驱动 lamp motion。"""

    def __init__(
        self,
        play_keyframes_fn: Callable,    # 原 motion_svc.play_keyframes 引用(绕过 mode gate)
        daemon_url: str = "http://127.0.0.1:9000",
        mode_box: Optional[list] = None,  # 单元素 list,跨闭包共享 device_mode
        poll_interval: float = POLL_INTERVAL_SEC,
    ) -> None:
        self._play_keyframes = play_keyframes_fn
        self._url = daemon_url.rstrip("/")
        self._mode_box = mode_box if mode_box is not None else ["persona"]
        self._poll_interval = poll_interval

        self._last_state: Optional[str] = None
        self._was_unreachable: bool = False  # 用来打"daemon 恢复"日志,避免重复打
        self._stopping = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

    # ---------- lifecycle ----------

    async def start(self) -> None:
        """创建并启动后台轮询 task。幂等。"""
        if self._task is not None:
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop(), name="buddy_awareness")
        logger.info("BuddyAwareness 已启动 url=%s poll=%.1fs", self._url, self._poll_interval)

    async def stop(self) -> None:
        """优雅停止:set _stopping → 等 task 退出 ≤3s。"""
        if self._task is None:
            return
        self._stopping.set()
        try:
            await asyncio.wait_for(self._task, timeout=3.0)
        except asyncio.TimeoutError:
            logger.warning("BuddyAwareness stop 超时,强制取消")
            self._task.cancel()
        except Exception:
            logger.exception("BuddyAwareness stop 异常")
        self._task = None
        logger.info("BuddyAwareness 已停止")

    # ---------- 主循环 ----------

    async def _loop(self) -> None:
        """主循环:首轮记录初始 state(不触发),之后只在 state 变化时触发动作。"""
        while not self._stopping.is_set():
            fetched = await self._fetch_state()
            if fetched is None:
                # daemon 不可达,静默
                if not self._was_unreachable:
                    logger.debug("BuddyAwareness: daemon 不可达,静默重试")
                    self._was_unreachable = True
            else:
                state, device_mode = fetched
                if self._was_unreachable:
                    logger.info("BuddyAwareness: daemon 恢复 state=%s mode=%s", state, device_mode)
                    self._was_unreachable = False

                # 同步 mode_box(让 SoulAgent 的 motion gate 读到)
                if device_mode in _VALID_MODES:
                    self._mode_box[0] = device_mode

                # 判断 state 变化触发 motion
                if self._last_state is None:
                    # 首次发现,只记录不触发(避免跟开机 wake_up 撞)
                    self._last_state = state
                    logger.info("BuddyAwareness 启动初值 state=%s device_mode=%s(不触发)",
                                state, device_mode)
                elif state != self._last_state:
                    logger.info("buddy state %s → %s (mode=%s)",
                                self._last_state, state, device_mode)
                    self._last_state = state
                    self._dispatch_motion(state)

            # 可中断 sleep:_stopping 被 set 时立即唤醒
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self._poll_interval)
                break  # _stopping 被 set → 退出
            except asyncio.TimeoutError:
                continue  # 正常 tick

    async def _fetch_state(self) -> Optional[tuple[str, str]]:
        """GET daemon /state,返回 (state, device_mode) 或 None。

        失败静默(只 DEBUG log)。daemon 重启期间的连续失败不打 warning。
        """
        try:
            body = await asyncio.to_thread(self._sync_get_state)
        except Exception as e:
            logger.debug("BuddyAwareness fetch 异常: %s", e)
            return None
        if body is None:
            return None
        state = body.get("state")
        device_mode = body.get("device_mode", "persona")  # 老 daemon 没字段时 fallback
        if state not in _VALID_STATES:
            logger.warning("BuddyAwareness: 未知 state %r,跳过", state)
            return None
        return (state, device_mode)

    def _sync_get_state(self) -> Optional[dict]:
        """同步 GET /state,放在 to_thread 里跑。"""
        try:
            req = urllib.request.Request(self._url + "/state", method="GET")
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SEC) as r:
                if r.status != 200:
                    return None
                return json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return None

    def _dispatch_motion(self, state: str) -> None:
        """state → STATE_TO_GENERATOR 查表 → play_keyframes(用原方法,绕过 gate)。

        device_mode != buddy 时不触发(让位 SoulAgent)。
        """
        if self._mode_box[0] != "buddy":
            logger.debug("BuddyAwareness: device_mode=%s 非 buddy,跳过 %s 动作",
                         self._mode_box[0], state)
            return
        gen = STATE_TO_GENERATOR.get(state)
        if gen is None:
            logger.warning("BuddyAwareness: 未知 state %s,无对应 motion", state)
            return
        try:
            segments = gen()
        except Exception:
            logger.exception("BuddyAwareness: 生成 segments 失败 state=%s", state)
            return
        try:
            err = self._play_keyframes(segments, intent=f"buddy_{state}")
            if err:
                logger.warning("BuddyAwareness play_keyframes 失败: %s", err)
        except Exception:
            logger.exception("BuddyAwareness play_keyframes 异常 state=%s", state)
