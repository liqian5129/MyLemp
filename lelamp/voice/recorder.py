"""
语音录音模块
使用 pynput 监听右 Alt 键 + sounddevice 录音
实时将音频流推送到 ASR
"""
import asyncio
import logging
import threading
import time
from typing import Callable, Optional
from dataclasses import dataclass
from enum import Enum, auto

import numpy as np
import sounddevice as sd
from pynput import keyboard

logger = logging.getLogger(__name__)


class RecordingState(Enum):
    """录音状态"""
    IDLE = auto()
    RECORDING = auto()
    PROCESSING = auto()


@dataclass
class VoiceSegment:
    """语音片段"""
    text: str
    duration_ms: float


class VoiceRecorder:
    """
    语音录音器

    按住右 Alt 键开始录音，松开结束
    实时将音频推送到 ASR 进行识别
    """

    def __init__(self,
                 asr_engine,
                 loop: Optional[asyncio.AbstractEventLoop] = None,
                 sample_rate: int = 16000,
                 channels: int = 1,
                 min_duration: float = 0.3,
                 trigger_key=keyboard.Key.alt_r):
        """
        Args:
            asr_engine: ASR 引擎实例 (如 FunasrASR)
            loop: 事件循环，用于跨线程调度协程
            sample_rate: 采样率，默认 16kHz
            channels: 声道数，默认单声道
            min_duration: 最短录音时长（秒），低于此值丢弃
            trigger_key: 触发录音的按键，默认右 Alt
        """
        self.asr = asr_engine
        self.loop = loop
        self.sample_rate = sample_rate
        self.channels = channels
        self.min_duration = min_duration
        self.trigger_key = trigger_key

        # 状态
        self.state = RecordingState.IDLE
        self._recording_start_time: Optional[float] = None
        self._audio_buffer: list[np.ndarray] = []

        # 回调
        self.on_text: Optional[Callable[[str], None]] = None
        self.on_segment: Optional[Callable[[VoiceSegment], None]] = None
        self.on_interrupt: Optional[Callable[[], None]] = None

        # 组件
        self._stream: Optional[sd.InputStream] = None
        self._keyboard_listener: Optional[keyboard.Listener] = None
        self._lock = threading.Lock()

        # 调试统计
        self._audio_callback_count = 0

        # wait_for_result 支持
        self._result_event: Optional[asyncio.Event] = None
        self._last_result: str = ""

    def _audio_callback(self, indata, frames, time_info, status):
        """sounddevice 音频回调"""
        if status:
            logger.warning(f"⚠️ 音频设备状态警告: {status}")

        if self.state == RecordingState.RECORDING:
            # 将 float32 转为 int16 PCM
            pcm_data = (indata * 32767).astype(np.int16).tobytes()

            self._audio_callback_count += 1

            if self._audio_callback_count % 50 == 0:
                logger.info(f"🎙️ 录音中... 已采集 {self._audio_callback_count} 包")

            self.asr.send_audio(pcm_data)
            self._audio_buffer.append(indata.copy())

    def _on_key_press(self, key):
        """按键按下"""
        if key == self.trigger_key and self.state == RecordingState.IDLE:
            if self.on_interrupt:
                try:
                    self.on_interrupt()
                except Exception as e:
                    logger.error(f"on_interrupt 回调错误: {e}")
            self._start_recording()

    def _on_key_release(self, key):
        """按键释放"""
        if key == self.trigger_key and self.state == RecordingState.RECORDING:
            self._stop_recording()

    def _start_recording(self):
        """开始录音"""
        with self._lock:
            if self.state != RecordingState.IDLE:
                return
            # 先设置 RECORDING，音频回调立即开始收包
            self.state = RecordingState.RECORDING
            self._recording_start_time = time.time()
            self._audio_buffer = []
            self._audio_callback_count = 0

        # asr.start() 移到锁外：PrewarmedASR 在 standby 未就绪时会等待，
        # 若在锁内调用会阻塞键盘释放事件导致 stop 无法及时触发
        logger.info("=" * 50)
        logger.info("🎤 开始录音...")
        logger.info("=" * 50)
        self.asr.start(on_result=self._on_asr_result)

    def _stop_recording(self):
        """停止录音"""
        with self._lock:
            if self.state != RecordingState.RECORDING:
                return

            self.state = RecordingState.PROCESSING
            duration = time.time() - self._recording_start_time

            logger.info("=" * 50)
            logger.info(f"🛑 停止录音，时长: {duration:.2f}s，采集 {self._audio_callback_count} 包")

            # 检查最短时长
            if duration < self.min_duration:
                logger.warning(f"⚠️ 录音时长过短 ({duration:.2f}s < {self.min_duration}s)，丢弃")
                self._cleanup_and_reset()
                return

            # 给 ASR 一点时间处理最后的数据
            logger.info("⏳ 等待 ASR 完成处理...")
            time.sleep(0.5)

            # 停止 ASR 并获取结果
            try:
                text = self.asr.stop()
            except Exception as e:
                logger.error(f"❌ 停止 ASR 失败: {e}")
                text = ""

            if text.strip():
                segment = VoiceSegment(text=text.strip(), duration_ms=duration * 1000)

                if self.on_segment:
                    try:
                        self.on_segment(segment)
                    except Exception as e:
                        logger.error(f"on_segment 回调错误: {e}")

                if self.on_text:
                    try:
                        # 如果有事件循环，使用 run_coroutine_threadsafe
                        if self.loop and asyncio.iscoroutinefunction(self.on_text):
                            asyncio.run_coroutine_threadsafe(
                                self.on_text(text.strip()),
                                self.loop
                            )
                        else:
                            self.on_text(text.strip())
                    except Exception as e:
                        logger.error(f"on_text 回调错误: {e}")

                # 触发 wait_for_result
                self._notify_result(text.strip())
            else:
                asr_health = self.asr.health() if hasattr(self.asr, "health") else "ready"
                if asr_health == "unavailable":
                    logger.warning("⚠️ 未识别到语音（ASR 服务不可用，等待自动恢复）")
                elif asr_health == "initializing":
                    logger.warning("⚠️ 未识别到语音（ASR 连接尚未就绪，请稍后再试）")
                else:
                    logger.warning("🤷 未识别到语音")
                # 空结果也触发，防止 wait_for_result 永久阻塞
                self._notify_result("")

            self.state = RecordingState.IDLE
            logger.info("=" * 50)

    def _cleanup_and_reset(self):
        """清理并重置状态"""
        try:
            self.asr.stop()
        except:
            pass
        self._audio_buffer = []
        self.state = RecordingState.IDLE
        self._notify_result("")
        logger.info("🧹 已清理并重置")

    def _on_asr_result(self, result):
        """ASR 中间结果回调"""
        logger.info(f"📝 ASR 中间结果: {result.text if hasattr(result, 'text') else result}")

    def _notify_result(self, text: str):
        """跨线程通知 wait_for_result"""
        if self._result_event is not None and self.loop is not None:
            self._last_result = text
            self.loop.call_soon_threadsafe(self._result_event.set)

    def start(self):
        """启动录音监听器"""
        # 启动音频流（保持打开，减少启动延迟）
        self._stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype=np.float32,
            callback=self._audio_callback,
            blocksize=1024,  # 约 64ms @ 16kHz
        )
        self._stream.start()

        # 启动键盘监听
        self._keyboard_listener = keyboard.Listener(
            on_press=self._on_key_press,
            on_release=self._on_key_release
        )
        self._keyboard_listener.start()

        logger.info(f"🎙️ 语音录音已启动，按住 [{self.trigger_key}] 说话")

    def stop(self):
        """停止录音监听器"""
        if self._keyboard_listener:
            self._keyboard_listener.stop()
            self._keyboard_listener = None

        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

        logger.info("🛑 语音录音已停止")

    def is_recording(self) -> bool:
        """是否正在录音"""
        return self.state == RecordingState.RECORDING

    def is_busy(self) -> bool:
        """是否正在录音或 ASR 推理中（RECORDING 或 PROCESSING 状态）"""
        return self.state != RecordingState.IDLE

    async def wait_for_result(self) -> str:
        """等待一次完整的 PTT 录音并返回识别文本"""
        if self.loop is None:
            self.loop = asyncio.get_event_loop()
        self._result_event = asyncio.Event()
        self._last_result = ""
        await self._result_event.wait()
        self._result_event = None
        return self._last_result


async def create_voice_recorder(asr_engine, loop=None, **kwargs) -> VoiceRecorder:
    """异步创建录音器"""
    recorder = VoiceRecorder(asr_engine, loop=loop, **kwargs)
    return recorder
