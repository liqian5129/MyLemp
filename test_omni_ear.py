"""OmniEar 独立验证脚本

对着麦克风说话，观察 AudioEvent 输出。
验证：ASR 转录、情绪识别、intent 分类、环境音感知。

用法：
    uv run python test_omni_ear.py
    uv run python test_omni_ear.py --threshold 0.3 --silence 600

环境变量：
    DASHSCOPE_API_KEY  必填
"""
import asyncio
import argparse
import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
# Omni RT 客户端的 debug 日志也打开，方便排查
logging.getLogger("lelamp.voice.qwen_omni_rt").setLevel(logging.DEBUG)

from lelamp.soul.audio_event import AudioEvent
from lelamp.soul.omni_ear import OmniEar

# 统计
event_count = 0


async def on_event(event: AudioEvent):
    global event_count
    event_count += 1

    # 高亮显示
    if event.is_speech:
        print(f"\n{'='*60}")
        print(f"  #{event_count}  语音事件")
        print(f"  转录:   {event.text}")
        print(f"  情绪:   {event.emotion}")
        print(f"  环境:   {event.audio_env or '(无)'}")
        print(f"{'='*60}\n")
    else:
        print(f"\n  #{event_count}  环境事件: {event.audio_env or event.emotion}")
        print()


async def main(args):
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        print("错误: 请设置 DASHSCOPE_API_KEY 环境变量")
        sys.exit(1)

    ear = OmniEar(
        api_key=api_key,
        vad_threshold=args.threshold,
        silence_duration_ms=args.silence,
        input_device=args.device,
    )
    ear.on_event = on_event

    print(f"\n连接 Omni RT (threshold={args.threshold}, silence={args.silence}ms)...")
    await ear.start()

    print("已连接! 对着麦克风说话，Ctrl-C 退出。\n")
    print("测试建议:")
    print("  1. 说一句新指令:  '帮我找一下杯子'       → intent=new_request")
    print("  2. 紧接着补充:    '绿色的那个'           → intent=supplement")
    print("  3. 取消:         '算了不找了'            → intent=cancel")
    print("  4. 闲聊:         '今天天气真好'          → intent=chat")
    print("  5. 保持安静，观察环境音变化               → is_speech=False")
    print()

    # 测试 mute/unmute
    print("  输入 m 回车 = mute, u 回车 = unmute (模拟 TTS 播放)\n")

    loop = asyncio.get_running_loop()

    # 在后台线程读 stdin，支持 m/u 控制
    def stdin_reader():
        while True:
            try:
                line = input()
            except EOFError:
                break
            cmd = line.strip().lower()
            if cmd == "m":
                ear.mute()
                print("  [已 mute]")
            elif cmd == "u":
                ear.unmute()
                print("  [已 unmute]")

    import threading
    threading.Thread(target=stdin_reader, daemon=True).start()

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        print(f"\n共收到 {event_count} 个事件")
        await ear.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OmniEar 独立验证")
    parser.add_argument("--threshold", type=float, default=0.5, help="VAD 灵敏度 0~1")
    parser.add_argument("--silence", type=int, default=800, help="静默判定时长 ms")
    parser.add_argument("--device", type=int, default=None, help="麦克风设备 ID")
    args = parser.parse_args()

    asyncio.run(main(args))
