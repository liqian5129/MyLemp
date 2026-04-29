"""SessionStateMachine — 持有 snapshot,响应 mutation,驱动 effect。

跟 dev-agent-events.md §3 派生触发时机对应。
单例,**线程安全**(HTTP server 多线程,设备 reader 线程,1Hz tick 线程都会触发)。

副作用全部封装在这里:
  - 持有 DisplayController(必需)+ ArmController(可选)
  - mutate() → 派生 → 下发(set_state / set_text / show_prompt)
  - 1Hz tick 线程处理时间窗口过期(celebrate→idle / failed→idle / sleep 转换)
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from lelamp.companion.snapshot import (
    SessionSnapshot,
    Prompt,
    Error,
    derive_state,
    initial_snapshot,
)

logger = logging.getLogger(__name__)

TICK_INTERVAL_SEC = 1.0


class SessionStateMachine:
    def __init__(self, disp, arm=None):
        """
        Args:
            disp: DisplayController(必需,且已 start)
            arm:  ArmController(可选,也已 start;None 表示不驱动臂)
        """
        self._disp = disp
        self._arm = arm

        self._lock = threading.Lock()
        self._snap: SessionSnapshot = initial_snapshot()
        self._last_dispatched_state: Optional[str] = None

        # 文字 / prompt 去重缓存:避免 1Hz tick 重复推送同样内容
        self._info_cache: dict[str, str] = {}
        self._last_prompt_key: Optional[tuple] = None

        self._stop_event = threading.Event()
        self._tick_thread: Optional[threading.Thread] = None

    # ---------- lifecycle ----------

    def start(self) -> None:
        """初始化设备到当前派生状态,启动 1Hz tick 线程。"""
        self._reconcile()
        self._stop_event.clear()
        self._tick_thread = threading.Thread(
            target=self._tick_loop, name="companion-tick", daemon=True
        )
        self._tick_thread.start()
        logger.info("SessionStateMachine 已启动")

    def stop(self) -> None:
        self._stop_event.set()
        if self._tick_thread:
            self._tick_thread.join(timeout=2.0)
            self._tick_thread = None
        # 退出前回 idle(无论之前在哪个状态)
        try:
            self._disp.set_state("idle")
            if self._arm:
                self._arm.set_state("idle")
        except Exception:
            logger.exception("stop 收尾下发 idle 失败")
        logger.info("SessionStateMachine 已停止")

    # ---------- 公共 mutation 接口 ----------

    def mutate(self, **changes) -> SessionSnapshot:
        """更新 snapshot 字段,触发派生 + 下发。线程安全。"""
        with self._lock:
            self._snap = self._snap.with_(**changes)
            snap_copy = self._snap
        self._reconcile()
        return snap_copy

    def get_snapshot(self) -> SessionSnapshot:
        with self._lock:
            return self._snap

    def wait_for_approval(self, prompt_id: str, timeout: float = 60.0) -> Optional[str]:
        """阻塞调用线程等设备 evt:approval。
        返回 'yes' / 'no' / None(超时)。透传 DisplayController。"""
        return self._disp.wait_for_approval(prompt_id=prompt_id, timeout=timeout)

    # ---------- 内部:tick / reconcile ----------

    def _tick_loop(self) -> None:
        """1Hz 触发 reconcile,处理时间窗口过期(celebrate→idle 等)。"""
        while not self._stop_event.is_set():
            self._stop_event.wait(TICK_INTERVAL_SEC)
            if self._stop_event.is_set():
                return
            try:
                self._reconcile()
            except Exception:
                logger.exception("tick reconcile 异常")

    def _reconcile(self) -> None:
        """根据当前 snapshot 派生 state,有变化即下发。"""
        with self._lock:
            snap = self._snap
        new_state = derive_state(snap)

        # 状态 transition → set_state
        if new_state != self._last_dispatched_state:
            logger.info("state %s → %s", self._last_dispatched_state, new_state)
            try:
                self._dispatch_state(new_state)
            except Exception:
                logger.exception("set_state(%s) 下发失败", new_state)
                return
            self._last_dispatched_state = new_state
            # state 切换 → 清 info 缓存,新状态会重发一次该发的内容
            self._info_cache.clear()
            self._last_prompt_key = None
            # 清掉过期 transient 字段(error / completed_at)
            self._maybe_clear_expired(snap, new_state)

        # 显示信息刷新(每次都跑,即使 state 没变)
        try:
            self._dispatch_info(snap, new_state)
        except Exception:
            logger.exception("dispatch_info 失败")

    def _dispatch_state(self, state: str) -> None:
        self._disp.set_state(state)
        if self._arm is not None:
            self._arm.set_state(state)

    def _dispatch_info(self, snap: SessionSnapshot, state: str) -> None:
        """根据当前 state 推送 set_text / show_prompt。**去重**:相同内容不重发。"""
        if state == "busy":
            subtitle = (
                f"Running {snap.current_tool}" if snap.current_tool
                else (snap.msg or "Working...")
            )
            self._send_text_if_changed("subtitle", subtitle[:200])

            meta_parts = []
            if snap.tokens_today > 0:
                meta_parts.append(f"{snap.tokens_today // 1000}k tokens")
            if snap.elapsed_ms > 0:
                meta_parts.append(f"{snap.elapsed_ms // 1000}s")
            if meta_parts:
                self._send_text_if_changed("meta", " · ".join(meta_parts))

        elif state == "attention" and snap.prompt is not None:
            key = (snap.prompt.id, snap.prompt.tool, snap.prompt.command)
            if self._last_prompt_key != key:
                self._disp.show_prompt(
                    id=snap.prompt.id,
                    tool=snap.prompt.tool,
                    command=snap.prompt.command,
                    desc=snap.prompt.hint,
                )
                self._last_prompt_key = key

        elif state == "failed" and snap.error is not None:
            self._send_text_if_changed("subtitle", snap.error.msg[:200])

    def _send_text_if_changed(self, text_id: str, text: str) -> None:
        """set_text 去重:相同 (id, text) 不重发,避免 1Hz tick 刷屏。"""
        if self._info_cache.get(text_id) != text:
            self._disp.set_text(text_id, text)
            self._info_cache[text_id] = text

    def _maybe_clear_expired(self, snap: SessionSnapshot, new_state: str) -> None:
        """state 不再属于某些 transient 字段时,清掉那些字段避免重复触发。"""
        # 离开 celebrate → 清 completed_at
        if new_state != "celebrate" and snap.completed_at is not None:
            self.mutate(completed_at=None)
        # 离开 failed → 清 error
        if new_state != "failed" and snap.error is not None:
            self.mutate(error=None)
