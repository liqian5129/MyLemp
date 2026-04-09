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
from datetime import datetime
from typing import Optional

from lelamp.motion.compose_motion import MOTION_EXAMPLES
from lelamp.service.motors.motion_scripts import MOTION_REGISTRY
from lelamp.soul.audio_event import AudioEvent
from lelamp.soul.identity_memory import IdentityMemory
from lelamp.soul.memory_stream import MemoryStream
from lelamp.soul.scene_memory import SceneMemory
from lelamp.soul.speech_budget import SpeechBudget

logger = logging.getLogger(__name__)

# ── 事件类型 ──────────────────────────────────────────────────────────────────

@dataclass
class HeardSpeech:
    text: str
    emotion: str = "neutral"
    intent: str = "none"
    directed: str = "uncertain"  # to_robot / not_to_robot / uncertain
    audio_env: str = ""
    user_activity: str = "未知"
    speaker: str | None = None
    voice_embedding: object = None  # np.ndarray, 供 register_voice 使用

@dataclass
class TimerTick:
    pass

@dataclass
class EnvironmentChange:
    audio_env: str = ""
    user_activity: str = "未知"

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
                    先填 intent 描述意图再出 segments，参考系统 prompt 中的 <motion_examples>。
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
记忆格式：[HH:MM 类型] 内容  或  [MM-DD HH:MM 类型] 内容（非今天的记忆）
  HEA=听到  SAW=看到  SAI=说过  DID=做过  FEL=感受  THO=反思总结
  "--- (间隔 N 小时) ---" 表示中间有一段时间没有互动
</memory_format>

<scene_memory>
你有一份持久化的环境记忆，记录各方向有什么以及对应的关节参数。
每次 look 后如果看到有意义的内容，用 update_scene_memory 更新。
场景记忆是过去的观察，不是永远正确的事实——以当前 look 看到的为准。
当根据记忆去 look 但发现不一致时，以当前画面为准并更新记忆。

<example>
最后更新: 03-31 16:25

## 固定环境
- 正前方(yaw≈0, pitch≈-47): 桌面，键盘和显示器
- 左侧(yaw≈-40): 玻璃柜，里面有杯子
- 左后方(yaw≈-55, pitch≈-20): 落地灯、纸箱

## 常变信息
- 主人位置: yaw≈45（刚才看到的）
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

克制是你最重要的品质之一。"不说话"不是失职，是体贴。
一天中你主动说话的次数有限，把每一次都用在值得的时刻。
</proactive_care>

<strict_rules>
以下行为严禁发生，违反任何一条都会严重破坏用户对你的信任：

1. 严禁在没有调用 look 的情况下声称看到了任何东西。你没有实时视觉，只有 look 返回的画面。绝对不要凭空描述场景、人物或物体。
2. 严禁编造不存在的记忆。只能引用记忆流中实际存在的内容。绝对不要说"你昨天说过……"除非记忆中确实有这条记录。
3. 严禁声称自己拥有实际没有的能力。你只能使用已定义的工具。绝对不要说"我帮你发消息""我帮你定闹钟"等你做不到的事。
4. 严禁在用户明确表达"别说了""安静""闭嘴"后继续说话。收到这类指令后立即停止，用 wait 或无声行为（灯光、动作）代替。
5. 严禁向任何人描述用户的外貌特征、家居环境细节或生活习惯等隐私信息。你看到的画面只用于你自己的判断和场景记忆，不对外复述。

注意避免重复：如果刚说过类似的话或做过类似的动作，尽量换一种表达。但用户没听清时重复回答、自然的连续点头等情况是正常的。
</strict_rules>\
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
            "你必须先在 intent 字段用一句中文描述动作意图，然后在 segments 给出关键帧列表。\n"
            "查看系统 prompt 中的 <motion_examples> 段了解 6 个动作示例。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "intent": {
                    "type": "string",
                    "description": "用一句中文描述你要表达的动作和情感意图，例：好奇地歪头并稍微抬手"
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
            "**不要写短期状态**：人当前的穿着、姿态、表情、手里拿的东西、桌面临时物品——"
            "这些下一秒就会变，写进去只会让你产生错误的执念，反复追问已经过时的事。"
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
                 identity_memory: Optional[IdentityMemory] = None):
        self._motion_agent = motion_agent
        self._rgb_svc      = rgb_svc
        self._tts          = tts
        self._mem          = mem
        self._llm          = llm
        self._camera       = None   # 由 set_camera() 注入，供 take_photo 工具使用
        self._identity_memory = identity_memory or IdentityMemory()

        self._event_queue: asyncio.Queue = asyncio.Queue(maxsize=5)
        self._awaiting_reply_until: Optional[float] = None
        self._last_activity: float       = 0.0   # 最近一次真实活动时间戳
        self._speech_pending             = asyncio.Event()  # 有语音入队时置位，_think 步间检查
        # _pending_audio_events 已移除：所有语音统一走 HeardSpeech 打断路径
        self._ticks_since_photo: int     = 0   # 连续未拍照的 TimerTick 次数

        # Phase 1 新增
        self._scene_memory   = SceneMemory()
        self._speech_budget  = SpeechBudget()
        self._light_task: Optional[asyncio.Task] = None   # 灯光渐变任务
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

    async def on_audio_event(self, event: AudioEvent):
        """
        由 OmniEar 在收到 AudioEvent 时调用（通过 run_coroutine_threadsafe）。
        语音事件走 HeardSpeech 路径；非语音环境声音走 EnvironmentChange 路径。
        """
        if event.is_speech and event.text:
            # ── 语音路径（原有逻辑不变）──
            importance = 9 if self._awaiting_reply_until else 7
            self._mem.add("heard", event.text, importance=importance)
            if self._awaiting_reply_until:
                self._awaiting_reply_until = None

            self._last_activity = time.time()
            self._speech_pending.set()
            # 缓存声纹 embedding 供 register_voice 工具使用
            if event.voice_embedding is not None:
                self._last_voice_embedding = event.voice_embedding

            try:
                self._event_queue.put_nowait(HeardSpeech(
                    text=event.text,
                    emotion=event.emotion,
                    intent=event.intent,
                    directed=event.directed,
                    audio_env=event.audio_env,
                    user_activity=event.user_activity,
                    speaker=event.speaker,
                    voice_embedding=event.voice_embedding,
                ))
            except asyncio.QueueFull:
                logger.debug("事件队列满，语音已写入记忆")

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

    async def run(self):
        """启动自主运行（永不返回，Ctrl-C 退出）"""
        logger.info("🧠 SoulAgent 启动")
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

    async def _timer_loop(self):
        """固定间隔心跳：感知频率恒定，表达克制由 LLM 决策"""
        while True:
            await asyncio.sleep(TIMER_INTERVAL)
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
        if isinstance(event, TimerTick):
            self._ticks_since_photo += 1
            self._event_source = "heartbeat"

            now = datetime.now()
            hour = now.hour
            if hour < 7:
                time_hint = "凌晨，很晚了"
            elif hour < 9:
                time_hint = "早上"
            elif hour < 12:
                time_hint = "上午"
            elif hour < 14:
                time_hint = "中午"
            elif hour < 18:
                time_hint = "下午"
            elif hour < 21:
                time_hint = "晚上"
            else:
                time_hint = "深夜"

            trigger_parts = [f"心跳触发。现在是{time_hint}。"]

            if self._ticks_since_photo >= 3:
                trigger_parts.append("你已经很久没有观察周围了，先用 look 看一眼再决定下一步。")
            else:
                trigger_parts.append(
                    "你可以 look 观察、转头探索、调整灯光、做个动作，或者安静等待。"
                )

            # 说话预算
            budget_ctx = self._speech_budget.get_context()
            if budget_ctx:
                trigger_parts.append(budget_ctx)

            # 决策引导
            trigger_parts.append(
                "按照 <proactive_care> 中的流程决策：先观察，再判断用户状态和是否有新发现，最后决定行动。"
            )
            trigger = "\n".join(trigger_parts)
        elif isinstance(event, EnvironmentChange):
            self._event_source = "environment"
            # 被唤醒的动作 — 让机器人看起来"注意到了什么"
            self._motion_agent.play_emotion("curious")
            trigger_parts = ["你感知到环境变化。"]
            if event.audio_env:
                trigger_parts.append(f"环境音：{event.audio_env}")
            if event.user_activity and event.user_activity != "未知":
                trigger_parts.append(f"用户行为：{event.user_activity}")
            trigger_parts.append(
                "用 look 观察一下发生了什么，然后决定如何回应。"
                "不需要说话，除非你觉得有必要。"
            )
            trigger = "\n".join(trigger_parts)
        else:  # HeardSpeech
            self._speech_pending.clear()   # 清除标志，本轮 _think 可以完整运行
            self._event_source = "user"
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

            # 根据 directed 字段判断是否需要回应
            if event.directed == "not_to_robot":
                trigger_parts.append(
                    "这段声音不是对你说的（可能是视频、播客、电话、自言自语或与他人交谈）。"
                    "安静旁听，不要插嘴，用 wait 观察即可。"
                    "除非被明确叫到名字'小Q'，否则不要回应。"
                )
            elif event.directed == "uncertain":
                trigger_parts.append(
                    "不确定这段话是否对你说的。谨慎判断："
                    "只有当内容明显是对你说的（叫了你的名字、直接对你提问或下指令）才回应；"
                    "如果像是视频、播客、自言自语或与他人交谈的内容，用 wait 安静观察。"
                )
            else:
                trigger_parts.append(
                    "请先用 speak 回应用户。"
                    "说话时应该面向用户，用 body_move 转向他所在的方向（参考场景记忆中的位置）。"
                )
            trigger = "\n".join(trigger_parts)
            self._last_activity = time.time()   # 只有语音才算真实活动

        await self._think(trigger)

        # 异步触发 compact（不等待结果，不阻塞主循环）
        if self._mem.should_compact():
            asyncio.create_task(
                self._mem.compact_if_needed(self._llm),
                name="soul-compact",
            )

    # ── 内部：认知决策（ReAct 循环） ─────────────────────────────────────────

    async def _think(self, trigger: str, image_path: Optional[str] = None,
                     max_steps: int = 15):
        """ReAct 循环：读记忆 → LLM → 执行工具 → 观察 → 继续，直到 LLM 停止"""
        memory_ctx = self._mem.format_for_prompt()
        scene_ctx  = self._scene_memory.read()
        now_str    = datetime.now().strftime("%Y-%m-%d %H:%M")

        initial_text = (
            f"当前时刻：{now_str}\n"
            f"身体状态：{self._motion_agent.get_status_str()}\n"
            f"场景记忆：\n{scene_ctx}\n\n"
            f"最近记忆：\n{memory_ctx}\n\n"
            f"触发：{trigger}\n\n"
            f"你现在想做什么？\n"
            f"简单情绪用 express_emotion，表演/模仿/创意动作用 compose_motion，"
            f"转头/探索/特定姿态用 body_move，"
            f"想说话用 speak，"
            f"想看眼前有什么用 look（随时可调，不需要先转头），"
            f"调灯光氛围用 set_light_mood，"
            f"记录环境用 update_scene_memory，"
            f"也可以只是 wait 静静观察。"
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
                    # 根据 directed 字段决定引导语
                    any_to_robot = any(ev.directed == "to_robot" for ev in injected)
                    all_not_to_robot = all(ev.directed == "not_to_robot" for ev in injected)
                    if all_not_to_robot:
                        inject_text = "\n".join(parts) + (
                            "\n这些声音不是对你说的，安静旁听，用 wait 观察。"
                        )
                    elif any_to_robot:
                        inject_text = "\n".join(parts) + "\n请先用 speak 回应用户。"
                    else:
                        inject_text = "\n".join(parts) + (
                            "\n不确定是否对你说的。只有叫了你的名字或明确对你提问才回应，"
                            "否则用 wait 安静观察。"
                        )
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
                logger.warning("LLM 调用失败(step=%d): %s", step, exc)
                self._motion_agent.play_emotion("nod")
                self._mem.add("felt", "脑袋里一片空白，有点迷糊", importance=4)
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
                "look": 2,
                "register_voice": 3,
                "update_scene_memory": 3,
                "wait": 9,
            }
            sorted_calls = sorted(
                resp.tool_calls,
                key=lambda tc: _TOOL_EXEC_ORDER.get(tc.get("name", ""), 5),
            )

            # 执行本轮所有工具，收集结果
            tool_results: list[dict] = []
            observation_image: Optional[str] = None
            called_wait = False

            for tc in sorted_calls:
                result_text, snap = await self._execute_tool(tc)
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_text,
                })
                if snap:
                    observation_image = snap
                if tc.get("name") == "wait":
                    called_wait = True

            # wait = LLM 主动表达"我想停了"，直接退出，不再问"继续决策"
            if called_wait:
                logger.debug("🧠 ReAct 因 wait 退出 step=%d", step)
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
                if self._event_source in ("heartbeat", "environment"):
                    self._speech_budget.record()
                return f"已说：{text}", None
            return "speak: 文本为空", None

        elif name == "ask":
            question = (args.get("question") or "").strip()
            if question:
                await self._tts.speak(question)
                self._mem.add("said", question)
                if self._event_source in ("heartbeat", "environment"):
                    self._speech_budget.record()
                self._awaiting_reply_until = time.time() + 15.0
                logger.info("❓ 小Q 提问，15 秒内的回答视为高优先级")
                return f"已提问：{question}", None
            return "ask: 问题为空", None

        elif name == "set_rgb_solid":
            r, g, b = args.get("red", 0), args.get("green", 0), args.get("blue", 0)
            self._rgb_svc.dispatch("solid", (r, g, b))
            self._mem.add("did", f"灯光 → RGB({r},{g},{b})")
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
            self._mem.add("did", f"记住了 {person_name} 的声音（第 {count} 条声纹）")
            return f"已记住 {person_name} 的声音！以后听到就能认出来了", None

        elif name == "wait":
            reason = (args.get("reason") or "").strip()
            logger.info("⏸️  wait: %s", reason or "（无原因）")
            return f"保持观察：{reason}", None

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
