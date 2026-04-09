"""
实时跳舞模式：麦克风拾音 → 实时节拍检测 → 跳舞

使用方式：
    uv run python dance_live.py
    uv run python dance_live.py --groove laid_back
    uv run python dance_live.py --prep 0.10
    uv run python dance_live.py --no-osc
    uv run python dance_live.py --device 1         # 指定音频输入设备

列出可用音频设备：
    python -c "import sounddevice; print(sounddevice.query_devices())"
"""
from __future__ import annotations

import argparse
import logging
import signal
import time

import numpy as np
import sounddevice as sd

from dotenv import load_dotenv

load_dotenv()

import os, datetime as _dt

_log_dir = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(_log_dir, exist_ok=True)
_log_file = os.path.join(
    _log_dir,
    f"dance_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(_log_file),
    ],
)
logger = logging.getLogger(__name__)
logger.info("📝 日志文件: %s", _log_file)

# ── 常量 ─────────────────────────────────────────────────────────────────────

SAMPLE_RATE = 44100
HOP_SIZE    = 1024     # ~23ms per chunk


def run_live_dance(
    groove_style: str  = "tight",
    enable_osc: bool   = True,
    prep_time: float   = 0.12,
    device: int | None = None,
) -> None:
    from dataclasses import replace

    from lelamp.dance.realtime_beat import (
        RealtimeBeatTracker,
        RealtimeEnergyAnalyzer,
        RealtimeSectionDetector,
    )
    from lelamp.dance.primitives import batch_generate_library
    from lelamp.dance.choreography import (
        ChoreographyStateMachine,
        MotionSelector,
        MotionScheduler,
    )
    from lelamp.dance.dance_executor import DanceExecutor
    from lelamp.dance.groove import apply_groove
    from lelamp.utils import find_serial_port

    # ── 覆盖 PREP_TIME ──────────────────────────────────────────────────────
    import lelamp.dance.choreography as _choreo_mod
    _choreo_mod.PREP_TIME = prep_time

    # ── 实时分析组件 ─────────────────────────────────────────────────────────
    beat_tracker = RealtimeBeatTracker(sr=SAMPLE_RATE, hop_size=HOP_SIZE)
    energy_anal  = RealtimeEnergyAnalyzer()
    section_det  = RealtimeSectionDetector()

    # ── 原语库 ───────────────────────────────────────────────────────────────
    library = batch_generate_library()
    logger.info("原语库: %d 条原语", len(library))

    # ── 机器人连接 ───────────────────────────────────────────────────────────
    port = find_serial_port()
    logger.info("🔌 串口: %s", port)

    from lelamp.follower import LeLampFollowerConfig, LeLampFollower
    config = LeLampFollowerConfig(port=port, id="lelamp")
    robot  = LeLampFollower(config)
    robot.connect(calibrate=False)

    # ── 舞蹈组件 ─────────────────────────────────────────────────────────────
    state_machine = ChoreographyStateMachine()
    selector      = MotionSelector(library)
    scheduler     = MotionScheduler()
    executor      = DanceExecutor(
        robot,
        dt=0.01,
        bpm=beat_tracker.bpm,
        groove_style=groove_style,
    )
    # 启动时关闭律动，等节拍锁定后再开
    executor.oscillation.enabled = False
    _user_wants_osc = enable_osc

    # ── 音频回调 ─────────────────────────────────────────────────────────────
    # sounddevice 回调在独立线程中运行，用共享列表传递节拍事件
    beat_events: list[dict] = []

    def audio_callback(indata: np.ndarray, frames: int, time_info, status):
        if status:
            logger.debug("audio status: %s", status)

        chunk = indata[:, 0].astype(np.float32)  # 取单声道

        # 节拍检测
        beats = beat_tracker.process_chunk(chunk)

        # 能量 & 段落
        energy          = energy_anal.process(chunk)
        section_changed = section_det.process(energy)

        for bt in beats:
            beat_events.append({
                "beat_time":       bt,
                "beat_period":     beat_tracker.beat_period,
                "bar_position":    beat_tracker.bar_position(),
                "energy":          energy,
                "section_changed": section_changed,
            })

    # ── 打开麦克风流 ─────────────────────────────────────────────────────────
    stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        blocksize=HOP_SIZE,
        channels=1,
        dtype="float32",
        device=device,
        callback=audio_callback,
    )

    running = True
    def handle_signal(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, handle_signal)

    logger.info(
        "🎤 实时跳舞模式启动!  groove=%s  prep=%.0fms  osc=%s  — Ctrl+C 停止",
        groove_style, prep_time * 1000, "on" if enable_osc else "off",
    )
    logger.info("🎵 开始播放音乐吧！")

    stream.start()
    last_bpm_log = 0.0

    was_active = False

    try:
        while running:
            # ── 音乐状态切换 ─────────────────────────────────────────────
            if beat_tracker.music_active and not was_active:
                # 节拍锁定 → 启动律动
                if _user_wants_osc:
                    executor.oscillation.enabled = True
                was_active = True
            elif not beat_tracker.music_active and was_active:
                # 音乐停止 → 停止一切运动
                executor.oscillation.enabled = False
                executor.active_move = None
                scheduler.queue.clear()
                was_active = False

            # ── 处理节拍事件 ──────────────────────────────────────────────
            while beat_events:
                info = beat_events.pop(0)

                executor.update_bpm(beat_tracker.bpm)

                state = state_machine.tick(
                    info["bar_position"],
                    info["energy"],
                    info["section_changed"],
                )
                prim = selector.select(state, info["bar_position"], info["energy"])

                # Groove 微时值
                grooved_times = apply_groove(list(prim.frame_times), groove_style)
                grooved_prim  = replace(prim, frame_times=grooved_times)

                scheduler.schedule(
                    beat_time=info["beat_time"],
                    beat_dur=info["beat_period"],
                    primitive=grooved_prim,
                )

                logger.info(
                    "🥁 拍 %d | bar=%d | energy=%.2f | BPM=%.0f | %s → %s",
                    beat_tracker.beat_counter,
                    info["bar_position"],
                    info["energy"],
                    beat_tracker.bpm,
                    state.value,
                    prim.id,
                )

            # ── 执行到时的动作 ───────────────────────────────────────────
            for ready_move in scheduler.pop_ready():
                executor.start_move(ready_move)

            # ── 控制步进（仅音乐活跃时）─────────────────────────────────
            if was_active:
                executor.step()

            # ── 定期输出状态 ─────────────────────────────────────────────
            now = time.time()
            if now - last_bpm_log > 5.0:
                last_bpm_log = now
                if beat_tracker.music_active:
                    logger.info(
                        "📊 BPM=%.0f  beats=%d  real=%d",
                        beat_tracker.bpm, beat_tracker.beat_counter,
                        beat_tracker._real_beat_count,
                    )
                else:
                    logger.info("🔇 等待音乐...")


            time.sleep(0.01)

    finally:
        stream.stop()
        stream.close()
        logger.info("👋 停止")
        try:
            robot.disconnect()
        except Exception:
            pass


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="台灯机器人实时跳舞（麦克风拾音）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  uv run python dance_live.py                          # 默认麦克风
  uv run python dance_live.py --groove laid_back        # R&B 风格
  uv run python dance_live.py --device 1                # 指定音频设备

列出音频设备：
  python -c "import sounddevice; print(sounddevice.query_devices())"
        """,
    )
    parser.add_argument(
        "--groove", default="tight",
        choices=["tight", "laid_back", "neutral"],
        help="律动风格 (default: tight)",
    )
    parser.add_argument(
        "--prep", type=float, default=0.12,
        help="舵机响应提前量（秒）(default: 0.12)",
    )
    parser.add_argument(
        "--no-osc", action="store_true",
        help="关闭基础律动层",
    )
    parser.add_argument(
        "--device", type=int, default=None,
        help="音频输入设备 ID（用 sounddevice.query_devices() 查看）",
    )

    args = parser.parse_args()
    run_live_dance(
        groove_style=args.groove,
        enable_osc=not args.no_osc,
        prep_time=args.prep,
        device=args.device,
    )
