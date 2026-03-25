# 情绪表达机器人运动生成：LLM + RL 系统设计文档（v2）

> 适用场景：台灯机器人、桌面机械臂等少自由度非拟人平台，目标是纯情绪表达（卖萌、跳舞），不要求到达特定末端位姿。

---

## 目录

1. [背景与核心思路](#1-背景与核心思路)
2. [理论基础：LMA 运动质量框架](#2-理论基础lma-运动质量框架)
3. [LLM Prompt 设计](#3-llm-prompt-设计)
4. [LLM 输出接口设计](#4-llm-输出接口设计)
5. [下游处理链路](#5-下游处理链路)
6. [参数化模板 Baseline（快速出效果）](#6-参数化模板-baseline快速出效果)
7. [离线预生成与缓存池](#7-离线预生成与缓存池)
8. [RL 框架设计](#8-rl-框架设计)
9. [动作序列编排](#9-动作序列编排)
10. [完整数据流总览](#10-完整数据流总览)
11. [可行性评估与风险分析](#11-可行性评估与风险分析)
12. [工程实施路径（修订版）](#12-工程实施路径修订版)

---

## 1. 背景与核心思路

### 1.1 问题定义

目标：给定一个情绪标签（如"开心"、"悲伤"、"好奇"）和强度值，让机器人生成对应的表达性运动序列。

约束条件：
- 不要求末端到达特定位姿（纯表达性场景）
- 不能修改 LLM 的模型权重
- 必须保证硬件安全（不超关节限位、不超速）

### 1.2 技术路线选择

**为什么用 LMA 参数化而非直接生成关节角度**

LLM 没有关节空间的几何直觉，直接让它猜角度数字质量不稳定。但 LLM 对两件事理解很好：
- 语言描述的运动感（"轻盈地向上弹起"）
- LMA（Laban Movement Analysis）词汇——因为训练语料里有大量舞蹈、动作描述文本

因此 prompt 策略的核心是：**让 LLM 先推理运动意图（语义层），再把意图转化为约束良好的角度数字**。

**为什么不能把"功能性"和"表达性"顺序解耦**

传统做法"先规划最优轨迹，再叠加情绪风格"在工程上是错的：

- 功能性约束（终点精度、避障）活在关节空间
- 表达性参数（弧线、弹跳）活在笛卡尔空间
- 两个空间从未在同一个优化问题里见过面

开心的台灯在"转向用户"时，不只是"快速"到达目标——它会走一条带有弹跳感的弧形路径，而这条路径在纯功能优化下根本不会出现。**情绪改变的是轨迹的形状，而不只是速度曲线。**

但对于纯表达性场景（无功能终点约束），这个矛盾消失了——所有约束都退化为安全裁剪。

### 1.3 整体架构

```
情绪输入
    ↓
┌──────────────────────────────────────────────────────────┐
│  [参数化模板]  ←──── 快速路径（Day 0 即可用）            │
│       │                                                  │
│       ├── OR ──→ [缓存池] ← 离线预生成（零实时延迟）     │
│       │                                                  │
│       ├── OR ──→ [Prompt 工程] ← 示例数据库              │
│       │              ↓                                   │
│       │          LLM（冻结）                              │
│       │              ↓                                   │
│       │          JSON 输出 → CoT 字段丢弃                │
└───────┴──────────────────────────────────────────────────┘
    ├── shape: f1, f2 (中间两帧的关节角度)
    └── timing: duration, accel_ratio, asymmetry
         ↓                        ↓
[残差修正器 f_θ]          [插值器]
（可训练，~2K参数）         ↓
         ↓             [安全裁剪]
    q_final (4,6)          ↓
         └─────────→ [关节控制器] → 舵机
                          ↓
                       [奖励评估]
                          ↓
                    [RL/CMA-ES 更新 f_θ]
```

**关键设计原则：**
1. LLM 完全隔离在梯度图之外，只在离线/推理阶段调用，RL 只训练残差修正器这个小网络。
2. **（新增）三级动作源——模板 / 缓存池 / 实时 LLM——按延迟从低到高排列，保证任何阶段都有可用的动作输出。**

### 1.4 核心设计洞察

本系统的设计围绕三个关键洞察：

**洞察一：LMA 是 LLM 世界和关节世界之间的翻译层。** LLM 对关节角度数字没有几何直觉，但训练语料里有海量舞蹈评论和运动描述文本，所以它对 Laban 的 Effort 四维度有不错的语义理解。CoT 强制 LLM 先写 LMA 分类再输出角度，形成自我一致性约束——写下 `"time": "sudden"` 后很难再生成慢动作。

**洞察二：情绪改变的是轨迹形状，不只是速度。** 开心的台灯应该走弧形弹跳路径，这在纯功能优化下根本不存在。但在纯表达性场景（无末端位置目标）下，这个矛盾消失，所有约束退化为安全裁剪。这大幅简化了问题。

**洞察三：`asymmetry` 一个参数编码速度曲线的情绪特征。** `+1` 产生"快起慢停"（弹跳感，开心），`-1` 产生"慢起快停"（拖沓感，悲伤），`0` 是对称钟形（平静）。一个连续浮点数把不可微的离散 easing 变成 RL 可优化的参数。

---

## 2. 理论基础：LMA 运动质量框架

Laban Movement Analysis 是 Rudolf Laban 1940 年代创立的人体运动分析框架，是本系统描述"运动质感"的核心词汇。

### 2.1 Effort 四个维度

| 维度 | 两端极值 | 对应情绪（示例） | 机器人参数映射 |
|---|---|---|---|
| **Time** | Sustained（持续/慢）↔ Sudden（突发/快）| 慢=悲伤/平静，快=开心/愤怒 | `v_peak`, `accel_ratio` |
| **Weight** | Light（轻盈）↔ Strong（强力）| 轻=愉悦/温柔，重=愤怒/郑重 | `amplitude`, `jerk_scale` |
| **Flow** | Free（连贯，停不下来）↔ Bound（随时可停，克制）| Free=放松/开心，Bound=焦虑/紧张 | `smoothness`, `asymmetry` |
| **Space** | Indirect（绕弯探索）↔ Direct（直指目标）| Indirect=好奇，Direct=专注/愤怒 | `path_curve`, `via_offset` |

### 2.2 情绪与 LMA 参数的对应关系

| 情绪 | Time | Weight | Flow | Space | asymmetry |
|---|---|---|---|---|---|
| 开心 | Sudden | Light | Free | Indirect | +0.6 ~ +1.0 |
| 悲伤 | Sustained | Strong | Bound | Indirect | -0.6 ~ -1.0 |
| 愤怒 | Sudden | Strong | Bound | Direct | 0 ~ -0.3 |
| 好奇 | 中性 | Light | Free | Indirect | +0.2 ~ +0.4 |
| 平静 | Sustained | Light | Free | Direct | 0 |

### 2.3 为什么 LMA 对 LLM 有效

人类感知情绪不是靠识别"这是什么姿势"，而是靠感知运动的质感。实验证明，人看不到身体具体形状（只看一个光点在黑暗中移动）的情况下，仍能高度准确地判断情绪——靠的正是速度曲线、节奏、路径间接性这些 Effort 特征。

LLM 训练语料里有大量包含 LMA 词汇的文本（舞蹈评论、运动描述），因此它对这套词汇的理解比直接理解关节角度数字好得多。**LMA 是连接 LLM 语义世界和机器人运动控制的桥梁。**

### 2.4 LMA 量化参数表（用于奖励计算和模板设计）

> **（新增）** 将 LMA 四维度量化为可计算的物理指标，供奖励函数和参数化模板使用。

| 情绪 | v_peak (°/s) | amplitude (°RMS) | jerk (°/s³ RMS) | indirectness (弧长/直线) |
|---|---|---|---|---|
| 开心 | 180~300 | 25~40 | 低 (< 500) | 高 (> 1.5) |
| 悲伤 | 40~80 | 10~20 | 高 (> 1000) | 中 (1.2~1.5) |
| 愤怒 | 250~400 | 30~50 | 高 (> 1500) | 低 (< 1.2) |
| 好奇 | 80~150 | 15~30 | 低 (< 400) | 高 (> 1.8) |
| 平静 | 30~60 | 8~15 | 极低 (< 200) | 低 (1.0~1.2) |

```python
EMOTION_LMA_TABLE = {
    "happy":   {"v_peak": 240, "amplitude": 32, "jerk": 300,  "indirectness": 1.7},
    "sad":     {"v_peak": 60,  "amplitude": 15, "jerk": 1200, "indirectness": 1.3},
    "angry":   {"v_peak": 320, "amplitude": 40, "jerk": 1800, "indirectness": 1.1},
    "curious": {"v_peak": 110, "amplitude": 22, "jerk": 250,  "indirectness": 2.0},
    "calm":    {"v_peak": 45,  "amplitude": 10, "jerk": 100,  "indirectness": 1.05},
}
```

---

## 3. LLM Prompt 设计

### 3.1 Prompt 整体结构

```
System prompt（每次不变）
    ├── 机器人关节物理定义
    ├── 安全约束
    └── 输出格式规范（严格）

Few-shot 示例（每次检索注入，2~3条）
    └── 从高奖励示例库按 (emotion, intensity) 检索

User turn（每次变化）
    ├── Step 1：LMA 分类（强制 CoT）
    ├── Step 2：运动意图描述（自然语言）
    ├── Step 3：f1, f2 关节角度
    └── Step 4：timing 参数
```

### 3.2 System Prompt 完整文本

```
你控制一个6自由度台灯机器人，专门用于情绪表达。

## 关节定义
q0  底座旋转  [-90, +90]°   0=正前  正=右转   影响：整体朝向
q1  下臂俯仰  [-20, +70]°   正=抬起            影响：整体高低
q2  中段弯曲  [-50, +50]°   正=前弯  负=后仰   影响：躬身/挺立
q3  上臂俯仰  [-60, +60]°   正=向上            影响：头的远近
q4  头部左右  [-45, +45]°   正=右歪            影响：歪头感
q5  头部俯仰  [-30, +50]°   正=抬头            影响：注视高低

## 关键约束
- 输出必须恰好包含 f1 和 f2 两帧（共4帧中的中间两帧）
- f0（当前姿态）和 f3（结束姿态）由系统填入，你无需生成
- 相邻帧角度差不超过 55°（任意单关节）
- timing.accel_ratio + decel_ratio(由 asymmetry 推导) ≤ 0.95

## 输出格式（严格遵守，不得增减字段）
{
  "lma": {
    "time":   "sudden | sustained",
    "weight": "light  | strong",
    "flow":   "free   | bound",
    "space":  "direct | indirect"
  },
  "intent": "一句话描述运动意图",
  "shape": {
    "f1": [q0, q1, q2, q3, q4, q5],
    "f2": [q0, q1, q2, q3, q4, q5]
  },
  "timing": {
    "duration":    秒数（0.5~5.0）,
    "accel_ratio": 0.05~0.50,
    "asymmetry":   -1.0~+1.0
  }
}

## asymmetry 含义
+1.0 → 快速起步，缓慢收尾（开心弹跳感）
-1.0 → 缓慢起步，快速收尾（悲伤拖沓感）
 0.0 → 对称钟形曲线（平静感）
```

### 3.3 CoT 顺序的作用

**为什么必须按 Step 1 → 2 → 3 → 4 的顺序**

LLM 先写出 `"time": "sudden"` 这个推理，会锚定后续数字的生成——它不能再生成一个"慢慢挪动"的角度序列了，因为那和已经写下的 LMA 分类矛盾。这是一种**自我一致性约束**，比直接让 LLM 输出数字的质量高得多。

`intent` 字段（"快速向上弹起，头部左右轻晃"）同理——它迫使 LLM 在生成角度前先把运动画面想清楚。

**这些 CoT 字段在执行时被丢弃，不进入任何下游计算。它们只是引导 LLM 生成质量更高的 shape 和 timing。**

### 3.4 Few-shot 示例管理

示例数据库随时间积累，每条记录包含：

```python
{
    "emotion":    "happy",
    "intensity":  0.8,
    "f1":         [8, 55, -12, 44, -22, 32],
    "f2":         [-6, 50, -8, 40, 18, 26],
    "timing":     {"duration": 1.8, "accel_ratio": 0.12, "asymmetry": 0.65},
    "reward":     0.87,        # 来自奖励模型的评分
    "lma_cot":   "...",        # 完整的 CoT 文本，用于 few-shot 注入
}
```

检索策略：按 `(emotion, intensity)` 最近邻，取 reward 最高的 K=2~3 条。

更新策略：
- `reward > threshold`：加入数据库
- 每个 `(emotion, round(intensity, 1))` 桶最多保留 20 条，淘汰低分

```python
class ExampleDatabase:
    def retrieve(self, emotion, intensity, k=3):
        candidates = [e for e in self.examples if e["emotion"] == emotion]
        # 按 intensity 距离加权 + reward 排序
        candidates.sort(key=lambda x: -(x["reward"] - 0.1 * abs(x["intensity"] - intensity)))
        return candidates[:k]

    def add(self, entry):
        self.examples.append(entry)
        self._prune()

    def _prune(self):
        buckets = defaultdict(list)
        for e in self.examples:
            key = (e["emotion"], round(e["intensity"], 1))
            buckets[key].append(e)
        self.examples = []
        for bucket in buckets.values():
            bucket.sort(key=lambda x: -x["reward"])
            self.examples.extend(bucket[:20])
```

### 3.5 f0 / f3 的注入策略

`f0` 和 `f3` 不让 LLM 生成，由调用方注入：

- **f0（起始帧）**：从关节编码器读取当前实际角度。这保证了动作从当前真实状态开始，不会有跳变。
- **f3（结束帧）**：系统预定义的中性姿态（如 `[0, 30, 0, 20, 0, 10]`）。所有动作结束后回到同一个"待机姿态"，保证连续动作之间衔接自然。

### 3.6 LLM 生成质量的现实预期

> **（新增）** 实际工程中需要对 LLM 的角度生成能力有清醒认知。

即使使用 LMA CoT + few-shot 示例注入，LLM 输出 6 维浮点数组本质上是在做"补全最可能的 token"，而非几何推理。实测预期：

| 指标 | 预期范围 | 说明 |
|---|---|---|
| 格式合法率 | > 95% | 通过严格 prompt + 重试可保证 |
| "合法且好看"的比例 | 30~50% | 单次生成质量不够稳定 |
| 关节间协调性 | 不稳定 | "头歪 + 身体前倾"等组合感 LLM 难把控 |

**结论：Best-of-N 不是锦上添花，而是必需品。** 单次生成的质量方差太大，必须依赖多次采样 + 自动评估来保证输出质量。

---

## 4. LLM 输出接口设计

### 4.1 输出数据结构

```python
from dataclasses import dataclass
import numpy as np

N_JOINTS = 6
K_FRAMES  = 4   # 永远是4，硬编码

@dataclass
class LLMOutput:
    """LLM 的原始输出，解析后的结构"""
    # CoT 字段 —— 只用于调试日志，不进任何下游计算
    lma_time:   str    # "sudden" | "sustained"
    lma_weight: str    # "light"  | "strong"
    lma_flow:   str    # "free"   | "bound"
    lma_space:  str    # "direct" | "indirect"
    intent:     str    # 自然语言描述

    # 下游消费字段
    f1: np.ndarray     # shape (6,)，关节角度，单位 degrees
    f2: np.ndarray     # shape (6,)

    duration:    float  # 总时长，单位秒，[0.5, 5.0]
    accel_ratio: float  # 加速段占比，[0.05, 0.50]
    asymmetry:   float  # 加减速不对称度，[-1.0, 1.0]

@dataclass
class MotionRequest:
    """调用方构造的完整请求"""
    q_current:  np.ndarray  # (6,) 当前关节读数，来自编码器
    q_rest:     np.ndarray  # (6,) 结束姿态
    emotion:    str
    intensity:  float       # [0.0, 1.0]
```

### 4.2 为什么固定 K=4 帧

| 方案 | 优点 | 缺点 |
|---|---|---|
| 可变帧数（原始设计） | LLM 自由度高 | 残差修正器输入维度不固定，无法用 MLP；RL action space 不固定 |
| **固定 K=4**（本设计） | 修正器输入永远是 (4,6)=24 维，RL 完全标准化 | LLM 表达空间略受限 |

4帧的语义是固定的：
- f0：当前姿态（系统注入）
- f1：情绪起势（LLM 生成，修正器调整）
- f2：情绪高潮/过渡（LLM 生成，修正器调整）
- f3：结束回归（系统注入）

> **（新增注记）** 4 帧的表达力有限，一个"开心"动作只有"起势 → 高潮"两个姿态，难以做出"弹一下 → 歪头 → 再弹一下"的丰富序列。后续可通过**动作序列编排**（见第 9 章）串联多个 4 帧片段来解决。

### 4.3 timing 三参数取代离散 easing

**原来的问题：** `"easing": "bounce"` 是字符串，无法对它求梯度，无法在 `bounce` 和 `sine` 之间插值，RL 无法优化。

**新设计：** 三个连续实数完整描述速度曲线形态。

```python
@dataclass
class TimingParams:
    duration:    float   # 总时长（秒）
    accel_ratio: float   # 加速段占比 [0.05, 0.50]
    asymmetry:   float   # 加减速不对称度 [-1.0, +1.0]
                         # +1 = 快起慢停（开心弹跳）
                         # -1 = 慢起快停（悲伤拖沓）
                         #  0 = 对称（平静）
```

`asymmetry` → 减速段占比的推导：

```python
def decel_ratio_from_asymmetry(accel_ratio, asymmetry):
    # asymmetry=+1 → decel = 2 × accel（减速段是加速段两倍长）
    # asymmetry=-1 → decel ≈ 0（几乎没有减速段，突然停止）
    raw = accel_ratio * (1.0 + asymmetry)
    return float(np.clip(raw, 0.05, 0.90 - accel_ratio))
```

### 4.4 输出解析 + 校验

```python
def parse_and_validate(raw_json: dict) -> LLMOutput | None:
    try:
        f1 = np.array(raw_json["shape"]["f1"], dtype=np.float32)
        f2 = np.array(raw_json["shape"]["f2"], dtype=np.float32)

        assert f1.shape == (N_JOINTS,), "f1 维度错误"
        assert f2.shape == (N_JOINTS,), "f2 维度错误"

        # 角度范围
        for name, f in [("f1", f1), ("f2", f2)]:
            for j in range(N_JOINTS):
                if not (Q_MIN[j] <= f[j] <= Q_MAX[j]):
                    raise ValueError(f"{name}[{j}]={f[j]:.1f} 超出限位 [{Q_MIN[j]}, {Q_MAX[j]}]")

        timing = raw_json["timing"]
        duration    = float(np.clip(timing["duration"],    0.5, 5.0))
        accel_ratio = float(np.clip(timing["accel_ratio"], 0.05, 0.50))
        asymmetry   = float(np.clip(timing["asymmetry"],  -1.0, 1.0))

        return LLMOutput(
            lma_time=raw_json["lma"]["time"],
            lma_weight=raw_json["lma"]["weight"],
            lma_flow=raw_json["lma"]["flow"],
            lma_space=raw_json["lma"]["space"],
            intent=raw_json.get("intent", ""),
            f1=f1, f2=f2,
            duration=duration, accel_ratio=accel_ratio, asymmetry=asymmetry
        )
    except (KeyError, AssertionError, ValueError) as e:
        log_warning(f"LLM 输出不合法: {e}，触发重生成")
        return None  # 调用方应重试（最多3次）
```

---

## 5. 下游处理链路

### 5.1 完整组装流程

```python
def assemble_motion(
    llm_out:   LLMOutput,
    request:   MotionRequest,
    corrector: ResidualCorrector,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    返回: (t_traj, q_traj, dqdt_traj)
    """

    # Step 1：组装完整 4 帧（注入 f0, f3）
    q_raw = np.stack([
        request.q_current,   # f0：当前姿态
        llm_out.f1,          # f1：LLM 生成
        llm_out.f2,          # f2：LLM 生成
        request.q_rest,      # f3：中性结束姿态
    ], axis=0)               # shape (4, 6)

    # Step 2：相邻帧角度差过大则线性收缩（保形不截断）
    MAX_FRAME_DELTA = 55.0  # degrees
    for i in range(1, K_FRAMES):
        delta = q_raw[i] - q_raw[i-1]
        max_delta = np.abs(delta).max()
        if max_delta > MAX_FRAME_DELTA:
            scale = MAX_FRAME_DELTA / max_delta
            q_raw[i] = q_raw[i-1] + delta * scale

    # Step 3：残差修正器（可选，RL 训练后才有效果）
    emotion_vec = encode_emotion(request.emotion, request.intensity)
    q_final = corrector.apply(q_raw, emotion_vec)   # (4, 6)

    # Step 4：插值 → 密集轨迹
    t_traj, q_traj = interpolate(
        q_final,
        duration=llm_out.duration,
        accel_ratio=llm_out.accel_ratio,
        asymmetry=llm_out.asymmetry,
        dt=0.01  # 100 Hz
    )

    # Step 5：速度限位（自动拉伸时间轴，不截断角度）
    t_traj, q_traj = velocity_safe_stretch(t_traj, q_traj, DQ_MAX_DEG_PER_S)

    # Step 6：求导
    dqdt_traj = np.gradient(q_traj, t_traj, axis=0)

    return t_traj, q_traj, dqdt_traj
```

### 5.2 插值器：asymmetry 的物理实现

```python
def build_speed_profile(t_norm: float, accel_ratio: float, asymmetry: float) -> float:
    """
    把均匀时间轴映射到运动进度（0→1）
    t_norm:     归一化时间 [0, 1]
    返回:       运动进度 s ∈ [0, 1]
    """
    decel_ratio  = float(np.clip(accel_ratio * (1.0 + asymmetry), 0.05, 0.90 - accel_ratio))
    plateau      = 1.0 - accel_ratio - decel_ratio

    if t_norm < accel_ratio:
        s = 0.5 * (t_norm / accel_ratio) ** 2
    elif t_norm < accel_ratio + plateau:
        s = 0.5 * accel_ratio + (t_norm - accel_ratio)
    else:
        d = t_norm - accel_ratio - plateau
        s = (0.5 * accel_ratio + plateau
             + decel_ratio
             - 0.5 * ((decel_ratio - d) / decel_ratio) ** 2 * decel_ratio)

    norm = 0.5 * accel_ratio + plateau + 0.5 * decel_ratio
    return float(np.clip(s / norm, 0.0, 1.0))


def interpolate(q_frames, duration, accel_ratio, asymmetry, dt=0.01):
    N = max(2, int(duration / dt))
    t_norm_arr = np.linspace(0, 1, N)

    q_traj = np.zeros((N, N_JOINTS))
    for idx, t_norm in enumerate(t_norm_arr):
        s = build_speed_profile(t_norm, accel_ratio, asymmetry)

        # s 在 [0,1] → 映射到 K-1 个段
        seg_f  = s * (K_FRAMES - 1)
        seg_i  = int(np.floor(seg_f))
        seg_i  = np.clip(seg_i, 0, K_FRAMES - 2)
        alpha  = seg_f - seg_i

        q_traj[idx] = (1 - alpha) * q_frames[seg_i] + alpha * q_frames[seg_i + 1]

    t_traj = np.linspace(0, duration, N)
    return t_traj, q_traj
```

### 5.3 安全裁剪：时间拉伸而非角度截断

```python
def velocity_safe_stretch(t_traj, q_traj, dq_max_deg_per_s):
    """
    如果某段速度超限，拉伸时间轴（保持动作形状），而不是截断角度（破坏动作形状）
    """
    dt_arr  = np.diff(t_traj)
    dq_arr  = np.abs(np.diff(q_traj, axis=0))             # (N-1, 6)
    dt_min  = (dq_arr / dq_max_deg_per_s).max(axis=1)     # (N-1,)
    dt_safe = np.maximum(dt_arr, dt_min * 1.05)            # 5% margin

    t_safe = np.concatenate([[0.0], np.cumsum(dt_safe)])
    return t_safe, q_traj  # 角度完全不变，只有时间轴变了
```

### 5.4 控制器接口

```python
class MotionExecutor:
    def __init__(self, t_traj, q_traj, dqdt_traj, Kp, Kd):
        self.t      = t_traj
        self.q_d    = q_traj       # (N, 6) 目标角度
        self.dqdt_d = dqdt_traj    # (N, 6) 目标速度
        self.Kp     = Kp           # (6,)   位置增益
        self.Kd     = Kd           # (6,)   速度增益

    def step(self, t_elapsed, q_actual, dqdt_actual):
        """每个控制周期（10ms）调用一次"""
        if t_elapsed >= self.t[-1]:
            return None   # 动作结束

        q_target    = np.array([np.interp(t_elapsed, self.t, self.q_d[:, j])    for j in range(N_JOINTS)])
        dqdt_target = np.array([np.interp(t_elapsed, self.t, self.dqdt_d[:, j]) for j in range(N_JOINTS)])

        torque = self.Kp * (q_target - q_actual) \
               + self.Kd * (dqdt_target - dqdt_actual)
        return torque   # (6,) 发给驱动器
```

**"编译期"与"运行期"的分离**：

- **编译期**（动作触发时，一次性，~50ms）：LLM 生成 → 解析 → 修正器 → 插值 → 安全裁剪 → 预计算轨迹数组
- **运行期**（执行过程中，每 10ms）：查表 + PD 控制，完全不依赖 LLM

LLM 的延迟只影响"发出指令到开始动"的等待时间，不影响执行流畅度。

---

## 6. 参数化模板 Baseline（快速出效果）

> **（新增章节）** 在 LLM 接入之前，用纯参数化模板即可让台灯"能动且好看"，作为 Day 0 可用的 baseline。

### 6.1 为什么需要模板层

等待 LLM 接入、prompt 调优、示例库积累需要 2~3 周。但台灯机器人应该在**第 1 周就能 demo**。参数化模板提供了：

- 零延迟的即时响应（无网络依赖）
- 作为 LLM 接入后的 fallback（API 不可用时降级到模板）
- 作为 few-shot 示例库的初始种子

### 6.2 模板数据结构

```python
import random

Q_REST = np.array([0, 30, 0, 20, 0, 10], dtype=np.float32)  # 中性待机姿态

# 每个情绪 3~5 个手工设计的原型动作
MOTION_TEMPLATES = {
    "happy": [
        {   # 弹跳抬头 + 左右晃
            "f1": [8, 55, -12, 44, -22, 32],
            "f2": [-6, 50, -8, 40, 18, 26],
            "timing": {"duration": 1.8, "accel_ratio": 0.12, "asymmetry": 0.65},
        },
        {   # 快速抬起 + 歪头
            "f1": [15, 60, -5, 50, 30, 40],
            "f2": [-10, 45, 5, 35, -25, 20],
            "timing": {"duration": 1.5, "accel_ratio": 0.15, "asymmetry": 0.8},
        },
        {   # 小幅点头 + 左右摆
            "f1": [20, 48, -3, 30, -15, 35],
            "f2": [-18, 52, 3, 28, 20, 30],
            "timing": {"duration": 1.2, "accel_ratio": 0.10, "asymmetry": 0.7},
        },
    ],
    "sad": [
        {   # 缓慢低头
            "f1": [-5, 10, 20, -20, -5, -15],
            "f2": [3, 5, 25, -25, 5, -20],
            "timing": {"duration": 3.5, "accel_ratio": 0.35, "asymmetry": -0.7},
        },
        {   # 微微侧倾 + 下垂
            "f1": [-8, 8, 15, -15, -20, -10],
            "f2": [5, 12, 18, -18, 10, -18],
            "timing": {"duration": 4.0, "accel_ratio": 0.40, "asymmetry": -0.8},
        },
    ],
    "angry": [
        {   # 快速前冲 + 低头
            "f1": [0, 40, 30, -10, 0, -20],
            "f2": [0, 35, 35, -15, 0, -25],
            "timing": {"duration": 0.8, "accel_ratio": 0.08, "asymmetry": -0.2},
        },
        {   # 猛烈左右扫视
            "f1": [40, 35, 10, 5, 0, -10],
            "f2": [-35, 35, 10, 5, 0, -10],
            "timing": {"duration": 1.0, "accel_ratio": 0.06, "asymmetry": 0.0},
        },
    ],
    "curious": [
        {   # 歪头 + 前探
            "f1": [15, 45, 15, 30, 25, 20],
            "f2": [-10, 50, 10, 25, -20, 25],
            "timing": {"duration": 2.2, "accel_ratio": 0.20, "asymmetry": 0.3},
        },
        {   # 左右张望
            "f1": [30, 40, 5, 20, 15, 15],
            "f2": [-25, 42, -5, 22, -18, 18],
            "timing": {"duration": 2.5, "accel_ratio": 0.25, "asymmetry": 0.2},
        },
    ],
    "calm": [
        {   # 微微起伏呼吸感
            "f1": [0, 35, -3, 22, 0, 12],
            "f2": [0, 28, 3, 18, 0, 8],
            "timing": {"duration": 4.0, "accel_ratio": 0.40, "asymmetry": 0.0},
        },
    ],
}
```

### 6.3 模板生成器（带 intensity 缩放和随机扰动）

```python
def template_generate(emotion: str, intensity: float) -> LLMOutput:
    """
    从模板库生成一个动作，用 intensity 缩放幅度，加随机扰动避免重复感。
    返回与 LLMOutput 接口兼容的结构。
    """
    template = random.choice(MOTION_TEMPLATES[emotion])

    # intensity 缩放：偏离中性姿态的幅度与 intensity 成正比
    f1_base = np.array(template["f1"], dtype=np.float32)
    f2_base = np.array(template["f2"], dtype=np.float32)
    f1 = Q_REST + (f1_base - Q_REST) * np.clip(intensity, 0.2, 1.0)
    f2 = Q_REST + (f2_base - Q_REST) * np.clip(intensity, 0.2, 1.0)

    # 随机扰动（幅度与 intensity 成正比，低 intensity 时扰动小）
    noise_scale = 3.0 * intensity
    f1 += np.random.randn(N_JOINTS) * noise_scale
    f2 += np.random.randn(N_JOINTS) * noise_scale

    # 裁剪到关节限位
    f1 = np.clip(f1, Q_MIN, Q_MAX)
    f2 = np.clip(f2, Q_MIN, Q_MAX)

    # timing 也根据 intensity 微调
    t = template["timing"]
    duration = t["duration"] * (1.1 - 0.3 * intensity)  # intensity 高 → 稍快
    accel_ratio = t["accel_ratio"]
    asymmetry = t["asymmetry"] * intensity

    return LLMOutput(
        lma_time="sudden" if emotion in ("happy", "angry") else "sustained",
        lma_weight="light" if emotion in ("happy", "curious", "calm") else "strong",
        lma_flow="free" if emotion in ("happy", "curious", "calm") else "bound",
        lma_space="indirect" if emotion in ("happy", "curious", "sad") else "direct",
        intent=f"[模板生成] {emotion} @ {intensity:.1f}",
        f1=f1, f2=f2,
        duration=float(np.clip(duration, 0.5, 5.0)),
        accel_ratio=float(np.clip(accel_ratio, 0.05, 0.50)),
        asymmetry=float(np.clip(asymmetry, -1.0, 1.0)),
    )
```

### 6.4 模板作为 few-shot 种子

模板生成的动作经过实际执行和人工评估后，高质量的样本直接注入 few-shot 示例库：

```python
def seed_example_database(example_db: ExampleDatabase, n_per_emotion=10):
    """用模板生成种子数据，人工筛选后入库"""
    for emotion in EMOTION_INDEX:
        for i in range(n_per_emotion):
            intensity = random.uniform(0.3, 1.0)
            llm_out = template_generate(emotion, intensity)
            # 执行并评估
            t, q, dq = assemble_motion(llm_out, make_request(emotion, intensity), identity_corrector)
            reward = compute_reward(q, dq, t, emotion, intensity)
            if reward > REWARD_THRESHOLD:
                example_db.add({
                    "emotion": emotion, "intensity": intensity,
                    "f1": llm_out.f1.tolist(), "f2": llm_out.f2.tolist(),
                    "timing": {"duration": llm_out.duration,
                               "accel_ratio": llm_out.accel_ratio,
                               "asymmetry": llm_out.asymmetry},
                    "reward": reward,
                    "lma_cot": f'{{"time":"{llm_out.lma_time}","weight":"{llm_out.lma_weight}",...}}',
                })
```

---

## 7. 离线预生成与缓存池

> **（新增章节）** 将 LLM 调用从实时路径上完全移除，消除延迟瓶颈。

### 7.1 问题：Best-of-N 的延迟

Best-of-N（N=8）意味着每次触发情绪需要 8 次 LLM 调用。即使使用最快的 API：
- 串行调用：5~15 秒（不可接受）
- 并行调用：受 rate limit 约束，仍需 1~3 秒
- 实时交互场景（人走到台灯前 → 台灯立刻反应）需要 < 200ms 响应

### 7.2 解决方案：离线预生成 + 缓存池

```python
import threading
from collections import defaultdict

class MotionCache:
    """
    离线预生成高质量动作，运行时从缓存池直接弹出，零延迟。
    后台线程定期补充被消费的缓存。
    """

    def __init__(self, pool_size_per_bucket=30, refill_threshold=10):
        self.pool = defaultdict(list)       # (emotion, intensity_bucket) → [LLMOutput, ...]
        self.pool_size = pool_size_per_bucket
        self.refill_threshold = refill_threshold
        self._lock = threading.Lock()

    def get(self, emotion: str, intensity: float) -> LLMOutput:
        """运行时调用，零延迟"""
        bucket = (emotion, round(intensity, 1))
        with self._lock:
            if self.pool[bucket]:
                result = self.pool[bucket].pop()
                # 如果库存不足，触发后台补充
                if len(self.pool[bucket]) < self.refill_threshold:
                    self._trigger_refill_async(emotion, intensity)
                return result

        # 缓存为空时 fallback 到参数化模板（零延迟）
        return template_generate(emotion, intensity)

    def prefill(self, emotion: str, intensity: float, n: int = 30):
        """离线批量预生成（启动时或后台调用）"""
        bucket = (emotion, round(intensity, 1))
        candidates = []
        for _ in range(n * 3):  # 生成 3x，筛选最好的 n 个
            try:
                raw_json = call_llm(emotion, intensity)
                llm_out = parse_and_validate(raw_json)
                if llm_out is None:
                    continue
                t, q, dq = downstream_pipeline_preview(llm_out)
                reward = compute_reward(q, dq, t, emotion, intensity)
                candidates.append((reward, llm_out))
            except Exception:
                continue

        # 按奖励排序，取 top n
        candidates.sort(key=lambda x: -x[0])
        with self._lock:
            for reward, llm_out in candidates[:n]:
                self.pool[bucket].append(llm_out)

    def _trigger_refill_async(self, emotion, intensity):
        """后台线程异步补充"""
        t = threading.Thread(target=self.prefill, args=(emotion, intensity, 10), daemon=True)
        t.start()

    def warmup(self):
        """系统启动时预热所有桶"""
        for emotion in EMOTION_INDEX:
            for intensity in [0.3, 0.5, 0.7, 0.9]:
                self.prefill(emotion, intensity, self.pool_size)
```

### 7.3 三级 Fallback 策略

```python
class MotionGenerator:
    """统一接口：三级动作源，按延迟从低到高 fallback"""

    def __init__(self, cache, example_db, corrector):
        self.cache = cache
        self.example_db = example_db
        self.corrector = corrector

    def generate(self, emotion: str, intensity: float, mode="auto") -> LLMOutput:
        """
        mode:
          "auto"     → 缓存池 → 模板（实时交互场景）
          "quality"  → 缓存池 → 实时 LLM Best-of-N（非实时场景，如预录制）
          "template" → 仅模板（LLM 不可用时）
        """
        if mode == "template":
            return template_generate(emotion, intensity)

        # Level 1: 缓存池（零延迟）
        result = self.cache.get(emotion, intensity)
        if result is not None and result.intent != "[模板生成]":
            return result

        if mode == "quality":
            # Level 2: 实时 LLM Best-of-N（高质量，高延迟）
            return self._best_of_n(emotion, intensity, n=8)

        # Level 3: 参数化模板（零延迟 fallback）
        return template_generate(emotion, intensity)

    def _best_of_n(self, emotion, intensity, n=8):
        candidates = []
        for _ in range(n):
            try:
                raw = call_llm_with_retrieval(emotion, intensity, self.example_db)
                out = parse_and_validate(raw)
                if out is None:
                    continue
                t, q, dq = downstream_pipeline_preview(out)
                score = compute_reward(q, dq, t, emotion, intensity)
                candidates.append((score, out))
            except Exception:
                continue
        if not candidates:
            return template_generate(emotion, intensity)
        return max(candidates, key=lambda x: x[0])[1]
```

---

## 8. RL 框架设计

### 8.1 核心约束与设计选择

**约束：不能修改 LLM 权重。**

这意味着：
- LLM 是固定的"先验生成器"，不在梯度图里
- 可训练的组件只有**残差修正器**（后处理）和**示例数据库**（Prompt 侧）
- RL 真正有意义的部分是训练残差修正器

### 8.2 残差修正器

```python
import torch
import torch.nn as nn

class ResidualCorrector(nn.Module):
    """
    输入: q_raw (4,6) + emotion_vec (n_e+1,)
    输出: q_corrected = q_raw + Δq，|Δq| ≤ max_delta
    参数量: ~2000，CPU 推理 < 1ms
    """
    def __init__(self, n_joints=6, n_frames=4, n_emotions=5, max_delta_deg=15.0):
        super().__init__()
        self.max_delta = max_delta_deg
        input_dim = n_joints * n_frames + n_emotions + 1

        self.net = nn.Sequential(
            nn.Linear(input_dim, 64), nn.ReLU(),
            nn.Linear(64, 64),        nn.ReLU(),
            nn.Linear(64, n_joints * n_frames),
            nn.Tanh()   # 输出 ∈ (-1, 1)，再乘 max_delta
        )
        self.n_joints = n_joints
        self.n_frames = n_frames

    def forward(self, q_raw_tensor, emotion_vec_tensor):
        x = torch.cat([q_raw_tensor.flatten(), emotion_vec_tensor])
        delta = self.net(x).reshape(self.n_frames, self.n_joints) * self.max_delta
        return q_raw_tensor + delta, delta

    def apply(self, q_raw_np, emotion_vec_np):
        """numpy 接口，推理时使用"""
        with torch.no_grad():
            q_t = torch.tensor(q_raw_np, dtype=torch.float32)
            e_t = torch.tensor(emotion_vec_np, dtype=torch.float32)
            q_out, _ = self.forward(q_t, e_t)
        return q_out.numpy()

    def get_flat_params(self):
        """导出为一维向量（供 CMA-ES 使用）"""
        return torch.cat([p.data.flatten() for p in self.parameters()]).numpy()

    def load_flat_params(self, flat):
        """从一维向量加载参数（供 CMA-ES 使用）"""
        idx = 0
        for p in self.parameters():
            n = p.numel()
            p.data = torch.tensor(flat[idx:idx+n], dtype=torch.float32).reshape(p.shape)
            idx += n
```

**为什么设计成残差形式（而非直接输出角度）：**

- `|Δq| ≤ 15°` 的约束保证修正器不能颠覆 LLM 的输出，最坏情况退化到原始输出
- LLM 的语义（"这是开心"）被保留，修正器只学"怎么让它更好看"
- 初始化为零输出时行为和没有修正器完全等价，可以安全地开始训练

### 8.3 奖励函数（扩展版）

> **（改进）** 将 r_lma 从单维 v_peak 扩展为 LMA 四维度全覆盖。

奖励由四部分组成：

```python
def compute_reward(q_traj, dqdt_traj, t_traj,
                   emotion, intensity,
                   reward_model=None):

    # ── r_smooth：平滑度（全自动，立刻可用）
    ddq = np.diff(dqdt_traj, axis=0) / np.diff(t_traj[1:])[:, None]
    r_smooth = -float(np.mean(np.linalg.norm(ddq, axis=1))) * 0.01

    # ── r_lma：LMA 四维一致性（全自动，立刻可用）
    r_lma = compute_r_lma_v2(q_traj, dqdt_traj, t_traj, emotion, intensity)

    # ── r_safety：安全（全自动）
    pos_violation = np.sum((q_traj < Q_MIN) | (q_traj > Q_MAX))
    vel_violation = np.sum(np.abs(dqdt_traj) > DQ_MAX_DEG_PER_S)
    r_safety = -10.0 * (pos_violation + vel_violation)

    # ── r_human：人类偏好（需要收集数据后训练）
    r_human = 0.0
    if reward_model is not None:
        r_human = float(reward_model.score(q_traj, emotion, intensity))

    # 加权求和
    r_total = 0.3 * r_smooth + 0.3 * r_lma + 0.2 * r_safety + 0.2 * r_human

    # 安全硬约束：超限直接清零
    if pos_violation > 0 or vel_violation > 0:
        r_total = min(r_total, -5.0)

    return r_total


def compute_r_lma_v2(q_traj, dqdt_traj, t_traj, emotion, intensity):
    """
    （改进版）LMA 四维度一致性奖励。
    原版只比较 v_peak（Time 维度），缺失了 75% 的 LMA 信息。
    新版覆盖 Time / Weight / Flow / Space 全部四个维度。
    """
    target = EMOTION_LMA_TABLE[emotion]

    # ── Time: 峰值速度
    v_peak = float(np.abs(dqdt_traj).max())
    r_time = -abs(v_peak - target["v_peak"]) / (target["v_peak"] + 1e-6)

    # ── Weight: 运动幅度 RMS（偏离起始姿态的程度）
    amplitude = float(np.std(q_traj - q_traj[0], axis=0).mean())
    r_weight = -abs(amplitude - target["amplitude"]) / (target["amplitude"] + 1e-6)

    # ── Flow: jerk RMS（加速度的变化率）
    #    Free 运动 jerk 低（流畅），Bound 运动 jerk 高（顿挫）
    dt = np.diff(t_traj)
    dt = np.where(dt > 0, dt, 1e-6)  # 防除零
    ddq = np.diff(dqdt_traj, axis=0) / dt[:, None]
    jerk = float(np.sqrt(np.mean(ddq ** 2)))
    r_flow = -abs(jerk - target["jerk"]) / (target["jerk"] + 1e-6)

    # ── Space: 路径间接度（弧长 / 起终点直线距离）
    #    Indirect 运动绕弯多，Direct 运动走直线
    arc_len = float(np.sum(np.linalg.norm(np.diff(q_traj, axis=0), axis=1)))
    straight = float(np.linalg.norm(q_traj[-1] - q_traj[0]) + 1e-6)
    indirectness = arc_len / straight
    r_space = -abs(indirectness - target["indirectness"]) / (target["indirectness"] + 1e-6)

    # 加权组合
    return 0.30 * r_time + 0.25 * r_weight + 0.25 * r_flow + 0.20 * r_space
```

**奖励模型（人类偏好，Bradley-Terry）：**

```python
class RewardModel(nn.Module):
    def __init__(self, n_joints=6, n_timesteps=200, n_emotions=5):
        super().__init__()
        input_dim = n_joints * n_timesteps + n_emotions + 1
        self.net = nn.Sequential(
            nn.Linear(input_dim, 128), nn.ReLU(),
            nn.Linear(128, 64),        nn.ReLU(),
            nn.Linear(64, 1)           # 标量奖励
        )

    def score(self, q_traj_np, emotion, intensity):
        # q_traj 填充/截断到固定长度 200 步
        ...

    def preference_loss(self, r_A, r_B, preferred_A):
        """Bradley-Terry loss：A 比 B 好的概率"""
        return -torch.log(torch.sigmoid(r_A - r_B)) * preferred_A \
               -torch.log(torch.sigmoid(r_B - r_A)) * (1 - preferred_A)
```

> **（新增注记：人类偏好数据量风险）** RewardModel 输入维度 = 6×200+6 = 1206。每个情绪 200~500 对比较数据对于这个输入维度偏少，过拟合风险高。建议：(1) 先用 PCA 或自编码器降维到 32~64 维再输入；(2) 增加 dropout 和 L2 正则；(3) 数据量目标提升到每个情绪 1000+ 对。

**人类偏好数据收集：**

每次给标注者看两段动作视频（循环播放），问"哪个更像 [情绪]？"

```
左边视频：动作 A（循环播放）
右边视频：动作 B（循环播放）
提示：哪个更像"开心"？
按钮：[左边更好] [差不多] [右边更好]

目标：每个情绪收集 200~500 对（初期），1000+ 对（理想），约需 1~2 小时/情绪
```

### 8.4 RL 训练循环

**阶段一：Best-of-N 采样（无需真正的 RL，立刻可用）**

```python
def best_of_n_step(emotion, intensity, n=8):
    candidates = [llm_generate(emotion, intensity) for _ in range(n)]
    scores = [compute_reward(c, emotion, intensity, reward_model=None)
              for c in candidates]
    best = candidates[np.argmax(scores)]

    # 把最好的加入示例数据库
    if max(scores) > REWARD_THRESHOLD:
        example_db.add(emotion, intensity, best, max(scores))

    return best, max(scores)
```

**阶段二（方案 A）：REINFORCE 训练残差修正器**

```python
optimizer = torch.optim.Adam(corrector.parameters(), lr=3e-4)
baseline  = 0.0   # 指数移动平均基线

def train_step(emotion, intensity):
    global baseline

    # LLM 生成（无梯度）
    with torch.no_grad():
        raw_json = call_llm_with_retrieval(emotion, intensity, example_db)
        llm_out  = parse_and_validate(raw_json)
        if llm_out is None:
            return None

    q_raw = assemble_raw_frames(llm_out, q_current, q_rest)   # numpy

    # 修正器（有梯度）
    emotion_vec = encode_emotion(emotion, intensity)
    q_t = torch.tensor(q_raw, dtype=torch.float32)
    e_t = torch.tensor(emotion_vec, dtype=torch.float32)
    q_corrected, delta = corrector(q_t, e_t)

    # 执行并拿奖励
    t_traj, q_traj, dqdt_traj = downstream_pipeline(
        q_corrected.detach().numpy(), llm_out.timing
    )
    reward = compute_reward(q_traj, dqdt_traj, t_traj, emotion, intensity)

    # REINFORCE + baseline
    advantage = reward - baseline
    baseline  = 0.95 * baseline + 0.05 * reward   # EMA

    log_prob = -0.5 * (delta ** 2).mean()          # 高斯策略
    loss = -log_prob * advantage

    optimizer.zero_grad()
    loss.backward()
    torch.nn.utils.clip_grad_norm_(corrector.parameters(), 1.0)
    optimizer.step()

    return reward
```

**阶段二（方案 B，推荐）：CMA-ES 训练残差修正器**

> **（新增）** 对于 ~2000 参数的修正器，进化策略比 REINFORCE 更稳定、更快收敛、实现更简单。

```python
import cma

def train_corrector_cmaes(corrector, q_raw_batch, emotion_batch, intensity_batch,
                          timing_batch, n_generations=200):
    """
    用 CMA-ES 训练残差修正器。
    优势：
    - 不需要 LLM 参与训练循环（用缓存的 q_raw 即可）
    - 不需要对数概率、baseline、梯度裁剪
    - 2000 维参数对 CMA-ES 来说完全可行
    - 黑盒优化，直接用奖励排序
    """

    def evaluate(flat_params):
        """评估一组修正器参数的平均奖励（CMA-ES 最小化，所以取负）"""
        corrector.load_flat_params(flat_params)
        total_reward = 0.0
        n = len(q_raw_batch)
        for i in range(n):
            emotion_vec = encode_emotion(emotion_batch[i], intensity_batch[i])
            q_corrected = corrector.apply(q_raw_batch[i], emotion_vec)
            t, q, dq = downstream_pipeline(q_corrected, timing_batch[i])
            total_reward += compute_reward(q, dq, t, emotion_batch[i], intensity_batch[i])
        return -total_reward / n   # CMA-ES 最小化

    # 初始化
    x0 = corrector.get_flat_params()
    sigma0 = 0.3   # 初始步长（保守，因为 Tanh 输出范围有限）

    es = cma.CMAEvolutionStrategy(x0, sigma0, {
        'maxiter': n_generations,
        'popsize': 20,           # 每代评估 20 个候选
        'tolx': 1e-6,
    })

    generation = 0
    while not es.stop():
        solutions = es.ask()
        fitnesses = [evaluate(s) for s in solutions]
        es.tell(solutions, fitnesses)
        generation += 1
        if generation % 20 == 0:
            print(f"Gen {generation}: best reward = {-es.result.fbest:.4f}")

    # 加载最优参数
    corrector.load_flat_params(es.result.xbest)
    return -es.result.fbest  # 返回最优奖励

# --- 使用方式 ---
# 1. 从缓存池收集一批已有的 LLM 输出（无需实时调用 LLM）
q_raw_batch = [...]        # 从缓存中收集的 q_raw (4,6)
emotion_batch = [...]      # 对应的情绪标签
intensity_batch = [...]    # 对应的强度
timing_batch = [...]       # 对应的 timing 参数

# 2. 离线训练（完全不需要 LLM API）
best_reward = train_corrector_cmaes(corrector, q_raw_batch, emotion_batch,
                                     intensity_batch, timing_batch)
```

**方案 A（REINFORCE）vs 方案 B（CMA-ES）对比：**

| 维度 | REINFORCE | CMA-ES（推荐） |
|---|---|---|
| 训练时是否需要调用 LLM | 是（每步一次） | 否（用缓存的 q_raw） |
| 实现复杂度 | 中（需 baseline、梯度裁剪） | 低（`pip install cma`，黑盒） |
| 收敛稳定性 | 方差大 | 稳定 |
| 适合的参数规模 | 不限 | < 10K（本修正器 ~2K，完全可行） |
| 依赖 | PyTorch | `cma` 库（纯 numpy） |

**阶段三（可选）：质量门控 + 奖励模型精炼**

```python
def gated_correction(q_raw, emotion_vec, corrector, reward_model):
    """修正前先评估 q_raw 质量，太差就直接重生成"""
    raw_score = reward_model.score(q_raw.flatten(), emotion, intensity)
    if raw_score < RAW_QUALITY_THRESHOLD:
        return None   # 触发重生成，不浪费修正器

    q_corrected, _ = corrector.apply(q_raw, emotion_vec)
    return q_corrected
```

### 8.5 两侧优化的分工与局限

| 优化侧 | 解决的问题 | 局限 |
|---|---|---|
| **Prompt 侧（示例检索）** | 改善 LLM 对当前情绪的先验理解，改善 q_raw 的起点质量 | 只能在 LLM 现有能力范围内榨取价值，改变不了能力边界 |
| **后处理侧（残差修正器）** | 学习 LLM 输出的系统性偏差，把偏差补回来 | 只能微调（±15°），无法弥补 LLM 的根本性错误 |

**共同天花板：LLM 本身的能力。** 碰到天花板时的出路：

1. 换能力更强的闭源 LLM
2. **（新增推荐）Fine-tune 开源小模型（7B~8B）**——用积累的高奖励样本做 SFT，直接突破能力天花板。配合 constrained decoding（outlines / guidance 库）可保证 JSON 合法率 100%，推理延迟从秒级降到百毫秒级。

---

## 9. 动作序列编排

> **（新增章节）** 解决单个 4 帧片段表达力不足的问题，支持多片段串联实现更丰富的情绪动作。

### 9.1 为什么需要序列编排

当前系统每次只生成一个 4 帧片段（2 个 LLM 生成帧），只能表达"起势 → 高潮"一个动作弧。但现实中丰富的情绪表达需要多个动作串联：

- 开心：弹一下 → 歪头 → 再弹一下
- 好奇：歪头左看 → 前探 → 歪头右看
- 悲伤：慢慢低头 → 停顿 → 微微颤抖

### 9.2 序列数据结构

```python
@dataclass
class MotionSequence:
    """多个 4 帧动作片段的编排"""
    clips: list[LLMOutput]
    transition_type: str = "smooth"   # "smooth" | "pause"
    pause_duration: float = 0.3       # 片段间停顿（仅 pause 模式）

SEQUENCE_PATTERNS = {
    "happy": [
        # 两段弹跳
        {"n_clips": 2, "intensity_curve": [1.0, 0.7], "transition": "smooth"},
        # 三段：弹 → 歪头 → 弹
        {"n_clips": 3, "intensity_curve": [0.8, 0.5, 1.0], "transition": "smooth"},
    ],
    "curious": [
        # 两段：歪头 → 前探
        {"n_clips": 2, "intensity_curve": [0.6, 0.9], "transition": "pause"},
    ],
    "sad": [
        # 两段：低头 → 停顿 → 微动
        {"n_clips": 2, "intensity_curve": [0.8, 0.3], "transition": "pause"},
    ],
}
```

### 9.3 序列执行器

```python
def generate_motion_sequence(emotion: str, intensity: float,
                              generator: MotionGenerator,
                              request: MotionRequest,
                              corrector: ResidualCorrector) -> tuple:
    """
    生成并串联多个动作片段。
    前一个片段的结束姿态自动成为后一个片段的起始姿态。
    """
    # 选择一个序列模式
    patterns = SEQUENCE_PATTERNS.get(emotion, [{"n_clips": 1, "intensity_curve": [1.0], "transition": "smooth"}])
    pattern = random.choice(patterns)

    all_t, all_q, all_dq = [], [], []
    t_offset = 0.0
    current_q = request.q_current.copy()

    for i in range(pattern["n_clips"]):
        clip_intensity = intensity * pattern["intensity_curve"][i]

        # 生成单个片段
        clip_out = generator.generate(emotion, clip_intensity)

        # 构造请求（起始姿态为上一段的结束姿态）
        clip_request = MotionRequest(
            q_current=current_q,
            q_rest=request.q_rest,
            emotion=emotion,
            intensity=clip_intensity,
        )

        # 组装轨迹
        t, q, dq = assemble_motion(clip_out, clip_request, corrector)

        # 偏移时间轴
        all_t.append(t + t_offset)
        all_q.append(q)
        all_dq.append(dq)

        t_offset = all_t[-1][-1]
        current_q = q[-1].copy()  # 下一段从这里开始

        # 片段间停顿
        if pattern["transition"] == "pause" and i < pattern["n_clips"] - 1:
            pause_t = np.array([t_offset, t_offset + pattern.get("pause_duration", 0.3)])
            pause_q = np.stack([current_q, current_q])
            pause_dq = np.zeros_like(pause_q)
            all_t.append(pause_t)
            all_q.append(pause_q)
            all_dq.append(pause_dq)
            t_offset = pause_t[-1]

    # 拼接所有片段
    t_full = np.concatenate(all_t)
    q_full = np.concatenate(all_q, axis=0)
    dq_full = np.concatenate(all_dq, axis=0)

    return t_full, q_full, dq_full
```

---

## 10. 完整数据流总览

```
[情绪请求] emotion="happy", intensity=0.8
    │
    ├─ mode=auto? ──→ [缓存池] ──→ 有缓存? ── 是 ──→ LLMOutput
    │                                  │
    │                                  └── 否 ──→ [参数化模板] ──→ LLMOutput
    │
    ├─ mode=quality? ──→ [示例检索] ──→ [构建 Prompt] ──→ [LLM ×N] ──→ Best-of-N
    │                        ↑                                              │
    │                   [示例数据库] ←── 高奖励样本入库 ──────────────────────┘
    │
    ▼
[LLMOutput]
  ├── shape: {f1, f2}
  └── timing: {duration, accel_ratio, asymmetry}
    │
    ▼
[组装 q_raw (4,6)]
  f0 ← q_current（编码器读数）
  f1, f2 ← 生成器输出
  f3 ← q_rest（预定义中性姿态）
    │
    ├──────────────────────── 可选：多片段串联（第9章）
    │
    ▼
[残差修正器 f_θ]（小 MLP，可训练）
  输入: q_raw (4,6) + emotion_vec (n_e+1,)
  输出: q_final = q_raw + Δq，|Δq| ≤ 15°
    │
    ▼
[插值器]
  q_final (4,6) + timing → q(t), q̇(t), 100Hz
    │
    ▼
[安全裁剪]
  超速 → 拉伸时间轴（保形）
  超限位 → clip + 警告
    │
    ▼
[关节控制器]  PD + 速度前馈，10ms 周期
    │
    ▼
[舵机执行]
    │
    ▼
[奖励评估]  r_smooth + r_lma(4D) + r_safety + r_human
    │
    ├──→ [CMA-ES / REINFORCE 更新 f_θ]
    │
    └──→ 高奖励样本 → 示例数据库 / 缓存池补充
```

---

## 11. 可行性评估与风险分析

> **（新增章节）** 对各模块按"能否直接落地"分三档评估。

### 11.1 高可行性（直接落地）

| 模块 | 理由 |
|---|---|
| 插值器 + 安全裁剪 | 梯形加减速是经典方案，"时间拉伸保形"策略优雅可靠，代码可直接用 |
| 4 帧固定结构 + f0/f3 注入 | 输出空间紧凑（12 个角度 + 3 个 timing），下游维度完全确定 |
| Best-of-N 采样 | 零可训练参数，生成 N 个候选取最优，几乎零风险 |
| 参数化模板 Baseline | 纯确定性计算，无网络依赖，第 1 周即可 demo |

### 11.2 中等可行性（需要验证）

| 模块 | 风险点 | 缓解措施 |
|---|---|---|
| LLM 生成关节角度 | 单次生成"合法且好看"的比例可能只有 30~50% | Best-of-N + 示例库对冲方差 |
| 残差修正器训练 | REINFORCE 方差大，每步需调用 LLM | 改用 CMA-ES + 离线 q_raw 缓存 |
| CoT 自我一致性 | LMA 分类可能与角度输出不一致 | few-shot 示例锚定 + 奖励筛选 |

### 11.3 低可行性 / 高风险

| 模块 | 问题 | 建议 |
|---|---|---|
| 原版 r_lma（仅 v_peak） | 只覆盖 LMA Time 维度，缺失 75% 信息 | **已修复**：扩展为四维 r_lma_v2 |
| 人类偏好奖励模型 | 1206 维输入 + 200~500 对数据 = 过拟合 | 先用 PCA 降维 + 增加数据量到 1000+ |
| Best-of-N 实时延迟 | 8 次 LLM 串行 = 5~15 秒 | **已解决**：离线预生成缓存池 |

### 11.4 长期演进建议：Fine-tune 本地小模型

当系统运行一段时间积累了足够多的高奖励样本后，建议考虑 fine-tune 一个 7B~8B 开源模型：

| 方面 | 闭源 API LLM | Fine-tuned 本地小模型 |
|---|---|---|
| 推理延迟 | 0.5~3 秒/次 | 50~200 毫秒/次 |
| JSON 合法率 | ~95%（靠 prompt） | ~100%（constrained decoding） |
| 质量上限 | 固定（不可微调） | 可用高奖励样本 SFT 持续提升 |
| 网络依赖 | 必需 | 无 |
| 成本 | 持续 API 费用 | 一次性训练成本 |

推荐工具链：Qwen-2.5-7B / Llama-3-8B + LoRA SFT + outlines（constrained decoding）。

---

## 12. 工程实施路径（修订版）

> **（修订）** 核心原则：**先用模板让系统"能动"，再用缓存让它"丰富多变"，最后用 RL/进化策略让它"越来越好"。** 不要一上来就搭完整 RL 闭环——那是优化的终点，不是起点。

### 第 1 周：基础运动能力 + 参数化模板

**目标：** 台灯能对 5 种情绪做出不同反应，可以 demo。

- 实现关节角度读取接口
- 实现插值器（含 `asymmetry` 速度曲线）
- 实现安全裁剪（速度拉伸版本）
- 实现控制器接口（位置目标 + 速度前馈）
- 实现参数化模板库（5 情绪 × 3~5 变体 = 15~25 个预设动作）
- 实现 `template_generate()` 带 intensity 缩放和随机扰动

**验收：** 给定情绪标签和强度，台灯能从模板库生成并安全执行动作，速度平滑，每个情绪有 3~5 种可区分的变体。

### 第 2 周：LLM 接入 + 离线缓存池

**目标：** 让 LLM 能生成可执行的动作，且实时路径无延迟。

- 实现 System prompt（关节定义 + 格式规范）
- 实现 JSON 解析 + 校验 + 重试逻辑
- 实现 f0/f3 注入
- 用模板生成的高分样本作为种子，建立初始示例库（20~30 条）
- 接入 few-shot 检索
- **实现 MotionCache 离线预生成缓存池**
- **系统启动时 warmup：每个 (emotion, intensity_bucket) 预生成 30 个候选**
- **实现三级 Fallback（缓存 → 模板 → 实时 LLM）**

**验收：** 输入情绪标签，台灯能从缓存池取出动作并执行，格式合法率 > 95%，实时延迟 < 50ms。

### 第 3 周：自动奖励闭环 + Best-of-N

**目标：** 有自动反馈循环，不依赖人工标注。

- 实现四维 r_lma_v2 奖励（Time + Weight + Flow + Space）
- 实现 `compute_reward()` 完整版（r_smooth + r_lma_v2 + r_safety）
- 实现 Best-of-N 采样（N=8），集成到缓存池预生成流程
- 实现示例数据库的自动更新（高分入库 + 低分淘汰）
- **对比实验：模板 baseline vs LLM+Best-of-N，量化改进幅度**

**验收：** Best-of-N 的平均奖励比单次生成提升 > 20%，缓存池中的平均质量持续提升。

### 第 4 周：残差修正器 + CMA-ES

**目标：** 可训练的后处理环节上线。

- 实现 `ResidualCorrector` 网络
- 从缓存池收集 200+ 条 q_raw 样本
- **用 CMA-ES 离线训练修正器（不需要实时调用 LLM）**
- 训练约 200 代，评估修正器的增量贡献

**验收：** 修正器开启后，规则奖励 > 修正器关闭（纯 LLM / 纯模板）。

### 第 5 周：动作序列编排

**目标：** 更丰富、更有层次的情绪表达。

- 实现 `MotionSequence` 数据结构
- 实现多片段串联逻辑（自动衔接 f3→f0）
- 为每个情绪设计 2~3 种序列模式
- 实现片段间 pause 和 smooth 两种过渡

**验收：** 台灯能执行多段式动作（如"弹 → 歪头 → 再弹"），比单段动作更生动。

### 第 6 周及以后：人类偏好 + 模型精炼

**目标：** 学习人类审美偏好，持续提升质量上限。

- 搭建偏好标注界面（A/B 比较视频）
- 收集每个情绪 500~1000 对偏好数据
- 训练 `RewardModel`（先 PCA 降维到 64 维）
- 把 `r_human` 加进奖励计算
- （可选）用高奖励样本 fine-tune 本地 7B 模型

---

## 附录 A：关键超参数参考值

| 参数 | 推荐值 | 说明 |
|---|---|---|
| `K_FRAMES` | 4 | 固定帧数，不可变 |
| `MAX_FRAME_DELTA` | 55° | 相邻帧最大角度差 |
| `max_delta_deg`（修正器）| 15° | 残差最大幅度 |
| `corrector_lr` | 3e-4 | Adam 学习率（REINFORCE 方案） |
| `cma_sigma0` | 0.3 | CMA-ES 初始步长（推荐方案） |
| `cma_popsize` | 20 | CMA-ES 种群大小 |
| `baseline_alpha` | 0.95 | EMA 衰减系数（REINFORCE 方案） |
| `best_of_n` | 8 | 采样候选数量 |
| `control_dt` | 10ms | 控制周期 |
| `interpolation_dt` | 10ms | 插值步长（与控制周期一致）|
| `reward_threshold` | 0.5 | 高分样本入库阈值 |
| `cache_pool_size` | 30 | 每个 (emotion, bucket) 的缓存容量 |
| `cache_refill_threshold` | 10 | 触发后台补充的库存下限 |
| `template_noise_scale` | 3.0 | 模板随机扰动基准幅度（°） |

## 附录 B：情绪编码

```python
EMOTION_INDEX = {"happy": 0, "sad": 1, "angry": 2, "curious": 3, "calm": 4}

def encode_emotion(emotion: str, intensity: float) -> np.ndarray:
    """返回 one-hot(5) + intensity(1) = shape (6,)"""
    vec = np.zeros(len(EMOTION_INDEX) + 1, dtype=np.float32)
    vec[EMOTION_INDEX[emotion]] = 1.0
    vec[-1] = float(intensity)
    return vec
```

## 附录 C：关节限位常量

```python
Q_MIN = np.array([-90, -20, -50, -60, -45, -30], dtype=np.float32)
Q_MAX = np.array([+90, +70, +50, +60, +45, +50], dtype=np.float32)
Q_REST = np.array([0, 30, 0, 20, 0, 10], dtype=np.float32)
DQ_MAX_DEG_PER_S = 300.0   # 全局速度限制
```

## 附录 D：参考文献

- Hu et al., **ELEGNT: Expressive and Functional Movement Design for Non-anthropomorphic Robot**, arXiv 2501.12493, Apple, 2025
- Huang et al., **EMOTION: Expressive Motion Sequence Generation for Humanoid Robots with In-Context Learning**, IEEE RA-L, arXiv 2410.23234, Apple, 2024
- Mahadevan et al., **Generative Expressive Robot Behaviors using Large Language Models (GenEM)**, HRI 2024, Google
- Laban, R., **The Mastery of Movement**, 1950（LMA 理论原始来源）
- Dragan & Srinivasa, **Legibility and Predictability of Robot Motion**, HRI 2013（运动意图可读性）
- Christiano et al., **Deep Reinforcement Learning from Human Preferences**, NeurIPS 2017（偏好奖励模型）
- Hansen, N., **The CMA Evolution Strategy: A Tutorial**, arXiv 1604.00772, 2016（CMA-ES 理论）

## 附录 E：版本变更记录

| 版本 | 变更内容 |
|---|---|
| v1 | 初始设计：LLM + REINFORCE + 单维 r_lma |
| **v2** | 新增：参数化模板 baseline（第6章）、离线缓存池（第7章）、四维 r_lma_v2（第8.3节）、CMA-ES 替代方案（第8.4节）、动作序列编排（第9章）、可行性评估（第11章）、修订实施路径（第12章）、LMA 量化参数表（第2.4节）、LLM 质量预期（第3.6节）、fine-tune 小模型建议（第11.4节） |
