"""加载并播放手动录制的关键帧 JSON（record_keyframe.py 的产物）。

两种用法：
  1. 函数：load_and_play(agent, "wake_me_up")   # 给编排脚本用
  2. CLI ：uv run python -m lelamp.motion.play_keyframes --name wake_me_up
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from typing import Optional

from lelamp.motion.motion_agent import MotionAgent
from lelamp.utils import find_serial_port

logger = logging.getLogger(__name__)

RECORDINGS_DIR = os.path.join(os.path.dirname(__file__), "..", "recordings")


def load_keyframes(name: str) -> dict:
    path = os.path.join(RECORDINGS_DIR, f"{name}.json")
    if not os.path.exists(path):
        raise FileNotFoundError(f"关键帧文件不存在: {path}")
    with open(path) as f:
        return json.load(f)


def load_and_play(agent: MotionAgent, name: str) -> Optional[str]:
    """同步入队，立即返回。返回错误字符串或 None。"""
    data = load_keyframes(name)
    return agent.play_keyframes(
        data.get("segments", []),
        intent=data.get("intent") or name,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--port", default=None, help="不指定则自动查找")
    parser.add_argument("--id",   default="lelamp")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    port = args.port or find_serial_port()
    print(f"串口: {port}")

    agent = MotionAgent(port=port, lamp_id=args.id)
    agent.start()
    try:
        err = load_and_play(agent, args.name)
        if err:
            print(f"播放失败: {err}")
            return
        while agent.is_playing():
            time.sleep(0.1)
        time.sleep(1.0)  # 多留一拍让末帧稳定
    finally:
        agent.stop()


if __name__ == "__main__":
    main()
