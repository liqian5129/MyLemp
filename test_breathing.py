"""
呼吸效果测试脚本

直接启动 MotionAgent，让机器人立即进入呼吸状态（跳过 hold 期），
持续运行直到 Ctrl-C。

用法：
    uv run python test_breathing.py

可选参数（通过环境变量调）：
    BREATH_DURATION=60   # 运行秒数，默认永久
"""
import logging
import signal
import time

from dotenv import load_dotenv

from lelamp.motion.motion_agent import MotionAgent, HOME_POS
from lelamp.utils import find_serial_port

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)
logging.getLogger("lelamp.motion.motion_agent").setLevel(logging.DEBUG)
logger = logging.getLogger(__name__)


def main():
    port = find_serial_port()
    logger.info("🔌 串口: %s", port)

    agent = MotionAgent(port=port, lamp_id="lelamp", fps=30)
    agent.start()

    # 先运动到 HOME_POS，再立即开启呼吸（不等 30s hold）
    logger.info("▶ 运动到 HOME_POS...")
    agent.play_waypoint(HOME_POS, duration=2.0)
    time.sleep(2.5)  # 等动作完成

    logger.info("✅ HOME_POS 到位，Ctrl-C 退出")

    stop = False
    def _on_signal(*_):
        nonlocal stop
        stop = True
    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    while not stop:
        time.sleep(0.5)

    logger.info("🛑 停止")
    agent.stop()


if __name__ == "__main__":
    main()
