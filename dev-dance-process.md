# 跳舞系统开发过程记录

> 2026-04-04 ~ 2026-04-05，基于 `robot_dance_system_design_v2.md` 设计文档实施

---

## 1. 设计文档评审

### v2 架构概要

三层架构，LLM 完全不在实时路径上：

```
层1：音乐分析（离线 librosa / 实时 aubio）
  → 节拍时间戳 + 能量 + 段落边界

层2：编舞调度器（每 beat 一次决策）
  → Phrase Template 状态机 → 动作选择器 → 时间调度器

层3：执行层（100Hz 控制循环）
  → 增量插值 + 基础律动叠加 + body wave + soft homing → 舵机
```

### 核心设计要点

| 要点 | 说明 |
|------|------|
| 预判式卡点 | `start_time = beat_time - accent_norm × total_dur - PREP_TIME`，动作在节拍到达时精确抵达重音帧 |
| 增量角度 | delta_frames 相对当前姿态，任何原语从当前位置无缝开始 |
| Phrase Template | 替代纯马尔可夫，8 拍一组模板（buildup/chill/hype/contrast/groove_focus），有编排结构感 |
| 参数化原语生成 | 方向模板 × 振幅 × 速度，批量生成 30+ 条原语，比手写效率高 10x |
| 基础律动 | 持续正弦振荡叠加，间隙不静止 |
| Body wave | 各关节延迟递增（0→60ms），模拟能量从底部向上传导 |
| Groove 微时值 | 对重音帧 ±4% 随机偏移，tight/laid_back/neutral 三种风格 |

---

## 2. 代码盘点：已有实现

评审时发现 `lelamp/dance/` 目录下核心代码**已全部实现**，与 v2 设计文档一致：

| 文件 | 内容 | 状态 |
|------|------|------|
| `lelamp/dance/music_analysis.py` | OfflineMusicAnalysis（librosa 离线分析） | 完成 |
| `lelamp/dance/choreography.py` | ChoreographyStateMachine + MotionSelector + MotionScheduler | 完成 |
| `lelamp/dance/primitives.py` | MotionPrimitive + 参数化生成器 + batch_generate_library() | 完成 |
| `lelamp/dance/dance_executor.py` | DanceExecutor + BaseOscillation（100Hz 控制） | 完成 |
| `lelamp/dance/groove.py` | apply_groove + apply_body_wave | 完成 |
| `dance_main.py` | 离线模式入口 CLI | 完成 |

### 已适配硬件

设计文档假设 6 关节，代码已适配实际 5 关节：

```
base_yaw    [-5,  14]   底座旋转
base_pitch  [-68, -20]  下臂俯仰
elbow_pitch [50, 100]   中段弯曲
wrist_roll  [-30,  15]  头部左右
wrist_pitch [-5,  68]   头部俯仰
```

### 依赖

librosa / sounddevice / soundfile 均已安装。

---

## 3. 需求：实时麦克风拾音跳舞

用户需求：不提供音频文件，现场播放音乐（手机/音箱），机器人通过麦克风听到音乐自动跳舞。

这需要 v2 设计文档中的"备选路径：实时节拍检测"，但该路径代码未实现。

### 3.1 aubio 安装失败

设计文档推荐 aubio 做实时节拍检测，但在 macOS 上编译失败：

```
error: command '/usr/bin/cc' failed with exit code 1
```

### 3.2 替代方案：纯 numpy 实时节拍检测

不依赖 aubio，用 spectral flux + inter-onset interval 实现：

**新增文件：**

- `lelamp/dance/realtime_beat.py` — RealtimeBeatTracker / RealtimeEnergyAnalyzer / RealtimeSectionDetector
- `dance_live.py` — 实时跳舞入口

**复用的组件：** ChoreographyStateMachine / MotionSelector / DanceExecutor / apply_groove — 全部复用，零改动。

---

## 4. 第一次实机测试：抖动抽搐

### 现象

- 没放音乐时，机器人已经在持续抖动
- 放了音乐后，仍然持续抖动，没有节奏变化
- 整体没有节奏感，不好看

### 根因分析

| 问题 | 原因 |
|------|------|
| 安静时就在抖 | BaseOscillation 从启动就以 BPM=120 持续输出 4° 正弦波，100Hz 发送给舵机 |
| 安静时也在"跳舞" | 虚拟节拍补偿无条件触发：0.75s 无 onset → 自动插入虚拟节拍 → 不停执行原语 |
| 放音乐后无区别 | 没有静音检测，系统不区分"有音乐"和"安静" |
| 整体无节奏感 | onset 检测阈值太低（均值 ×1.8），环境噪声和舵机声被当成节拍 |

### 修复

**realtime_beat.py：**
1. 噪音门控 — RMS < noise_floor（0.005）时完全不处理
2. 虚拟节拍延迟启用 — 至少 4 次真实 onset 后才允许漏检补偿
3. 阈值提高 — spectral flux 倍数 1.8 → 2.5
4. `music_active` 状态机 — 明确区分有音乐/静音
5. 静音超时 — 3s 无响声 → 音乐停止，重置状态

**dance_live.py：**
1. 启动时 oscillation 关闭，节拍锁定后才开
2. 音乐停止时清空动作队列、停止 step()
3. 日志同时写文件（`logs/dance_*.log`）

---

## 5. 第二次实机测试：有改善但效果不佳

### 日志分析（dance_20260405_120258.log）

**改善：**
- 安静时不再抖动 ✅
- 音乐停止后正确静止 ✅
- BPM 稳定在 105 ✅

**仍存在的问题：**

| 指标 | 值 | 说明 |
|------|-----|------|
| 总拍数 | 36 | 约 26 秒 |
| 真实 onset | 11 (31%) | 太少 |
| 虚拟节拍 | 25 (69%) | 主导，不是真正跟音乐 |
| 节拍间隔范围 | 0.169s ~ 0.896s | 应约 0.57s，方差极大 |
| HIT 占比 | ~80% | 能量归一化导致几乎全是高能量 → 强制 HIT |

**根因：**

1. **Spectral flux + 简单阈值从麦克风拾音效果差** — 环境混响、频率复杂，onset 检测既漏检又误检
2. **能量归一化失真** — `rms / recent_peak` 让中等音量也被归一化到接近 1.0，全程触发 HIT
3. **虚拟节拍掩盖了真实检测不足** — 看似有拍，实际是预测填充

---

## 6. 结论与下一步

### 结论

**实时麦克风节拍检测这条路暂时走不通。** 纯 spectral flux 从麦克风拾音检测节拍，精度不足以驱动好看的舞蹈动作。问题不在执行层（DanceExecutor / 原语库 / groove 机制都是好的），而在输入层（节拍检测不可靠）。

### 可行的替代方案

| 方案 | 说明 | 复杂度 |
|------|------|--------|
| **离线分析 + 播放**（已实现） | `dance_main.py song.mp3`，librosa 离线分析精确节拍，电脑边播边跳 | 零，直接能用 |
| **系统音频回环** | macOS 装 BlackHole 虚拟声卡，捕获系统正在播放的音频，信号干净，onset 准确 | 中，需用户装驱动 |
| **更好的实时算法** | 低频带通滤波（60-200Hz bass/kick）+ 自相关 tempo 估算 + 相位锁定振荡器 | 高，本质上是重写 aubio |

### 建议优先级

1. 先用 `dance_main.py` 验证舞蹈效果（执行层 + 原语库 + groove 是否好看）
2. 执行层调优满意后，再解决实时输入问题（系统音频回环最实际）
