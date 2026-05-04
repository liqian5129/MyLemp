"""SessionStateMachine — 持有多个 session 的 snapshot,响应 mutation,驱动 effect。

跟 dev-agent-events.md §3 派生触发时机对应。
单例,**线程安全**(HTTP server 多线程,设备 reader 线程,1Hz tick 线程都会触发)。

副作用全部封装在这里:
  - 持有 DisplayController(必需)+ ArmController(可选)
  - mutate(session_id, **changes) → 派生 → 下发(set_state / set_text / show_prompt / set_session_pips)
  - 1Hz tick 处理时间窗口过期 + 30s 一次 GC 清掉长期空闲 session

多 session 设计:
  - self._sessions: dict[sid, SessionSnapshot],没带 sid 的事件进 _default 桶
  - aggregate_state 跨 session 取最高优先级 winner
  - _info_cache key 加 winner sid 前缀,winner 切换时自动 invalidate
  - len(sessions) > 1 时下发 set_session_pips,= 1 时清空
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from typing import Optional

from lelamp.companion.snapshot import (
    SessionSnapshot,
    Prompt,
    Error,
    derive_state,
    aggregate_state,
    initial_snapshot,
    SESSION_IDLE_TTL,
)

logger = logging.getLogger(__name__)

TICK_INTERVAL_SEC = 1.0
GC_INTERVAL_TICKS = 30   # 每 30 个 tick(~30 秒)做一次 session GC

# activity log: 全局 ring(屏底只渲一份),debounce 节流避免 USB CDC 灌爆
ACTIVITY_LOG_MAX = 8
ACTIVITY_DEBOUNCE_SEC = 0.2

# tokens 节流(set_tokens 同样需要)
TOKENS_THRESHOLD = 100
TOKENS_INTERVAL_SEC = 30.0


class SessionStateMachine:
    DEFAULT_SID = "_default"

    def __init__(self, disp, arm=None):
        """
        Args:
            disp: DisplayController(必需,且已 start)
            arm:  ArmController(可选,也已 start;None 表示不驱动臂)
        """
        self._disp = disp
        self._arm = arm

        self._lock = threading.Lock()
        # 多 session:每个 sid 一份 snapshot
        self._sessions: dict[str, SessionSnapshot] = {}

        # 派生 / 下发缓存
        self._last_dispatched_state: Optional[str] = None
        self._last_winner_sid: Optional[str] = None
        self._info_cache: dict[str, str] = {}      # 文字去重
        self._last_prompt_key: Optional[tuple] = None  # show_prompt 去重
        self._last_pips_sig: Optional[tuple] = None   # set_session_pips 去重

        # activity log:全局 deque(entries[0] 最新)
        self._activity_ring: deque[str] = deque(maxlen=ACTIVITY_LOG_MAX)
        self._activity_dirty: bool = False
        self._last_activity_sig: Optional[tuple] = None
        self._last_activity_dispatch: float = 0.0

        # tokens 节流
        self._last_tokens_sent: Optional[int] = None
        self._last_tokens_dispatch: float = 0.0

        self._stop_event = threading.Event()
        self._tick_thread: Optional[threading.Thread] = None

    # ---------- lifecycle ----------

    def start(self) -> None:
        """初始化默认 session 到 idle,启动 1Hz tick 线程。"""
        with self._lock:
            self._sessions[self.DEFAULT_SID] = initial_snapshot()
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
        # 退出前回 idle + 清 pips
        try:
            self._disp.set_state("idle")
            if self._arm:
                self._arm.set_state("idle")
            try:
                self._disp.set_session_pips([])
            except AttributeError:
                pass  # DisplayController 未实现 set_session_pips 时静默
        except Exception:
            logger.exception("stop 收尾下发失败")
        logger.info("SessionStateMachine 已停止")

    # ---------- 公共 mutation 接口 ----------

    def mutate(
        self,
        session_id: Optional[str] = None,
        **changes,
    ) -> SessionSnapshot:
        """更新指定 session 的 snapshot,触发派生 + 下发。

        session_id=None 时落入 _default 桶(向后兼容,/update 调试也走这条)。
        """
        sid = session_id or self.DEFAULT_SID
        with self._lock:
            cur = self._sessions.get(sid) or initial_snapshot()
            self._sessions[sid] = cur.with_(**changes)
            new_snap = self._sessions[sid]
        self._reconcile()
        return new_snap

    def remove_session(self, session_id: str) -> None:
        """显式移除 session(SessionEnd hook 用)。"""
        if not session_id or session_id == self.DEFAULT_SID:
            return
        with self._lock:
            removed = self._sessions.pop(session_id, None)
        if removed is not None:
            logger.info("移除 session %s", session_id)
            self._reconcile()

    def bulk_register_sessions(self, sids: list[str]) -> int:
        """批量注册 session 占位(daemon 启动时 bootstrap 用)。

        所有 sid 注册为 idle 状态,**最后只 reconcile 一次**。避免 N 次 reconcile
        导致 N 条 set_session_pips 突发(可能撑爆设备 USB CDC RX buffer)。
        """
        if not sids:
            return 0
        with self._lock:
            count = 0
            for sid in sids:
                if not sid or sid == self.DEFAULT_SID:
                    continue
                if sid in self._sessions:
                    continue  # 已存在跳过
                self._sessions[sid] = initial_snapshot()
                count += 1
        if count > 0:
            self._reconcile()
        return count

    def get_sessions(self) -> dict[str, SessionSnapshot]:
        """返回所有 session 的副本(供 /state 调试)。"""
        with self._lock:
            return dict(self._sessions)

    def get_snapshot(self) -> SessionSnapshot:
        """向后兼容:返回 winning session 的 snapshot。"""
        with self._lock:
            sessions = dict(self._sessions)
        _, winner_sid = aggregate_state(sessions)
        if winner_sid is None or winner_sid not in sessions:
            return initial_snapshot()
        return sessions[winner_sid]

    def wait_for_approval(self, prompt_id: str, timeout: float = 60.0) -> Optional[str]:
        """阻塞调用线程等设备 evt:approval。透传 DisplayController。"""
        return self._disp.wait_for_approval(prompt_id=prompt_id, timeout=timeout)

    # ---------- activity log(协议 v0.4.0)----------

    def add_activity(self, text: str) -> None:
        """往全局活动日志 ring 里 push 一条(entries[0] 最新)。

        单条 ≤80 字节(超出截断);相邻完全相同的连续条目去重(避免 PreToolUse 风暴)。
        实际下发由 _reconcile 调度,debounce 200ms。
        """
        if not text:
            return
        s = str(text).strip().replace("\n", " ").replace("\r", " ")
        if not s:
            return
        b = s.encode("utf-8")
        if len(b) > 80:
            s = b[:80].decode("utf-8", errors="ignore")
        with self._lock:
            # 跟当前最新一条完全一样 → 不入 ring(常见的连续 tool 调用降噪)
            if self._activity_ring and self._activity_ring[0] == s:
                return
            self._activity_ring.appendleft(s)
            self._activity_dirty = True

    def set_tokens_today(self, today: int) -> None:
        """transcript_watcher 用:推送当日累计 tokens(已节流)。

        节流:与上次差值 ≥ TOKENS_THRESHOLD 或时间 ≥ TOKENS_INTERVAL_SEC 才下发;
        跨日 today=0 时立即推。
        """
        n = max(0, int(today))
        now = time.monotonic()
        with self._lock:
            last = self._last_tokens_sent
            last_t = self._last_tokens_dispatch
            # 跨日清零(从大数变 0)立即推
            if n == 0 and last is not None and last > 0:
                send = True
            elif last is None:
                send = True
            else:
                send = (abs(n - last) >= TOKENS_THRESHOLD) or (now - last_t >= TOKENS_INTERVAL_SEC and n != last)
            if not send:
                return
            self._last_tokens_sent = n
            self._last_tokens_dispatch = now
        try:
            send_fn = getattr(self._disp, "set_tokens", None)
            if send_fn is not None:
                send_fn(n)
                logger.info("set_tokens dispatched today=%d", n)
        except Exception:
            logger.exception("set_tokens 下发失败")

    # ---------- 内部:tick / reconcile ----------

    def _tick_loop(self) -> None:
        """1Hz 触发 reconcile;每 30 tick 做一次 GC sweep。"""
        gc_counter = 0
        while not self._stop_event.is_set():
            self._stop_event.wait(TICK_INTERVAL_SEC)
            if self._stop_event.is_set():
                return
            try:
                self._reconcile()
            except Exception:
                logger.exception("tick reconcile 异常")

            gc_counter += 1
            if gc_counter >= GC_INTERVAL_TICKS:
                gc_counter = 0
                try:
                    self._gc_sessions()
                except Exception:
                    logger.exception("session GC 异常")

    def _reconcile(self) -> None:
        """聚合所有 session 取 winner,有变化即下发。"""
        with self._lock:
            sessions = dict(self._sessions)

        new_state, winner_sid = aggregate_state(sessions)
        winner_snap = sessions.get(winner_sid) if winner_sid else initial_snapshot()

        state_changed = (new_state != self._last_dispatched_state)
        winner_changed = (winner_sid != self._last_winner_sid)

        # 状态切换 → set_state(臂同步)
        if state_changed:
            logger.info("state %s → %s  winner=%s",
                        self._last_dispatched_state, new_state, winner_sid)
            try:
                self._dispatch_state(new_state)
            except Exception:
                logger.exception("set_state(%s) 下发失败", new_state)
                return
            self._last_dispatched_state = new_state

        # state 切换 或 winner 切换 → 清 info 缓存(让新 winner 的内容重发)
        if state_changed or winner_changed:
            self._info_cache.clear()
            self._last_prompt_key = None
            self._last_winner_sid = winner_sid
            # 清掉过期 transient 字段(只在 winner session 上做)
            self._maybe_clear_expired(winner_sid, winner_snap, new_state)

        # 显示信息刷新(每次 tick 都跑,内部去重)
        try:
            self._dispatch_info(winner_snap, new_state)
        except Exception:
            logger.exception("dispatch_info 失败")

        # 多 session 时下发屏角点
        try:
            self._dispatch_pips(sessions, winner_sid)
        except Exception:
            logger.exception("dispatch_pips 失败")

        # 活动日志(节流下发)
        try:
            self._dispatch_activity_log()
        except Exception:
            logger.exception("dispatch_activity_log 失败")

    def _dispatch_state(self, state: str) -> None:
        self._disp.set_state(state)
        if self._arm is not None:
            self._arm.set_state(state)

    def _dispatch_info(self, snap: SessionSnapshot, state: str) -> None:
        """根据当前 state 推送 set_text / show_prompt。

        **subtitle = session 标识(msg,即 session 最近的 prompt)**,所有 active state 都显,
        这样用户能从 subtitle 认出"这是哪个 terminal 的事件"。
        state 特定信息(工具名、错误、token 用量等)走 **meta** 槽或 show_prompt UI。
        """
        # subtitle:active state 显 session msg(标识),idle/sleep 清空
        if state in ("busy", "attention", "failed", "celebrate"):
            self._send_text_if_changed("subtitle", (snap.msg or "")[:200])
        else:  # idle / sleep
            self._send_text_if_changed("subtitle", "")

        # state-specific 信息走 meta 或 show_prompt
        if state == "busy":
            meta_parts = []
            if snap.current_tool:
                meta_parts.append(f"Running {snap.current_tool}")
            if snap.tokens_today > 0:
                meta_parts.append(f"{snap.tokens_today // 1000}k tokens")
            if snap.elapsed_ms > 0:
                meta_parts.append(f"{snap.elapsed_ms // 1000}s")
            self._send_text_if_changed("meta", " · ".join(meta_parts))

        elif state == "attention" and snap.prompt is not None:
            # 审批 UI 走 show_prompt;**desc 字段塞 session msg**,让用户在 attention 屏
            # 也能看到"是哪个 terminal 的 prompt 触发的"(Agent B 的 attention 布局
            # 不渲染 subtitle,只渲染 show_prompt 字段)
            session_label = snap.msg or snap.prompt.hint or ""
            key = (snap.prompt.id, snap.prompt.tool, snap.prompt.command, session_label)
            if self._last_prompt_key != key:
                self._disp.show_prompt(
                    id=snap.prompt.id,
                    tool=snap.prompt.tool,
                    command=snap.prompt.command,
                    desc=session_label[:80] if session_label else None,
                )
                self._last_prompt_key = key
            self._send_text_if_changed("meta", "")

        elif state == "failed" and snap.error is not None:
            # 错误信息走 meta(subtitle 已是 session msg,标识 session)
            self._send_text_if_changed("meta", snap.error.msg[:200])

        else:  # celebrate / idle / sleep
            self._send_text_if_changed("meta", "")

    def _send_text_if_changed(self, text_id: str, text: str) -> None:
        """set_text 去重:相同 (id, text) 不重发,避免 1Hz tick 刷屏。"""
        if self._info_cache.get(text_id) != text:
            self._disp.set_text(text_id, text)
            self._info_cache[text_id] = text

    def _maybe_clear_expired(
        self,
        winner_sid: Optional[str],
        snap: SessionSnapshot,
        new_state: str,
    ) -> None:
        """state 不再属于某些 transient 字段时,在 winner session 上清掉。"""
        if winner_sid is None:
            return
        # 离开 celebrate → 清 completed_at
        if new_state != "celebrate" and snap.completed_at is not None:
            self.mutate(session_id=winner_sid, completed_at=None)
        # 离开 failed → 清 error
        if new_state != "failed" and snap.error is not None:
            self.mutate(session_id=winner_sid, error=None)

    # ---------- 多 session 屏角点 ----------

    def _dispatch_pips(
        self,
        sessions: dict[str, SessionSnapshot],
        winner_sid: Optional[str],
    ) -> None:
        """多 session 时把每个 session 的状态作为一个圆点下发到屏角。

        单 session(包括 _default 桶单独存在的情况)不发,设备保持 pips 空。
        """
        # 数实际的非默认 session(_default 是占位,不算)
        active_sids = [sid for sid in sessions.keys() if sid != self.DEFAULT_SID]

        if len(active_sids) == 0:
            # 完全没活跃 session → 发空 list 让设备清屏角
            # (启动时 last_pips_sig=None 也发,清前一次 daemon 残留)
            if self._last_pips_sig != ():
                self._send_pips([])
                self._last_pips_sig = ()
            return

        # N≥1 全部下发(含单 session)。设备侧 session count label 从 pips.length 派生,
        # 单 session 也要显示"1 sessions",所以这里不再 ≤1 时清空。
        # 视觉上单 session 是否渲染屏角点由设备自决(协议未强制)。
        pips = []
        for sid in active_sids:
            snap = sessions[sid]
            state = derive_state(snap)
            pips.append({
                "sid": sid[:8],  # 截短显示
                "state": state,
                "winner": (sid == winner_sid),
            })
        # 排序:winner 优先,然后按 sid 稳定排(避免抖动)
        pips.sort(key=lambda p: (not p["winner"], p["sid"]))

        sig = tuple((p["sid"], p["state"], p["winner"]) for p in pips)
        if sig == self._last_pips_sig:
            return
        self._send_pips(pips)
        self._last_pips_sig = sig

    def _send_pips(self, pips: list) -> None:
        """安全调用 DisplayController.set_session_pips(可能未实现)。"""
        send = getattr(self._disp, "set_session_pips", None)
        if send is None:
            return  # 旧版本 DisplayController,静默忽略
        try:
            send(pips)
        except Exception:
            logger.exception("set_session_pips 失败")

    # ---------- activity log dispatch ----------

    def _dispatch_activity_log(self) -> None:
        """节流下发 set_activity_log。debounce 200ms,内容 hash 去重。"""
        now = time.monotonic()
        with self._lock:
            if not self._activity_dirty:
                return
            if (now - self._last_activity_dispatch) < ACTIVITY_DEBOUNCE_SEC:
                return  # 等下次 tick 再说(tick 1Hz,debounce 自然 ≥1s)
            entries = list(self._activity_ring)  # entries[0] 最新
            sig = tuple(entries)
            if sig == self._last_activity_sig:
                self._activity_dirty = False
                return
            self._last_activity_sig = sig
            self._last_activity_dispatch = now
            self._activity_dirty = False
        send = getattr(self._disp, "set_activity_log", None)
        if send is None:
            return
        try:
            send(entries)
            logger.info("set_activity_log dispatched n=%d head=%r",
                        len(entries), entries[0] if entries else None)
        except Exception:
            logger.exception("set_activity_log 失败")

    # ---------- session GC ----------

    def _gc_sessions(self) -> None:
        """清掉**长期无活动**的 session(任何 state,age > SESSION_IDLE_TTL)。

        不再区分 state — 因为 Claude Code 终端被 Ctrl-C / 强杀时 SessionEnd 不会
        触发,卡在 busy/attention 的 session 会留 ghost 点。绝对超时兜底处理。

        永不删 DEFAULT_SID。
        """
        now = time.monotonic()
        to_remove = []
        with self._lock:
            for sid, snap in self._sessions.items():
                if sid == self.DEFAULT_SID:
                    continue
                if (now - snap.last_updated) > SESSION_IDLE_TTL:
                    to_remove.append(sid)
        if not to_remove:
            return
        with self._lock:
            for sid in to_remove:
                self._sessions.pop(sid, None)
        logger.info("GC 清掉 %d 个长期无活动 session(>%ds): %s",
                    len(to_remove), int(SESSION_IDLE_TTL), to_remove)
        self._reconcile()

    def reset_sessions(self) -> int:
        """立即清掉所有非 _default 的 session(给 /reset 端点用)。

        返回清掉的数量。用户的"手动清屏角"按钮:当 daemon tracking 错(终端
        关掉但 SessionEnd 没触发)时,给个立即生效的退路。
        """
        with self._lock:
            removed = [sid for sid in self._sessions.keys() if sid != self.DEFAULT_SID]
            for sid in removed:
                self._sessions.pop(sid, None)
        if removed:
            logger.info("reset_sessions 清掉 %d 个 session: %s", len(removed), removed)
            self._reconcile()
        return len(removed)
