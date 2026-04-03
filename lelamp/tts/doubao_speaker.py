"""
豆包 TTS 模块 (火山引擎)
使用豆包大模型语音合成 API
文档: https://www.volcengine.com/docs/6561/1257584
"""
import asyncio
import gzip
import json
import logging
import shutil
import subprocess
import uuid
import time
from typing import Callable, Optional, List, AsyncGenerator
from dataclasses import dataclass
import os

import websockets

logger = logging.getLogger(__name__)


@dataclass
class TTSRequest:
    """TTS 请求"""
    text: str
    voice_type: str = "BV001_streaming"
    emotion: str = "happy"
    speed_ratio: float = 1.0
    volume_ratio: float = 1.0
    pitch_ratio: float = 1.0
    interrupt: bool = False


class DoubaoTTS:
    """
    豆包 TTS 引擎
    使用火山引擎 WebSocket API 进行流式语音合成
    """

    # 火山引擎 TTS WebSocket 地址
    WS_URL = "wss://openspeech.bytedance.com/api/v1/tts/ws_binary"

    def __init__(self,
                 appid: str,
                 token: str,
                 cluster: str = "volcano_tts",
                 voice_type: str = "BV001_streaming",
                 emotion: str = "happy",
                 speed_ratio: float = 1.0,
                 volume_ratio: float = 1.0,
                 pitch_ratio: float = 1.0):
        """
        Args:
            appid: 应用 ID
            token: Access Token (从火山引擎控制台获取)
            cluster: 集群 ID
            voice_type: 声音类型
            emotion: 情感类型
            speed_ratio: 语速倍率 0.8-1.2
            volume_ratio: 音量倍率 0.1-3.0
            pitch_ratio: 音调倍率 0.1-3.0
        """
        self.appid = appid
        self.token = token
        self.cluster = cluster
        self.voice_type = voice_type
        self.emotion = emotion
        self.speed_ratio = speed_ratio
        self.volume_ratio = volume_ratio
        self.pitch_ratio = pitch_ratio

    def _construct_request(self, text: str, reqid: str, emotion: str | None = None) -> bytes:
        """
        构建 TTS 请求

        Args:
            text: 要合成的文本
            reqid: 请求 ID
            emotion: 情绪，None 则使用默认值

        Returns:
            gzip 压缩后的请求数据
        """
        payload = {
            "app": {
                "appid": self.appid,
                "token": self.token,
                "cluster": self.cluster
            },
            "user": {
                "uid": "lelamp_user"
            },
            "audio": {
                "voice_type": self.voice_type,
                "encoding": "mp3",
                "speed_ratio": self.speed_ratio,
                "volume_ratio": self.volume_ratio,
                "pitch_ratio": self.pitch_ratio,
                "emotion": emotion or self.emotion
            },
            "request": {
                "reqid": reqid,
                "text": text,
                "text_type": "plain",
                "operation": "submit"
            }
        }

        logger.info("🎵 TTS 请求: text=%r emotion=%s voice=%s", text[:30], emotion or self.emotion, self.voice_type)

        # 压缩 payload
        payload_bytes = json.dumps(payload).encode('utf-8')
        compressed = gzip.compress(payload_bytes)

        # 火山引擎 TTS 二进制协议 header (4 bytes):
        #   Byte 0: version=1 (高4位) | header_size=1 (低4位, 单位4字节, 即4字节头)
        #   Byte 1: msg_type=1 (高4位, full client request) | flags=0 (低4位)
        #   Byte 2: serial=1 (高4位, JSON) | compression=1 (低4位, gzip)
        #   Byte 3: reserved=0
        header = bytes([0x11, 0x10, 0x11, 0x00])
        # payload size (4 bytes, big endian)
        size = len(compressed).to_bytes(4, 'big')

        return header + size + compressed

    async def synthesize(self, text: str, max_retries: int = 3, emotion: str | None = None) -> Optional[bytes]:
        """
        流式合成语音，失败时自动重试（最多 max_retries 次）

        Args:
            text: 要合成的文本
            max_retries: 最大重试次数
            emotion: 情绪，None 则使用默认值

        Returns:
            MP3 音频数据
        """
        if not text.strip():
            return None

        for attempt in range(max_retries):
            result = await self._synthesize_once(text, emotion=emotion)
            if result is not None:
                return result
            if attempt < max_retries - 1:
                wait = 1.0 * (attempt + 1)
                logger.warning(f"⚠️ 豆包 TTS 第 {attempt + 1} 次失败，{wait:.0f}s 后重试...")
                await asyncio.sleep(wait)

        logger.error(f"❌ 豆包 TTS 重试 {max_retries} 次后仍失败")
        return None

    async def _synthesize_once(self, text: str, emotion: str | None = None) -> Optional[bytes]:
        """单次合成尝试"""
        reqid = str(uuid.uuid4())
        audio_chunks = []

        try:
            logger.debug(f"🎵 豆包 TTS 开始合成: {text[:50]}...")

            auth_headers = {"Authorization": f"Bearer; {self.token}"}
            async with websockets.connect(self.WS_URL, additional_headers=auth_headers) as ws:
                # 发送合成请求
                request_data = self._construct_request(text, reqid, emotion=emotion)
                await ws.send(request_data)

                # 接收音频数据
                while True:
                    try:
                        # 设置接收超时
                        response = await asyncio.wait_for(ws.recv(), timeout=30.0)

                        if isinstance(response, bytes):
                            # 解析二进制协议 header (4 bytes)
                            if len(response) < 4:
                                continue

                            header = response[:4]
                            header_size = (header[0] & 0x0f) * 4  # 低4位 * 4 = 实际header字节数
                            msg_type = (header[1] >> 4) & 0x0f

                            payload_start = header_size  # 通常为 4

                            if msg_type == 0xb:
                                # Audio-only response: 4字节序列号 + 4字节size + 音频数据
                                if len(response) < payload_start + 8:
                                    continue
                                seq_num = int.from_bytes(
                                    response[payload_start:payload_start + 4],
                                    'big', signed=True
                                )
                                audio_size = int.from_bytes(
                                    response[payload_start + 4:payload_start + 8], 'big'
                                )
                                audio_data = response[payload_start + 8:payload_start + 8 + audio_size]
                                if audio_data:
                                    audio_chunks.append(audio_data)
                                    logger.debug(f"🎵 收到音频数据: {len(audio_data)} bytes")
                                # 负序列号表示最后一包
                                if seq_num < 0:
                                    logger.debug("✅ 豆包 TTS 合成完成")
                                    break

                            elif msg_type == 0x9:
                                # Full server response: 4字节序列号 + 4字节size + JSON payload
                                if len(response) < payload_start + 8:
                                    continue
                                payload_size = int.from_bytes(
                                    response[payload_start + 4:payload_start + 8], 'big'
                                )
                                payload_data = response[payload_start + 8:payload_start + 8 + payload_size]
                                compression = header[2] & 0x0f
                                if compression == 1:
                                    payload_data = gzip.decompress(payload_data)
                                result = json.loads(payload_data.decode('utf-8'))
                                code = result.get('code', -1)
                                if code == 1000:
                                    logger.debug("✅ 豆包 TTS 合成完成")
                                    break
                                else:
                                    logger.error(f"❌ 豆包 TTS 错误: {code} - {result.get('message', '')}")
                                    return None

                            elif msg_type == 0xf:
                                # Error response
                                if len(response) < payload_start + 8:
                                    continue
                                error_code = int.from_bytes(
                                    response[payload_start:payload_start + 4], 'big'
                                )
                                payload_size = int.from_bytes(
                                    response[payload_start + 4:payload_start + 8], 'big'
                                )
                                payload_data = response[payload_start + 8:payload_start + 8 + payload_size]
                                # 尝试解压 gzip
                                if payload_data[:2] == b'\x1f\x8b':
                                    try:
                                        payload_data = gzip.decompress(payload_data)
                                    except Exception:
                                        pass
                                try:
                                    error_info = json.loads(payload_data.decode('utf-8'))
                                    logger.error(f"❌ 豆包 TTS 错误: {error_code} - {error_info}")
                                except Exception:
                                    logger.error(f"❌ 豆包 TTS 错误: {error_code} - {payload_data}")
                                return None

                    except asyncio.TimeoutError:
                        logger.warning("⚠️ 豆包 TTS 接收超时")
                        break

            # 合并所有音频数据
            if audio_chunks:
                full_audio = b''.join(audio_chunks)
                logger.info(f"✅ 豆包 TTS 合成成功: {len(full_audio)} bytes")
                return full_audio
            else:
                logger.error("❌ 豆包 TTS 未收到音频数据")
                return None

        except Exception as e:
            logger.error(f"❌ 豆包 TTS 请求失败: {e}")
            return None

    async def synthesize_stream(self, text: str, emotion: str | None = None) -> AsyncGenerator[bytes, None]:
        """
        流式合成：WebSocket 每帧到达即 yield，首帧延迟约 0.3-0.8s。
        调用方可以在首帧到达时立即开始播放，无需等待全部合成完成。
        """
        if not text.strip():
            return

        reqid = str(uuid.uuid4())
        auth_headers = {"Authorization": f"Bearer; {self.token}"}

        try:
            async with websockets.connect(self.WS_URL, additional_headers=auth_headers) as ws:
                await ws.send(self._construct_request(text, reqid, emotion=emotion))

                while True:
                    try:
                        response = await asyncio.wait_for(ws.recv(), timeout=30.0)
                    except asyncio.TimeoutError:
                        logger.warning("⚠️ 豆包 TTS 流式接收超时")
                        break

                    if not isinstance(response, bytes) or len(response) < 4:
                        continue

                    header = response[:4]
                    header_size = (header[0] & 0x0f) * 4
                    msg_type = (header[1] >> 4) & 0x0f
                    ps = header_size  # payload start

                    if msg_type == 0xb:
                        # Audio-only frame
                        if len(response) < ps + 8:
                            continue
                        seq_num = int.from_bytes(response[ps:ps + 4], 'big', signed=True)
                        audio_size = int.from_bytes(response[ps + 4:ps + 8], 'big')
                        audio_data = response[ps + 8:ps + 8 + audio_size]
                        if audio_data:
                            yield audio_data
                        if seq_num < 0:
                            break  # 最后一帧

                    elif msg_type == 0x9:
                        # Full server response (正常结束)
                        break

                    elif msg_type == 0xf:
                        # Error
                        logger.error("❌ 豆包 TTS 流式合成收到错误帧")
                        break

        except Exception as e:
            logger.error(f"❌ 豆包 TTS 流式合成失败: {e}")


class DoubaoTTSPlayer:
    """
    豆包 TTS 播放器 —— 合成/播放双流水线

    架构：
      speak() → _text_queue → [_synth_worker] → _audio_queue → [_play_worker]

    播放 A 时合成 B 已在并发进行，段间停顿 ≈ 0。
    """

    MAX_TEXT_LENGTH = 400
    MAX_AUDIO_QUEUE = 6  # 预合成音频的最大缓存段数

    def __init__(self,
                 appid: str,
                 token: str,
                 cluster: str = "volcano_tts",
                 voice_type: str = "BV001_streaming",
                 emotion: str = "happy",
                 speed_ratio: float = 1.0,
                 volume_ratio: float = 1.0,
                 pitch_ratio: float = 1.0,
                 player_cmd: str = "afplay",
                 max_queue_size: int = 10):
        self.tts = DoubaoTTS(
            appid=appid,
            token=token,
            cluster=cluster,
            voice_type=voice_type,
            emotion=emotion,
            speed_ratio=speed_ratio,
            volume_ratio=volume_ratio,
            pitch_ratio=pitch_ratio
        )
        self.player_cmd = player_cmd
        self.max_queue_size = max_queue_size

        # 检测 mpg123：有则用流式管道模式，无则退回双流水线 afplay 模式
        self._use_mpg123 = bool(shutil.which("mpg123"))
        if self._use_mpg123:
            logger.info("🎵 检测到 mpg123，启用流式播放模式（首字延迟更低）")
        else:
            logger.info("🎵 未检测到 mpg123，使用 afplay 双流水线模式")

        # 文本输入队列（speak() 写入）
        self._text_queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue_size)
        # 音频就绪队列（合成完成后写入，播放协程消费）
        # 元素格式：(audio_bytes, text_preview) 或 None（跳过）
        self._audio_queue: asyncio.Queue = asyncio.Queue(maxsize=self.MAX_AUDIO_QUEUE)

        self._playing = False
        self._interrupt_event = asyncio.Event()

        # 合成完成信号（用于外部精确计时）
        self._synthesis_done = asyncio.Event()
        self.last_synthesis_ms: float = 0.0

        # 全链路时间戳（绝对 time.time()，None 表示尚未到达）
        self.first_synth_start: Optional[float] = None   # synth_worker 开始合成首段
        self.first_synth_end: Optional[float] = None     # 首段合成完成、音频就绪
        self.first_play_start: Optional[float] = None    # play_worker 开始播放首段

        import tempfile
        self._temp_dir = tempfile.mkdtemp(prefix="lelamp_doubao_")

        self._synth_task: Optional[asyncio.Task] = None
        self._play_task: Optional[asyncio.Task] = None
        self._stream_task: Optional[asyncio.Task] = None  # mpg123 流式模式
        self._running = False

        # AEC 回调：播放开始/结束时通知外部（如 OmniEar mute/unmute）
        self.on_play_start: Optional[Callable[[], None]] = None
        self.on_play_end: Optional[Callable[[], None]] = None

    @staticmethod
    def _clean_markdown(text: str) -> str:
        """去除 Markdown 格式及 TTS 无法朗读的字符"""
        import re
        # 去掉代码块 ```...```
        text = re.sub(r'```[\s\S]*?```', '', text)
        # 去掉行内代码 `code`
        text = re.sub(r'`[^`]*`', '', text)
        # 去掉图片 ![alt](url)
        text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
        # 链接 [text](url) → 保留文字
        text = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', text)
        # 去掉粗体/斜体/删除线 **text** / *text* / ~~text~~
        text = re.sub(r'~~(.*?)~~', r'\1', text)
        text = re.sub(r'\*{1,3}(.*?)\*{1,3}', r'\1', text)
        text = re.sub(r'_{1,2}(.*?)_{1,2}', r'\1', text)
        # 去掉标题 # ## ###
        text = re.sub(r'^#{1,6}\s*', '', text, flags=re.MULTILINE)
        # 去掉水平分割线
        text = re.sub(r'^[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)
        # 去掉表格行（含 | 符号）
        text = re.sub(r'^\|.*\|$', '', text, flags=re.MULTILINE)
        # 去掉列表符号 - / * / 数字. 开头
        text = re.sub(r'^\s*[-*]\s+', '', text, flags=re.MULTILINE)
        text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
        # 去掉引用符号 >
        text = re.sub(r'^\s*>\s*', '', text, flags=re.MULTILINE)
        # 去掉 emoji（4字节辅助平面字符）
        text = re.sub(r'[\U00010000-\U0010ffff]', '', text)
        # 去掉常见符号区 emoji（Misc Symbols / Dingbats / 变体选择符）
        text = re.sub(r'[\u2600-\u26FF\u2700-\u27BF\uFE0F\u20E3]', '', text)
        # 合并多个空行为单个换行
        text = re.sub(r'\n{2,}', '\n', text)
        # 去掉行首行尾空白
        text = '\n'.join(line.strip() for line in text.splitlines())
        return text.strip()

    def _split_text(self, text: str, max_length: int = MAX_TEXT_LENGTH) -> List[str]:
        """去 Markdown 后分段"""
        import re
        text = self._clean_markdown(text)

        if len(text) <= max_length:
            return [text] if text else []

        segments = []
        current = ""

        # 按中文句子边界分割
        sentences = re.split(r'([。！？；\n])', text)

        for i in range(0, len(sentences), 2):
            sentence = sentences[i]
            if i + 1 < len(sentences):
                sentence += sentences[i + 1]

            if len(current) + len(sentence) <= max_length:
                current += sentence
            else:
                if current:
                    segments.append(current)
                current = sentence

        if current:
            segments.append(current)

        # 强制分割超长段落
        final_segments = []
        for seg in segments:
            while len(seg) > max_length:
                final_segments.append(seg[:max_length])
                seg = seg[max_length:]
            if seg:
                final_segments.append(seg)

        return final_segments if final_segments else [text[:max_length]]

    async def start(self):
        """启动播放器"""
        self._running = True
        if self._use_mpg123:
            # 流式模式：synthesize_stream → 直接 pipe mpg123，首帧 ~0.3-0.8s 即出声
            self._synth_task = asyncio.create_task(self._stream_worker())
            # _play_worker 不启动，mpg123 已在 _stream_worker 中驱动
            logger.info("豆包 TTS 播放器已启动（流式合成 / mpg123 stdin）")
        else:
            # 批量模式：afplay 双流水线兜底
            self._synth_task = asyncio.create_task(self._synth_worker())
            self._play_task = asyncio.create_task(self._play_worker())
            logger.info("豆包 TTS 播放器已启动（双流水线 / afplay 临时文件）")

    async def stop(self):
        """停止播放器"""
        self._running = False
        self.interrupt()

        for q in (self._text_queue, self._audio_queue):
            while not q.empty():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    break

        for task in (self._synth_task, self._play_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._cleanup_temp_files()
        logger.info("豆包 TTS 播放器已停止")

    def _cleanup_temp_files(self):
        """清理临时文件"""
        try:
            for f in os.listdir(self._temp_dir):
                try:
                    os.remove(os.path.join(self._temp_dir, f))
                except:
                    pass
            os.rmdir(self._temp_dir)
        except Exception as e:
            logger.warning(f"清理临时文件失败: {e}")

    async def speak(self, text: str, interrupt: bool = False, emotion: str | None = None) -> bool:
        """将文本送入合成队列

        Args:
            text: 要合成的文本
            interrupt: 是否打断当前播放
            emotion: 本次语音的情绪，None 则使用默认值
        """
        if not text.strip():
            return False

        self._synthesis_done.clear()
        segments = self._split_text(text.strip())
        use_emotion = emotion or self.tts.emotion

        try:
            if interrupt:
                self.interrupt()
                for q in (self._text_queue, self._audio_queue):
                    while not q.empty():
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            break

            for segment in segments:
                request = TTSRequest(
                    text=segment,
                    voice_type=self.tts.voice_type,
                    emotion=use_emotion,
                    speed_ratio=self.tts.speed_ratio,
                    volume_ratio=self.tts.volume_ratio,
                    pitch_ratio=self.tts.pitch_ratio,
                )
                await self._text_queue.put(request)

            return True

        except Exception as e:
            logger.error(f"添加 TTS 请求失败: {e}")
            return False

    def interrupt(self):
        """打断当前播放，并清空待合成的文本队列"""
        self._interrupt_event.set()
        drained = 0
        while not self._text_queue.empty():
            try:
                self._text_queue.get_nowait()
                drained += 1
            except asyncio.QueueEmpty:
                break
        logger.info(f"🛑 TTS 被打断，已清除 {drained} 条待合成文本")

    def is_playing(self) -> bool:
        """是否正在播放"""
        return self._playing

    def reset_timing(self):
        """每次新消息开始前调用，清空时间戳"""
        self.first_synth_start = None
        self.first_synth_end = None
        self.first_play_start = None
        self._synthesis_done.clear()
        self._interrupt_event.clear()  # 防止旧 interrupt 干扰新会话

    async def wait_synthesized(self, timeout: float = 30.0) -> float:
        """等待第一段 TTS 合成完成，返回合成耗时 ms"""
        try:
            await asyncio.wait_for(self._synthesis_done.wait(), timeout=timeout)
            return self.last_synthesis_ms
        except asyncio.TimeoutError:
            return 0.0

    # ── 流式合成+播放协程（mpg123 模式）────────────────────────────────────────
    async def _do_synth_to_queue(self, text: str, queue: asyncio.Queue, emotion: str | None = None):
        """在独立 Task 中运行 synthesize_stream，将音频帧放入 queue，结束后放 None sentinel。"""
        try:
            async for frame in self.tts.synthesize_stream(text, emotion=emotion):
                await queue.put(frame)
        except Exception as e:
            logger.error(f"❌ 合成帧收集失败: {e}")
        finally:
            await queue.put(None)  # sentinel：通知消费方合成结束

    async def _stream_worker(self):
        """
        流式合成 + mpg123 播放，并在播放当前段期间预合成下一段。
        """
        # next_item: 预合成好的下一段 (request, frames_queue, synth_task)
        next_item: Optional[tuple] = None

        while self._running:
            # ── 取请求：优先用预合成的，否则阻塞等队列 ──────────────────────
            if next_item is not None:
                cur_request, cur_frames_q, cur_synth_task = next_item
                next_item = None
            else:
                try:
                    cur_request = await asyncio.wait_for(self._text_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if self._interrupt_event.is_set():
                    continue
                cur_frames_q: asyncio.Queue = asyncio.Queue()
                cur_synth_task = asyncio.create_task(
                    self._do_synth_to_queue(cur_request.text, cur_frames_q, emotion=cur_request.emotion)
                )

            # ── 启动 mpg123 ───────────────────────────────────────────────
            is_first = (self.first_synth_start is None)
            if is_first:
                self.first_synth_start = time.time()
            synth_start = time.time()

            try:
                proc = await asyncio.create_subprocess_exec(
                    "mpg123", "-q", "-",
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception as e:
                logger.error(f"❌ mpg123 启动失败: {e}")
                cur_synth_task.cancel()
                continue

            self._playing = True
            if self.on_play_start:
                self.on_play_start()
            first_frame = True
            total_bytes = 0

            # ── 边收帧边 pipe 给 mpg123 ───────────────────────────────────
            try:
                while not self._interrupt_event.is_set():
                    try:
                        frame = await asyncio.wait_for(cur_frames_q.get(), timeout=0.5)
                    except asyncio.TimeoutError:
                        if cur_synth_task.done() and cur_frames_q.empty():
                            break
                        continue

                    if frame is None:  # sentinel：合成结束
                        break

                    if first_frame:
                        first_frame = False
                        if self.first_play_start is None:
                            self.first_play_start = time.time()
                        if is_first:
                            self.first_synth_end = time.time()
                            self.last_synthesis_ms = (self.first_synth_end - synth_start) * 1000
                            self._synthesis_done.set()
                            logger.info(f"🚀 TTS 流式首帧: {self.last_synthesis_ms:.0f} ms")

                    total_bytes += len(frame)
                    try:
                        proc.stdin.write(frame)
                        await asyncio.sleep(0)
                        if self._interrupt_event.is_set():
                            break
                        await proc.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        break

            except Exception as e:
                logger.error(f"❌ 流式 TTS pipe 失败: {e}")

            # ── 关闭 stdin，mpg123 继续播完其缓冲区 ──────────────────────
            try:
                proc.stdin.close()
            except Exception:
                pass

            # ── 预合成下一段（与 mpg123 播放剩余缓冲并行） ────────────────
            if not self._interrupt_event.is_set():
                try:
                    next_req = self._text_queue.get_nowait()
                    next_frames_q: asyncio.Queue = asyncio.Queue()
                    next_synth_task = asyncio.create_task(
                        self._do_synth_to_queue(next_req.text, next_frames_q, emotion=next_req.emotion)
                    )
                    next_item = (next_req, next_frames_q, next_synth_task)
                    logger.debug(f"📦 预合成下一段: {next_req.text[:20]}...")
                except asyncio.QueueEmpty:
                    pass

            # ── 等 mpg123 播完 ────────────────────────────────────────────
            if self._interrupt_event.is_set():
                try:
                    proc.stdin.transport.abort()
                except Exception:
                    pass
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    proc.kill()
                if next_item:
                    next_item[2].cancel()
                    next_item = None
            else:
                deadline = time.time() + 60.0
                while proc.returncode is None:
                    if self._interrupt_event.is_set():
                        try:
                            proc.stdin.transport.abort()
                        except Exception:
                            pass
                        proc.terminate()
                        try:
                            await asyncio.wait_for(proc.wait(), timeout=1.0)
                        except asyncio.TimeoutError:
                            proc.kill()
                        if next_item:
                            next_item[2].cancel()
                            next_item = None
                        break
                    if time.time() > deadline:
                        proc.kill()
                        break
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=0.05)
                        break  # 自然结束
                    except asyncio.TimeoutError:
                        continue

            self._playing = False
            if self.on_play_end:
                self.on_play_end()
            elapsed = (time.time() - synth_start) * 1000
            logger.info(f"🔊 豆包 TTS 流式播完: {elapsed:.0f} ms, {total_bytes} bytes")

    # ── 批量合成协程（afplay 兜底模式）────────────────────────────────────────
    async def _synth_worker(self):
        """
        从文本队列取一段 → synthesize → 推入音频队列。
        与播放协程并发运行，播放 A 的同时合成 B。
        """
        while self._running:
            try:
                request = await asyncio.wait_for(self._text_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if self._interrupt_event.is_set():
                continue

            is_first = (self.first_synth_start is None)
            if is_first:
                self.first_synth_start = time.time()

            synth_start = time.time()
            audio_data = await self.tts.synthesize(request.text, emotion=request.emotion)
            synth_time = (time.time() - synth_start) * 1000

            if self._interrupt_event.is_set():
                logger.info(f"🛑 TTS 合成完成但已被打断，丢弃 {len(audio_data) if audio_data else 0} bytes")
                continue

            if is_first:
                self.first_synth_end = time.time()
                self.last_synthesis_ms = synth_time
                self._synthesis_done.set()

            if audio_data is None:
                logger.error("TTS 合成失败，跳过此段")
                await self._audio_queue.put(None)
                continue

            logger.info(f"🔊 豆包 TTS 合成完成: {synth_time:.0f} ms, {len(audio_data)} bytes")
            await self._audio_queue.put((audio_data, request.text[:20]))

    # ── 播放协程 ──────────────────────────────────────────────────────────────
    async def _play_worker(self):
        """
        从音频队列取已合成音频 → 播放。
        串行播放，与合成并发（synth 播 A 时已在合成 B）。
        """
        while self._running:
            try:
                item = await asyncio.wait_for(self._audio_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if item is None:
                continue

            if self._interrupt_event.is_set():
                while not self._audio_queue.empty():
                    try:
                        self._audio_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                continue

            audio_data, preview = item

            # 贪婪合并：把 audio_queue 中已就绪的后续段全部拼入同一批次
            extra = 0
            while True:
                try:
                    nxt = self._audio_queue.get_nowait()
                    if nxt is None:
                        break
                    audio_data += nxt[0]
                    extra += 1
                except asyncio.QueueEmpty:
                    break
            if extra:
                logger.debug(f"📦 贪婪合并 {extra + 1} 段音频")

            if self.first_play_start is None:
                self.first_play_start = time.time()

            self._playing = True
            if self.on_play_start:
                self.on_play_start()
            try:
                if self._use_mpg123:
                    await self._play_via_mpg123(audio_data)
                else:
                    temp_file = os.path.join(self._temp_dir, f"tts_{time.monotonic_ns()}.mp3")
                    with open(temp_file, "wb") as f:
                        f.write(audio_data)
                    await self._play_via_afplay(temp_file)
                    try:
                        os.remove(temp_file)
                    except Exception:
                        pass
            finally:
                self._playing = False
                if self.on_play_end:
                    self.on_play_end()

    async def _play_via_mpg123(self, audio_data: bytes):
        """用 mpg123 stdin 播放：无临时文件，启动开销小"""
        try:
            proc = await asyncio.create_subprocess_exec(
                "mpg123", "-q", "-",
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            comm_task = asyncio.ensure_future(proc.communicate(audio_data))

            while True:
                if self._interrupt_event.is_set():
                    comm_task.cancel()
                    try:
                        proc.stdin.transport.abort()
                    except Exception:
                        pass
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        proc.kill()
                    return
                if comm_task.done():
                    break
                await asyncio.sleep(0.02)

            try:
                await comm_task
            except (asyncio.CancelledError, BrokenPipeError, ConnectionResetError):
                pass

            if proc.returncode is not None and proc.returncode != 0:
                logger.warning(f"mpg123 异常退出: {proc.returncode}")

        except Exception as e:
            logger.error(f"mpg123 播放失败: {e}")

    async def _play_via_afplay(self, audio_file: str):
        """afplay 播放（兜底路径）"""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.player_cmd, audio_file,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            while True:
                if self._interrupt_event.is_set():
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        proc.kill()
                    return
                if proc.returncode is not None:
                    break
                await asyncio.sleep(0.05)

        except Exception as e:
            logger.error(f"afplay 播放失败: {e}")
