"""OmniEar — 小Q 的智能耳朵。

本地 VAD（RMS 自适应阈值）检测语音段 → HTTP Omni（qwen3-omni-flash）结构化分析。
替换 ContinuousListener + FunASR。
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

from lelamp.soul.audio_event import AudioEvent
from lelamp.voice.qwen_omni_http import QwenOmniHTTPClient

logger = logging.getLogger(__name__)

# ── 音频参数 ──────────────────────────────────────────────────────────────────
_SAMPLE_RATE = 16000
_CHANNELS = 1
_CHUNK_FRAMES = 1600       # 100ms @ 16kHz

# ── VAD 默认参数 ──────────────────────────────────────────────────────────────
_SILENCE_SEC = 1.2         # 连续静音多久截断一段
_MAX_DURATION = 30.0       # 单段最长秒数
_MIN_DURATION = 0.3        # 短于此的片段丢弃
_CALIBRATION_SEC = 2.0     # 噪底校准时长


class _VADState(Enum):
    IDLE = auto()
    COLLECTING = auto()
    ANALYZING = auto()


class OmniEar:
    """智能耳朵：本地 VAD + HTTP Omni 分析 → AudioEvent 回调。

    用法::

        ear = OmniEar(api_key="...")
        ear.on_event = my_async_callback
        await ear.start()
        # ...
        ear.mute()    # TTS 播放时
        ear.unmute()  # TTS 播放结束
        # ...
        await ear.stop()
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "qwen3-omni-flash",
        silence_sec: float = _SILENCE_SEC,
        max_duration: float = _MAX_DURATION,
        min_duration: float = _MIN_DURATION,
        calibration_sec: float = _CALIBRATION_SEC,
        input_device: Optional[int] = None,
    ):
        self._http_client = QwenOmniHTTPClient(api_key=api_key, model=model)
        self._silence_sec = silence_sec
        self._max_duration = max_duration
        self._min_duration = min_duration
        self._calibration_sec = calibration_sec
        self._input_device = input_device

        # VAD 状态
        self._state = _VADState.IDLE
        self._threshold = 0.030
        self._last_voice_t = 0.0
        self._collect_start = 0.0
        self._audio_chunks: list[bytes] = []
        self._lock = threading.Lock()
        self._muted = False

        self._stream: Optional[sd.InputStream] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

        # 外部回调
        self.on_event: Optional[Callable[[AudioEvent], Coroutine]] = None

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """校准噪底 → 开启麦克风流。"""
        self._loop = asyncio.get_running_loop()
        self._calibrate()

        stream_kwargs = {
            "samplerate": _SAMPLE_RATE,
            "channels": _CHANNELS,
            "dtype": np.float32,
            "blocksize": _CHUNK_FRAMES,
            "callback": self._audio_callback,
        }
        if self._input_device is not None:
            stream_kwargs["device"] = self._input_device

        self._stream = sd.InputStream(**stream_kwargs)
        self._stream.start()
        logger.info(
            "OmniEar 已启动 (threshold=%.4f, silence=%.1fs, device=%s)",
            self._threshold, self._silence_sec, self._input_device,
        )

    async def stop(self) -> None:
        """停止监听。"""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        logger.info("OmniEar 已停止")

    def mute(self) -> None:
        """TTS 播放时调用：停止接收音频。"""
        self._muted = True
        with self._lock:
            if self._state == _VADState.COLLECTING:
                self._state = _VADState.IDLE
                self._audio_chunks.clear()

    def unmute(self) -> None:
        """TTS 播放结束后调用：恢复接收。"""
        self._muted = False

    # ── 噪底校准 ──────────────────────────────────────────────────────────────

    def _calibrate(self) -> None:
        """采集静默背景，设置自适应阈值。"""
        logger.info("采集环境噪底（%.1f 秒）...", self._calibration_sec)
        rms_samples: list[float] = []

        def _collect(indata, frames, time_info, status):
            rms_samples.append(float(np.sqrt(np.mean(indata ** 2))))

        kwargs = {
            "samplerate": _SAMPLE_RATE,
            "channels": _CHANNELS,
            "dtype": np.float32,
            "blocksize": _CHUNK_FRAMES,
            "callback": _collect,
        }
        if self._input_device is not None:
            kwargs["device"] = self._input_device

        with sd.InputStream(**kwargs):
            time.sleep(self._calibration_sec)

        if rms_samples:
            noise_floor = float(np.mean(rms_samples))
            self._threshold = max(noise_floor * 3.0, 0.015)
            logger.info("噪底=%.4f → 阈值=%.4f", noise_floor, self._threshold)
        else:
            logger.warning("噪底采集失败，使用默认阈值 %.4f", self._threshold)

    # ── 音频回调（sounddevice 线程）────────────────────────────────────────────

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status):
        if self._muted:
            return

        rms = float(np.sqrt(np.mean(indata ** 2)))
        pcm = (indata * 32767).astype(np.int16).tobytes()
        now = time.monotonic()

        with self._lock:
            state = self._state

        if state == _VADState.IDLE:
            if rms > self._threshold:
                with self._lock:
                    self._state = _VADState.COLLECTING
                    self._audio_chunks = [pcm]
                    self._last_voice_t = now
                    self._collect_start = now
                logger.debug("语音开始")

        elif state == _VADState.COLLECTING:
            if rms > self._threshold:
                with self._lock:
                    self._last_voice_t = now
            self._audio_chunks.append(pcm)

            with self._lock:
                silence = now - self._last_voice_t
                duration = now - self._collect_start

            if silence >= self._silence_sec or duration >= self._max_duration:
                with self._lock:
                    self._state = _VADState.ANALYZING
                    chunks = list(self._audio_chunks)
                    self._audio_chunks.clear()
                    speech_dur = self._last_voice_t - self._collect_start

                threading.Thread(
                    target=self._analyze,
                    args=(chunks, speech_dur),
                    daemon=True,
                    name="omni-ear-analyze",
                ).start()

    # ── 分析线程 ──────────────────────────────────────────────────────────────

    def _analyze(self, chunks: list[bytes], speech_duration: float) -> None:
        if speech_duration < self._min_duration:
            logger.debug("片段过短 (%.2fs)，丢弃", speech_duration)
            with self._lock:
                self._state = _VADState.IDLE
            return

        pcm_data = b"".join(chunks)

        async def _do():
            try:
                result = await self._http_client.analyze_audio(pcm_data, _SAMPLE_RATE)
                event = AudioEvent(
                    text=result.get("text", ""),
                    emotion=result.get("emotion", "neutral"),
                    intent=result.get("intent", "none"),
                    audio_env=result.get("audio_env", ""),
                    user_activity=result.get("user_activity", "未知"),
                    is_speech=bool(result.get("text")),
                )
                logger.info(
                    "AudioEvent: text=%r emotion=%s intent=%s env=%r activity=%r",
                    event.text, event.emotion, event.intent, event.audio_env, event.user_activity,
                )
                if self.on_event and (event.text or event.audio_env):
                    await self.on_event(event)
            except Exception as exc:
                logger.warning("HTTP Omni 分析失败: %s", exc)
            finally:
                with self._lock:
                    self._state = _VADState.IDLE

        asyncio.run_coroutine_threadsafe(_do(), self._loop)
