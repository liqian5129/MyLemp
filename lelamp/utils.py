import glob
import subprocess
import sys


_ESP32S3_VID = 0x303A  # Espressif(屏 USB CDC),要从舵机匹配里排除


def find_serial_port() -> str:
    """Auto-detect 舵机 USB 串口。

    macOS:同时插着屏(ESP32-S3,VID 0x303A)和舵机时,glob 顺序不稳定。
    所以按 VID 排除 ESP32-S3,剩下的就是舵机。
    """
    if sys.platform == "darwin":
        # 优先按 VID 过滤(排除 ESP32 屏)
        try:
            from serial.tools import list_ports
            non_display = [
                p.device for p in list_ports.comports()
                if p.device.startswith("/dev/cu.usbmodem") and p.vid != _ESP32S3_VID
            ]
            if non_display:
                return non_display[0]
        except ImportError:
            pass
        # fallback:pyserial 没装时退回 glob(单设备场景仍能用)
        matches = glob.glob("/dev/cu.usbmodem*")
        if matches:
            return matches[0]
        print("WARNING: No /dev/cu.usbmodem* device found, falling back to /dev/cu.usbmodem1101")
        return "/dev/cu.usbmodem1101"
    return "/dev/ttyACM0"


def set_system_volume(volume_percent: int):
    """Set system audio volume (0-100). Handles macOS and Linux."""
    try:
        if sys.platform == "darwin":
            subprocess.run(
                ["osascript", "-e", f"set volume output volume {volume_percent}"],
                capture_output=True, text=True, timeout=5,
            )
        else:
            subprocess.run(["amixer", "sset", "Line", f"{volume_percent}%"], capture_output=True, text=True, timeout=5)
            subprocess.run(["amixer", "sset", "Line DAC", f"{volume_percent}%"], capture_output=True, text=True, timeout=5)
            subprocess.run(["amixer", "sset", "HP", f"{volume_percent}%"], capture_output=True, text=True, timeout=5)
    except Exception:
        pass
