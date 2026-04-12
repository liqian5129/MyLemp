"""
小Q 智能耳朵入口（本地 VAD + HTTP Omni 架构）

本地 VAD 检测语音段 → HTTP Omni（qwen3-omni-flash）结构化分析，
替换 ContinuousListener + FunASR。SoulAgent 保持唯一决策者。

运行：
    uv run python main_dual.py

环境变量：
    KIMI_API_KEY           必填  — SoulAgent LLM
    KIMI_MODEL             可选，默认 kimi-k2.5
    DASHSCOPE_API_KEY      必填  — Qwen Omni HTTP
    EMBEDDING_API_KEY      可选  — 长期记忆向量搜索（DashScope text-embedding-v4）
    REVIEW_LLM_API_KEY     可选  — Background Review 独立 API key（默认复用主 LLM key）
    REVIEW_LLM_MODEL       可选  — Background Review 模型（默认同主 LLM）
    DOUBAO_TTS_APPID       必填
    DOUBAO_TTS_TOKEN       必填
    DOUBAO_TTS_CLUSTER     可选，默认 volcano_tts
    DOUBAO_TTS_VOICE_TYPE  可选
    DOUBAO_TTS_EMOTION     可选，默认 happy
    OMNI_SILENCE_SEC       可选，默认 2.0  — VAD 静默截断时长
    AUDIO_INPUT_DEVICE     可选  — 麦克风设备 ID
"""
import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from lelamp.agent.ai_client import AIClient
from lelamp.motion.motion_agent import MotionAgent
from lelamp.service.rgb.rgb_service import RGBService
from lelamp.soul.camera_capture import CameraCapture
from lelamp.soul.memory import HistoryDB, IdentityMemory, MemoryStream, LongTermMemory
from lelamp.soul.omni_ear import OmniEar
from lelamp.soul.soul_agent import SoulAgent
from lelamp.tts.doubao_speaker import DoubaoTTSPlayer
from lelamp.utils import find_serial_port

load_dotenv()

_LOG_DIR = Path("logs")
_LOG_DIR.mkdir(exist_ok=True)

_fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s %(message)s")

_console = logging.StreamHandler()
_console.setFormatter(_fmt)

_log_file = _LOG_DIR / f"soul_{datetime.now():%Y%m%d_%H%M%S}.log"
_file = logging.FileHandler(_log_file, encoding="utf-8")
_file.setFormatter(_fmt)

logging.basicConfig(level=logging.INFO, handlers=[_console, _file])
logger = logging.getLogger(__name__)


async def main():
    port = find_serial_port()
    logger.info("串口: %s", port)

    # ── LLM 客户端 ────────────────────────────────────────────────────────────
    llm_provider = os.environ.get("LLM_PROVIDER", "kimi").lower()

    if llm_provider == "qwen":
        llm = AIClient(
            provider="qwen",
            api_key=os.environ["QWEN_API_KEY"],
            model=os.environ.get("QWEN_MODEL", "qwen3.6-plus"),
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
    elif llm_provider == "kimi":
        llm = AIClient(
            provider="kimi",
            api_key=os.environ["KIMI_API_KEY"],
            model=os.environ.get("KIMI_MODEL", "kimi-k2.5"),
            base_url="https://api.moonshot.cn/v1",
            enable_thinking=os.environ.get("KIMI_THINKING", "").lower() in ("1", "true", "yes"),
        )
    else:
        raise ValueError(f"不支持的 LLM_PROVIDER: {llm_provider}")

    # ── Background Review LLM 客户端（独立，不阻塞主对话）───────────────────────
    # 默认交叉 provider：主 qwen → review kimi，主 kimi → review qwen
    review_api_key = os.environ.get("REVIEW_LLM_API_KEY", "")
    if review_api_key:
        # 显式指定 → 沿用主 provider
        review_llm = AIClient(
            provider=llm_provider,
            api_key=review_api_key,
            model=os.environ.get("REVIEW_LLM_MODEL", llm.model),
            base_url=llm.base_url,
        )
    elif llm_provider == "qwen" and os.environ.get("KIMI_API_KEY"):
        review_llm = AIClient(
            provider="kimi",
            api_key=os.environ["KIMI_API_KEY"],
            model=os.environ.get("REVIEW_LLM_MODEL", "kimi-k2.5"),
            base_url="https://api.moonshot.cn/v1",
        )
    elif llm_provider == "kimi" and os.environ.get("QWEN_API_KEY"):
        review_llm = AIClient(
            provider="qwen",
            api_key=os.environ["QWEN_API_KEY"],
            model=os.environ.get("REVIEW_LLM_MODEL", "qwen3.6-plus"),
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
    else:
        # 兜底：复用主 LLM
        review_llm = AIClient(
            provider=llm_provider,
            api_key=llm.api_key,
            model=llm.model,
            base_url=llm.base_url,
        ) if llm.api_key else None

    # ── 运动服务 ──────────────────────────────────────────────────────────────
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

    # ── 身份识别 + 记忆 + 智能体 ────────────────────────────────────────────────
    identity_mem = IdentityMemory()

    # SQLite 会话历史（首次启动时迁移旧 archive）
    history_db = HistoryDB()
    archive_path = Path.home() / ".lelamp" / "memories.archive.json"
    if archive_path.exists():
        migrated = history_db.migrate_from_archive(archive_path)
        if migrated > 0:
            logger.info("📦 已迁移 %d 条历史到 SQLite", migrated)

    mem = MemoryStream(history_db=history_db)
    mem.start()
    ltm = LongTermMemory(api_key=os.environ.get("EMBEDDING_API_KEY", ""))
    agent = SoulAgent(motion_svc, rgb_svc, tts, mem, llm,
                      identity_memory=identity_mem, longterm_memory=ltm,
                      review_llm=review_llm, history_db=history_db)

    # ── 开机动作 ──────────────────────────────────────────────────────────────
    motion_svc.play_emotion("wake_up")
    rgb_svc.dispatch("solid", (180, 180, 255))
    await asyncio.sleep(2.2)

    # ── 摄像头 ────────────────────────────────────────────────────────────────
    camera_device = int(os.environ.get("CAMERA_DEVICE", "0"))
    camera_flip = os.environ.get("CAMERA_FLIP", "").lower() in ("1", "true", "yes")
    camera = CameraCapture(device_id=camera_device, flip=camera_flip)
    camera.start()
    agent.set_camera(camera)

    # ── 智能耳朵（OmniEar：本地 VAD + HTTP Omni）────────────────────────────
    ear = OmniEar(
        api_key=os.environ.get("DASHSCOPE_API_KEY"),
        silence_sec=float(os.environ.get("OMNI_SILENCE_SEC", "1.2")),
        input_device=int(os.environ["AUDIO_INPUT_DEVICE"]) if os.environ.get("AUDIO_INPUT_DEVICE") else None,
        identity_memory=identity_mem,
    )
    ear.on_event = agent.on_audio_event

    # AEC：TTS 播放时进入 barge-in 模式，用户说话可打断
    tts.on_play_start = ear.mute
    tts.on_play_end = ear.unmute
    ear.on_barge_in = tts.interrupt

    await ear.start()
    await tts.speak("呼——我醒来了。")

    logger.info("小Q 智能耳朵系统已启动（Ctrl-C 退出）")

    try:
        await agent.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("收到退出信号")
    finally:
        await agent.shutdown()
        camera.stop()
        await ear.stop()
        await tts.stop()
        motion_svc.stop_recording()
        motion_svc.stop()
        rgb_svc.stop()
        history_db.close()
        logger.info("小Q 已休眠")


if __name__ == "__main__":
    asyncio.run(main())
