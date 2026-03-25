# 机器人跟音乐跳街舞：系统设计文档（v2 增强版）

> 适用平台：台灯机器人、桌面机械臂等少自由度非拟人机器人
> 目标：随机播放音乐时，机器人能卡点、变节奏、做出多样动作，像街舞演员一样

---

## 目录

1. [核心难点与设计原则](#1-核心难点与设计原则)
2. [系统整体架构](#2-系统整体架构)
3. [层1：音乐分析](#3-层1音乐分析)
4. [动作原语库设计](#4-动作原语库设计)
5. [层2：编舞调度器](#5-层2编舞调度器)
6. [层3：执行层](#6-层3执行层)
7. [主循环](#7-主循环)
8. [Groove 感：让机器人不像节拍器](#8-groove-感让机器人不像节拍器)
9. [LLM 的角色](#9-llm-的角色)
10. [工程实施路径（快速推进版）](#10-工程实施路径快速推进版)

---

## 1. 核心难点与设计原则

### 1.1 跳舞和情绪表达的本质区别

情绪表达是**状态驱动**的：给定情绪标签 → 生成一段动作，一次性完成。

跳舞是**时间驱动**的：持续感知音乐流 → 在正确的时刻做正确的动作，这是一个时间耦合的控制问题。

两者的关键差异：

| | 情绪表达 | 跳舞 |
|---|---|---|
| 时间关系 | 与时间无关，一次性 | 必须和音乐时间精确同步 |
| 触发方式 | 状态变化触发 | 节拍周期性触发 |
| LLM 角色 | 实时生成路径上 | 只在离线阶段使用 |
| 核心问题 | 情绪 → 动作的映射 | 节拍 → 动作的时间对齐 |

### 1.2 三个核心难点

**难点1：卡点需要预判，而非反应**

街舞演员卡点的秘密是：在节拍到来之前就已经开始运动，在节拍精确到来的时刻让身体**抵达**重音姿态。

如果在听到节拍时才发指令，由于舵机惯性（响应延迟通常 80-150ms），动作会明显滞后，视觉上"慢半拍"。

```
错误做法（反应式）：
  节拍到来 → 发送指令 → 舵机开始运动 → 动作到达  ← 滞后 ~100ms

正确做法（预判式）：
  提前 prep_time → 发送指令 → 节拍到来时动作精确到达重音姿态
```

**难点2：律动感（groove）不只是卡节拍**

完全在节拍上的机器人动作听起来对但看起来像节拍器。街舞的"感觉"来自：
- 对节拍的细微超前/延迟（micro-timing）
- 身体各部位的协调关系（身体波，body wave）

**难点3：多样性和同步性的矛盾**

随机动作多样但不卡点，预设动作卡点但重复。需要通过**状态机 + 原语库**的组合，在有限的原语里产生足够丰富的动作感受。

### 1.3 卡点的工程定义

```
move_start_time = beat_time - accent_norm × total_duration - PREP_TIME

其中：
  beat_time     = 目标节拍的绝对时间戳
  accent_norm   = 重音帧在动作内的归一化位置（约 0.3）
  total_duration = 动作总时长
  PREP_TIME     = 舵机响应补偿（实测，通常 80-150ms）
```

---

## 2. 系统整体架构

系统分三层，每层工作频率不同：

```
┌─────────────────────────────────────────────────────────────┐
│  离线阶段（一次性）                                           │
│  LLM → 动作原语库（按节拍位置×能量×类型×时值分类）             │
│  librosa → 整首歌的 beat/段落/能量预分析（推荐主路径）         │
└─────────────────────────────────────────────────────────────┘
                           ↓ 预加载
┌─────────────────────────────────────────────────────────────┐
│  层1：音乐分析                                                │
│  主路径（离线预分析）：查表获取精确 beat 时间戳（0延迟）        │
│  备选路径（实时）：aubio 在线检测（~10ms 延迟）                │
│  节拍检测 | 小节位置 | 能量包络 | 段落检测                      │
└─────────────────────────────────────────────────────────────┘
                           ↓ 每 beat 一次
┌─────────────────────────────────────────────────────────────┐
│  层1.5：基础律动层（持续运行）                                 │
│  正弦基础振荡 → 叠加在所有动作输出之上，保证无动作时不静止     │
└─────────────────────────────────────────────────────────────┘
                           ↓
┌─────────────────────────────────────────────────────────────┐
│  层2：编舞调度器  (每 beat 一次决策)                           │
│  Phrase Template 编排（替代纯马尔可夫）                        │
│  → 动作选择器（从原语库检索，防重复）                           │
│  → 时间调度器（计算 move_start = beat_time - prep）           │
└─────────────────────────────────────────────────────────────┘
                           ↓ 10ms 控制循环
┌─────────────────────────────────────────────────────────────┐
│  层3：执行层  (100Hz)                                         │
│  运动队列 → 增量插值 + 基础律动叠加 → 安全裁剪 → 关节控制器    │
│  → 姿态漂移校正 → 舵机                                       │
└─────────────────────────────────────────────────────────────┘
```

**关键设计：LLM 完全不在实时路径上**，只用于离线生成原语库。实时路径全部是轻量的 numpy 计算。

---

## 3. 层1：音乐分析

### 3.1 推荐主路径：离线预分析（精度最高、复杂度最低）

如果使用场景允许提前知道要播放什么歌曲（演示、表演、预设歌单等），**强烈推荐先用 librosa 做整首歌的离线分析**。相比实时检测，离线分析的优势是：

- 节拍时间戳 100% 精确，没有在线检测的漏检/误检问题
- 段落边界可以用频谱聚类（`librosa.segment.agglomerative`），远比能量突变准确
- `bar_position` 直接从节拍序列索引算，不可能漂移
- 系统复杂度直接砍掉一半——整个 `BeatClock` 变成一个数组查表

```python
import librosa
import numpy as np

class OfflineMusicAnalysis:
    """
    离线分析整首歌曲，生成精确的节拍、段落、能量时间线
    一次分析，运行时零延迟查表
    """
    def __init__(self, audio_path: str):
        self.y, self.sr = librosa.load(audio_path, sr=44100)

        # ── 节拍检测 ──
        self.tempo, self.beat_frames = librosa.beat.beat_track(
            y=self.y, sr=self.sr
        )
        self.beat_times = librosa.frames_to_time(
            self.beat_frames, sr=self.sr
        )
        self.beat_period = 60.0 / self.tempo

        # ── 段落检测（频谱聚类，比能量突变准确得多）──
        self.segments = self._detect_segments()

        # ── 逐拍能量 ──
        self.beat_energies = self._compute_beat_energies()

        # ── 预计算 bar_position（假设 4/4 拍）──
        self.bar_positions = [i % 4 for i in range(len(self.beat_times))]

        self._current_idx = 0

    def _detect_segments(self) -> list[float]:
        """用频谱聚类检测段落边界"""
        chroma = librosa.feature.chroma_cqt(y=self.y, sr=self.sr)
        bounds = librosa.segment.agglomerative(chroma, k=8)
        bound_times = librosa.frames_to_time(bounds, sr=self.sr)
        return bound_times.tolist()

    def _compute_beat_energies(self) -> np.ndarray:
        """计算每个节拍附近的相对能量"""
        rms = librosa.feature.rms(y=self.y)[0]
        rms_times = librosa.frames_to_time(
            np.arange(len(rms)), sr=self.sr
        )
        energies = np.interp(self.beat_times, rms_times, rms)
        # 归一化到 [0, 1]
        if energies.max() > 0:
            energies = energies / energies.max()
        return energies

    def get_beat_info(self, current_time: float):
        """
        根据当前播放时间，返回下一个节拍的全部信息
        纯查表操作，零延迟
        """
        # 找到下一个尚未到来的节拍
        while (self._current_idx < len(self.beat_times)
               and self.beat_times[self._current_idx] <= current_time):
            self._current_idx += 1

        if self._current_idx >= len(self.beat_times):
            return None  # 歌曲结束

        idx = self._current_idx
        beat_time = self.beat_times[idx]

        # 判断是否处于段落边界附近
        section_changed = any(
            abs(beat_time - seg) < self.beat_period * 0.5
            for seg in self.segments
        )

        return {
            "beat_time":       beat_time,
            "bar_position":    self.bar_positions[idx],
            "energy":          float(self.beat_energies[idx]),
            "section_changed": section_changed,
            "beat_period":     self.beat_period,
            # 预览未来 4 拍
            "upcoming_beats":  self.beat_times[idx:idx+4].tolist(),
        }
```

### 3.2 备选路径：实时节拍检测

当场景要求处理未知音频流（如现场随机放歌）时，使用 aubio 实时检测。

> **⚠️ 已知风险：bar_position 累积漂移**
>
> 以下实现用 `round((t_current - last_beat_time) / beat_period)` 来推算小节内位置，
> 但 `last_beat_time` 只在检测到节拍时更新。如果 aubio 漏检一拍（在过渡段或节奏变化时常见），
> `bar_position` 会整体错位，导致 downbeat 判断错误、状态机在错误时刻触发转移。
>
> **缓解方案**：加入节拍漏检检测——如果距上次节拍超过 `1.5 × beat_period` 仍无新节拍，
> 主动插入一个"虚拟节拍"并重置计数。

```python
import aubio
import numpy as np
from collections import deque

class BeatClock:
    """
    实时节拍检测，输出未来节拍预测
    使用 aubio 库实现在线检测
    """
    def __init__(self, sample_rate=44100, hop_size=512):
        self.sr       = sample_rate
        self.hop      = hop_size
        self.tempo    = aubio.tempo("default", 2048, hop_size, sample_rate)
        self.bpm      = 120.0
        self.beat_period = 60.0 / self.bpm
        self.last_beat_time = None
        self.t_current = 0.0
        self.beat_counter = 0  # 全局节拍计数器（用于准确的 bar_position）

        # 预测的未来 4 个节拍时间
        self.upcoming_beats: deque = deque(maxlen=8)

    def process_frame(self, audio_chunk: np.ndarray) -> list[float]:
        """
        每个音频 chunk 调用一次
        返回：这帧内检测到的节拍时间列表（绝对时间戳）
        """
        is_beat = self.tempo(audio_chunk)[0]
        self.t_current += self.hop / self.sr

        detected = []
        if is_beat:
            beat_t = self.t_current
            # 滑动平均更新 BPM，防止抖动
            new_bpm = self.tempo.get_bpm()
            if 60 < new_bpm < 200:
                self.bpm = 0.85 * self.bpm + 0.15 * new_bpm
                self.beat_period = 60.0 / self.bpm

            self.last_beat_time = beat_t
            self.beat_counter += 1
            detected.append(beat_t)
            self._update_upcoming(beat_t)

        # ── 漏检补偿：距上次节拍超过 1.5 倍周期，插入虚拟节拍 ──
        elif (self.last_beat_time is not None
              and (self.t_current - self.last_beat_time) > 1.5 * self.beat_period):
            virtual_beat = self.last_beat_time + self.beat_period
            self.last_beat_time = virtual_beat
            self.beat_counter += 1
            detected.append(virtual_beat)
            self._update_upcoming(virtual_beat)

        return detected

    def _update_upcoming(self, last_beat: float):
        """基于当前 BPM 预测未来 4 个节拍"""
        self.upcoming_beats.clear()
        for i in range(1, 5):
            self.upcoming_beats.append(last_beat + i * self.beat_period)

    def next_beat_in(self) -> float:
        """距下一个节拍还有多少秒"""
        if not self.upcoming_beats:
            return self.beat_period
        return max(0.0, self.upcoming_beats[0] - self.t_current)

    def bar_position(self) -> int:
        """当前是 bar 内第几拍（0-3），假设 4/4 拍"""
        # 改用全局计数器取模，避免浮点除法的累积漂移
        return self.beat_counter % 4
```

### 3.3 能量分析

驱动动作强度，用于状态机的转移决策：

```python
class EnergyAnalyzer:
    def __init__(self, sr=44100, hop=512, window=20):
        self.history = deque(maxlen=window)   # 最近 20 帧的 RMS
        self.sr, self.hop = sr, hop

    def process(self, chunk: np.ndarray) -> float:
        """
        返回 [0, 1] 的相对能量值
        0 = 很安静，1 = 很响（相对于近期平均）
        """
        rms = float(np.sqrt(np.mean(chunk ** 2)))
        self.history.append(rms)
        avg = np.mean(self.history) if self.history else rms
        return float(np.clip(rms / (avg + 1e-6), 0.0, 1.0))
```

### 3.4 段落检测（实时路径）

> **⚠️ 已知局限**：仅用能量突变做段落检测，误触发率较高——一个鼓 fill 就可能被误判为段落切换。
> 冷却期 0.37s 不足以抑制连续高能量段。建议优先使用离线路径的频谱聚类段落检测。

用于实时路径的简单实现（冷却期已加大）：

```python
class SectionDetector:
    def __init__(self, threshold=0.4):
        self.prev_energy    = 0.0
        self.threshold      = threshold
        self.cooldown       = 0
        self.energy_history = deque(maxlen=64)  # 更长的能量历史

    def process(self, energy: float) -> bool:
        """返回 True 表示检测到段落切换"""
        self.energy_history.append(energy)
        changed = False

        if self.cooldown <= 0:
            # 对比长期均值而非仅前一帧，减少鼓 fill 误触发
            if len(self.energy_history) > 16:
                long_avg = np.mean(list(self.energy_history)[:32])
                short_avg = np.mean(list(self.energy_history)[-8:])
                delta = abs(short_avg - long_avg)
            else:
                delta = abs(energy - self.prev_energy)

            if delta > self.threshold:
                changed = True
                self.cooldown = 64   # 冷却加大到 64 帧（约 0.74s）
        else:
            self.cooldown -= 1

        self.prev_energy = energy
        return changed
```

---

## 4. 动作原语库设计

### 4.1 原语的数据结构

原语使用**增量角度**而非绝对角度，使任何原语可以从当前姿态无缝开始，不需要先回中性。

```python
from dataclasses import dataclass, field
from typing import Literal
import numpy as np

BeatPos  = Literal["downbeat", "beat2", "beat3", "beat4", "offbeat"]
EnergyLv = Literal["low", "mid", "high"]
MoveType = Literal["hit", "groove", "flow", "freeze"]
Duration = Literal["half", "one", "two"]  # 占 0.5 / 1 / 2 拍

@dataclass
class MotionPrimitive:
    id:        str
    beat_pos:  BeatPos    # 最适合出现在哪个拍位
    energy:    EnergyLv   # 适合什么能量级别
    type:      MoveType   # 动作类型
    duration:  Duration   # 时值

    # 关键帧：相对于当前姿态的增量（度）
    # 第 1 帧（frame_times[1]）是重音帧，通常在 25~35% 处
    delta_frames: list[list[float]]  # [(Δq0,...,Δq5), ...]
    frame_times:  list[float]        # 各帧归一化时间 [0.0 ... 1.0]

    tags:   list[str] = field(default_factory=list)
    weight: float = 1.0   # 被选中的基础概率（可通过反馈更新）
```

### 4.2 四类动作的设计原则

**HIT（重音强调）**
- 重音帧在 25~35% 位置，快速到达
- 幅度明显（15-30°），不含糊
- 在目标节拍时刻精确抵达重音姿态

**GROOVE（律动振荡）**
- 通常跨越 2 拍，在 beat 1 和 beat 3 各有一次强调
- 幅度中等（10-20°），节奏感强
- 可以无限循环，末帧接近起始帧

**FLOW（流动动作）**
- 跨越 2-4 拍的大幅流动
- 无明显强拍点，更强调路径感
- 适合 verse 等低能量段落

**FREEZE（静止保持）**
- 快速到达一个固定姿态，保持不动
- 末尾快速回位
- 适合高能量段落的强调

### 4.3 参数化原语生成器（批量生产替代手调）

手动填写 `delta_frames` 数组效率极低且容易出错。以下参数化生成器可以从"动作动词 + 强度 + 速度"快速生成原语，效率约为手写的 **10 倍**：

```python
import random
import string

# 关节定义：
# q0=底座旋转, q1=下臂俯仰, q2=中段弯曲,
# q3=上臂俯仰, q4=头部左右, q5=头部俯仰

# 方向 → 各关节的增量方向模板（归一化，实际幅度由 intensity 控制）
DIRECTION_PROFILES = {
    "up":      np.array([0,    +0.6, -0.3, +0.5, 0,    +1.0]),
    "down":    np.array([0,    -0.4, +0.5, -0.6, 0,    -0.8]),
    "forward": np.array([0,     0,   +1.0,  0,   0,    -0.4]),
    "back":    np.array([0,     0,   -0.8,  0,   0,    +0.5]),
    "twist_r": np.array([+0.8, +0.3, -0.4, +0.2, -0.6, +0.5]),
    "twist_l": np.array([-0.8, +0.3, -0.4, +0.2, +0.6, +0.5]),
    "tilt_r":  np.array([0,     0,    0,    0,   +1.0, +0.2]),
    "tilt_l":  np.array([0,     0,    0,    0,   -1.0, +0.2]),
    "nod":     np.array([0,     0,    0,    0,    0,   -1.0]),
    "bounce":  np.array([0,    +0.6, -0.4, +0.6,  0,   +0.5]),
}


def generate_hit(direction: str = "up",
                 intensity: float = 0.7,
                 speed: str = "fast",
                 beat_pos: BeatPos = "downbeat",
                 energy: EnergyLv = "mid") -> MotionPrimitive:
    """
    参数化生成 HIT 原语
    direction: DIRECTION_PROFILES 中的键
    intensity: 0.0~1.0，映射到角度范围
    speed: "fast" / "medium" → 控制重音帧位置
    """
    amp = intensity * 25  # 最大约 25°
    accent_t = 0.25 if speed == "fast" else 0.35

    profile = DIRECTION_PROFILES[direction]
    peak = (profile * amp).tolist()
    mid  = [v * 0.4 for v in peak]

    uid = f"hit_{direction}_{int(intensity*10)}_{speed[:1]}"

    return MotionPrimitive(
        id=uid,
        beat_pos=beat_pos,
        energy=energy,
        type="hit",
        duration="one",
        delta_frames=[[0]*6, peak, mid, [0]*6],
        frame_times=[0.0, accent_t, 0.65, 1.0],
        tags=[direction, speed],
    )


def generate_groove(direction1: str = "twist_r",
                    direction2: str = "twist_l",
                    intensity: float = 0.5,
                    energy: EnergyLv = "low") -> MotionPrimitive:
    """参数化生成 GROOVE 原语（2拍振荡）"""
    amp = intensity * 20

    p1 = (DIRECTION_PROFILES[direction1] * amp).tolist()
    p2 = (DIRECTION_PROFILES[direction2] * amp).tolist()

    uid = f"groove_{direction1}_{direction2}_{int(intensity*10)}"

    return MotionPrimitive(
        id=uid,
        beat_pos="downbeat",
        energy=energy,
        type="groove",
        duration="two",
        delta_frames=[[0]*6, p1, [0]*6, p2, [0]*6],
        frame_times=[0.0, 0.25, 0.50, 0.75, 1.0],
        tags=["groove", direction1, direction2],
    )


def generate_freeze(direction: str = "forward",
                    intensity: float = 0.8,
                    energy: EnergyLv = "high") -> MotionPrimitive:
    """参数化生成 FREEZE 原语"""
    amp = intensity * 22

    pose = (DIRECTION_PROFILES[direction] * amp).tolist()
    uid = f"freeze_{direction}_{int(intensity*10)}"

    return MotionPrimitive(
        id=uid,
        beat_pos="downbeat",
        energy=energy,
        type="freeze",
        duration="two",
        delta_frames=[[0]*6, pose, pose, pose, [0]*6],
        frame_times=[0.0, 0.15, 0.50, 0.85, 1.0],
        tags=["freeze", direction],
    )


def generate_flow(waypoints: list[str],
                  intensity: float = 0.5,
                  energy: EnergyLv = "low") -> MotionPrimitive:
    """参数化生成 FLOW 原语（多路径点流动）"""
    amp = intensity * 18
    frames = [[0]*6]
    n = len(waypoints)
    times = [0.0]

    for i, wp in enumerate(waypoints):
        profile = DIRECTION_PROFILES[wp]
        frames.append((profile * amp).tolist())
        times.append((i + 1) / (n + 1))

    frames.append([0]*6)
    times.append(1.0)

    uid = f"flow_{'_'.join(w[:2] for w in waypoints)}_{int(intensity*10)}"

    return MotionPrimitive(
        id=uid,
        beat_pos="downbeat",
        energy=energy,
        type="flow",
        duration="two",
        delta_frames=frames,
        frame_times=times,
        tags=["flow"] + waypoints,
    )


def batch_generate_library() -> list[MotionPrimitive]:
    """
    批量生成完整原语库
    用参数排列组合快速产出，再筛选效果好的
    """
    library = []

    # HIT：方向 × 强度 × 速度
    for direction in ["up", "down", "forward", "twist_r", "twist_l", "tilt_r", "nod"]:
        for intensity in [0.5, 0.7, 0.9]:
            for speed in ["fast", "medium"]:
                energy = "high" if intensity > 0.7 else "mid"
                library.append(generate_hit(direction, intensity, speed, energy=energy))

    # GROOVE：对称方向对
    groove_pairs = [
        ("twist_r", "twist_l"), ("tilt_r", "tilt_l"),
        ("up", "down"), ("nod", "nod"), ("bounce", "bounce"),
    ]
    for d1, d2 in groove_pairs:
        for intensity in [0.4, 0.6]:
            library.append(generate_groove(d1, d2, intensity))

    # FREEZE：各方向
    for direction in ["forward", "up", "twist_r", "tilt_r", "back"]:
        library.append(generate_freeze(direction, intensity=0.8))

    # FLOW：多路径点
    flow_paths = [
        ["twist_r", "up", "twist_l"],
        ["tilt_r", "forward", "tilt_l"],
        ["up", "twist_r", "down", "twist_l"],
    ]
    for path in flow_paths:
        library.append(generate_flow(path, intensity=0.5))

    return library
```

使用方式：
```python
# 一次性生成 50+ 条候选原语
all_primitives = batch_generate_library()

# 在仿真器中快速预览，保留效果好的
# 删除看起来不好的，调整剩余的 weight
PRIMITIVE_LIBRARY = [p for p in all_primitives if p.id not in REJECTED_IDS]
```

### 4.4 手工精调原语示例（作为参数化生成的补充）

参数化生成覆盖大部分需求，但少数"招牌动作"仍需手工精调：

```python
# 关节定义：
# q0=底座旋转, q1=下臂俯仰, q2=中段弯曲,
# q3=上臂俯仰, q4=头部左右, q5=头部俯仰

HANDCRAFTED_PRIMITIVES = [

    # ── HIT 类（手工精调的招牌动作）─────────────────────────
    MotionPrimitive(
        id="head_pop_up",
        beat_pos="downbeat", energy="mid", type="hit", duration="one",
        delta_frames=[
            [  0,   0,   0,   0,   0,   0],   # 起始（当前姿态）
            [  0, +15,  -8, +12,   0, +18],   # 重音帧 ← beat 精确到达
            [  0,  +8,  -4,  +6,   0, +10],   # 回落一半
            [  0,   0,   0,   0,   0,   0],   # 回中性
        ],
        frame_times=[0.0, 0.32, 0.65, 1.0],
        tags=["energetic", "upward"],
    ),

    MotionPrimitive(
        id="body_lean_forward",
        beat_pos="downbeat", energy="high", type="hit", duration="one",
        delta_frames=[
            [  0,   0,   0,   0,   0,   0],
            [  0,   0, +20,   0,   0, -10],   # 前倾重音
            [  0,   0,  +8,   0,   0,  -4],
            [  0,   0,   0,   0,   0,   0],
        ],
        frame_times=[0.0, 0.28, 0.60, 1.0],
        tags=["aggressive", "forward"],
    ),

    MotionPrimitive(
        id="head_tilt_sharp",
        beat_pos="beat2", energy="mid", type="hit", duration="half",
        delta_frames=[
            [  0,   0,   0,   0,   0,   0],
            [  0,   0,   0,   0, +25,  +5],   # 快速歪头
            [  0,   0,   0,   0, +10,  +2],
        ],
        frame_times=[0.0, 0.25, 1.0],
        tags=["cute", "sideways"],
    ),

    MotionPrimitive(
        id="twist_and_pop",
        beat_pos="downbeat", energy="high", type="hit", duration="one",
        delta_frames=[
            [  0,   0,   0,   0,   0,   0],
            [+20,  +8, -10,  +5, -15, +12],   # 扭转重音
            [ +8,  +3,  -4,  +2,  -6,  +5],
            [  0,   0,   0,   0,   0,   0],
        ],
        frame_times=[0.0, 0.30, 0.62, 1.0],
        tags=["dynamic", "twist"],
    ),

    # ── GROOVE 类 ───────────────────────────────────────────
    MotionPrimitive(
        id="base_sway",
        beat_pos="downbeat", energy="low", type="groove", duration="two",
        delta_frames=[
            [   0,  0,  0,  0,   0,  0],
            [ +12,  0,  0,  0,  -8,  0],   # 右摇（beat 1）
            [   0,  0,  0,  0,   0,  0],   # 中（beat 2）
            [ -12,  0,  0,  0,  +8,  0],   # 左摇（beat 3）
            [   0,  0,  0,  0,   0,  0],   # 中（beat 4）
        ],
        frame_times=[0.0, 0.25, 0.50, 0.75, 1.0],
        tags=["groove", "rhythmic"],
    ),

    MotionPrimitive(
        id="head_nod",
        beat_pos="downbeat", energy="low", type="groove", duration="two",
        delta_frames=[
            [0, 0, 0, 0, 0,   0],
            [0, 0, 0, 0, 0, -14],   # 点头（beat 1）
            [0, 0, 0, 0, 0,   0],
            [0, 0, 0, 0, 0, -14],   # 点头（beat 3）
            [0, 0, 0, 0, 0,   0],
        ],
        frame_times=[0.0, 0.25, 0.50, 0.75, 1.0],
        tags=["groove", "head"],
    ),

    MotionPrimitive(
        id="bounce_groove",
        beat_pos="downbeat", energy="mid", type="groove", duration="two",
        delta_frames=[
            [  0,   0,   0,   0,   0,   0],
            [  0,  +8,  -6,  +8,   0,  +6],   # 上弹（beat 1）
            [  0,   0,   0,   0,   0,   0],
            [  0,  +8,  -6,  +8,   0,  +6],   # 上弹（beat 3）
            [  0,   0,   0,   0,   0,   0],
        ],
        frame_times=[0.0, 0.22, 0.50, 0.72, 1.0],
        tags=["groove", "bounce"],
    ),

    # ── FREEZE 类 ───────────────────────────────────────────
    MotionPrimitive(
        id="freeze_lean",
        beat_pos="downbeat", energy="high", type="freeze", duration="two",
        delta_frames=[
            [  0,   0,   0,   0,   0,   0],
            [ +8,  +5, +15,   0, +15,  -8],   # 快速到达 freeze 姿态
            [ +8,  +5, +15,   0, +15,  -8],   # 保持
            [ +8,  +5, +15,   0, +15,  -8],   # 保持
            [  0,   0,   0,   0,   0,   0],   # 回位
        ],
        frame_times=[0.0, 0.15, 0.50, 0.85, 1.0],
        tags=["dramatic", "hold"],
    ),

    MotionPrimitive(
        id="freeze_low",
        beat_pos="beat3", energy="high", type="freeze", duration="one",
        delta_frames=[
            [  0,   0,   0,   0,   0,   0],
            [  0, -10, +18, -15,   0, -20],   # 低伏 freeze
            [  0, -10, +18, -15,   0, -20],   # 保持
            [  0,   0,   0,   0,   0,   0],
        ],
        frame_times=[0.0, 0.20, 0.75, 1.0],
        tags=["freeze", "low"],
    ),

    # ── FLOW 类 ─────────────────────────────────────────────
    MotionPrimitive(
        id="slow_sweep",
        beat_pos="downbeat", energy="low", type="flow", duration="two",
        delta_frames=[
            [   0,  0,   0,   0,   0,  0],
            [ -15, +8,  -5, +10, +20, +8],
            [ +15, +5,   0,  +5, -20, +5],
            [   0,  0,   0,   0,   0,  0],
        ],
        frame_times=[0.0, 0.33, 0.67, 1.0],
        tags=["fluid", "wide"],
    ),

    MotionPrimitive(
        id="head_circle_flow",
        beat_pos="downbeat", energy="low", type="flow", duration="two",
        delta_frames=[
            [  0,  0,  0,  0,   0,  0],
            [  0,  0,  0,  0, +20, +15],
            [  0,  0,  0,  0,   0, +25],
            [  0,  0,  0,  0, -20, +15],
            [  0,  0,  0,  0,   0,  0],
        ],
        frame_times=[0.0, 0.25, 0.50, 0.75, 1.0],
        tags=["fluid", "circular"],
    ),
]
```

### 4.5 原语库规模建议

| 类型 | 建议数量 | 优先级 |
|---|---|---|
| HIT（各拍位）| 10-15 条 | 最高 |
| GROOVE | 5-8 条 | 高 |
| FREEZE | 4-6 条 | 中 |
| FLOW | 4-6 条 | 中 |
| 合计 | 25-35 条 | — |

**原语质量远比数量重要。** 25 条精心设计的原语，配合状态机的组合调度，能产生数百种不同的动作序列。推荐先用参数化生成器批量产出 50+，在仿真中快速筛选保留最好的 25-35 条。

---

## 5. 层2：编舞调度器

### 5.1 Phrase Template 编排（替代纯马尔可夫状态机）

> **改进说明**：原设计使用纯概率马尔可夫状态机，每 4 小节随机转移。实际效果容易出现
> "乱切"——缺乏结构感。真正的舞蹈有 AABA 之类的重复-变化模式。
>
> 新方案：用 **Phrase Template + 能量调制** 替代纯概率采样。在段落开始时选一个 8 小节模板，
> 沿模板执行，同时允许能量信号在模板基础上做局部调整。这样既有编排感又不死板。

```python
from enum import Enum
import random

class DanceState(Enum):
    GROOVE  = "groove"   # 持续律动，低能量
    HIT     = "hit"      # 每拍强调，高能量
    FLOW    = "flow"     # 长流动，舒缓段落
    FILL    = "fill"     # 段落边界特殊处理
    FREEZE  = "freeze"   # 静止保持

# ── Phrase Templates（8 小节一组）──
# 每个模板定义一个小段落的"编排骨架"
PHRASE_TEMPLATES = {
    "buildup": [
        DanceState.GROOVE, DanceState.GROOVE,
        DanceState.GROOVE, DanceState.HIT,
        DanceState.HIT,    DanceState.HIT,
        DanceState.HIT,    DanceState.FREEZE,
    ],
    "chill": [
        DanceState.FLOW,   DanceState.FLOW,
        DanceState.GROOVE, DanceState.GROOVE,
        DanceState.FLOW,   DanceState.FLOW,
        DanceState.GROOVE, DanceState.GROOVE,
    ],
    "hype": [
        DanceState.HIT,    DanceState.HIT,
        DanceState.GROOVE, DanceState.HIT,
        DanceState.HIT,    DanceState.HIT,
        DanceState.FREEZE, DanceState.HIT,
    ],
    "contrast": [
        DanceState.GROOVE, DanceState.GROOVE,
        DanceState.HIT,    DanceState.FREEZE,
        DanceState.FLOW,   DanceState.FLOW,
        DanceState.HIT,    DanceState.FREEZE,
    ],
    "groove_focus": [
        DanceState.GROOVE, DanceState.GROOVE,
        DanceState.GROOVE, DanceState.GROOVE,
        DanceState.HIT,    DanceState.GROOVE,
        DanceState.GROOVE, DanceState.HIT,
    ],
}

# 能量范围 → 倾向的模板
ENERGY_TEMPLATE_WEIGHTS = {
    "high":  {"hype": 0.5, "buildup": 0.3, "contrast": 0.2},
    "mid":   {"buildup": 0.3, "contrast": 0.3, "groove_focus": 0.4},
    "low":   {"chill": 0.6, "groove_focus": 0.3, "contrast": 0.1},
}


class ChoreographyStateMachine:
    def __init__(self):
        self.state         = DanceState.GROOVE
        self.bar_count     = 0
        self.beat_count    = 0

        # Phrase Template 相关
        self.current_template: list[DanceState] = []
        self.template_idx  = 0
        self.last_template_name = ""

    def tick(self, bar_pos: int, energy: float,
             section_changed: bool) -> DanceState:
        """
        每个节拍调用一次
        bar_pos: 0-3，当前 bar 内第几拍
        energy:  [0,1] 当前音乐能量
        """
        if bar_pos == 0:
            self.bar_count += 1

        # ── 段落切换：强制 FILL + 重选模板 ──
        if section_changed:
            self.state = DanceState.FILL
            self._select_template(energy)
            return self.state

        # ── 模板推进（每小节在 downbeat 时前进一步）──
        if bar_pos == 0 and self.current_template:
            if self.template_idx < len(self.current_template):
                self.state = self.current_template[self.template_idx]
                self.template_idx += 1
            else:
                # 模板用完，重选
                self._select_template(energy)
                self.state = self.current_template[0]
                self.template_idx = 1

        # ── 没有模板时（初始状态），先选一个 ──
        if not self.current_template:
            self._select_template(energy)
            self.state = self.current_template[0]
            self.template_idx = 1

        # ── 能量强制覆盖（保留实时响应能力）──
        if energy > 0.85 and self.state in (DanceState.GROOVE, DanceState.FLOW):
            self.state = DanceState.HIT
        if energy < 0.2 and self.state == DanceState.HIT:
            self.state = DanceState.GROOVE

        return self.state

    def _select_template(self, energy: float):
        """根据当前能量水平选择一个 phrase template"""
        if energy > 0.65:
            level = "high"
        elif energy > 0.35:
            level = "mid"
        else:
            level = "low"

        weights_map = ENERGY_TEMPLATE_WEIGHTS[level]

        # 避免连续选同一模板
        candidates = {k: v for k, v in weights_map.items()
                      if k != self.last_template_name}
        if not candidates:
            candidates = weights_map

        names   = list(candidates.keys())
        weights = [candidates[n] for n in names]
        chosen  = random.choices(names, weights=weights)[0]

        self.current_template = PHRASE_TEMPLATES[chosen]
        self.template_idx = 0
        self.last_template_name = chosen
```

### 5.2 动作选择器

```python
from collections import deque

class MotionSelector:
    def __init__(self, library: list[MotionPrimitive]):
        self.library = library
        self.recent  = deque(maxlen=6)   # 防止重复：最近 6 个不再选

    def select(self, state: DanceState, bar_pos: int,
               energy: float) -> MotionPrimitive:

        beat_map = {0: "downbeat", 1: "beat2", 2: "beat3", 3: "beat4"}
        target_pos = beat_map[bar_pos]

        # 第一轮：严格过滤（类型 + 拍位 + 能量 + 不重复）
        candidates = [
            p for p in self.library
            if p.type.value == state.value
            and (p.beat_pos == target_pos or p.beat_pos == "downbeat")
            and self._energy_match(p.energy, energy)
            and p.id not in self.recent
        ]

        # 第二轮放宽：去掉能量限制
        if not candidates:
            candidates = [
                p for p in self.library
                if p.type.value == state.value
                and p.id not in self.recent
            ]

        # 最终兜底
        if not candidates:
            candidates = self.library

        weights = [p.weight for p in candidates]
        chosen  = random.choices(candidates, weights=weights)[0]
        self.recent.append(chosen.id)
        return chosen

    def _energy_match(self, prim_energy: str, actual: float) -> bool:
        return (
            (prim_energy == "low"  and actual < 0.40) or
            (prim_energy == "mid"  and 0.30 < actual < 0.75) or
            (prim_energy == "high" and actual > 0.60)
        )
```

### 5.3 时间调度器

这是"卡点"的实现核心：

```python
import time
from dataclasses import dataclass

PREP_TIME = 0.12   # 提前 120ms 开始（根据实测舵机响应调整）

@dataclass
class ScheduledMove:
    primitive:  MotionPrimitive
    start_time: float   # 何时开始执行（绝对时间）
    beat_time:  float   # 对应的节拍时间（调试用）
    beat_dur:   float   # 一拍的时长（秒）

class MotionScheduler:
    def __init__(self, beat_clock: BeatClock):
        self.clock = beat_clock
        self.queue: list[ScheduledMove] = []

    def schedule_for_beat(self, beat_time: float,
                           primitive: MotionPrimitive):
        """
        把动作排进队列，使其在 beat_time 时刻精确抵达重音帧
        """
        beat_dur = self.clock.beat_period
        dur_map  = {"half": 0.5, "one": 1.0, "two": 2.0}
        total_dur = dur_map[primitive.duration] * beat_dur

        # 重音帧的归一化位置（通常是第1帧，约 30%）
        accent_norm = primitive.frame_times[1]

        # 计算动作开始时间（往前推）
        start_time = beat_time - accent_norm * total_dur - PREP_TIME
        start_time = max(start_time, time.time() + 0.005)  # 不往过去调度

        self.queue.append(ScheduledMove(
            primitive=primitive,
            start_time=start_time,
            beat_time=beat_time,
            beat_dur=beat_dur,
        ))
        self.queue.sort(key=lambda m: m.start_time)

    def pop_ready(self) -> list[ScheduledMove]:
        """返回所有已到执行时间的动作"""
        now = time.time()
        ready   = [m for m in self.queue if m.start_time <= now]
        pending = [m for m in self.queue if m.start_time >  now]
        self.queue = pending
        return ready
```

---

## 6. 层3：执行层

### 6.1 基础律动层（持续运行）

> **新增机制**：当前 GROOVE 原语是一段一段独立执行的，间隙里机器人会完全静止。
> 真正的律动感来自一个**持续的、不间断的基础振荡**，局部动作叠加在上面。
> 这个持续的"呼吸"让机器人在不执行任何原语的间隙也不静止，
> 视觉上大幅提升"活着"的感觉。

```python
class BaseOscillation:
    """
    持续正弦律动，叠加在所有动作输出之上
    模拟"身体呼吸感"，让机器人永远不会完全静止
    """
    def __init__(self, amplitude: float = 5.0):
        self.amplitude = amplitude
        self.enabled   = True

    def get_offset(self, t: float, bpm: float) -> np.ndarray:
        """
        返回 6 个关节的正弦偏移量
        t:   当前时间（秒）
        bpm: 当前 BPM
        """
        if not self.enabled:
            return np.zeros(6, dtype=np.float32)

        freq = bpm / 60.0  # 与节拍同频
        phase = 2 * np.pi * freq * t
        a = self.amplitude

        # q1(下臂俯仰) 和 q5(头部俯仰) 做微小上下起伏
        # q0(底座) 做极微的左右晃动（半频）
        return np.array([
            a * 0.15 * np.sin(phase * 0.5),  # q0: 底座微晃（半频）
            a * 1.0  * np.sin(phase),          # q1: 下臂起伏
            0,                                  # q2: 不动
            a * 0.3  * np.sin(phase),           # q3: 上臂微随
            0,                                  # q4: 不动
            a * 0.6  * np.sin(phase),           # q5: 头部跟随
        ], dtype=np.float32)
```

### 6.2 增量动作执行器（含律动叠加和姿态漂移校正）

使用增量角度的关键好处：任何动作都可以从当前关节位置开始，无需先回中性，动作之间自然衔接。

> **⚠️ 已知风险与缓解：增量角度的累积偏差**
>
> 增量角度依赖 `joint_reader.get_q()` 精确读回当前角度。但廉价舵机（Feetech 等）的
> 位置反馈精度有限，长时间运行后基础姿态可能逐渐偏移，关节在不知不觉中靠近限位。
>
> **缓解方案**：在空闲时（无动作执行）缓慢回归中性姿态（soft homing），
> 每个控制周期将当前位置向中性方向移动微小量。

```python
import numpy as np
import time

# 关节安全限位（度）
Q_MIN = np.array([-90, -20, -50, -60, -45, -30], dtype=np.float32)
Q_MAX = np.array([ 90,  70,  50,  60,  45,  50], dtype=np.float32)
Q_NEUTRAL = (Q_MIN + Q_MAX) / 2.0  # 中性姿态

# 空闲时回归中性的速率（度/步）
SOFT_HOMING_RATE = 0.05

class DanceExecutor:
    """
    维护当前关节位置，把增量原语转换成绝对轨迹并执行
    含基础律动叠加和姿态漂移校正
    """
    def __init__(self, joint_reader, joint_writer, dt=0.01, bpm=120.0):
        self.reader      = joint_reader   # 读取当前关节角度 → np.ndarray (6,)
        self.writer      = joint_writer   # 写入目标角度
        self.dt          = dt
        self.bpm         = bpm

        self.active_move:      ScheduledMove | None = None
        self.move_start_time:  float = 0.0
        self.q_base:           np.ndarray = np.zeros(6)  # 动作开始时的基础姿态

        self.oscillation = BaseOscillation(amplitude=5.0)
        self.idle_frames = 0  # 空闲帧计数

    def update_bpm(self, bpm: float):
        self.bpm = bpm

    def start_move(self, scheduled: ScheduledMove):
        """开始执行一个新动作（直接从当前位置出发）"""
        self.active_move     = scheduled
        self.move_start_time = time.time()
        self.q_base          = self.reader.get_q().copy()
        self.idle_frames     = 0

    def step(self):
        """每 10ms 调用一次"""
        now = time.time()

        # ── 基础律动（持续叠加）──
        osc_offset = self.oscillation.get_offset(now, self.bpm)

        if self.active_move is not None:
            prim     = self.active_move.primitive
            beat_dur = self.active_move.beat_dur
            dur_map  = {"half": 0.5, "one": 1.0, "two": 2.0}
            total    = dur_map[prim.duration] * beat_dur

            elapsed = now - self.move_start_time
            t_norm  = min(1.0, elapsed / total)   # [0, 1]

            # 插值增量
            q_delta  = self._interpolate(prim, t_norm)
            q_target = self.q_base + q_delta + osc_offset

            if t_norm >= 1.0:
                self.active_move = None
        else:
            # ── 空闲时：仅基础律动 + 缓慢回归中性 ──
            self.idle_frames += 1
            q_current = self.reader.get_q()

            # Soft homing：缓慢回归中性，防止累积漂移
            homing = np.zeros(6, dtype=np.float32)
            if self.idle_frames > 100:  # 1秒空闲后开始回归
                diff = Q_NEUTRAL - q_current
                homing = np.clip(diff, -SOFT_HOMING_RATE, SOFT_HOMING_RATE)

            q_target = q_current + osc_offset + homing

        # 安全裁剪
        q_target = np.clip(q_target, Q_MIN, Q_MAX)
        self.writer.set_q(q_target)

    def _interpolate(self, prim: MotionPrimitive, t_norm: float) -> np.ndarray:
        """在关键帧序列中线性插值增量"""
        frames = np.array(prim.delta_frames, dtype=np.float32)  # (K, 6)
        times  = prim.frame_times

        for i in range(len(times) - 1):
            if times[i] <= t_norm <= times[i+1]:
                alpha = (t_norm - times[i]) / (times[i+1] - times[i] + 1e-9)
                return (1 - alpha) * frames[i] + alpha * frames[i+1]

        return frames[-1]
```

### 6.3 动作衔接

当新动作到来时，旧动作可能还没结束。策略：**直接切换，以当前实际位置为新动作的基础**。

```python
def start_move(self, scheduled: ScheduledMove):
    # 无论旧动作是否完成，直接用当前位置作为新动作起点
    # 这保证了过渡的平滑性——新动作总是从当前实际状态出发
    self.active_move     = scheduled
    self.move_start_time = time.time()
    self.q_base          = self.reader.get_q().copy()  # 实时读取
```

---

## 7. 主循环

### 7.1 离线预分析主路径（推荐）

```python
def run_dance_offline(audio_path: str, joint_reader, joint_writer):
    """
    离线分析 + 实时执行的主循环（推荐路径）
    精度最高、实现最简
    """
    import sounddevice as sd
    import soundfile as sf

    # ── 离线分析 ──
    analysis = OfflineMusicAnalysis(audio_path)
    print(f"歌曲分析完成: {analysis.tempo:.1f} BPM, "
          f"{len(analysis.beat_times)} 个节拍, "
          f"{len(analysis.segments)} 个段落")

    state_machine = ChoreographyStateMachine()
    selector      = MotionSelector(PRIMITIVE_LIBRARY)
    executor      = DanceExecutor(joint_reader, joint_writer,
                                  bpm=analysis.tempo)

    # ── 开始播放 ──
    data, sr = sf.read(audio_path)
    play_start = time.time()
    sd.play(data, samplerate=sr)

    last_beat_idx = -1

    try:
        while True:
            now = time.time()
            play_time = now - play_start

            info = analysis.get_beat_info(play_time)
            if info is None:
                break  # 歌曲结束

            beat_time = info["beat_time"]
            beat_idx  = analysis._current_idx

            # 每个新节拍触发一次编舞决策
            if beat_idx != last_beat_idx:
                last_beat_idx = beat_idx

                state = state_machine.tick(
                    info["bar_position"],
                    info["energy"],
                    info["section_changed"],
                )
                prim = selector.select(
                    state,
                    info["bar_position"],
                    info["energy"],
                )

                # 计算提前启动时间
                dur_map = {"half": 0.5, "one": 1.0, "two": 2.0}
                total_dur = dur_map[prim.duration] * info["beat_period"]
                accent_norm = prim.frame_times[1]
                move_start = beat_time - accent_norm * total_dur - PREP_TIME
                move_start = max(move_start, now + 0.005)

                # 排入调度
                scheduled = ScheduledMove(
                    primitive=prim,
                    start_time=move_start,
                    beat_time=beat_time,
                    beat_dur=info["beat_period"],
                )

                # 到时间则执行
                if move_start <= now:
                    executor.start_move(scheduled)

            # 控制器步进（100 Hz）
            executor.step()
            time.sleep(0.01)

    finally:
        sd.stop()
```

### 7.2 实时音频流主路径（备选）

```python
def run_dance_realtime(audio_stream, joint_reader, joint_writer):
    """实时音频流主循环（用于未知音频源场景）"""
    beat_clock    = BeatClock()
    energy_anal   = EnergyAnalyzer()
    section_det   = SectionDetector()
    state_machine = ChoreographyStateMachine()
    selector      = MotionSelector(PRIMITIVE_LIBRARY)
    scheduler     = MotionScheduler(beat_clock)
    executor      = DanceExecutor(joint_reader, joint_writer)

    last_bar_pos = -1

    for audio_chunk in audio_stream:
        # 层1：音乐分析
        beats_this_frame = beat_clock.process_frame(audio_chunk)
        energy           = energy_anal.process(audio_chunk)
        section_changed  = section_det.process(energy)
        bar_pos          = beat_clock.bar_position()

        # 更新执行器的 BPM
        executor.update_bpm(beat_clock.bpm)

        # 每拍触发一次编舞决策
        beat_triggered = (bar_pos != last_bar_pos or bool(beats_this_frame))
        if beat_triggered:
            last_bar_pos = bar_pos

            # 层2：状态机 → 动作选择 → 时间调度
            state = state_machine.tick(bar_pos, energy, section_changed)
            prim  = selector.select(state, bar_pos, energy)

            # 为即将到来的节拍调度动作
            if beat_clock.upcoming_beats:
                next_beat = beat_clock.upcoming_beats[0]
                scheduler.schedule_for_beat(next_beat, prim)

        # 层3：执行到时的动作
        for ready_move in scheduler.pop_ready():
            executor.start_move(ready_move)

        # 控制器步进（100 Hz）
        executor.step()
```

---

## 8. Groove 感：让机器人不像节拍器

只卡节拍是不够的——完全机械地卡在节拍上的动作听起来正确但看起来像节拍器。街舞的"律动感"来自两个额外机制。

### 8.1 微时值扰动（Micro-timing）

对重音帧时间施加小的随机偏移：

```python
def apply_groove(frame_times: list[float],
                 groove_style: str = "tight") -> list[float]:
    """
    groove_style:
      "tight"    → 略微提前（-）= 紧张感，街舞风格
      "laid_back" → 略微延迟（+）= 放松感，R&B 风格
      "neutral"  → 随机双向
    """
    GROOVE_AMT = 0.04   # 最大扰动量（一拍的 4%）

    if groove_style == "tight":
        jitter = np.random.uniform(-GROOVE_AMT, -GROOVE_AMT * 0.1)
    elif groove_style == "laid_back":
        jitter = np.random.uniform(GROOVE_AMT * 0.1, GROOVE_AMT)
    else:
        jitter = np.random.uniform(-GROOVE_AMT, GROOVE_AMT * 0.5)

    new_times = frame_times.copy()
    new_times[1] = float(np.clip(frame_times[1] + jitter, 0.10, 0.50))
    return new_times
```

在执行时应用：

```python
# 在 MotionSelector.select() 返回后，调用前
frame_times_grooved = apply_groove(prim.frame_times, groove_style="tight")
# 替换原语的 frame_times 再调度
```

### 8.2 身体波（Body Wave）

不同关节使用略微不同的时间延迟，模拟"能量从底部向上传导"：

> **⚠️ 注意：快节拍下需降低强度**
>
> 当 BPM > 130 时，75ms 的延迟相对于一拍时长（~460ms）占比高达 16%，
> 头部动作会显得明显迟钝而非"波浪感"。建议根据 BPM 动态缩放延迟量。

```python
# 各关节相对于 q0 的延迟（毫秒）— 基准值（BPM=100 时）
BODY_WAVE_BASE_DELAYS_MS = np.array([0, 15, 30, 45, 60, 75], dtype=np.float32)

def get_body_wave_delays(bpm: float) -> np.ndarray:
    """根据 BPM 动态缩放 body wave 延迟，快节拍时减弱"""
    # BPM 100 时使用基准值，BPM 越高延迟越小
    scale = min(1.0, 100.0 / max(bpm, 60.0))
    return BODY_WAVE_BASE_DELAYS_MS * scale

def apply_body_wave(t_norm: float, total_dur: float,
                    frame_times: list[float],
                    frames: np.ndarray,
                    bpm: float = 120.0) -> np.ndarray:
    """
    对每个关节使用略微不同的时间进行插值
    t_norm: 当前归一化时间 [0, 1]
    """
    delays_ms = get_body_wave_delays(bpm)
    delays_norm = delays_ms / 1000.0 / total_dur  # 转为归一化单位
    result = np.zeros(6, dtype=np.float32)

    for j in range(6):
        # 关节 j 的等效时间（略微超前）
        t_j = np.clip(t_norm - delays_norm[j], 0.0, 1.0)

        # 在该关节的时间上插值
        for i in range(len(frame_times) - 1):
            if frame_times[i] <= t_j <= frame_times[i+1]:
                alpha = (t_j - frame_times[i]) / (frame_times[i+1] - frame_times[i] + 1e-9)
                result[j] = (1 - alpha) * frames[i][j] + alpha * frames[i+1][j]
                break
        else:
            result[j] = frames[-1][j]

    return result
```

### 8.3 groove 参数的调优

| 音乐风格 | groove_style | BODY_WAVE 强度 |
|---|---|---|
| Hip-hop | tight | 强（60-80ms @ BPM≤100） |
| R&B / Soul | laid_back | 中（30-50ms） |
| EDM / Electronic | neutral | 弱（0-20ms） |
| Jazz | tight | 中（30-50ms） |

---

## 9. LLM 的角色

在这个系统里，**LLM 只在一个地方发挥作用：离线生成原语库的候选动作**。

LLM 完全不在实时路径上，不需要 API 延迟的考虑。

### 9.1 用 LLM 生成原语的 Prompt 格式

```
台灯机器人有6个关节：
q0=底座旋转 [-90,+90]°   q1=下臂俯仰 [-20,+70]°   q2=中段弯曲 [-50,+50]°
q3=上臂俯仰 [-60,+60]°   q4=头部左右 [-45,+45]°   q5=头部俯仰 [-30,+50]°

请生成一个「downbeat HIT」动作原语，要求：
- 时值：1拍（total_duration = 1 × beat_period）
- 重音帧在约 30% 位置（快速到达，突然感）
- 重音幅度明显（15-25°），不含糊
- 末尾回到起始姿态
- 使用增量表示（相对于起始姿态的角度差 Δq）

JSON 格式：
{
  "delta_frames": [[Δq0,Δq1,Δq2,Δq3,Δq4,Δq5], ...],
  "frame_times":  [0.0, 0.30, 0.65, 1.0],
  "tags":         ["energetic", "upward"]
}
```

### 9.2 LLM 生成后的人工审核流程

1. LLM 生成 5-10 个候选
2. 在仿真器或可视化工具里检查角度合理性
3. 上机实测，观察视觉效果
4. 调整 `frame_times` 中重音帧的位置（这对卡点感影响最大）
5. 调整 `weight` 字段控制被选中的频率

> **建议**：LLM 生成适合做"方向探索"——快速生成大量候选，找到人类设计师没想到的动作方向。
> 但最终上机的精调仍需人工完成。推荐结合 4.3 的参数化生成器，
> 用 LLM 提供"方向灵感"，用生成器输出工程上精确的原语。

---

## 10. 工程实施路径（快速推进版）

> **设计思路**：目标是"快速实现高质量跳舞效果"。以下路径比原计划（4周）更激进，
> 假设硬件和舵机通信已就绪，核心逻辑可在 **7-10 天**内达到演示级效果。

### 第 1 步（1-2 天）：卡点基础 — "能踩上拍"

**目标：** 能接收音乐，能执行固定关键帧序列，能安全运动，能看到明确的"卡点"效果。

- 用 `librosa` 离线分析一首测试歌曲，拿到精确 beat timestamps
- 实现 `DanceExecutor`（增量插值 + 安全裁剪 + 基础律动层）
- 手工设计 3-5 条 HIT 原语（只需最基础的 up/forward/twist）
- 调试 `PREP_TIME`：用视觉校准——逐帧比对机器人重音帧到达时刻与 click 对齐

**视觉校准方法**（替代纯肉眼调参）：
```bash
# 1. 播放节拍器音频 + 录制机器人视频
# 2. 用 ffmpeg 提取视频帧和音频波形
ffmpeg -i robot_dance.mp4 -vf "fps=30" frames/%04d.png
ffmpeg -i robot_dance.mp4 -ac 1 -ar 44100 audio.wav
# 3. 找到 click 时刻和动作极值帧，计算时间差
# 4. 时间差 = 需要补偿的 PREP_TIME 修正量
```

**验收：** 在固定 BPM 音乐下，能看到动作在节拍上精确到达重音姿态，间隙有基础律动。

### 第 2 步（2-3 天）：多样性 — "不重复"

**目标：** 参数化批量生成原语，状态机驱动动作切换，有基本多样性。

- 用 `batch_generate_library()` 批量生成 50+ 条候选原语
- 在仿真/实机中快速筛选，保留 20-25 条效果最好的
- 实现 `ChoreographyStateMachine`（phrase template 版本，先用 2-3 个模板）
- 实现 `MotionSelector`（带防重复）
- 实现完整的调度 → 执行管线

**验收：** 随机音乐播放 2 分钟，动作不机械重复，卡点保持稳定。

### 第 3 步（2-3 天）：质感 — "像在跳舞"

**目标：** 加入段落感知、groove 机制、body wave，从"卡点机器"变成"有舞感"。

- 完善 phrase template（加到 5+ 种）
- 接入离线段落检测（频谱聚类）
- 实现 micro-timing（`apply_groove`）
- 实现 body wave（BPM 自适应版本）
- 精调原语库的 `weight`（好看的提高权重）

**验收：** 音乐 chorus 时动作明显变强，verse 时变柔和，有情绪变化感。动作有"律动"而非"机械"。

### 第 4 步（2-3 天）：打磨 — "可以演示"

- 针对 3-5 首不同风格的歌曲分别调优 groove 参数
- 加入实时音频流支持（aubio 路径）作为备选
- 加入更多 FREEZE 和 FLOW 原语增加戏剧性
- 用 LLM 探索更多"创意动作方向"，补充到原语库
- 考虑加入简单的 LED 灯效同步（如果硬件支持）

### 后续扩展方向

- 根据歌曲 key / 音调信息（`librosa.key`）自动选择 groove_style
- 自动分析音乐风格（hip-hop / EDM / R&B）并切换整套参数
- 多机器人协同编舞（共享同一首歌的 beat_times，各自偏移相位）
- 观众反馈闭环：通过摄像头检测观众反应，动态调整能量级别

---

## 附录A：关键超参数参考

| 参数 | 推荐初始值 | 说明 |
|---|---|---|
| `PREP_TIME` | 0.10~0.15s | 实测调整，舵机越慢值越大 |
| `accent_norm`（重音帧位置）| 0.28~0.35 | 偏小=更"突然"，偏大=更"流畅" |
| `groove_amount` | 0.03~0.05 | 太大会乱，太小没效果 |
| `recent` deque 长度 | 6 | 防重复窗口，原语少时可减小 |
| 状态转换周期 | 按 phrase template | 8 小节一组，能量可局部覆盖 |
| BPM 滑动平均系数 | 0.85 | 越大越稳定但跟不上节奏变化（仅实时路径） |
| 控制频率 | 100 Hz | 与舵机刷新率匹配 |
| 基础律动振幅 | 5° | 太大喧宾夺主，太小看不出来 |
| Body wave BPM 缩放 | scale = min(1, 100/BPM) | 快节拍时自动减弱 |
| Soft homing 启动延迟 | 100 帧 (1s) | 空闲超过此时间开始回归中性 |
| Soft homing 速率 | 0.05°/步 | 越大回归越快但可能看出突变 |

## 附录B：调试技巧

**卡点调试（推荐视觉校准法）：** 播放节拍器 + 录制视频 → 用 ffmpeg 逐帧提取 → 比对 click 时刻和动作极值帧 → 计算时间差作为 `PREP_TIME` 修正量。比肉眼调更准确、可重复。

**动作多样性调试：** 打印每次选择的动作 ID 和当前 phrase template 到终端，观察是否有明显的重复模式，调整 `recent` 窗口大小和各原语的 `weight`。

**能量响应调试：** 在能量包络图上标注状态机的状态切换点和 phrase template 边界，确认高能量段对应 HIT/FREEZE，低能量段对应 GROOVE/FLOW。

**累积漂移调试：** 记录每次动作执行后的 `q_base` 值，画出关节位置的时间序列，观察是否有持续的单方向偏移。如有，检查 soft homing 是否生效。

## 附录C：参考工具

| 工具 | 用途 |
|---|---|
| `aubio` | 实时节拍检测（备选路径） |
| `librosa` | 离线音乐分析（主路径：BPM、段落、能量、音调） |
| `pyaudio` / `sounddevice` | 音频流接口 |
| Dynamixel SDK / Feetech SDK | 舵机控制接口 |
| `numpy` | 所有数值计算（无需 PyTorch） |
| `ffmpeg` | 视觉校准（视频逐帧分析） |

## 附录D：v1 → v2 变更记录

| 变更项 | v1 设计 | v2 改进 | 理由 |
|---|---|---|---|
| 音乐分析主路径 | 实时 aubio 检测 | 离线 librosa 预分析 | 精度 100%，复杂度降一半 |
| bar_position | 浮点除法推算 | 全局计数器取模 + 漏检补偿 | 防累积漂移 |
| 段落检测 | 单帧能量突变 | 频谱聚类（离线）/ 长短期均值差（实时） | 减少鼓 fill 误触发 |
| 状态机 | 纯马尔可夫概率 | Phrase Template + 能量调制 | 增加编排结构感 |
| 原语生成 | 手写 delta_frames | 参数化生成器批量产出 | 效率提升 10 倍 |
| 基础律动 | 无 | 正弦基础振荡持续叠加 | 消除间隙静止感 |
| 姿态漂移 | 无处理 | Soft homing 空闲回归 | 防长时间运行偏移 |
| Body wave | 固定延迟 | BPM 自适应缩放 | 快节拍下不迟钝 |
| PREP_TIME 调试 | 肉眼看着调 | 视频逐帧校准法 | 可重复、精确 |
| 实施周期 | 4 周 | 7-10 天 | 离线分析 + 参数化生成加速 |
