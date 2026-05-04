"""DisplayController — Waveshare ESP32-S3 显示设备的 USB CDC JSON 客户端。

设计原则(参见 dev-protocol.md):
  - 内部全异步:后台 reader 线程持续读串口、解析 JSON 行、分发事件
  - 对外两套 API:
      1. 事件回调  disp.on("approval", cb)        — 用于并发场景
      2. 同步便利  disp.wait_for_approval(timeout) — 用于线性 demo 脚本
  - 命令发送 fire-and-forget,不阻塞主线程
  - mock 模式:不连真硬件,把发出的 JSON 打印,模拟 ack

协议版本:v0.5.0(含 set_face / play_arc;persona/buddy 双模式)
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections.abc import Callable
from typing import Any, Optional

logger = logging.getLogger(__name__)

EventCallback = Callable[[dict], None]

ESP32S3_VID = 0x303A  # Espressif Systems(原生 USB CDC)
DEFAULT_BAUDRATE = 115200
READ_CHUNK_BYTES = 256
READ_TIMEOUT_SEC = 0.1


def find_display_port() -> str:
    """按 VID 0x303A(Espressif)定位显示设备的 USB CDC 端口。

    注意:不能复用 lelamp.utils.find_serial_port —— 后者匹配 cu.usbmodem* 会同时
    命中舵机串口。这里必须用 VID 区分。
    """
    try:
        from serial.tools import list_ports
    except ImportError as e:
        raise RuntimeError("pyserial 未安装") from e

    candidates = [p for p in list_ports.comports() if p.vid == ESP32S3_VID]
    if not candidates:
        raise RuntimeError(
            f"找不到 Espressif USB CDC 设备(VID 0x{ESP32S3_VID:04X}),"
            "请确认设备已连接且固件启用 ARDUINO_USB_CDC_ON_BOOT=1"
        )
    if len(candidates) > 1:
        logger.warning("发现多个 ESP32-S3 设备,默认用第一个:%s", candidates[0].device)
    return candidates[0].device


class DisplayController:
    """跟显示设备的 JSON 行协议客户端。

    用法(同步便利风格):
        disp = DisplayController(port=find_display_port())
        disp.start()
        disp.set_state("attention")
        disp.show_prompt(id="req_1", tool="Bash", command="rm -rf /tmp/x")
        decision = disp.wait_for_approval(prompt_id="req_1", timeout=30)
        disp.stop()

    用法(回调风格):
        disp = DisplayController(...)
        disp.on("approval", lambda evt: print("got approval:", evt["decision"]))
        disp.start()
        disp.show_prompt(...)
        # 主线程继续做别的事

    mock 模式:
        DisplayController(mock=True)  # 不连硬件,自动 fake ack
    """

    def __init__(self, port: Optional[str] = None, mock: bool = False):
        if not mock and port is None:
            raise ValueError("非 mock 模式必须提供 port")
        self.port = port
        self.mock = mock

        self._serial: Any = None  # serial.Serial,延迟 import
        self._reader_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._event_queue: queue.Queue[dict] = queue.Queue(maxsize=256)
        self._handlers: dict[str, list[EventCallback]] = {}
        self._handlers_lock = threading.Lock()
        self._send_lock = threading.Lock()

    # ---------- lifecycle ----------

    def start(self) -> None:
        if self.mock:
            logger.info("[mock] DisplayController 启动")
        else:
            import serial  # 延迟 import,mock 模式不需要 pyserial
            self._serial = serial.Serial(self.port, DEFAULT_BAUDRATE, timeout=READ_TIMEOUT_SEC)
            logger.info("DisplayController 已连接:%s", self.port)

        self._stop_event.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="display-reader", daemon=True
        )
        self._reader_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._reader_thread:
            self._reader_thread.join(timeout=1.0)
            self._reader_thread = None
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                logger.exception("串口关闭异常")
            self._serial = None

    def __enter__(self) -> "DisplayController":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---------- 命令发送(Mac → 设备),fire-and-forget ----------

    def ping(self, seq: Optional[int] = None) -> None:
        payload: dict = {"cmd": "ping"}
        if seq is not None:
            payload["seq"] = seq
        self._send(payload)

    def set_state(self, state: str, transition_ms: int = 300) -> None:
        self._send({"cmd": "set_state", "state": state, "transition_ms": transition_ms})

    def set_progress(self, pct: int) -> None:
        self._send({"cmd": "set_progress", "pct": int(pct)})

    def set_text(self, id: str, text: str) -> None:
        self._send({"cmd": "set_text", "id": id, "text": text})

    def show_prompt(
        self, id: str, tool: str, command: str, desc: Optional[str] = None
    ) -> None:
        payload: dict = {"cmd": "show_prompt", "id": id, "tool": tool, "command": command}
        if desc is not None:
            payload["desc"] = desc
        self._send(payload)

    def set_brightness(self, value: int) -> None:
        self._send({"cmd": "set_brightness", "value": int(value)})

    def set_session_pips(self, pips: list) -> None:
        """多 session 状态点(协议 v0.3.0)。

        pips: list[{"sid": str, "state": str, "winner": bool}],可为空 list 清空。
        每次全量替换(非增量)。Mac daemon 在多 session 并发时下发。
        """
        self._send({"cmd": "set_session_pips", "pips": list(pips)})

    def set_activity_log(self, entries: list) -> None:
        """底部活动日志 strip(协议 v0.4.0)。

        entries: list[str],长度 0-8,entries[0] 最新;每条 ≤80 字节(超出截断)。
        全量替换。Mac daemon 在事件流变化时节流推送(≥200ms debounce)。
        """
        # 80 字节硬性截断,避免设备解析炸
        clean = []
        for e in entries[:8]:
            s = str(e)
            b = s.encode("utf-8")
            if len(b) > 80:
                # 截到 80 字节,但要落在 utf-8 char 边界
                s = b[:80].decode("utf-8", errors="ignore")
            clean.append(s)
        self._send({"cmd": "set_activity_log", "entries": clean})

    def set_tokens(self, today: int) -> None:
        """今日累计 tokens(协议 v0.4.0)。

        today: 非负整数,当前日历日累计 token 数(含 cache)。
        Mac daemon 节流:变化 ≥100 或 ≥30s 才推。
        """
        self._send({"cmd": "set_tokens", "today": max(0, int(today))})

    def set_face(self, face: str) -> None:
        """设置表情(协议 v0.5.0,persona 模式渲染)。

        face: 16 个枚举之一,跟 xq_face.h 对齐(lower_snake_case 去 XQ_ 前缀):
              neutral / focus / idle_watch / sleep / content / warm_smile /
              listen / comfort / wink / smirk / side_eye / peek /
              surprised / blush / sleepy / love

        行为(协议 §3.10):
          - persona 模式:渲染 face,300ms 淡入淡出过渡
          - buddy 模式:ack ok=true 但 store 不渲染,切回 persona 重绘
          - play_arc 进行时:ack ok=true detail=arc_in_progress,被忽略

        频率:不超过 1Hz。
        """
        self._send({"cmd": "set_face", "face": str(face)})

    def play_arc(self, arc: str) -> None:
        """播放表情剧本(协议 v0.5.0,persona 模式渲染)。

        arc: 6 个枚举之一,跟 xq_arcs.h 对齐:
             morning / pat / tease / goodnight / noticed / comfort

        行为(协议 §3.11):
          - persona 模式:演完整套剧本,内部步骤间 600ms 过渡
          - 进行中收到新 play_arc → 立即打断切到新 arc
          - 进行中收到 set_face → 被忽略
          - goodnight 永停 sleep 帧;其他 arc 停最后一帧
        """
        self._send({"cmd": "play_arc", "arc": str(arc)})

    def send_raw(self, payload: dict) -> None:
        """直接发送任意 payload(供测试 / 自定义命令用)。
        跟其他命令方法一样 fire-and-forget,绕过任何客户端校验。"""
        self._send(payload)

    # 关键命令打 INFO log,便于诊断设备渲染问题;高频命令走 DEBUG 避免刷屏
    _LOG_INFO_CMDS = {"set_state", "show_prompt", "set_brightness", "set_session_pips",
                      "set_face", "play_arc"}

    def _send(self, payload: dict) -> None:
        line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
        cmd = payload.get("cmd", "?")
        if self.mock:
            logger.info("[mock send] %s", line.strip())
            self._mock_response(payload)
            return
        with self._send_lock:
            assert self._serial is not None, "DisplayController 未 start"
            self._serial.write(line.encode("utf-8"))
        # 写完打 log(在锁外,避免阻塞);size 让我们看出突发命令体量,排查 USB CDC 缓冲
        size = len(line.encode("utf-8"))
        if cmd in self._LOG_INFO_CMDS:
            logger.info("→ send %s (%dB) %s", cmd, size, json.dumps({k: v for k, v in payload.items() if k != "cmd"}, ensure_ascii=False))
        else:
            logger.debug("→ send %s (%dB)", cmd, size)

    # ---------- 事件订阅 ----------

    def on(self, event_name: str, callback: EventCallback) -> None:
        """注册回调。event_name 是 evt 名(ready / approval / tap / imu)
        或 ack 名(ping / set_state / ...);'*' 表示所有消息。"""
        with self._handlers_lock:
            self._handlers.setdefault(event_name, []).append(callback)

    # ---------- 同步便利 facade ----------

    def wait_for(
        self,
        predicate: Callable[[dict], bool],
        timeout: Optional[float] = None,
    ) -> Optional[dict]:
        """阻塞**调用线程**,直到队列里出现匹配 predicate 的消息;
        超时返回 None。不阻塞 reader 线程。"""
        deadline = (time.monotonic() + timeout) if timeout is not None else None
        while True:
            remaining = (deadline - time.monotonic()) if deadline is not None else None
            if remaining is not None and remaining <= 0:
                return None
            try:
                msg = self._event_queue.get(timeout=remaining)
            except queue.Empty:
                return None
            if predicate(msg):
                return msg
            # 不匹配的消息丢弃 —— 真正需要订阅多种消息时请用 on() 注册回调

    def wait_for_approval(
        self,
        prompt_id: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Optional[str]:
        """阻塞调用线程等待 approval 事件;返回 'yes'/'no',超时返回 None。"""
        def match(m: dict) -> bool:
            if m.get("evt") != "approval":
                return False
            return prompt_id is None or m.get("id") == prompt_id

        msg = self.wait_for(match, timeout)
        return msg.get("decision") if msg else None

    # ---------- 内部:reader 线程 + 分发 ----------

    def _reader_loop(self) -> None:
        if self.mock:
            # mock 模式下没有真串口可读,只等 stop
            self._stop_event.wait()
            return

        assert self._serial is not None
        buf = b""
        while not self._stop_event.is_set():
            try:
                chunk = self._serial.read(READ_CHUNK_BYTES)
            except Exception:
                logger.exception("串口读异常")
                time.sleep(0.5)
                continue
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self._handle_line(line.decode("utf-8", errors="replace"))

    def _handle_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            logger.warning("非法 JSON 帧已丢弃: %r", line)
            return
        if not isinstance(msg, dict):
            logger.warning("非对象 JSON 帧已丢弃: %r", msg)
            return
        self._dispatch(msg)

    def _dispatch(self, msg: dict) -> None:
        # 设备 → Mac 入站可见性 log
        evt = msg.get("evt")
        ack = msg.get("ack")
        ok = msg.get("ok")
        if evt:
            logger.info("← recv evt=%s %s", evt, json.dumps({k: v for k, v in msg.items() if k != "evt"}, ensure_ascii=False))
        elif ack:
            if ok is False:
                logger.warning("← recv ack=%s FAIL %s", ack, msg.get("error", ""))
            elif ack in self._LOG_INFO_CMDS:
                logger.info("← recv ack=%s ok", ack)
            else:
                logger.debug("← recv ack=%s ok", ack)

        # 先入队(给 sync facade),再调回调(异步订阅者)
        try:
            self._event_queue.put_nowait(msg)
        except queue.Full:
            # 没人在 wait_for 时事件会堆积,丢最旧的避免占内存
            try:
                self._event_queue.get_nowait()
                self._event_queue.put_nowait(msg)
            except queue.Empty:
                pass

        key = msg.get("evt") or msg.get("ack")
        with self._handlers_lock:
            handlers = list(self._handlers.get(key, [])) if key else []
            handlers.extend(self._handlers.get("*", []))
        for h in handlers:
            try:
                h(msg)
            except Exception:
                logger.exception("回调处理异常 key=%s", key)

    # ---------- mock helpers ----------

    def _mock_response(self, sent: dict) -> None:
        """模拟设备响应:每条 cmd 回 ack ok=true,seq 回传。"""
        cmd = sent.get("cmd")
        if not cmd:
            return
        ack: dict = {"ack": cmd, "ok": True}
        if "seq" in sent:
            ack["seq"] = sent["seq"]
        self._dispatch(ack)
