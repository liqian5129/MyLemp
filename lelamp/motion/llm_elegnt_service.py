"""
LLM ELEGNT 运动服务（第1阶段：模板层）

替换 elegnt_service.py 的手写正弦函数，改用关键帧插值。
dispatch 接口与 ELEGNTService 完全兼容。

架构：
  dispatch("emotion", {...})
      ↓ 模板生成 MotionKeyframes（第1阶段）/ LLM生成（第2阶段+）
      ↓ MotionExecutor.from_frames(...)
      ↓ 30fps 控制循环 executor.step(t_elapsed) → send_action
      → 动作结束后返回 idle 呼吸循环（复用 elegnt_service.py）
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

import numpy as np

from lelamp.motion.motion_executor import MotionExecutor, Q_REST, JOINT_NAMES
from lelamp.motion.templates import template_generate, EMOTION_INDEX, MotionKeyframes
from lelamp.motion.elegnt_service import ELEGNTService  # 用于 idle 呼吸过渡

logger = logging.getLogger(__name__)

# ── 默认配置 ──────────────────────────────────────────────────────────────────

DEFAULT_FPS = 30
IDLE_INTENSITY = 0.25


class LLMELEGNTService:
    """
    LLM 驱动的情绪运动服务（当前：模板层）。

    用法与 ELEGNTService 完全一致：
        service = LLMELEGNTService(port="...", lamp_id="...")
        service.start()
        service.dispatch("emotion", {"emotion": "happy", "intensity": 0.8})
        service.stop()
    """

    def __init__(self, port: str, lamp_id: str, fps: int = DEFAULT_FPS, **kwargs):
        self.port = port
        self.lamp_id = lamp_id
        self.fps = fps
        self._dt = 1.0 / fps

        # idle 过渡层：复用旧服务的呼吸函数（内部不启动机器人连接）
        self._idle_service = ELEGNTService.__new__(ELEGNTService)
        ELEGNTService.__init__(self._idle_service, port=port, lamp_id=lamp_id, fps=fps)

        # 运动状态
        self._executor: Optional[MotionExecutor] = None
        self._exec_start: float = 0.0
        self._mode = "idle"         # "idle" | "playing"
        self._emotion = "idle"
        self._intensity = IDLE_INTENSITY

        # attention / attitude（保持接口兼容）
        self._attention_target = 0.0
        self._attitude = 0.0

        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.robot = None

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    def start(self):
        from lelamp.follower import LeLampFollowerConfig, LeLampFollower
        config = LeLampFollowerConfig(port=self.port, id=self.lamp_id)
        self.robot = LeLampFollower(config)
        self.robot.connect(calibrate=False)

        # 同步给 idle_service 绑定同一个 robot（共享连接）
        self._idle_service.robot = self.robot
        self._idle_service._emotion_start_t = time.time()

        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="llm-elegnt-motion"
        )
        self._thread.start()
        logger.info("✨ LLMELEGNTService 已启动（模板模式）port=%s", self.port)

    def stop(self, timeout: float = 3.0):
        self._running = False
        if self._thread:
            self._thread.join(timeout=timeout)
        if self.robot:
            self.robot.disconnect()
            self.robot = None
        logger.info("🛑 LLMELEGNTService 已停止")

    # ── 公共接口（与 ELEGNTService 完全兼容）─────────────────────────────────

    def dispatch(self, event_type: str, payload: Any):
        if event_type == "emotion":
            if isinstance(payload, dict):
                emotion   = payload.get("emotion", "idle")
                intensity = float(payload.get("intensity", 0.8))
                attn      = payload.get("attention_yaw", None)
            else:
                emotion, intensity, attn = str(payload), 0.8, None

            self._trigger_emotion(emotion, intensity)
            if attn is not None:
                self.set_attention(float(attn))

        elif event_type == "attention":
            self.set_attention(float(payload))
        elif event_type == "attitude":
            self.set_attitude(float(payload))

    def set_attention(self, yaw: float):
        with self._lock:
            self._attention_target = float(np.clip(yaw, -5.0, 14.0))
        self._idle_service.set_attention(yaw)

    def set_attitude(self, score: float):
        with self._lock:
            self._attitude = float(np.clip(score, -1.0, 1.0))
        self._idle_service.set_attitude(score)

    def get_available_emotions(self):
        return EMOTION_INDEX

    # ── 内部：情绪触发 ────────────────────────────────────────────────────────

    def _trigger_emotion(self, emotion: str, intensity: float):
        """生成关键帧并切换到 playing 模式"""
        if emotion not in EMOTION_INDEX:
            logger.warning("未知情绪 '%s'，可用: %s", emotion, EMOTION_INDEX)
            return

        # 读取当前关节角度作为 f0
        f0 = self._get_current_q()

        # 生成关键帧（第1阶段：模板；第2阶段后：LLM）
        kf: MotionKeyframes = template_generate(emotion, intensity)
        logger.info("🎭 触发情绪 %s @ %.1f  intent=%s", emotion, intensity, kf.intent)

        executor = MotionExecutor.from_frames(
            f0=f0,
            f1=kf.f1,
            f2=kf.f2,
            f3=Q_REST,
            duration=kf.duration,
            accel_ratio=kf.accel_ratio,
            asymmetry=kf.asymmetry,
        )

        with self._lock:
            self._executor    = executor
            self._exec_start  = time.perf_counter()
            self._mode        = "playing"
            self._emotion     = emotion
            self._intensity   = intensity

    def _get_current_q(self) -> np.ndarray:
        """从机器人读取当前关节角度；连接失败时返回 Q_REST"""
        try:
            if self.robot is not None:
                obs = self.robot.get_observation()
                return np.array([
                    obs.get("base_yaw.pos",    Q_REST[0]),
                    obs.get("base_pitch.pos",  Q_REST[1]),
                    obs.get("elbow_pitch.pos", Q_REST[2]),
                    obs.get("wrist_roll.pos",  Q_REST[3]),
                    obs.get("wrist_pitch.pos", Q_REST[4]),
                ], dtype=np.float32)
        except Exception as e:
            logger.debug("读取关节角度失败（使用 Q_REST）: %s", e)
        return Q_REST.copy()

    # ── 30fps 控制循环 ────────────────────────────────────────────────────────

    def _run_loop(self):
        logger.info("🔄 LLMELEGNTService 控制循环启动 @ %d fps", self.fps)
        while self._running:
            t0 = time.perf_counter()

            with self._lock:
                mode      = self._mode
                executor  = self._executor
                exec_start = self._exec_start

            if mode == "playing" and executor is not None:
                t_elapsed = time.perf_counter() - exec_start
                action = executor.step(t_elapsed)

                if action is None:
                    # 动作结束，切回 idle
                    with self._lock:
                        self._mode = "idle"
                        self._executor = None
                    self._idle_service._emotion_start_t = time.time()
                    logger.debug("▶ 动作结束，回到 idle 呼吸")
                else:
                    self._send(action)

            else:
                # idle 模式：复用旧服务的呼吸+attention+attitude 逻辑
                self._run_idle_step()

            elapsed = time.perf_counter() - t0
            sleep_t = self._dt - elapsed
            if sleep_t > 0:
                time.sleep(sleep_t)

    def _run_idle_step(self):
        """复用 ELEGNTService 的单步 idle 逻辑"""
        try:
            # 借用旧服务的内部状态计算 idle 帧
            svc = self._idle_service
            svc._intensity = IDLE_INTENSITY
            svc._emotion = "idle"

            import time as _time
            emotion_t = _time.time() - svc._emotion_start_t

            from lelamp.motion.elegnt_service import HOME_POSE, JOINT_LIMITS, JOINTS, _compute_emotion, _ease_in_out
            import math

            pose = dict(HOME_POSE)
            e: dict = {}

            # Attention
            attn_yaw = svc._step_attention(svc._attention_target, self._dt)
            e["base_yaw"] = attn_yaw

            # Attitude
            for k, v in svc._attitude_delta(svc._attitude).items():
                e[k] = e.get(k, 0.0) + v

            # Idle 情绪
            e_cur = _compute_emotion("idle", emotion_t)
            for k, v in e_cur.items():
                e[k] = e.get(k, 0.0) + v

            action: dict = {}
            for joint in JOINTS:
                key = f"{joint}.pos" if not joint.endswith(".pos") else joint
                jname = key.removesuffix(".pos")
                t_val = pose[key] + IDLE_INTENSITY * e.get(jname, 0.0)
                lo, hi = JOINT_LIMITS[jname]
                action[key] = max(lo, min(hi, t_val))

            self._send(action)
        except Exception as exc:
            logger.debug("idle step 异常: %s", exc)

    def _send(self, action: dict):
        try:
            if self.robot is not None:
                self.robot.send_action(action)
        except Exception as exc:
            logger.warning("send_action 失败: %s", exc)
