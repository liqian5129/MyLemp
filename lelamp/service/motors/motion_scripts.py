"""
程序化动作脚本
每个动作函数接收当前关节位置，返回帧序列（list of dict），在当前姿态基础上执行动作。

安全约束：
- 所有目标角度 clamp 到 [-92, 92]，留 8 单位安全余量
- 每段过渡时长控制在速度 ≤ 60°/s（≈ 2 单位/帧 @30fps）
- 不操作的关节保持当前位置不动
"""
import random

CLAMP_MIN = -92.0
CLAMP_MAX = 92.0
FPS = 30

HOME_POS = {
    "base_yaw":    7.4,
    "base_pitch":  -38.4,
    "elbow_pitch": 48.5,
    "wrist_roll":  -0.2,
    "wrist_pitch": -46.9,
}


def _clamp(v: float) -> float:
    return max(CLAMP_MIN, min(CLAMP_MAX, v))


def _smoothstep(t: float) -> float:
    return t * t * (3 - 2 * t)


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
                target_full[k] = _clamp(v)
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

def nod(pos: dict) -> list:
    """点头：wrist_pitch 上下两次，从当前位置出发"""
    p = pos["wrist_pitch"]
    down = _clamp(p - 22)
    up   = _clamp(p + 8)
    return _build_frames(pos, [
        ({"wrist_pitch": down}, 0.35),
        ({"wrist_pitch": up},   0.35),
        ({"wrist_pitch": down}, 0.35),
        ({"wrist_pitch": p},    0.45),
    ])


def headshake(pos: dict) -> list:
    """摇头：base_yaw 左右两次"""
    y = pos["base_yaw"]
    left  = _clamp(y - 22)
    right = _clamp(y + 22)
    return _build_frames(pos, [
        ({"base_yaw": left},  0.35),
        ({"base_yaw": right}, 0.40),
        ({"base_yaw": left},  0.35),
        ({"base_yaw": y},     0.40),
    ])


def curious(pos: dict) -> list:
    """好奇：转头+歪头，像在打量"""
    y = pos["base_yaw"]
    r = pos["wrist_roll"]
    return _build_frames(pos, [
        ({"base_yaw": _clamp(y + 18), "wrist_roll": _clamp(r + 22)}, 0.8),
        ({},                                                           0.4),  # 停顿
        ({"base_yaw": _clamp(y - 12), "wrist_roll": _clamp(r - 10)}, 0.7),
        ({},                                                           0.3),
        ({"base_yaw": y, "wrist_roll": r},                            0.8),
    ])


def excited(pos: dict) -> list:
    """兴奋：臂上下弹跳三次，幅度适中"""
    bp = pos["base_pitch"]
    ep = pos["elbow_pitch"]
    wp = pos["wrist_pitch"]
    up_bp = _clamp(bp + 14)
    up_ep = _clamp(ep - 12)
    up_wp = _clamp(wp + 12)
    return _build_frames(pos, [
        ({"base_pitch": up_bp, "elbow_pitch": up_ep, "wrist_pitch": up_wp}, 0.5),
        ({"base_pitch": bp,    "elbow_pitch": ep,    "wrist_pitch": wp},    0.45),
        ({"base_pitch": up_bp, "elbow_pitch": up_ep, "wrist_pitch": up_wp}, 0.45),
        ({"base_pitch": bp,    "elbow_pitch": ep,    "wrist_pitch": wp},    0.45),
        ({"base_pitch": up_bp, "elbow_pitch": up_ep, "wrist_pitch": up_wp}, 0.45),
        ({"base_pitch": bp,    "elbow_pitch": ep,    "wrist_pitch": wp},    0.55),
    ])


def happy_wiggle(pos: dict) -> list:
    """开心抖动：base_yaw + wrist_roll 左右晃四次"""
    y = pos["base_yaw"]
    r = pos["wrist_roll"]
    d = 18  # 幅度
    return _build_frames(pos, [
        ({"base_yaw": _clamp(y + d), "wrist_roll": _clamp(r + d)}, 0.40),
        ({"base_yaw": _clamp(y - d), "wrist_roll": _clamp(r - d)}, 0.40),
        ({"base_yaw": _clamp(y + d), "wrist_roll": _clamp(r + d)}, 0.40),
        ({"base_yaw": _clamp(y - d), "wrist_roll": _clamp(r - d)}, 0.40),
        ({"base_yaw": y,             "wrist_roll": r},              0.55),
    ])


def sad(pos: dict) -> list:
    """伤心：灯头缓缓垂下，停留，慢慢回来"""
    wp = pos["wrist_pitch"]
    down = _clamp(wp - 28)
    return _build_frames(pos, [
        ({"wrist_pitch": down}, 1.5),
        ({},                    1.0),  # 停留
        ({"wrist_pitch": wp},   1.8),
    ])


def scanning(pos: dict) -> list:
    """扫视：base_yaw 大幅缓慢左右扫"""
    y = pos["base_yaw"]
    left  = _clamp(y - 42)
    right = _clamp(y + 42)
    return _build_frames(pos, [
        ({"base_yaw": left},  1.5),
        ({"base_yaw": right}, 2.0),
        ({"base_yaw": y},     1.5),
    ])


def shock(pos: dict) -> list:
    """震惊：灯头猛地后仰，停顿，慢慢回来"""
    wp = pos["wrist_pitch"]
    bp = pos["base_pitch"]
    back_wp = _clamp(wp + 22)
    back_bp = _clamp(bp - 15)
    return _build_frames(pos, [
        ({"wrist_pitch": back_wp, "base_pitch": back_bp}, 0.25),  # 快速后仰
        ({},                                               0.4),   # 停顿
        ({"wrist_pitch": wp, "base_pitch": bp},           0.6),   # 慢慢回来
    ])


def shy(pos: dict) -> list:
    """害羞：偏头躲避，缓缓回来"""
    y = pos["base_yaw"]
    r = pos["wrist_roll"]
    wp = pos["wrist_pitch"]
    return _build_frames(pos, [
        ({"base_yaw": _clamp(y - 25), "wrist_roll": _clamp(r + 28), "wrist_pitch": _clamp(wp - 10)}, 1.0),
        ({},                                                                                            1.2),
        ({"base_yaw": y, "wrist_roll": r, "wrist_pitch": wp},                                         1.2),
    ])


def wake_up(pos: dict) -> list:
    """唤醒：从当前姿态平滑站起来到 HOME"""
    return _build_frames(pos, [
        ({k: HOME_POS[k] for k in HOME_POS}, 2.0),
    ])


def breath_cycle(pos: dict) -> list:
    """呼吸循环帧序列（可连续播放）：多关节 smoothstep 插值，起止点相同可无缝衔接。"""
    y  = pos["base_yaw"]
    r  = pos["wrist_roll"]
    wp = pos["wrist_pitch"]
    return _build_frames(pos, [
        # 右漂·抬头：吸气感
        ({"base_yaw": _clamp(y + 20), "wrist_roll": _clamp(r + 15),
          "wrist_pitch": _clamp(wp + 8)},  3.5),
        # 转向左·低头：呼气感
        ({"base_yaw": _clamp(y - 17), "wrist_roll": _clamp(r - 13),
          "wrist_pitch": _clamp(wp - 7)},  4.0),
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
}
