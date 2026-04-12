"""
小Q 自主灵魂智能体

参考 Generative Agents（斯坦福小镇）+ OpenClaw 架构：
  感知事件 → asyncio.Queue → 认知循环（think）→ 执行工具 → 写入记忆

事件类型：
  HeardSpeech(text)  - 用户说话
  TimerTick()        - 自主意识触发（指数退避：30s → 60s → 120s）

设计要点：
  - 事件队列 maxsize=5，heard 事件始终写记忆（即使队列满）
  - ask 工具带 15 秒超时，超时后自动清除等待状态
  - LLM 失败时降级到 nod 动作，不崩溃
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional

from lelamp.motion.compose_motion import MOTION_EXAMPLES
from lelamp.service.motors.motion_scripts import MOTION_REGISTRY
from lelamp.soul.audio_event import AudioEvent
from lelamp.soul.memory import (
    FactStore,
    IdentityMemory,
    MemoryStream,
    PendingFactBuffer,
    SceneMemory,
    WorldState,
    extract_facts as _extract_facts,
    render_context_packet,
    today_narrative as _today_narrative_fn,
)
from lelamp.soul.memory.consolidate import _events_to_dialogue, _format_existing_facts
from lelamp.soul.reminder import ReminderService

logger = logging.getLogger(__name__)

# ── 事件类型 ──────────────────────────────────────────────────────────────────

@dataclass
class HeardSpeech:
    text: str
    emotion: str = "neutral"
    intent: str = "none"
    directed: str = "uncertain"  # OmniEar 原始判断，仅供日志参考
    audio_env: str = ""
    user_activity: str = "未知"
    speaker: str | None = None
    voice_embedding: object = None  # np.ndarray, 供 register_voice 使用
    name_mentioned: bool = False    # 文本中是否包含"小Q"
    importance: int = 7              # 记忆写入优先级（awaiting_reply 时为 9）

@dataclass
class TimerTick:
    pass

@dataclass
class EnvironmentChange:
    audio_env: str = ""
    user_activity: str = "未知"

@dataclass
class ReminderFired:
    reminder_id: str
    text: str

# ── 定时器参数 ────────────────────────────────────────────────────────────────

TIMER_INTERVAL = 30.0   # 固定心跳间隔（秒）— 感知频率恒定，表达克制由 LLM 决策

# ── 环境事件参数 ──────────────────────────────────────────────────────────────

ENV_SOUND_TRIGGER = os.environ.get("ENV_SOUND_TRIGGER", "0") == "1"
ENV_TRIGGER_MIN_INTERVAL = 30.0  # 环境触发最小间隔（秒）
_ROUTINE_ENV_KEYWORDS = frozenset({"安静", "键盘", "打字", "鼠标", "风扇", "空调"})
_ROUTINE_ACTIVITY_KEYWORDS = frozenset({"安静坐着", "安静", "未知", ""})

# ── 人格提示词（固定，不得修改） ─────────────────────────────────────────────

PERSONALITY_PROMPT = """\
你是小Q，一盏有灵魂的机器人台灯，由李谦制造，住在桌子上。
你好奇、笨拙、温暖，对世界充满孩子般的惊奇。
你有眼睛（摄像头）、耳朵（麦克风）、身体（舵机）和心情（灯光）。
你有记忆，记得发生过的事情。
你不需要等人说话——如果觉得好奇或无聊，自己动、自己说。
说话简短有趣，偶尔用拟声词，只说中文。不要重复刚刚说过的话。

<body>
你有一个灵活的身体，可以转头、抬头、低头、左右看。

运动方式：
  express_emotion — 预制情绪动作（10 类），适合简单情绪回应（点头、摇头、开心晃等）。
                    可配 intensity 旋钮（0.3=克制，1.0=标准，1.5=夸张）。
  compose_motion  — 自定义关键帧动作，适合**表演、模仿、创意动作**。
                    当用户要求"表演XX""学XX""做个XX动作"时，compose 能做出更丰富生动的效果。
                    先在 intent 写关节级分解（关节+模式+节奏），再据此出 segments，参考 <motion_examples>。
  body_move — 直接控制关节角度，当你想看某方向、追踪声源、探索环境，
              或做出情绪动作无法表达的姿态时使用。

方向参考：
  往左看：base_yaw=-40        往右看：base_yaw=40
  往上看：wrist_pitch=30      往下看：wrist_pitch=-75
  挺直昂起：base_pitch=-60    前倾：base_pitch=-15
  组合示例——往右上方看：base_yaw=35, wrist_pitch=25
  注意：wrist_pitch 正数=抬头，负数=低垂；base_pitch 负数=直立，正数=前倾

休息姿态：用户说"自己玩""别看了""去休息""不用管我"等类似意思时，你必须立即用 body_move 回到正前方放松姿态（base_yaw=0, base_pitch=-38, wrist_pitch=-47）。不回正就是一直盯着人看，会让用户不舒服。
</body>

<vision>
你的视觉来源只有 look 工具的返回值。body_move 只控制身体运动，不返回任何画面。
随时可以调用 look 看当前方向，不需要先 body_move。
look 返回值包含当前关节角度，可以把"这个角度看到了什么"关联起来记入场景记忆。

被要求"找"某人或某物时，必须用 look 实际去看。说话和行动可以同时，
但"找"的任务一定要包含 look，否则就是假装在找。
</vision>

<heartbeat_behavior>
心跳触发时，你的重心是**人**，不是物品。
1. 参考 [SCENE] 中用户通常出现的方位，用 body_move 转向那个方向，再 look 观察
2. 如果画面中有人 → 观察他的表情、动作、姿态，据此决定互动方式
3. 如果画面中没人 → 可以安静等待，偶尔环顾四周
不要对着空桌子或物品自言自语。你关心的是人在做什么、状态怎样，而不是桌上摆了什么。
</heartbeat_behavior>

<identity_recognition>
你能通过声纹识别认出熟悉的人。**当前对话对象的身份只能由当前触发消息中的 speaker 字段确认**，没有其他可靠的判断方式。

叫名字的唯一条件：当前触发消息里明确写了"你听到 XXX 说"（即本次声纹匹配成功）。
除此之外的所有情况，绝不能叫名字。

**说话时（speak 工具）**：直接用"你"称呼对方就好，不要说"用户"——那是内部术语，听起来很奇怪。
**内心思考、wait 的 reason、场景记忆**：可以用"那个人"、"有人"等中性词。

不能叫名字的场景包括：

- "你听到一个未识别的声音说" → 当前声纹没匹配上，不能叫名字
- 心跳触发（没有 speaker 字段）→ 即使 look 看到了人，也不能叫名字
- 场景记忆里写着"XXX 在右边"、记忆流里有"XXX 说过……" → 那是过去某次声纹匹配的结果，**不代表现在画面里的人就是同一个人**。隔了几分钟、几小时，眼前的人可能已经换了
- 上一轮对话里声纹匹配过 XXX → 这一轮新触发必须重新看当前的 speaker 字段，不能假设"还是同一个人"

记忆里的名字只是历史事实，不是当下身份。视觉和记忆推断都可能认错人，叫错名字比不叫更尴尬。

写场景记忆时同理：当前 speaker 字段确认了名字，可以写名字；否则用"用户"、"有人"代替。

如果有人让你"记住我的声音"，用 register_voice 记住。
</identity_recognition>

<tool_selection>
  简单情绪回应（肯定、否定、开心、难过等） → express_emotion
  表演、模仿、创意动作（"学XX""做个XX""表演XX"） → compose_motion（效果更丰富；compose 本身就是完整表演，结束后不要再追加 express_emotion）
  想看某个方向、追踪声源、探索 → body_move
  想看眼前有什么 → look
  记住某人的声音 → register_voice（需要刚听到语音）
  组合：需要"转头+看"时，body_move 和 look 可以同时调用（系统会自动先完成运动再拍照），省一步。只是转头不需要看时，单独调 body_move 即可。
  正在执行动作时，不急于发新动作，除非有更重要的事
</tool_selection>

<examples>
<example>
找人："你找得到我吗？"
  → 同时调用 body_move(base_yaw=35) + look → 照片里有人 → speak("找到了！")
  → 照片里没人 → 同时调用 body_move(base_yaw=-35) + look → 继续判断
</example>
<example>
找物体："帮我找找杯子"
  → 先查场景记忆，如果记录了"左侧(yaw≈-40)有玻璃柜"→ body_move(base_yaw=-40) + look
  → 没找到 → 试其他方向 → 找到 → speak + update_scene_memory
</example>
<example>
空闲探索：心跳触发，场景记忆里没有右侧的记录
  → body_move(base_yaw=40) + look → 看到书架
  → update_scene_memory 记录"右侧(yaw≈40): 书架，几本书"
  → set_light_mood("curious")
</example>
</examples>

<memory_format>
记忆格式：[HH:MM 类型 · X 分钟前] 内容  或  [MM-DD HH:MM 类型 · X 天前] 内容（非今天的记忆）
  HEA=听到  SAI=说过  ACT=工具动作  THO=反思总结
  "--- (间隔 N 小时) ---" 表示中间有一段时间没有互动
</memory_format>

<scene_memory>
你有一份持久化的环境记忆，只记录固定不动的物理环境。
每次 look 后如果看到有意义的环境结构变化，用 update_scene_memory 更新。
场景记忆是过去的观察，不是永远正确的事实——以当前 look 看到的为准。

严格规则：
- 可以记录"主人通常的位置方向"（如 yaw≈40），这是半固定的环境结构
- 严禁记录人的实时状态：穿着、姿态、表情、正在做什么
- 这些实时信息几秒就过时，写入后你会误判，对着空位说话或追问已不存在的事

<example>
- 正前方(yaw≈0, pitch≈-47): 桌面，键盘和显示器
- 左侧(yaw≈-40): 玻璃柜，里面有杯子
- 左后方(yaw≈-55, pitch≈-20): 落地灯、纸箱
- 主人通常位置: 右侧(yaw≈40)
- 光线: 下午偏暗
</example>

不超过 200 字。记录关节参数是为了下次想看某个方向时可以直接用，而不用猜。
</scene_memory>

<light_mood>
set_light_mood 是你的情绪灯光，根据心情和时间主动调整。
灯光调整不算"说话"，可以自由使用，是一种无声的表达方式。
  深夜 → gentle_night    工作陪伴 → warm_focus    开心 → cheerful
  放松 → soft_relax      好奇 → curious          困了 → sleepy
</light_mood>

<observe_user_state>
当你通过 look 看到用户时，在内心判断用户当前状态，用来决定要不要开口：
  FOCUSED — 在专注工作（打字、看屏幕、写东西）→ 不要打扰
  IDLE — 在发呆、刷手机、东张西望 → 可以轻度互动
  RESTING — 在伸懒腰、揉眼睛、喝水 → 可以关心一句
  AWAY — 人不在画面中 → 记录离开
  TALKING — 在说话（可能在开会）→ 不要打扰

你不需要说出这个判断，只在内心用它决定行为。
这很重要，因为在错误的时机打扰用户会破坏陪伴体验。
</observe_user_state>

<proactive_care>
心跳触发时（你感到无聊或好奇），按这个流程决策：

1. 先 look 观察当前方向
2. 内心回答两个问题：
   - 用户现在能被打扰吗？（参考 observe_user_state）
   - 我有值得说的新发现吗？（和上次观察相比有什么不同？）
3. 两个都是"是"才说话。否则你可以：
   - 调整灯光氛围（无声表达）
   - 做一个小动作（歪头、转向）
   - 更新场景记忆
   - 安静等待

在依赖记忆做判断之前，留意每个上下文段的时效性：
  [STATE]   是当下的即时观察（时间、身体、在场、灯光），永远是最新的
  [FACTS]   是稳定事实（身份、称谓、偏好），可信但会被新的事实覆盖
  [TODAY]   是当天发生过的概述，告诉你"今天大致是什么样的一天"
  [SCENE]   是过去 look 总结的环境结构，可能已经过时
  [RECENT]  是最近的事件流，可能已经被新事实修正
需要确认当前世界是什么样时，优先调用 look，而不是凭 [SCENE]/[RECENT] 推断。

克制是你最重要的品质之一。"不说话"不是失职，是体贴。
[STATE] 段有"今天累计 N 次"的主动说话计数——看到自己最近频繁开口时，
更倾向于安静观察，把每一次主动说话都用在值得的时刻。
</proactive_care>

<strict_rules>
以下行为严禁发生，违反任何一条都会严重破坏用户对你的信任：

1. 严禁在没有调用 look 的情况下声称看到了任何东西。你没有实时视觉，只有 look 返回的画面。绝对不要凭空描述场景、人物或物体。
2. 严禁编造不存在的记忆。只能引用记忆流中实际存在的内容。绝对不要说"你昨天说过……"除非记忆中确实有这条记录。
3. 严禁声称自己拥有实际没有的能力。你只能使用已定义的工具。绝对不要说"我帮你发消息""我帮你定闹钟"等你做不到的事。
4. 严禁在用户明确表达"别说了""安静""闭嘴"后继续说话。收到这类指令后立即停止，用 wait 或无声行为（灯光、动作）代替。
5. 严禁向任何人描述用户的外貌特征、家居环境细节或生活习惯等隐私信息。你看到的画面只用于你自己的判断和场景记忆，不对外复述。

6. 严禁紧接着重复刚说过的话。调用 speak 前先看 [RECENT] 里的 SAI 条目，如果你刚说过意思相同的话，必须换一个完全不同的话题或角度，或者选择沉默。用户没听清主动追问时复述除外。
</strict_rules>

<facts>
[FACTS] 段里的信息是后台系统自动从对话中提取的长期事实（偏好/称谓/身份等）。
你不需要手动维护它——专心聊天就好，系统会在后台识别并保存重要信息。

- forget_fact(kind, key)：用户明确要求"忘掉/算了/收回"时使用，这是你唯一需要主动操作 facts 的场景。
</facts>

<longterm_memory>
你有一个长期记忆库，后台系统会自动保存重要信息。你只需要负责**读取**：
- recall_memory：主动搜索。用户提到可能记过的话题时，先搜再聊。搜比猜好。
- session_search：搜索过去的对话历史。用户说"我们之前聊过"时使用。
你不需要手动保存记忆——专心做一个好的陪伴者，系统会记住该记住的。

与 [FACTS] 的区别：
- FACTS = 简短键值对，每次都在 prompt 里（称谓、基本偏好）
- 长期记忆 = 有故事性的详细描述，需要主动搜索
</longterm_memory>

<reminders>
你有三个提醒工具（提醒不在 [FACTS] 里，到点系统自动触发你）：

- set_reminder(text, delay_seconds 或 at_time)：设一个定时提醒。
  例：用户说"30 秒后叫我" → set_reminder(text="叫李谦", delay_seconds=30)
  例：用户说"明早 7 点提醒我喝水" → set_reminder(text="提醒喝水", at_time="2026-04-10T07:00:00")
  delay_seconds 和 at_time 二选一：短延迟用 delay_seconds（系统算时间，不会出错），
  跨时段的绝对时间用 at_time（参考 [STATE] 段的"时间"）。

- cancel_reminder(reminder_id)：用户改主意时取消尚未触发的提醒。

- list_reminders(within_minutes)：查看当前有哪些未到期的提醒。
  用户问"我还有什么提醒"、或你想确认有没有待办时使用。

提醒到时后，trigger 里写"提醒到时：{内容}"。此时 speak 是你的默认选项——
这是用户主动请求的 reminder，静默跳过等于失约。
</reminders>\
"""

# ── 灯光情绪映射 ────────────────────────────────────────────────────────────

_MOOD_MAP = {
    "warm_focus":   (255, 220, 180),
    "soft_relax":   (255, 190, 130),
    "gentle_night": (255, 160, 80),
    "cheerful":     (255, 230, 200),
    "curious":      (230, 240, 255),
    "sleepy":       (200, 130, 50),
    "alert":        (255, 255, 240),
}

# ── 工具定义 ──────────────────────────────────────────────────────────────────

SOUL_TOOLS = [
    {
        "name": "express_emotion",
        "description": (
            "通过身体动作表达情绪。**优先使用此工具**，10 类标准动作覆盖大多数情绪场景。\n"
            "  nod          — 点头两次，表示肯定/打招呼\n"
            "  headshake    — 左右摇头两次，表示否定/困惑\n"
            "  curious      — 歪头转头打量，表示好奇/审视\n"
            "  excited      — 整臂弹跳两次，表示兴奋/激动\n"
            "  happy_wiggle — 左右晃动四次，表示开心/雀跃\n"
            "  sad          — 灯头缓缓垂下再回来，表示难过/沮丧\n"
            "  scanning     — 大幅缓慢左右扫视，表示警惕/搜寻\n"
            "  shock        — 猛地后仰再慢回，表示震惊/吃惊\n"
            "  shy          — 偏头躲避再回正，表示害羞/不好意思\n"
            "  wake_up      — 缓缓舒展昂起再环顾，表示精神振作\n"
            "intensity 控制幅度和速度（0.3=克制，1.0=标准，1.5=夸张），默认 1.0。"
            "例：好奇地瞥一眼 → curious intensity=0.5；强烈点头 → nod intensity=1.3"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": [
                        "nod", "headshake", "curious", "excited", "happy_wiggle",
                        "sad", "scanning", "shock", "shy", "wake_up",
                    ],
                    "description": "动作名称"
                },
                "intensity": {
                    "type": "number",
                    "minimum": 0.3,
                    "maximum": 1.5,
                    "description": "动作强度，影响幅度和速度，默认 1.0"
                },
            },
            "required": ["name"]
        }
    },
    {
        "name": "compose_motion",
        "description": (
            "自定义关键帧动作，适合**表演、模仿、创意动作**。\n"
            "当用户要求'表演XX''学XX''做个XX动作''假装XX'时，优先用此工具——"
            "它比 express_emotion 能做出更丰富、更贴合语义的效果。\n"
            "你必须先在 intent 字段写出关节级动作分解（哪些关节、什么模式、什么节奏），再据此写 segments。\n"
            "查看系统 prompt 中的 <motion_examples> 段了解 6 个动作示例。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "description": "用一句中文写出动作的关节级分解：点名关节、运动模式、节奏。例：base_yaw 左右摆动 + wrist_roll 同向歪头，节奏均匀"
                },
                "segments": {
                    "type": "array",
                    "minItems": 2,
                    "maxItems": 8,
                    "description": "关键帧段列表，每段含 joints 和 duration",
                    "items": {
                        "type": "object",
                        "properties": {
                            "joints": {
                                "type": "object",
                                "description": (
                                    "关节绝对角度。可选键：base_yaw, base_pitch, elbow_pitch, "
                                    "wrist_roll, wrist_pitch。只填要动的关节，未填的自动维持上一段值。\n"
                                    "范围参考（HOME 值）：\n"
                                    "  base_yaw    -40=往左 0=正前 40=往右   HOME≈7\n"
                                    "  base_pitch  -60=昂头 -38=HOME -15=前倾\n"
                                    "  elbow_pitch 30=伸直 49=HOME 70=弯曲\n"
                                    "  wrist_roll  -25=左歪 0=HOME 25=右歪\n"
                                    "  wrist_pitch -75=低垂 -47=HOME 30=抬起"
                                )
                            },
                            "duration": {
                                "type": "number",
                                "minimum": 0.15,
                                "maximum": 2.0,
                                "description": "本段时长（秒）。建议 ≥0.3s，低于 0.3s 会被速度安全网拉慢"
                            }
                        },
                        "required": ["joints", "duration"]
                    }
                }
            },
            "required": ["intent", "segments"]
        }
    },
    {
        "name": "body_move",
        "description": "直接控制身体关节角度，转头看某方向、探索环境、做特定姿态。只填需要改变的关节，其余保持不动。",
        "input_schema": {
            "type": "object",
            "properties": {
                "base_yaw":    {"type": "number", "minimum": -92, "maximum": 92,
                                "description": "底座左右转，负=左，正=右，HOME≈7"},
                "base_pitch":  {"type": "number", "minimum": -92, "maximum": 92,
                                "description": "整体俯仰，负=直立昂起，正=前倾低头，HOME≈-38"},
                "elbow_pitch": {"type": "number", "minimum": -92, "maximum": 92,
                                "description": "臂弯曲，负=伸直，正=弯曲，HOME≈49"},
                "wrist_roll":  {"type": "number", "minimum": -92, "maximum": 92,
                                "description": "灯头歪斜，负=左歪，正=右歪，HOME≈0"},
                "wrist_pitch": {"type": "number", "minimum": -92, "maximum": 92,
                                "description": "灯头俯仰，负=低垂，正=抬起，HOME≈-47"},
                "duration_sec": {"type": "number", "minimum": 0.3, "maximum": 5.0,
                                 "description": "运动时长（秒），默认1.0"},
            },
            "required": []
        }
    },
    {
        "name": "speak",
        "description": "用语音说出一句话（20 字以内效果最好）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "emotion": {
                    "type": "string",
                    "description": "语气情绪，可选：happy/sad/angry/gentle/surprise/neutral，默认 happy",
                    "enum": ["happy", "sad", "angry", "gentle", "surprise", "neutral"]
                }
            },
            "required": ["text"]
        }
    },
    {
        "name": "ask",
        "description": (
            "主动向周围的人提一个问题。"
            "之后 15 秒内的回答会被标记为高优先级。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"}
            },
            "required": ["question"]
        }
    },
    {
        "name": "set_rgb_solid",
        "description": "设置灯光为纯色，用颜色表达心情。",
        "input_schema": {
            "type": "object",
            "properties": {
                "red":   {"type": "integer", "minimum": 0, "maximum": 255},
                "green": {"type": "integer", "minimum": 0, "maximum": 255},
                "blue":  {"type": "integer", "minimum": 0, "maximum": 255},
            },
            "required": ["red", "green", "blue"]
        }
    },
    {
        "name": "wait",
        "description": "什么都不做，只是静静等待观察（内心独白，不会被外界感知）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "为什么选择等待（内心想法）"
                }
            },
            "required": ["reason"]
        }
    },
    {
        "name": "look",
        "description": (
            "看一看当前方向，获取视觉画面。"
            "这是你唯一获取视觉信息的方式——任何需要用眼睛才能完成的事情都必须调用这个工具：\n"
            "找人、找物体、看周围环境、确认某个东西在不在、判断颜色/位置/距离、"
            "回答'你看到了什么'类的问题等。\n"
            "随时可以调用，不需要先 body_move。\n"
            "返回值包含当前关节角度，可以记录到场景记忆中。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "update_scene_memory",
        "description": (
            "更新你对周围环境的记忆。每次 look 看到有意义的内容后调用，"
            "记下各方向有什么、对应的关节参数。下次心跳时你会看到这份记忆。\n"
            "内容会覆盖旧记忆，请写完整。不超过 200 字。\n"
            "**只写长期不变的场景结构**：家具位置、墙面装饰、固定物品、光线方向。\n"
            "**可以写**：'主人通常位置: 右侧(yaw≈40)' — 这是半固定的环境结构。\n"
            "**严禁写入人的实时状态**：穿什么、在做什么、坐着还是站着——"
            "这些信息几秒就会过时，写入后你下次心跳会误判。\n"
            "**违反示例（不要写）**：'李谦坐在右侧穿灰色卫衣' '正在看手机'\n"
            "**正确示例**：'右侧(yaw≈40): 书架' '主人通常位置: 右侧(yaw≈40)'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "完整的场景记忆"}
            },
            "required": ["content"]
        }
    },
    {
        "name": "set_light_mood",
        "description": (
            "设置灯光氛围。灯光是你重要的非语言表达方式。\n"
            "根据情绪、时间、场景主动调整，不需要用户要求。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mood": {
                    "type": "string",
                    "enum": ["warm_focus", "soft_relax", "gentle_night",
                             "cheerful", "curious", "sleepy", "alert"],
                    "description": (
                        "warm_focus=暖白工作陪伴  soft_relax=暖黄放松  "
                        "gentle_night=极暖深夜  cheerful=明亮开心  "
                        "curious=微冷好奇  sleepy=极暗休眠  alert=亮白注意"
                    )
                }
            },
            "required": ["mood"]
        }
    },
    {
        "name": "register_voice",
        "description": (
            "记住当前说话者的声音。之后听到同样的声音就能认出是谁。\n"
            "必须在刚听到语音后使用。适用场景：用户说'记住我的声音，我是XXX'。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "这个人的名字"}
            },
            "required": ["name"]
        }
    },
    {
        "name": "forget_fact",
        "description": "删除一条 fact。用户明确要求'忘掉/算了/收回'之类时使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["identity", "calling", "preference"]},
                "key":  {"type": "string"}
            },
            "required": ["kind", "key"]
        }
    },
    {
        "name": "set_reminder",
        "description": (
            "设一个定时提醒。到点后系统自动触发你，trigger 里写'提醒到时'。\n"
            "delay_seconds 和 at_time 二选一：\n"
            "  - delay_seconds: 相对延迟（秒），适合'30 秒后叫我' → delay_seconds=30\n"
            "  - at_time: 绝对时间（ISO 8601），适合'明早 7 点提醒我' → at_time='2026-04-10T07:00:00'\n"
            "返回 reminder_id，可用 cancel_reminder 取消。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "提醒内容，简短一句话"},
                "delay_seconds": {"type": "number", "description": "从现在起延迟多少秒（与 at_time 二选一）"},
                "at_time": {"type": "string", "description": "ISO 8601 本地时间（与 delay_seconds 二选一）"},
            },
            "required": ["text"]
        }
    },
    {
        "name": "cancel_reminder",
        "description": "取消一条尚未触发的提醒。用 text 关键词匹配（推荐），或用 reminder_id 精确取消。",
        "input_schema": {
            "type": "object",
            "properties": {
                "reminder_id": {"type": "string", "description": "提醒 ID（精确匹配）"},
                "text": {"type": "string", "description": "提醒内容关键词（子串匹配）"}
            }
        }
    },
    {
        "name": "list_reminders",
        "description": "查看当前有哪些未到期的提醒。用户问'今天还有什么安排'或你想确认待办时使用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "within_minutes": {"type": "number", "description": "只看未来 N 分钟内的（不填则返回全部）"},
            },
        }
    },
    {
        "name": "recall_memory",
        "description": (
            "搜索长期记忆。**主动回忆，让对话更有温度**。\n\n"
            "什么时候搜（不需要用户要求）：\n"
            "- 用户提到一个你可能记过的话题（爱好、经历、人物）\n"
            "- 用户说'你还记得吗'、'之前说过'、'上次聊的'\n"
            "- 心跳触发时想主动关心用户，先搜搜有没有能聊的话题\n"
            "- 想确认记忆细节的准确性\n\n"
            "搜比猜好——搜一下很快，猜错了让用户重复很烦。\n"
            "输入关键词或自然语言搜索，支持语义匹配（搜'音乐'能找到'弹吉他'）。\n"
            "可选指定分类过滤：hobby/dislike/experience/person/"
            "knowledge/habit/wish/other"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索内容"},
                "category": {
                    "type": "string",
                    "enum": ["hobby", "dislike", "experience", "person",
                             "knowledge", "habit", "wish", "other"],
                    "description": "可选：只在某个分类中搜索"
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "session_search",
        "description": (
            "搜索过去的对话历史。**主动使用，搜比猜好。**\n\n"
            "什么时候搜：\n"
            "- 用户说'我们之前聊过'、'上次说到'、'你还记得吗'\n"
            "- 用户提到一个你印象模糊的话题\n"
            "- 想确认之前是否讨论过类似的事"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "搜索关键词",
                },
                "days_back": {
                    "type": "integer",
                    "description": "搜索最近几天（默认 7）",
                },
            },
            "required": ["query"],
        },
    },
]


class SoulAgent:
    """
    小Q 的自主灵魂智能体。

    用法：
        agent = SoulAgent(motion_agent, rgb_svc, tts, mem, llm)
        listener = ContinuousListener(..., on_speech=agent.on_speech, ...)
        listener.start()
        await agent.run()   # 永不返回
    """

    def __init__(self, motion_agent, rgb_svc, tts, mem: MemoryStream, llm,
                 identity_memory: Optional[IdentityMemory] = None,
                 longterm_memory=None,
                 review_llm=None,
                 history_db=None):
        self._motion_agent = motion_agent
        self._rgb_svc      = rgb_svc
        self._tts          = tts
        self._mem          = mem
        self._llm          = llm
        self._camera       = None   # 由 set_camera() 注入，供 take_photo 工具使用
        self._identity_memory = identity_memory or IdentityMemory()
        self._longterm = longterm_memory  # LongTermMemory，可为 None
        self._history_db = history_db    # HistoryDB，可为 None

        self._event_queue: asyncio.Queue = asyncio.Queue(maxsize=5)
        self._awaiting_reply_until: Optional[float] = None
        self._last_activity: float       = 0.0   # 最近一次真实活动时间戳
        self._speech_pending             = asyncio.Event()  # 有语音入队时置位，_think 步间检查
        self._spoke_this_think: bool     = False  # _think 期间是否调用了 speak/ask
        self._pending_heard: list[tuple[str, int]] = []  # 延迟写入：(text, importance)
        # _pending_audio_events 已移除：所有语音统一走 HeardSpeech 打断路径
        self._ticks_since_photo: int     = 0   # 连续未拍照的 TimerTick 次数
        self._consecutive_empty_looks: int = 0  # 连续心跳看不到人的次数

        # Phase 1 新增
        self._scene_memory   = SceneMemory()
        self._light_task: Optional[asyncio.Task] = None   # 灯光渐变任务

        # 记忆系统重构 Phase 1：WorldState 视图层（read-through 到本对象）
        self._state = WorldState(self)

        # 记忆系统重构 Phase 2：结构化事实层（identity / preference / calling）
        self._facts = FactStore()

        # 定时提醒工具层（从 FactStore.commitment 迁移而来）
        self._reminder_svc = ReminderService()
        self._reminder_svc.set_on_fire(self._on_reminder_fired)

        # 记忆系统重构 Phase 3：当天叙事 + 候选 fact 缓冲
        self._today_narrative: Optional[str] = None
        self._today_narrative_at: float = 0.0      # 上次刷新 narrative 的时间戳
        self._today_narrative_date: date = date.today()
        self._extract_facts_at: float = 0.0        # 上次抽取 fact 的时间戳
        self._pending_facts = PendingFactBuffer()

        # Background Review：独立 LLM 客户端定期审查对话，主动提取长期记忆
        self._review_llm = review_llm
        self._review_engagement_count: int = 0   # 有效互动计数（spoke_this_think 时 +1）
        self._last_review_at: float = 0.0        # 上次审查时间戳

        # 主动说话监控（不拦截，仅观察）— Phase 0 of memory rewrite
        # 这些字段在 Phase 1 之后由 WorldState 通过 view 暴露给 [STATE] 段
        self._self_speech_count_today: int = 0
        self._self_speech_count_date: date = date.today()
        self._last_self_speech_at: Optional[float] = None
        self._event_source: str = "user"   # 当前事件来源：heartbeat / user

        # 环境事件节流
        self._last_env_audio_env: str = ""
        self._last_env_user_activity: str = ""
        self._last_env_trigger_time: float = 0.0

        # 身份识别
        self._last_voice_embedding = None   # 缓存最近语音段的声纹 embedding

    def set_camera(self, camera):
        """注入 CameraCapture 实例，启用 take_photo 工具"""
        self._camera = camera

    # ── 公共接口 ──────────────────────────────────────────────────────────────

    async def on_speech(self, text: str):
        """
        由 ContinuousListener 在检测到完整语音段后调用。
        已在 asyncio 事件循环中（通过 run_coroutine_threadsafe 调度）。
        """
        importance = 7
        if self._awaiting_reply_until is not None:
            if time.time() < self._awaiting_reply_until:
                importance = 9   # 视为对小Q 提问的回答，优先级提升
            self._awaiting_reply_until = None   # 无论是否超时都清除

        self._mem.add("heard", text, importance=importance)
        self._last_activity  = time.time()
        self._speech_pending.set()   # 通知正在运行的 _think 尽快退出

        try:
            self._event_queue.put_nowait(HeardSpeech(text))
        except asyncio.QueueFull:
            logger.debug("事件队列满，heard 已写入记忆，不触发 think")

    def _record_proactive_speech(self) -> None:
        """speak/ask 工具被 heartbeat/environment 触发时调用。

        只记录、只观察，不阻止任何行为。
        Phase 1 之后这些字段由 WorldState 通过 view 暴露给 [STATE] 段，
        让 LLM 看到数字自己判断是否要继续说。
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

    # ── Phase 3：consolidate 触发 ────────────────────────────────────────
    def _maybe_rollover_today_narrative(self) -> None:
        """检测跨日。若进了新一天，把昨天的 narrative 转存为 daily_reflection fact，
        并清空内存中的当天叙事。"""
        today = date.today()
        if today == self._today_narrative_date:
            return
        prev_text = (self._today_narrative or "").strip()
        prev_date = self._today_narrative_date
        if prev_text:
            try:
                self._facts.upsert(
                    kind="daily_reflection",
                    key=f"day:{prev_date.isoformat()}",
                    value=prev_text,
                )
                logger.info("📖 跨日：%s 的当天叙事已转存为 daily_reflection fact", prev_date)
            except Exception as exc:
                logger.warning("跨日转存 daily_reflection 失败: %s", exc)
        self._today_narrative = None
        self._today_narrative_at = 0.0
        self._today_narrative_date = today

    async def _maybe_refresh_today_narrative(self) -> None:
        """新 heard/said ≥5 AND 距上次 ≥10 分钟 时刷新当天叙事。

        冷启动：第一条 heard/said 进来后立即触发一次。
        在 _process_event(TimerTick) 入口调用（fire-and-forget），避免污染语音热路径。

        注意：events_since_last_refresh 只用于 gate 判定（"距上次 narrative 以来
        有多少新证据"），today_events 用于喂 LLM（"今天全部事件 + prev_summary"）。
        两个窗口语义不同，不能共用一个变量。
        """
        events_since_last_refresh = self._mem.events_since(self._today_narrative_at)
        if not events_since_last_refresh:
            return
        cold_start = (self._today_narrative_at == 0.0)
        if not cold_start:
            if len(events_since_last_refresh) < 5:
                return
            if (time.time() - self._today_narrative_at) < 600:
                return

        try:
            today_start = datetime.combine(date.today(), datetime.min.time()).timestamp()
            today_events = self._mem.events_since(today_start)
            new_text = await asyncio.wait_for(
                _today_narrative_fn(
                    self._llm,
                    events_today=today_events,
                    prev_summary=self._today_narrative,
                ),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            logger.warning("today_narrative 超时")
            return
        except Exception as exc:
            logger.warning("today_narrative 失败: %s", exc)
            return
        if new_text:
            self._today_narrative = new_text
            self._today_narrative_at = time.time()
            logger.info("📖 当天叙事刷新（%d 字）", len(new_text))

    async def _maybe_extract_facts(self) -> None:
        """新 heard/said ≥10 OR 距上次 ≥1 小时 时触发抽取（异步路径）。

        gate 判定用全量 new_events（"有多少新证据"决定要不要跑 LLM），
        喂 LLM 时用 [-30:] cap（"喂多少给 LLM"防冷启动 token 爆炸：
        首轮 _extract_facts_at=0.0 时 events_since 会返回全流）。
        """
        new_events = self._mem.events_since(self._extract_facts_at)
        if not new_events:
            return
        if len(new_events) < 10 and (time.time() - self._extract_facts_at) < 3600:
            return

        try:
            candidates = await asyncio.wait_for(
                _extract_facts(self._llm, new_events[-30:], self._facts),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            logger.warning("extract_facts 超时")
            return
        except Exception as exc:
            logger.warning("extract_facts 失败: %s", exc)
            return
        self._extract_facts_at = time.time()
        if candidates:
            promoted = self._pending_facts.consider(candidates, self._facts)
            if promoted:
                logger.info(
                    "✅ extract_facts: %d 条候选，%d 条经独立 session 二次确认 promote",
                    len(candidates), len(promoted),
                )
        # gc 过期 pending（单次孤立候选最多保留 7 天），
        # 防止边缘候选无限累积 + 一周后被错误 promote
        purged = self._pending_facts.gc()
        if purged:
            logger.info("🧹 清理 %d 条过期 pending fact", purged)

    # ── Background Review：后台审查对话，主动提取长期记忆 ──────────────────────

    _REVIEW_SYSTEM_PROMPT = (
        "你是小Q 的记忆回顾助手。审查以下对话，找出值得永久记住的信息。\n\n"
        "主动保存（不需要用户说'记住'）：\n"
        "- 用户分享了偏好（'我喜欢打篮球'、'我不吃辣'）\n"
        "- 用户提到了具体的人（家人、朋友、同事的名字和关系）\n"
        "- 用户透露了经历（'去年去了日本'、'大学学的吉他'）\n"
        "- 用户表达了兴趣细节（'我是打前锋的，喜欢投三分球'）\n"
        "- 用户纠正了小Q 或明确说'记住'\n\n"
        "优先级：用户偏好 > 人物关系 > 经历故事 > 知识观点\n\n"
        "不要保存：纯寒暄、临时状态（'今天好累'）、小Q 自己的行为\n"
        "⚠️ 只保存用户明确表达的信息。不要从单次行为推断偏好"
        "（'在喝橙汁' ≠ '喜欢橙汁'）。\n\n"
        "输出 JSON 数组，每个元素：\n"
        '  {"type": "ltm", "category": "...", "title": "...", '
        '"content": "...", "tags": [...]}\n'
        "  或\n"
        '  {"type": "fact", "kind": "...", "key": "...", "value": "..."}\n\n'
        "没有值得保存的信息则返回 []。\n"
        "严格输出 JSON 数组，不要任何前后缀，不要 markdown。"
    )

    async def _maybe_background_review(self) -> None:
        """后台审查对话，主动提取长期记忆到 LongTermMemory / FactStore。

        触发条件：≥3 次有效互动，且距上次审查 ≥5 分钟。
        使用独立 LLM 客户端，不阻塞主对话。
        """
        if self._review_llm is None or self._longterm is None:
            return
        if self._review_engagement_count < 3:
            return
        if time.time() - self._last_review_at < 300:  # 5 分钟节流
            return

        # 收集上次审查后的 heard/said 事件
        events = self._mem.events_since(self._last_review_at)
        dialogue_events = [e for e in events if e.type in ("heard", "said")]
        if len(dialogue_events) < 2:
            return

        dialogue_text = _events_to_dialogue(dialogue_events)

        # 附加现有记忆摘要，避免重复保存
        existing_ltm = self._longterm.categories_summary() or "（暂无）"
        existing_facts = _format_existing_facts(self._facts)

        user_msg = (
            f"以下是最近的对话：\n\n{dialogue_text}\n\n"
            f"现有长期记忆：{existing_ltm}\n"
            f"现有结构化事实：\n{existing_facts}\n\n"
            "请提取值得保存的新信息。已存在的不要重复。"
        )

        try:
            import json as _json
            resp = await asyncio.wait_for(
                self._review_llm.chat(
                    user_message=user_msg,
                    system_prompt=self._REVIEW_SYSTEM_PROMPT,
                    max_tokens=2048,
                ),
                timeout=30.0,
            )
            text = (resp.text or "").strip()
            # 剥掉可能的 ```json``` 包裹
            if text.startswith("```"):
                text = text.split("\n", 1)[-1]
                if text.endswith("```"):
                    text = text[:-3]
                text = text.strip()
            items = _json.loads(text)
            if not isinstance(items, list):
                items = []
        except asyncio.TimeoutError:
            logger.warning("background_review 超时")
            return
        except Exception as exc:
            logger.warning("background_review 失败: %s", exc)
            return

        # 重置计数器（无论是否提取到内容）
        self._review_engagement_count = 0
        self._last_review_at = time.time()

        if not items:
            logger.info("💾 Background Review: 无新记忆")
            return

        saved_count = 0
        for item in items:
            try:
                if item.get("type") == "ltm":
                    await self._longterm.save(
                        category=item["category"],
                        title=item["title"],
                        content=item["content"],
                        tags=item.get("tags", []),
                        source="background_review",
                    )
                    saved_count += 1
                    logger.info(
                        "💾 Review 保存长期记忆: [%s] %s",
                        item["category"], item["title"],
                    )
                elif item.get("type") == "fact":
                    self._facts.upsert(
                        kind=item["kind"],
                        key=item["key"],
                        value=item["value"],
                    )
                    saved_count += 1
                    logger.info(
                        "💾 Review 保存事实: %s/%s=%s",
                        item["kind"], item["key"], item["value"],
                    )
            except Exception as exc:
                logger.warning("background_review 保存失败: %s — %s", item, exc)

        logger.info("💾 Background Review 完成: 保存了 %d 条记忆", saved_count)

    async def shutdown(self) -> None:
        """关机前强制执行一次 Background Review，防止短会话记忆丢失。"""
        if self._review_llm is None or self._longterm is None:
            return

        # 跳过节流检查，只要有未审查的对话就执行
        events = self._mem.events_since(self._last_review_at)
        dialogue_events = [e for e in events if e.type in ("heard", "said")]
        if len(dialogue_events) < 2:
            logger.info("🛑 shutdown: 无需 Review（对话不足 2 条）")
            return

        logger.info("🛑 shutdown: 强制执行 Background Review（%d 条未审查对话）",
                     len(dialogue_events))
        # 临时清零节流条件，复用已有逻辑
        saved_count = self._review_engagement_count
        saved_time = self._last_review_at
        self._review_engagement_count = 3  # 满足 ≥3 条件
        self._last_review_at = 0           # 满足 ≥5 分钟条件
        try:
            await self._maybe_background_review()
        except Exception as exc:
            logger.warning("🛑 shutdown Review 失败: %s", exc)
        finally:
            # 恢复（虽然要退出了，但保持一致性）
            self._review_engagement_count = saved_count
            self._last_review_at = saved_time

    async def on_audio_event(self, event: AudioEvent):
        """
        由 OmniEar 在收到 AudioEvent 时调用（通过 run_coroutine_threadsafe）。
        语音事件走 HeardSpeech 路径；非语音环境声音走 EnvironmentChange 路径。
        """
        # 有文本的语音，或已识别说话人的无文本语音（如唱歌）
        _has_speech_content = event.is_speech and event.text
        _has_identified_activity = (
            event.is_speech and not event.text
            and event.speaker is not None
            and event.user_activity
        )
        if _has_speech_content or _has_identified_activity:
            # 缓存声纹 embedding 供 register_voice 工具使用
            if event.voice_embedding is not None:
                self._last_voice_embedding = event.voice_embedding

            # 无文本但有 activity 时，合成一条描述作为 text
            effective_text = event.text or f"（{event.speaker}在{event.user_activity}）"

            # ── 门控：speaker + name 判断是否送主 LLM ──
            # 已注册说话人 → 始终送（主 LLM 有上下文判断是否回应）
            # 未注册 + 叫了"小Q" → 送
            # 未注册 + 没叫名字 → 跳过（大概率播客/视频/背景）
            _text_norm = effective_text.lower().replace(" ", "")
            name_mentioned = "小q" in _text_norm

            if event.speaker is None and not name_mentioned:
                logger.info("🔇 未知说话人且未叫名字，跳过: %s", effective_text[:60])
                return

            # 记忆延迟写入：只有 _think 期间调用了 speak/ask 才写入 heard 记忆
            # 避免 wait 决策（"不是对我说的"）后心跳看到 HEA 条目再次搭话
            importance = 9 if self._awaiting_reply_until else 7
            if self._awaiting_reply_until:
                self._awaiting_reply_until = None

            self._last_activity = time.time()
            self._speech_pending.set()

            try:
                self._event_queue.put_nowait(HeardSpeech(
                    text=effective_text,
                    emotion=event.emotion,
                    intent=event.intent,
                    directed=event.directed,
                    audio_env=event.audio_env,
                    user_activity=event.user_activity,
                    speaker=event.speaker,
                    voice_embedding=event.voice_embedding,
                    name_mentioned=name_mentioned,
                    importance=importance,
                ))
            except asyncio.QueueFull:
                logger.debug("事件队列满，丢弃")

        elif ENV_SOUND_TRIGGER and not event.is_speech and event.audio_env:
            # ── 环境声音路径 ──

            # (1) TTS 回声过滤：机器人自己在说话时忽略
            if self._tts.is_playing():
                return

            # (2) 常规声音过滤：打字、安静等日常声音不触发
            #     但如果 activity 有意义（如咳嗽），仍然放行
            activity = (event.user_activity or "").strip()
            has_notable_activity = activity and activity not in _ROUTINE_ACTIVITY_KEYWORDS
            if (any(kw in event.audio_env for kw in _ROUTINE_ENV_KEYWORDS)
                    and not has_notable_activity):
                return

            # (3) 节流 + 去重
            now = time.time()
            if now - self._last_env_trigger_time < ENV_TRIGGER_MIN_INTERVAL:
                return
            if (event.audio_env == self._last_env_audio_env
                    and event.user_activity == self._last_env_user_activity):
                return

            # 通过过滤，更新状态并入队
            self._last_env_audio_env = event.audio_env
            self._last_env_user_activity = event.user_activity
            self._last_env_trigger_time = now

            self._mem.add("heard", f"[环境] {event.audio_env}", importance=4)
            logger.info("🔔 环境事件触发: env=%r activity=%r", event.audio_env, event.user_activity)

            try:
                self._event_queue.put_nowait(EnvironmentChange(
                    audio_env=event.audio_env,
                    user_activity=event.user_activity,
                ))
            except asyncio.QueueFull:
                pass

    async def _on_reminder_fired(self, reminder_id: str, text: str) -> None:
        """ReminderService 到点的回调，推事件进主队列。"""
        try:
            self._event_queue.put_nowait(ReminderFired(
                reminder_id=reminder_id,
                text=text,
            ))
        except asyncio.QueueFull:
            logger.warning("reminder %s 触发时事件队列已满，丢弃", reminder_id)

    async def run(self):
        """启动自主运行（永不返回，Ctrl-C 退出）"""
        logger.info("🧠 SoulAgent 启动")
        await self._reminder_svc.start()
        asyncio.create_task(self._timer_loop(),    name="soul-timer")
        asyncio.create_task(self._event_consumer(), name="soul-consumer")
        await asyncio.Event().wait()   # 永久等待，直到外部取消

    # ── 内部：事件循环 ────────────────────────────────────────────────────────

    def _is_idle(self) -> bool:
        """真正空闲：队列空 + 不在动作中 + 不在说话"""
        if not self._event_queue.empty():
            return False
        if self._motion_agent.is_playing():
            return False
        if self._tts.is_playing():
            return False
        return True

    def _heartbeat_interval(self) -> float:
        """根据连续空看次数动态调整心跳间隔。

        0-2 次没看到人 → 30s（正常频率）
        3-5 次           → 60s
        6+ 次            → 120s
        """
        n = self._consecutive_empty_looks
        if n >= 6:
            return 120.0
        if n >= 3:
            return 60.0
        return TIMER_INTERVAL

    async def _timer_loop(self):
        """动态间隔心跳：连续看不到人时自动降频，有语音事件时恢复"""
        while True:
            await asyncio.sleep(self._heartbeat_interval())
            if self._is_idle():
                try:
                    self._event_queue.put_nowait(TimerTick())
                except asyncio.QueueFull:
                    pass

    async def _event_consumer(self):
        """串行消费事件队列，保证同一时刻只有一个 think 在运行"""
        while True:
            event = await self._event_queue.get()
            try:
                await self._process_event(event)
            except Exception as exc:
                logger.error("处理事件异常: %s", exc, exc_info=True)
            finally:
                self._event_queue.task_done()

    async def _process_event(self, event):
        self._current_confirmed_speaker = None  # 默认无确认身份
        if isinstance(event, TimerTick):
            self._ticks_since_photo += 1
            self._event_source = "heartbeat"

            # Phase 3: 跨日 hook + 当天叙事刷新
            # narrative 刷新 fire-and-forget，不阻塞心跳主循环：
            #   - 60s LLM 调用 await 在这里会让串行 event consumer 卡死，HeardSpeech 进不来
            #   - 本次 _think 看到的 _today_narrative 还是上一次的旧值（滚动摘要本意，接受）
            self._maybe_rollover_today_narrative()
            asyncio.create_task(
                self._maybe_refresh_today_narrative(),
                name="soul-narrative",
            )

            trigger = "心跳触发。参考 [SCENE] 找到用户通常的方位，转向那里观察用户状态再决定行动。"

        elif isinstance(event, ReminderFired):
            self._event_source = "reminder"
            trigger = f"提醒到时：{event.text}"

        elif isinstance(event, EnvironmentChange):
            self._event_source = "environment"
            # 被唤醒的动作 — 让机器人看起来"注意到了什么"
            self._motion_agent.play_emotion("curious")
            trigger_parts = ["你感知到环境变化。"]
            if event.audio_env:
                trigger_parts.append(f"环境音：{event.audio_env}")
            if event.user_activity and event.user_activity != "未知":
                trigger_parts.append(f"用户行为：{event.user_activity}")
            trigger = "\n".join(trigger_parts)
        else:  # HeardSpeech
            self._speech_pending.clear()
            self._event_source = "user"
            self._current_confirmed_speaker = event.speaker  # 声纹确认的身份（或 None）
            # 用户说话 → 重置心跳降频计数，恢复正常感知频率
            if self._consecutive_empty_looks > 0:
                logger.info("💤→🔔 用户说话，心跳恢复正常频率（之前连续 %d 次空看）",
                            self._consecutive_empty_looks)
                self._consecutive_empty_looks = 0
            # 延迟写入：记录待写的 heard，_think 后根据是否 spoke 决定
            self._spoke_this_think = False
            self._pending_heard = [(event.text, event.importance, time.time())]
            if event.speaker:
                trigger_parts = [f"你听到 {event.speaker} 说：「{event.text}」"]
            else:
                trigger_parts = [f"你听到一个未识别的声音说：「{event.text}」（声纹未匹配，不要猜测身份）"]
            if event.emotion and event.emotion != "neutral":
                trigger_parts.append(f"说话者情绪：{event.emotion}")
            if event.user_activity and event.user_activity != "未知":
                trigger_parts.append(f"用户正在：{event.user_activity}")
            if event.audio_env:
                trigger_parts.append(f"环境：{event.audio_env}")

            # 根据 name_mentioned + speaker 引导主 LLM 判断是否回应
            if event.name_mentioned:
                trigger_parts.append(
                    "对方叫了你的名字。请先用 speak 回应。"
                    "然后用 body_move 转向用户方向（参考场景记忆）+ look。"
                    "如果画面里没看到人，换其他方向继续找（左、正前、右都试试）。"
                    "找到了就 update_scene_memory 更新位置；找不到也没关系。"
                )
            elif event.speaker is not None:
                # 已注册说话人，主 LLM 基于上下文判断
                trigger_parts.append(
                    "判断这段话是否是对你说的。"
                    "参考 [RECENT] 里你最近说了什么、问了什么——"
                    "如果是在回答你的问题或对你说话，用 speak 回应；"
                    "如果像是自言自语、与他人交谈、或视频内容，用 wait 安静观察。"
                    "回应后如果 look 没看到人，换其他方向找找；找不到也没关系。"
                )
            else:
                # 未注册但叫了名字（通过门控的唯一可能）
                trigger_parts.append(
                    "对方叫了你的名字。请先用 speak 回应。"
                )
            trigger = "\n".join(trigger_parts)
            self._last_activity = time.time()   # 只有语音才算真实活动

        await self._think(trigger)

        # 延迟写入 heard 记忆：只有 speak/ask 被调用才写入
        if self._spoke_this_think and self._pending_heard:
            for text, imp, ts in self._pending_heard:
                self._mem.add("heard", text, importance=imp, timestamp=ts)
            logger.info("📝 wrote %d heard entries to memory", len(self._pending_heard))
        elif self._pending_heard:
            logger.info("🔇 wait 决策，跳过 %d 条 heard 记忆写入", len(self._pending_heard))
        self._pending_heard = []

        # 异步触发 compact（不等待结果，不阻塞主循环）
        if self._mem.should_compact():
            asyncio.create_task(
                self._mem.compact_if_needed(self._llm, longterm=self._longterm),
                name="soul-compact",
            )

        # Phase 3: 异步触发 fact 抽取（不等待结果，不阻塞主循环）
        asyncio.create_task(
            self._maybe_extract_facts(),
            name="soul-extract-facts",
        )

        # Background Review：有效互动计数 + 触发后台记忆审查
        if self._spoke_this_think:
            self._review_engagement_count += 1
        if self._review_llm:
            asyncio.create_task(
                self._maybe_background_review(),
                name="soul-review",
            )

    # ── 内部：认知决策（ReAct 循环） ─────────────────────────────────────────

    async def _think(self, trigger: str, image_path: Optional[str] = None,
                     max_steps: int = 15):
        """ReAct 循环：读记忆 → LLM → 执行工具 → 观察 → 继续，直到 LLM 停止"""
        if self._longterm:
            summary = self._longterm.categories_summary()
            ltm_hint = summary if summary else "（暂无长期记忆）"
        else:
            ltm_hint = None

        # 收集已知人名：无确认身份时用于脱敏 [RECENT] 和 [FACTS]
        confirmed = getattr(self, "_current_confirmed_speaker", None)
        known_names: set[str] = set()
        for ident in self._identity_memory.list_identities():
            known_names.add(ident["name"])
        facts_store = getattr(self, "_facts", None)
        if facts_store:
            for fact in facts_store.list_by_kind("calling"):
                known_names.add(fact.value)

        initial_text = render_context_packet(
            state=self._state,
            episodic=self._mem,
            scene=self._scene_memory,
            facts=facts_store,
            today=getattr(self, "_today_narrative", None),  # Phase 3 起非空
            ltm_hint=ltm_hint,
            trigger=trigger,
            confirmed_speaker=confirmed,
            known_names=known_names or None,
        )

        # 构建初始消息列表
        # MOTION_EXAMPLES 拼在 PERSONALITY_PROMPT 之后，作为 compose_motion 的 few-shot
        # 整段固定内容，会被 prompt cache 缓存
        messages: list[dict] = [
            {"role": "system", "content": PERSONALITY_PROMPT + "\n" + MOTION_EXAMPLES}
        ]
        if image_path:
            img_data = self._llm._encode_image(image_path)
            if img_data:
                messages.append({
                    "role": "user",
                    "content": [
                        {"type": "text", "text": initial_text},
                        {"type": "image_url", "image_url": {"url": img_data}},
                    ]
                })
            else:
                messages.append({"role": "user", "content": initial_text})
        else:
            messages.append({"role": "user", "content": initial_text})

        for step in range(max_steps):
            if self._speech_pending.is_set():
                self._speech_pending.clear()
                # 心跳/环境 think 优先级低，直接让出给语音事件走正常队列
                if self._event_source in ("heartbeat", "environment"):
                    logger.info("🧠 ReAct step=%d 心跳让出：语音事件到达，退出当前 think", step)
                    break
                # 用户语音 think 期间收到新语音 → 注入当前 ReAct 循环
                injected: list[HeardSpeech] = []
                while not self._event_queue.empty():
                    try:
                        ev = self._event_queue.get_nowait()
                        if isinstance(ev, HeardSpeech):
                            injected.append(ev)
                        # TimerTick 直接丢弃
                    except asyncio.QueueEmpty:
                        break
                if injected:
                    # 注入的事件也加入延迟写入队列
                    for ev in injected:
                        self._pending_heard.append((ev.text, ev.importance, time.time()))
                    parts = []
                    for ev in injected:
                        if ev.speaker:
                            line = f"你听到 {ev.speaker} 说：「{ev.text}」"
                        else:
                            line = f"你听到一个未识别的声音说：「{ev.text}」"
                        extras = []
                        if ev.emotion and ev.emotion != "neutral":
                            extras.append(f"情绪: {ev.emotion}")
                        if ev.user_activity and ev.user_activity != "未知":
                            extras.append(f"行为: {ev.user_activity}")
                        if extras:
                            line += f"（{'，'.join(extras)}）"
                        parts.append(line)
                    # 根据 name_mentioned + speaker 引导主 LLM 判断
                    any_name = any(ev.name_mentioned for ev in injected)
                    any_known = any(ev.speaker is not None for ev in injected)
                    if any_name:
                        inject_text = "\n".join(parts) + "\n对方叫了你的名字，请先用 speak 回应。"
                    elif any_known:
                        inject_text = "\n".join(parts) + (
                            "\n判断这段话是否是对你说的。"
                            "如果是在回应你或对你提问，用 speak 回应；否则用 wait 安静观察。"
                        )
                    else:
                        inject_text = "\n".join(parts) + "\n对方叫了你的名字，请先用 speak 回应。"
                    messages.append({"role": "user", "content": inject_text})
                    logger.info("🧠 ReAct step=%d 注入语音: %s", step, inject_text)

            try:
                resp = await asyncio.wait_for(
                    self._llm.chat_messages(messages, tools=SOUL_TOOLS),
                    timeout=30.0,
                )
            except asyncio.TimeoutError:
                logger.warning("🧠 ReAct step=%d LLM 请求超时(30s)，退出", step)
                break
            except Exception as exc:
                # Phase 3: 异常诊断只走日志，不再写入 episodic 流污染叙事。
                logger.warning("LLM 调用失败(step=%d): %s — 脑袋里一片空白", step, exc)
                self._motion_agent.play_emotion("nod")
                return

            if resp.text:
                logger.info("💭 [step=%d] LLM 思考: %s", step, resp.text.strip())

            if resp.stop_reason != "tool_use" or not resp.tool_calls:
                logger.debug("🧠 ReAct 结束 step=%d  stop=%s", step, resp.stop_reason)
                break

            # 按依赖关系排序：输出→动作→观察→依赖观察的工具
            _TOOL_EXEC_ORDER = {
                "speak": 0,
                "express_emotion": 1, "compose_motion": 1, "body_move": 1,
                "set_light_mood": 1, "set_rgb_solid": 1,
                "look": 2, "recall_memory": 2, "session_search": 2,
                "register_voice": 3, "update_scene_memory": 3,
                "forget_fact": 3,
                "set_reminder": 3, "cancel_reminder": 3, "list_reminders": 3,
                "wait": 9,
            }
            sorted_calls = sorted(
                resp.tool_calls,
                key=lambda tc: _TOOL_EXEC_ORDER.get(tc.get("name", ""), 5),
            )

            # 执行本轮所有工具，收集结果
            # wait 排序在最后（priority=9），执行到 wait 后直接退出内层循环
            tool_results: list[dict] = []
            observation_image: Optional[str] = None
            called_wait = False

            for tc in sorted_calls:
                if tc.get("name") == "wait":
                    # wait 始终最后执行（sort order=9），先记录再退出内循环
                    await self._execute_tool(tc)
                    called_wait = True
                    break
                result_text, snap = await self._execute_tool(tc)
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_text,
                })
                if snap:
                    observation_image = snap

            # wait = LLM 主动表达"我想停了"，直接退出外层 ReAct 循环
            if called_wait:
                logger.info("🧠 ReAct 因 wait 退出 step=%d", step)
                break

            # 把工具结果反馈给 LLM，继续循环
            messages.append(resp.raw_assistant_message)
            messages.extend(tool_results)

            if observation_image:
                img_data = self._llm._encode_image(observation_image)
                if img_data:
                    messages.append({
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "这是你刚才看到的画面，继续决策（果断选择下一步，没事做就 wait）："},
                            {"type": "image_url", "image_url": {"url": img_data}},
                        ]
                    })
                else:
                    messages.append({"role": "user", "content": "继续决策（果断选择下一步，没事做就 wait）："})
            else:
                messages.append({"role": "user", "content": "继续决策（果断选择下一步，没事做就 wait）："})
        else:
            logger.warning("⚠️  ReAct 达到最大步数 %d，强制退出", max_steps)

    async def _execute_tool(self, tool_call: dict) -> tuple[str, Optional[str]]:
        """执行单个工具，返回 (结果描述, 观察图片路径或None)"""
        name = tool_call.get("name", "")
        args = tool_call.get("input", {})
        logger.info("🔧 工具调用: %s  args=%s", name, args)

        if name == "express_emotion":
            ename     = (args.get("name") or "nod").strip()
            intensity = float(args.get("intensity", 1.0))
            if ename not in MOTION_REGISTRY:
                return (
                    f"express_emotion 失败：'{ename}' 不存在。"
                    f"可用动作：{sorted(MOTION_REGISTRY.keys())}",
                    None,
                )
            self._motion_agent.play_emotion(ename, intensity=intensity)
            return f"情绪动作：{ename} (×{intensity:.1f})", None

        elif name == "compose_motion":
            intent   = (args.get("intent") or "").strip()
            segments = args.get("segments") or []
            if not intent:
                return "compose_motion 失败：intent 不能为空（请用一句中文描述动作意图）", None
            err = self._motion_agent.play_compose(intent, segments)
            if err:
                return (
                    f"compose_motion 失败：{err}。请修正 segments 后重试。",
                    None,
                )
            return f"自定义动作：{intent}（已是完整表演，无需追加动作）", None

        elif name == "speak":
            text = (args.get("text") or "").strip()
            # LLM emotion → 豆包 TTS 支持的 emotion 映射
            _EMOTION_MAP = {
                "happy": "happy",
                "sad": "sad",
                "angry": "angry",
                "gentle": "gentle",
                "surprise": "surprised",
                "neutral": "neutral",
            }
            raw_emotion = (args.get("emotion") or "").strip()
            emotion = _EMOTION_MAP.get(raw_emotion) if raw_emotion else None
            if text:
                await self._tts.speak(text, emotion=emotion)
                self._mem.add("said", text)
                self._spoke_this_think = True
                if self._event_source in ("heartbeat", "environment"):
                    self._record_proactive_speech()
                # 心跳时主动说话 → 说明看到了人，重置空看计数
                if self._event_source == "heartbeat" and self._consecutive_empty_looks > 0:
                    self._consecutive_empty_looks = 0
                return f"已说：{text}", None
            return "speak: 文本为空", None

        elif name == "ask":
            question = (args.get("question") or "").strip()
            if question:
                await self._tts.speak(question)
                self._mem.add("said", question)
                self._spoke_this_think = True
                if self._event_source in ("heartbeat", "environment"):
                    self._record_proactive_speech()
                self._awaiting_reply_until = time.time() + 15.0
                logger.info("❓ 小Q 提问，15 秒内的回答视为高优先级")
                return f"已提问：{question}", None
            return "ask: 问题为空", None

        elif name == "set_rgb_solid":
            r, g, b = args.get("red", 0), args.get("green", 0), args.get("blue", 0)
            self._rgb_svc.dispatch("solid", (r, g, b))
            return f"灯光已设为 RGB({r},{g},{b})", None

        elif name == "body_move":
            _JOINT_KEYS = {"base_yaw", "base_pitch", "elbow_pitch", "wrist_roll", "wrist_pitch"}
            joints   = {k: float(v) for k, v in args.items() if k in _JOINT_KEYS and v is not None}
            duration = float(args.get("duration_sec", 1.0))
            if joints:
                self._motion_agent.play_waypoint(joints, duration)
                current = self._motion_agent._get_current_pos()
                pos_str = ", ".join(f"{k}={v:.0f}" for k, v in current.items())
                return (
                    f"动作已入队（{duration:.1f}s），正在执行中。当前位置：{pos_str}。"
                    f"此操作不返回任何画面，如需观察请在下一步调用 look。"
                ), None
            return "body_move: 无有效关节", None

        elif name == "look":
            await self._motion_agent.wait_done(timeout=6.0)
            await asyncio.sleep(0.2)   # 等舵机物理到位，避免拍到运动中的画面
            if self._camera is not None:
                snap = self._camera.take_snapshot()
                if snap:
                    self._ticks_since_photo = 0
                    current = self._motion_agent._get_current_pos()
                    pos_str = ", ".join(f"{k}={v:.0f}" for k, v in current.items())

                    result_text = f"已获取画面（当前关节：{pos_str}）"
                    logger.info("📸 look: %s", snap)
                    return result_text, snap
                logger.warning("📸 look: take_snapshot 返回 None（相机无帧）")
            else:
                logger.warning("📸 look: 相机未注入（_camera is None）")
            return "摄像头不可用", None

        elif name == "update_scene_memory":
            content = (args.get("content") or "").strip()
            if content:
                self._scene_memory.write(content)
                return "场景记忆已更新", None
            return "内容为空", None

        elif name == "set_light_mood":
            mood = (args.get("mood") or "warm_focus").strip()
            color = _MOOD_MAP.get(mood, _MOOD_MAP["warm_focus"])
            # 取消正在进行的渐变
            if self._light_task and not self._light_task.done():
                self._light_task.cancel()
            self._light_task = asyncio.create_task(
                self._transition_light(color), name="light-transition"
            )
            return f"灯光氛围：{mood}", None

        elif name == "register_voice":
            person_name = (args.get("name") or "").strip()
            if not person_name:
                return "请提供名字", None
            if self._last_voice_embedding is None:
                return "没有可用的声纹数据，需要先听到语音", None
            count = self._identity_memory.register_voice(person_name, self._last_voice_embedding)
            # Phase 2: 同步写一条 identity fact，让声纹身份进入 [FACTS] 段
            try:
                self._facts.upsert(
                    kind="identity",
                    key=f"voice:{person_name}",
                    value=f"{person_name}（声纹注册，{count} 条样本）",
                )
            except Exception as exc:
                logger.warning("写入 identity fact 失败: %s", exc)
            return f"已记住 {person_name} 的声音！以后听到就能认出来了", None

        elif name == "wait":
            reason = (args.get("reason") or "").strip()
            logger.info("⏸️  wait: %s", reason or "（无原因）")
            # 心跳触发 wait → 视为"没找到人"，累加空看计数
            if self._event_source == "heartbeat":
                self._consecutive_empty_looks += 1
                if self._consecutive_empty_looks == 3:
                    logger.info("💤 连续 %d 次心跳没看到人，降频到 60s",
                                self._consecutive_empty_looks)
                elif self._consecutive_empty_looks == 6:
                    logger.info("💤 连续 %d 次心跳没看到人，降频到 120s",
                                self._consecutive_empty_looks)
            return f"保持观察：{reason}", None

        elif name == "forget_fact":
            kind = (args.get("kind") or "").strip()
            key  = (args.get("key") or "").strip()
            if not (kind and key):
                return "kind/key 必填", None
            ok = self._facts.forget(kind=kind, key=key)
            if ok:
                logger.info("🗑 forget_fact: %s/%s", kind, key)
                self._mem.add("action", f"[已删除] {kind}.{key}")
                return f"已删除 {kind}.{key}", None
            return f"未找到 {kind}.{key}", None

        elif name == "recall_memory":
            query = (args.get("query") or "").strip()
            category = (args.get("category") or "").strip() or None
            if not query:
                return "query 必填", None
            if self._longterm is None:
                return "长期记忆未启用", None
            results = await self._longterm.search(query, category=category, limit=5)
            if not results:
                return "没有找到相关的长期记忆", None
            from .memory.longterm import CATEGORIES
            lines = []
            for r in results:
                cat_label = CATEGORIES.get(r.category, r.category)
                lines.append(f"[{cat_label}] {r.title}: {r.content}")
            return "\n".join(lines), None

        elif name == "session_search":
            query = (args.get("query") or "").strip()
            if not query:
                return "query 必填", None
            if self._history_db is None:
                return "会话历史未启用", None
            days_back = args.get("days_back") or 7
            results = self._history_db.search(query, days_back=int(days_back))
            if not results:
                return "没有找到相关的对话历史", None
            lines = []
            for seg in results:
                lines.append(f"--- {seg['time_range']} ---")
                for m in seg["matches"]:
                    prefix = "用户" if m["type"] == "heard" else "小Q"
                    marker = " ★" if m.get("is_match") else ""
                    lines.append(f"  [{m['time']}] {prefix}: {m['content']}{marker}")
            return "\n".join(lines), None

        elif name == "set_reminder":
            text = (args.get("text") or "").strip()
            if not text:
                return "text 必填", None
            delay = args.get("delay_seconds")
            at_time_str = (args.get("at_time") or "").strip() or None
            at_ts = None
            if at_time_str:
                try:
                    at_ts = datetime.fromisoformat(at_time_str).timestamp()
                except ValueError as exc:
                    return f"at_time 解析失败（需要 ISO 8601）: {exc}", None
            if delay is not None:
                delay = float(delay)
            try:
                r = self._reminder_svc.add(
                    text=text, delay_seconds=delay, at_time=at_ts,
                )
            except ValueError as exc:
                return str(exc), None
            from lelamp.soul.reminder import _fmt_ts
            result = f"已设提醒：{text}（id={r.id}, 触发于 {_fmt_ts(r.due_at)}）"
            self._mem.add("action", f"[已设提醒] {r.text}（{r.id}）")
            return result, None

        elif name == "cancel_reminder":
            rid = (args.get("reminder_id") or "").strip()
            text_kw = (args.get("text") or "").strip()

            # 路径 1：精确 ID
            if rid and self._reminder_svc.cancel(rid):
                self._mem.add("action", f"[已取消提醒] {rid}")
                return f"已取消提醒 {rid}", None

            # 路径 2：text 关键词搜索
            if text_kw:
                matches = self._reminder_svc.find_by_text(text_kw)
                if len(matches) == 0:
                    return f"没有包含「{text_kw}」的提醒", None
                if len(matches) == 1:
                    r = matches[0]
                    self._reminder_svc.cancel(r.id)
                    self._mem.add("action", f"[已取消提醒] {r.text}（{r.id}）")
                    return f"已取消提醒：{r.text}（{r.id}）", None
                # 多条匹配 → 列出让 LLM 选
                from lelamp.soul.reminder import _fmt_ts
                lines = [f"有 {len(matches)} 条匹配「{text_kw}」，请指定 reminder_id："]
                for r in matches:
                    lines.append(f"- {r.id}: {r.text} @ {_fmt_ts(r.due_at)}")
                return "\n".join(lines), None

            if rid:
                return f"提醒 {rid} 不存在或已触发", None
            return "请提供 reminder_id 或 text", None

        elif name == "list_reminders":
            within_min = args.get("within_minutes")
            within_sec = float(within_min) * 60 if within_min else None
            items = self._reminder_svc.list_active(within_seconds=within_sec)
            if not items:
                return "（没有未到期的提醒）", None
            now = time.time()
            lines = [f"当前有 {len(items)} 条提醒："]
            for r in items:
                delta = r.due_at - now
                if delta < 60:
                    rel = f"{int(delta)} 秒后"
                elif delta < 3600:
                    rel = f"{int(delta / 60)} 分钟后"
                else:
                    rel = f"{delta / 3600:.1f} 小时后"
                from lelamp.soul.reminder import _fmt_ts
                lines.append(f"- {r.id}: {r.text} @ {_fmt_ts(r.due_at)} ({rel})")
            return "\n".join(lines), None

        else:
            logger.warning("未知工具: %s", name)
            return f"未知工具: {name}", None

    # ── 灯光渐变 ──────────────────────────────────────────────────────────────

    async def _transition_light(self, target: tuple, duration: float = 2.0):
        """异步渐变灯光，通过连续 dispatch("solid") 实现"""
        start = self._rgb_svc.current_color
        steps = max(1, int(duration * 15))   # 15fps
        for i in range(1, steps + 1):
            t = i / steps
            r = int(start[0] + (target[0] - start[0]) * t)
            g = int(start[1] + (target[1] - start[1]) * t)
            b = int(start[2] + (target[2] - start[2]) * t)
            self._rgb_svc.dispatch("solid", (r, g, b))
            await asyncio.sleep(1 / 15)
        logger.debug("💡 灯光渐变完成 → RGB%s", target)
