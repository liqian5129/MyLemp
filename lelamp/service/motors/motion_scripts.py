"""
程序化动作脚本
每个动作函数接收当前关节位置，返回帧序列（list of dict），在当前姿态基础上执行动作。

安全约束：
- 所有目标角度按关节物理极限 clamp（含 5° 安全余量）
- 每段过渡时长控制在速度 ≤ 60°/s（≈ 2 单位/帧 @30fps）
- 不操作的关节保持当前位置不动
"""
import logging
import random

logger = logging.getLogger(__name__)

FPS = 30

HOME_POS = {
    "base_yaw":    17.8,
    "base_pitch":  -12.2,
    "elbow_pitch": 52.9,
    "wrist_roll":  -5.5,
    "wrist_pitch": 54.6,
}

# 实测物理极限 + 5° 安全余量
JOINT_LIMITS = {
    "base_yaw":    (-91.0, 95.0),
    "base_pitch":  (-95.0, 95.0),
    "elbow_pitch": (30.0, 95.0),
    "wrist_roll":  (-95.0, 76.0),
    "wrist_pitch": (-92.0, 72.0),
}

_DEFAULT_LIMIT = (-92.0, 92.0)


def _clamp(v: float, joint: str = "") -> float:
    lo, hi = JOINT_LIMITS.get(joint, _DEFAULT_LIMIT)
    return max(lo, min(hi, v))


def _smoothstep(t: float) -> float:
    return t * t * (3 - 2 * t)


def _scaled(rng: random.Random, base: float, intensity: float, jitter: float) -> float:
    """幅度缩放 + 抖动。intensity 直接乘。"""
    return base * intensity * rng.uniform(1.0 - jitter, 1.0 + jitter)


def _scaled_dur(rng: random.Random, base: float, intensity: float, jitter: float) -> float:
    """时长缩放：intensity < 1 时变慢，intensity ≥ 1 时不缩短。
    若 intensity > 1 同时缩短时长 + 放大幅度，速度会双倍叠加冲破 DQ_MAX，
    被 velocity_audit 拉伸后反而比 intensity=1 还慢（旋钮反向）。
    所以 intensity ≥ 1 只放大幅度，时长保持基线，让大动作自然多花一点时间。
    """
    if intensity < 1.0:
        factor = 2.0 / (1.0 + intensity)        # ∈ (1.0, 1.54]
    else:
        factor = 1.0
    return base * factor * rng.uniform(1.0 - jitter, 1.0 + jitter)


def _interp(start: dict, end: dict, n_frames: int) -> list:
    """smoothstep 插值，返回 n_frames 帧（不含起始帧）"""
    frames = []
    for i in range(n_frames):
        t = _smoothstep((i + 1) / n_frames)
        frames.append({m: start[m] + (end[m] - start[m]) * t for m in start})
    return frames


def _hold(pos: dict, duration: float) -> list:
    """保持当前位置"""
    n = max(1, int(duration * FPS))
    return [dict(pos)] * n


def _build_frames(current: dict, segments: list) -> list:
    """
    Catmull-Rom 样条：全局平滑，中间路点速度连续，起止点速度=0。
    消除旧逐段 smoothstep 在段衔接处的顿挫感。

    segments: [(partial_target_dict, duration_sec), ...]
    partial_target_dict 只含要动的关节，其余继承上一路点值。
    """
    if not segments:
        return []

    # ── 1. 构建完整路点序列 ────────────────────────────────────────────
    joints = list(HOME_POS.keys())
    waypoints  = [dict(current)]
    timestamps = [0.0]
    t_acc = 0.0
    prev = dict(current)
    for target_partial, duration in segments:
        target_full = dict(prev)
        for k, v in target_partial.items():
            if k in target_full:
                target_full[k] = _clamp(v, k)
        t_acc += max(1e-3, float(duration))
        waypoints.append(target_full)
        timestamps.append(t_acc)
        prev = target_full

    n_wp = len(waypoints)

    # ── 2. 计算各路点切线（Catmull-Rom 变体） ─────────────────────────
    # 起点/终点：切线=0（ease-in/out），内部路点：跨段中心差分（速度连续）
    tangents = []
    for i in range(n_wp):
        if i == 0 or i == n_wp - 1:
            tangents.append({k: 0.0 for k in joints})
        else:
            dt_span = timestamps[i + 1] - timestamps[i - 1]
            tangents.append({
                k: (waypoints[i + 1][k] - waypoints[i - 1][k]) / dt_span
                for k in joints
            })

    # ── 3. 逐段生成帧（三次 Hermite） ─────────────────────────────────
    frames = []
    for seg in range(n_wp - 1):
        t0, t1 = timestamps[seg], timestamps[seg + 1]
        P0, P1 = waypoints[seg],  waypoints[seg + 1]
        T0, T1 = tangents[seg],   tangents[seg + 1]
        dt     = t1 - t0
        n_frames = max(1, int(dt * FPS))

        for i in range(n_frames):
            s = (i + 1) / n_frames          # 不含起始帧，含末尾帧
            s2, s3 = s * s, s * s * s
            h00 =  2*s3 - 3*s2 + 1
            h10 =    s3 - 2*s2 + s
            h01 = -2*s3 + 3*s2
            h11 =    s3 -   s2
            frames.append({
                k: max(-100.0, min(100.0, h00*P0[k] + h10*dt*T0[k] + h01*P1[k] + h11*dt*T1[k]))
                for k in joints
            })

    return frames


# ─────────────────────────────────────────────
# 动作函数
# ─────────────────────────────────────────────

def nod(pos: dict, intensity: float = 1.0, jitter: float = 0.10, seed=None) -> list:
    """点头：wrist_pitch 下-停-上-下-停-回。
    每次低头后加一个 hold 段，让"低到了哪儿"被眼睛看见，
    intensity<1 时 hold 会被 _scaled_dur 拉长，克制的小点头反而更易辨识深度。
    """
    rng = random.Random(seed)
    p = pos["wrist_pitch"]
    down = _clamp(p + _scaled(rng, 22, intensity, jitter))
    up   = _clamp(p - _scaled(rng,  8, intensity, jitter))
    return _build_frames(pos, [
        ({"wrist_pitch": down}, _scaled_dur(rng, 0.30, intensity, jitter)),
        ({},                    _scaled_dur(rng, 0.18, intensity, jitter)),  # hold@底
        ({"wrist_pitch": up},   _scaled_dur(rng, 0.30, intensity, jitter)),
        ({"wrist_pitch": down}, _scaled_dur(rng, 0.30, intensity, jitter)),
        ({},                    _scaled_dur(rng, 0.18, intensity, jitter)),  # hold@底
        ({"wrist_pitch": p},    _scaled_dur(rng, 0.42, intensity, jitter)),
    ])


def headshake(pos: dict, intensity: float = 1.0, jitter: float = 0.10, seed=None) -> list:
    """摇头：base_yaw 左-停-右-停-左-回。
    每次到达左右极限时加 hold 段，让"摆到了哪儿"被眼睛看见。
    base_yaw 的 DQ_MAX=60 deg/s 比较严格，所以 base 16°（不是 22°）+ 较长 transit，
    让 intensity=1.0 不再触发 audit 拉伸；intensity=1.4 也只小幅触发。
    """
    rng = random.Random(seed)
    y = pos["base_yaw"]
    left  = _clamp(y - _scaled(rng, 16, intensity, jitter))
    right = _clamp(y + _scaled(rng, 16, intensity, jitter))
    return _build_frames(pos, [
        ({"base_yaw": left},  _scaled_dur(rng, 0.40, intensity, jitter)),
        ({},                  _scaled_dur(rng, 0.18, intensity, jitter)),  # hold@左
        ({"base_yaw": right}, _scaled_dur(rng, 0.45, intensity, jitter)),
        ({},                  _scaled_dur(rng, 0.18, intensity, jitter)),  # hold@右
        ({"base_yaw": left},  _scaled_dur(rng, 0.40, intensity, jitter)),
        ({"base_yaw": y},     _scaled_dur(rng, 0.50, intensity, jitter)),
    ])


def curious(pos: dict, intensity: float = 1.0, jitter: float = 0.10, seed=None) -> list:
    """好奇：转头+歪头，像在打量"""
    rng = random.Random(seed)
    y = pos["base_yaw"]
    r = pos["wrist_roll"]
    return _build_frames(pos, [
        ({"base_yaw":  _clamp(y + _scaled(rng, 18, intensity, jitter)),
          "wrist_roll": _clamp(r + _scaled(rng, 22, intensity, jitter))},
         _scaled_dur(rng, 0.8, intensity, jitter)),
        ({}, _scaled_dur(rng, 0.4, intensity, jitter)),  # 停顿
        ({"base_yaw":  _clamp(y - _scaled(rng, 12, intensity, jitter)),
          "wrist_roll": _clamp(r - _scaled(rng, 10, intensity, jitter))},
         _scaled_dur(rng, 0.7, intensity, jitter)),
        ({}, _scaled_dur(rng, 0.3, intensity, jitter)),
        ({"base_yaw": y, "wrist_roll": r},
         _scaled_dur(rng, 0.8, intensity, jitter)),
    ])


def excited(pos: dict, intensity: float = 1.0, jitter: float = 0.10, seed=None) -> list:
    """兴奋：臂上下弹跳两次，温和但有活力"""
    rng = random.Random(seed)
    bp = pos["base_pitch"]
    ep = pos["elbow_pitch"]
    wp = pos["wrist_pitch"]
    up_bp = _clamp(bp + _scaled(rng, 8, intensity, jitter))
    up_ep = _clamp(ep - _scaled(rng, 7, intensity, jitter))
    up_wp = _clamp(wp - _scaled(rng, 7, intensity, jitter))
    return _build_frames(pos, [
        ({"base_pitch": up_bp, "elbow_pitch": up_ep, "wrist_pitch": up_wp},
         _scaled_dur(rng, 0.45, intensity, jitter)),
        ({"base_pitch": bp,    "elbow_pitch": ep,    "wrist_pitch": wp},
         _scaled_dur(rng, 0.40, intensity, jitter)),
        ({"base_pitch": up_bp, "elbow_pitch": up_ep, "wrist_pitch": up_wp},
         _scaled_dur(rng, 0.40, intensity, jitter)),
        ({"base_pitch": bp,    "elbow_pitch": ep,    "wrist_pitch": wp},
         _scaled_dur(rng, 0.50, intensity, jitter)),
    ])


def happy_wiggle(pos: dict, intensity: float = 1.0, jitter: float = 0.10, seed=None) -> list:
    """开心抖动：base_yaw + wrist_roll 左右晃四次"""
    rng = random.Random(seed)
    y = pos["base_yaw"]
    r = pos["wrist_roll"]
    d_yaw  = _scaled(rng, 18, intensity, jitter)
    d_roll = _scaled(rng, 18, intensity, jitter)
    return _build_frames(pos, [
        ({"base_yaw": _clamp(y + d_yaw), "wrist_roll": _clamp(r + d_roll)},
         _scaled_dur(rng, 0.40, intensity, jitter)),
        ({"base_yaw": _clamp(y - d_yaw), "wrist_roll": _clamp(r - d_roll)},
         _scaled_dur(rng, 0.40, intensity, jitter)),
        ({"base_yaw": _clamp(y + d_yaw), "wrist_roll": _clamp(r + d_roll)},
         _scaled_dur(rng, 0.40, intensity, jitter)),
        ({"base_yaw": _clamp(y - d_yaw), "wrist_roll": _clamp(r - d_roll)},
         _scaled_dur(rng, 0.40, intensity, jitter)),
        ({"base_yaw": y, "wrist_roll": r},
         _scaled_dur(rng, 0.55, intensity, jitter)),
    ])


def sad(pos: dict, intensity: float = 1.0, jitter: float = 0.10, seed=None) -> list:
    """伤心：灯头缓缓垂下，停留，慢慢回来"""
    rng = random.Random(seed)
    wp = pos["wrist_pitch"]
    down = _clamp(wp + _scaled(rng, 28, intensity, jitter))
    return _build_frames(pos, [
        ({"wrist_pitch": down}, _scaled_dur(rng, 1.5, intensity, jitter)),
        ({},                    _scaled_dur(rng, 1.0, intensity, jitter)),  # 停留
        ({"wrist_pitch": wp},   _scaled_dur(rng, 1.8, intensity, jitter)),
    ])


def scanning(pos: dict, intensity: float = 1.0, jitter: float = 0.10, seed=None) -> list:
    """扫视：base_yaw 大幅缓慢左右扫"""
    rng = random.Random(seed)
    y = pos["base_yaw"]
    left  = _clamp(y - _scaled(rng, 42, intensity, jitter))
    right = _clamp(y + _scaled(rng, 42, intensity, jitter))
    return _build_frames(pos, [
        ({"base_yaw": left},  _scaled_dur(rng, 1.5, intensity, jitter)),
        ({"base_yaw": right}, _scaled_dur(rng, 2.0, intensity, jitter)),
        ({"base_yaw": y},     _scaled_dur(rng, 1.5, intensity, jitter)),
    ])


def shock(pos: dict, intensity: float = 1.0, jitter: float = 0.10, seed=None) -> list:
    """震惊：灯头快速后仰，停顿，慢慢回来"""
    rng = random.Random(seed)
    wp = pos["wrist_pitch"]
    bp = pos["base_pitch"]
    back_wp = _clamp(wp - _scaled(rng, 14, intensity, jitter))
    back_bp = _clamp(bp - _scaled(rng,  8, intensity, jitter))
    return _build_frames(pos, [
        ({"wrist_pitch": back_wp, "base_pitch": back_bp},
         _scaled_dur(rng, 0.30, intensity, jitter)),   # 快速后仰
        ({}, _scaled_dur(rng, 0.35, intensity, jitter)),  # 停顿
        ({"wrist_pitch": wp, "base_pitch": bp},
         _scaled_dur(rng, 0.50, intensity, jitter)),   # 慢慢回来
    ])


def shy(pos: dict, intensity: float = 1.0, jitter: float = 0.10, seed=None) -> list:
    """害羞：偏头躲避，缓缓回来"""
    rng = random.Random(seed)
    y = pos["base_yaw"]
    r = pos["wrist_roll"]
    wp = pos["wrist_pitch"]
    return _build_frames(pos, [
        ({"base_yaw":   _clamp(y - _scaled(rng, 25, intensity, jitter)),
          "wrist_roll": _clamp(r + _scaled(rng, 28, intensity, jitter)),
          "wrist_pitch": _clamp(wp + _scaled(rng, 10, intensity, jitter))},
         _scaled_dur(rng, 1.0, intensity, jitter)),
        ({}, _scaled_dur(rng, 1.2, intensity, jitter)),
        ({"base_yaw": y, "wrist_roll": r, "wrist_pitch": wp},
         _scaled_dur(rng, 1.2, intensity, jitter)),
    ])


def wake_up(pos: dict, intensity: float = 1.0, jitter: float = 0.10, seed=None) -> list:
    """唤醒：从当前姿态平滑站起来到 HOME。
    例外：必须严格回到 HOME，幅度不缩放，仅时长接受 intensity/jitter 调整。
    """
    rng = random.Random(seed)
    return _build_frames(pos, [
        ({k: HOME_POS[k] for k in HOME_POS},
         _scaled_dur(rng, 2.0, intensity, jitter)),
    ])


def breath_cycle(pos: dict) -> list:
    """呼吸循环帧序列（可连续播放）：多关节 smoothstep 插值，起止点相同可无缝衔接。"""
    y  = pos["base_yaw"]
    r  = pos["wrist_roll"]
    wp = pos["wrist_pitch"]
    return _build_frames(pos, [
        # 右漂·抬头：吸气感
        ({"base_yaw": _clamp(y + 20), "wrist_roll": _clamp(r + 15),
          "wrist_pitch": _clamp(wp - 8)},  3.5),
        # 转向左·低头：呼气感
        ({"base_yaw": _clamp(y - 17), "wrist_roll": _clamp(r - 13),
          "wrist_pitch": _clamp(wp + 7)},  4.0),
        # 缓缓回正
        ({"base_yaw": y, "wrist_roll": r, "wrist_pitch": wp}, 2.5),
    ])


def idle_glance(pos: dict) -> list:
    """空闲探索：随机朝一侧张望，wrist_roll 同向倾斜，增加好奇感"""
    y = pos["base_yaw"]
    r = pos["wrist_roll"]
    direction = random.choice([-1, 1])
    yaw_delta = random.uniform(16, 26) * direction
    roll_delta = random.uniform(8, 14) * direction  # 同向倾斜
    return _build_frames(pos, [
        ({"base_yaw": _clamp(y + yaw_delta),
          "wrist_roll": _clamp(r + roll_delta)}, 0.8),
        ({},                                       0.5),   # 停留打量
        ({"base_yaw": y, "wrist_roll": r},         0.8),
    ])


def idle_breath(pos: dict, intensity: float = 1.0, jitter: float = 0.30, seed=None) -> list:
    """呆萌呼吸：极微幅的头部晃动，随机选一种变体，让每次心跳看起来不一样。

    变体池（随机选 1）：
      - 轻叹气：微微低头再回来
      - 左歪头：小幅歪向一侧
      - 右歪头：小幅歪向另一侧
      - 伸懒腰：微微抬头展开
      - 小晃神：先向一侧偏，再向另一侧偏，回正

    幅度极小（3-8°），时长缓慢（1.5-3s），营造安静但有生命感的呼吸节奏。
    """
    rng = random.Random(seed)
    y  = pos["base_yaw"]
    bp = pos["base_pitch"]
    ep = pos["elbow_pitch"]
    r  = pos["wrist_roll"]
    wp = pos["wrist_pitch"]

    # 幅度缩放
    def _s(base):
        return base * intensity * rng.uniform(1.0 - jitter, 1.0 + jitter)

    variant = rng.choice(["sigh", "tilt_l", "tilt_r", "stretch", "drift"])
    logger.info("idle_breath 变体: %s", variant)

    if variant == "sigh":
        # 轻叹气：低头 + 收肘
        dip = _s(12.0)
        return _build_frames(pos, [
            ({"wrist_pitch": _clamp(wp + dip), "elbow_pitch": _clamp(ep - _s(7.0))}, _s(1.2)),
            ({},                                                                      _s(0.6)),
            ({"wrist_pitch": wp, "elbow_pitch": ep},                                  _s(1.0)),
        ])

    elif variant == "tilt_l":
        # 左歪头
        roll_d = _s(16.0)
        return _build_frames(pos, [
            ({"wrist_roll": _clamp(r - roll_d), "base_yaw": _clamp(y - _s(6.0))}, _s(1.0)),
            ({},                                                                    _s(0.8)),
            ({"wrist_roll": r, "base_yaw": y},                                     _s(0.8)),
        ])

    elif variant == "tilt_r":
        # 右歪头
        roll_d = _s(16.0)
        return _build_frames(pos, [
            ({"wrist_roll": _clamp(r + roll_d), "base_yaw": _clamp(y + _s(6.0))}, _s(1.0)),
            ({},                                                                    _s(0.8)),
            ({"wrist_roll": r, "base_yaw": y},                                     _s(0.8)),
        ])

    elif variant == "stretch":
        # 伸懒腰：抬头 + 展肘
        lift = _s(12.0)
        return _build_frames(pos, [
            ({"wrist_pitch": _clamp(wp - lift), "elbow_pitch": _clamp(ep + _s(8.0))}, _s(1.5)),
            ({},                                                                       _s(0.5)),
            ({"wrist_pitch": wp, "elbow_pitch": ep},                                   _s(1.2)),
        ])

    else:  # drift
        # 小晃神：左偏→右偏→回正
        d1 = _s(8.0) * rng.choice([-1, 1])
        d2 = -d1 * rng.uniform(0.6, 1.0)
        return _build_frames(pos, [
            ({"base_yaw": _clamp(y + d1), "wrist_roll": _clamp(r + d1 * 0.5)}, _s(1.0)),
            ({"base_yaw": _clamp(y + d2), "wrist_roll": _clamp(r + d2 * 0.5)}, _s(1.0)),
            ({"base_yaw": y, "wrist_roll": r},                                  _s(0.8)),
        ])


# ─────────────────────────────────────────────
# 动作注册表
# ─────────────────────────────────────────────

MOTION_REGISTRY = {
    "nod":         nod,
    "headshake":   headshake,
    "curious":     curious,
    "excited":     excited,
    "happy_wiggle": happy_wiggle,
    "sad":         sad,
    "scanning":    scanning,
    "shock":       shock,
    "shy":         shy,
    "wake_up":     wake_up,
    "idle_breath": idle_breath,
}
