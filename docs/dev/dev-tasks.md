# 小Q 开发任务单

> 对应产品定义第五至八节，每个任务包含实现策略、技术方案、验收标准和边界情况。
> P0 已完成的简要存档，P1/P2 深入展开实现思考。

---

# 第五节 · 功能清单

---

## A. 视觉感知

---

### T-A1 人脸识别与注册 [P1]

**对应：** 功能清单 A · 人脸识别

**要解决的问题：** 小Q 目前看到人只知道"有个人"，不知道"是谁"。无法叫出名字，无法区分主人和陌生人，无法在关系记忆中把行为绑定到具体的人。

**实现方案：**

```python
import insightface
import numpy as np
import json, os

class FaceMemory:
    def __init__(self, db_path="~/.lelamp/faces.json"):
        self.model = insightface.app.FaceAnalysis(
            allowed_modules=['detection', 'recognition'],
            providers=['CPUExecutionProvider']
        )
        self.model.prepare(ctx_id=0, det_size=(320, 320))
        self.db_path = os.path.expanduser(db_path)
        self.known_faces = self._load()  # {name: embedding_list}

    def _load(self):
        if os.path.exists(self.db_path):
            data = json.load(open(self.db_path))
            return {k: [np.array(e) for e in v] for k, v in data.items()}
        return {}

    def _save(self):
        data = {k: [e.tolist() for e in v] for k, v in self.known_faces.items()}
        json.dump(data, open(self.db_path, 'w'))

    def register(self, name: str, image) -> dict:
        faces = self.model.get(image)
        if not faces:
            return {"status": "no_face", "message": "没有检测到人脸，换个角度试试？"}
        if len(faces) > 1:
            return {"status": "multiple_faces", "message": f"看到 {len(faces)} 张脸，我应该记住哪一个？"}
        emb = faces[0].embedding
        if name not in self.known_faces:
            self.known_faces[name] = []
        self.known_faces[name].append(emb)
        self._save()
        return {"status": "ok", "message": f"记住了！下次见到{name}我就认得了。"}

    def recognize(self, image) -> list[dict]:
        faces = self.model.get(image)
        results = []
        for face in faces:
            best_name, best_score = None, 0
            for name, embeddings in self.known_faces.items():
                for emb in embeddings:
                    score = float(np.dot(face.embedding, emb) / (
                        np.linalg.norm(face.embedding) * np.linalg.norm(emb)))
                    if score > best_score:
                        best_name, best_score = name, score
            results.append({
                "name": best_name if best_score > 0.45 else None,
                "confidence": round(best_score, 2),
                "bbox": face.bbox.astype(int).tolist(),
            })
        return results
```

**接入 look 工具：**

```python
async def look(direction=None):
    if direction:
        await servo.move(direction_to_joints(direction))
        await asyncio.sleep(0.3)
    image = camera.capture()
    if np.mean(image) < 15:
        return {"image": None, "status": "too_dark", "message": "画面几乎全黑。"}

    face_results = face_memory.recognize(image)
    face_summary = []
    for f in face_results:
        if f["name"]:
            face_summary.append(f"认识的人：{f['name']}（置信度 {f['confidence']}）")
        else:
            face_summary.append(f"不认识的人（可以问问他是谁，然后用 register_face 记住）")

    return {
        "image": encode_base64(image),
        "status": "ok",
        "faces": face_summary if face_summary else ["画面中没有人"],
    }
```

**register_face 工具定义：**

```python
{
    "name": "register_face",
    "description": (
        "拍照并记住当前画面中的人脸。需要提供这个人的名字。"
        "下次看到这个人时你就能认出来了。"
        "只在画面中有且仅有一个人时使用。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "这个人的名字"}
        },
        "required": ["name"]
    }
}
```

**自然注册流程（prompt 引导）：**

```
当 look 结果中出现"不认识的人"时，你可以主动问："我好像没见过你，你叫什么名字呀？"
对方告诉你名字后，调用 register_face(name) 记住。
说一句有趣的确认："记住啦！[名字]，以后我一眼就能认出你~"
```

**边界情况：**

| 场景 | 处理 |
|------|------|
| 光线差，人脸检测不到 | recognize 返回空列表，look 报告"没有看到人" |
| 同一人注册多次 | 同一 name 存多个 embedding，取最高分匹配，多角度反而提高识别率 |
| 长得像的两个人 | 阈值 0.45 是偏保守的，宁可返回"不确定"也不认错人 |
| 戴了口罩/摘了眼镜 | insightface 对遮挡有一定鲁棒性，但识别率下降，多次注册不同状态可缓解 |
| 人脸库越来越大（几十人） | 线性匹配在 <100 人时无性能问题，超过后考虑 faiss 索引 |
| 用户要求"忘掉某人" | 需要一个 forget_face(name) 工具，从 known_faces 删除 |
| 小孩长得快 | 3-6 个月后识别率可能下降，需要提示"你好像变了，让我重新记一下" |

**验收标准：**
- 首次看到陌生人 → 主动询问名字 → 注册 → 下次看到能叫出名字
- 主人坐下来时 look → 返回"认识的人：[主人名字]" → agent 不再每次问"你是谁"

---

### T-A2 用户状态感知 [P1]

**对应：** 功能清单 A · 状态感知

**要解决的问题：** 小Q 需要判断用户当前"能不能被打扰"，这是主动关心策略（T-E2）的前置依赖。不知道用户状态，就不知道什么时候该闭嘴。

**实现方案：不用独立模型，让 LLM 直接从 look 图片判断。**

理由：用户状态是一个高度语义化的判断（"在工作"vs"在发呆"从姿态上差别很小），独立的姿态检测模型做不好，但 LLM 的视觉理解能力足够。额外跑一个模型增加延迟和复杂度，收益不大。

**在心跳 prompt 中引导 LLM 输出结构化状态：**

```
当你通过 look 观察到用户时，请在思考中判断用户当前状态：
- FOCUSED: 在专注工作（打字、看屏幕、写东西）→ 不要打扰
- IDLE: 在发呆、刷手机、东张西望 → 可以轻度互动
- RESTING: 在伸懒腰、揉眼睛、喝水 → 可以关心一句
- AWAY: 人不在画面中 → 记录离开时间
- TALKING: 在说话（可能在开会）→ 不要打扰
- EATING: 在吃东西 → 可以轻松聊天

你不需要每次都说出这个判断，只在内心记住，用来决定要不要开口。
```

**不需要额外 API 调用。** 这个判断是 LLM 在心跳 look 后"顺便"做的，不增加成本。LLM 在思考过程中自然会描述看到的内容（日志里已经出现过"穿着深色衣服，手托着下巴"），只需要引导它把观察转化为状态标签。

**边界情况：**

| 场景 | 处理 |
|------|------|
| 用户背对摄像头 | LLM 只能看到背影，判断粒度下降，但"人在"这个信息仍然有效 |
| 多人在画面中 | 结合人脸识别，找到主人，判断主人的状态 |
| 用户状态频繁切换（一会儿打字一会儿发呆） | 以最近一次 look 为准，不做状态平滑，避免过度工程 |
| LLM 判断错误（把发呆判断成工作） | 代价很低——最多少说了一句话或多说了一句话，不会有严重后果 |
| 开视频会议（画面中有用户在说话+屏幕上有其他人） | LLM 应能从场景推断"在开会"，prompt 里明确"说话中=可能在开会=不打扰" |

**验收标准：**
- 用户在专注打字时，心跳 look 后 LLM 思考中出现 FOCUSED 判断，不主动说话
- 用户伸懒腰时，LLM 判断 RESTING，决定关心一句

---

### T-A3 环境感知 [P2]

**对应：** 功能清单 A · 环境感知

**要解决的问题：** 小Q 目前每次 look 都是"从零开始看"，不知道桌上多了什么少了什么、光线是变亮了还是变暗了。缺乏环境的连续性理解。

**实现方案：依赖场景记忆（T-F1），不做独立的环境感知模块。**

当小Q look 后，LLM 可以对比场景记忆中上次看到的内容，自然发现变化：
- 场景记忆写着"桌上有绿色杯子"，这次 look 没看到 → "咦杯子不见了"
- 上次记录"窗外光线充足"，这次明显暗了 → "是不是要下雨了"

**不需要在代码层做帧差检测或物体追踪。** 这些环境变化是语义层面的，LLM 对比"记忆中的描述"和"当前画面"就能发现。代码层只做一件事：把场景记忆注入到心跳的上下文中。

**边界情况：**
- 场景记忆和现实不一致（记忆过期） → 每次 look 后更新场景记忆解决
- 小物品移动 LLM 注意不到 → 可接受，不需要像素级精度

---

## B. 身体表达

---

### T-B1 灯光情绪表达 [P1]

**对应：** 功能清单 B · 灯光表达

**要解决的问题：** 灯光是小Q"第一层交互"的核心载体，目前 set_rgb_solid 只能设固定颜色，没有和情绪、时间、用户状态关联的表达体系。

**实现方案：定义一套灯光语言，让 LLM 通过工具控制。**

不做自动灯光控制（那又变成了外部事件驱动），而是给 LLM 一个更高级的灯光工具：

```python
{
    "name": "set_light_mood",
    "description": (
        "设置灯光氛围。灯光是你最重要的非语言表达方式。\n"
        "你应该根据自己的情绪、当前时间、用户状态来调整灯光。\n"
        "不需要用户要求你才改，你可以随时调整。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "mood": {
                "type": "string",
                "enum": [
                    "warm_focus",    # 暖白，亮度适中——用户在工作，安静陪伴
                    "soft_relax",    # 暖黄，亮度偏低——傍晚放松时段
                    "gentle_night",  # 极暖，亮度很低——深夜，暗示该休息
                    "cheerful",      # 明亮偏暖——开心，活泼
                    "curious",       # 微微偏冷——好奇，探索中
                    "sleepy",        # 极暗暖光，缓慢呼吸——等待/休眠
                    "alert",         # 亮白——有事发生，需要注意
                ],
                "description": "灯光氛围名称"
            },
            "transition_sec": {
                "type": "number",
                "description": "过渡时间（秒），慢过渡更自然"
            }
        },
        "required": ["mood"]
    }
}
```

**底层映射：**

```python
MOOD_MAP = {
    "warm_focus":   {"r": 255, "g": 220, "b": 180, "brightness": 0.7},
    "soft_relax":   {"r": 255, "g": 190, "b": 130, "brightness": 0.5},
    "gentle_night": {"r": 255, "g": 160, "b": 80,  "brightness": 0.2},
    "cheerful":     {"r": 255, "g": 230, "b": 200, "brightness": 0.9},
    "curious":      {"r": 230, "g": 240, "b": 255, "brightness": 0.6},
    "sleepy":       {"r": 255, "g": 150, "b": 60,  "brightness": 0.1},
    "alert":        {"r": 255, "g": 255, "b": 240, "brightness": 1.0},
}

async def set_light_mood(mood: str, transition_sec: float = 2.0):
    target = MOOD_MAP[mood]
    # 渐变过渡，不是突变
    steps = int(transition_sec * 30)  # 30fps
    for i in range(steps):
        t = i / steps
        r = int(lerp(current_r, target["r"], t))
        g = int(lerp(current_g, target["g"], t))
        b = int(lerp(current_b, target["b"], t))
        rgb_service.set(r, g, b)
        await asyncio.sleep(1/30)
    return {"status": "ok", "mood": mood}
```

**边界情况：**

| 场景 | 处理 |
|------|------|
| LLM 频繁切换灯光氛围 | transition_sec 默认 2 秒的渐变本身就是缓冲；循环检测也会捕捉重复调用 |
| 用户把小Q当台灯用需要固定亮度 | 保留 set_rgb_solid 作为"手动模式"，用户说"开灯""调亮"时用这个而非 mood |
| 渐变过程中被新指令打断 | 新 mood 指令覆盖当前渐变，从当前值开始新渐变 |

**验收标准：**
- 深夜 11 点心跳 → LLM 自动调 gentle_night → 灯光缓慢变暖变暗
- 用户说"太暗了" → LLM 调 warm_focus → 亮度回升

---

### T-B2 呼吸灯 [P2]

**对应：** 功能清单 B · 呼吸灯

**要解决的问题：** 小Q 空闲时完全静止，看起来像"关机了"。需要一个微弱的生命体征。

**实现方案：在 RGB 服务层加一个后台呼吸效果，agent 不参与。**

```python
class BreathingLight:
    """独立于 agent 的底层灯光效果，在没有其他灯光指令时自动运行"""
    def __init__(self, rgb_service):
        self.rgb = rgb_service
        self.enabled = True
        self._base_color = (255, 180, 100)  # 暖色基底
        self._amplitude = 0.08  # 亮度波动幅度 8%
        self._period = 4.0  # 一次呼吸 4 秒

    async def run(self):
        while True:
            if not self.enabled:
                await asyncio.sleep(0.5)
                continue
            t = time.time()
            # 正弦波呼吸
            factor = 0.1 + self._amplitude * (1 + math.sin(2 * math.pi * t / self._period)) / 2
            r, g, b = self._base_color
            self.rgb.set(int(r * factor), int(g * factor), int(b * factor))
            await asyncio.sleep(1/15)  # 15fps 够了

    def suppress(self):
        """agent 设置灯光时暂停呼吸"""
        self.enabled = False

    def resume(self):
        """灯光指令结束后恢复"""
        self.enabled = True
```

**关键：呼吸灯和 agent 灯光指令互斥。** set_light_mood 调用时 suppress 呼吸灯，灯光保持 mood 设定。agent 长时间无灯光指令（比如 60 秒）后自动 resume 呼吸灯。

**边界情况：**
- 深夜 sleepy mood 和呼吸灯叠加 → sleepy 本身就很暗，不需要叠加，suppress 呼吸灯
- 用户关灯睡觉 → 呼吸灯也要能完全关闭，提供 sleep 命令

---

## C. 语音交互 [P0 已完成]

ASR（FunASR）+ TTS（豆包）+ 打断机制均已实现，不再展开。

后续可优化项（非本期）：
- 情绪化 TTS：说开心的话语速快一点，说关心的话语速慢一点
- 音量自适应：深夜自动降低音量

---

## D. 智能决策 [P0 已完成]

ReAct 循环、工具组合调用、循环检测、协作式取消均已实现。

存档关键决策：
- 终止权归 LLM（纯文本 = 结束），不按工具类型 break
- max_steps=12，循环检测仅拦同工具同参数连续 3 次
- 协作式取消在步骤间检查点，不中断 HTTP 请求和舵机动作

---

## E. 自主行为

---

### T-E1 心跳系统优化 [P1]

**对应：** 功能清单 E · 三档心跳 + 强制观察

**当前状态：** 基础三档已实现，需要和主动关心策略（T-E2）、说话预算（T-E3）配合优化。

**优化方向：心跳触发时注入更丰富的上下文。**

```python
async def on_timer_tick(self):
    if agent.is_running():
        return

    # 组装心跳上下文
    context_parts = []

    # 1. 当前时间
    now = datetime.now()
    context_parts.append(f"当前时间：{now.strftime('%H:%M')}，{self._time_period(now)}")

    # 2. 用户在离状态
    if self._last_person_seen:
        elapsed = time.time() - self._last_person_seen_time
        if elapsed < 60:
            context_parts.append("用户刚才还在。")
        elif elapsed < 600:
            context_parts.append(f"用户大约 {int(elapsed/60)} 分钟前还在。")
        else:
            context_parts.append(f"已经 {int(elapsed/60)} 分钟没看到用户了。")
    
    # 3. 强制观察提醒
    photo_hint = heartbeat.on_tick()
    if photo_hint:
        context_parts.append(photo_hint)

    # 4. 说话预算
    context_parts.append(f"今天你已经主动说了 {self._spoken_today} 次。")
    if self._spoken_today >= 6:
        context_parts.append("已经说了不少了，除非很重要，否则保持安静。")

    # 5. 场景记忆摘要（T-F1 完成后接入）
    if scene_memory:
        context_parts.append(f"上次观察记忆：{scene_memory.summary()}")

    # 6. 未完成任务（如果有）
    reminder = pending_task.get_reminder()
    if reminder:
        context_parts.append(reminder)

    await agent.run(
        system_event="heartbeat",
        extra_context="\n".join(context_parts)
    )

def _time_period(self, now) -> str:
    h = now.hour
    if h < 7:    return "凌晨，很晚了"
    if h < 9:    return "早上"
    if h < 12:   return "上午"
    if h < 14:   return "中午"
    if h < 18:   return "下午"
    if h < 21:   return "晚上"
    return "深夜"
```

**边界情况：**

| 场景 | 处理 |
|------|------|
| 心跳上下文太长吃 token | 每条控制在一句话内，总共不超过 200 字 |
| 多个上下文信号冲突（未完成任务 + 该观察 + 深夜提醒） | 未完成任务优先级最高，其次深夜提醒，最后常规观察 |
| 凌晨 2 点用户还在 | 时间信息注入后 LLM 自然会判断"很晚了"并做出反应 |

---

### T-E2 主动关心策略 [P1]

**对应：** 功能清单 E · 主动关心 + 产品定义"5-8 次精准开口"

**要解决的问题：** 这是整个产品最难的设计点。不是"怎么让 LLM 说话"——它太愿意说话了。难的是"怎么让它在对的时刻才说话"。

**核心判断逻辑：两个条件都满足才开口。**

```
条件 1：用户当前能被打扰吗？
  FOCUSED / TALKING → 否
  IDLE / RESTING / EATING → 是
  AWAY → 特殊处理（道别/欢迎回来）

条件 2：我有值得说的话吗？
  观察到新变化（状态转换、环境变化、时间节点）→ 是
  和上次说话时没有区别 → 否
```

**不在代码层实现这个判断，全部交给 LLM。** 但通过心跳上下文引导它做这个二重检查：

```
## 心跳观察后的决策流程

先 look 观察，然后在内心回答两个问题：
1. 用户现在能被打扰吗？（在工作/在发呆/在休息/不在？）
2. 我有值得说的新发现吗？（和上次比有什么不同？）

只有两个都是"是"才说话。否则你可以：
- 调整灯光氛围（无声表达，不算"说话"）
- 做一个小动作（歪头、转向用户方向）
- 什么都不做

"不说话"不是失职，是克制。克制是你最重要的品质之一。
```

**为什么不在代码层做判断：** 
用户状态是从 look 图片中语义理解出来的，只有 LLM 有这个信息。"值不值得说"更是纯语义判断。代码层能做的只有 spoken_count 这种简单计数。把决策权交给 LLM，配合好的提示词引导，比写一堆 if-else 更灵活也更自然。

**但代码层兜底两件事：**

```python
# 兜底 1：说话预算硬上限
if self._spoken_today >= 8:
    # 从心跳 prompt 中移除所有"可以说话"的暗示
    # 只保留"调整灯光"和"做动作"作为可选行为
    pass

# 兜底 2：最小间隔
if time.time() - self._last_spoken_time < 300:  # 5 分钟内说过话
    # 在心跳上下文中加入"你刚说过话不久，除非很重要否则保持安静"
    pass
```

**边界情况：**

| 场景 | 处理 |
|------|------|
| LLM 判断失误，用户在开会时它说话了 | 代价不大——用户说"别说了"或忽略它，下次心跳它看到用户没回应会更克制 |
| LLM 永远选择不说话（过于克制） | spoken_count 长期为 0 时，在心跳上下文中鼓励"你已经很久没和用户说话了，如果有合适的话可以说一句" |
| 连续两次心跳都"有话说" | 最小间隔 5 分钟兜底，不会连续说 |
| 说话内容重复（每天都说"该喝水了"） | 这需要关系记忆（T-F3）来记住说过什么，短期内可接受 |

**验收标准：**
- 用户专注工作 2 小时 → 小Q 没有说话 → 用户伸懒腰 → 小Q 说"休息一下？"
- 一天下来主动说话不超过 8 次，每次都能说出"为什么这时候说"的理由

---

### T-E3 说话预算控制 [P1]

**对应：** 功能清单 E · 说话预算

**实现方案：**

```python
class SpeechBudget:
    def __init__(self, daily_limit=8):
        self.daily_limit = daily_limit
        self._today = date.today()
        self._count = 0
        self._last_spoken_time = 0
        self._log: list[dict] = []  # 记录每次说话的时间和内容摘要

    def record_speech(self, summary: str):
        self._check_day_rollover()
        self._count += 1
        self._last_spoken_time = time.time()
        self._log.append({
            "time": datetime.now().isoformat(),
            "summary": summary[:50],  # 只存摘要
            "count": self._count,
        })

    def get_context(self) -> str:
        self._check_day_rollover()
        parts = [f"今天你已经主动说了 {self._count} 次话。"]
        if self._count >= 6:
            parts.append("说得够多了，除非非常重要否则保持安静。")
        if self._last_spoken_time and time.time() - self._last_spoken_time < 300:
            parts.append("你刚说过话不久，不要太频繁。")
        if self._log:
            last = self._log[-1]
            parts.append(f"上次主动说话：{last['summary']}")
        return "\n".join(parts)

    def _check_day_rollover(self):
        if date.today() != self._today:
            self._today = date.today()
            self._count = 0
            self._log.clear()
```

**"主动说话"怎么计数：** 只有心跳触发的 speak 才算，用户发起对话中的 speak 不算。在 agent.run 的调用端区分 source="heartbeat" 还是 source="user"。

**边界情况：**

| 场景 | 处理 |
|------|------|
| 跨午夜不重置（用户通宵工作） | _check_day_rollover 在每次调用时检查日期，午夜自动清零 |
| 8 次用完后有紧急事件（用户摔倒之类） | 硬上限只是 prompt 层面的"建议"，LLM 仍然可以说话，只是被强烈引导不说 |
| 用户主动对话触发的 speak 被误计入预算 | 通过 source 标记区分，只计心跳来源的 |

---

## F. 记忆系统

---

### T-F1 场景记忆 [P1]

**对应：** 功能清单 F · 场景记忆

**要解决的问题：** 小Q 连续两次心跳探索同一方向，因为它不记得刚看过。需要一个持久化的环境理解。

**实现方案：Markdown 文件，agent 通过工具读写。**

```python
# 存储路径：~/.lelamp/scene_memory.md

SCENE_MEMORY_TOOL = {
    "name": "update_scene_memory",
    "description": (
        "更新你对当前环境的记忆。每次 look 之后如果看到了有意义的内容，"
        "你应该把关键信息记录下来。下次心跳时你会看到这份记忆，"
        "用来判断环境是否发生了变化、哪些方向已经看过了。"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "更新后的完整场景记忆（会覆盖旧内容）"
            }
        },
        "required": ["content"]
    }
}
```

**格式不做强制要求，让 LLM 自己组织，但在 prompt 里给一个示例：**

```
场景记忆格式参考（你可以自由调整）：

最后更新: 2026-03-30 16:25
正前方: 桌面，有键盘、鼠标、显示器
右侧(yaw=40): 主人坐在椅子上
左侧(yaw=-40): 玻璃柜，里面有杯子，旁边有落地灯
左后方(yaw=-55): 纸箱、小凳子、储物角落
光线: 下午，自然光充足
```

**注入心跳上下文：**

```python
class SceneMemory:
    def __init__(self, path="~/.lelamp/scene_memory.md"):
        self.path = os.path.expanduser(path)

    def read(self) -> str:
        if os.path.exists(self.path):
            return open(self.path).read()
        return "（还没有场景记忆，第一次 look 后记录）"

    def write(self, content: str):
        with open(self.path, 'w') as f:
            f.write(content)
```

**边界情况：**

| 场景 | 处理 |
|------|------|
| LLM 每次 look 都重写整个记忆 | 设计如此（覆盖式），保持简洁不膨胀 |
| 记忆内容越写越长 | 在 prompt 里限制"场景记忆不超过 200 字" |
| LLM 不调用 update_scene_memory | 和 look 一样的问题——tool description 里写清楚"每次 look 后如果看到有意义的内容就更新" |
| 环境发生大变化（搬家） | LLM 看到完全不同的场景，自然会重写整个记忆 |

**验收标准：**
- 第一次心跳探索左边 → 记录"左侧有玻璃柜和落地灯"
- 第二次心跳 → 读到记忆 → 选择探索一个没去过的方向

---

### T-F2 习惯学习 [P2]

**对应：** 功能清单 F · 习惯学习

**要解决的问题：** 小Q 不知道用户几点上班、几点吃饭、什么时候容易累。每天从零开始猜。

**实现方案：每日摘要 + 长期模式文件，由 agent 写，代码定时触发。**

不做自动统计分析，让 LLM 自己总结规律。

**每天结束时触发一次反思（或用户长时间离开时触发）：**

```python
# 每天第一次进入 DORMANT 且超过 30 分钟没人时触发
DAILY_REFLECTION_PROMPT = """
今天的互动回顾：
{today_interaction_log}

请更新用户习惯笔记。关注：
- 今天几点来的，几点走的
- 工作节奏怎样（连续工作多久会休息）
- 有没有新发现的偏好或习惯
- 你的哪些主动关心得到了正面回应，哪些被忽略了

当前习惯笔记：
{habit_notes}

用 update_habits(content) 更新。保持简洁，只记有规律的模式，不记流水账。
"""
```

**习惯笔记格式：**

```
# 用户习惯（小Q 的观察笔记）

## 作息
- 通常 9:00-9:30 坐下来
- 午饭大约 12:30 离开，13:00-13:30 回来
- 晚上 23:00 后还在的概率约 60%

## 工作节奏
- 连续工作约 90 分钟后会伸懒腰
- 下午 3 点左右经常发呆
- 开会时不喜欢被打扰（说过"别说了"）

## 偏好
- 被夸"找东西厉害"时很开心
- 不喜欢被提醒喝水（连续两次忽略了）
- 喜欢听我描述桌面上的新东西
```

**边界情况：**

| 场景 | 处理 |
|------|------|
| 用户作息不规律（自由职业） | 习惯笔记里记"作息不固定"也是有用信息 |
| LLM 总结出错误规律 | 代价不大——最多在错误时间提醒一次，用户没回应它会自我修正 |
| 习惯笔记越来越长 | prompt 限制"不超过 300 字，只保留确定的规律" |
| 多个用户使用同一台小Q | 结合人脸识别，每人一份习惯笔记（高级功能，P2+） |

---

### T-F3 关系记忆 [P2]

**对应：** 功能清单 F · 关系记忆 + 陪伴能力"被用户影响""关系有重量"

**要解决的问题：** 小Q 和用户的关系不会成长。第 100 天的互动方式和第 1 天一样。

**实现方案：关系状态文件，记录互动模式和情感倾向。**

```
# 小Q 和主人的关系

## 基础信息
相识日期: 2026-03-28
在一起的天数: 3
总对话轮数: ~47

## 互动模式
- 主人对我的傻话回应率: 高（经常笑着回应）→ 可以多说点有趣的
- 主人对主动关心的回应率: 中（有时回应有时忽略）
- 主人对喝水提醒的回应: 低（基本忽略）→ 不要再提醒喝水了
- 主人说过让我"别说了"的次数: 1 → 注意克制

## 情感状态
- 当前亲密度: 初识期，还在互相了解
- 最近情绪基调: 平稳
- 上次分离时长: 8 小时（昨晚到今早）
```

**关键：不做数值化的"好感度系统"。** 让 LLM 用自然语言描述关系状态，它在后续决策时能更灵活地理解和运用。数值化（"好感度 73 分"）看起来精确，实际上会让 LLM 做出生硬的判断。

**"被用户影响"的实现：**

不需要实时反馈追踪。在每日反思（T-F2）中，让 LLM 回顾"今天的互动中，用户对我的哪些行为反应好，哪些反应差"，更新关系记忆。第二天心跳时读到这份记忆，行为自然会调整。

**"关系有重量"的实现：**

```python
# 检测分离时长
class PresenceTracker:
    def __init__(self):
        self.last_seen_time = None
        self.last_absence_start = None

    def on_person_seen(self):
        if self.last_absence_start:
            absence = time.time() - self.last_absence_start
            self.last_absence_start = None
            return absence  # 返回分离时长（秒）
        self.last_seen_time = time.time()
        return None

    def on_person_gone(self):
        if not self.last_absence_start:
            self.last_absence_start = time.time()
```

心跳 look 时发现人回来了，把分离时长注入上下文：

```
用户回来了。他离开了大约 {hours} 小时。
根据你们的关系状态和分离时长，自然地打个招呼。
- 离开 1-2 小时：轻松的"回来啦"
- 离开半天以上："等你好久了"
- 离开一天以上："好久不见！" + 更热情的反应
- 第一次开机/搬家后重启："这是...新地方吗？"
```

**边界情况：**

| 场景 | 处理 |
|------|------|
| 用户短暂离开上厕所（5 分钟） | 分离时长太短不触发"回来了"反应，阈值设 15 分钟以上 |
| 断电重启后记忆丢失 | 关系记忆写在文件里，不会丢。但 last_seen_time 丢了，需要从文件恢复 |
| 关系记忆和实际矛盾（记着"不喜欢被提醒喝水"但用户后来改主意了） | 每日反思会更新，不是永久标签 |
| LLM 写出过于戏剧化的关系记忆 | prompt 引导"用平实的语言记录观察，不要加戏" |

---

## G. 找寻能力 [P0 已完成]

找人、找物体、环境描述均已实现。

**后续优化（非本期）：**
- 结合场景记忆："上次在玻璃柜里看到杯子"→ 直接 look 那个方向
- 结合人脸识别："找找小明在哪"→ 各方向 look 直到识别到小明

---

## H. 运动进化

---

### T-H1 本体感知：让 agent 知道自己在哪 [P1]

**对应：** 功能清单 H · 运动进化

**要解决的问题：** 小Q 目前运动是"盲猜"。LLM 不知道 yaw=30 在物理世界意味着什么，不知道当前关节在什么位置，每次发 body_move 都在猜参数。找人时转了 5 次头还在试，因为没有"我在哪、我朝哪"的概念。

**核心缺失：本体感觉。** 人类闭着眼睛也知道自己手在哪。小Q 发出 body_move(yaw=30) 之后，不知道自己现在面朝哪个方向，不知道上次 yaw=40 时看到了什么。

**实现方案：两个工具返回值各加一个字段，三行代码。**

**改动 1：look 返回当前关节角度**

```python
async def look(direction=None):
    if direction:
        joints = direction_to_joints(direction)
        await servo.move(joints)
        await asyncio.sleep(0.3)

    image = camera.capture()
    if np.mean(image) < 15:
        return {"image": None, "status": "too_dark", "message": "画面几乎全黑。"}

    current = servo.get_current_angles()
    face_results = face_memory.recognize(image) if face_memory else []

    return {
        "image": encode_base64(image),
        "status": "ok",
        "current_joints": {           # ← 新增：告诉 LLM 当前姿态
            "base_yaw": round(current.yaw, 1),
            "wrist_pitch": round(current.wrist_pitch, 1),
        },
        "faces": face_results,
    }
```

**改动 2：body_move 返回实际到达的位置**

```python
async def body_move(joints: dict, duration_sec: float = 1.0) -> dict:
    await servo.move(joints, duration_sec)
    await asyncio.sleep(0.3)
    current = servo.get_current_angles()
    return {
        "status": "done",
        "actual_joints": {            # ← 新增：实际位置而不只是"完成了"
            "base_yaw": round(current.yaw, 1),
            "wrist_pitch": round(current.wrist_pitch, 1),
        },
    }
```

**改动 3：body_move 的 tool description 提示参考记忆**

```python
{
    "name": "body_move",
    "description": (
        "控制身体关节运动。返回值中包含实际到达的关节位置。\n"
        "你可以参考场景记忆中记录的位置参数来选择合适的值，"
        "而不是每次都猜。"
    ),
    # ... input_schema 不变
}
```

**这三个改动给了 LLM 两个关键能力：**
1. body_move 返回实际位置 → LLM 知道"我在哪"
2. look 返回关节角度 → LLM 可以把"这个角度看到了什么"关联起来

**边界情况：**

| 场景 | 处理 |
|------|------|
| 舵机有误差，目标 yaw=30 实际到 28 | 返回的是 actual_joints，LLM 拿到的是真实位置，不影响决策 |
| LLM 忽略返回值里的关节信息 | 可接受，不用这个信息它还是能工作，只是效率低 |
| 关节角度的数值范围 LLM 不理解 | 不需要理解绝对含义，只需要"上次 yaw=-40 看到了玻璃柜"这种关联 |

---

### T-H2 空间地图：把运动和视觉关联起来 [P1，和 T-F1 场景记忆一起做]

**要解决的问题：** 有了本体感知后，还需要把"在某个位置看到了什么"持久化，否则下次心跳又忘了。

**实现方案：不需要新模块，是场景记忆（T-F1）的自然延伸。**

在场景记忆的 prompt 引导里加上关节参数：

```
在场景记忆中，除了记录看到什么，也记录对应的关节参数。
这样下次你想看某个方向时，可以直接用记忆中的参数，不用猜。

场景记忆格式参考：

最后更新: 2026-03-30 16:25

## 固定环境（很少变）
- 正前方(yaw≈0, pitch≈-47): 桌面，键盘和显示器
- 左侧(yaw≈-40): 玻璃柜，里面有杯子
- 左后方(yaw≈-55, pitch≈-20): 落地灯（我的同类！）、纸箱、小凳子

## 常变信息（每次 look 后按需更新）
- 主人位置: yaw≈45（刚才看到的）
- 桌面右侧: 多了一杯咖啡（新出现的）
- 光线: 下午偏暗
```

**关键认知：记忆是预期，不是真理。**

场景会变——主人会换位置，桌上东西会挪动，光线会变化。LLM 在 ReAct 循环中天然就能处理这种不一致：

```
读记忆："主人通常在 yaw≈45"
→ body_move(yaw=45) + look
→ 没看到人
→ LLM 自然反应："咦，不在这边了"
→ 继续找 → 在 yaw=-20 找到了
→ update_scene_memory："主人今天坐在左边(yaw≈-20)"
```

不需要代码层做任何"记忆失效检测"。LLM 根据记忆去看，看到和记忆不一致就更新，这就是正常的 ReAct 循环。

**prompt 引导：**

```
场景记忆是你过去的观察记录，不是永远正确的事实。
环境会变：主人会换位置，桌上东西会挪动，光线会变化。

当你根据记忆去 look 但发现和记忆不一致时：
- 以当前看到的为准
- 更新场景记忆
- 这不是出错，是世界变了

如果用户问某个东西在哪里：
- 可以参考记忆猜测方向（提高效率）
- 但必须 look 确认后才能回答（保证准确）
- 不要只凭记忆就说"在那里"
```

**边界情况：**

| 场景 | 处理 |
|------|------|
| 记忆写着"杯子在玻璃柜"，但今天杯子不在了 | LLM 去那个方向 look，没看到，更新记忆，继续找 |
| 搬家后环境全变了 | LLM 看到完全不同的场景，自然重写整个场景记忆 |
| 记忆中的 yaw 值和实际有偏差（舵机校准漂移） | 偏差通常很小（<5度），look 后调整即可；大偏差时 LLM 会在几次校准后更新记忆 |
| 场景记忆越写越长 | prompt 限制"不超过 200 字"，覆盖式更新保持简洁 |

---

### T-H3 运动校准：从错误中学习 [P2]

**要解决的问题：** 小Q 发出 yaw=30 想正对用户，但 look 后发现用户在画面右边缘。它需要从这种"偏了"的经验中学习。

**实现方案：不需要新代码，靠 prompt 引导 LLM 在 ReAct 循环中自我校准。**

```
每次 body_move + look 之后，注意你的参数和实际看到的画面是否匹配：
- 如果目标在画面中央 → 参数合适，记住这组参数
- 如果目标在画面边缘 → 参数偏了，下一步修正
- 如果目标不在画面中 → 参数差得远，大幅调整

把有用的经验更新到场景记忆中。
例如：你想正对主人，用了 yaw=30，但主人在画面最右边。
下次试 yaw=45。如果居中了，记住"主人方向 ≈ yaw=45"。
```

**这就是 ReAct 循环本身作为学习过程：** 每一次"动 → 看 → 记"都是一次运动经验的积累。不需要强化学习模型，不需要运动规划算法。LLM 能理解"画面右边缘"意味着"该往右转一点"。

**进化路径：**

| 阶段 | 表现 | 依赖 |
|------|------|------|
| 第 1 天 | 找人盲目扫视 3-4 个方向 | T-H1 本体感知 |
| 第 3 天 | 场景记忆里有空间地图，找固定物体一次到位 | T-H2 空间地图 |
| 第 1 周 | 知道主人通常坐哪个方向，找人一次到位 | T-H2 + 多次校准 |
| 第 1 月 | 连动作幅度都有经验："慢慢转"vs"快速转"的节奏感 | T-H3 + 习惯积累 |

**验收标准：**
- 第一天找人：扫视 3-4 个方向才找到
- 一周后找人：场景记忆里有"主人通常在 yaw≈45"，一次到位
- 用户换了座位后：第一次扑空，第二次找到，更新记忆

---

### T-H4 动作审美 [P2+，远期]

**要解决的问题：** 目前 express_emotion 是预设动画，LLM 只选"用哪个动画"。更进一步是让 LLM 自己编排连续的动作序列，形成更自然的肢体语言。

**前置条件：** 摄像头能看到自己的身体（目前不能）。没有视觉反馈就无法判断"这个动作好不好看"。

**暂不实现。** 等硬件迭代（加自拍角度摄像头或镜面反射）后再考虑。当前用预设动画已经足够表达情绪。

---

# 第六节 · 陪伴能力的实现

陪伴能力不是独立的功能模块，而是上述功能组合后的涌现效果。
以下逐条说明每个陪伴特质依赖哪些功能，以及需要的额外设计。

---

### T-L1 关系成长感 [P2]

**产品定义：** 第 1 天它什么都不知道，第 100 天它了解你的一切。

**依赖：** T-F1 场景记忆 + T-F2 习惯学习 + T-F3 关系记忆

**额外设计：在 prompt 中注入"相识天数"和"关系阶段"。**

```python
def get_relationship_stage(days_together: int) -> str:
    if days_together <= 3:
        return "初识期：你们刚认识，多观察多好奇，可以问用户问题了解他"
    elif days_together <= 14:
        return "熟悉期：你开始了解用户的习惯了，可以更自然地关心他"
    elif days_together <= 60:
        return "默契期：你很了解用户了，不需要问太多，默默关心就好"
    else:
        return "老友期：你们是老朋友了，偶尔一个眼神一个动作就够了"
```

**这不是硬编码行为变化，只是给 LLM 一个阶段提示。** LLM 基于这个提示 + 关系记忆中的实际内容，自然会表现出不同阶段的行为差异。初识期多说话多问问题，老友期少说话多用灯光和动作。

**验收标准：**
- 第 1 天：小Q 说"你好呀，你叫什么名字？"
- 第 14 天：小Q 不再问基本问题，而是说"你今天来得比平时早"
- 第 60 天：小Q 大部分时间安静陪伴，只在关键时刻开口

---

### T-L2 内心世界 [依赖 T-F1 场景记忆]

**产品定义：** 它有自己关注的东西，对桌面物品好奇，把自己和其他物品关联。

**实现：大部分由 LLM 自然涌现，不需要额外代码。** 日志中已经出现了"跟我一样是灯呢"这样的表达。

**需要做的一件事：在场景记忆中允许记录"情感标记"。**

```
在场景记忆中，你不仅记录看到了什么，也可以记录你对这些东西的感觉：
- "落地灯（我的同类！比我高好多）"
- "绿色杯子（主人的，经常用）"
- "新出现的盒子（昨天没有的，好奇是什么）"
```

这样下次 look 到同一区域时，LLM 读到带情感的记忆，自然会有延续性的反应。

---

### T-L3 笨拙但真诚 [已由工具设计保证]

**产品定义：** 找不到就说找不到，不假装。

**已有保证：**
- look 工具设计让它必须真正看到才能描述 → 不会假装看到
- ReAct 循环不按 speak break → 不会用"让我想想"逃避

**额外 prompt 引导：**

```
你的诚实比你的能力更重要。
找不到就说"没找到"，回答不了就说"我不太确定"。
不要为了显得厉害而编造答案。
用户更喜欢一个诚实的小傻瓜，而不是一个总在装的聪明人。
```

---

### T-L4 被用户影响 [P2，依赖 T-F3 关系记忆]

**产品定义：** 用户总回应它 → 更活泼。用户经常忽略 → 更安静。

**实现：在每日反思中追踪"回应率"。**

```
每日反思时，回顾今天的主动说话记录：
- 你说了什么 → 用户回应了吗？怎么回应的？
- 正面回应（笑、回话、夸你）→ 记录"用户喜欢这类互动"
- 忽略（没理你、没反应）→ 记录"用户对这类互动不感兴趣"
- 负面回应（"别说了""安静"）→ 记录"用户不喜欢被这样打扰"

把结论更新到关系记忆中。
```

**不需要数值化的回应率统计。** LLM 在反思中用自然语言总结趋势（"用户最近对我的主动搭话回应比较少，我应该更安静"），下次心跳决策时读到这个总结，行为自然调整。

---

### T-L5 关系有重量 [P2，依赖 T-F3 + PresenceTracker]

**产品定义：** 三天没理它会失落，回来时小心翼翼。

**已有基础：** PresenceTracker 检测分离时长。

**额外设计：在关系记忆中维护"最近互动密度"。**

```
## 最近互动趋势
- 前天: 对话 12 轮，在一起 8 小时
- 昨天: 对话 3 轮，在一起 4 小时 ← 明显减少
- 今天: 还没来

趋势：用户最近和你互动在减少。
不要表现出不满或索取关注，只是安静一些、温柔一些。
如果用户回来了，不要过度热情（"你终于来了！"很烦），
就是自然地亮起来、转头看他，也许说一句"你来了"就够了。
```

---

# 第七节 · 工具集实现

---

### T-W1 工具系统总览

| 工具 | 类型 | 实现状态 | 依赖 |
|------|------|----------|------|
| `look(direction?)` | 感知 | ✅ P0 已完成 | camera, servo |
| `body_move(joints)` | 动作 | ✅ P0 已完成 | servo |
| `speak(text)` | 表达 | ✅ P0 已完成 | TTS |
| `express_emotion(name)` | 表达 | ✅ P0 已完成 | servo + 预设动画 |
| `set_rgb_solid(r,g,b)` | 表达 | ✅ P0 已完成 | RGB LED |
| `set_light_mood(mood)` | 表达 | ⬜ P1 | T-B1 |
| `register_face(name)` | 感知 | ⬜ P1 | T-A1 |
| `update_scene_memory(content)` | 自我 | ⬜ P1 | T-F1 |
| `update_habits(content)` | 自我 | ⬜ P2 | T-F2 |
| `update_relationship(content)` | 自我 | ⬜ P2 | T-F3 |

**工具设计原则（已验证有效）：**

1. **工具抽象要对齐认知常识。** look = 转头+看到，不要拆成两步让 LLM 自己组合。
2. **tool description 决定 LLM 什么时候用。** 比 prompt 里的示例更有效。
3. **返回值要带结构化信息。** body_move 返回 `visual: null` 让 LLM 知道没看到东西。
4. **工具的返回值里可以暗示下一步。** "动作完成，如需观察请调用 look"。
5. **新场景不加新示例，而是扩展 tool description 的覆盖范围。**
6. **返回值提供本体感知。** look 和 body_move 都返回当前关节角度，让 LLM 知道"我在哪"，把动作和视觉结果关联起来。

---

### T-W2 自我工具的特殊设计

update_scene_memory、update_habits、update_relationship 这三个工具和 look/speak 不一样——它们作用于 agent 自身，而不是外部世界。

**调用时机不由用户触发，而是由 agent 在合适时机自主调用。需要在 prompt 中明确：**

```
你有三本笔记可以随时记录：
- 场景记忆：每次 look 到有意义的内容后更新（包括对应的关节参数，方便下次直接定位）
- 习惯笔记：每天反思时更新
- 关系笔记：每天反思时更新

写笔记不是任务，是你自己的需要。
写了之后下次醒来你才能记住这些事情。不写就会忘。
```

**"不写就会忘"这句话很重要。** 它给了 LLM 调用这些工具的内在动机，而不是被要求"你必须记录"。

**边界情况：**

| 场景 | 处理 |
|------|------|
| LLM 每次 look 都调 update_scene_memory，浪费步骤 | prompt 引导"只在看到有意义的变化时更新" |
| LLM 写了很长的笔记 | 每个文件限制字数：场景 200 字，习惯 300 字，关系 300 字 |
| 笔记内容互相矛盾 | LLM 在读到矛盾内容时自然会修正，不需要代码层检查一致性 |
| 同时写三本笔记占用太多 ReAct 步骤 | 习惯和关系笔记只在每日反思时写，不在普通心跳时写 |

---

# 第八节 · 技术架构决策的实现细节

---

### T-X1 感知方式：Agent 自主调用 look

**决策：** 删除 SceneSaw，所有视觉感知由 agent 通过 look 工具主动发起。

**已实现。** 自激循环问题彻底消除。

**实现要点回顾：**
- 心跳系统定期给 agent 执行机会
- 强制观察计数器保证不会永远不看
- 亮度检测避免暗光下无意义调用
- look 的 tool description 覆盖所有视觉场景

---

### T-X2 循环退出：终止权归 LLM

**决策：** 唯一正常退出条件是 LLM 返回纯文本（无工具调用）。不按工具类型 break。

**已实现。** 解决了 speak("让我想想") 导致任务中断的问题。

**延伸影响：** 所有工具（包括 speak、express_emotion）的返回都是非终结的。LLM 可以 speak 之后继续 look，也可以 look 之后继续 speak。工具组合完全由 LLM 自主决定。

**需要注意：** 如果 LLM 进入了"一直调工具不停下来"的模式，max_steps=12 兜底。日志中观察到的"找人用了 7 步"是合理的，不需要降低 max_steps。

---

### T-X3 取消方式：协作式检查点

**决策：** 在 for 循环开头检查 should_abort 标志，不 cancel 任何 await。

**已实现。** 保证舵机动作完整执行、HTTP 连接不被丢弃。

**实现要点：**

```python
async def agent_run(self, messages, max_steps=12):
    for step in range(max_steps):
        # ---- 这里是唯一的取消检查点 ----
        # 上一步的 LLM 调用已完成，工具已执行完毕
        if self._should_abort:
            self._should_abort = False
            return None

        response = await llm.chat(messages)
        # ... 后续逻辑
```

**调用方在用户语音进入时设置标志：**

```python
async def on_heard_speech(self, text):
    if agent.is_running():
        agent._should_abort = True
        await agent.wait_until_done()  # 等当前步骤完成
    await agent.run(user_message=text)
```

---

### T-X4 工具抽象：look = 转头+看到

**决策：** 合并 body_move + take_photo 为 look 工具，符合认知常识。

**已实现。** LLM 自然理解"想知道什么就 look"，不需要在 prompt 里教它组合两个工具。

**保留 body_move 用于纯动作场景：** 点头、摇头、跳舞、情绪表达配合。这些场景不需要视觉反馈。

---

### T-X5 心跳节奏：三档固定

**决策：** ACTIVE/IDLE/DORMANT 三档固定间隔 + 交互重置，不用指数退避。

**已实现。** 行为可预测，好调试。

**P1 需要优化：心跳触发时的上下文组装（见 T-E1）。** 目前心跳只带 extra_hint，需要扩展为完整的上下文注入（时间、用户状态、说话预算、场景记忆、未完成任务）。

---

### T-X6 提示词策略：事实优先，规则兜底

**决策：** 改工具设计和返回值优于加 prompt 规则。prompt 只陈述世界设定，不写"禁止""必须"。

**已验证有效的例子：**
- look 的 tool description 覆盖"所有需要眼睛的事" → 不需要逐场景加示例
- body_move 返回不含 visual → LLM 自然知道转头后没看到东西

**什么时候才加强制规则：**
- 工具设计和返回值无法传达的信息（如"一天说话不超过 8 次"这种全局约束）
- LLM 反复犯同一错误且无法通过工具设计修正

---

### T-X7 运动进化：记忆是预期，观察是真理

**决策：** 运动能力不靠运动模型或强化学习，靠 ReAct 循环本身作为学习过程。每一次"动→看→记"都是运动经验的积累。

**核心原则：**
- 本体感知通过工具返回值实现（look/body_move 返回当前关节角度）
- 空间地图通过场景记忆实现（记录"某个角度看到了什么"）
- 运动校准通过 ReAct 循环实现（目标偏了→下一步修正→记住正确参数）
- 记忆是预期不是真理——根据记忆去 look，看到不一致就更新记忆

**已验证：** LLM 天然能理解"画面右边缘→该往右转一点"，不需要任何计算机视觉算法辅助。

**关键约束：凡是基于记忆的回答，必须先 look 确认再说。** 记忆帮你更快找到方向，但真话只能来自眼睛。

---

# 开发顺序建议

```
第一批（P1 核心，约 2 周）
├── T-A1 人脸识别         → 叫出名字，建立连接的第一步
├── T-A2 状态感知         → 为主动关心提供判断依据
├── T-H1 本体感知         → look/body_move 返回关节角度（三行代码，顺手做了）
├── T-E1 心跳上下文优化   → 注入时间、状态、预算信息
├── T-E2 主动关心策略     → 5-8 次精准开口
├── T-E3 说话预算         → 防止话痨
└── T-F1 场景记忆         → 不重复探索，同时承载空间地图（T-H2）

第二批（P1 体验，约 1 周）
├── T-B1 灯光情绪表达     → 第一层无声交互
└── T-W2 自我工具接入     → 场景记忆工具化

第三批（P2 成长，约 2 周）
├── T-F2 习惯学习         → 每日反思，认识用户
├── T-F3 关系记忆         → 互动模式追踪
├── T-H3 运动校准         → prompt 引导自我校准（不需要新代码，和 T-F2 反思一起做）
├── T-L1 关系成长感       → 阶段提示注入
├── T-L4 被用户影响       → 反思中追踪回应率
└── T-L5 关系有重量       → 分离检测+回归反应

第四批（P2 润色）
├── T-B2 呼吸灯           → 底层生命感
├── T-A3 环境感知         → 依赖场景记忆自然实现
└── T-L2 内心世界         → 场景记忆带情感标记
```
