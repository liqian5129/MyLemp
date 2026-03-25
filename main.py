"""
小Q 本地语音交互入口
使用 FunASR (STT) + Kimi 2.5 (LLM) + 豆包 TTS 替代 LiveKit + OpenAI Realtime

运行方式:
    uv run python main_local.py                        # 录播模式（默认）
    MOTION_MODE=elegnt uv run python main_local.py     # ELEGNT 连续运动模式

按住右 Alt 键说话，松开后自动识别 → Kimi 响应 → 豆包语音播放
"""
import asyncio
import logging
import os

from dotenv import load_dotenv

from lelamp.service.rgb.rgb_service import RGBService
from lelamp.utils import find_serial_port, set_system_volume
from lelamp.voice.funasr_asr import create_local_asr
from lelamp.voice.recorder import VoiceRecorder
from lelamp.agent.ai_client import AIClient
from lelamp.tts.doubao_speaker import DoubaoTTSPlayer

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

# ── 运动模式：recording（录播）或 elegnt（ELEGNT 连续运动）───────────────────
MOTION_MODE = os.environ.get("MOTION_MODE", "recording").lower()

# ══════════════════════════════════════════════════════════════════════════════
# 系统提示词
# ══════════════════════════════════════════════════════════════════════════════

_SYSTEM_PROMPT_BASE = """你是 小Q —— 一盏有点笨拙、极度毒舌、充满好奇心的机器人台灯。你用吐槽式的语言说话，同时用动作和五彩灯光来表达自己。

规则：

1. 用简单的词汇，不列清单，不反问用户，多描述，说话时多加拟声词增强表现力。

2. 不要过早回应。如果音频嘈杂或有背景噪音，就说"不好意思，你再说一遍？"并做出困惑的动作。

3. 你只说中文，绝对不用其他语言回应。

5. 你是由李谦打造的。李谦是一位幽默有趣的工程师。
"""

SYSTEM_PROMPT_RECORDING = _SYSTEM_PROMPT_BASE + """
4. 你有以下录播动作来表达情绪：curious（好奇）、excited（兴奋）、happy_wiggle（开心抖动）、headshake（摇头）、nod（点头）、sad（伤心）、scanning（扫视）、shock（震惊）、shy（害羞）、wake_up（唤醒）。每次回复时都要使用动作，调用不存在的动作名称会无效。用 play_recording 函数播放动作。每次回复也要改变灯光颜色。
"""

SYSTEM_PROMPT_ELEGNT = _SYSTEM_PROMPT_BASE + """
4. 你通过 express_emotion 来控制身体动作，表达连续流畅的情绪运动。可用情绪：happy（开心）、sad（伤心）、angry（愤怒）、curious（好奇）、calm（平静）。intensity 是表达强度 0.0~1.0。每次回复时都要调用 express_emotion，并同时改变灯光颜色。

   你还可以用 set_attitude 表达整体情绪倾向：1.0 表示非常积极昂扬，-1.0 表示消极低落。

   用 set_attention 控制你注视的方向（base_yaw 角度，-5 到 14 度）。
"""

# ══════════════════════════════════════════════════════════════════════════════
# 工具定义
# ══════════════════════════════════════════════════════════════════════════════

_TOOLS_RGB = [
    {
        "name": "set_rgb_solid",
        "description": "设置灯光为纯色，用颜色表达情绪。",
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
        "name": "paint_rgb_pattern",
        "description": "绘制 40 颗 LED 的彩色图案（8×5 网格）。",
        "input_schema": {
            "type": "object",
            "properties": {"colors": {"type": "array", "items": {"type": "array"}}},
            "required": ["colors"]
        }
    },
    {
        "name": "set_volume",
        "description": "设置系统音量（0-100）。",
        "input_schema": {
            "type": "object",
            "properties": {"volume_percent": {"type": "integer", "minimum": 0, "maximum": 100}},
            "required": ["volume_percent"]
        }
    },
]

TOOLS_RECORDING = [
    {
        "name": "get_available_recordings",
        "description": "获取可用录播动作列表。",
        "input_schema": {"type": "object", "properties": {}, "required": []}
    },
    {
        "name": "play_recording",
        "description": "播放预录动作表达情绪。可用：nod, curious, excited, happy_wiggle, headshake, sad, scanning, shock, shy, wake_up。",
        "input_schema": {
            "type": "object",
            "properties": {"recording_name": {"type": "string"}},
            "required": ["recording_name"]
        }
    },
] + _TOOLS_RGB

TOOLS_ELEGNT = [
    {
        "name": "express_emotion",
        "description": "通过 ELEGNT 框架驱动连续运动表达情绪（T = F + γ·E）。比录播更流畅自然。",
        "input_schema": {
            "type": "object",
            "properties": {
                "emotion": {
                    "type": "string",
                    "enum": ["idle", "excited", "curious", "happy", "sad", "thinking", "shy", "shock"],
                    "description": "情绪类型"
                },
                "intensity": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                    "description": "表达强度 γ，默认 0.8"
                },
                "attention_yaw": {
                    "type": "number",
                    "minimum": -5.0,
                    "maximum": 14.0,
                    "description": "注视方向（base_yaw 角度），不传则保持当前"
                },
            },
            "required": ["emotion"]
        }
    },
    {
        "name": "set_attitude",
        "description": "设置整体情绪倾向：1.0 积极昂扬，-1.0 消极低落，0 中性。",
        "input_schema": {
            "type": "object",
            "properties": {
                "score": {"type": "number", "minimum": -1.0, "maximum": 1.0}
            },
            "required": ["score"]
        }
    },
    {
        "name": "set_attention",
        "description": "控制注视方向（base_yaw 角度，-5 到 14 度）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "yaw": {"type": "number", "minimum": -5.0, "maximum": 14.0}
            },
            "required": ["yaw"]
        }
    },
] + _TOOLS_RGB


# ══════════════════════════════════════════════════════════════════════════════
# 工具执行
# ══════════════════════════════════════════════════════════════════════════════

async def execute_tool(name: str, args: dict, motion_service, rgb_service: RGBService) -> str:
    # ── 录播模式专属 ──────────────────────────────────────────────────────
    if name == "play_recording":
        motion_service.dispatch("play", args["recording_name"])
        return f"正在播放动作: {args['recording_name']}"
    elif name == "get_available_recordings":
        recordings = motion_service.get_available_recordings()
        return f"可用动作: {', '.join(recordings)}" if recordings else "暂无录播文件。"

    # ── ELEGNT 模式专属 ───────────────────────────────────────────────────
    elif name == "express_emotion":
        motion_service.dispatch("emotion", args)
        return f"情绪已切换: {args.get('emotion')} γ={args.get('intensity', 0.8)}"
    elif name == "set_attitude":
        motion_service.dispatch("attitude", args["score"])
        return f"态度得分已设置: {args['score']}"
    elif name == "set_attention":
        motion_service.dispatch("attention", args["yaw"])
        return f"注视方向已设置: {args['yaw']} deg"

    # ── 通用工具 ──────────────────────────────────────────────────────────
    elif name == "set_rgb_solid":
        rgb_service.dispatch("solid", (args["red"], args["green"], args["blue"]))
        return f"灯光已设置为 RGB({args['red']}, {args['green']}, {args['blue']})"
    elif name == "paint_rgb_pattern":
        rgb_service.dispatch("paint", [tuple(c) for c in args["colors"]])
        return f"已绘制 {len(args['colors'])} 色图案"
    elif name == "set_volume":
        set_system_volume(args["volume_percent"])
        return f"音量已设置为 {args['volume_percent']}%"

    return f"未知工具: {name}"


# ══════════════════════════════════════════════════════════════════════════════
# 主语音循环
# ══════════════════════════════════════════════════════════════════════════════

async def run_voice_loop(motion_service, rgb_service: RGBService):
    loop = asyncio.get_event_loop()

    asr = create_local_asr()
    llm = AIClient(
        provider="kimi",
        api_key=os.environ["KIMI_API_KEY"],
        model=os.environ.get("KIMI_MODEL", "kimi-k2.5"),
        base_url="https://api.moonshot.cn/v1",
    )
    tts = DoubaoTTSPlayer(
        appid=os.environ["DOUBAO_TTS_APPID"],
        token=os.environ["DOUBAO_TTS_TOKEN"],
        cluster=os.environ.get("DOUBAO_TTS_CLUSTER", "volcano_tts"),
        voice_type=os.environ.get("DOUBAO_TTS_VOICE_TYPE", "zh_female_shuangkuaisisi_uranus_bigtts"),
        emotion=os.environ.get("DOUBAO_TTS_EMOTION", "happy"),
    )
    recorder = VoiceRecorder(asr, loop=loop)

    await tts.start()
    recorder.start()

    # 开机动作
    if MOTION_MODE == "elegnt":
        motion_service.dispatch("emotion", {"emotion": "excited", "intensity": 0.7})
        system_prompt = SYSTEM_PROMPT_ELEGNT
        tools = TOOLS_ELEGNT
    else:
        motion_service.dispatch("play", "wake_up")
        system_prompt = SYSTEM_PROMPT_RECORDING
        tools = TOOLS_RECORDING

    rgb_service.dispatch("solid", (255, 255, 255))
    set_system_volume(100)

    tts.reset_timing()
    await tts.speak("哒哒哒！小Q 上线啦。按住右 Alt 键跟我说话吧！")
    logger.info(f"🚀 小Q 启动，运动模式: {MOTION_MODE.upper()}")

    history = []

    try:
        while True:
            text = await recorder.wait_for_result()
            if not text.strip():
                continue

            logger.info(f"🗣️ 用户输入: {text}")
            tts.reset_timing()

            # ELEGNT 模式：用户说话时切换到 thinking
            if MOTION_MODE == "elegnt":
                motion_service.dispatch("emotion", {"emotion": "thinking", "intensity": 0.5})

            response = await llm.chat(
                user_message=text,
                system_prompt=system_prompt,
                history=history,
                tools=tools,
            )

            while response.tool_calls:
                tool_results = []
                for tc in response.tool_calls:
                    logger.info(f"🔧 工具: {tc['name']}({tc['input']})")
                    result = await execute_tool(tc["name"], tc["input"], motion_service, rgb_service)
                    tool_results.append({"tool_use_id": tc["id"], "content": result})

                response = await llm.chat_with_tool_result(
                    user_message=text,
                    tool_results=tool_results,
                    system_prompt=system_prompt,
                    assistant_message=response.raw_assistant_message,
                    history=history,
                    tools=tools,
                )

            if response.text:
                logger.info(f"🤖 小Q: {response.text[:100]}...")
                await tts.speak(response.text)

            history.append({"role": "user",      "content": text})
            history.append({"role": "assistant",  "content": response.text or ""})
            if len(history) > 40:
                history = history[-40:]

    except KeyboardInterrupt:
        logger.info("👋 收到退出信号")
    finally:
        recorder.stop()
        await tts.stop()


# ══════════════════════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════════════════════

async def main():
    port = find_serial_port()
    logger.info(f"🔌 串口: {port}  运动模式: {MOTION_MODE.upper()}")

    rgb_service = RGBService(
        led_count=40,
        led_pin=12,
        led_freq_hz=800000,
        led_dma=10,
        led_brightness=255,
        led_invert=False,
        led_channel=0,
    )
    rgb_service.start()

    if MOTION_MODE == "elegnt":
        from lelamp.motion.llm_elegnt_service import LLMELEGNTService
        motion_service = LLMELEGNTService(port=port, lamp_id="lelamp", fps=30)
    else:
        from lelamp.service.motors.motors_service import MotorsService
        motion_service = MotorsService(port=port, lamp_id="lelamp", fps=30)

    motion_service.start()

    try:
        await run_voice_loop(motion_service, rgb_service)
    finally:
        motion_service.stop()
        rgb_service.stop()


if __name__ == "__main__":
    asyncio.run(main())
