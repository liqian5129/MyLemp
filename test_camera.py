"""
摄像头测试脚本

实时显示摄像头画面，叠加 MSE 数值和触发状态，方便调试。

用法:
    uv run python test_camera.py
    uv run python test_camera.py --device 1
    uv run python test_camera.py --device 1 --threshold 0.002
"""
from __future__ import annotations

import argparse
import os
import time

import cv2
import numpy as np
from dotenv import load_dotenv

load_dotenv()

DEFAULT_THRESHOLD = 0.002


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", type=int,
                        default=int(os.environ.get("CAMERA_DEVICE", "0")),
                        help="摄像头设备号（默认读 .env CAMERA_DEVICE，否则 0）")
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                        help=f"MSE 触发阈值（默认 {DEFAULT_THRESHOLD}）")
    parser.add_argument("--flip", action="store_true",
                        default=os.environ.get("CAMERA_FLIP", "").lower() in ("1", "true", "yes"),
                        help="上下+左右翻转（默认读 .env CAMERA_FLIP）")
    args = parser.parse_args()
    print(f"   .env: CAMERA_DEVICE={os.environ.get('CAMERA_DEVICE', '未设置')}  "
          f"CAMERA_FLIP={os.environ.get('CAMERA_FLIP', '未设置')}")

    cap = cv2.VideoCapture(args.device)
    if not cap.isOpened():
        print(f"❌ 无法打开摄像头 device={args.device}")
        return

    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print(f"✅ 摄像头已打开 device={args.device}  {w}x{h}")
    print("   在摄像头前走动，观察 MSE 变化。按 q 退出。")

    prev_gray = None
    mse = 0.0
    trigger_count = 0
    triggered = False
    trigger_flash = 0   # 触发后高亮帧数

    while True:
        ret, frame = cap.read()
        if not ret:
            print("⚠️  读取失败")
            break

        if args.flip:
            frame = cv2.flip(frame, -1)

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype("float32") / 255.0

        if prev_gray is not None:
            mse = float(np.mean((gray - prev_gray) ** 2))
            triggered = mse > args.threshold
            if triggered:
                trigger_count += 1
                trigger_flash = 10   # 高亮 10 帧

        prev_gray = gray

        # ── 叠加信息 ──────────────────────────────────────────────────────────
        display = frame.copy()

        # 触发时红色边框
        if trigger_flash > 0:
            cv2.rectangle(display, (0, 0), (w - 1, h - 1), (0, 0, 255), 12)
            trigger_flash -= 1

        # MSE 数值
        mse_color = (0, 0, 255) if triggered else (0, 255, 0)
        cv2.putText(display, f"MSE: {mse:.5f}", (20, 50),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.2, mse_color, 2)

        # 阈值线提示
        cv2.putText(display, f"threshold: {args.threshold:.4f}", (20, 95),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (200, 200, 200), 1)

        # 触发次数
        cv2.putText(display, f"triggers: {trigger_count}", (20, 140),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 0), 2)

        # 触发大字
        if trigger_flash > 0:
            cv2.putText(display, "TRIGGERED!", (w // 2 - 150, h // 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 2.0, (0, 0, 255), 4)

        # 退出提示
        cv2.putText(display, "press q to quit", (20, h - 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (150, 150, 150), 1)

        cv2.imshow(f"Camera device={args.device}", display)

        if cv2.waitKey(33) & 0xFF == ord("q"):   # ~30fps 刷新
            break

    cap.release()
    cv2.destroyAllWindows()
    print(f"\n已退出。共触发 {trigger_count} 次。")


if __name__ == "__main__":
    main()
