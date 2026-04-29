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
            snap = _machine.get_snapshot()
            self._send(200, {
                "ok": True,
                "state": derive_state(snap),
                "snapshot": _snapshot_to_dict(snap),
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
        elif self.path == "/shutdown":
            self._send(200, {"ok": True})
            # 让主线程退出 serve_forever
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self._send(404, {"ok": False, "error": "not_found"})


# ──────────────────────────────────────────────────────────────────────────
# 事件处理(canonical event → snapshot mutation)
# ──────────────────────────────────────────────────────────────────────────

def handle_event(machine: SessionStateMachine, body: dict) -> dict:
    """canonical event → snapshot mutation。返回 HTTP body dict。"""
    event_type = body.get("type")
    data = body.get("data") or {}

    if event_type == "agent_session_start":
        # 新 session 起来 → 清 msg(给 task_started 落 session topic 让位)
        machine.mutate(running=False, msg="")
        return {"ok": True, "decision": None}

    if event_type == "task_started":
        # 新任务开始:清残留 prompt / error / completed_at
        # **session topic 粘性**:msg 只在当前为空时落地(session 内第一个 prompt),
        # 后续 prompts 不覆盖,屏 subtitle 在整个 session 维持同一句 topic
        current_msg = machine.get_snapshot().msg
        new_summary = data.get("summary", "")
        sticky_msg = current_msg if current_msg else new_summary

        machine.mutate(
            running=True,
            completed_at=None,
            current_tool=None,
            elapsed_ms=0,
            msg=sticky_msg,
            prompt=None,
            error=None,
        )
        return {"ok": True, "decision": None}

    if event_type == "tool_started":
        machine.mutate(
            current_tool=data.get("tool"),
            msg=data.get("summary", machine.get_snapshot().msg),
        )
        return {"ok": True, "decision": None}

    if event_type == "tool_completed":
        machine.mutate(current_tool=None)
        return {"ok": True, "decision": None}

    if event_type == "task_completed":
        # 任务结束:清 prompt(用户已经处理完了),触发 celebrate → idle
        machine.mutate(running=False, completed_at=time.monotonic(), prompt=None)
        return {"ok": True, "decision": None}

    if event_type == "task_failed":
        err = Error(
            msg=data.get("msg", "Unknown error"),
            timestamp=time.monotonic(),
        )
        machine.mutate(error=err, running=False)
        return {"ok": True, "decision": None}

    if event_type == "agent_session_end":
        machine.mutate(running=False)
        return {"ok": True, "decision": None}

    if event_type == "awaiting_approval":
        prompt_id = data.get("id") or f"prompt_{int(time.monotonic() * 1000)}"
        prompt = Prompt(
            id=prompt_id,
            tool=data.get("tool", "unknown"),
            command=data.get("command", ""),
            hint=data.get("hint"),
        )
        # snapshot 加 prompt → 派生为 attention → 下发 set_state attention + show_prompt
        machine.mutate(prompt=prompt)

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
        machine.mutate(prompt=None)
        return {"ok": True, "decision": decision}

    return {"ok": False, "error": f"unknown event type: {event_type}"}


def handle_update(machine: SessionStateMachine, body: dict) -> None:
    """直接 merge partial snapshot(供调试 / 兼容 Anthropic snapshot 转发)。

    白名单字段;未识别的字段静默忽略。
    """
    allowed = {"running", "msg", "tokens_today", "current_tool", "elapsed_ms"}
    changes = {k: v for k, v in body.items() if k in allowed}
    if changes:
        machine.mutate(**changes)


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

    # ---- 启动 HTTP server ----
    server = ThreadingHTTPServer(("127.0.0.1", args.port), CompanionHandler)
    log.info("Daemon 监听 http://127.0.0.1:%d", args.port)
    log.info("  GET  /state    查 snapshot + 派生 state")
    log.info("  POST /event    canonical event 入口")
    log.info("  POST /update   partial snapshot patch")
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
