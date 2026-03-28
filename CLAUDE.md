# Claude Code Guidelines for lelamp_runtime

## 舵机安全规则

每次修改涉及舵机运动的代码，必须检查以下两点，**不要轻易改动已有限制**：

1. **速度限制**：追踪完整调用链路到 `send_action` / `sync_write`，确认播放/回放逻辑使用时间戳对齐（而不是固定 fps），避免速度过快触发舵机过载保护。

2. **最大角度限制**：确认 `LeLampFollowerConfig` 的 `max_relative_target` 不为 `None`（当前默认值为 100.0，约等于无限速，忠实还原录制动作；全量程为 200 单位，约 1 unit ≈ 1°），保证每帧位移有上限，防止舵机瞬间跳变。不得擅自修改。
