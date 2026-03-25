"""
ELEGNT 运动模式服务

复现论文: ELEGNT: Expressive and Functional Movement Design for Non-Anthropomorphic Robot
(Apple, arXiv 2501.12493)

核心公式: T(τ) = F(τ) + γ · E(τ)

四个表达维度:
  - Intention  (意图)：行动前的预期性动作
  - Attention  (注意力)：base_yaw 朝向目标追踪
  - Attitude   (态度)：整体姿态偏置，反映情绪极性
  - Emotion    (情绪)：解析函数驱动的动态关节波动

与录播模式完全独立，通过 MOTION_MODE=elegnt 启用。
"""
import logging
import math
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# ── 关节基准姿态（从录播数据中位值估算）────────────────────────────────────────
HOME_POSE: Dict[str, float] = {
    "base_yaw.pos":    0.0,
    "base_pitch.pos": -44.0,
    "elbow_pitch.pos": 75.0,
    "wrist_roll.pos":   0.0,
    "wrist_pitch.pos": 25.0,
}

# ── 安全关节限位（比录播实测范围略宽，留余量）──────────────────────────────────
JOINT_LIMITS: Dict[str, tuple] = {
    "base_yaw":    (-5.0,  14.0),
    "base_pitch":  (-68.0, -20.0),
    "elbow_pitch": (50.0,  100.0),
    "wrist_roll":  (-30.0, 15.0),
    "wrist_pitch": (-5.0,  68.0),
}

JOINTS = list(HOME_POSE.keys())


# ══════════════════════════════════════════════════════════════════════════════
# 情绪生成器（E 层）
# 每个函数接收时间 t（秒，从情绪切换时刻起），返回各关节的偏移量 delta
# ══════════════════════════════════════════════════════════════════════════════

def _sin(freq: float, t: float, phase: float = 0.0) -> float:
    return math.sin(2.0 * math.pi * freq * t + phase)


def _emotion_idle(t: float) -> Dict[str, float]:
    """待机呼吸：极轻微起伏，γ 建议 0.3"""
    return {
        "base_pitch":  2.0 * _sin(0.20, t),
        "wrist_pitch": 1.0 * _sin(0.15, t, math.pi / 3),
    }


def _emotion_excited(t: float) -> Dict[str, float]:
    """兴奋：快速弹跳，三个关节错相"""
    return {
        "base_pitch":  9.0 * _sin(3.0, t),
        "elbow_pitch": 6.0 * _sin(3.0, t, math.pi / 4),
        "wrist_pitch": 7.0 * _sin(3.0, t, math.pi / 2),
    }


def _emotion_curious(t: float) -> Dict[str, float]:
    """好奇：缓慢左右转头 + 腕部倾斜"""
    return {
        "base_yaw":   8.0 * _sin(0.4, t),
        "wrist_roll": 5.0 * _sin(0.4, t, math.pi / 6),
    }


def _emotion_happy(t: float) -> Dict[str, float]:
    """开心：轻柔弹跳"""
    return {
        "base_pitch":  5.0 * _sin(1.5, t),
        "elbow_pitch": 3.0 * _sin(1.5, t, math.pi / 5),
        "wrist_pitch": 3.0 * _sin(1.5, t, math.pi / 3),
    }


def _emotion_sad(t: float) -> Dict[str, float]:
    """伤心：前3秒渐渐低头 + 轻微颤抖"""
    droop = min(t / 3.0, 1.0)
    tremble = 1.5 * _sin(0.8, t) * droop
    return {
        "base_pitch":  -10.0 * droop + tremble,
        "elbow_pitch":  -8.0 * droop,
        "wrist_pitch":  -5.0 * droop,
    }


def _emotion_thinking(t: float) -> Dict[str, float]:
    """思考：腕部来回摆 + 头部轻微晃动"""
    return {
        "wrist_roll": 7.0 * _sin(0.5,  t),
        "base_yaw":   3.5 * _sin(0.35, t, math.pi / 4),
        "base_pitch": 2.0 * _sin(0.25, t),
    }


def _emotion_shy(t: float) -> Dict[str, float]:
    """害羞：低头侧倾，渐渐稳定"""
    settle = 1.0 - math.exp(-2.0 * t)
    tremble = 1.5 * _sin(0.4, t)
    return {
        "base_pitch": -8.0 * settle + tremble,
        "wrist_roll":  9.0 * settle,
        "base_yaw":   -4.0 * settle,
    }


def _emotion_shock(t: float) -> Dict[str, float]:
    """惊讶：急速后仰 + 衰减颤抖"""
    recoil  = 15.0 * math.exp(-4.0 * t)
    tremble =  4.0 * math.exp(-2.0 * t) * _sin(5.0, t)
    return {
        "base_pitch":  recoil + tremble,
        "elbow_pitch":  6.0 * math.exp(-3.0 * t),
        "wrist_pitch":  5.0 * math.exp(-2.5 * t) * _sin(4.0, t),
    }


_EMOTION_GENERATORS = {
    "idle":     _emotion_idle,
    "excited":  _emotion_excited,
    "curious":  _emotion_curious,
    "happy":    _emotion_happy,
    "sad":      _emotion_sad,
    "thinking": _emotion_thinking,
    "shy":      _emotion_shy,
    "shock":    _emotion_shock,
}

VALID_EMOTIONS = list(_EMOTION_GENERATORS.keys())


def _compute_emotion(emotion: str, t: float) -> Dict[str, float]:
    gen = _EMOTION_GENERATORS.get(emotion, _emotion_idle)
    return gen(t)


def _ease_in_out(x: float) -> float:
    """平滑三次插值"""
    return x * x * (3.0 - 2.0 * x)


# ══════════════════════════════════════════════════════════════════════════════
# ELEGNTService
# ══════════════════════════════════════════════════════════════════════════════

class ELEGNTService:
    """
    ELEGNT 运动服务，与录播模式完全独立。

    用法:
        service = ELEGNTService(port="/dev/cu.usbmodem...", lamp_id="lelamp")
        service.start()

        # LLM 工具调用触发
        service.dispatch("emotion", {"emotion": "excited", "intensity": 0.8})
        service.dispatch("attention", 5.0)   # yaw 目标角度
        service.dispatch("attitude",  0.6)   # -1 ~ +1

        service.stop()
    """

    def __init__(self, port: str, lamp_id: str, fps: int = 30):
        self.port = port
        self.lamp_id = lamp_id
        self.fps = fps
        self._dt = 1.0 / fps

        # 运动状态
        self._emotion: str = "idle"
        self._prev_emotion: str = "idle"
        self._intensity: float = 0.3
        self._emotion_start_t: float = 0.0
        self._transition_progress: float = 1.0
        self._transition_duration: float = 0.4

        # Attention 追踪
        self._attention_target: float = 0.0
        self._attention_current: float = 0.0

        # Attitude 偏置
        self._attitude: float = 0.0

        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self.robot = None

    # ── 生命周期 ──────────────────────────────────────────────────────────

    def start(self):
        from lelamp.follower import LeLampFollowerConfig, LeLampFollower
        config = LeLampFollowerConfig(port=self.port, id=self.lamp_id)
        self.robot = LeLampFollower(config)
        self.robot.connect(calibrate=False)
        logger.info(f"✅ ELEGNTService 已连接串口 {self.port}")

        self._running = True
        self._emotion_start_t = time.time()
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="elegnt-motion"
        )
        self._thread.start()
        logger.info("✨ ELEGNT 运动模式已启动")

    def stop(self, timeout: float = 3.0):
        self._running = False
        if self._thread:
            self._thread.join(timeout=timeout)
        if self.robot:
            self.robot.disconnect()
            self.robot = None
        logger.info("🛑 ELEGNT 运动模式已停止")

    # ── 公共接口 ──────────────────────────────────────────────────────────

    def dispatch(self, event_type: str, payload: Any):
        """统一分发接口，与 ServiceBase.dispatch 签名一致"""
        if event_type == "emotion":
            if isinstance(payload, dict):
                emotion   = payload.get("emotion",   "idle")
                intensity = float(payload.get("intensity", 0.8))
                attn      = payload.get("attention_yaw", None)
            else:
                emotion, intensity, attn = str(payload), 0.8, None
            self.set_emotion(emotion, intensity)
            if attn is not None:
                self.set_attention(float(attn))
        elif event_type == "attention":
            self.set_attention(float(payload))
        elif event_type == "attitude":
            self.set_attitude(float(payload))

    def set_emotion(self, emotion: str, intensity: float = 0.8):
        if emotion not in _EMOTION_GENERATORS:
            logger.warning(f"未知情绪 '{emotion}'，可用: {VALID_EMOTIONS}")
            emotion = "idle"
        with self._lock:
            if emotion == self._emotion:
                self._intensity = intensity
                return
            self._prev_emotion = self._emotion
            self._emotion = emotion
            self._intensity = intensity
            self._emotion_start_t = time.time()
            self._transition_progress = 0.0
        logger.info(f"🎭 {self._prev_emotion} → {emotion}  γ={intensity:.2f}")

    def set_attention(self, yaw: float):
        """设置注意力目标偏航角（base_yaw 追踪目标）"""
        lo, hi = JOINT_LIMITS["base_yaw"]
        with self._lock:
            self._attention_target = max(lo, min(hi, yaw))

    def set_attitude(self, score: float):
        """态度得分 -1(消极/低落) ~ +1(积极/昂扬)"""
        with self._lock:
            self._attitude = max(-1.0, min(1.0, score))

    def get_available_emotions(self):
        return VALID_EMOTIONS

    # ── 30fps 控制循环 ────────────────────────────────────────────────────

    def _run_loop(self):
        logger.info("🔄 ELEGNT 控制循环启动 @ %d fps", self.fps)
        while self._running:
            t0 = time.perf_counter()

            # 快照当前状态（减少锁持有时间）
            with self._lock:
                emotion           = self._emotion
                prev_emotion      = self._prev_emotion
                intensity         = self._intensity
                attitude          = self._attitude
                attention_target  = self._attention_target
                emotion_t         = time.time() - self._emotion_start_t
                trans_prog        = self._transition_progress

            # 更新过渡进度
            if trans_prog < 1.0:
                trans_prog = min(1.0, trans_prog + self._dt / 0.4)
                with self._lock:
                    self._transition_progress = trans_prog

            # ── F(τ): 功能性基础姿态 ──────────────────────────────────────
            pose: Dict[str, float] = dict(HOME_POSE)

            # ── E(τ): 四个表达层叠加 ─────────────────────────────────────
            e: Dict[str, float] = {}

            # 1. Attention 层（意图 + 注意力：base_yaw 追踪）
            attn_yaw = self._step_attention(attention_target, self._dt)
            e["base_yaw"] = e.get("base_yaw", 0.0) + attn_yaw

            # 2. Attitude 层（态度偏置）
            for k, v in self._attitude_delta(attitude).items():
                e[k] = e.get(k, 0.0) + v

            # 3. Emotion 层（当前 + 前一情绪平滑过渡）
            blend  = _ease_in_out(trans_prog)
            e_cur  = _compute_emotion(emotion,      emotion_t)
            e_prev = _compute_emotion(prev_emotion, emotion_t)
            for k in set(list(e_cur.keys()) + list(e_prev.keys())):
                mixed = e_prev.get(k, 0.0) * (1.0 - blend) + e_cur.get(k, 0.0) * blend
                e[k] = e.get(k, 0.0) + mixed

            # ── T = F + γ · E，夹紧到安全限位 ────────────────────────────
            action: Dict[str, float] = {}
            for joint in JOINTS:
                key = f"{joint}.pos" if not joint.endswith(".pos") else joint
                jname = key.removesuffix(".pos")
                t_val = pose[key] + intensity * e.get(jname, 0.0)
                lo, hi = JOINT_LIMITS[jname]
                action[key] = max(lo, min(hi, t_val))

            try:
                self.robot.send_action(action)
            except Exception as exc:
                logger.warning(f"send_action 失败: {exc}")

            elapsed = time.perf_counter() - t0
            sleep_t = self._dt - elapsed
            if sleep_t > 0:
                time.sleep(sleep_t)

    def _step_attention(self, target: float, dt: float) -> float:
        """平滑追踪目标 yaw，最大速度 20 deg/s"""
        speed = 20.0
        delta = target - self._attention_current
        step  = math.copysign(min(abs(delta), speed * dt), delta) if delta != 0 else 0.0
        self._attention_current += step
        return self._attention_current

    @staticmethod
    def _attitude_delta(score: float) -> Dict[str, float]:
        """积极→抬头伸展；消极→低头收缩"""
        return {
            "base_pitch":  8.0 * score,
            "elbow_pitch": 5.0 * score,
        }
