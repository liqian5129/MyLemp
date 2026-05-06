"""
小Q persona 模式入口(协议 v0.5.0,在 main_dual.py 基础上加表情驱动)

跟 main_dual.py 的差异:
  - 接 FaceDirector,通过 buddy daemon HTTP /face /arc 路由驱动设备表情
  - 用户语音 → 关键词触发 arc(早安/晚安/难过等),wrap agent.on_audio_event
  - SoulAgent 注入 face 标签提示,LLM 在每段说话尾部带 <face>name</face>
  - tts.speak patch:朗读前解析标签 → set_face,朗读时 strip 标签

依赖:
  - buddy daemon 必须在跑(持 USB,转发 face/arc 命令到设备)
  - 长按屏左下角切到 buddy 模式时,设备暂停渲染 face;切回 persona 立即恢复

运行:
    # 终端 1:启动 daemon(持 USB,接 cc hooks,转发 /face /arc)
    uv run python -m lelamp.companion --no-arm

    # 终端 2:启动 persona
    uv run python main_persona.py

环境变量(沿用 main_dual.py):
    KIMI_API_KEY           必填  — SoulAgent LLM
    KIMI_MODEL             可选,默认 kimi-k2.5
    DASHSCOPE_API_KEY      必填  — Qwen Omni HTTP
    EMBEDDING_API_KEY      可选  — 长期记忆向量搜索
    REVIEW_LLM_API_KEY     可选  — Background Review 独立 API key
    REVIEW_LLM_MODEL       可选
    DOUBAO_TTS_APPID       必填
    DOUBAO_TTS_TOKEN       必填
    DOUBAO_TTS_CLUSTER     可选,默认 volcano_tts
    DOUBAO_TTS_VOICE_TYPE  可选
    DOUBAO_TTS_EMOTION     可选,默认 happy
    OMNI_SILENCE_SEC       可选,默认 1.2  — VAD 静默截断时长
    AUDIO_INPUT_DEVICE     可选  — 麦克风设备 ID
    LELAMP_DAEMON_URL      可选,默认 http://127.0.0.1:9000
"""
import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from lelamp.agent.ai_client import AIClient
from lelamp.motion.motion_agent import MotionAgent
from lelamp.persona import FaceDirector, BuddyAwareness
from lelamp.service.rgb.rgb_service import RGBService
from lelamp.soul.camera_capture import CameraCapture
from lelamp.soul.memory import HistoryDB, IdentityMemory, MemoryStream, LongTermMemory
from lelamp.soul.omni_ear import OmniEar
from lelamp.soul.soul_agent import SoulAgent
from lelamp.soul.visual_monitor import VisualMonitor
from lelamp.tts.doubao_speaker import DoubaoTTSPlayer
from lelamp.utils import find_serial_port

load_dotenv()

# face 标签规则:让 LLM 在每段说话(speak/ask 工具)尾部附加 <face>name</face>。
# 拼到 SoulAgent.PERSONALITY_PROMPT 末尾,只在 main_persona 入口生效,
# main_soul / main_dual 完全不受影响。
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

_log_file = _LOG_DIR / f"persona_{datetime.now():%Y%m%d_%H%M%S}.log"
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
    elif llm_provider == "openrouter":
        llm = AIClient(
            provider="openrouter",
            api_key=os.environ["OPENROUTER_API_KEY"],
            model=os.environ.get("OPENROUTER_MODEL", "anthropic/claude-sonnet-4"),
            base_url="https://openrouter.ai/api/v1",
        )
    else:
        raise ValueError(f"不支持的 LLM_PROVIDER: {llm_provider}")

    # ── Background Review LLM 客户端(独立,不阻塞主对话)─────────────────────
    # 默认交叉 provider:主 qwen → review kimi,主 kimi → review qwen
    review_api_key = os.environ.get("REVIEW_LLM_API_KEY", "")
    if review_api_key:
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
        review_llm = AIClient(
            provider=llm_provider,
            api_key=llm.api_key,
            model=llm.model,
            base_url=llm.base_url,
        ) if llm.api_key else None

    # ── 运动服务 ──────────────────────────────────────────────────────────────
    motion_svc = MotionAgent(port=port, lamp_id="lelamp", fps=30)
    try:
        motion_svc.start()
    except Exception as e:
        logger.error("舵机启动失败: %s", e)
        logger.error("─" * 60)
        logger.error("物理排查清单(按概率从高到低):")
        logger.error("  1. 12V 电源适配器是否插好,LED 是否亮")
        logger.error("  2. 主控板 DC 接头是否插紧")
        logger.error("  3. 舵机串联线是否松动(5 个 servo 串联,第一个断后面全断)")
        logger.error("  4. 通电瞬间 5 个舵机有无'咔哒'声(torque self-test)")
        logger.error("─" * 60)
        logger.error("修复后跑: uv run python scripts/scan_motors.py --port %s", port)
        logger.error("应见 ID 1-5 全部响应,然后再 ./scripts/start_persona.sh")
        return  # 直接退出 main(),不启 face/语音(SoulAgent 强依赖 motion_svc)
    if os.environ.get("MOTION_RECORD", "").lower() in ("1", "true", "yes"):
        motion_svc.start_recording("data/motion_record.csv")

    # ── device_mode 互斥 gate(协议 v0.5.1)──────────────────────────────────
    # BuddyAwareness 维护 device_mode_box[0],SoulAgent 的 motion 调用全部经过
    # _gate 包装,buddy 模式时拦截(让 BuddyAwareness 主导 buddy motion)。
    # BuddyAwareness 用 _orig_play_keyframes 引用绕过 gate 直接调原方法。
    device_mode_box: list[str] = ["persona"]

    _orig_play_emotion   = motion_svc.play_emotion
    _orig_play_compose   = motion_svc.play_compose
    _orig_play_keyframes = motion_svc.play_keyframes
    _orig_body_move      = motion_svc.body_move

    def _gate(name: str, orig):
        def wrapper(*args, **kwargs):
            if device_mode_box[0] == "buddy":
                logger.debug("buddy 模式跳过 SoulAgent.%s", name)
                return None
            return orig(*args, **kwargs)
        return wrapper

    motion_svc.play_emotion   = _gate("play_emotion",   _orig_play_emotion)
    motion_svc.play_compose   = _gate("play_compose",   _orig_play_compose)
    motion_svc.play_keyframes = _gate("play_keyframes", _orig_play_keyframes)
    motion_svc.body_move      = _gate("body_move",      _orig_body_move)
    logger.info("🚦 motion gate 已装(persona 透传,buddy 拦截 SoulAgent)")

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

    # 包装 tts.speak:拦截 LLM 输出 → 解析 <face>...</face> 触发 set_face
    # → 朗读前 strip 标签
    _orig_speak = tts.speak

    async def speak_with_face(text, *args, **kwargs):
        face_director.maybe_face_from_assistant_text(text)
        cleaned = FaceDirector.strip_face_tag(text)
        return await _orig_speak(cleaned, *args, **kwargs)

    tts.speak = speak_with_face

    # ── BuddyAwareness(协议 v0.5.1):buddy 模式下根据 cc state 驱动 lamp motion
    # 用 _orig_play_keyframes 绕过 gate(否则 buddy 模式自己的 motion 也被拦)
    buddy_awareness = BuddyAwareness(
        play_keyframes_fn=_orig_play_keyframes,
        daemon_url=daemon_url,
        mode_box=device_mode_box,
        poll_interval=2.0,
    )

    # ── 身份识别 + 记忆 + 智能体 ────────────────────────────────────────────────
    identity_mem = IdentityMemory()

    # SQLite 会话历史(首次启动时迁移旧 archive)
    history_db = HistoryDB()
    archive_path = Path.home() / ".lelamp" / "memories.archive.json"
    if archive_path.exists():
        migrated = history_db.migrate_from_archive(archive_path)
        if migrated > 0:
            logger.info("📦 已迁移 %d 条历史到 SQLite", migrated)

    mem = MemoryStream(history_db=history_db)
    mem.start()
    ltm = LongTermMemory(api_key=os.environ.get("EMBEDDING_API_KEY", ""))
    agent = SoulAgent(
        motion_svc, rgb_svc, tts, mem, llm,
        identity_memory=identity_mem,
        longterm_memory=ltm,
        review_llm=review_llm,
        history_db=history_db,
        personality_prompt_extra=_PERSONA_FACE_PROMPT,
    )

    # ── 开机动作 ──────────────────────────────────────────────────────────────
    motion_svc.play_emotion("wake_up")
    rgb_svc.dispatch("solid", (180, 180, 255))
    # 临时禁用 play_arc:Agent B 的固件 v0.5.0 在收到 play_arc 命令后会触发 reset
    # 循环,让 USB CDC 端点失效。改用 set_face 单条命令避开。等 Agent B 修固件
    # 后再启用 morning/goodnight 剧本。
    face_director.set_face("warm_smile")  # 起始表情(替代 morning arc)
    await asyncio.sleep(2.2)

    # ── 摄像头 ────────────────────────────────────────────────────────────────
    camera_device = int(os.environ.get("CAMERA_DEVICE", "0"))
    camera_flip = os.environ.get("CAMERA_FLIP", "").lower() in ("1", "true", "yes")
    camera = CameraCapture(device_id=camera_device, flip=camera_flip)
    camera.start()
    agent.set_camera(camera)

    # ── 视觉变化检测 ──────────────────────────────────────────────────────────
    visual_monitor = VisualMonitor(
        camera=camera,
        motion_agent=motion_svc,
        on_change=agent.on_visual_change,
    )
    visual_monitor.start()

    # ── 智能耳朵(OmniEar:本地 VAD + HTTP Omni)───────────────────────────────
    ear = OmniEar(
        api_key=os.environ.get("DASHSCOPE_API_KEY"),
        silence_sec=float(os.environ.get("OMNI_SILENCE_SEC", "1.2")),
        input_device=int(os.environ["AUDIO_INPUT_DEVICE"]) if os.environ.get("AUDIO_INPUT_DEVICE") else None,
        identity_memory=identity_mem,
    )

    # 包装 on_audio_event:用户语音先过关键词 arc 触发,再走原 SoulAgent 处理
    _orig_on_audio = agent.on_audio_event

    async def on_audio_event_with_face(event):
        if getattr(event, "text", ""):
            face_director.maybe_arc_from_user_text(event.text)
        await _orig_on_audio(event)

    ear.on_event = on_audio_event_with_face

    # AEC:TTS 播放时静音麦克风,播放结束恢复
    tts.on_play_start = ear.mute
    tts.on_play_end = ear.unmute

    await ear.start()
    await buddy_awareness.start()
    await tts.speak("呼——我醒来了。")

    # ── 开机环境扫描 ──────────────────────────────────────────────────────────
    logger.info("📡 开始开机环境扫描")
    await agent.boot_scan()

    logger.info("✨ 小Q persona 模式已启动(Ctrl-C 退出)")

    try:
        await agent.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("收到退出信号")
    finally:
        # 临时禁用 play_arc(同上,Agent B 固件 bug 待修)
        face_director.set_face("sleep")  # 退场表情(替代 goodnight arc)
        await agent.shutdown()
        # 先停 BuddyAwareness 避免 motion_svc.stop() 后还调 play_keyframes
        await buddy_awareness.stop()
        visual_monitor.stop()
        camera.stop()
        await ear.stop()
        await tts.stop()
        motion_svc.stop_recording()
        motion_svc.stop()
        rgb_svc.stop()
        history_db.close()
        face_director.close()
        logger.info("🌙 小Q 已休眠")


if __name__ == "__main__":
    asyncio.run(main())
