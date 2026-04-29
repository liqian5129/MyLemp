# Claude Code Guidelines for lelamp_runtime

## 舵机安全规则

每次修改涉及舵机运动的代码，必须检查以下一点：

1. **速度限制**：追踪完整调用链路到 `send_action` / `sync_write`，确认播放/回放逻辑使用时间戳对齐（而不是固定 fps），避免速度过快触发舵机过载保护。

---

## AI Working Companion 实施(当前主线)

本工作区是 **Agent A**,负责 Mac 侧 Python 代码。

### 必读文档(优先级从高到低)

1. `dev-implementation-plan.md` — 总体计划与分工(权威 SSOT)
2. `dev-protocol.md` — Mac↔设备 USB CDC JSON 协议契约
3. `dev-ai-working-companion.md` — 产品定位与背景
4. 项目 memory(`/Users/qliau/.claude/projects/-Users-qliau-playground-lelamp-runtime/memory/MEMORY.md`)

### 工作边界

**Agent A 负责**(本工作区):
- `lelamp/transport/` — USB CDC client、DisplayController、ArmController
- `lelamp/demo/` — Demo orchestrator(Watchman 等)
- `lelamp/recordings/buddy_*.json` — buddy 专用关键帧
- `scripts/test_*.py` — 测试脚本
- 上述文档的更新(协议变更必须同步通知 Agent B)

**Agent A 不做**:
- 设备固件代码(那是 Agent B 的事,在 `~/playground/lelamp_display/`)
- 屏幕 UI 设计与像素角色绘制(归 Agent B)

### Step-by-Step 任务

按 `dev-implementation-plan.md` 第 3 节执行(Step 1 → Step 6)。每个 Step 完成后自查测试方法是否通过,通过后再进入下一 Step。

### 协议变更流程

如需在协议里加字段或命令:
1. 先更新 `dev-protocol.md`(版本号 +0.1)
2. 更新 `dev-implementation-plan.md` 相关章节
3. 通知用户(让用户告知 Agent B)
4. 再写代码

不允许"代码先行,文档后补"。
