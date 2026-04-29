# 动作系统重构开发总结：intensity 参数化 + compose_motion

> 工作分支：`feature/action`（**全部改动尚未 commit**）
> 完成度：代码实现 + 单元测试 + 真机测试均通过；headshake 最新一次降幅修复尚未真机验证；LLM 端到端未测

## 1. 背景与目标

`motion_scripts.py` 里的 10 个程序化情绪动作（nod、curious 等）原来全是硬编码角度和时长，每次播放完全一致，表现力受限。

**用户决策（方案 F：A + C 并存 + intent CoT）**：
- **Phase 1 (A)**：把 10 个函数参数化为 `intensity` + `jitter`，保留程序化骨架
- **Phase 2 (C)**：新增 `compose_motion` LLM 工具，让 LLM 直接出关键帧，10 个原动作作为 few-shot

**显式拒绝**：
- 不基于 CSV 录制回放（rejected E）
- 不复活已废弃的 `templates.py`
- 不引入"模板层"等历史结构

## 2. 已完成的代码改动

### 文件清单

| 文件 | 状态 | 说明 |
|---|---|---|
| `lelamp/service/motors/motion_scripts.py` | 修改 | 10 个函数参数化 + nod/headshake 加 hold |
| `lelamp/motion/motion_agent.py` | 修改 | `play_emotion`/`play_compose`/`_velocity_audit` |
| `lelamp/motion/compose_motion.py` | **新建** | `validate_segments` + `MOTION_EXAMPLES` few-shot |
| `lelamp/soul/soul_agent.py` | 修改 | compose_motion 工具 schema + dispatcher + system prompt 注入 |
| `tests/test_intensity_and_compose.py` | **新建** | 15 个单元测试 |
| `tests/test_motion_real.py` | **新建** | 21 个真机 case |

### Phase 1 (A) — `motion_scripts.py`

**两个核心 helper**：
```python
def _scaled(rng, base, intensity, jitter):
    """幅度缩放：base * intensity * uniform(1-j, 1+j)"""

def _scaled_dur(rng, base, intensity, jitter):
    """时长缩放：intensity<1 时拉长（factor=2/(1+intensity)），
    intensity≥1 时不缩短（factor=1.0）— 避免幅度+速度双叠加冲破 DQ_MAX"""
```

**10 个函数全部改签名为** `(pos, intensity=1.0, jitter=0.10, seed=None)`：
- nod、headshake、curious、excited、happy_wiggle、sad、scanning、shock、shy、wake_up
- **wake_up 例外**：必须严格回到 HOME，幅度不缩放，仅时长接受 intensity 调整
- **nod / headshake**：加了"谷底/极限 hold 段"，让动作深度被眼睛看见
- **headshake 已降幅**：base 22°→16°，transit 0.30→0.40s，hold 0.15→0.18s（因为 base_yaw 的 DQ_MAX=60 deg/s 比较严格）

**默认 jitter** 从 0.15 → 0.10（避免基线动作抖动后超速触发 audit）

### Phase 1 — `motion_agent.py`

```python
def play_emotion(name, intensity=1.0, jitter=0.10, seed=None):
    # clamp intensity 到 [0.3, 1.5], jitter 到 [0.0, 0.3]
    # 调用 MOTION_REGISTRY[name](current, ...) → _velocity_audit → _enqueue_frames

def play_compose(intent, segments) -> Optional[str]:
    # 校验 → _build_frames → _velocity_audit → _enqueue_frames
    # 返回错误字符串给 LLM 自我修正，None 表示成功

def _velocity_audit(frames):
    # 30fps dict 列表 → (t, q) 数组 → velocity_safe_stretch → 重采样回 fps
    # A 路径和 C 路径共用此安全网
```

**🔴 已修复的灾难性 bug**：原本 `_velocity_audit` 里有
```python
q_safe = np.clip(q_safe, Q_MIN, Q_MAX)  # 已删除
```
但 motion_executor 的 `Q_MIN/Q_MAX` 是另一套**废弃管道**的坐标系：
- `wrist_pitch` 范围 `[-5, 68]`，但 HOME=-47（差 42°）
- `base_pitch` 范围 `[-68, -20]`，但启动姿态常 -99（差 31°）

audit 触发时这一行会把 `wrist_pitch=-69` 截到 `-5`（猛抬 64°），LeLamp follower 内部可能做了二次截断救场，但这是侥幸。**已删除该行 + Q_MIN/Q_MAX import**。

### Phase 2 (C) — `compose_motion.py`（新建）

```python
VALID_JOINTS = {"base_yaw", "base_pitch", "elbow_pitch", "wrist_roll", "wrist_pitch"}
MIN_SEG_COUNT, MAX_SEG_COUNT = 2, 8
MIN_DUR, MAX_DUR = 0.15, 2.0
MAX_TOTAL_DUR = 6.0
JOINT_RANGE = (-92.0, 92.0)

def validate_segments(segments) -> tuple[list, str|None]:
    """LLM 风格 [{joints, duration}] → _build_frames 风格 [(joints, duration)]
    返回 (转换后列表, 错误信息)"""

MOTION_EXAMPLES = "..."  # 6 个 few-shot：nod/headshake/curious/sad/shock/excited
                         # 用 <motion_examples> XML 包裹，含坐标系说明和构造原则
```

### Phase 2 — `soul_agent.py`

- `express_emotion` schema 加 `intensity` 字段（0.3–1.5）
- 新增 `compose_motion` 工具，schema 含 `intent`（CoT）+ `segments` 数组
- system message 拼接 `MOTION_EXAMPLES`（被 prompt cache 缓存）
- dispatcher 加 `compose_motion` 分支，错误回报 LLM 用于自修正
- `_TOOL_EXEC_ORDER` 加 `compose_motion: 1`
- `<tool_selection>` 引导 LLM 优先 express_emotion，仅在 10 类标准动作不够用时用 compose

## 3. 测试状态

### 单元测试 `tests/test_intensity_and_compose.py`：15/15 ✓

覆盖：intensity 缩放幅度、intensity 缩放速度、jitter seed 行为、含停顿段、极端 intensity 限位、双关节缩放、validate_segments 8 种合法/非法路径、_build_frames 全链路。

### 真机测试 `tests/test_motion_real.py`：21 个 case

最近一次完整跑通的所有 case 状态：

#### Phase 1 — A1-A10（10 个动作）

| Case | 状态 | 备注 |
|---|---|---|
| A1 nod 三档 | ✓ | 1.0/1.4 触发小幅 audit（spline 峰值，可接受） |
| A2 curious 三档 | ✓ | 节奏缩放清晰，1.4 几乎无 audit |
| A3 happy_wiggle 双档 | ⚠ | 1.4 audit 拉伸 1.83x（4 段震荡 base_yaw 跨度大，未优化） |
| A4 jitter 三次随机 | ✓ | 三次帧数不同，jitter 工作 |
| **A5 headshake 三档** | ⚠ **待复测** | **刚改了 base 22→16 + 时长延长，未在硬件验证** |
| A6 excited 双档 | ✓ | 多关节小幅，无 audit |
| A7 sad 双档 | ✓ | 慢动作，时长缩放正确（170 vs 128 帧） |
| A8 scanning 双档 | ✓ | 仅 0.5/1.0（避免 base_yaw 越限），无 audit |
| A9 shock 双档 | ✓ | 1.4 仅 1.02x 微 audit |
| A10 shy 双档 | ✓ | 三关节复合，无 audit |

#### Phase 2 — C1-C11（compose_motion）

| Case | 状态 | 备注 |
|---|---|---|
| C1 复刻 nod | ✓ | 与 A1 不完全一致（A1 加了 hold，C1 没加，符合预期） |
| C2 复合三关节 | ✓ | 歪头 + 低头 |
| C3 速度安全网 90°/0.2s | ✓ | 24→77 帧，audit 拉伸 3.35x |
| C4 5 种非法输入 | ✓ | 全部 `playing=False`，错误信息清晰 |
| C5 边界值（2/8 段、6.0s 总） | ✓ | 边界检查 `<=` 正确 |
| C6 partial 关节继承 | ✓ | 视觉确认转头时仍低着头 |
| C7 hold via 重复段 | ✓ | 凝视 0.8s 可见 |
| C8 队列衔接 | ✓ | **设计为队列，非抢占** |
| C9 链式 3 个 compose | ✓ | 第 1→第 3 衔接，第 2 被 pending 替换 |
| C10 A↔C 队列衔接 | ✓ | express 与 compose 互入对方 pending |
| C11 漂移恢复 | ✓ | nod 从漂移位 yaw=-25.4 起播，未先回 HOME |

## 4. 已知问题与设计决策

### ⚠ 接受的限制

1. **Catmull-Rom 速度峰值 > 端点速度**
   - nod 1.0/1.4 即使加 hold 仍触发小幅 audit，因为 Hermite 曲线中点速度高于 `delta/dt`
   - 不是 bug，audit 兜底完全够用

2. **happy_wiggle 1.4 audit 拉伸 1.83x**
   - 4 段震荡 base_yaw 跨度大，未单独优化
   - 1.4 视觉上比 1.0 慢，但用户没强制要求修复

3. **C8/C10 是队列不是抢占**
   - 当前 `_enqueue_frames` 设计：playing 状态下新 frames 进 pending，等当前 frames 跑完才切换
   - **用户判断**："实际 main loop 里 LLM 推理时延 > 单次动作时长，不会出现真正抢占场景"
   - 已把测试 case 改名"队列衔接"，**不修改代码语义**

### 🔴 仍待验证

1. **headshake 最新降幅修复**
   - base 22→16，transit 0.30→0.40，mid 0.35→0.45，hold 0.15→0.18
   - 数学验证：1.0 时左→右段 spline 峰值速度 ≈ 56 deg/s（< DQ_MAX=60），**应不再触发 audit**
   - **未在真机验证**，下次跑 `tests/test_motion_real.py` 检查 A5 数据

2. **LLM 端到端测试 L1-L5**（完全没跑过）
   - L1: "好奇地看一眼" → 期待 `express_emotion(curious, intensity≈0.5)`
   - L2: "用力点头" → 期待 `express_emotion(nod, intensity≥1.2)`
   - L3: "做一个奇怪的动作" → 期待 `compose_motion`
   - L4: 故意诱导 LLM 出错 → 观察自修正
   - L5: 30 min 自然对话监控 → compose_motion 占比应在 5-15%
   - 需要起 `main_soul.py` 跑

## 5. 关键安全约束（CLAUDE.md 规则）

- 所有运动路径（A 和 C）都过 `_velocity_audit` → `velocity_safe_stretch`
- DQ_MAX (deg/s)：base_yaw=60、base_pitch=80、elbow_pitch=80、wrist_roll=60、wrist_pitch=80
- `_clamp` 限位 ±92°（**不要**用 motion_executor.py 的 Q_MIN/Q_MAX，那是废弃管道的坐标系）
- 真机测试脚本必须先调用 `wake_up` 归位 HOME 后再跑后续动作

## 6. 代码改动提示（新对话接手）

下次接手时：
1. **先看本文件** + 当前 `git status` / `git diff` 了解未提交改动
2. **执行未完成测试**：
   ```
   uv run python tests/test_motion_real.py    # 验证 headshake 修复
   PYTHONPATH=. uv run python tests/test_intensity_and_compose.py  # 单元测试
   ```
3. **如果 A5 headshake 通过**：可以考虑 commit
4. **如果要做 LLM 端到端**：起 `main_soul.py`，按 L1-L5 验证

## 7. 文件绝对路径速查

- `/Users/qliau/playground/lelamp_runtime_action/lelamp/service/motors/motion_scripts.py`
- `/Users/qliau/playground/lelamp_runtime_action/lelamp/motion/motion_agent.py`
- `/Users/qliau/playground/lelamp_runtime_action/lelamp/motion/compose_motion.py` (新)
- `/Users/qliau/playground/lelamp_runtime_action/lelamp/soul/soul_agent.py`
- `/Users/qliau/playground/lelamp_runtime_action/tests/test_intensity_and_compose.py` (新)
- `/Users/qliau/playground/lelamp_runtime_action/tests/test_motion_real.py` (新)
