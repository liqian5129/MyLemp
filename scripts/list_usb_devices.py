"""列出当前接到 Mac 的所有串口设备,标出谁是显示设备(ESP32-S3)、谁是舵机总线。

背景:本项目同时挂两个串口外设
  - 显示设备:Waveshare ESP32-S3,VID 0x303A(原生 USB CDC)
  - 舵机总线:Feetech 控制板,通常通过 USB-UART 桥(CH340 / CP210x / FTDI)

两者在 macOS 上都可能呈现为 /dev/cu.usbmodem* 或 /dev/cu.usbserial-*,
现有 `lelamp.utils.find_serial_port()` 只匹配 `cu.usbmodem*` 取第一个,
**有撞到显示设备的风险**。本脚本帮忙诊断。

用法:
  uv run python scripts/list_usb_devices.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from serial.tools import list_ports  # noqa: E402

ESP32S3_VID = 0x303A  # Espressif Systems(原生 USB CDC)

# 常见 USB-UART 桥芯片 → 舵机总线候选
KNOWN_BRIDGE_VIDS = {
    0x1A86: "CH340 / CH341",
    0x10C4: "Silicon Labs CP210x",
    0x0403: "FTDI",
    0x067B: "Prolific PL2303",
    0x2341: "Arduino LLC",
    0x16C0: "Teensyduino / Van Ooijen Tech",
}


def fmt_vid_pid(vid, pid) -> str:
    if vid is None:
        return "(未知)"
    return f"0x{vid:04X}:0x{pid:04X}"


def main() -> int:
    ports = list(list_ports.comports())
    if not ports:
        print("⚠ 没找到任何串口设备")
        print("  检查:USB 线插好?板子供电?显示固件启用 ARDUINO_USB_CDC_ON_BOOT=1?")
        return 1

    print(f"发现 {len(ports)} 个串口设备:\n")

    display_ports = []
    bridge_ports = []
    other_ports = []

    for p in ports:
        marker = ""
        if p.vid == ESP32S3_VID:
            marker = "← 显示设备(ESP32-S3)"
            display_ports.append(p)
        elif p.vid in KNOWN_BRIDGE_VIDS:
            marker = f"← USB-UART 桥({KNOWN_BRIDGE_VIDS[p.vid]}),疑似舵机总线"
            bridge_ports.append(p)
        else:
            other_ports.append(p)

        print(f"  {p.device}")
        print(f"    VID:PID = {fmt_vid_pid(p.vid, p.pid)}")
        if p.manufacturer:
            print(f"    厂商    = {p.manufacturer}")
        if p.product:
            print(f"    产品    = {p.product}")
        if p.description and p.description != "n/a":
            print(f"    描述    = {p.description}")
        if p.serial_number:
            print(f"    序列号  = {p.serial_number}")
        if marker:
            print(f"    >>> {marker}")
        print()

    print("=" * 60)
    print("总结")
    print("=" * 60)

    # 显示设备
    if not display_ports:
        print("❌ 未识别到 ESP32-S3 显示设备(VID 0x303A)")
        print("   - 板子未插 / 没供电 / 固件未启用 ARDUINO_USB_CDC_ON_BOOT=1")
    elif len(display_ports) == 1:
        print(f"✓ 显示设备 = {display_ports[0].device}")
        print("  → DisplayController 用 find_display_port() 会自动选中它")
    else:
        print(f"⚠ 多个 ESP32-S3 候选({len(display_ports)} 个):")
        for p in display_ports:
            print(f"    - {p.device}")
        print("  find_display_port() 默认取第一个;如不对请用 --port 显式指定")

    # 舵机总线
    if bridge_ports:
        if len(bridge_ports) == 1:
            print(f"✓ 舵机总线(疑似) = {bridge_ports[0].device}")
        else:
            print(f"⚠ 多个 USB-UART 桥({len(bridge_ports)} 个):")
            for p in bridge_ports:
                print(f"    - {p.device}  ({KNOWN_BRIDGE_VIDS.get(p.vid, '?')})")
    else:
        print("? 未识别到常见 USB-UART 桥(可能舵机用的不是常见芯片,或没接)")

    # 跟现有 find_serial_port 对比
    print()
    print("-" * 60)
    print("现有 lelamp.utils.find_serial_port() 行为对比")
    print("-" * 60)
    try:
        from lelamp.utils import find_serial_port
        legacy = find_serial_port()
        print(f"  find_serial_port() → {legacy}")

        display_devices = [p.device for p in display_ports]
        if legacy in display_devices:
            print()
            print("  ⚠⚠⚠ 警告:它选中了显示设备!⚠⚠⚠")
            print("       如果用它跑舵机驱动,会把舵机指令发到显示设备 → 不会动 + 报错")
            print("       建议:")
            print("       - 跑舵机前先 unplug 显示设备,或")
            print("       - 给舵机驱动也加 VID 过滤(类似 find_display_port 的做法)")
        elif bridge_ports and legacy in [p.device for p in bridge_ports]:
            print("  ✓ 选中了 USB-UART 桥(舵机总线方向),正常")
        else:
            print(f"  ? 选中了未分类设备 {legacy},检查它是不是真的舵机总线")
    except Exception as e:
        print(f"  find_serial_port() 调用失败: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
