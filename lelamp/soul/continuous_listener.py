"""
持续 VAD 语音监听（替代 PTT 按键录音）

设计要点：
  - 自适应噪底：启动时采集 2 秒背景，threshold = noise_floor × 3
  - AEC（回声消除）：直接轮询 tts.is_playing()，无需额外 flag
  - 合并窗口：silence_sec=2.0 秒静音才截断，避免中文停顿被误切
  - 后台线程：ASR 推理（~300-600ms）在独立线程中完成，不阻塞音频回调
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from enum import Enum, auto
from typing import Callable, Coroutine, Optional

import numpy as np
import sounddevice as sd

logger = logging.getLogger(__name__)

SAMPLE_RATE    = 16000
CHUNK_FRAMES   = 1600      # 100ms @ 16kHz
SILENCE_SEC    = 2.0       # 连续静音多久才截断一段
MAX_DURATION   = 30.0      # 单段最长秒数（防止无限积累）
MIN_DURATION   = 0.3       # 短于此的片段丢弃


class _VADState(Enum):
    IDLE       = auto()   # 没有语音
    COLLECTING = auto()   # 正在收集语音
    DRAINING   = auto()   # ASR 推理中，暂停接收


class ContinuousListener:
    """
    持续监听麦克风，自动分割语音段并调用 ASR。

    用法：
        listener = ContinuousListener(
            asr=asr, on_speech=agent.on_speech, tts=tts, loop=loop
        )
        listener.start()
        # ...
        listener.stop()
    """

    def __init__(
        self,
        asr,
        on_speech: Callable[[str], Coroutine],
        tts,
        loop: asyncio.AbstractEventLoop,
        silence_sec:     float = SILENCE_SEC,
        max_duration:    float = MAX_DURATION,
        min_duration:    float = MIN_DURATION,
        calibration_sec: float = 2.0,
    ):
        self._asr            = asr
        self._on_speech      = on_speech
        self._tts            = tts
        self._loop           = loop
        self._silence_sec    = silence_sec
        self._max_duration   = max_duration
        self._min_duration   = min_duration
        self._calibration_sec = calibration_sec

        self._state          = _VADState.IDLE
        self._threshold      = 0.030       # 初始默认值，校准后更新
        self._last_voice_t   = 0.0         # 最后一次检测到语音的时间
        self._collect_start  = 0.0         # 本段收集开始时间
        self._lock           = threading.Lock()

        self._stream: Optional[sd.InputStream] = None

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    def start(self):
        """先校准噪底，再开启持续监听流"""
        self._calibrate()
        self._stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype=np.float32,
            blocksize=CHUNK_FRAMES,
            callback=self._audio_callback,
        )
        self._stream.start()
        logger.info(
            "🎙️ ContinuousListener 启动 (threshold=%.4f, silence=%.1fs)",
            self._threshold, self._silence_sec,
        )

    def stop(self):
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        logger.info("🛑 ContinuousListener 已停止")

    # ── 内部：噪底校准 ────────────────────────────────────────────────────────

    def _calibrate(self):
        """采集静默背景，设置自适应阈值"""
        logger.info("🔇 采集环境噪底（%.1f 秒）...", self._calibration_sec)
        rms_samples: list[float] = []

        def _collect(indata, frames, time_info, status):
            rms_samples.append(float(np.sqrt(np.mean(indata ** 2))))

        with sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype=np.float32,
            blocksize=CHUNK_FRAMES, callback=_collect,
        ):
            time.sleep(self._calibration_sec)

        if rms_samples:
            noise_floor = float(np.mean(rms_samples))
            self._threshold = max(noise_floor * 5.0, 0.015)
            logger.info(
                "✅ 噪底=%.4f → 阈值=%.4f", noise_floor, self._threshold
            )
        else:
            logger.warning("噪底采集失败，使用默认阈值 %.4f", self._threshold)

    # ── 内部：音频回调（在 sounddevice 音频线程中运行）────────────────────────

    def _audio_callback(
        self, indata: np.ndarray, frames: int, time_info, status
    ):
        # AEC：TTS 播放时屏蔽麦克风，防止录到自己的声音
        if self._tts.is_playing():
            with self._lock:
                if self._state == _VADState.COLLECTING:
                    self._state = _VADState.IDLE
                    try:
                        self._asr.stop()   # 丢弃当前积累的缓冲
                    except Exception:
                        pass
            return

        rms = float(np.sqrt(np.mean(indata ** 2)))
        now = time.monotonic()

        with self._lock:
            state = self._state

        if state == _VADState.IDLE:
            if rms > self._threshold:
                with self._lock:
                    self._state         = _VADState.COLLECTING
                    self._last_voice_t  = now
                    self._collect_start = now
                self._asr.start()
                self._asr.send_audio(_to_pcm(indata))
                logger.debug("🎤 语音开始")

        elif state == _VADState.COLLECTING:
            if rms > self._threshold:
                with self._lock:
                    self._last_voice_t = now

            self._asr.send_audio(_to_pcm(indata))

            with self._lock:
                silence  = now - self._last_voice_t
                duration = now - self._collect_start

            # 静音超时或超过最大时长 → 结束本段
            if silence >= self._silence_sec or duration >= self._max_duration:
                with self._lock:
                    actual_speech = self._last_voice_t - self._collect_start
                    self._state = _VADState.DRAINING
                threading.Thread(
                    target=self._finalize,
                    args=(actual_speech,),
                    daemon=True,
                    name="vad-finalize",
                ).start()

        # DRAINING 状态：忽略所有输入，等待 ASR 完成

    # ── 内部：ASR 推理（在独立线程中运行，不阻塞事件循环）────────────────────

    def _finalize(self, actual_speech_duration: float):
        try:
            text = self._asr.stop()
        except Exception as exc:
            logger.warning("ASR stop 失败: %s", exc)
            text = ""
        finally:
            with self._lock:
                self._state = _VADState.IDLE

        if actual_speech_duration < self._min_duration:
            logger.debug("🤐 片段过短 (%.2fs)，丢弃", actual_speech_duration)
            return

        text = (text or "").strip()
        if text:
            logger.info("🗣️ 识别: %s", text)
            asyncio.run_coroutine_threadsafe(
                self._on_speech(text),
                self._loop,
            )
        else:
            logger.debug("🤐 ASR 未识别到内容")


def _to_pcm(indata: np.ndarray) -> bytes:
    """float32 → int16 PCM bytes"""
    return (indata * 32767).astype(np.int16).tobytes()
