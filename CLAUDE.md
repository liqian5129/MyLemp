# Claude Code Guidelines for lelamp_runtime

## 舵机安全规则

每次修改涉及舵机运动的代码，必须检查以下一点：

1. **速度限制**：追踪完整调用链路到 `send_action` / `sync_write`，确认播放/回放逻辑使用时间戳对齐（而不是固定 fps），避免速度过快触发舵机过载保护。
