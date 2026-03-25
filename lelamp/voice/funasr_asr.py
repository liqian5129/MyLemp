"""
本地 FunASR 批处理语音识别（PTT 模式）
录音期间只收集 PCM，松开按键后一次性推理，无流式分块开销。

接口与 AliyunStreamASR / PrewarmedASR 兼容：start() / send_audio() / stop() / health()
"""
import logging
import os
import threading
import time
from typing import Callable, Optional

# 禁止 ModelScope 发起网络请求，完全使用本地缓存
os.environ.setdefault("MODELSCOPE_OFFLINE", "1")

logger = logging.getLogger(__name__)


def _resolve_device(device: str) -> str:
    if device != "auto":
        return device
    try:
        import torch
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"


class FunasrASR:
    """
    本地 FunASR 批处理 ASR（PTT 场景）。

    工作流程：
    - start()       重置 PCM 缓冲区
    - send_audio()  追加 PCM bytes（仅内存操作，极轻量）
    - stop()        将全部 PCM 一次性推理，返回完整文字

    模型在后台线程加载，不阻塞 asyncio 事件循环。
    首次 start() 若模型未就绪，会等待加载完成。
    """

    def __init__(self,
                 device: str = "auto",
                 model: str = "paraformer-zh-streaming",
                 chunk_size_frames: int = 10,
                 on_ready: Optional[Callable] = None):
        self._device_str = _resolve_device(device)
        self._model_name = model
        self._chunk_size_frames = chunk_size_frames
        self._chunk_samples = chunk_size_frames * 960  # warmup 用

        self._model = None
        self._model_loaded = False
        self._model_error: Optional[str] = None
        self._model_ready = threading.Event()
        self._on_ready_cb: Optional[Callable] = on_ready

        # 录音缓冲（每次 start() 重置）
        self._pcm_buffer = bytearray()
        self._result_callback: Optional[Callable] = None
        self._lock = threading.Lock()

        # 后台加载模型，不阻塞主线程
        threading.Thread(target=self._load_model, daemon=True,
                         name="funasr-loader").start()

    # ------------------------------------------------------------------
    # 模型加载
    # ------------------------------------------------------------------

    def _load_model(self):
        logger.info(f"⏳ FunASR: 正在加载模型 {self._model_name} "
                    f"(device={self._device_str})...")
        t0 = time.time()
        try:
            from funasr import AutoModel
            self._model = AutoModel(
                model=self._model_name,
                device=self._device_str,
                disable_update=True,
                punc_model="ct-punc",
            )
            self._model_loaded = True
            elapsed = time.time() - t0
            logger.info(f"✅ FunASR 模型加载完成 ({elapsed:.1f}s), "
                        f"device={self._device_str}")
            self._warmup()
            if self._on_ready_cb:
                try:
                    self._on_ready_cb(time.time() - t0)
                except Exception:
                    pass
        except Exception as e:
            self._model_error = str(e)
            logger.error(f"❌ FunASR 模型加载失败: {e}")
        finally:
            self._model_ready.set()

    def _warmup(self):
        """静音推理预热 MPS kernel，避免首次识别延迟高。"""
        try:
            import numpy as np
            silence = np.zeros(self._chunk_samples, dtype=np.float32)
            self._model.generate(
                input=silence, cache={}, is_final=True,
                chunk_size=[0, self._chunk_size_frames, 5],
                encoder_chunk_look_back=4, decoder_chunk_look_back=1,
            )
            logger.info("🔥 FunASR MPS warmup 完成")
        except Exception as e:
            logger.warning(f"FunASR warmup 失败（不影响使用）: {e}")

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    @property
    def is_ready(self) -> bool:
        return self._model_loaded

    def health(self) -> str:
        if self._model_loaded:
            return "ready"
        if self._model_error:
            return "unavailable"
        return "initializing"

    def start(self, on_result: Optional[Callable] = None):
        """重置缓冲区，立即返回（模型不需要在此时就绪）。"""
        with self._lock:
            self._pcm_buffer = bytearray()
            self._result_callback = on_result
        logger.info("🎤 FunASR: 开始录音缓冲")

    def send_audio(self, pcm_bytes: bytes):
        """追加 PCM 数据到缓冲区（内存操作，不做任何推理）。"""
        with self._lock:
            self._pcm_buffer.extend(pcm_bytes)

    def stop(self, timeout: float = 10.0) -> str:
        """
        对全部录音做一次批量推理，返回完整识别文字。
        典型耗时：~300-600ms（3-4s 音频，MPS RTF≈0.15）
        """
        t0 = time.time()

        # 模型若还在后台加载，在此等待（录音已结束，等待不影响用户体验）
        if not self._model_loaded:
            logger.info("⏳ FunASR 模型加载中，等待完成...")
            self._model_ready.wait(timeout=120)
        if not self._model_loaded:
            logger.error(f"FunASR 模型加载失败: {self._model_error}")
            return ""

        with self._lock:
            pcm_bytes = bytes(self._pcm_buffer)
            self._pcm_buffer = bytearray()

        if not pcm_bytes:
            return ""

        try:
            import numpy as np
            audio = (np.frombuffer(pcm_bytes, dtype=np.int16)
                     .astype(np.float32) / 32768.0)

            result = self._model.generate(
                input=audio,
                cache={},                         # 批处理：每次重置 cache
                is_final=True,
                chunk_size=[0, self._chunk_size_frames, 5],
                encoder_chunk_look_back=4,
                decoder_chunk_look_back=1,
            )

            text = ""
            if result and isinstance(result, list):
                text = result[0].get("text", "").strip()

        except Exception as e:
            logger.error(f"❌ FunASR 推理失败: {e}", exc_info=True)
            text = ""

        elapsed = (time.time() - t0) * 1000
        logger.info(f"🛑 FunASR stop(): {elapsed:.0f}ms, 结果='{text}'")
        return text

    def is_active(self) -> bool:
        return self._model_loaded


# ------------------------------------------------------------------
# 工厂函数
# ------------------------------------------------------------------

def create_local_asr(device: str = "auto",
                     model: str = "paraformer-zh-streaming",
                     chunk_size_frames: int = 10,
                     on_ready: Optional[Callable] = None) -> FunasrASR:
    """创建本地 FunASR 实例（模型后台异步加载）。"""
    return FunasrASR(device=device, model=model,
                     chunk_size_frames=chunk_size_frames,
                     on_ready=on_ready)
