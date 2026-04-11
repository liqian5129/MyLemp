# 小Q 记忆系统重构方案

## Context

lelamp 当前的"记忆"是一个事件流 logger 加了一层召回（`MemoryStream` + `SceneMemory` + `IdentityMemory`），存在五个互相纠缠的问题：

1. **结构错配** — 把"状态"（灯色、舵机位）、"事件"（用户说话）、"事实"（声纹身份）、"内省"（LLM 异常自述）混进同一个 `did/heard/said/felt/thought` 流，用同一个衰减公式处理，每种语义都失真。
2. **历史污染** — `~/.lelamp/memories.archive.json` 已经积累 2125 条，其中 905 条 `did` 是已删除代码（`express_emotion`/`set_rgb_solid`）写的噪音，365 条 `felt` 全是被移除的启动文案"刚刚启动，世界感觉是新鲜的"。
3. **fact 没有专属层** — `IdentityMemory` 是一个孤立的声纹库，从不进 prompt；偏好/承诺等结构化事实根本无处存放。LLM 每次都得从事件流里推理"用户是谁"。
4. **新鲜状态没有明确通道** — 当前时间、灯光、在场人员、距上次说话时长这些"现在世界什么样"的信号散在 `_event_source` / `_last_activity` / `_ticks_since_photo` / `motion_agent.get_status_str()` 等十几个 SoulAgent 实例字段里，每次 ReAct 临时拼接，遗漏率高。
5. **错位的硬规则** — `SpeechBudget` 把"每日 8 次主动说话上限"做成代码硬约束，把决策权从 LLM 抢回到代码里。这违背 agent 化设计原则——智能行为应由 LLM 结合上下文判断，规则只用于物理安全底线。

**目标**：把记忆系统重构为六层模型，每层用不同的更新模式、过期策略和注入方式，全部以"让 LLM 看到正确的上下文"为终点；删除一切非物理层的硬规则。

---

## 设计原则（贯穿所有 phase）

1. **LLM 是唯一决策者**。工程的工作是搭舞台（让上下文清晰、新鲜、结构化），不是替它判断。
2. **规则只在物理安全底线**。舵机角度/速度限制、TTS 通道独占、相机互斥——这些是物理事实。其他所有"行为合不合适"的判断都属于 LLM。
3. **记忆按"更新模式"分层**，不按"信息源"分层。append-only 的进事件流；last-write-wins 的进状态；有 schema 的结构化事实进 facts。
4. **新鲜度按声明类型而非统一 lambda**。状态声明硬过期、事件声明衰减相关性、事实声明永久或被反例覆盖。
5. **每条进 prompt 的内容都要能回答"它会改变本回合 LLM 的输出吗？"**。不能回答的不进 prompt。
6. **不复制状态，只暴露视图**。同一份状态出现在两个对象里就是异步 bug 温床——视图层只读 + read-through 到唯一所有者。

---

## 目标架构

### 六层记忆模型

| 层 | 时间尺度 | 形态 | 注入到 LLM 的方式 | 物理存储 |
|---|---|---|---|---|
| **Working** | 秒-分 | ReAct 循环 messages 列表 | LLM multi-turn context | 内存（已有，不动） |
| **State** | 秒-分 | 当前世界快照（dict） | 每次 ReAct 重渲染的 `[STATE]` 段 | 内存 view（read-through 到 SoulAgent） |
| **Today (derived)** | 当天 | 当天叙事滚动摘要 | `[TODAY]` 段，约 200 字 | 异步生成、内存缓存 |
| **Episodic** | 小时-天 | append-only 事件流 | 衰减召回的 `[RECENT]` 段 | 文件持久化 |
| **Semantic / Facts** | 天-永久 | 结构化字段 fact | always-on 的 `[FACTS]` 段 | JSON 文件 |
| **Long-term** | 周-永久 | 自由文本记忆条目（向量搜索） | `[LONGTERM]` 段（摘要）+ `recall_memory` / `session_search` 工具 | JSON + numpy embedding + SQLite |
| **Procedural** | 永久 | 嵌入 PERSONALITY_PROMPT 的角色描述 | system prompt（已有，不动） | 代码 |

> Today 是 derived view，不持久化也不重复存数据——它是 episodic 的每日叙事投影，填补 State（即时点）和 Episodic（散点事件）之间的语义鸿沟，让 LLM 知道"今天整体发生过什么"。
>
> Long-term 是 Phase 5 新增的永久记忆层，由 `LongTermMemory` 实现。写入路径有四条：主 LLM 调用 `save_memory` 工具、Background Review 后台提取、`extract_facts` 的候选 fact、episodic 压缩时的 `extract_longterm_memories`。读取通过 `recall_memory`（向量搜索 + 关键词 fallback）和 `session_search`（SQLite FTS5 历史搜索）两个工具暴露给 LLM。

### 注入通道

| 通道 | 内容 | 位置 | 触发 |
|---|---|---|---|
| **A 事件召回** | episodic 衰减召回 | initial user message 的 `[RECENT]` 段 | 每次 ReAct |
| **C 状态注入** | STATE + FACTS + LONGTERM + TODAY | initial user message 的 `[STATE]` + `[FACTS]` + `[LONGTERM]` + `[TODAY]` 段 | 每次 ReAct |
| **D 目标生成** | 到期的 reminder | ReminderService 定时器 fire → trigger 注入 | 精确时间唤醒 + 心跳兜底 |
| **E 工具召回** | LLM 主动搜索 | `recall_memory` / `session_search` 工具返回值 | LLM 决策 |

> 通道 B（"硬约束过滤工具调用"）已被否决——见设计原则 #1 #2。
> 通道 D 已从 FactStore commitment 迁移到独立的 ReminderService（`lelamp/soul/reminder.py`），天然实现精确时间唤醒 + 跨重启恢复。

### 当前目录结构（Phase 5 完成后）

```
lelamp/soul/
├── soul_agent.py          ← 主体（ReAct 循环、工具注册、后台任务调度）
├── reminder.py            ← 定时提醒工具层（独立模块 + 独立存储）
├── audio_event.py
├── camera_capture.py
├── omni_ear.py
└── memory/
    ├── __init__.py        ← re-export 所有公共符号
    ├── episodic.py        ← heard/said/thought/action 事件流 + SQLite 双写 + timestamp 参数
    ├── scene.py           ← 场景记忆（纯文件读写）
    ├── identity.py        ← 声纹 + 人脸 embedding 库
    ├── state.py           ← WorldState 视图层（@property read-through）
    ├── render.py          ← render_context_packet（STATE/FACTS/LONGTERM/TODAY/SCENE/RECENT/TRIGGER）
    ├── facts.py           ← FactStore（identity/calling/preference/daily_reflection）
    ├── consolidate.py     ← extract_facts + today_narrative + PendingFactBuffer
    ├── longterm.py        ← LongTermMemory（向量搜索 + 关键词 fallback）
    └── history_db.py      ← SQLite + FTS5 全量会话历史（替代 archive JSON 的搜索功能）
```

`lelamp/soul/speech_budget.py` 已删除（Phase 0）。

---

## 阶段执行计划

### Phase 0：止血 + 数据清洗

**目标**：源头停止污染，归档数据彻底重置，删除硬规则模块（但保留监控）。

**改动列表**：

1. **备份 + 清空 archive**
   - 一次性脚本（plan 通过后可以是 shell 命令）：
     - `cp ~/.lelamp/memories.archive.json ~/.lelamp/memories.archive.json.bak`
     - `echo '[]' > ~/.lelamp/memories.archive.json`
   - **`.bak` 永久保留作为冷归档**——不再扫描、不抽取、不进 prompt。新数据从这一刻开始干净增量。
   - 不需要 migration 代码（用户明确选择"导出后清空"）。

2. **删除 `set_rgb_solid` 的 did 写入**
   - `lelamp/soul/soul_agent.py:926` 的 `self._mem.add("did", f"灯光 → RGB({r},{g},{b})")` 删除
   - 与 `set_light_mood`（lines 968-977）行为对齐，瞬态控制不写记忆

3. **删除 `SpeechBudget` 模块 + 添加 inline 监控（不拦截）**
   - 删除文件 `lelamp/soul/speech_budget.py`
   - 删除 `soul_agent.py:461` 的 `self._speech_budget = SpeechBudget()` 实例化
   - 删除 `soul_agent.py:642-644` 的 `budget_ctx` 注入
   - 删除 `soul_agent.py:906-907` 和 `916-917` 的 `self._speech_budget.record()` 调用
   - 删除 `soul_agent.py` 顶部的 `from .speech_budget import SpeechBudget` import

   **新增 SoulAgent 监控字段（不拦截，仅观察）**：
   ```python
   # __init__
   self._self_speech_count_today: int = 0
   self._self_speech_count_date: date = date.today()
   self._last_self_speech_at: Optional[float] = None

   def _record_proactive_speech(self) -> None:
       """speak/ask 工具被 heartbeat/environment 触发时调用。

       只记录、只观察，不阻止任何行为。
       Phase 1 之后这些字段由 WorldState 通过 view 暴露给 prompt，
       LLM 看到数字自己判断是否要继续说。
       """
       today = date.today()
       if today != self._self_speech_count_date:
           self._self_speech_count_date = today
           self._self_speech_count_today = 0
       self._self_speech_count_today += 1
       self._last_self_speech_at = time.time()
       if self._self_speech_count_today >= 10:
           logger.warning(
               "🗣️ 主动说话次数偏高: %d/天 - 观察 LLM 是否会自我收敛",
               self._self_speech_count_today,
           )
   ```
   - speak/ask 工具中：当 `self._event_source in ("heartbeat", "environment")` 时调用 `_record_proactive_speech()`
   - **关键变化 vs 原方案**：监控 ≠ 拦截。warning 是给人看的（观察 LLM 能否自我收敛），不影响任何代码路径
   - 这些字段在 Phase 1 由 `WorldState` 通过 `@property` 视图暴露给 `[STATE]` 段

4. **保留**：
   - `soul_agent.py:811` 的 `felt "脑袋里一片空白"` —— 只在 LLM 异常时触发，是诊断信号，保留到 Phase 3
   - `soul_agent.py:986` 的 `did "记住了 X 的声音"` —— 是有意义的身份事件（Phase 2 之后会迁移到 facts.py，本阶段不动）

**验证**：
- 启动 lelamp，让它响应几次用户说话和心跳事件
- 检查 `~/.lelamp/memories.json` 不再出现 RGB 条目
- 检查心跳触发的 prompt 里不再含"今天你已经主动说了 X 次"硬文案
- 触发 ≥10 次主动说话，确认日志出现 `🗣️ 主动说话次数偏高` 但行为不被拦截
- 检查 archive 文件停留在空数组状态

**预计文件变更**：
- `lelamp/soul/soul_agent.py`（多处删除 + 监控字段 + helper 方法）
- `lelamp/soul/speech_budget.py`（删除）

---

### Phase 1：引入 memory/ 目录骨架 + State 通道（视图模式）

**目标**：把现有三个模块迁入 `memory/` 子目录；新建 `state.py`（视图层）与 `render.py`；让 ReAct 的 prompt 包含 `[STATE]` 段。

**改动列表**：

1. **建立 memory/ 子目录**
   - 新建 `lelamp/soul/memory/__init__.py`
   - 文件搬迁（git mv 保留历史）：
     - `lelamp/soul/memory_stream.py` → `lelamp/soul/memory/episodic.py`
     - `lelamp/soul/scene_memory.py` → `lelamp/soul/memory/scene.py`
     - `lelamp/soul/identity_memory.py` → `lelamp/soul/memory/identity.py`
   - **本阶段只搬不改**——保持类名、接口不变，只更新 import 路径
   - 在 `lelamp/soul/memory/__init__.py` 重新导出原符号
   - 全仓库其他 import 这些模块的位置一并修改

2. **新建 `lelamp/soul/memory/state.py`（视图模式）**

   核心类：
   ```python
   class WorldState:
       """SoulAgent 的只读视图层 —— 不拥有任何状态。

       所有字段都是 @property，read-through 到 SoulAgent 的实例字段。
       这避免双份状态在异步系统里因为 sync 不及时而打架。
       """

       def __init__(self, agent: 'SoulAgent'):
           self._agent = agent

       # ── 时间 ──
       @property
       def now(self) -> datetime:
           return datetime.now()

       @property
       def time_phase(self) -> str:
           return _phase_of(self.now.hour)  # "凌晨"/"上午"/...

       # ── 在场感知 ──
       @property
       def last_user_speech_at(self) -> Optional[float]:
           return self._agent._last_activity

       @property
       def last_user_speaker(self) -> Optional[str]:
           return self._agent._last_recognized_speaker

       @property
       def last_user_emotion(self) -> Optional[str]:
           return self._agent._last_user_emotion

       @property
       def last_user_activity(self) -> Optional[str]:
           return self._agent._last_user_activity

       # ── 自我状态 ──
       @property
       def last_self_speech_at(self) -> Optional[float]:
           return self._agent._last_self_speech_at

       @property
       def self_speech_count_today(self) -> int:
           return self._agent._self_speech_count_today  # 跨日 rollover 由 SoulAgent 负责

       @property
       def body_status(self) -> str:
           return self._agent.motion_agent.get_status_str()

       # ── 视觉 / 环境 ──
       @property
       def ticks_since_photo(self) -> int:
           return self._agent._ticks_since_photo

       @property
       def last_audio_env(self) -> Optional[str]:
           return self._agent._last_env_audio

       @property
       def last_env_event_at(self) -> Optional[float]:
           return self._agent._last_env_event_at

       @property
       def current_light_mood(self) -> Optional[str]:
           return self._agent._current_light_mood

       # ── 触发上下文 ──
       @property
       def current_trigger(self) -> str:
           return self._agent._event_source or "user"

       def snapshot(self) -> dict:
           """渲染前调用，返回扁平字段字典（None 字段会在 render 时省略，超长字段会被截断）。"""
           ...

   # ── 字段硬长度上限（防膨胀，不做运行时优先级裁剪）──
   _FIELD_MAX_LEN = {
       "body_status": 80,
       "last_audio_env": 40,
       "last_user_activity": 40,
       "last_user_emotion": 20,
       "last_user_speaker": 20,
       "current_light_mood": 20,
   }

   def _truncate(value: str, max_len: int) -> str:
       if value is None or len(value) <= max_len:
           return value
       return value[: max_len - 1] + "…"
   ```
   - **关键变化 vs 原方案**：view 模式而不是双份字段
   - SoulAgent 的实例字段保持不动（它们是事实的唯一所有者）
   - WorldState 是它们的对外观察接口
   - `time_phase` 的映射函数从 `soul_agent.py:615-630` 抽出来，移到 `state.py` 的模块级 `_phase_of()`
   - 跨日 rollover 已在 Phase 0 的 `_record_proactive_speech` 内部处理
   - **不引入硬约束方法** —— `WorldState` 只是数据袋
   - **字段硬长度上限**：`snapshot()` 内部对每个字段按 `_FIELD_MAX_LEN` 截断；不写运行时优先级裁剪逻辑（避免 debug 时猜"为什么这次没显示某字段"）

3. **新建 `lelamp/soul/memory/render.py`**

   核心函数：
   ```python
   def render_context_packet(
       state: WorldState,
       episodic: 'MemoryStream',
       scene: 'SceneMemory',
       facts: Optional['FactStore'],      # Phase 2 注入
       today: Optional[str],               # Phase 3 注入
       longterm: Optional['LongTermMemory'],  # Phase 5 注入
       trigger: str,
   ) -> str:
       """构造 ReAct initial user message 的正文。

       结构（按顺序）：
         [STATE]    当前世界快照（state.snapshot()）
         [FACTS]    facts.format()
         [LONGTERM] longterm.format_summary()  ← Phase 5 新增
         [TODAY]    today
         [SCENE]    scene.read()
         [RECENT]   episodic.format_for_prompt()
         [TRIGGER]  本次触发原因
       """
   ```

   - 段之间用空行 + 明显的 ALL-CAPS 段头分隔
   - 字段为空时省略对应行（不要打印 "None"）
   - `[STATE]` 段示例：
     ```
     [STATE]
     时间: 2026-04-08 22:30 (深夜)
     身体: base_yaw=15, base_pitch=-5, ...
     在场: 主人 Q (8 秒前说话, 平静)
     灯光: warm_focus
     上次主动说话: 4 分钟前 (今天累计 3 次)
     视觉: 已 4 次心跳没看了
     上次环境事件: 30 秒前 (有人走动)
     ```
   - 这个函数取代当前 `_think()` 中 lines 718-732 的 `initial_text` 拼接逻辑

4. **修改 `SoulAgent`**

   - `__init__`（line 442 起）新建 `self._state = WorldState(self)`（注意传入 self）
   - `_process_event`（lines 610-697）：
     - **删除** `_process_event` 内部的 trigger_parts 拼接逻辑
     - 保留 trigger 的"来由"信息作为一行 `[TRIGGER]` 文本，不再混杂时间提示和决策引导
     - SoulAgent 的内部字段照常更新（`_event_source` / `_last_activity` / `_ticks_since_photo` 等）；WorldState 因为是视图层会自动看到最新值
   - `_think()`（lines 711-749）：
     ```python
     async def _think(self, trigger: str, image_path=None, max_steps=15):
         packet = render_context_packet(
             state=self._state,
             episodic=self._mem,
             scene=self._scene_memory,
             facts=getattr(self, "_facts", None),    # Phase 2 起非空
             today=getattr(self, "_today_narrative", None),  # Phase 3 起非空
             trigger=trigger,
         )
         messages = [{"role": "system", "content": PERSONALITY_PROMPT}]
         # ... initial user message 用 packet 替代 initial_text
     ```

5. **散落 state 字段的归并（视图方式，不复制）**

   不动 SoulAgent 的字段定义；只在 WorldState 上对应增加 `@property`：

   | SoulAgent 字段（不动） | WorldState 视图属性 |
   |---|---|
   | `_event_source` | `current_trigger` |
   | `_last_activity` | `last_user_speech_at` |
   | `_ticks_since_photo` | `ticks_since_photo` |
   | `motion_agent.get_status_str()` | `body_status` |
   | `_last_recognized_speaker` | `last_user_speaker` |
   | `_self_speech_count_today` | `self_speech_count_today` |
   | `_last_self_speech_at` | `last_self_speech_at` |
   | `_last_env_audio` | `last_audio_env` |
   | `_last_env_event_at` | `last_env_event_at` |

   *注：`_speech_pending` / `_light_task` / `_compacting` 等异步原语不出现在视图里——它们不是世界状态。*

**验证**：
- 启动后用 user/heartbeat/environment 三种事件触发 ReAct
- 在 `_think` 入口加临时 `print(packet)`，肉眼检查 `[STATE]` 段格式正确、字段齐全
- 检查现有行为没有回归（机器人能正常应答、能调用工具、能压缩记忆）
- 删除临时 print

**预计文件变更**：
- 新建 `lelamp/soul/memory/__init__.py`、`memory/state.py`、`memory/render.py`
- 移动 `memory_stream.py` / `scene_memory.py` / `identity_memory.py` 到 `memory/` 下
- `lelamp/soul/soul_agent.py`（imports + `__init__` + `_process_event` + `_think`）
- 全仓库 import 路径修复

---

### Phase 2：FactStore 与 [FACTS] 段 + commitment v0

> **后续变更**：Commitment 机制已在 Reminder 迁移阶段从 FactStore 迁出到独立的 `ReminderService`（`lelamp/soul/reminder.py`）。FactStore 不再管理 commitment，只负责 identity/calling/preference/daily_reflection 四种 fact。下文 commitment 相关描述保留作为历史记录。

**目标**：把"用户偏好/称谓/承诺/已知身份"等结构化事实从事件流和 identity 库里独立出来，作为 always-on 的 `[FACTS]` 段进入 prompt；引入承诺机制的最小可用版本。

**改动列表**：

1. **新建 `lelamp/soul/memory/facts.py`**

   ```python
   @dataclass
   class Fact:
       """普通 fact：identity / preference / calling。

       last-write-wins 语义：同 (kind, key) 的新写入直接覆盖旧值。
       历史变化由 episodic 流自然记录，facts 只回答"此刻是什么"。
       """
       id: str
       kind: str               # "identity" / "preference" / "calling"
       key: str
       value: str
       confidence: float = 1.0
       first_seen: float = ...
       last_confirmed: float = ...
       source_event_ids: list[str] = ...   # 链回 episodic（Phase 3 用于独立 session 判定）

   @dataclass
   class Commitment:
       """承诺：独立的状态机，不走 fact 的 upsert 语义。

       生命周期：pending → fired / cancelled / expired
       """
       id: str
       text: str
       due_at: float
       status: str = "pending"     # "pending" / "fired" / "cancelled" / "expired"
       created_at: float = ...
       fired_at: Optional[float] = None

   class FactStore:
       """L3 结构化事实。JSON 文件持久化。

       两套独立 API：
         - 普通 fact: upsert / forget / list_by_kind  (last-write-wins)
         - commitment: add_commitment / mark_fired / cancel  (状态机)

       upsert() 显式拒绝 kind="commitment"，避免语义混淆。

       总条目数预期 < 100，全量进 prompt。
       """
       # ── 普通 fact ──
       def upsert(self, kind, key, value, confidence=1.0, source_event_ids=None):
           if kind == "commitment":
               raise ValueError("commitment 必须用 add_commitment()")
           ...
       def forget(self, kind, key): ...
       def list_by_kind(self, kind): ...
       def all_active_facts(self) -> list[Fact]: ...

       # ── commitment 状态机 ──
       def add_commitment(self, text: str, due_at: float) -> str: ...
       def mark_commitment_fired(self, commitment_id: str): ...
       def cancel_commitment(self, commitment_id: str): ...
       def due_commitments(self, now: float) -> list[Commitment]:   # status=pending and due_at<=now
           ...
       def all_active_commitments(self) -> list[Commitment]:        # status=pending
           ...
   ```

   - 持久化：`~/.lelamp/facts.json`（同时存 facts 和 commitments，但顶层结构区分两个数组）
   - 写盘策略：写入临时文件 `facts.json.tmp` → atomic rename → `.bak` 由 OS rename 隐式保护
   - **`.bak` 不是产品层历史链**，只是写盘事务保护（防进程崩在 json.dumps 中间）
   - 不依赖任何 LLM——纯 CRUD

2. **identity 升级**
   - 保持 `lelamp/soul/memory/identity.py` 作为声纹/人脸 embedding 的二进制存储不变
   - 在 SoulAgent 中：每次 `register_voice` 后**同步写一条 fact**：
     ```python
     self._facts.upsert(
         kind="identity",
         key=f"voice:{person_name}",
         value=f"{person_name}（声纹注册，{count} 条样本）",
     )
     ```

3. **render.py 增加 [FACTS] 段**
   - 在 `[STATE]` 之后、`[TODAY]`/`[SCENE]` 之前插入
   - 按 kind 分组排序：identity → calling → preference → commitment
   - 空分组省略

4. **新增 LLM 工具**
   - `update_fact(kind, key, value)` —— 偏好/称谓（kind ∈ identity/preference/calling）
   - `forget_fact(kind, key)` —— 主动删除普通 fact
   - `make_commitment(text: str, due_at: str) -> str` —— 承诺 v0
     - 实现：`self._facts.add_commitment(text=text, due_at=parse_iso(due_at).timestamp())`
     - LLM 在用户提出"明天 8 点叫我"时调用
   - `cancel_commitment(commitment_id: str)` —— 用户改主意时

5. **TimerTick 路径加 due 检查**
   - `_process_event(TimerTick)` 入口：
     ```python
     due = self._facts.due_commitments(now=time.time())
     if due:
         trigger_lines = [f"承诺到期: {c.text}" for c in due]
         trigger = "\n".join(trigger_lines) + "\n（心跳触发）"
         for c in due:
             self._facts.mark_commitment_fired(c.id)
         await self._think(trigger=trigger)
         return
     ```
   - **关键设计**：mark_commitment_fired 在 `_think` 之前调用，避免重复触发
   - 失败回退：若 `_think` 抛异常，commitment 已 fired——这是有意的"宁可漏一次也不刷屏"

6. **PERSONALITY_PROMPT 在 `<proactive_care>` 内部加 "before relying on memory" 段**
   - 一两句话："`[STATE]` 段是即时观察；`[FACTS]` 是稳定事实；`[TODAY]` 是当天发生的事；`[RECENT]` 是过去事件，可能已变化。需要确认当前世界状态时优先调用 `look`。"
   - **关键变化 vs 原方案**：放在 `<proactive_care>` 而不是 prompt 末尾——让自验证成为主动关心流程的一部分

7. **PERSONALITY_PROMPT 末尾加新工具说明**
   - `update_fact` / `forget_fact` / `make_commitment` 的使用时机和示例

**验证**：
- 让用户对小Q 说"以后叫我老板"，确认 facts.json 出现新条目
- 重启后 facts 仍然加载，且 [FACTS] 段在 prompt 中可见
- 让用户改回原称谓，确认旧 fact 被覆盖
- 让用户说"30 秒后叫我"，LLM 调 make_commitment(due_at="2026-04-08T22:31:00")
- 30 秒后心跳触发，trigger 含"承诺到期"，小Q 主动说话
- 确认 fact.fired 被置 True，不再重复触发

**预计文件变更**：
- 新建 `lelamp/soul/memory/facts.py`
- `lelamp/soul/memory/render.py`（增加 facts section）
- `lelamp/soul/soul_agent.py`（实例化 FactStore、注册新工具、TimerTick due 扫描、PERSONALITY_PROMPT 微调）

---

### Phase 3：Episodic 简化 + 抽取通道 + Today 叙事

**目标**：把事件流瘦身到只剩 `heard/said`；引入"事件 → fact"的抽取 promoter（独立 session 判定）；新增 today_narrative 滚动叙事。

**改动列表**：

1. **`memory/episodic.py` 收紧 schema**
   - 在 `MemoryStream.add()` 顶部加白名单校验：
     ```python
     ALLOWED_TYPES = {"heard", "said"}
     def add(self, type_, content, importance=None):
         if type_ not in ALLOWED_TYPES:
             raise ValueError(f"episodic 不接受类型 {type_}")
         ...
     ```
   - **删除** `_score_importance` 中的 `did/saw/felt/thought` 分支
   - 删除 `soul_agent.py:811` 的 `felt` 写入（异常诊断改用 `logger.warning`，不入记忆）
   - 删除 `soul_agent.py:986` 的 `did` 写入（已被 Phase 2 的 fact 取代）
   - `format_for_prompt()` 输出按时间倒序的 `[HEA]/[SAI]` 事件流，加入"距今 X 分钟前"的相对时间标签

2. **新建 `memory/consolidate.py`**

   ```python
   async def extract_facts(
       llm,
       events: list[MemoryEntry],
       existing_facts: FactStore,
   ) -> list[FactCandidate]:
       """从一批事件中抽取候选 fact。

       LLM 被要求只返回'对用户/世界长期成立'的陈述，
       并在 prompt 中显式列出 existing_facts 避免重复。
       """

   async def compress_episodic(
       llm,
       events: list[MemoryEntry],
   ) -> str:
       """把一批低信息事件合并为一句叙事摘要。

       与 extract_facts 是不同动作：
         - extract_facts 产出 facts.json 的更新
         - compress_episodic 产出留在 episodic 流的 thought 条目（叙事用）
       """

   async def today_narrative(
       llm,
       episodic: MemoryStream,
       prev_summary: Optional[str],
   ) -> str:
       """生成/更新当天滚动叙事，约 200 字。

       - 输入：从凌晨开始的所有 heard/said 事件 + prev_summary
       - 输出：紧凑的当天叙事，给 [TODAY] 段用
       - 触发策略见下文（在 TimerTick 入口 check，不在 add() 里 check）
       - 跨日：每天 00:00 把昨天的 narrative 转存为 daily_reflection fact，
              然后清空 prev_summary
       """
   ```

   - 抽取 prompt 必须包含约束："只在你 80% 以上确信这是关于用户/世界的稳定事实时才返回；玩笑、临时状态、不确定的内容不返回"
   - **`extract_facts` LLM 调用**：用同一个 `_llm`，timeout 设为 60s（异步路径，不在用户响应热路径上；10s 是错把主循环延迟预算迁移过来）

3. **Fact 双次确认 = "独立 session" 判定（不是计数也不是时间窗）**
   - 候选 fact 进入"待确认"状态后，需要由 *第二个独立 ReAct session* 再次抽取出本质上等价的 fact 才 promote 到 FactStore
   - "独立 session" 的判断：候选 fact 的 `source_event_ids` 与已有候选的 `source_event_ids` **没有重叠**
   - 这避免了"等次数"或"等 24 小时"两种方法的双重单点故障——它们本质都是让同一个 LLM 在同一上下文里反复确认自己
   - 实现位置：`consolidate.py` 内部，作为 `extract_facts` 调用方的判断逻辑

4. **改进压缩 prompt**
   - 字数 80 → 150
   - 把事件类型翻译成自然语言（heard → "用户说"，said → "我说"）再喂给 LLM
   - thought 条目的 importance 从 6 → 5（让 heard/said 优先）

5. **触发时机**

   | 任务 | 触发位置 | 触发条件 |
   |---|---|---|
   | `extract_facts` | ReAct 结束后异步 task | 距上次 ≥10 条新 heard/said OR 距上次 ≥1 小时 |
   | `today_narrative` | **`_process_event(TimerTick)` 入口** | 新 heard/said ≥5 **AND** 距上次 ≥10 分钟 |
   | `compress_episodic` | 沿用 `compact_if_needed` | 活跃条目 > 40 |

   **`today_narrative` 触发要点**：
   - **不要在 `episodic.add()` 里 check** —— 会污染语音响应热路径
   - **不要写"无更新时也触发"** —— 没有新事件 = 没有新信息 = 现有 narrative 仍然有效，重算只是浪费 LLM 调用
   - 冷启动：第一条 heard/said 进来后立即触发一次（让"今天刚开始有点动静"成为可用的 [TODAY] 段）
   - **跨日 hook**：每天 00:00（或检测到事件跨入新一天时）把当前 `today_narrative` 写入一条 `kind="daily_reflection"` 的普通 fact，然后清空内存中的 narrative。这条 daily_reflection fact 进入 [FACTS] 段，自然成为长期"昨天发生了什么"记忆——同时对接 dev_process.md P2 的"日反思"任务

   **today_narrative vs compress_episodic 是两个独立任务**，互不驱动：

   |  | today_narrative | compress_episodic |
   |---|---|---|
   | 目的 | 给 LLM 提供今日叙事上下文 | 压缩存储，控制 episodic 条目数 |
   | 产物 | prompt 里的 [TODAY] 段（内存） | 归档到 memories.archive.json |
   | 更新频率 | 新事件累积到阈值 | 活跃条目 > 40 时 |
   | 是否覆盖之前输出 | 是（每次重写今日摘要） | 否（追加一条 thought） |

**验证**：
- 让用户在两个不同 session（中间隔一段时间或重启）各说一次"我喜欢猫"
- 确认抽取通道把 `preference: likes_cats` 写进 facts.json
- 让用户在同一对话里连续说"我喜欢猫" + "对，猫真的很可爱"
- 确认这条 *不* 进 facts.json（因为 source_event_ids 重叠 = 同一 session）
- 让用户单次说"我想当宇航员"（玩笑级别）
- 确认这条 *不* 进 facts.json（被 80% 置信度拒绝）
- 触发若干 heard/said 后，确认 `_today_narrative` 字段非空且 ≤ 200 字
- 确认 episodic.json 不再有 `did/saw/felt/thought` 类型条目

**预计文件变更**：
- `lelamp/soul/memory/episodic.py`（schema 收紧、类型白名单）
- 新建 `lelamp/soul/memory/consolidate.py`
- `lelamp/soul/soul_agent.py`（删除 `felt` 写入、删除 `did` 写入、注册抽取与 today 异步任务）

---

### Phase 4：Commitment 精度 + 跨重启恢复 → ✅ 已由 ReminderService 解决

**原目标**：把 commitment 从"心跳粒度触发"提升到"精确时间唤醒 + 跨重启自愈"。

**实际方案**：Commitment 机制整体迁移到独立的 `ReminderService`（`lelamp/soul/reminder.py`），天然实现了三个目标：

1. **精确时间唤醒** — `asyncio.create_task(_sleep_until_and_fire())` 为每个 reminder 创建独立唤醒任务
2. **跨重启恢复** — `~/.lelamp/reminders.json` 持久化；启动时扫描 pending reminders 并重建唤醒 task
3. **Missed reminders 策略** — 过期未触发的 reminder 直接丢弃（闹钟睡过头就过了），不做补偿触发

LLM 通过 `set_reminder` / `cancel_reminder` 工具操作，替代了原 `make_commitment` / `cancel_commitment`。

---

### Phase 5：Background Review + HistoryDB + LongTermMemory

**目标**：解决"主 LLM 从不主动保存长期记忆"的问题；引入会话历史搜索能力。

**背景**：LongTermMemory 上线后实测发现主 LLM **从未调用 save_memory**。原因：
1. 冷启动 bug（已修复）：0 条记忆时 `[LONGTERM]` 段不渲染
2. 主 LLM 无暇管记忆：每次 `_think()` 是独立的单次 API 调用，LLM 忙着回应对话

借鉴 Hermes agent 的闭环学习系统，两条路径同时解决：

#### 5.1 Background Review（安全网）

独立 Review LLM 后台审查对话，主动提取记忆。

**交叉 provider 策略**（避免 rate limit 竞争）：

| 主 LLM | Review LLM |
|--------|-----------|
| qwen3.6-plus (DashScope) | kimi-k2.5 (Moonshot) |
| kimi-k2.5 (Moonshot) | qwen3.6-plus (DashScope) |

`_maybe_background_review()` 方法：
- **触发条件**：有效互动 ≥3 次 且 距上次审查 ≥5 分钟
- **流程**：收集 heard/said 事件 → 格式化对话 → Review LLM 单次调用 → 解析 JSON → 写入 LongTermMemory/FactStore
- **安全约束**：提示词明确"只保存用户明确表达的信息，不要从单次行为推断偏好"
- **超时**：30 秒

**配置**（`main_dual.py`）：
```python
# 默认交叉 provider：主 qwen → review kimi，主 kimi → review qwen
# 可通过 REVIEW_LLM_API_KEY + REVIEW_LLM_MODEL 显式指定
```

#### 5.2 LongTermMemory（向量搜索）

`lelamp/soul/memory/longterm.py` — 自由文本记忆的永久存储：

- **存储**：JSON 条目 + numpy embedding 矩阵
- **搜索**：余弦相似度向量搜索 + 关键词 fallback（无 embedding 时）
- **分类**：每条记忆有 category（preference/identity/relationship/habit/event/other）
- **去重**：保存时检查相似度 > 0.9 的已有记忆，更新而非新建

**写入路径**：

| 来源 | 目标 | 触发 |
|------|------|------|
| 主 LLM（_think 中） | LongTermMemory / FactStore | save_memory / update_fact 工具调用 |
| Background Review | LongTermMemory / FactStore | 有效互动 ≥3 且 ≥5 分钟间隔 |
| extract_facts（consolidate.py） | FactStore（经 PendingFactBuffer 二次确认） | 新事件 ≥10 或 ≥1 小时 |
| extract_longterm_memories（压缩时） | LongTermMemory | episodic 压缩触发（>40 条） |

#### 5.3 SQLite 会话历史（HistoryDB）

`lelamp/soul/memory/history_db.py` — 替代 archive JSON（写了不读的死数据）：

- SQLite + FTS5（trigram 分词，支持中文）
- 短于 3 字符的查询用 LIKE 兜底
- 搜索结果按时间间隔分组（>30min = 不同对话段）
- WAL 模式 + synchronous=NORMAL
- episodic.py 双写：`add()` 时实时写 + 归档时批量写

**新增 `session_search` 工具**：LLM 可搜索过去的对话历史。

#### 5.4 工具描述改进

- `recall_memory`：改为主动引导"搜比猜好"
- `save_memory`：改为主动引导"看到就存，不要犹豫"
- PERSONALITY_PROMPT `<longterm_memory>` 段："主动使用它——这是你的超能力"

#### 5.5 时间戳修复（_pending_heard 顺序颠倒）

**Root cause**：`_pending_heard` 延迟写入导致 `heard` 条目的时间戳晚于 `said` 条目。`format_for_prompt()` 按时间排序后 [RECENT] 显示为"小Q 先说、用户后问"，心跳时 LLM 看到"未回答的新问题"导致重复回答。

**修复**：
- `episodic.add()` 新增 `timestamp` 可选参数
- `_pending_heard` 从 2-tuple 改为 3-tuple `(text, importance, timestamp)`
- 延迟写入时传入事件到达时的原始时间戳

#### 5.6 删除 `_consolidate_lock`

三个后台任务（today_narrative、extract_facts、compact）操作不同数据，不需要互斥锁。锁的存在导致 60s LLM 调用期间其他任务被跳过。

**文件改动清单**：

| 文件 | 改动 |
|------|------|
| `lelamp/soul/soul_agent.py` | 删锁 + Background Review + 工具描述 + PERSONALITY_PROMPT + session_search 工具 + 时间戳修复 |
| `main_dual.py` | 创建交叉 provider review_llm + HistoryDB + 传参 |
| `lelamp/soul/memory/history_db.py` | **新建** — SQLite + FTS5 |
| `lelamp/soul/memory/longterm.py` | **新建** — 向量搜索 + 关键词 fallback |
| `lelamp/soul/memory/episodic.py` | 双写 SQLite + `add()` 支持 timestamp 参数 |
| `lelamp/soul/memory/__init__.py` | 导出 HistoryDB, LongTermMemory |
| `tests/test_background_review.py` | **新建** — 7 个 async 测试用例 |
| `tests/test_heard_timestamp.py` | **新建** — 时间戳修复验证 |

---

## 明确不做的事

| 不做 | 原因 |
|---|---|
| `behavior_constraints.py`（规则化偏好执行） | 违反"LLM 是唯一决策者"原则；改用通道 C 把数据做厚 |
| ~~向量数据库 / embedding 召回~~ | ~~已在 Phase 5 实现~~：LongTermMemory 用 numpy embedding + 余弦相似度，轻量够用 |
| LLM-driven sideQuery 召回（Claude Code 那种 router） | 机器人 ReAct 对延迟极敏感，加一次 LLM 调用代价太大 |
| Fork 子代理跑抽取（Claude Code 模式） | lelamp 单进程，async task 足够 |
| 灰度门控 / feature flag | 单用户系统，没有灰度需求 |
| 路径校验纵深防御（PSR M22186 那套） | 没有恶意第三方代码访问内存目录 |
| 删除 `_speech_pending` / `_last_env_*` 等协调字段 | 它们是异步原语不是状态 |
| 重写 PERSONALITY_PROMPT | 只做局部微调（`<proactive_care>` 段 + `<longterm_memory>` 段），不动整体角色定义 |
| **每日主动说话上限硬限制** | 这是人设决策，应该靠 prompt + STATE 数据驱动 LLM 自己收敛；硬上限会让小Q 显得机械 |
| **从 archive .bak 抽取历史 fact** | 历史归档信噪比过低（90%+ 是已删代码的噪音）；冷藏即可，新数据从 Phase 0 起干净增量 |
| **WorldState 拥有自己的字段** | 双份状态在异步系统里是 bug 温床；视图层只读 + read-through |
| **`[STATE]` 段运行时优先级裁剪** | STATE 总量 200-300 token 永远不是预算瓶颈；改字段级硬长度上限 + 截断；运行时省略会让 debug 时猜"为什么这次没显示某字段" |
| **Missed reminders 补偿触发** | 闹钟睡过头就过了；补偿触发会让用户困惑"刚启动就提醒我昨天的事" |

---

## 执行顺序与状态

| 优先级 | Phase | 状态 | 解决的痛点 |
|---|---|---|---|
| **P0** | Phase 0 | ✅ 完成 | 历史污染、噪音源、错位硬规则、缺少观察 |
| **P0** | Phase 1 | ✅ 完成 | 状态散乱、prompt 不清晰、模块组织混乱 |
| **P1** | Phase 2 | ✅ 完成 | facts 没有专属层、identity 不进 prompt |
| **P1** | Phase 3 | ✅ 完成 | 类型混杂、压缩质量差、缺少抽取与当天叙事 |
| **P1** | Reminder 迁移 | ✅ 完成 | commitment 从 FactStore 迁出为独立 ReminderService |
| **P2** | Phase 4 | ✅ 已解决 | 精确时间唤醒、跨重启恢复 → ReminderService 天然实现 |
| **P2** | Phase 5 | ✅ 完成 | 长期记忆不保存、缺少会话历史搜索、heard 时间戳颠倒 |

实际执行顺序：Phase 0 → Phase 1 → Phase 2 → Phase 3 → Phase 3 修复 → Reminder 迁移 → Phase 5。

---

## 关键文件速查

### 核心文件（Phase 5 完成后）

| 文件 | 职责 |
|---|---|
| `lelamp/soul/soul_agent.py` | 主体：ReAct 循环、工具注册、后台任务（today_narrative/extract_facts/compact/background_review） |
| `lelamp/soul/reminder.py` | 定时提醒：set_reminder/cancel_reminder 工具、精确时间唤醒、跨重启恢复 |
| `lelamp/soul/memory/episodic.py` | 事件流：heard/said/thought/action + 衰减召回 + SQLite 双写 + timestamp 参数 |
| `lelamp/soul/memory/state.py` | WorldState 视图层：@property read-through 到 SoulAgent |
| `lelamp/soul/memory/render.py` | render_context_packet：STATE/FACTS/LONGTERM/TODAY/SCENE/RECENT/TRIGGER |
| `lelamp/soul/memory/facts.py` | FactStore：identity/calling/preference/daily_reflection（纯 CRUD） |
| `lelamp/soul/memory/consolidate.py` | extract_facts + today_narrative + PendingFactBuffer + extract_longterm_memories |
| `lelamp/soul/memory/longterm.py` | LongTermMemory：向量搜索 + 关键词 fallback |
| `lelamp/soul/memory/history_db.py` | HistoryDB：SQLite + FTS5 全量会话历史 |
| `lelamp/soul/memory/identity.py` | 声纹 + 人脸 embedding 库 |
| `lelamp/soul/memory/scene.py` | 场景记忆（纯文件读写） |
| `main_dual.py` | 入口：创建主 LLM + 交叉 provider review_llm + HistoryDB + 传参 |

### 持久化文件

| 路径 | 用途 |
|---|---|
| `~/.lelamp/memories.json` | 活跃 episodic（heard/said/thought/action） |
| `~/.lelamp/memories.archive.json` | 归档（JSON 备份，保留向后兼容） |
| `~/.lelamp/history.db` | SQLite + FTS5 全量会话历史（替代 archive 的搜索） |
| `~/.lelamp/facts.json` | FactStore |
| `~/.lelamp/longterm.json` + `longterm_emb.npy` | LongTermMemory |
| `~/.lelamp/reminders.json` | ReminderService |
| `~/.lelamp/scene_memory.md` | SceneMemory |
| `~/.lelamp/identities/` | 声纹 + 人脸 |

### 测试

| 文件 | 覆盖范围 |
|---|---|
| `tests/test_background_review.py` | Background Review 触发、记忆提取、JSON 解析（7 个 async 测试） |
| `tests/test_heard_timestamp.py` | heard 时间戳修复、默认时间戳、format_for_prompt 排序 |

---

## 一次性数据操作（Phase 0 已完成）

```bash
# Phase 0 执行的操作：
cp ~/.lelamp/memories.archive.json ~/.lelamp/memories.archive.json.bak
echo '[]' > ~/.lelamp/memories.archive.json
# .bak 永久保留作为冷归档；不再被代码读取
```

Phase 5 启动时自动执行：`HistoryDB.migrate_from_archive()` 将 archive JSON 条目迁移到 SQLite（INSERT OR IGNORE 防重复）。

---

## 端到端验证

### Phase 0-3 验证（已通过）

- ✅ memories.json 不再有 RGB / did / felt 条目
- ✅ SpeechBudget 已删除，监控字段只观察不拦截
- ✅ WorldState 视图层正确，[STATE] 段字段齐全
- ✅ FactStore CRUD 正常，update_fact / forget_fact 工具可用
- ✅ extract_facts 双次确认（source_event_ids 独立 session 判定）
- ✅ today_narrative 生成与跨日 hook
- ✅ episodic 压缩 + thought 条目

### Phase 5 验证

#### 已验证 ✅

| 项目 | 方式 | 结果 |
|------|------|------|
| Background Review 触发 + 记忆写入 | 真机日志 (213405, 225750) | kimi-k2.5 成功保存 2 条记忆 |
| 交叉 provider 策略 | 真机日志 | qwen 主 LLM + kimi review，无 401 |
| 主 LLM save_memory | 真机日志 (210554, 225750) | 主动调用，工具描述改进生效 |
| 主 LLM update_fact | 真机日志 (213405) | "橙汁" → preference/drink 自动保存 |
| 主 LLM recall_memory | 真机日志 (210554) | 主动搜索"李谦 篮球 运动 爱好" |
| HistoryDB 初始化 + 迁移 | 真机日志 | 迁移 400/425 条历史到 SQLite |
| 心跳不再重复回答 | 真机日志 (225750) | 3 次心跳全部正确 wait，零重复 |
| Background Review 单元测试 | test_background_review.py | 7/7 通过 |
| 时间戳修复单元测试 | test_heard_timestamp.py | HEA 正确排在 SAI 前面 |

#### 待验证 ⏳

| 项目 | 验证方式 | 优先级 |
|------|---------|--------|
| session_search 工具 | 用户说"我们之前聊过什么" → 观察 LLM 调用 | 中 |
| HistoryDB FTS5 搜索准确性 | 写搜索单元测试或真机触发 session_search | 中 |
| 长对话压缩 + Review 共存 | 连续聊 15 分钟以上（>40 条触发压缩） | 中 |
| 跨会话记忆回忆 | 新会话问"你记得我喜欢什么运动吗" → recall_memory | 高 |
| HistoryDB 重启不重复迁移 | 多次重启观察是否重复 INSERT | 低（INSERT OR IGNORE） |

---

## 后台任务清单

| 任务 | 触发 | 用途 | 写入目标 |
|------|------|------|----------|
| today_narrative | 心跳入口 fire-and-forget | 刷新当天叙事 | `_today_narrative` 字段 |
| extract_facts | _process_event 末尾 | 抽取结构化 fact | FactStore（经 PendingFactBuffer） |
| compact | _process_event 末尾 | episodic 压缩归档 | episodic + archive + HistoryDB + LongTermMemory |
| background_review | _process_event 末尾 | 后台审查对话提取记忆 | LongTermMemory + FactStore |

四个任务操作不同数据，无互斥锁，可并行运行。

---

## 已决定的设计点

1. **LLM 是唯一决策者** — SpeechBudget 已删；监控只 logger.warning 不拦截
2. **WorldState 是视图层** — `@property` read-through，不复制状态
3. **Fact 冲突解决** — 普通 fact 直接覆盖（last-write-wins）；commitment 已迁移到 ReminderService
4. **Fact 抽取双次确认** — 用 `source_event_ids` 的"独立 session"判定，不靠时间窗或计数
5. **`[STATE]` 段字段优先级** — 假议题，STATE 总量 200-300 token；字段级硬长度上限 + 截断
6. **`extract_facts` LLM 选型** — 用同一个 `_llm`；timeout 60s（异步路径）
7. **`today_narrative` 触发节流** — 新 heard/said ≥5 AND 距上次 ≥10 分钟，在 TimerTick 入口 check
8. **日程是工具，不是记忆** — ReminderService 独立模块 + 独立存储（`~/.lelamp/reminders.json`）
9. **Background Review 是安全网** — 不替代主 LLM 记忆能力，而是后台兜底
10. **交叉 provider** — 主 LLM 和 Review LLM 用不同 provider，避免 rate limit 竞争
11. **heard 延迟写入保留原始时间戳** — 防止 [RECENT] 对话顺序颠倒导致重复回答
12. **HistoryDB 是 archive 的搜索替代** — JSON archive 保留作备份，SQLite 负责查询
13. **Missed reminders 直接丢弃** — 闹钟睡过头就过了，不做补偿触发
14. **后台任务无互斥锁** — today_narrative/extract_facts/compact/background_review 操作不同数据，可并行

## 已知小坑 / Open Issues

1. **`last_user_speaker` 字段缺失**：`WorldState.last_user_speaker` 读 `_last_recognized_speaker`，SoulAgent 没有持久化这个字段
2. **`_today_narrative_date` 跨重启重置**：重启后 narrative 不持久化，第一次心跳 cold_start 重建
3. **archive 迁移每次启动都跑**：`memories.archive.json` 存在时每次启动都调 `migrate_from_archive`，靠 INSERT OR IGNORE 防重复。可优化为检查 history.db 是否已有数据
4. **Review LLM category 偏差**：Review 保存的记忆 category 有时为 `other` 而非准确分类，可在 prompt 加示例引导
