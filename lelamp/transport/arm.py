"""ArmController — 跟 DisplayController 对称的状态化臂控制器。

设计原则:
  - **薄包装**:绝大多数逻辑委托给现有 `MotionAgent`(线程、串口、速度审计、帧插值都在它那)
  - **fire-and-forget**:`set_state` 入队即返回,不阻塞调用线程
  - **状态映射**:state → buddy_motions 生成器函数 → segment list → motion_agent.play_keyframes
  - **mock 模式**:不连舵机,只 log 路由

舵机速度安全:链路一路走到 motion_agent._velocity_audit,会在帧速过快时拉伸时间轴。
本类无需关心速度限制,只要 buddy_motions 给的 duration 不太离谱即可。
"""
from __future__ import annotations

import logging
from typing import Optional

from lelamp.transport.buddy_motions import STATE_TO_GENERATOR

logger = logging.getLogger(__name__)

ESP32S3_DISPLAY_VID = 0x303A  # 跟 display.py 同源,本文件专做"排除"


def find_arm_port() -> str:
    """找舵机总线端口。

    跟现有 `lelamp.utils.find_serial_port()` 的区别:
      - 后者只 glob `cu.usbmodem*` 取第一个,会跟显示设备(也是 cu.usbmodem*)撞车
      - 本函数按 VID **排除**显示设备(VID 0x303A),返回剩下的第一个 USB 串口

    用排除而非白名单 → 舵机将来换 USB-UART 桥芯片(CH340 → CP210x → FTDI)
    也不需要改这里。
    """
    try:
        from serial.tools import list_ports
    except ImportError as e:
        raise RuntimeError("pyserial 未安装") from e

    candidates = [
        p for p in list_ports.comports()
        if p.vid is not None and p.vid != ESP32S3_DISPLAY_VID
    ]
    if not candidates:
        raise RuntimeError(
            f"找不到舵机总线设备(已排除显示设备 VID 0x{ESP32S3_DISPLAY_VID:04X})。"
            "请确认舵机 USB 已接 Mac。"
        )
    if len(candidates) > 1:
        logger.warning(
            "发现多个非显示设备 USB 串口,取第一个: %s。其他: %s",
            candidates[0].device,
            [p.device for p in candidates[1:]],
        )
    return candidates[0].device


class ArmController:
    """臂的 buddy 状态接口。

    用法(真硬件):
        arm = ArmController(port="/dev/cu.usbmodemXXXX", lamp_id="lelamp")
        with arm:
            arm.set_state("busy")
            time.sleep(2)
            arm.set_state("attention")

    用法(mock,无硬件 dry-run):
        arm = ArmController(mock=True)
        with arm:
            arm.set_state("celebrate")
    """

    def __init__(
        self,
        port: Optional[str] = None,
        lamp_id: str = "lelamp",
        mock: bool = False,
    ):
        if not mock and port is None:
            raise ValueError("非 mock 模式必须提供 port")
        self.port = port
        self.lamp_id = lamp_id
        self.mock = mock
        self._agent: Optional[object] = None  # MotionAgent,延迟 import 避免 mock 模式拉舵机依赖
        self._current_state: Optional[str] = None

    # ---------- lifecycle ----------

    def start(self) -> None:
        if self.mock:
            logger.info("[mock] ArmController 启动")
            return
        from lelamp.motion.motion_agent import MotionAgent
        self._agent = MotionAgent(port=self.port, lamp_id=self.lamp_id)
        self._agent.start()
        logger.info("ArmController 已连接 motion_agent  port=%s", self.port)

    def stop(self) -> None:
        if self._agent is not None:
            self._agent.stop()
            self._agent = None
        logger.info("ArmController 已停止")

    def __enter__(self) -> "ArmController":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---------- 状态切换(主 API) ----------

    def set_state(self, state: str) -> None:
        """切换状态对应的臂姿态。fire-and-forget,不阻塞调用线程。

        - 未知 state 仅 warn,不抛异常(跟 DisplayController 对称,容错优先)
        - 入队失败时记日志但不抛
        - 实际播放在 motion_agent 后台线程进行
        """
        gen = STATE_TO_GENERATOR.get(state)
        if gen is None:
            logger.warning("未知 arm state: %s,可用: %s", state, list(STATE_TO_GENERATOR))
            return
        segments = gen()

        if self.mock:
            logger.info("[mock] arm.set_state(%s) → %d segs", state, len(segments))
            self._current_state = state
            return

        if self._agent is None:
            raise RuntimeError("ArmController 未 start")
        err = self._agent.play_keyframes(segments, intent=f"buddy_{state}")
        if err:
            logger.warning("set_state(%s) 入队失败: %s", state, err)
            return
        self._current_state = state

    # ---------- 直接动作(绕过状态映射) ----------

    def play_segments(self, segments: list, intent: str = "") -> Optional[str]:
        """直接传 segment list,用于 demo 内的自定义动作或调试。

        返回错误字符串(同 motion_agent.play_keyframes)或 None。
        """
        if self.mock:
            logger.info("[mock] arm.play_segments(%d segs, intent=%s)", len(segments), intent)
            return None
        if self._agent is None:
            raise RuntimeError("ArmController 未 start")
        return self._agent.play_keyframes(segments, intent=intent)

    # ---------- 状态查询 ----------

    def is_busy(self) -> bool:
        """是否正在播放(任何动作未走完即 True)"""
        if self.mock or self._agent is None:
            return False
        return self._agent.is_playing()

    @property
    def current_state(self) -> Optional[str]:
        return self._current_state
