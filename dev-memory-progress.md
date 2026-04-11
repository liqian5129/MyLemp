# 小Q 记忆系统重构 — 进度快照

> 配套文档：`dev-memory-system.md` 是权威方案与设计原理；本文档是当前实施快照。
> 上一次更新：2026-04-10（Phase 5 Background Review + HistoryDB + 时间戳修复）

---

## 状态总览

| Phase | 状态 | 核心交付 |
|---|---|---|
| **Phase 0** | ✅ 完成 | 数据清洗、删 SpeechBudget、加监控（不拦截） |
| **Phase 1** | ✅ 完成 | `memory/` 目录骨架、`WorldState` 视图层、`render_context_packet` |
| **Phase 2** | ✅ 完成 | `FactStore`（纯 fact）、`[FACTS]` 段、update_fact/forget_fact 工具 |
| **Phase 3** | ✅ 完成 | episodic 收紧、`consolidate.py`、`PendingFactBuffer`、`[TODAY]` 段、跨日 hook |
| **Phase 3-fix** | ✅ 完成 | review 9 处修复 + 真机 smoke 通过 |
| **Phase 3-fix-2** | ✅ 完成 | commitment 场景 4 处修复（已被 Reminder 迁移取代） |
| **Reminder 迁移** | ✅ 完成 | Commitment 从记忆层迁到工具层（ReminderService） |
| **Phase 4** | ✅ 已解决 | commitment 精度 + 跨重启恢复 → ReminderService 天然实现 |
| **Phase 5** | ✅ 完成 | Background Review + HistoryDB + 工具描述改进 + 时间戳修复 |

---

## Phase 5：Background Review + HistoryDB（2026-04-10）

### 背景

LongTermMemory 上线后实测发现主 LLM **从未调用 save_memory**。原因：
1. 冷启动 bug（已修复）：0 条记忆时 `[LONGTERM]` 段不渲染
2. 主 LLM 无暇管记忆：每次 `_think()` 是独立的单次 API 调用，LLM 忙着回应对话

借鉴 Hermes agent 的闭环学习系统，两条路径同时解决：
- **Background Review**（安全网）：独立 LLM 后台审查对话，主动提取记忆
- **工具描述改进**（引导）：改善 save_memory/recall_memory 描述，引导主 LLM 主动使用

### 实施内容

#### 5.1 Background Review

**独立 Review LLM 客户端** — 交叉 provider 策略避免 rate limit 竞争：

| 主 LLM | Review LLM |
|--------|-----------|
| qwen3.6-plus (DashScope) | kimi-k2.5 (Moonshot) |
| kimi-k2.5 (Moonshot) | qwen3.6-plus (DashScope) |

`_maybe_background_review()` 方法：
- **触发条件**：有效互动 ≥3 次 且 距上次审查 ≥5 分钟
- **流程**：收集 heard/said 事件 → 格式化对话 → Review LLM 单次调用 → 解析 JSON → 写入 LongTermMemory/FactStore
- **安全约束**：提示词明确"只保存用户明确表达的信息，不要从单次行为推断偏好"
- **超时**：30 秒

#### 5.2 删除 `_consolidate_lock`

三个后台任务（today_narrative、extract_facts、compact）操作不同数据，不需要互斥锁。锁的存在导致 60s LLM 调用期间其他任务被跳过。

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

### 文件改动清单

| 文件 | 改动 |
|------|------|
| `lelamp/soul/soul_agent.py` | 删锁 + Background Review + 工具描述 + PERSONALITY_PROMPT + session_search 工具 + 时间戳修复 |
| `main_dual.py` | 创建交叉 provider review_llm + HistoryDB + 传参 |
| `lelamp/soul/memory/history_db.py` | **新建** — SQLite + FTS5 |
| `lelamp/soul/memory/episodic.py` | 双写 SQLite + `add()` 支持 timestamp 参数 |
| `lelamp/soul/memory/__init__.py` | 导出 HistoryDB |
| `tests/test_background_review.py` | **新建** — 7 个 async 测试用例 |
| `tests/test_heard_timestamp.py` | **新建** — 时间戳修复验证 |

### 验证状态

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
| extract_facts + Review 不冲突 | 两者写不同存储（FactStore vs LongTermMemory） | 低 |

---

## 当前目录结构

```
lelamp/soul/
├── soul_agent.py          ← 主体
├── reminder.py            ← 定时提醒工具层
├── audio_event.py
├── camera_capture.py
├── omni_ear.py
└── memory/
    ├── __init__.py        ← re-export 所有公共符号
    ├── episodic.py        ← Phase 3 收紧 + Phase 5 双写 SQLite + timestamp 参数
    ├── scene.py
    ├── identity.py
    ├── state.py           ← WorldState 视图层
    ├── render.py          ← render_context_packet
    ├── facts.py           ← FactStore
    ├── consolidate.py     ← extract_facts + today_narrative + PendingFactBuffer
    ├── longterm.py        ← LongTermMemory（向量搜索 + 关键词 fallback）
    └── history_db.py      ← Phase 5 新建：SQLite + FTS5 会话历史
```

---

## 六层模型与注入通道（已落地）

| 层 | 实现 | 注入位置 |
|---|---|---|
| Working | ReAct messages 列表 | LLM multi-turn |
| **State** | `WorldState`（@property read-through） | `[STATE]` 段 |
| **Today** | `_today_narrative` + 异步刷新 | `[TODAY]` 段 |
| **Episodic** | `MemoryStream`（heard/said/thought/action） | `[RECENT]` 段 |
| **Semantic / Facts** | `FactStore`（identity/calling/preference/daily_reflection） | `[FACTS]` 段 |
| **Long-term** | `LongTermMemory`（向量搜索） | `[LONGTERM]` 段（摘要）+ recall_memory 工具 |
| Procedural | `PERSONALITY_PROMPT` | system prompt |

`render_context_packet` 段顺序：`[STATE] → [FACTS] → [LONGTERM] → [TODAY] → [SCENE] → [RECENT] → [TRIGGER]`

### 记忆写入路径

| 来源 | 目标 | 触发 |
|------|------|------|
| 主 LLM（_think 中） | LongTermMemory / FactStore | save_memory / update_fact 工具调用 |
| Background Review | LongTermMemory / FactStore | 有效互动 ≥3 且 ≥5 分钟间隔 |
| extract_facts（consolidate.py） | FactStore（经 PendingFactBuffer 二次确认） | 新事件 ≥10 或 ≥1 小时 |
| extract_longterm_memories（压缩时） | LongTermMemory | episodic 压缩触发（>40 条） |

---

## 持久化文件状态

| 路径 | 用途 |
|---|---|
| `~/.lelamp/memories.json` | 活跃 episodic（heard/said/thought/action） |
| `~/.lelamp/memories.archive.json` | 归档（JSON 备份，保留向后兼容） |
| `~/.lelamp/history.db` | **Phase 5 新建** — SQLite + FTS5 全量会话历史（替代 archive 的搜索） |
| `~/.lelamp/facts.json` | FactStore |
| `~/.lelamp/longterm.json` + `longterm_emb.npy` | LongTermMemory |
| `~/.lelamp/reminders.json` | ReminderService |
| `~/.lelamp/scene_memory.md` | SceneMemory |
| `~/.lelamp/identities/` | 声纹 + 人脸 |

---

## 后台任务清单

| 任务 | 触发 | 用途 | 独立性 |
|------|------|------|--------|
| today_narrative | 心跳入口 fire-and-forget | 刷新当天叙事 | 写 `_today_narrative` 字段 |
| extract_facts | _process_event 末尾 | 抽取结构化 fact | 写 FactStore（经 PendingFactBuffer） |
| compact | _process_event 末尾 | episodic 压缩归档 | 写 episodic + archive + HistoryDB |
| background_review | _process_event 末尾 | 后台审查对话提取记忆 | 写 LongTermMemory + FactStore |

四个任务操作不同数据，无互斥锁，可并行运行。

---

## 已 settle 的关键设计决策

1. **LLM 是唯一决策者** — SpeechBudget 已删；监控只 logger.warning 不拦截
2. **WorldState 是视图层** — `@property` read-through，不复制状态
3. **日程是工具，不是记忆** — ReminderService 独立模块 + 独立存储
4. **fact 双次确认** — `source_event_ids` 无重叠才 promote
5. **today_narrative 触发在 TimerTick 入口** — 不污染语音热路径
6. **Background Review 是安全网** — 不替代主 LLM 记忆能力，而是后台兜底
7. **交叉 provider** — 主 LLM 和 Review LLM 用不同 provider，避免 rate limit 竞争
8. **heard 延迟写入保留原始时间戳** — 防止 [RECENT] 对话顺序颠倒
9. **HistoryDB 是 archive 的搜索替代** — JSON archive 保留作备份，SQLite 负责查询
10. **Missed reminders 直接丢弃** — 闹钟睡过头就过了，系统决定

---

## Open issues / 已知小坑

1. **`last_user_speaker` 字段缺失**：`WorldState.last_user_speaker` 读 `_last_recognized_speaker`，SoulAgent 没有持久化这个字段
2. **`_today_narrative_date` 跨重启重置**：重启后 narrative 不持久化，第一次心跳 cold_start 重建
3. **archive 迁移每次启动都跑**：`memories.archive.json` 存在时每次启动都调 `migrate_from_archive`，靠 INSERT OR IGNORE 防重复。可优化为检查 history.db 是否已有数据
4. **Review LLM category 偏差**：Review 保存的记忆 category 有时为 `other` 而非准确分类，可在 prompt 加示例引导

---

## 资源索引

- **权威方案**：`dev-memory-system.md`
- **代码主体**：`lelamp/soul/soul_agent.py`、`lelamp/soul/memory/`、`lelamp/soul/reminder.py`
- **持久化**：`~/.lelamp/{memories.json, facts.json, longterm.json, history.db, reminders.json, scene_memory.md}`
- **测试**：`tests/test_background_review.py`、`tests/test_heard_timestamp.py`
