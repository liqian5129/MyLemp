"""
实时节拍检测（麦克风拾音）

用 spectral flux 做 onset 检测，inter-onset interval 估算 BPM。
不依赖 aubio，纯 numpy 实现。

组件：
  RealtimeBeatTracker — 逐帧处理音频，输出节拍事件
  RealtimeEnergyAnalyzer — 滑动窗口 RMS 能量
"""
from __future__ import annotations

import logging
from collections import deque

import numpy as np

logger = logging.getLogger(__name__)


class RealtimeBeatTracker:
    """
    实时节拍检测器。

    关键设计：
      - 噪音门控：RMS 低于阈值时完全静默，不检测 onset 也不插虚拟节拍
      - 虚拟节拍需至少 4 次真实 onset 后才启用（BPM 需要可靠估算）
      - spectral flux 阈值倍数较高（2.5），减少环境噪声误触发
    """

    # 至少检测到这么多真实 onset 后，才允许虚拟节拍补偿
    MIN_REAL_BEATS_FOR_VIRTUAL = 4

    def __init__(self, sr: int = 44100, hop_size: int = 1024,
                 noise_floor: float = 0.005):
        self.sr  = sr
        self.hop = hop_size

        # 噪音门控：RMS 低于此值视为静音
        self.noise_floor = noise_floor

        # spectral flux 历史
        self._prev_spectrum: np.ndarray | None = None
        self._flux_history: deque[float] = deque(maxlen=128)
        self._flux_threshold_mult = 2.5

        # 节拍跟踪
        self.beat_times: deque[float] = deque(maxlen=64)
        self.bpm: float         = 120.0
        self.beat_period: float = 0.5
        self.last_beat_time: float | None = None
        self.beat_counter: int  = 0
        self._real_beat_count: int = 0   # 真实 onset 计数（不含虚拟）

        # 音乐状态
        self.music_active: bool = False  # 是否检测到音乐在播放

        # 时间
        self.t_current: float = 0.0

        # 最小 onset 间隔（秒），防止连击误触
        self._min_ioi = 0.25

        # 静音持续时间（秒），超过此值认为音乐停止
        self._silence_timeout = 3.0
        self._last_loud_time: float = 0.0

        # 窗函数缓存
        self._window: np.ndarray | None = None

    def process_chunk(self, audio_chunk: np.ndarray) -> list[float]:
        """
        处理一个音频帧，返回本帧内检测到的节拍时间戳列表。
        audio_chunk: (hop_size,) float32/float64，单声道
        """
        n = len(audio_chunk)
        self.t_current += n / self.sr

        # ── 噪音门控 ────────────────────────────────────────────────────
        rms = float(np.sqrt(np.mean(audio_chunk ** 2)))
        is_loud = rms > self.noise_floor

        if is_loud:
            self._last_loud_time = self.t_current
            if not self.music_active and self._real_beat_count == 0:
                logger.info("🎵 检测到音频信号 (RMS=%.4f)", rms)

        # 超过 silence_timeout 没有响声 → 音乐停止
        if self.t_current - self._last_loud_time > self._silence_timeout:
            if self.music_active:
                logger.info("🔇 音乐停止，进入静默")
                self.music_active = False
                self._real_beat_count = 0
                self.last_beat_time = None
            return []

        # 静音帧直接跳过
        if not is_loud:
            return []

        # ── spectral flux ────────────────────────────────────────────────
        if self._window is None or len(self._window) != n:
            self._window = np.hanning(n).astype(np.float32)

        spectrum = np.abs(np.fft.rfft(audio_chunk * self._window))
        flux = 0.0
        if self._prev_spectrum is not None and len(self._prev_spectrum) == len(spectrum):
            diff = spectrum - self._prev_spectrum
            flux = float(np.sum(np.maximum(diff, 0.0)))
        self._prev_spectrum = spectrum
        self._flux_history.append(flux)

        detected: list[float] = []

        # ── onset 检测 ───────────────────────────────────────────────────
        if len(self._flux_history) >= 10:
            mean_flux = float(np.mean(self._flux_history))
            threshold = mean_flux * self._flux_threshold_mult + 1e-6

            if (flux > threshold
                    and len(self._flux_history) >= 2
                    and flux >= self._flux_history[-2]):
                beat_t = self.t_current
                if (self.last_beat_time is None
                        or beat_t - self.last_beat_time >= self._min_ioi):
                    self._register_beat(beat_t, real=True)
                    detected.append(beat_t)

                    if not self.music_active and self._real_beat_count >= 3:
                        self.music_active = True
                        logger.info("🎶 节拍锁定! BPM=%.0f", self.bpm)

        # ── 漏检补偿（仅在有足够真实节拍后启用）──────────────────────────
        if (self._real_beat_count >= self.MIN_REAL_BEATS_FOR_VIRTUAL
                and self.music_active
                and self.last_beat_time is not None
                and self.t_current - self.last_beat_time > 1.5 * self.beat_period):
            virtual = self.last_beat_time + self.beat_period
            self._register_beat(virtual, real=False)
            detected.append(virtual)

        return detected

    def bar_position(self) -> int:
        """当前 8 拍短句内的位置 [0, 7]"""
        return self.beat_counter % 8

    def _register_beat(self, t: float, real: bool) -> None:
        """记录一个节拍，更新 BPM。"""
        self.beat_times.append(t)
        self.last_beat_time = t
        self.beat_counter += 1
        if real:
            self._real_beat_count += 1
        self._update_bpm()

    def _update_bpm(self) -> None:
        """从最近 onset 间隔估算 BPM（滑动平均）。"""
        if len(self.beat_times) < 3:
            return

        intervals = []
        times = list(self.beat_times)
        for i in range(1, min(len(times), 12)):
            ioi = times[-i] - times[-i - 1]
            if 0.25 < ioi < 1.5:   # 40-240 BPM 范围
                intervals.append(ioi)

        if intervals:
            avg_ioi = float(np.median(intervals))
            new_bpm = 60.0 / avg_ioi
            self.bpm = 0.8 * self.bpm + 0.2 * new_bpm
            self.beat_period = 60.0 / self.bpm


class RealtimeEnergyAnalyzer:
    """滑动窗口 RMS 能量，归一化到 [0, 1]。"""

    def __init__(self, window: int = 30):
        self.history: deque[float] = deque(maxlen=window)

    def process(self, audio_chunk: np.ndarray) -> float:
        rms = float(np.sqrt(np.mean(audio_chunk ** 2)))
        self.history.append(rms)
        peak = max(self.history) + 1e-8
        return float(np.clip(rms / peak, 0.0, 1.0))


class RealtimeSectionDetector:
    """简单能量梯度段落检测。"""

    def __init__(self, threshold: float = 0.35):
        self.threshold = threshold
        self.energy_history: deque[float] = deque(maxlen=64)
        self.cooldown: int = 0

    def process(self, energy: float) -> bool:
        self.energy_history.append(energy)

        if self.cooldown > 0:
            self.cooldown -= 1
            return False

        if len(self.energy_history) > 16:
            long_avg  = float(np.mean(list(self.energy_history)[:32]))
            short_avg = float(np.mean(list(self.energy_history)[-8:]))
            if abs(short_avg - long_avg) > self.threshold:
                self.cooldown = 48
                return True

        return False
