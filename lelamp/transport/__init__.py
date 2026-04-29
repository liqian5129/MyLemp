"""Mac↔设备 USB CDC 通信层 + 臂状态化控制。

DisplayController:跟 Waveshare ESP32-S3 显示设备的 JSON 协议客户端。
ArmController:基于 motion_agent 的状态化臂控制器,跟 Display 对称。
"""
from lelamp.transport.arm import ArmController, find_arm_port
from lelamp.transport.display import DisplayController, find_display_port

__all__ = [
    "ArmController",
    "DisplayController",
    "find_arm_port",
    "find_display_port",
]
