"""手动循环测试 16 个 face + 6 个 arc,验证 v0.5.0 链路。

不依赖 motion / TTS / ASR / 摄像头 — 只需 daemon 在跑。

用法:
  ./scripts/start_persona.sh   # 起 daemon(main_persona 失败也没关系,daemon 在跑就行)
  或:
    nohup uv run python -m lelamp.companion --no-arm > /tmp/lelamp_daemon.log 2>&1 &

  然后:
    uv run python scripts/test_faces.py            # 全部 face + 全部 arc
    uv run python scripts/test_faces.py faces      # 只测 face
    uv run python scripts/test_faces.py arcs       # 只测 arc
    uv run python scripts/test_faces.py warm_smile # 切单个 face

设备需在 persona 模式(若在 buddy 模式,长按屏左下角切回)。
"""
import sys
import time
from pathlib import Path

# 让 standalone 脚本能 import lelamp.*(scripts/ 不在默认 sys.path)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lelamp.persona import FaceDirector  # noqa: E402

FACES = [
    "neutral", "focus", "idle_watch", "sleep", "content", "warm_smile",
    "listen", "comfort", "wink", "smirk", "side_eye", "peek",
    "surprised", "blush", "sleepy", "love",
]
ARCS = ["morning", "pat", "tease", "goodnight", "noticed", "comfort"]


def main() -> int:
    fd = FaceDirector()
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"

    if arg in FACES:
        print(f"→ 单 face: {arg}")
        ok = fd.set_face(arg)
        print(f"  {'✓' if ok else '✗'} 发送 {'成功' if ok else '失败(daemon 没起?)'}")
        return 0 if ok else 1

    if arg in ARCS:
        print(f"▶ 单 arc: {arg}")
        ok = fd.play_arc(arg)
        print(f"  {'✓' if ok else '✗'} 发送 {'成功' if ok else '失败(daemon 没起?)'}")
        return 0 if ok else 1

    if arg in ("all", "faces"):
        print("循环 16 个 face,每个 3 秒...")
        for f in FACES:
            print(f"  → {f}")
            fd.set_face(f)
            time.sleep(3)

    if arg in ("all", "arcs"):
        print("\n循环 6 个 arc,每个 7 秒(给剧本播完时间)...")
        for a in ARCS:
            print(f"  ▶ {a}")
            fd.play_arc(a)
            time.sleep(7)

    if arg not in ("all", "faces", "arcs") and arg not in FACES and arg not in ARCS:
        print(f"未知参数 {arg!r}")
        print("\n可选 face:")
        for f in FACES:
            print(f"  {f}")
        print("\n可选 arc:")
        for a in ARCS:
            print(f"  {a}")
        return 1

    fd.close()
    print("\n完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
