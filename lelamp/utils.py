import glob
import subprocess
import sys


def find_serial_port() -> str:
    """Auto-detect USB serial port based on platform."""
    if sys.platform == "darwin":
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
