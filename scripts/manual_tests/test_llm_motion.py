"""
LLM 运动路径测试工具

固定 f0，逐个测试 LLM 为每种情绪生成的关键帧，直接在真实机器人上播放。

用法:
    uv run python test_llm_motion.py
    uv run python test_llm_motion.py --emotion happy
    uv run python test_llm_motion.py --emotion curious --intensity 0.6
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

from lelamp.motion.llm_elegnt_service import LLMELEGNTService
from lelamp.motion.llm_generator import LLMGenerator
from lelamp.motion.motion_executor import MotionExecutor, Q_REST, JOINT_NAMES
from lelamp.motion.motion_service import create_motion_service
from lelamp.motion.templates import EMOTION_INDEX
from lelamp.utils import find_serial_port

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

GOOD_EXAMPLES_FILE = Path("llm_good_examples.json")


def save_good_example(emotion: str, intensity: float, f0, kf):
    examples = []
    if GOOD_EXAMPLES_FILE.exists():
        examples = json.loads(GOOD_EXAMPLES_FILE.read_text())
    examples.append({
        "emotion": emotion,
        "intensity": intensity,
        "f0": [round(float(v), 1) for v in f0],
        "f1": [round(float(v), 1) for v in kf.f1],
        "f2": [round(float(v), 1) for v in kf.f2],
        "f3": [round(float(v), 1) for v in kf.f3],
        "duration": round(kf.duration, 2),
        "accel_ratio": round(kf.accel_ratio, 2),
        "asymmetry": round(kf.asymmetry, 2),
        "intent": kf.intent,
    })
    GOOD_EXAMPLES_FILE.write_text(json.dumps(examples, ensure_ascii=False, indent=2))
    logger.info("✅ 已保存到 %s（共 %d 条）", GOOD_EXAMPLES_FILE, len(examples))


def play_kf(svc: LLMELEGNTService, kf, f0):
    """直接注入关键帧播放，不走 hint"""
    executor = MotionExecutor.from_frames(
        f0=f0, f1=kf.f1, f2=kf.f2, f3=kf.f3,
        duration=kf.duration,
        accel_ratio=kf.accel_ratio,
        asymmetry=kf.asymmetry,
    )
    with svc._lock:
        svc._executor_queue.clear()
        svc._pending_ready = None
        svc._pending_kf = None
        svc._executor = executor
        svc._exec_start = time.perf_counter()
        svc._mode = "playing"
    time.sleep(kf.duration + 1.5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--emotion", default=None)
    parser.add_argument("--intensity", type=float, default=0.8)
    args = parser.parse_args()

    port = find_serial_port()
    generator = LLMGenerator(api_key=os.environ.get("KIMI_API_KEY", ""))
    mot_svc = create_motion_service(use_llm=False, warmup=False)
    svc = LLMELEGNTService(port=port, lamp_id="lelamp", fps=30,
                            motion_service=mot_svc, generator=generator)
    svc.start()
    time.sleep(1.0)

    emotions = [args.emotion] if args.emotion else EMOTION_INDEX

    try:
        for emotion in emotions:
            print(f"\n{'='*60}")
            print(f"情绪: {emotion}  intensity={args.intensity}")

            while True:
                f0 = svc._get_current_q()
                print(f"\n当前 f0: {[round(float(v),1) for v in f0]}")
                print("生成中...")

                kf = asyncio.run(
                    generator.generate(emotion, args.intensity, f0=f0)
                )

                if kf is None:
                    print("❌ LLM 生成失败")
                    cmd = input("r=重试，s=跳过，q=退出: ").strip().lower()
                    if cmd == "q": return
                    if cmd == "s": break
                    continue

                print(f"f1={[round(float(v),1) for v in kf.f1]}")
                print(f"f2={[round(float(v),1) for v in kf.f2]}")
                print(f"f3={[round(float(v),1) for v in kf.f3]}")
                print(f"duration={kf.duration:.2f}s  accel={kf.accel_ratio:.2f}  asym={kf.asymmetry:.2f}")

                cmd = input("播放? 回车=播放，r=重新生成，s=跳过，q=退出: ").strip().lower()
                if cmd == "q": return
                if cmd == "s": break
                if cmd == "r": continue

                play_kf(svc, kf, f0)

                rating = input("评分: g=好（保存），b=差，r=重新生成，s=下一个情绪: ").strip().lower()
                if rating == "q": return
                if rating == "g":
                    save_good_example(emotion, args.intensity, f0, kf)
                    break
                if rating == "s": break
                # r 或 b 都重新生成

    except KeyboardInterrupt:
        pass
    finally:
        svc.stop()
        print("\n已停止")


if __name__ == "__main__":
    main()
