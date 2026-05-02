"""
情绪模板调试工具

逐个播放每种情绪的每个变体，方便对着真实机器人校准关键帧。

用法:
    uv run python test_emotion_templates.py
    uv run python test_emotion_templates.py --emotion happy
    uv run python test_emotion_templates.py --emotion curious --variant 2 --intensity 0.8
"""
from __future__ import annotations

import argparse
import time
import logging

from lelamp.motion.llm_elegnt_service import LLMELEGNTService
from lelamp.motion.motion_service import create_motion_service
from lelamp.motion.templates import _MOTION_TEMPLATES, template_generate, EMOTION_INDEX
from lelamp.utils import find_serial_port

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def play_and_wait(svc: LLMELEGNTService, emotion: str, intensity: float, wait: float = 5.0):
    svc.dispatch("emotion", {"emotion": emotion, "intensity": intensity})
    time.sleep(wait)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--emotion", default=None, help="只测某种情绪，如 happy")
    parser.add_argument("--variant", type=int, default=None, help="只测第 N 个变体（从 1 开始）")
    parser.add_argument("--intensity", type=float, default=0.8)
    parser.add_argument("--repeat", type=int, default=1, help="每个变体重复几次")
    args = parser.parse_args()

    port = find_serial_port()
    logger.info("串口: %s", port)

    mot_svc = create_motion_service(use_llm=False, warmup=False)
    svc = LLMELEGNTService(port=port, lamp_id="lelamp", fps=30, motion_service=mot_svc)
    svc.start()
    time.sleep(1.0)   # 等待 idle 稳定

    emotions = [args.emotion] if args.emotion else EMOTION_INDEX

    try:
        for emotion in emotions:
            variants = _MOTION_TEMPLATES.get(emotion, [])
            indices = [args.variant - 1] if args.variant else range(len(variants))

            for i in indices:
                v = variants[i]
                print(f"\n{'='*60}")
                print(f"情绪: {emotion}  变体 {i+1}/{len(variants)}  intensity={args.intensity}")
                print(f"intent: {v.get('intent', '')}")
                print(f"f1: {v['f1']}")
                print(f"f2: {v['f2']}")
                print(f"timing: {v['timing']}")

                for rep in range(args.repeat):
                    prompt = f"  [rep {rep+1}/{args.repeat}] 按回车播放，r=重播，s=跳过，q=退出: "
                    while True:
                        cmd = input(prompt).strip().lower()
                        if cmd == "q":
                            return
                        if cmd == "s":
                            break
                        # 强制选取指定变体（绕过随机选取）
                        import numpy as np
                        from lelamp.motion.motion_executor import Q_REST, Q_MIN, Q_MAX, N_JOINTS
                        from lelamp.motion.templates import MotionKeyframes

                        intensity = args.intensity
                        f1_base = np.array(v["f1"], dtype=np.float32)
                        f2_base = np.array(v["f2"], dtype=np.float32)
                        f1 = Q_REST + (f1_base - Q_REST) * intensity
                        f2 = Q_REST + (f2_base - Q_REST) * intensity
                        f1 = np.clip(f1, Q_MIN, Q_MAX)
                        f2 = np.clip(f2, Q_MIN, Q_MAX)
                        t = v["timing"]
                        duration = float(t["duration"] * (1.1 - 0.3 * intensity))
                        f3 = np.clip(Q_REST + (f2 - Q_REST) * 0.3, Q_MIN, Q_MAX)

                        kf = MotionKeyframes(
                            f1=f1, f2=f2, f3=f3,
                            duration=duration,
                            accel_ratio=t["accel_ratio"],
                            asymmetry=t["asymmetry"] * intensity,
                            intent=v.get("intent", ""),
                        )
                        print(f"    实际 f1={np.round(f1,1).tolist()}")
                        print(f"    实际 f2={np.round(f2,1).tolist()}")
                        print(f"    实际 f3={np.round(f3,1).tolist()}")
                        print(f"    duration={duration:.2f}s")

                        # 直接注入关键帧，跳过 hint，跳过随机噪声
                        from lelamp.motion.motion_executor import MotionExecutor
                        import threading
                        f0 = svc._get_current_q()
                        executor = MotionExecutor.from_frames(
                            f0=f0, f1=kf.f1, f2=kf.f2, f3=kf.f3,
                            duration=kf.duration,
                            accel_ratio=kf.accel_ratio,
                            asymmetry=kf.asymmetry,
                        )
                        with svc._lock:
                            svc._executor_queue.clear()
                            svc._executor = executor
                            svc._exec_start = time.perf_counter()
                            svc._mode = "playing"

                        print(f"    播放中（{duration:.1f}s）...")
                        time.sleep(duration + 1.5)   # 等动作完成

                        if cmd != "r":
                            break

    except KeyboardInterrupt:
        pass
    finally:
        svc.stop()
        print("\n已停止")


if __name__ == "__main__":
    main()
