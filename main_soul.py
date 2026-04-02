"""
小Q 自主灵魂系统入口

参考 Generative Agents 和 OpenClaw 架构，将小Q 从被动响应升级为
有感知、有记忆、自主决策的具身智能体。

运行：
    uv run python main_soul.py

环境变量（与 main.py 相同，复用 .env）：
    KIMI_API_KEY         必填
    KIMI_MODEL           可选，默认 kimi-k2.5
    DOUBAO_TTS_APPID     必填
    DOUBAO_TTS_TOKEN     必填
    DOUBAO_TTS_CLUSTER   可选，默认 volcano_tts
    DOUBAO_TTS_VOICE_TYPE 可选
    DOUBAO_TTS_EMOTION   可选，默认 happy
"""
import asyncio
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

from lelamp.agent.ai_client import AIClient
from lelamp.motion.motion_agent import MotionAgent
from lelamp.service.rgb.rgb_service import RGBService
from lelamp.soul.camera_capture import CameraCapture
from lelamp.soul.continuous_listener import ContinuousListener
from lelamp.soul.memory_stream import MemoryStream
from lelamp.soul.soul_agent import SoulAgent
from lelamp.tts.doubao_speaker import DoubaoTTSPlayer
from lelamp.utils import find_serial_port
from lelamp.voice.funasr_asr import create_local_asr

load_dotenv()

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
        motion_svc.start_recording("motion_record.csv")

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

    # ── 记忆 + 智能体 ─────────────────────────────────────────────────────────
    mem   = MemoryStream()
    mem.start()   # 启动后台防抖写盘
    agent = SoulAgent(motion_svc, rgb_svc, tts, mem, llm)

    # ── 开机动作：wake_up 在最轻负载下执行（ASR/摄像头均未启动）────────────────
    motion_svc.play_emotion("wake_up")
    rgb_svc.dispatch("solid", (180, 180, 255))
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
    mem.add("felt", "刚刚启动，世界感觉是新鲜的", importance=6)

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
        camera.stop()
        listener.stop()
        await tts.stop()
        motion_svc.stop_recording()
        motion_svc.stop()
        rgb_svc.stop()
        logger.info("🌙 小Q 已休眠")


if __name__ == "__main__":
    asyncio.run(main())
