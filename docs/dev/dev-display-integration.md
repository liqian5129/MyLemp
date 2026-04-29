# lelamp-display 接入方案

> 目标：把 Waveshare ESP32-S3-Touch-LCD-4B 装在小Q 头部，作为**面部表情显示**。同时支持办公/idle 模式下的辅助信息展示。
> 协议：USB CDC 串口 + JSON 行协议（后续可平替为 WebSocket，详见 `~/.claude/projects/.../memory/project_lamp_io_hardware.md`）。
> 角色定位：**这是小Q 的"脸"，不是副屏**。表情是主，信息显示是次。

---

## 0. 角色定位（务必先读）

这块屏装在小Q **头部**，跟着舵机一起转动俯仰，是小Q 的**面部表达通道**。

**优先级**：
1. **Face mode（默认）** — 持续显示一张表情脸，跟随情绪、动作、TTS 状态变化。这是小Q 醒着时屏幕的常态。
2. **Office mode** — 用户主动切换（语音指令 "进入办公模式" / 长时间无互动自动进入），屏幕变成桌面信息面板：时间/天气/番茄钟/通知摘要。退出条件：用户说话、检测到关注。
3. **Idle ambient** — 长时间无互动时的过渡态，在 face 上叠加微表情（眨眼、扫视、打哈欠），可能配合显示一句轻文案。

**铁律**：屏幕**永远不黑**（除非主程序退出），因为黑屏 = 小Q 死了。哪怕 Mac 端断连，ESP32 也要自己渲染一个"失联表情"（比如 `disconnected` 脸 + 旋转点）。

---

## 1. 硬件 & 连接

- **板子**：Waveshare ESP32-S3-Touch-LCD-4B（4 寸 480×480 IPS + GT911 触屏，8MB PSRAM，16MB Flash，QMI8658 IMU，ES8311/ES7210 音频，AXP2101 电源）
- **物理安装**：装在小Q 灯头位置，跟随 wrist_pitch/wrist_roll 舵机一起动
- **连线**：一根 USB-C
  - 开发期插 **UART 口**（稳定、不掉端口，需装 CH343 驱动）
  - 部署期改插 **原生 USB 口**（免驱、上传快、单线集成）
- **驱动**（开发期）：`brew install --cask wch-ch34x-usb-serial-driver`，装完重启
- **端口识别**：
  - 原生 USB → `/dev/cu.usbmodem*`
  - UART → `/dev/cu.wchusbserial*`
  - Python 端自动选择：先扫 wchusbserial，找不到再扫 usbmodem

---

## 2. ESP32 固件（独立工程 `lelamp-display`）

### 2.1 工程位置

跟 lelamp_runtime **同级**，不要嵌套：
```
~/playground/
  lelamp_runtime/        # Python 大脑（不动）
  lelamp-display/        # 新建（PlatformIO + Arduino + LVGL）
    platformio.ini
    src/
      main.cpp
      protocol.cpp/.h    # JSON 行协议解析
      face/
        face_renderer.cpp/.h    # 脸的绘制核心
        expressions.cpp/.h      # 7 种表情资源
        micro_anim.cpp/.h       # 眨眼/扫视/呼吸的过渡动画
      modes/
        face_mode.cpp/.h
        office_mode.cpp/.h
        ambient_mode.cpp/.h
      hud.cpp/.h         # 状态栏（电量/连接/时间，可隐藏）
    include/
    lib/
    test/
      host_send.py       # Python 测试脚本（不依赖 lelamp_runtime）
```

### 2.2 PlatformIO 配置

```ini
[env:waveshare_4b]
platform = espressif32 @ 6.7.0
board = esp32-s3-devkitc-1
framework = arduino

board_build.mcu = esp32s3
board_build.f_cpu = 240000000L
board_build.flash_mode = qio
board_build.flash_size = 16MB
board_build.psram_type = opi
board_upload.flash_size = 16MB
board_upload.maximum_size = 16777216
board_build.partitions = default_16MB.csv
board_build.arduino.memory_type = qio_opi

build_flags =
    -DBOARD_HAS_PSRAM
    -DARDUINO_USB_CDC_ON_BOOT=1
    -DARDUINO_USB_MODE=1

monitor_speed = 115200
upload_speed = 921600

lib_deps =
    moononournation/GFX Library for Arduino@^1.6.0
    lvgl/lvgl@^9.3.0
    bblanchon/ArduinoJson@^7.0.0
```

⚠️ `ARDUINO_USB_CDC_ON_BOOT=1` 必须有，否则原生 USB 没串口输出。

### 2.3 ESP32 端运行架构

```
[USB CDC RX (Serial.read)]
        │
        ▼
[行缓冲器（遇 \n 出一行）]
        │
        ▼
[JSON 解析（ArduinoJson）]
        │
        ▼
[消息队列（FreeRTOS Queue）]   ← 串口任务到此结束
        │
        ▼
[loop() 主循环每帧出队 → ModeManager 路由 → 对应 Mode 更新 LVGL]
        │
        ▼
[微动画引擎（独立 timer，给 Face Mode 加眨眼/呼吸）]
```

**铁律**：LVGL 操作只能在 loop() 主线程，串口回调里**绝不能**直接调 lv_*。否则会崩。

### 2.4 屏幕初始化

参考 Waveshare 官方 demo `06_LVGL_Arduino_v9`：把 LVGL display driver 配好（ST7701 RGB + GT911 触摸），先跑通官方 widgets demo，再开始写自己的 face renderer。

### 2.5 表情资源

7 种基础脸，对应 lelamp_runtime 现有的 `play_emotion`：

| Face name | 触发场景 | 视觉特征 |
|---|---|---|
| `idle` | 默认/idle | 安静的两点眼 + 微笑曲线，会眨眼 |
| `happy` | excited / happy_wiggle / nod | 月牙眼 + 大笑 |
| `curious` | curious / scanning | 一边眼睛大、一边小，眼珠偏移 |
| `sad` | sad / shy | 下垂眼 + 直线嘴 |
| `shock` | shock | 圆眼 + O 嘴 |
| `sleepy` | wake_up（反向）/ sleep | 半闭眼 + 直线嘴 |
| `thinking` | LLM 调用中 | 眼睛上看 + 三个动态点 |

**实现方式**：用 LVGL 的 `lv_obj` + 几何图形（圆/弧/线）画矢量脸，**不用 PNG**。理由：可参数化（眨眼 = 改椭圆短轴），尺寸自适应，无资源加载延迟。

---

## 3. 线协议（JSON 行）

每条 UTF-8 JSON，`\n` 结尾。ESP32 行 buffer 上限 1KB，超出丢弃整行。

### 3.1 Python → ESP32

```jsonc
// ── 模式切换 ───────────────────────────────────────────
{"t":"mode","name":"face"}        // 切回脸（默认）
{"t":"mode","name":"office"}      // 进办公模式
{"t":"mode","name":"ambient"}     // 进 idle ambient

// ── Face mode 控制 ────────────────────────────────────
{"t":"face","name":"happy"}                    // 切表情
{"t":"face","name":"happy","sec":2}            // 切表情维持 2s 后回 idle
{"t":"face_micro","action":"blink"}            // 触发一次眨眼（不改主表情）
{"t":"face_micro","action":"glance","dir":"left"}  // 眼珠瞥一下

// ── Office mode 控制 ──────────────────────────────────
{"t":"office","clock":"14:32","date":"04-26 周日","weather":"晴 22°"}
{"t":"office","pomodoro":{"phase":"focus","remain_sec":1245}}
{"t":"office","notify":{"app":"Slack","msg":"qiang: 看下 PR"}}

// ── 通用 ──────────────────────────────────────────────
{"t":"toast","msg":"小Q 醒了","sec":3}        // 任何模式上方浮一条文字
{"t":"hud","battery":85,"net":"ok"}            // 状态栏更新
{"t":"ping"}                                    // 心跳（Python 每 2s 发一次）
```

### 3.2 ESP32 → Python

```jsonc
{"evt":"boot","ver":"0.1.0"}                  // 启动 ack
{"evt":"pong","ts":12345}                     // 心跳响应
{"evt":"touch","x":240,"y":300}               // 触屏点击
{"evt":"shake","intensity":0.8}               // IMU 检测到摇晃
{"evt":"err","msg":"json parse failed","raw":"..."}
```

### 3.3 协议规则

- 字段名都用短名（`t/msg/sec`），减少串口带宽
- 未知 `t` → ESP32 回 `{"evt":"err","msg":"unknown type","t":"xxx"}`
- ESP32 处理完每条消息打印 `[OK] t=face name=happy` 到串口（调试用）
- **断连保护**：5s 没收到 ping → ESP32 自动切到 face=`disconnected`，恢复后自动复位

---

## 4. Python 客户端（在 lelamp_runtime 内）

### 4.1 目录

```
lelamp_runtime/lelamp/display/
  __init__.py
  screen_client.py     # 串口连接 + 异步写队列 + 心跳 + 重连
  protocol.py          # 消息构造函数
```

### 4.2 对外 API

```python
from lelamp.display import ScreenClient

screen = ScreenClient(port=None)        # port=None 自动扫描
await screen.start()                    # 打开串口、等 boot evt

# Face 控制（最常用）
await screen.set_face("happy")
await screen.set_face("curious", sec=2)        # 维持 2s 后回 idle
await screen.face_blink()
await screen.face_glance("left")

# 模式切换
await screen.set_mode("office")
await screen.set_mode("face")          # 切回脸

# Office mode 内容
await screen.office_clock("14:32", date="04-26 周日", weather="晴 22°")
await screen.office_notify(app="Slack", msg="...")

# 通用
await screen.toast("小Q 醒了", sec=3)   # 任何模式上方浮文字
await screen.hud(battery=85, net="ok")

# 事件回调（Phase 4 起）
screen.on_touch = lambda x, y: ...
screen.on_shake = lambda intensity: ...

# 状态
screen.is_connected   # bool

await screen.stop()
```

### 4.3 实现要点

- **依赖**：`pyserial-asyncio`（同步 pyserial 会卡 asyncio loop）
- **写队列**：`asyncio.Queue(maxsize=64)`，满了 drop oldest（避免阻塞调用方）
- **心跳**：每 2s 发 `{"t":"ping"}`，5s 没 `pong` → 标记断连
- **重连**：断连后每 3s 重试打开串口
- **日志**：`logging.getLogger("lelamp.display")`

### 4.4 接入 main_soul.py

启动时初始化，**断连不能让主程序崩**：
```python
screen = ScreenClient()
try:
    await asyncio.wait_for(screen.start(), timeout=5.0)
    logger.info("📺 脸屏已连接")
except (asyncio.TimeoutError, Exception) as exc:
    logger.warning("📺 脸屏未连接: %s（继续运行，不显示表情）", exc)
    screen = None
```

后续所有调用包一层：
```python
if screen and screen.is_connected:
    await screen.set_face("happy")
```

或者更优雅：把 `screen` 注入成可选依赖，提供一个 NoOp 版本，调用方无脑调。

### 4.5 表情同步触发点

定位现有代码里所有 `motion_svc.play_emotion(...)` 调用点，**在每个调用点旁边**加对应 `screen.set_face(...)`。映射表：

| `play_emotion(...)` | `screen.set_face(...)` |
|---|---|
| `nod` / `happy_wiggle` / `excited` | `happy` |
| `curious` / `scanning` | `curious` |
| `sad` / `shy` | `sad` |
| `shock` | `shock` |
| `headshake` | `sad`（短暂）|
| `wake_up` | `idle`（从 sleepy 渐变）|

TTS 期间叠加微动画：`tts.speak()` 开始时 → `screen.face_micro("speaking_start")`，结束时 → `face_micro("speaking_end")`（嘴部跟着动）。**这是 Phase 4 的事，先不做**。

---

## 5. 落地分阶段

### Phase 1 — Spike（1 天）
**目标**：跑通 "Mac Python 发 JSON → ESP32 屏幕画脸"。

- [ ] 新建 `lelamp-display` PlatformIO 工程
- [ ] `src/main.cpp` 实现：
  - LVGL 初始化（ST7701 + GT911）
  - 串口行缓冲解析
  - 处理 `{"t":"face","name":"happy"}`，画一张静态 happy 脸
  - 启动时打印 `{"evt":"boot","ver":"0.1.0"}`
- [ ] `test/host_send.py` 用 pyserial 发几条 face 切换，肉眼验证
- [ ] **不接入 lelamp_runtime**，spike 阶段保持隔离

✅ 完成标准：Python 脚本发 `{"t":"face","name":"happy"}`，屏幕显示 happy 脸。切换 5 种表情都正确。

### Phase 2 — 表情系统完善（1-2 天）
- [ ] 实现 7 种基础脸（矢量绘制）
- [ ] 微动画引擎：眨眼（每 3-7s 随机）、瞥眼（手动触发）、呼吸（idle 时眼睛大小微变）
- [ ] `face` 带 `sec` 参数：临时表情，到时自动回 `idle`
- [ ] disconnected 自救：5s 无 ping → 自动渲染失联脸

### Phase 3 — 接入 lelamp_runtime（半天）
- [ ] 新建 `lelamp/display/` 模块
- [ ] 实现 `ScreenClient`（pyserial-asyncio + 写队列 + 心跳 + 自动重连 + NoOp 降级）
- [ ] 修改 `main_soul.py` 启动序列，断连容错
- [ ] 在所有 `motion_svc.play_emotion(...)` 旁边补 `screen.set_face(...)`

### Phase 4 — Office mode（1 天）
- [ ] ESP32 端实现 office mode 屏（时钟 + 日期 + 天气 + 番茄钟 + 通知卡片）
- [ ] Python 端：在 soul_agent 加 `enter_office_mode` / `exit_office_mode` 工具
- [ ] 触发条件：
  - 用户语音 "进入办公模式" → enter
  - 用户语音 / 听到环境声音 → exit
  - 长时间无互动（>10min）→ 自动 enter

### Phase 5 — 触屏 + IMU 事件（看需求）
- [ ] ESP32 上报 `{"evt":"touch"}` / `{"evt":"shake"}`
- [ ] Python 端 `ScreenClient` 暴露回调
- [ ] 接到 soul_agent 作为新事件源（类似 HeardSpeech）
- [ ] 例：摇晃触发 dizzy、点触发关注响应

### Phase 6 — 嘴部同步动画（提升体验）
- [ ] TTS 开始/结束时通过 `face_micro` 触发嘴部 open/close 循环
- [ ] 字符级时间戳更准（如果 TTS 支持）

---

## 6. 验收 & 调试

### 6.1 串口监控

开发期同开两个终端：
```bash
# 终端 1：看 ESP32 打印
pio device monitor -b 115200

# 终端 2：发测试消息
python lelamp-display/test/host_send.py face happy
```

### 6.2 已知坑

| 现象 | 原因 | 解决 |
|---|---|---|
| Upload timeout | 自动 download 失败 | 按 BOOT → 短按 RST → 松 BOOT，再 Upload |
| Serial Monitor 没输出 | `ARDUINO_USB_CDC_ON_BOOT` 没开 | 改 ini 重 build |
| 编译报 PSRAM 找不到 | memory_type 没设 qio_opi | 检查 ini |
| Python write 卡住 | 串口被独占（PlatformIO Monitor 还开着）| 关掉 Monitor 再跑 |
| LVGL 卡死 / 花屏 | 在串口回调里直接调 lv_* | 改成入队，loop() 出队 |
| 屏幕跟着头动一甩就花 | LVGL 缓冲跨核操作 | 全部 LVGL 操作绑定到主 core |

### 6.3 性能基线

- 串口 921600 baud ≈ 92KB/s，远超需要
- LVGL + PSRAM 矢量脸 60+ FPS 没问题
- 端到端延迟（Python → 屏幕显示）应 < 50ms

---

## 7. 项目约束

参考 `lelamp_runtime/CLAUDE.md`：

> 每次修改涉及舵机运动的代码，必须检查速度限制：追踪完整调用链路到 send_action / sync_write，确认播放/回放逻辑使用时间戳对齐（而不是固定 fps），避免速度过快触发舵机过载保护。

本任务**不动舵机代码**，只新增显示模块。如果改到任何 motion 调用链，遵守此规则。

参考 `~/.claude/projects/.../memory/project_lamp_io_hardware.md`：

> 协议设计要兼顾 USB CDC 和 WebSocket 两种 transport。建立的不是某块板的代码，是一套 IO 抽象。

`screen_client.py` 内部要有 transport 抽象层，方便后续平替到 WebSocket（阶段 2）。

---

## 8. 不要做的事

- ❌ 不要把 ESP32 固件代码塞进 lelamp_runtime 仓库（独立工程 `lelamp-display`）
- ❌ 不要用 Arduino IDE，统一用 PlatformIO（工程化、版本锁定）
- ❌ 不要在 ESP32 端做任何 LLM/AI 推理，它只是显示+输入终端
- ❌ 不要让脸屏断连导致主程序崩（所有调用必须容错或走 NoOp）
- ❌ 不要在串口回调里调 LVGL（必须主循环出队）
- ❌ 不要改舵机/RGB 服务的实现（只是新增 screen 调用点）
- ❌ 不要把屏幕当"副屏"对待（它是脸，face mode 是默认态，黑屏 = 死）
- ❌ 不要用 PNG 做表情（用矢量画，可参数化、尺寸自适应、零加载延迟）

---

## 9. 给执行 agent 的提示

- 先读完本文档 + memory 中的 `project_lamp_io_hardware.md` + `lelamp_runtime/main_soul.py`
- 完成每个 Phase 后跑测试脚本验证，再进下一个
- ESP32 端代码改动后必须 Upload + Serial Monitor 跑一遍，肉眼看屏幕
- Python 端改动用 `python -m lelamp.display.screen_client --test` 这种方式做单元验证，**不要直接跑 main_soul.py 测试小改动**
- 如果遇到串口/驱动/Mac 权限问题，先记录现象，不要瞎改 sudo
