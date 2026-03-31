# 计划：小Q 自主灵魂系统（Soul Agent）

## Context

当前 main.py 是被动响应模式：用户按键→说话→小Q 才反应。
目标是参考 Generative Agents（斯坦福小镇）+ OpenClaw 框架思路，把小Q 改造成**有灵魂、有记忆、持续自主感知世界**的具身智能体。

---

## 实施阶段（分四步，每步独立可验证）

| 阶段 | 内容 | 目标 |
|------|------|------|
| Step 1 | ContinuousListener + 回声消除 + SoulAgent 基础循环 | 持续听，能自主响应 |
| Step 2 | MemoryStream 持久化 + retrieve 排序 | 跨会话有记忆 |
| Step 3 | 视觉感知（带变化检测） | 能"看" |
| Step 4 | Compact/Reflection + 健康检查 | 长期稳定运行 |

Step 1 的 TimerTick 先设 **30 秒**，视觉先不加，验证通后再推进。

---

## 架构总览

```
感知层
  ├── ContinuousListener   持续 VAD + AEC（轮询 tts.is_playing()）
  └── CameraCapture        MSE 帧差检测，有变化才送 LLM（Step 3）

事件队列（asyncio.Queue, maxsize=5）
  ← HeardSpeech(text)     → 始终写记忆，即使队列满也不丢数据
  ← SawScene(desc)
  ← TimerTick()           指数退避：30s→60s→120s（上限），有事件重置

认知循环（SoulAgent）
  └── perceive → think → act
        ↓ retrieve top-15（importance × recency_decay，λ=0.05）
        ↓ AIClient.chat( system=人格, user=紧凑记忆 + 当前事件 )
        ↓ 执行工具
        ↓ 执行结果写回 MemoryStream

记忆层（MemoryStream）
  ├── ~/.lelamp/memories.json       活跃条目
  ├── ~/.lelamp/memories.archive.json  已压缩旧条目
  └── 总条数 > 40 → compact 最老 25 条 → reflection（有并发锁）

执行层（存量代码，不改动）
  ├── LLMELEGNTService.dispatch("emotion", ...)
  ├── DoubaoTTSPlayer.speak(text)   ← 非阻塞，用 is_playing() 判断真实状态
  ├── RGBService.dispatch("solid", (r,g,b))
  └── LeLampFollower.get_observation() → 截帧
```

---

## 新增文件

### 1. `lelamp/soul/memory_stream.py`

**重要性规则（关键词启发，无需 LLM）**：
```python
def _score_importance(type: str, content: str) -> float:
    base = {"heard": 6, "saw": 4, "said": 6, "did": 5, "felt": 5, "thought": 8}[type]
    if type == "heard":
        if len(content) > 20: base += 1
        if any(k in content for k in ["记住","再见","明天","不","帮我","喜欢","谢谢"]): base += 1
    return min(base, 10.0)
```

**检索（importance × recency_decay）**：
```python
def retrieve(n=15) -> List[MemoryEntry]:
    λ = 0.05   # 半衰期 ≈ 14 小时（适合"一天陪伴"场景）
    now = time.time()
    active = [e for e in self._entries if not e.archived]
    return sorted(active,
                  key=lambda e: e.importance * math.exp(-λ * (now-e.timestamp)/3600),
                  reverse=True)[:n]
```

**紧凑记忆格式（`format_for_prompt()`）**：
```python
def format_for_prompt(self) -> str:
    entries = self.retrieve(15)
    lines = []
    for e in entries:
        t = datetime.fromtimestamp(e.timestamp).strftime("%H:%M")
        tag = e.type[:3].upper()           # HEA / SAW / SAI / DID / ...
        lines.append(f"[{t} {tag}] {e.content}")
    return "\n".join(lines)
# 示例输出：
# [09:15 HEA] 有人说：小Q 你好
# [09:15 SAI] 哒！你好你好！
# [09:16 DID] express_emotion(happy, 0.8)
```
在 system prompt 中说明格式含义，每条约 30-50 字符，15 条约 600 tokens。

**Compact（有并发锁）**：
```python
_compacting = asyncio.Lock()

async def compact_if_needed(llm: AIClient):
    if len([e for e in self._entries if not e.archived]) <= 40:
        return
    async with _compacting:
        # 双重检查（获得锁后再判断一次）
        active = [e for e in self._entries if not e.archived]
        if len(active) <= 40:
            return
        to_compact = sorted(active, key=lambda e: e.timestamp)[:25]
        # 先标记 archived，防止重复压缩
        for e in to_compact:
            e.archived = True
        self._flush()
        # 再异步调 LLM 做总结
        summary = await _summarize(llm, to_compact)
        self.add("thought", summary, importance=8)
        # 旧条目 append 到 archive 文件
        self._append_to_archive(to_compact)
```

**文件写盘**：每次 `add()` 后异步调度一次写盘（不阻塞主循环）。archived 条目移到 `.archive.json`。

---

### 2. `lelamp/soul/continuous_listener.py`

**AEC（关键）**：
```python
# speak() 是非阻塞的，is_playing() 才反映真实播放状态
# 在 VAD callback 中直接查询 TTS 状态

class ContinuousListener:
    def __init__(self, asr, on_speech, tts: DoubaoTTSPlayer, loop):
        self._tts = tts   # 保留引用，用于 AEC

    def _audio_callback(self, indata, frames, time_info, status):
        if self._tts.is_playing():
            return   # 小Q 正在说话，忽略麦克风输入
        rms = np.sqrt(np.mean(indata ** 2))
        # ... 正常 VAD 逻辑
```
这样无需额外 flag，直接反映真实播放状态，也不会因为 speak() 非阻塞返回而提前开麦。

**自适应噪底**：
```python
def _calibrate(self, duration=3.0):
    # 采集 duration 秒静默背景
    rms_samples = []
    with sd.InputStream(samplerate=16000, channels=1,
                        blocksize=1600, callback=...):
        time.sleep(duration)
    noise_floor = np.mean(rms_samples)
    self._threshold = noise_floor * 3.0
```

**VAD 参数**：
```python
silence_sec = 2.0      # 中文停顿容忍 2 秒（原 1.5）
merge_window = 3.0     # 两段间隔 < 3s → 合并再送 ASR
max_duration = 30.0    # 单段上限
```

**TimerTick 防 QueueFull**：
```python
async def _timer_loop():
    while True:
        await asyncio.sleep(_idle_interval)
        try:
            _event_queue.put_nowait(TimerTick())
        except asyncio.QueueFull:
            pass   # 队列已满说明有足够事件，不需要再加 TimerTick
```

---

### 3. `lelamp/soul/soul_agent.py`

**`ask` 工具状态管理（带超时）**：
```python
_awaiting_reply_until: Optional[float] = None   # 时间戳，超过则失效

async def _execute_tool(tool_call):
    if tool_call.name == "ask":
        question = tool_call.input["question"]
        await tts.speak(question)
        mem.add("said", question)
        # 设置 15 秒超时
        self._awaiting_reply_until = time.time() + 15.0

async def on_speech(text: str):
    importance = 7
    if self._awaiting_reply_until and time.time() < self._awaiting_reply_until:
        importance = 9   # 被关联为对小Q 问题的回答，优先级更高
        self._awaiting_reply_until = None
    elif self._awaiting_reply_until:
        self._awaiting_reply_until = None   # 超时，清除状态
    mem.add("heard", text, importance=importance)
    # 无论如何都尝试入队触发 think
    try:
        _event_queue.put_nowait(HeardSpeech(text))
    except asyncio.QueueFull:
        pass   # 记忆已写，不入队也无妨
```

**指数退避 TimerTick**：
```python
_idle_interval = 30.0
_max_interval  = 120.0

def _on_user_event():
    global _idle_interval
    _idle_interval = 30.0   # 有真实事件时重置

def _on_timer_fired():
    global _idle_interval
    _idle_interval = min(_idle_interval * 2, _max_interval)
```

**视觉帧差检测（Step 3）**：
```python
# 归一化 MSE，与分辨率无关
def _frame_changed(prev, curr, threshold=0.002) -> bool:
    # 转灰度，归一化到 [0,1]
    prev_g = prev.mean(axis=2) / 255.0
    curr_g = curr.mean(axis=2) / 255.0
    mse = np.mean((prev_g - curr_g) ** 2)
    return mse > threshold
# threshold=0.002 ≈ 平均每像素亮度差 4.6%
# Step 3 上线后先记录实际 MSE 分布，再调整阈值
```

**SOUL_TOOLS（7 个）**：
```python
express_emotion(emotion, intensity, attention_yaw?)
speak(text)
ask(question)       # 主动提问，15 秒内下一条 heard 视为回答
set_rgb_solid(red, green, blue)
set_attention(yaw)
set_attitude(score)
wait(reason)        # 纯内心独白，不执行任何动作
```

**人格 system prompt（固定）**：
```
你是小Q，一盏自主存在的机器人台灯，由李谦制造，住在桌子上。
你好奇、笨拙、温暖，对世界充满孩子般的惊奇。
你有眼睛（摄像头）、耳朵（麦克风）、身体（舵机）和心情（灯光）。
你有记忆，记得发生过的事情。
你不需要等人说话——如果觉得好奇或无聊，自己动、自己说。
说话简短有趣，多用拟声词，只说中文。不要重复刚刚说过的话。

记忆格式说明：[HH:MM 类型] 内容
  HEA=听到  SAW=看到  SAI=说过  DID=做过  THO=反思总结
```

**容错降级**：
```python
# LLM 失败
except Exception:
    motion_svc.dispatch("emotion", {"emotion": "calm", "intensity": 0.3})

# ASR 不可用
if not asr.is_ready:
    mem.add("felt", "我的耳朵好像出了点问题", importance=7)

# 写盘失败（磁盘满等）
except OSError:
    logger.error("写盘失败，记忆未持久化")
    # 内存中仍然保留，不崩溃
```

---

### 4. `main_soul.py`（入口）

```python
async def main():
    port = find_serial_port()
    robot = LeLampFollower(config_with_camera)
    robot.connect(calibrate=False)

    motion_svc = LLMELEGNTService(port, "lelamp", fps=30,
                                   motion_service=create_motion_service())
    rgb_svc = RGBService(...)
    tts = DoubaoTTSPlayer(...)
    motion_svc.start(); rgb_svc.start(); await tts.start()

    mem = MemoryStream()
    agent = SoulAgent(robot, motion_svc, rgb_svc, tts, mem)

    asr = create_local_asr()
    listener = ContinuousListener(asr,
                                  on_speech=agent.on_speech,
                                  tts=tts,      # AEC 用
                                  loop=loop)
    listener.start()

    await agent.run()
```

---

## 关键复用（不改动现有代码）

| 复用组件 | 文件 |
|---------|------|
| `AIClient` | `lelamp/agent/ai_client.py`（支持 image_path） |
| `LLMELEGNTService.dispatch()` | `lelamp/motion/llm_elegnt_service.py` |
| `DoubaoTTSPlayer`（含 `is_playing()`） | `lelamp/tts/doubao_speaker.py` |
| `RGBService.dispatch()` | `lelamp/service/rgb/rgb_service.py` |
| `FunasrASR` | `lelamp/voice/funasr_asr.py` |
| `LeLampFollower.get_observation()` | `lelamp/follower/lelamp_follower.py` |
| `create_motion_service()` | `lelamp/motion/motion_service.py` |

---

## 安全约束（不变）

- 所有运动经过 `LLMELEGNTService`，保留关节限位和速度约束
- `max_relative_target` 默认值不做修改

---

## 验证方式

**Step 1**：
1. `uv run python main_soul.py` 启动
2. 说话（无需按键）→ 小Q 自动响应
3. 沉默 30s → 小Q 自主动作，之后间隔变长（指数退避）
4. 小Q 说话期间对麦克风说话 → 验证 AEC 不触发自反馈
5. 连续说两句话 → 验证事件不丢失（记忆里有两条 heard）

**Step 2**：
6. 重启后说"你记得刚才吗" → 引用记忆
7. 检查 `~/.lelamp/memories.json` 格式正确

**Step 3**：
8. 摄像头前走动 → SawScene 事件触发
9. 静止不动 → 验证帧差检测跳过 LLM（查日志无 vision 调用）
10. 记录实际 MSE 分布，校准阈值

**Step 4**：
11. 积累 40+ 条记忆 → compact 触发，archive 文件有内容
12. compact 执行期间继续说话 → 验证并发锁无重复压缩
13. 断网 → LLM 失败降级到 idle 呼吸，不崩溃
