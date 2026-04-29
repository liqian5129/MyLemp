# USB CDC JSON 协议规范

> Mac(主控)与设备(Waveshare ESP32-S3-Touch-LCD-4B)之间通信的契约文档。
> 双方都按本规范实现,不允许私自扩展字段而不更新本文档。

---

## 0. 设计原则

1. **简单**:JSON 行协议,每行一个对象,`\n` 结尾。任何串口工具都能调试
2. **隐式状态过渡**:不引入"mode"概念。设备就是处于某个 state,Mac 改 state,设备响应
3. **设备自治**:idle 等"无任务"状态下,设备**自己**循环表情/呼吸,不需要 Mac 持续推命令
4. **协议中立**:本协议未来可平移到 BLE NUS / WebSocket,只换 transport 不改帧格式

---

## 1. 传输层

| 项 | 值 |
|---|---|
| Transport | USB CDC(原生 USB,不经过 UART 桥) |
| 编码 | UTF-8 |
| 帧分隔 | 每行一个 JSON 对象,以 `\n`(0x0A)结束 |
| 行内换行 | JSON 字符串里出现 `\n` 必须转义为 `\\n` |
| 行长上限 | 单行 ≤ 1024 字节(超长视为非法,设备回错误) |
| 波特率 | 不重要(USB CDC 不使用),建议设 115200 兼容串口工具 |

---

## 2. 报文总览

### 2.1 Mac → 设备(命令 / Command)

每条命令必须包含 `cmd` 字段。

| Cmd | 作用 | 必需字段 | 可选字段 |
|---|---|---|---|
| `ping` | 心跳探活 | — | `seq`(整数,设备 ack 时回传) |
| `set_state` | 切换主状态 | `state`(string) | `transition_ms`(过渡时长,默认 300) |
| `set_progress` | 设置进度(busy 状态用) | `pct`(0-100) | — |
| `set_text` | 设置文字区内容 | `id`(string), `text`(string) | — |
| `show_prompt` | 显示审批 prompt(attention 状态用) | `tool`(string), `command`(string), `id`(string) | `desc`(string,补充说明) |
| `set_brightness` | 屏幕亮度 | `value`(0-100) | — |

### 2.2 设备 → Mac(事件 / Event,ack / Ack)

每条消息必须包含 `evt` 或 `ack` 字段(二选一)。

| 类型 | 触发 | 字段 |
|---|---|---|
| `evt: ready` | 设备开机自发 | `evt`, `fw_version`, `device_id`(可选) |
| `evt: approval` | 用户在 attention 屏点 ✓ 或 ✗ | `evt`, `id`, `decision`("yes"\|"no") |
| `evt: tap` | 通用屏触摸(非按钮区) | `evt`, `x`, `y` |
| `evt: imu` | IMU 检测到拍打/摇晃/翻转 | `evt`, `type`("tap"\|"shake"\|"tilt") |
| `ack` | 每条命令的回执 | `ack`(原 cmd 名), `ok`(bool), 可选 `error`, 可选 `seq` |

---

## 3. 命令详细规范

### 3.1 `ping`

**Mac 发送**:
```json
{"cmd":"ping"}
{"cmd":"ping","seq":42}
```

**设备回**:
```json
{"ack":"ping","ok":true}
{"ack":"ping","ok":true,"seq":42}
```

**用途**:Mac 端定期(建议 5 秒)探活;设备超过 30 秒没收到任何命令时,可视为 Mac 离线,自主进入 idle 自治。

---

### 3.2 `set_state`

**Mac 发送**:
```json
{"cmd":"set_state","state":"busy"}
{"cmd":"set_state","state":"attention","transition_ms":500}
```

**设备回**:
```json
{"ack":"set_state","ok":true}
```

如果 `state` 不识别:
```json
{"ack":"set_state","ok":false,"error":"unknown_state"}
```

**合法 state 值**(本期):
- `sleep` — 闭眼睡眠
- `idle` — 醒着,无任务,设备自治循环表情
- `busy` — Claude 工作中
- `attention` — 等审批/需要用户决定
- `failed` — 任务失败/出错,需要用户注意但不一定要决定(不自动回 idle)
- `celebrate` — 完成/庆祝(1.5 秒后设备自动回到 idle)

**Idle 自治行为(关键)**:
设备在 idle 状态下,**自己驱动**以下行为,Mac 不需要发任何命令:
- 像素角色 7 个变体随机循环(眨眼 / 伸懒腰 / 看你 / 打哈欠 等)
- 屏背景温暖中性色微妙呼吸感渐变
- 灯臂微小幅上下浮动(由 Mac 端 ArmController 配合)

**Celebrate 自动回 idle**:
设备在 `celebrate` 状态保持 **4 秒**后,**自动**切回 `idle`,无需 Mac 再发命令。
(实测 1.5s/3s 都偏短,4s 是动画完整呈现的舒适时长。Mac 端 `CELEBRATE_DURATION` 同步。)

**Failed 不自动回 idle**:
设备进入 `failed` 后**保持该状态**(类似 attention),由 Mac 在用户处理完后显式切到 `idle` 或 `busy`。设备本身不计时退出。

---

### 3.3 `set_progress`

**Mac 发送**:
```json
{"cmd":"set_progress","pct":42}
```

**设备回**:
```json
{"ack":"set_progress","ok":true}
```

**约束**:
- 仅在 `busy` 状态有效;其他状态收到时 ack ok=true 但忽略
- `pct` 超出 [0, 100] 自动 clamp,不报错
- 进度环刷新建议节流到 10Hz(高频请求会被合并)

---

### 3.4 `set_text`

**Mac 发送**:
```json
{"cmd":"set_text","id":"subtitle","text":"Refactoring auth module..."}
{"cmd":"set_text","id":"meta","text":"3.2k tokens · 12s"}
```

**设备回**:
```json
{"ack":"set_text","ok":true}
```

**约束**:
- `id` 必须是设备已知的文字区(本期定义两个:`subtitle` / `meta`)
- 未知 id → ack ok=false, error="unknown_text_id"
- text 超过显示宽度自动截断或换行,不报错
- 单条 text 长度上限 200 字节(避免 UI 卡顿)

---

### 3.5 `show_prompt`

**Mac 发送**:
```json
{
  "cmd": "show_prompt",
  "id": "req_abc123",
  "tool": "Bash",
  "command": "rm -rf node_modules"
}
```

**设备回**:
```json
{"ack":"show_prompt","ok":true}
```

**行为**:
- 必须先切到 `attention` 状态(Mac 应该在 `show_prompt` 前发 `set_state attention`)
- 设备在屏上显示工具名 + 命令文本 + 两个大按钮(✓ / ✗)
- 用户点击按钮 → 设备发送 `evt: approval`(见 4.2)
- `id` 用于关联 prompt 和后续 approval 事件,必须唯一
- 同一时刻只能有一个 prompt;新 prompt 覆盖旧的

---

### 3.6 `set_brightness`

**Mac 发送**:
```json
{"cmd":"set_brightness","value":80}
```

**设备回**:
```json
{"ack":"set_brightness","ok":true}
```

**约束**:value [0, 100],0 = 屏熄但不关。

---

## 4. 事件详细规范

### 4.1 `evt: ready`

**设备开机时自发**(无需 Mac 触发):
```json
{"evt":"ready","fw_version":"0.1.0","device_id":"lelamp-display-01"}
```

**Mac 用途**:重连检测、固件版本检查。

---

### 4.2 `evt: approval`

**触发**:用户在 attention 屏的 ✓ / ✗ 按钮上点触

**设备发送**:
```json
{"evt":"approval","id":"req_abc123","decision":"yes"}
{"evt":"approval","id":"req_abc123","decision":"no"}
```

**约束**:
- `id` 必须与最近一次 `show_prompt` 的 `id` 一致
- 设备在按钮按下后,**先**显示 0.5 秒"已批准/已拒绝"反馈动画,**再**发出此事件
- Mac 收到事件后通常会发 `set_state busy` 让 Claude 继续

---

### 4.3 `evt: tap`

**触发**:用户在屏幕**非按钮区**的触摸

**设备发送**:
```json
{"evt":"tap","x":120,"y":80}
```

**用途**:Mac 端按当前状态决定路由。本期 Watchman 已用法:`failed` 状态下任意触屏 → Mac 切到 `idle`(语义:"用户已知晓错误")。其他状态下可作为通用交互入口(双击切场景、长按进设置等)。

---

### 4.4 `evt: imu`

**触发**:QMI8658 IMU 检测到特定动作

**设备发送**:
```json
{"evt":"imu","type":"tap"}     // 拍打设备
{"evt":"imu","type":"shake"}   // 摇晃
{"evt":"imu","type":"tilt"}    // 显著倾斜
```

**用途**:tap 可作为 attention 的快速 approval 备选(拍灯 = ✓)。

---

## 5. ack 详细规范

每条命令**必须**有对应的 ack 响应。

**成功**:
```json
{"ack":"<cmd_name>","ok":true}
```

**失败**:
```json
{"ack":"<cmd_name>","ok":false,"error":"<error_code>"}
```

**标准 error 码**:

| code | 含义 |
|---|---|
| `parse_error` | JSON 解析失败 |
| `unknown_cmd` | cmd 字段值不识别 |
| `missing_field` | 缺少必需字段 |
| `invalid_value` | 字段值非法(超范围、错类型) |
| `unknown_state` | set_state 的 state 不识别 |
| `unknown_text_id` | set_text 的 id 不识别 |
| `busy_only` | 命令仅在 busy 状态有效(可选,也可静默忽略) |

---

## 6. 时序与边缘情况

### 6.1 启动

```
设备开机
  ↓
loading 屏(可选,1-2 秒,如有资源加载)
  ↓
进入 sleep 状态
  ↓
发送 {"evt":"ready",...}
  ↓
等 Mac 命令
```

### 6.2 Mac 离线(连接断开 / 长时间无命令)

设备**自己**保持当前状态运行。如果当前是 idle,继续 idle 自治循环。**不要黑屏**(黑屏 = 设备死了的视觉印象,要避免)。

可选:30 秒无命令,可在屏角显示一个小的"disconnected"图标,不影响主显示。

### 6.3 Mac 重连

Mac 端探测到 USB CDC 设备后,**重新发**:
1. `ping`(确认链路)
2. `set_brightness`(恢复亮度设置)
3. `set_state idle`(回归基线)

设备无需做特殊处理,正常响应即可。

### 6.4 命令乱序 / 高频

- 设备允许命令任意顺序,不需要 Mac 等 ack 才能发下一条(全双工)
- 但 Mac 发送速率不要超过 50 Hz,设备处理能力有限
- 如果设备处理不过来,可能丢部分中间命令,但**最后一条 set_state 必须生效**

### 6.5 状态转换冲突

如果 Mac 在 1 秒内连发多个 set_state,设备**只生效最后一条**,中间动画可被打断。

---

## 7. 协议版本

| Version | Date | Changes |
|---|---|---|
| 0.1.0 | 2026-04-26 | 初版,含 5 状态 + 6 命令 + 4 事件 |
| 0.2.0 | 2026-04-26 | 新增 `failed` state(任务失败/出错,不自动回 idle,视觉上区别于 attention) |
| 0.2.1 | 2026-04-29 | celebrate 自动回 idle 时长由 1.5s 调整为 3s(实测动画播放不充分) |
| 0.2.2 | 2026-04-29 | celebrate 时长 3s → 4s(用户实测 3s 仍偏短) |

未来变更**必须**:
- 单调递增版本号
- 列出 breaking change(如有)
- 双方 agent 同步更新对接代码

---

## 8. 测试用 JSON 样本(供双方 agent 自测)

### 完整 Watchman demo 序列(Mac 发送)

```
{"cmd":"set_state","state":"sleep"}
{"cmd":"set_state","state":"idle"}
{"cmd":"set_state","state":"busy"}
{"cmd":"set_progress","pct":15}
{"cmd":"set_text","id":"subtitle","text":"Refactoring auth module..."}
{"cmd":"set_progress","pct":35}
{"cmd":"set_progress","pct":60}
{"cmd":"set_state","state":"attention"}
{"cmd":"show_prompt","id":"req_001","tool":"Bash","command":"rm -rf node_modules"}
(等待 evt:approval ...)
{"cmd":"set_state","state":"busy"}
{"cmd":"set_progress","pct":85}
{"cmd":"set_progress","pct":100}
{"cmd":"set_state","state":"celebrate"}
(1.5 秒后设备自动回 idle)
```

### 期望的设备响应

```
{"evt":"ready","fw_version":"0.1.0"}
{"ack":"set_state","ok":true}
{"ack":"set_state","ok":true}
... (每条 cmd 都有对应 ack)
{"evt":"approval","id":"req_001","decision":"yes"}
{"ack":"set_state","ok":true}
...
```
