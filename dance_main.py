"""
台灯舞蹈系统入口

使用方式：
    uv run python dance_main.py song.mp3
    uv run python dance_main.py song.mp3 --groove laid_back
    uv run python dance_main.py song.mp3 --no-osc          # 关闭基础律动
    uv run python dance_main.py song.mp3 --prep 0.10       # 调整舵机响应补偿

依赖（首次安装）：
    uv add librosa soundfile

调试 PREP_TIME：
    录制视频后用 ffmpeg 提取帧，比对动作极值帧与节拍 click 的时间差，
    将差值加到 --prep 参数上。
"""
from __future__ import annotations

import argparse
import logging
import time
import os
from dataclasses import replace

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def run_dance(
    audio_path: str,
    groove_style: str = "tight",
    enable_osc: bool  = True,
    prep_time: float  = 0.12,
) -> None:
    import sounddevice as sd
    import soundfile as sf

    from lelamp.dance.music_analysis import OfflineMusicAnalysis
    from lelamp.dance.primitives import batch_generate_library
    from lelamp.dance.choreography import (
        ChoreographyStateMachine,
        MotionSelector,
        MotionScheduler,
        PREP_TIME,
    )
    from lelamp.dance.dance_executor import DanceExecutor
    from lelamp.dance.groove import apply_groove
    from lelamp.utils import find_serial_port

    # ── 覆盖全局 PREP_TIME ────────────────────────────────────────────────────
    import lelamp.dance.choreography as _choreo_mod
    _choreo_mod.PREP_TIME = prep_time

    # ── 音乐分析 ──────────────────────────────────────────────────────────────
    analysis = OfflineMusicAnalysis(audio_path)

    # ── 原语库 ────────────────────────────────────────────────────────────────
    library = batch_generate_library()
    logger.info("原语库: %d 条原语", len(library))

    # ── 机器人连接 ────────────────────────────────────────────────────────────
    port = find_serial_port()
    logger.info("🔌 串口: %s", port)

    from lelamp.follower import LeLampFollowerConfig, LeLampFollower
    config = LeLampFollowerConfig(port=port, id="lelamp")
    robot  = LeLampFollower(config)
    robot.connect(calibrate=False)

    # ── 舞蹈组件 ──────────────────────────────────────────────────────────────
    state_machine = ChoreographyStateMachine()
    selector      = MotionSelector(library)
    scheduler     = MotionScheduler()
    executor      = DanceExecutor(
        robot,
        dt=0.01,
        bpm=analysis.tempo,
        groove_style=groove_style,
    )
    executor.oscillation.enabled = enable_osc

    # ── 开始播放 ──────────────────────────────────────────────────────────────
    data, sr = sf.read(audio_path)
    play_start = time.time()
    sd.play(data, samplerate=sr)

    logger.info(
        "🕺 开始跳舞!  BPM=%.1f  groove=%s  prep=%.0fms  osc=%s  — Ctrl+C 停止",
        analysis.tempo, groove_style, prep_time * 1000, "on" if enable_osc else "off",
    )

    last_beat_idx = -1

    try:
        while True:
            now       = time.time()
            play_time = now - play_start

            info = analysis.get_beat_info(play_time)
            if info is None:
                logger.info("🎵 歌曲结束")
                break

            beat_idx = analysis._current_idx

            # ── 每个新节拍触发一次编舞决策 ────────────────────────────────────
            if beat_idx != last_beat_idx:
                last_beat_idx = beat_idx

                state = state_machine.tick(
                    info["bar_position"],
                    info["energy"],
                    info["section_changed"],
                )
                prim = selector.select(state, info["bar_position"], info["energy"])

                # Groove 微时值扰动
                grooved_times = apply_groove(list(prim.frame_times), groove_style)
                grooved_prim  = replace(prim, frame_times=grooved_times)

                scheduler.schedule(
                    beat_time=info["beat_time"],
                    beat_dur=info["beat_period"],
                    primitive=grooved_prim,
                )

                logger.debug(
                    "拍 %d | bar=%d | energy=%.2f | state=%s | → %s",
                    beat_idx, info["bar_position"], info["energy"],
                    state.value, prim.id,
                )

            # ── 执行到时的动作 ────────────────────────────────────────────────
            for ready_move in scheduler.pop_ready():
                executor.start_move(ready_move)

            # ── 控制步进（100 Hz）────────────────────────────────────────────
            executor.step()
            time.sleep(0.01)

    except KeyboardInterrupt:
        logger.info("👋 停止")
    finally:
        sd.stop()
        try:
            robot.disconnect()
        except Exception:
            pass


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="台灯机器人跳舞系统",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  uv run python dance_main.py song.mp3
  uv run python dance_main.py song.mp3 --groove laid_back
  uv run python dance_main.py song.mp3 --prep 0.10 --no-osc
        """,
    )
    parser.add_argument("audio", help="音频文件路径（.mp3 / .wav / .flac）")
    parser.add_argument(
        "--groove", default="tight",
        choices=["tight", "laid_back", "neutral"],
        help="律动风格：tight=街舞, laid_back=R&B, neutral=随机  (default: tight)",
    )
    parser.add_argument(
        "--prep", type=float, default=0.12,
        help="舵机响应提前量（秒），实测校准后调整  (default: 0.12)",
    )
    parser.add_argument(
        "--no-osc", action="store_true",
        help="关闭基础律动层（用于调试卡点精度）",
    )

    args = parser.parse_args()
    run_dance(
        audio_path=args.audio,
        groove_style=args.groove,
        enable_osc=not args.no_osc,
        prep_time=args.prep,
    )
