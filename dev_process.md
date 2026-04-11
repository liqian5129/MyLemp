# 小Q 开发进度

> 架构图见 architecture.html

---

## 已完成

| 编号 | 任务 | 说明 |
|------|------|------|
| T-H1 | 本体感知 | body_move/look 返回关节角度 |
| T-H2 | 空间地图 | 场景记忆中记录关节参数 |
| T-F1 | 场景记忆 | 持久化环境描述 + update_scene_memory 工具 |
| T-E1 | 心跳系统优化 | 注入时间/预算/场景/用户状态 |
| T-E2 | 主动关心策略 | 心跳决策流：look → 判断 → 行动 |
| T-E3 | 说话预算 | 心跳触发的主动说话计数+限流（8次/天，5分钟间隔） |
| T-A2 | 用户状态感知 | prompt 引导 LLM 判断 FOCUSED/IDLE/RESTING/AWAY/TALKING |
| T-B1 | 灯光情绪 | set_light_mood 7种氛围 + 渐变 |
| — | VAD 阈值修复 | 5x→3x + ASR 结果优先于时长过滤 |
| T-S1 | Omni RT 客户端 | `lelamp/voice/qwen_omni_rt.py` — WebSocket 连接管理、report_event function calling、mute/unmute、自动重连 |
| T-S1b | AudioEvent 定义 | `lelamp/soul/audio_event.py` — text/emotion/intent/audio_env/is_speech |
| T-S2 | 智能耳朵层 | `lelamp/soul/omni_ear.py` — sounddevice 麦克风 → Omni RT → AudioEvent 回调 |
| T-S3 | ReAct 注入机制 | `soul_agent.py` — on_audio_event + _pending_audio_events 队列 + 步骤间注入 |
| T-S4b | TTS 播放回调 | `doubao_speaker.py` — on_play_start/on_play_end 回调（AEC 用） |
| T-S4 | 新入口 + 接线 | `main_dual.py` — OmniEar → SoulAgent 接线，TTS ↔ mute/unmute |

---

## 待办：设备验证 + 清理

> main_dual.py 在设备上跑通后执行。

| 编号 | 任务 | 状态 | 说明 |
|------|------|------|------|
| — | 设备验证 | 待办 | main_dual.py 全流程验证：正常对话、中途补充(supplement)、取消(cancel)、心跳+环境音、AEC |
| T-S5 | 清理废弃代码 | 待办（依赖设备验证通过） | 删除 continuous_listener.py + funasr_asr.py。main_soul.py 保留为降级入口 |

---

## 待办：功能增强

> 架构迁移完成后实施。

| 编号 | 任务 | 状态 | 依赖 | 说明 |
|------|------|------|------|------|
| T-B2 | 呼吸灯 | 待办 | T-B1 | RGB 后台呼吸效果，与 mood 互斥，空闲时显示"活着" |
| T-A1 | 人脸识别 | 待办 | T-H1 | insightface 集成 look 工具，需评估 RPi 4B 内存 |
| T-H3 | 运动校准 | 待办 | T-H1, T-H2 | prompt 引导 ReAct 中自我校准，无需新代码 |

---

## 待办：成长能力

> 依赖链较长，需功能增强完成后推进。

| 编号 | 任务 | 状态 | 依赖 | 说明 |
|------|------|------|------|------|
| T-F2 | 习惯学习 | 待办 | T-E2 | 每日反思 + 习惯笔记（作息、工作节奏、偏好） |
| T-F3 | 关系记忆 | 待办 | T-A1 | 关系成长文件（互动模式、情感倾向、回应率） |
| T-L1 | 关系成长感 | 待办 | T-F2, T-F3 | 注入相识天数+关系阶段，行为随时间变化 |
| T-L4 | 被用户影响 | 待办 | T-F3 | 每日反思追踪回应率，调整主动度 |
| T-L5 | 关系有重量 | 待办 | T-F3 | 分离检测+回归反应，PresenceTracker |
| T-W2 | 自我工具接入 | 待办 | T-F2, T-F3 | update_habits + update_relationship 工具化 |

---

## 远期 / 待评估

| 编号 | 任务 | 说明 |
|------|------|------|
| T-H4 | 动作审美 | LLM 编排连续动作序列，依赖硬件迭代（需自拍摄像头） |
| T-L2 | 内心世界 | 场景记忆带情感标记，已部分由 prompt 自然涌现 |
| T-L3 | 笨拙但真诚 | 已由工具设计保证（look 必须真看，不能假装） |
