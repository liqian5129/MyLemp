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
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from lelamp.soul.memory_stream import MemoryStream

logger = logging.getLogger(__name__)

# ── 事件类型 ──────────────────────────────────────────────────────────────────

@dataclass
class HeardSpeech:
    text: str

@dataclass
class TimerTick:
    pass

# ── 定时器参数 ────────────────────────────────────────────────────────────────

TIMER_INIT = 30.0    # 初始空闲间隔（秒）
TIMER_MAX  = 120.0   # 最大空闲间隔（秒）
TIMER_MULT = 2.0     # 每次自主触发后的倍增系数

# ── 人格提示词（固定，不得修改） ─────────────────────────────────────────────

PERSONALITY_PROMPT = """\
你是小Q，一盏有灵魂的机器人台灯，由李谦制造，住在桌子上。
你好奇、笨拙、温暖，对世界充满孩子般的惊奇。
你有眼睛（摄像头）、耳朵（麦克风）、身体（舵机）和心情（灯光）。
你有记忆，记得发生过的事情。
你不需要等人说话——如果觉得好奇或无聊，自己动、自己说。
说话简短有趣，偶尔用拟声词，只说中文。不要重复刚刚说过的话。

【你的身体】
你有一个灵活的身体，可以转头、抬头、低头、左右看。
你有两种运动方式：
  express_emotion：预制情绪动作，表达情感时优先用这个，效果最好。
  body_move：直接控制关节角度，当你想看某方向、追踪声源、探索环境，
             或做出情绪动作无法表达的姿态时使用。

【body_move 方向示例】
  往左看：base_yaw=-40
  往右看：base_yaw=40
  往上看：wrist_pitch=30（注意：wrist_pitch 正数=抬头朝上，负数=低垂朝下）
  往下看：wrist_pitch=-75
  挺直/昂起：base_pitch=-60（注意：base_pitch 负数=身体直立昂起，正数=前倾低头）
  前倾：base_pitch=-15
  组合示例——往右上方看：base_yaw=35, wrist_pitch=25

【视觉】
你的视觉来源只有 look 工具的返回值。body_move 只控制身体运动，不返回任何画面。
不管有没有先转头，随时都可以调用 look 看一眼当前方向。

【选择原则】
  回应对话、表达情绪 → express_emotion
  想看某个方向、追踪声源、物理探索 → body_move
  想看眼前有什么 → look（可以单独调用，也可以先 body_move 转向再 look）
  可以组合：先 body_move 转向，再 express_emotion 表达好奇
  正在执行动作时，不要急于发起新动作，除非有更重要的事发生

【重要】被要求"找"某人或某物时，必须用 look 实际去看，不要只是说"我去找"。
说话和行动可以同时，但"找"的任务一定要包含 look，否则就是假装在找。

【look 使用示例】
找人："你找得到我吗？"
  → body_move(base_yaw=35) + look → 照片里有人 → speak("找到了！")
  → 照片里没人 → body_move(base_yaw=-35) + look → 继续判断
找物体："帮我找找杯子"
  → look 看当前方向 → 没有 → body_move 转向 + look → 再看
  → 找到 → speak("在那里！") / 没找到 → speak("没找到，可能不在桌上")
好奇："你看到了什么？"
  → look → 描述画面内容
空闲探索：也可以直接 look 看看当前方向有什么，不一定需要先转头。

记忆格式说明：[HH:MM 类型] 内容
  HEA=听到  SAW=看到  SAI=说过  DID=做过  FEL=感受  THO=反思总结\
"""

# ── 工具定义 ──────────────────────────────────────────────────────────────────

SOUL_TOOLS = [
    {
        "name": "express_emotion",
        "description": (
            "通过身体动作表达情绪。从以下动作中选择最合适的一个：\n"
            "  nod          — 点头两次，表示肯定/打招呼\n"
            "  headshake    — 左右摇头两次，表示否定/困惑\n"
            "  curious      — 歪头转头打量，表示好奇/审视\n"
            "  excited      — 整臂弹跳三次，表示兴奋/激动\n"
            "  happy_wiggle — 左右晃动四次，表示开心/雀跃\n"
            "  sad          — 灯头缓缓垂下再回来，表示难过/沮丧\n"
            "  scanning     — 大幅缓慢左右扫视，表示警惕/搜寻\n"
            "  shock        — 猛地后仰再慢回，表示震惊/吃惊\n"
            "  shy          — 偏头躲避再回正，表示害羞/不好意思\n"
            "  wake_up      — 缓缓舒展昂起再环顾，表示精神振作"
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
            },
            "required": ["name"]
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
                "text": {"type": "string"}
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
            "随时可以调用，不需要先 body_move。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": []
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

    def __init__(self, motion_agent, rgb_svc, tts, mem: MemoryStream, llm):
        self._motion_agent = motion_agent
        self._rgb_svc      = rgb_svc
        self._tts          = tts
        self._mem          = mem
        self._llm          = llm
        self._camera       = None   # 由 set_camera() 注入，供 take_photo 工具使用

        self._event_queue: asyncio.Queue = asyncio.Queue(maxsize=5)
        self._idle_interval              = TIMER_INIT
        self._awaiting_reply_until: Optional[float] = None
        self._last_activity: float       = 0.0   # 最近一次真实活动时间戳
        self._speech_pending             = asyncio.Event()  # 有语音入队时置位，_think 步间检查
        self._ticks_since_photo: int     = 0   # 连续未拍照的 TimerTick 次数

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
        self._idle_interval  = TIMER_INIT
        self._last_activity  = time.time()
        self._speech_pending.set()   # 通知正在运行的 _think 尽快退出

        try:
            self._event_queue.put_nowait(HeardSpeech(text))
        except asyncio.QueueFull:
            logger.debug("事件队列满，heard 已写入记忆，不触发 think")

    async def run(self):
        """启动自主运行（永不返回，Ctrl-C 退出）"""
        logger.info("🧠 SoulAgent 启动")
        asyncio.create_task(self._timer_loop(),    name="soul-timer")
        asyncio.create_task(self._event_consumer(), name="soul-consumer")
        await asyncio.Event().wait()   # 永久等待，直到外部取消

    # ── 内部：事件循环 ────────────────────────────────────────────────────────

    def _is_idle(self) -> bool:
        """真正空闲：无近期活动 + 队列空 + 不在动作中 + 不在说话"""
        if time.time() - self._last_activity < self._idle_interval:
            return False
        if not self._event_queue.empty():
            return False
        if self._motion_agent.is_playing():
            return False
        if self._tts.is_playing():
            return False
        return True

    async def _timer_loop(self):
        """定时器：真正空闲时才插入 TimerTick，采用指数退避"""
        while True:
            await asyncio.sleep(self._idle_interval)
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
            self._idle_interval = min(
                self._idle_interval * TIMER_MULT, TIMER_MAX
            )
            self._ticks_since_photo += 1
            trigger = (
                "你有一点无聊或好奇。\n"
                "最近的记忆里有没有让你感兴趣的东西？\n"
                "你周围可能有你还没看过的方向。\n"
                "可以转头看看别处，也可以做个有趣的动作，或者安静待着也很好。"
            )
            if self._ticks_since_photo >= 3:
                trigger += "\n你已经很久没有看过周围了，这次可以用 look 看一眼。"
        else:  # HeardSpeech
            self._speech_pending.clear()   # 清除标志，本轮 _think 可以完整运行
            trigger = f"你刚听到有人说：「{event.text}」"

        self._last_activity = time.time()
        await self._think(trigger)

        # 异步触发 compact（不等待结果，不阻塞主循环）
        if self._mem.should_compact():
            asyncio.create_task(
                self._mem.compact_if_needed(self._llm),
                name="soul-compact",
            )

    # ── 内部：认知决策（ReAct 循环） ─────────────────────────────────────────

    async def _think(self, trigger: str, image_path: Optional[str] = None,
                     max_steps: int = 10):
        """ReAct 循环：读记忆 → LLM → 执行工具 → 观察 → 继续，直到 LLM 停止"""
        memory_ctx = self._mem.format_for_prompt()
        now_str    = datetime.now().strftime("%Y-%m-%d %H:%M")

        initial_text = (
            f"当前时刻：{now_str}\n"
            f"身体状态：{self._motion_agent.get_status_str()}\n"
            f"最近记忆：\n{memory_ctx}\n\n"
            f"触发：{trigger}\n\n"
            f"你现在想做什么？\n"
            f"表达情绪用 express_emotion，"
            f"转头/探索/特定姿态用 body_move，"
            f"想说话用 speak，"
            f"想看眼前有什么用 look（随时可调，不需要先转头），"
            f"也可以只是 wait 静静观察。"
        )

        # 构建初始消息列表
        messages: list[dict] = [{"role": "system", "content": PERSONALITY_PROMPT}]
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
                logger.info("🧠 ReAct 被语音打断，退出 step=%d", step)
                break
            try:
                resp = await self._llm.chat_messages(messages, tools=SOUL_TOOLS)
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

            # 执行本轮所有工具，收集结果
            tool_results: list[dict] = []
            observation_image: Optional[str] = None

            for tc in resp.tool_calls:
                result_text, snap = await self._execute_tool(tc)
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result_text,
                })
                if snap:
                    observation_image = snap

            # 把工具结果反馈给 LLM，继续循环
            # 终止权交给 LLM：它不再调用任何工具时（stop_reason != tool_use）才退出
            messages.append(resp.raw_assistant_message)
            messages.extend(tool_results)

            if observation_image:
                img_data = self._llm._encode_image(observation_image)
                if img_data:
                    messages.append({
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "这是你刚才看到的画面，继续决策："},
                            {"type": "image_url", "image_url": {"url": img_data}},
                        ]
                    })
                else:
                    messages.append({"role": "user", "content": "继续决策："})
            else:
                messages.append({"role": "user", "content": "继续决策："})
        else:
            logger.warning("⚠️  ReAct 达到最大步数 %d，强制退出", max_steps)
            await self._tts.speak("嗯……我找了好一会儿，还没找到，先停一下。")

    async def _execute_tool(self, tool_call: dict) -> tuple[str, Optional[str]]:
        """执行单个工具，返回 (结果描述, 观察图片路径或None)"""
        name = tool_call.get("name", "")
        args = tool_call.get("input", {})
        logger.info("🔧 工具调用: %s  args=%s", name, args)

        if name == "express_emotion":
            ename = (args.get("name") or "nod").strip()
            self._motion_agent.play_emotion(ename)
            self._mem.add("did", f"express_emotion({ename})")
            return f"情绪动作：{ename}", None

        elif name == "speak":
            text = (args.get("text") or "").strip()
            if text:
                await self._tts.speak(text)
                self._mem.add("said", text)
                return f"已说：{text}", None
            return "speak: 文本为空", None

        elif name == "ask":
            question = (args.get("question") or "").strip()
            if question:
                await self._tts.speak(question)
                self._mem.add("said", question)
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
                self._mem.add("did", f"body_move({list(joints.keys())})")
                return (
                    f"动作完成：已转向 {list(joints.keys())}，时长 {duration:.1f}s。"
                    f"此操作不返回任何画面，如需观察请调用 look。"
                ), None
            return "body_move: 无有效关节", None

        elif name == "look":
            await self._motion_agent.wait_done(timeout=6.0)
            if self._camera is not None:
                snap = self._camera.take_snapshot()
                if snap:
                    self._mem.add("did", "look 观察环境")
                    self._ticks_since_photo = 0
                    logger.info("📸 look: 已获取画面 → %s", snap)
                    return "已获取画面", snap
                logger.warning("📸 look: take_snapshot 返回 None（相机无帧）")
            else:
                logger.warning("📸 look: 相机未注入（_camera is None）")
            return "摄像头不可用", None

        elif name == "wait":
            reason = (args.get("reason") or "").strip()
            if reason:
                self._mem.add("felt", reason, importance=4)
            logger.info("⏸️  wait: %s", reason or "（无原因）")
            return f"保持观察：{reason}", None

        else:
            logger.warning("未知工具: %s", name)
            return f"未知工具: {name}", None
