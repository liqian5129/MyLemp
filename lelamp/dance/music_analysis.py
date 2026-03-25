"""
音乐离线分析

使用 librosa 一次性分析音频文件，提取：
  - 精确 beat_times（拍点时间戳数组）
  - BPM（tempo）
  - 逐拍能量（归一化到 [0,1]）
  - 段落边界（能量梯度检测）

运行时只做表查询，零计算开销。
"""
from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)


class OfflineMusicAnalysis:
    """
    离线音乐分析器。

    用法：
        analysis = OfflineMusicAnalysis("song.mp3")
        info = analysis.get_beat_info(play_time_seconds)
    """

    def __init__(self, audio_path: str):
        import librosa

        logger.info("正在分析音乐: %s", audio_path)

        y, sr = librosa.load(audio_path, sr=None, mono=True)

        # ── 节拍跟踪 ──────────────────────────────────────────────────────────
        tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units="frames")
        self.tempo      = float(np.squeeze(tempo))
        self.beat_times = librosa.frames_to_time(beat_frames, sr=sr)  # (N,) seconds

        # ── 逐拍能量 ──────────────────────────────────────────────────────────
        hop_length = 512
        rms        = librosa.feature.rms(y=y, hop_length=hop_length)[0]
        rms_times  = librosa.frames_to_time(
            np.arange(len(rms)), sr=sr, hop_length=hop_length
        )
        # 插值到每个拍点
        beat_energies = np.interp(self.beat_times, rms_times, rms)
        e_max = beat_energies.max() + 1e-6
        self.beat_energies: np.ndarray = (beat_energies / e_max).astype(np.float32)

        # ── 段落检测 ──────────────────────────────────────────────────────────
        self.segments: list[int] = self._detect_segments()

        # ── 内部状态 ──────────────────────────────────────────────────────────
        self._current_idx: int = 0

        logger.info(
            "分析完成: %.1f BPM, %d 拍, %d 个段落",
            self.tempo, len(self.beat_times), len(self.segments) - 1,
        )

    # ── 段落检测 ──────────────────────────────────────────────────────────────

    def _detect_segments(self, window: int = 8, threshold: float = 0.18) -> list[int]:
        """
        简单能量梯度段落检测。
        相邻窗口平均能量差超过 threshold 时判定段落边界。
        返回段落起始拍索引列表（含头尾）。
        """
        energies = self.beat_energies
        segments = [0]

        for i in range(window, len(energies) - window, window):
            before = energies[max(0, i - window) : i].mean()
            after  = energies[i : i + window].mean()
            if abs(float(after) - float(before)) > threshold:
                segments.append(i)

        segments.append(len(self.beat_times))
        return segments

    # ── 运行时查询 ────────────────────────────────────────────────────────────

    def get_beat_info(self, play_time: float) -> dict | None:
        """
        返回当前播放时间对应的节拍信息。
        歌曲结束（play_time 超出最后一拍 2 秒）返回 None。

        Returns:
            {
                "beat_time":      float   当前拍绝对时间（秒）
                "beat_period":    float   一拍时长（秒）
                "bar_position":   int     在 8 拍短句中的位置 [0, 7]
                "energy":         float   归一化能量 [0, 1]
                "section_changed": bool   是否为新段落起点
            }
        """
        beats = self.beat_times

        if play_time > beats[-1] + 2.0:
            return None

        # 找当前节拍索引
        idx = int(np.searchsorted(beats, play_time, side="right")) - 1
        idx = int(np.clip(idx, 0, len(beats) - 1))
        self._current_idx = idx

        # 一拍时长
        if idx + 1 < len(beats):
            beat_period = float(beats[idx + 1] - beats[idx])
        else:
            beat_period = 60.0 / self.tempo

        # 段落变化
        section_changed = idx in self.segments[1:-1]

        return {
            "beat_time":       float(beats[idx]),
            "beat_period":     beat_period,
            "bar_position":    idx % 8,
            "energy":          float(self.beat_energies[idx]),
            "section_changed": section_changed,
        }
