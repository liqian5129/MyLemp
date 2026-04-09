"""
豆包 TTS 情绪对比测试

两种方式对比：
1. audio.emotion 字段（当前方式）
2. SSML <emotion> 标签

运行：
    uv run python tests/test_tts_emotion.py
"""
import asyncio
import gzip
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

import websockets

WS_URL = "wss://openspeech.bytedance.com/api/v1/tts/ws_binary"

APPID = os.environ["DOUBAO_TTS_APPID"]
TOKEN = os.environ["DOUBAO_TTS_TOKEN"]
CLUSTER = os.environ.get("DOUBAO_TTS_CLUSTER", "volcano_tts")
VOICE = os.environ.get("DOUBAO_TTS_VOICE_TYPE", "zh_male_dongmanhaimian_mars_bigtts")

EMOTIONS = ["happy", "sad", "angry", "conniving", "gentle", "neutral"]
TEXT = "今天的天气真不错呀，我们出去玩好不好？"


def build_request(text: str, text_type: str = "plain", emotion: str = "") -> bytes:
    payload = {
        "app": {"appid": APPID, "token": TOKEN, "cluster": CLUSTER},
        "user": {"uid": "test"},
        "audio": {
            "voice_type": VOICE,
            "encoding": "mp3",
            "speed_ratio": 1.0,
            "volume_ratio": 1.0,
            "pitch_ratio": 1.0,
        },
        "request": {
            "reqid": str(uuid.uuid4()),
            "text": text,
            "text_type": text_type,
            "operation": "submit",
        },
    }
    if emotion:
        payload["audio"]["emotion"] = emotion

    data = json.dumps(payload).encode('utf-8')
    compressed = gzip.compress(data)

    # 火山引擎 TTS 二进制协议 header (4 bytes):
    #   Byte 0: version=1 | header_size=1 (4字节头)
    #   Byte 1: msg_type=1 (full client request) | flags=0
    #   Byte 2: serial=1 (JSON) | compression=1 (gzip)
    #   Byte 3: reserved=0
    header = bytes([0x11, 0x10, 0x11, 0x00])
    size = len(compressed).to_bytes(4, 'big')
    return header + size + compressed


async def synthesize(request_data: bytes) -> bytes | None:
    auth = {"Authorization": f"Bearer; {TOKEN}"}
    chunks = []
    try:
        async with websockets.connect(WS_URL, additional_headers=auth) as ws:
            await ws.send(request_data)
            while True:
                resp = await asyncio.wait_for(ws.recv(), timeout=30)
                if not isinstance(resp, bytes) or len(resp) < 4:
                    continue
                header_size = (resp[0] & 0x0F) * 4
                msg_type = (resp[1] >> 4) & 0x0F
                if msg_type == 0xB:  # audio
                    seq = resp[1] & 0x0F
                    chunks.append(resp[header_size:])
                    if seq == 3:  # last
                        break
                elif msg_type == 0xF:  # error
                    print(f"  ERROR: {resp[header_size:]}")
                    return None
    except Exception as e:
        print(f"  ERROR: {e}")
        return None
    return b"".join(chunks) if chunks else None


async def play(audio: bytes):
    proc = await asyncio.create_subprocess_exec(
        "mpg123", "-q", "-",
        stdin=asyncio.subprocess.PIPE,
    )
    proc.stdin.write(audio)
    proc.stdin.close()
    await proc.wait()


async def main():
    print(f"音色: {VOICE}")
    print(f"文本: {TEXT}")

    # ── 方式 1: audio.emotion 字段 ──
    print("\n" + "=" * 50)
    print("方式 1: audio.emotion 字段")
    print("=" * 50)
    for emo in EMOTIONS:
        print(f"\n▶ [{emo}] ", end="", flush=True)
        req = build_request(TEXT, text_type="plain", emotion=emo)
        audio = await synthesize(req)
        if audio:
            print(f"OK ({len(audio)} bytes)  播放中...")
            await play(audio)
        else:
            print("FAILED")
        await asyncio.sleep(0.3)

    # ── 方式 2: SSML <emotion> 标签 ──
    print("\n" + "=" * 50)
    print("方式 2: SSML <emotion> 标签")
    print("=" * 50)
    for emo in EMOTIONS:
        ssml = f'<speak><emotion type="{emo}">{TEXT}</emotion></speak>'
        print(f"\n▶ [{emo}] ssml={ssml[:60]}... ", end="", flush=True)
        req = build_request(ssml, text_type="ssml")
        audio = await synthesize(req)
        if audio:
            print(f"OK ({len(audio)} bytes)  播放中...")
            await play(audio)
        else:
            print("FAILED")
        await asyncio.sleep(0.3)

    print("\n\n测试完毕。对比两种方式哪种有情绪差异。")


if __name__ == "__main__":
    asyncio.run(main())
