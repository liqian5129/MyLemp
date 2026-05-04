"""
小Q persona 模式入口(协议 v0.5.0,在 main_soul.py 基础上加表情驱动)

跟 main_soul.py 的差异:
  - 接 FaceDirector,通过 buddy daemon HTTP /face /arc 路由驱动设备表情
  - 用户语音 → 关键词触发 arc(早安/晚安/难过等)
  - 在 LLM 输出中识别 face 标签 → set_face

依赖:
  - buddy daemon 必须在跑(持 USB,转发 face/arc 命令到设备)
  - 长按屏左下角切到 buddy 模式时,设备暂停渲染 face;切回 persona 立即恢复

运行:
    # 终端 1:启动 daemon
    uv run python -m lelamp.companion --no-arm

    # 终端 2:启动 persona
    uv run python main_persona.py

环境变量(与 main_soul.py 相同,复用 .env):
    KIMI_API_KEY / KIMI_MODEL / DOUBAO_TTS_*
    LELAMP_DAEMON_URL    可选,默认 http://127.0.0.1:9000
"""
import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

from lelamp.agent.ai_client import AIClient
from lelamp.motion.motion_agent import MotionAgent
from lelamp.persona import FaceDirector
from lelamp.service.rgb.rgb_service import RGBService
from lelamp.soul.camera_capture import CameraCapture
from lelamp.soul.continuous_listener import ContinuousListener
from lelamp.soul.memory import MemoryStream
from lelamp.soul.soul_agent import SoulAgent
from lelamp.tts.doubao_speaker import DoubaoTTSPlayer
from lelamp.utils import find_serial_port
from lelamp.voice.funasr_asr import create_local_asr

load_dotenv()

# face 标签规则:让 LLM 在每段说话(speak/ask 工具的 text/question 字段)
# 末尾附加 <face>name</face> 标签,FaceDirector 解析它驱动设备表情。
# 这段拼到 SoulAgent.PERSONALITY_PROMPT 末尾,只在 main_persona 入口生效,
# main_soul 完全不受影响。
_PERSONA_FACE_PROMPT = """\
<face_expression>
你能用屏幕上的表情同步表达情绪。在每段说话**结尾**(无论是 speak 还是 ask
的文本)附加一个表情标签 <face>name</face>,让屏幕上的脸跟你说话同步变化。

格式:`<face>name</face>` 紧贴文本结尾,**不用空格分隔**(避免读出来)。

可选(必须从这 16 个里选,小写下划线):
- 平静类:neutral / content / warm_smile
- 关注/惊讶:surprised / focus / idle_watch / peek
- 倾听/安抚:listen / comfort
- 调皮/亲昵:wink / smirk / side_eye / blush / love
- 困倦:sleepy / sleep

举例:
- "今天过得怎么样?<face>listen</face>"
- "哇,真厉害!<face>surprised</face>"
- "嘿嘿,被你发现了<face>blush</face>"

不确定时用 neutral 或 content。**只附标签**,标签里的 `<face>` 符号不会
被朗读出来 — 系统会在朗读前剥离。
</face_expression>"""

_LOG_DIR = Path("logs")
_LOG_DIR.mkdir(exist_ok=True)

_fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")

_console = logging.StreamHandler()
_console.setFormatter(_fmt)

_file = RotatingFileHandler(
    _LOG_DIR / "soul.log",
    maxBytes=5 * 1024 * 1024,   # 5 MB per file
    backupCount=5,
    encoding="utf-8",
)
_file.setFormatter(_fmt)

logging.basicConfig(level=logging.INFO, handlers=[_console, _file])
logger = logging.getLogger(__name__)


async def main():
    port = find_serial_port()
    logger.info("🔌 串口: %s", port)

    # ── LLM 客户端（运动 + 灵魂共用）────────────────────────────────────────
    llm = AIClient(
        provider="kimi",
        api_key=os.environ["KIMI_API_KEY"],
        model=os.environ.get("KIMI_MODEL", "kimi-k2.5"),
        base_url="https://api.moonshot.cn/v1",
        enable_thinking=os.environ.get("KIMI_THINKING", "").lower() in ("1", "true", "yes"),
    )

    # ── 运动服务（Motion Agent）──────────────────────────────────────────────
    motion_svc = MotionAgent(port=port, lamp_id="lelamp", fps=30)
    motion_svc.start()
    if os.environ.get("MOTION_RECORD", "").lower() in ("1", "true", "yes"):
        motion_svc.start_recording("data/motion_record.csv")

    # ── RGB 服务 ──────────────────────────────────────────────────────────────
    rgb_svc = RGBService(
        led_count=40,
        led_pin=12,
        led_freq_hz=800000,
        led_dma=10,
        led_brightness=255,
        led_invert=False,
        led_channel=0,
    )
    rgb_svc.start()

    # ── TTS ───────────────────────────────────────────────────────────────────
    tts = DoubaoTTSPlayer(
        appid=os.environ["DOUBAO_TTS_APPID"],
        token=os.environ["DOUBAO_TTS_TOKEN"],
        cluster=os.environ.get("DOUBAO_TTS_CLUSTER", "volcano_tts"),
        voice_type=os.environ.get(
            "DOUBAO_TTS_VOICE_TYPE",
            "zh_female_shuangkuaisisi_uranus_bigtts",
        ),
        emotion=os.environ.get("DOUBAO_TTS_EMOTION", "happy"),
    )
    await tts.start()

    # ── 表情驱动(persona 模式新增,通过 buddy daemon HTTP /face /arc 转发)─
    daemon_url = os.environ.get("LELAMP_DAEMON_URL", "http://127.0.0.1:9000")
    face_director = FaceDirector(daemon_url=daemon_url)
    logger.info("🎭 FaceDirector 接 daemon: %s", daemon_url)

    # 包装 tts.speak:拦截 LLM 输出 → 解析 <face>...</face> 触发 set_face → 朗读前 strip 标签
    _orig_speak = tts.speak

    async def speak_with_face(text, *args, **kwargs):
        face_director.maybe_face_from_assistant_text(text)
        cleaned = FaceDirector.strip_face_tag(text)
        return await _orig_speak(cleaned, *args, **kwargs)

    tts.speak = speak_with_face

    # ── 记忆 + 智能体 ─────────────────────────────────────────────────────────
    mem   = MemoryStream()
    mem.start()   # 启动后台防抖写盘
    agent = SoulAgent(
        motion_svc, rgb_svc, tts, mem, llm,
        personality_prompt_extra=_PERSONA_FACE_PROMPT,
    )

    # 包装 on_speech:用户语音先过关键词 arc 触发,再走原 SoulAgent 处理
    _orig_on_speech = agent.on_speech

    async def on_speech_with_face(text: str):
        face_director.maybe_arc_from_user_text(text)
        await _orig_on_speech(text)

    agent.on_speech = on_speech_with_face

    # ── 开机动作：wake_up 在最轻负载下执行（ASR/摄像头均未启动）────────────────
    motion_svc.play_emotion("wake_up")
    rgb_svc.dispatch("solid", (180, 180, 255))
    face_director.play_arc("morning")  # 表情走 morning 剧本(sleep→sleepy→...→warm_smile)
    await asyncio.sleep(2.2)   # 等 wake_up（2.0s）执行完毕

    # ── 摄像头感知（wake_up 结束后再启动）────────────────────────────────────
    camera_device = int(os.environ.get("CAMERA_DEVICE", "0"))
    camera_flip = os.environ.get("CAMERA_FLIP", "").lower() in ("1", "true", "yes")
    camera = CameraCapture(
        device_id=camera_device,
        flip=camera_flip,
    )
    camera.start()
    agent.set_camera(camera)

    # ── ASR（wake_up 结束后再启动后台加载，避免模型加载与运动争抢 CPU）─────────
    loop = asyncio.get_running_loop()
    asr  = create_local_asr()
    await asyncio.to_thread(asr.wait_ready)   # 等 ASR 加载完成（后台线程，不阻塞循环）
    await tts.speak("呼——我醒来了。")

    # ── 持续监听（ASR 已就绪，直接启动）─────────────────────────────────────
    listener = ContinuousListener(
        asr=asr,
        on_speech=agent.on_speech,
        tts=tts,
        loop=loop,
    )
    listener.start()

    logger.info("✨ 小Q 灵魂系统已启动（Ctrl-C 退出）")

    try:
        await agent.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("👋 收到退出信号")
    finally:
        face_director.play_arc("goodnight")  # 退场剧本:warm_smile→...→sleep(永停)
        camera.stop()
        listener.stop()
        await tts.stop()
        motion_svc.stop_recording()
        motion_svc.stop()
        rgb_svc.stop()
        face_director.close()
        logger.info("🌙 小Q 已休眠")


if __name__ == "__main__":
    asyncio.run(main())
