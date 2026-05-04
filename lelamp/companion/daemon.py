"""Companion daemon — HTTP server + Claude Code hook 事件入口。

启动:
  uv run python -m lelamp.companion                    # 真硬件
  uv run python -m lelamp.companion --mock-display     # 不连屏
  uv run python -m lelamp.companion --no-arm           # 不启用臂

API 见 dev-agent-events.md §2。
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

# 保证 lelamp 可 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from lelamp.companion.snapshot import (  # noqa: E402
    SessionSnapshot,
    Prompt,
    Error,
    derive_state,
)
from lelamp.companion.state_machine import SessionStateMachine  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_HTTP_PORT = 9000
APPROVAL_TIMEOUT_SEC = 60.0

# 模块级:HTTP handler 通过它访问 state machine
_machine: Optional[SessionStateMachine] = None


# ──────────────────────────────────────────────────────────────────────────
# HTTP Handler
# ──────────────────────────────────────────────────────────────────────────

class CompanionHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        logger.debug("HTTP " + fmt % args)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length == 0:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    # ----- routes -----

    def do_GET(self):
        assert _machine is not None
        if self.path == "/state":
            from lelamp.companion.snapshot import aggregate_state
            sessions = _machine.get_sessions()
            agg_state, winner_sid = aggregate_state(sessions)
            winning_snap = sessions.get(winner_sid) if winner_sid else _machine.get_snapshot()
            self._send(200, {
                "ok": True,
                "state": agg_state,
                "winner_sid": winner_sid,
                "snapshot": _snapshot_to_dict(winning_snap),  # 向后兼容:winning 那个
                "sessions": {sid: _snapshot_to_dict(s) for sid, s in sessions.items()},
            })
        elif self.path == "/health":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        assert _machine is not None
        body = self._read_json()
        if self.path == "/event":
            try:
                response = handle_event(_machine, body)
                self._send(200, response)
            except Exception as e:
                logger.exception("handle_event 异常")
                self._send(500, {"ok": False, "error": str(e)})
        elif self.path == "/update":
            try:
                handle_update(_machine, body)
                self._send(200, {"ok": True})
            except Exception as e:
                logger.exception("handle_update 异常")
                self._send(500, {"ok": False, "error": str(e)})
        elif self.path == "/reset":
            # 手动清掉所有非 _default 的 session(用于关终端但 SessionEnd 没触发的情况)
            try:
                n = _machine.reset_sessions()
                self._send(200, {"ok": True, "removed": n})
            except Exception as e:
                logger.exception("handle_reset 异常")
                self._send(500, {"ok": False, "error": str(e)})
        elif self.path == "/shutdown":
            self._send(200, {"ok": True})
            # 让主线程退出 serve_forever
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self._send(404, {"ok": False, "error": "not_found"})


# ──────────────────────────────────────────────────────────────────────────
# 事件处理(canonical event → snapshot mutation)
# ──────────────────────────────────────────────────────────────────────────

def _summarize_tool_input(tool: str, tinput: dict) -> str:
    """把 tool_input 抽成一行短摘要,用于 activity log。

    各 tool 字段不同,挑最有信息量的一个。截到 60 字节。
    """
    if not isinstance(tinput, dict):
        return ""
    if tool == "Bash":
        cmd = tinput.get("command", "")
        return cmd[:60]
    if tool in ("Read", "NotebookEdit"):
        return str(tinput.get("file_path", ""))[-60:]
    if tool in ("Edit", "Write"):
        return str(tinput.get("file_path", ""))[-60:]
    if tool == "Glob":
        return str(tinput.get("pattern", ""))[:60]
    if tool == "Grep":
        return str(tinput.get("pattern", ""))[:60]
    if tool == "WebFetch":
        return str(tinput.get("url", ""))[:60]
    if tool == "WebSearch":
        return str(tinput.get("query", ""))[:60]
    # 通用 fallback
    for k in ("description", "prompt", "command", "file_path", "pattern", "url"):
        v = tinput.get(k)
        if v:
            return str(v)[:60]
    return ""


def handle_event(machine: SessionStateMachine, body: dict) -> dict:
    """canonical event → snapshot mutation。返回 HTTP body dict。

    所有事件按 body.session_id 路由到对应 session;无 sid 落 _default 桶。
    顺便把"人类可读"的事件摘要 push 进全局 activity log ring。
    """
    event_type = body.get("type")
    data = body.get("data") or {}
    sid = body.get("session_id") or None

    if event_type == "agent_session_start":
        # 新 session 起来 → 清 msg(给 task_started 落 session topic 让位)
        machine.mutate(session_id=sid, running=False, msg="")
        return {"ok": True, "decision": None}

    if event_type == "task_started":
        # 新任务开始:msg 显示**最新 prompt 的前 40 字符**(覆盖,不再粘性)
        # 清残留 prompt / error / completed_at
        summary = data.get("summary", "")
        machine.mutate(
            session_id=sid,
            running=True,
            completed_at=None,
            current_tool=None,
            elapsed_ms=0,
            msg=summary,
            prompt=None,
            error=None,
        )
        if summary:
            machine.add_activity(f"user: {summary}")
        return {"ok": True, "decision": None}

    if event_type == "tool_started":
        # 关键:清 prompt(若审批已通过,attention 应回 busy)
        tool = data.get("tool", "")
        machine.mutate(
            session_id=sid,
            current_tool=tool,
            prompt=None,
        )
        if tool:
            arg = _summarize_tool_input(tool, data.get("tool_input") or {})
            line = f"{tool}: {arg}" if arg else tool
            machine.add_activity(line)
        return {"ok": True, "decision": None}

    if event_type == "tool_completed":
        # 关键:清 prompt(用户在 attention 屏批了 yes 后,tool 执行完此 hook fire,
        # 派生应从 attention 回到 busy)。不清 prompt 会一直死挂 attention 到 Stop。
        machine.mutate(session_id=sid, current_tool=None, prompt=None)
        return {"ok": True, "decision": None}

    if event_type == "task_completed":
        # 任务结束:清 prompt(用户已经处理完了),触发 celebrate → idle
        machine.mutate(
            session_id=sid,
            running=False,
            completed_at=time.monotonic(),
            prompt=None,
        )
        machine.add_activity("✓ done")
        return {"ok": True, "decision": None}

    if event_type == "task_failed":
        msg = data.get("msg", "Unknown error")
        err = Error(msg=msg, timestamp=time.monotonic())
        machine.mutate(session_id=sid, error=err, running=False)
        machine.add_activity(f"✗ {msg}")
        return {"ok": True, "decision": None}

    if event_type == "agent_session_end":
        # 优先显式移除 session;无 sid 时仅 mutate 默认桶
        if sid:
            machine.remove_session(sid)
        else:
            machine.mutate(session_id=None, running=False)
        return {"ok": True, "decision": None}

    if event_type == "awaiting_approval":
        prompt_id = data.get("id") or f"prompt_{int(time.monotonic() * 1000)}"
        tool = data.get("tool", "unknown")
        cmd = data.get("command", "")
        prompt = Prompt(
            id=prompt_id,
            tool=tool,
            command=cmd,
            hint=data.get("hint"),
        )
        # snapshot 加 prompt → 派生为 attention → 下发 set_state attention + show_prompt
        machine.mutate(session_id=sid, prompt=prompt)
        machine.add_activity(f"⚠ needs you: {tool} {cmd}"[:80])

        # blocking 模式(默认 false):
        #   - false(Notification hook 用):set prompt 即返回,屏保持 attention,
        #     由后续事件(task_completed / task_started / Stop)清掉 prompt
        #   - true(将来 PreToolUse 真审批用):阻塞等设备 evt:approval,返回 yes/no
        if not bool(data.get("blocking", False)):
            return {"ok": True, "decision": None}

        decision = machine.wait_for_approval(
            prompt_id=prompt_id,
            timeout=APPROVAL_TIMEOUT_SEC,
        )
        machine.mutate(session_id=sid, prompt=None)
        return {"ok": True, "decision": decision}

    return {"ok": False, "error": f"unknown event type: {event_type}"}


def handle_update(machine: SessionStateMachine, body: dict) -> None:
    """直接 merge partial snapshot(供调试 / 兼容 Anthropic snapshot 转发)。

    白名单字段;未识别的字段静默忽略。
    可带 session_id;不带则落 _default 桶。
    """
    sid = body.get("session_id") or None
    allowed = {"running", "msg", "tokens_today", "current_tool", "elapsed_ms"}
    changes = {k: v for k, v in body.items() if k in allowed}
    if changes:
        machine.mutate(session_id=sid, **changes)


def _snapshot_to_dict(snap: SessionSnapshot) -> dict:
    """SessionSnapshot → JSON-friendly dict(用于 GET /state 调试)。"""
    return {
        "last_updated": snap.last_updated,
        "running": snap.running,
        "prompt": (
            {"id": snap.prompt.id, "tool": snap.prompt.tool, "command": snap.prompt.command}
            if snap.prompt else None
        ),
        "error": (
            {"msg": snap.error.msg, "timestamp": snap.error.timestamp}
            if snap.error else None
        ),
        "completed_at": snap.completed_at,
        "current_tool": snap.current_tool,
        "elapsed_ms": snap.elapsed_ms,
        "tokens_today": snap.tokens_today,
        "msg": snap.msg,
        "entries": list(snap.entries),
        "agent": snap.agent,
        "session_id": snap.session_id,
    }


# ──────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_HTTP_PORT, help="HTTP 端口")
    parser.add_argument("--display-port", default=None, help="USB CDC 端口,默认按 VID 自动找")
    parser.add_argument("--arm-port", default=None, help="舵机端口,默认按 VID 自动找")
    parser.add_argument("--mock-display", action="store_true", help="display 走 mock 模式")
    parser.add_argument("--no-arm", action="store_true", help="不启用 arm")
    parser.add_argument("--lamp-id", default="lelamp")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s %(message)s",
    )
    log = logging.getLogger("companion")

    # ---- 启动 transport ----
    from lelamp.transport import (
        ArmController,
        DisplayController,
        find_arm_port,
        find_display_port,
    )

    if args.mock_display:
        disp = DisplayController(mock=True)
    else:
        try:
            disp_port = args.display_port or find_display_port()
        except RuntimeError as e:
            log.error("找不到显示设备: %s", e)
            log.error("→ 用 --display-port 指定,或 --mock-display 走 mock")
            return 1
        disp = DisplayController(port=disp_port)
    disp.start()
    log.info("Display 已启动")

    arm = None
    if not args.no_arm:
        try:
            arm_port = args.arm_port or find_arm_port()
            arm = ArmController(port=arm_port, lamp_id=args.lamp_id)
            arm.start()
            log.info("Arm 已启动: %s", arm_port)
        except Exception as e:
            log.warning("Arm 未启动,降级为纯屏模式: %s", e)
            arm = None

    # ---- 启动 state machine ----
    global _machine
    _machine = SessionStateMachine(disp, arm)
    _machine.start()

    # ---- 启动 transcript watcher(协议 v0.4.0:今日 tokens)----
    from lelamp.companion.transcript_watcher import (  # noqa: E402
        TranscriptWatcher,
        encode_cwd_to_project_dir,
    )
    import os
    cwd = os.getcwd()
    project_dir = Path.home() / ".claude" / "projects" / encode_cwd_to_project_dir(cwd)
    watcher = TranscriptWatcher(project_dir, on_tokens_change=_machine.set_tokens_today)
    watcher.start()

    # ---- bootstrap 已活跃的 session(daemon 启动前已开的 cc)----
    # daemon 不能"扫描进程",但 Claude Code transcript jsonl mtime 能反映 session 活跃度。
    # 扫 project_dir 下 mtime 在 SESSION_IDLE_TTL(15min) 内的 jsonl,
    # 把 session_id(文件名 stem)懒注册为 idle 占位。后续 hook 事件覆盖真实状态;
    # 无事件的话 SESSION_IDLE_TTL GC 会自动清掉,不留鬼 session。
    if project_dir.exists():
        from lelamp.companion.snapshot import SESSION_IDLE_TTL  # noqa: E402
        now_wall = time.time()
        cutoff = now_wall - SESSION_IDLE_TTL
        active_sids = []
        for f in project_dir.glob("*.jsonl"):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            sid = f.stem
            if sid:
                active_sids.append(sid)
        n = _machine.bulk_register_sessions(active_sids)
        if n:
            log.info("Bootstrap 已注册 %d 个活跃 session(从 transcript mtime 推断,15min 内有更新)", n)

    # ---- 启动 HTTP server ----
    server = ThreadingHTTPServer(("127.0.0.1", args.port), CompanionHandler)
    log.info("Daemon 监听 http://127.0.0.1:%d", args.port)
    log.info("  GET  /state    查 snapshot + 派生 state")
    log.info("  POST /event    canonical event 入口")
    log.info("  POST /update   partial snapshot patch")
    log.info("  POST /reset    手动清掉所有非默认 session(屏角清屏)")
    log.info("  POST /shutdown 优雅关闭")

    exit_code = 0
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Ctrl-C 收到,优雅关闭...")
    except Exception:
        log.exception("Daemon 异常")
        exit_code = 1
    finally:
        server.server_close()
        try:
            watcher.stop()
        except Exception:
            log.exception("transcript watcher stop 异常")
        try:
            _machine.stop()
        except Exception:
            log.exception("state machine stop 异常")
        if arm is not None:
            try:
                arm.stop()
            except Exception:
                log.exception("arm stop 异常")
        try:
            disp.stop()
        except Exception:
            log.exception("disp stop 异常")
        log.info("Daemon 已停止")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
