import os
import csv
import math
import random
import time
import signal
import logging
import multiprocessing as mp
from multiprocessing import queues as mp_queues
from typing import Any, List


from lelamp.follower import LeLampFollowerConfig, LeLampFollower
from .motion_scripts import MOTION_REGISTRY, breath_cycle, idle_glance

_BREATH_START_DELAY = 2.0   # 静止多久后开始呼吸（秒）
_GLANCE_MIN         = 40.0  # 两次随机探索的最短间隔（秒）
_GLANCE_MAX         = 80.0  # 最长间隔


# ─────────────────────────────────────────────
# 子进程顶层函数（spawn context 要求在模块顶层定义）
# ─────────────────────────────────────────────

def _play(robot, last_pos: dict, recording_name: str, fps: int, logger) -> dict:
    """电机播放逻辑（在子进程内执行），返回新的 last_pos"""
    recordings_dir = os.path.join(os.path.dirname(__file__), "..", "..", "recordings")

    # 程序化动作
    if recording_name in MOTION_REGISTRY:
        frames = MOTION_REGISTRY[recording_name](last_pos)
        n = len(frames)
        logger.info(f"Playing scripted motion: {recording_name} ({n} frames, expected {n/fps*1000:.0f}ms)")
        dt = 1.0 / fps
        motion_start = time.perf_counter()
        late_frames = 0
        max_late_ms = 0.0
        for i, frame in enumerate(frames):
            robot.bus.sync_write("Goal_Position", frame)
            sleep = motion_start + (i + 1) * dt - time.perf_counter()
            if sleep > 0.001:
                time.sleep(sleep)
            else:
                late_ms = -sleep * 1000
                late_frames += 1
                if late_ms > max_late_ms:
                    max_late_ms = late_ms
        total_ms = (time.perf_counter() - motion_start) * 1000
        logger.info(
            f"Finished: {recording_name} | "
            f"total={total_ms:.0f}ms expected={n/fps*1000:.0f}ms | "
            f"late_frames={late_frames}/{n} max_late={max_late_ms:.0f}ms"
        )
        return frames[-1]

    # CSV 回退
    csv_path = os.path.join(recordings_dir, f"{recording_name}.csv")
    if not os.path.exists(csv_path):
        logger.error(f"Recording not found: {csv_path}")
        return last_pos

    with open(csv_path, 'r') as f:
        actions = list(csv.DictReader(f))

    logger.info(f"Playing {len(actions)} actions from {recording_name}")
    replay_start = time.perf_counter()
    record_start = float(actions[0]['timestamp'])

    for row in actions:
        action = {k: float(v) for k, v in row.items() if k != 'timestamp'}
        robot.send_action(action)
        elapsed_record = float(row['timestamp']) - record_start
        elapsed_replay = time.perf_counter() - replay_start
        sleep_time = elapsed_record - elapsed_replay
        if sleep_time > 0:
            time.sleep(sleep_time)

    logger.info(f"Finished playing: {recording_name}")
    return last_pos


def _motor_process(port: str, lamp_id: str, fps: int,
                   cmd_queue, idle_event, ready_event, log_level: int):
    """子进程入口：有独立 GIL，主进程负载不影响此处"""
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    logging.basicConfig(
        level=log_level,
        format="%(asctime)s,%(msecs)03d [%(name)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger("service.motors")

    robot_config = LeLampFollowerConfig(port=port, id=lamp_id)
    robot = LeLampFollower(robot_config)
    robot.connect(calibrate=False)
    last_pos = robot.bus.sync_read("Present_Position")
    logger.info(f"Motor process ready, initial pos: { {k: round(v,1) for k,v in last_pos.items()} }")

    ready_event.set()
    idle_event.set()

    dt = 1.0 / fps
    idle_since       = time.perf_counter()
    next_glance      = random.uniform(_GLANCE_MIN, _GLANCE_MAX)
    breath_frames    = None   # 当前呼吸帧序列
    breath_loop_start= None   # 绝对起始时间（用于绝对时钟补偿）
    breath_frame_idx = 0

    while True:
        # ── 非阻塞取命令 ─────────────────────────────────────────────────────
        got_cmd = True
        try:
            cmd = cmd_queue.get_nowait()
        except mp_queues.Empty:
            got_cmd = False

        if got_cmd:
            if cmd is None:  # stop sentinel
                break
            # 重置空闲状态，执行指令
            idle_since        = time.perf_counter()
            breath_frames     = None
            breath_loop_start = None
            breath_frame_idx  = 0
            next_glance       = random.uniform(_GLANCE_MIN, _GLANCE_MAX)
            event_type, payload = cmd
            try:
                if event_type == "play":
                    last_pos = _play(robot, last_pos, payload, fps, logger)
                else:
                    logger.warning(f"Unknown event type: {event_type}")
            except Exception as e:
                logger.error(f"Error handling {event_type}: {e}")
            finally:
                idle_event.set()
            continue  # 立即检查下一命令，不 sleep

        # ── 空闲行为 ─────────────────────────────────────────────────────────
        idle_dur = time.perf_counter() - idle_since
        if idle_dur >= _BREATH_START_DELAY:
            # 首次进入呼吸：生成帧序列，记录绝对起始时间
            if breath_frames is None:
                breath_frames     = breath_cycle(last_pos)
                breath_loop_start = time.perf_counter()
                breath_frame_idx  = 0
                logger.info(f"Idle breathing started ({len(breath_frames)} frames/cycle)")

            # 循环播放 breath_cycle 帧（smoothstep 插值，与其他动作一致）
            frame = breath_frames[breath_frame_idx % len(breath_frames)]
            robot.bus.sync_write("Goal_Position", frame)

            # 绝对时钟补偿：单帧抖动由下一帧自动吸收
            target_time = breath_loop_start + (breath_frame_idx + 1) * dt
            breath_frame_idx += 1

            # 随机探索
            next_glance -= dt
            if next_glance <= 0:
                logger.info("Idle: random glance")
                frames = idle_glance(last_pos)
                motion_start = time.perf_counter()
                for i, frame in enumerate(frames):
                    robot.bus.sync_write("Goal_Position", frame)
                    sleep = motion_start + (i + 1) * dt - time.perf_counter()
                    if sleep > 0.001:
                        time.sleep(sleep)
                # 探索结束后重新生成呼吸帧（从当前位置出发）
                breath_frames     = breath_cycle(last_pos)
                breath_loop_start = time.perf_counter()
                breath_frame_idx  = 0
                next_glance       = random.uniform(_GLANCE_MIN, _GLANCE_MAX)
                continue

            sleep = target_time - time.perf_counter()
            if sleep > 0.001:
                time.sleep(sleep)
        else:
            time.sleep(dt)

    # 断开时屏蔽 SIGINT 防止中断
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    robot.disconnect()
    logger.info("Motor process stopped")


# ─────────────────────────────────────────────
# 主进程侧接口（与原 MotorsService 保持一致）
# ─────────────────────────────────────────────

class MotorsService:
    def __init__(self, port: str, lamp_id: str, fps: int = 30):
        self.port = port
        self.lamp_id = lamp_id
        self.fps = fps
        self.logger = logging.getLogger("service.motors")
        self._process = None
        self._cmd_queue = None
        self._idle_event = None

    def start(self):
        ctx = mp.get_context("spawn")
        self._cmd_queue = ctx.Queue()
        self._idle_event = ctx.Event()
        ready_event = ctx.Event()

        log_level = logging.getLogger().level or logging.INFO
        self._process = ctx.Process(
            target=_motor_process,
            args=(self.port, self.lamp_id, self.fps,
                  self._cmd_queue, self._idle_event, ready_event, log_level),
            daemon=True,
        )
        self._process.start()
        if not ready_event.wait(timeout=15):
            self.logger.warning("Motor process did not become ready within 15s")
        self.logger.info("Motors service started")

    def stop(self, timeout: float = 5.0):
        if self._process and self._process.is_alive():
            self._cmd_queue.put(None)  # stop sentinel
            self._process.join(timeout=timeout)
            if self._process.is_alive():
                self.logger.warning("Motor process did not stop in time, terminating")
                self._process.terminate()
        self.logger.info("Motors service stopped")

    def dispatch(self, event_type: str, payload: Any, priority: Any = None):
        if not self.is_running:
            self.logger.warning(f"Motor process not running, ignoring {event_type}")
            return
        # 清空旧命令（保持单事件槽语义，新事件覆盖旧事件）
        while not self._cmd_queue.empty():
            try:
                self._cmd_queue.get_nowait()
            except mp_queues.Empty:
                break
        self._idle_event.clear()  # 立即标记为忙碌，避免 wait_until_idle 竞态
        self._cmd_queue.put((event_type, payload))

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def wait_until_idle(self, timeout: float = None) -> bool:
        """等待当前动作完成。返回 True=已空闲，False=超时"""
        if self._idle_event is None:
            return True
        return self._idle_event.wait(timeout=timeout)

    def get_available_recordings(self) -> List[str]:
        recordings_dir = os.path.join(os.path.dirname(__file__), "..", "..", "recordings")
        if not os.path.exists(recordings_dir):
            return []
        return sorted(f[:-4] for f in os.listdir(recordings_dir) if f.endswith(".csv"))
