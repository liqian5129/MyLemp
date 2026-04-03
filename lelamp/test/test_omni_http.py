"""测试 HTTP Omni API：本地 VAD 录音 → HTTP qwen3-omni-flash → 结构化输出。

验证：转录、情绪识别、意图分类、环境音感知是否能通过 function calling 可靠获取。

用法：
    uv run python test_omni_http.py
    uv run python test_omni_http.py --silence 1.5 --device 1

环境变量：
    DASHSCOPE_API_KEY  必填
"""
import argparse
import asyncio
import base64
import io
import logging
import os
import struct
import sys
import threading
import time
import wave

import numpy as np
import sounddevice as sd
from dotenv import load_dotenv
from openai import AsyncOpenAI

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logger = logging.getLogger("test_omni_http")

# ── 音频参数 ──────────────────────────────────────────────────────────────────
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_FRAMES = 1600  # 100ms @ 16kHz

# ── HTTP Omni 分析 ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "你是小Q的耳朵。分析用户的语音，调用 report_event 报告你听到的内容。\n"
    "判断：\n"
    "- text: 转录用户说的话\n"
    "- emotion: 从语气判断说话者情绪：neutral/happy/tired/frustrated/curious/sad\n"
    "- intent: 判断意图：new_request（新指令）/ supplement（补充）/ cancel（取消）/ chat（闲聊）\n"
    "- audio_env: 描述背景环境音（安静、键盘声、多人说话等）\n"
)

REPORT_TOOL = {
    "type": "function",
    "function": {
        "name": "report_event",
        "description": "报告音频分析结果",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "用户说的话（转录）"},
                "emotion": {
                    "type": "string",
                    "enum": ["neutral", "happy", "tired", "frustrated", "curious", "sad"],
                    "description": "说话者情绪",
                },
                "intent": {
                    "type": "string",
                    "enum": ["new_request", "supplement", "cancel", "chat"],
                    "description": "用户意图",
                },
                "audio_env": {"type": "string", "description": "环境音描述"},
            },
            "required": ["text", "emotion", "intent", "audio_env"],
        },
    },
}


def pcm_to_wav_base64(pcm_bytes: bytes, sample_rate: int = 16000) -> str:
    """int16 PCM → WAV → base64 字符串。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # int16 = 2 bytes
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_bytes)
    return base64.b64encode(buf.getvalue()).decode("ascii")


async def analyze_audio(client: AsyncOpenAI, pcm_bytes: bytes) -> dict:
    """发送音频到 HTTP Omni API，返回结构化结果。"""
    wav_b64 = pcm_to_wav_base64(pcm_bytes)
    duration = len(pcm_bytes) / (SAMPLE_RATE * 2)  # int16 = 2 bytes/sample
    logger.info("发送音频到 HTTP Omni (%.1f 秒, %.1f KB)...", duration, len(pcm_bytes) / 1024)

    t0 = time.monotonic()
    resp = await client.chat.completions.create(
        model="qwen3-omni-flash",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": f"data:audio/wav;base64,{wav_b64}",
                            "format": "wav",
                        },
                    },
                ],
            },
        ],
        modalities=["text"],
        tools=[REPORT_TOOL],
        tool_choice={"type": "function", "function": {"name": "report_event"}},
    )
    elapsed = time.monotonic() - t0
    logger.info("HTTP Omni 响应耗时: %.2f 秒", elapsed)

    # 解析 function call
    msg = resp.choices[0].message
    if msg.tool_calls:
        import json
        args = json.loads(msg.tool_calls[0].function.arguments)
        return args
    else:
        # 没有 function call，打印原始回复
        logger.warning("未返回 function call，原始回复: %s", msg.content)
        return {"text": msg.content or "", "emotion": "neutral", "intent": "chat", "audio_env": ""}


# ── 本地 VAD 录音 ─────────────────────────────────────────────────────────────

def calibrate_threshold(device=None, duration=2.0) -> float:
    """采集环境噪底，返回自适应阈值。"""
    print(f"采集环境噪底（{duration} 秒），请保持安静...")
    rms_samples = []

    def collect(indata, frames, time_info, status):
        rms_samples.append(float(np.sqrt(np.mean(indata ** 2))))

    kwargs = dict(samplerate=SAMPLE_RATE, channels=1, dtype=np.float32,
                  blocksize=CHUNK_FRAMES, callback=collect)
    if device is not None:
        kwargs["device"] = device

    with sd.InputStream(**kwargs):
        time.sleep(duration)

    if rms_samples:
        noise_floor = float(np.mean(rms_samples))
        threshold = max(noise_floor * 3.0, 0.015)
        print(f"噪底={noise_floor:.4f} → 阈值={threshold:.4f}")
        return threshold
    return 0.030


async def main(args):
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        print("错误: 请设置 DASHSCOPE_API_KEY 环境变量")
        sys.exit(1)

    client = AsyncOpenAI(
        api_key=api_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
    )

    # 校准噪底
    threshold = calibrate_threshold(device=args.device)

    # VAD 状态
    lock = threading.Lock()
    state = {"value": "idle"}  # idle / collecting / analyzing
    audio_chunks: list[bytes] = []
    last_voice_t = [0.0]
    collect_start = [0.0]
    loop = asyncio.get_running_loop()
    event_count = [0]
    muted = [False]

    async def on_speech_segment(pcm_bytes: bytes, speech_duration: float):
        """语音段结束，发送到 HTTP Omni 分析。"""
        event_count[0] += 1
        n = event_count[0]
        print(f"\n--- #{n} 语音段 (时长 {speech_duration:.1f}s) ---")

        try:
            result = await analyze_audio(client, pcm_bytes)
            print(f"  转录:   {result.get('text', '(无)')}")
            print(f"  情绪:   {result.get('emotion', '?')}")
            print(f"  意图:   {result.get('intent', '?')}")
            print(f"  环境音: {result.get('audio_env', '?')}")
            print()
        except Exception as exc:
            print(f"  分析失败: {exc}\n")
        finally:
            with lock:
                state["value"] = "idle"

    def audio_callback(indata, frames, time_info, status):
        if muted[0]:
            with lock:
                if state["value"] == "collecting":
                    state["value"] = "idle"
                    audio_chunks.clear()
            return

        rms = float(np.sqrt(np.mean(indata ** 2)))
        pcm = (indata * 32767).astype(np.int16).tobytes()
        now = time.monotonic()

        with lock:
            s = state["value"]

        if s == "idle":
            if rms > threshold:
                with lock:
                    state["value"] = "collecting"
                    audio_chunks.clear()
                    audio_chunks.append(pcm)
                    last_voice_t[0] = now
                    collect_start[0] = now
                logger.debug("语音开始")

        elif s == "collecting":
            if rms > threshold:
                last_voice_t[0] = now
            audio_chunks.append(pcm)

            silence = now - last_voice_t[0]
            duration = now - collect_start[0]

            if silence >= args.silence or duration >= 30.0:
                with lock:
                    state["value"] = "analyzing"
                    chunks_copy = list(audio_chunks)
                    audio_chunks.clear()
                    speech_dur = last_voice_t[0] - collect_start[0]

                if speech_dur < 0.3:
                    logger.debug("片段过短 (%.2fs)，丢弃", speech_dur)
                    with lock:
                        state["value"] = "idle"
                    return

                pcm_data = b"".join(chunks_copy)
                asyncio.run_coroutine_threadsafe(
                    on_speech_segment(pcm_data, speech_dur),
                    loop,
                )

    # 开启麦克风
    stream_kwargs = dict(
        samplerate=SAMPLE_RATE, channels=1, dtype=np.float32,
        blocksize=CHUNK_FRAMES, callback=audio_callback,
    )
    if args.device is not None:
        stream_kwargs["device"] = args.device

    stream = sd.InputStream(**stream_kwargs)
    stream.start()

    print(f"\n已开始监听 (silence={args.silence}s)，对着麦克风说话，Ctrl-C 退出。")
    print("测试建议:")
    print('  1. 说 "帮我找一下杯子"     → intent=new_request, emotion=neutral')
    print('  2. 说 "今天好累啊"          → emotion=tired')
    print('  3. 说 "算了不找了"          → intent=cancel')
    print('  4. 说 "今天天气真好"        → intent=chat')
    print("  5. 制造键盘声再说话         → audio_env 应该变化")
    print()
    print("  输入 m 回车 = mute, u 回车 = unmute\n")

    def stdin_reader():
        while True:
            try:
                line = input()
            except EOFError:
                break
            cmd = line.strip().lower()
            if cmd == "m":
                muted[0] = True
                print("  [已 mute]")
            elif cmd == "u":
                muted[0] = False
                print("  [已 unmute]")

    threading.Thread(target=stdin_reader, daemon=True).start()

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        stream.stop()
        stream.close()
        print(f"\n共处理 {event_count[0]} 个语音段")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="测试 HTTP Omni (本地VAD + HTTP分析)")
    parser.add_argument("--silence", type=float, default=2.0, help="静默截断时长 (秒)")
    parser.add_argument("--device", type=int, default=None, help="麦克风设备 ID")
    args = parser.parse_args()

    asyncio.run(main(args))
