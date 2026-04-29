# AI Working Companion — 双 Agent 实施计划

> 工作分给两个 Claude Code agent 并行执行,本文件是**两边共同的 source of truth**。

**配套文档**:
- 产品定位 → `dev-ai-working-companion.md`
- 协议规范 → `dev-protocol.md`(Step 1 由 Agent A 写)

**重要约束**:
- 机械臂**没有灯**。一切"光晕 / 颜色 / 状态指示"全部通过**屏幕颜色与图案**呈现(背景色、边框色、脉冲图案等)
- 用户看到灯臂的**姿态**(物理动作)+ 屏幕的**视觉**(角色 + 颜色 + 信息),两者合起来构成产品表达

---

## 0. 整体架构与分工

### 项目拆分

```
~/playground/
  lelamp_runtime/        ← Agent A 工作区(Python,Mac 侧)
    lelamp/
      transport/         ← 新建,USB CDC 客户端 + 控制器封装
      demo/              ← 新建,demo 编排
      recordings/        ← 新增 buddy_*.json 关键帧
    scripts/             ← 测试脚本
    dev-*.md             ← 共享文档
  
  lelamp_display/        ← Agent B 工作区(ESP32-S3 固件)
    platformio.ini       ← 工程配置
    src/                 ← 固件代码
    assets-source/       ← Aseprite 源文件
    docs/                ← 设备文档
```

### Agent A:lelamp_runtime(Mac 侧 Python)

**职责**:
- Mac 端的 demo orchestrator(整体演出节奏)
- USB CDC 客户端(发命令 / 收事件)
- 跟现有舵机控制系统对接(复用 `lelamp/motion/`)
- 录制 buddy 专用关键帧
- 屏 + 臂的同步控制

**不做**:
- 设备固件(归 Agent B)
- 屏幕 UI 设计与实现(归 Agent B)
- 像素角色绘制(归 Agent B)

### Agent B:lelamp_display(ESP32-S3 固件)

**职责**:
- Waveshare ESP32-S3-Touch-LCD-4B 固件
- LVGL UI(5 个状态屏 + 颜色/图案表达)
- USB CDC server(收 JSON、解析、回 ack/event)
- 像素角色动画渲染
- 屏幕颜色/边框作为状态"光晕"
- 触摸事件捕获 + 上报
- IMU 事件检测

**不做**:
- Python 编排(归 Agent A)
- 舵机控制(归 Agent A)
- Demo 时序逻辑(归 Agent A)

### 通信契约

**唯一耦合点**:USB CDC JSON 协议(`dev-protocol.md`)。

只要双方遵守协议,两端可以**完全独立开发**:
- Agent A 写完 DisplayController 后,可以用 mock 模式开发,不需要真硬件
- Agent B 写完固件后,可以用串口工具手动发 JSON 测试

---

## 1. 共享契约:USB CDC JSON 协议(高层概要)

> 完整规范见 `dev-protocol.md`(Step 1 由 Agent A 写)

### Mac → 设备(命令)

| 命令 | 作用 |
|---|---|
| `ping` | 心跳,设备 ack |
| `set_state` | 切换主状态(sleep / idle / busy / attention / failed / celebrate) |
| `set_progress` | busy 状态下的进度百分比 |
| `set_text` | 设置某个文字区(字幕/状态行等) |
| `show_prompt` | attention 状态显示审批信息(工具名 + 命令文本 + 请求 id) |
| `set_brightness` | 屏幕亮度 |

### 设备 → Mac(事件 / ack)

| 事件 | 触发时机 |
|---|---|
| `ready` | 设备开机自报 |
| `ack` | 每条命令的回执(包含成功/失败状态) |
| `evt: approval` | attention 屏上点按钮(yes / no) |
| `evt: tap` | 通用触摸 |
| `evt: imu` | IMU 检测到拍打/摇晃 |

### 帧分隔约定

- UTF-8 JSON 单行 + `\n` 结尾
- 每行一个对象,设备和 Mac 都按行解析
- JSON 字符串内的换行符要转义

---

## 2. 6 个状态的行为契约(双方共同遵守)

每个状态 = **屏幕表达(角色 + 颜色 + 信息)+ 灯臂姿态**。屏幕颜色承担了原本"环境光"的角色。

### Sleep
- **触发**:`set_state sleep`,或长时间无任何命令
- **屏角色**:像素 Claude 闭眼呼吸
- **屏颜色**:整体暗,深蓝紫调,亮度自动降低
- **臂姿态**:完全垂下
- **预期持续**:无限,直到下次状态变更

### Idle
- **触发**:`set_state idle`
- **屏角色**:像素 Claude 坐着,7 个变体随机循环(眨眼 / 伸懒腰 / 看你 / 打哈欠 等)
- **屏颜色**:温暖中性调(米色/暖灰),呼吸感渐变,克制
- **臂姿态**:微微呼吸感(小幅上下浮动,2 秒周期)
- **预期持续**:无限

### Busy
- **触发**:`set_state busy`,通常配 `set_progress`
- **屏角色**:像素 Claude 敲键盘,头顶偶尔出现思考符号
- **屏颜色**:工作冷白调(略偏蓝),稳定不闪
- **屏信息**:进度环 + tokens 数 + 已耗时
- **臂姿态**:朝屏幕方向倾斜(凑过去看 Mac 屏幕)
- **可附带**:`set_progress` 实时更新进度

### Attention
- **触发**:`set_state attention`,通常配 `show_prompt`
- **屏角色**:Claude 抬头看你,头顶问号闪烁
- **屏颜色**:**边框警告橙 #f59e0b 脉冲慢呼吸**(~0.5Hz),平静等待,强调"需要你决定"而非"出事了"
- **屏信息**:大字显示工具名 + 命令文本 + 两个大按钮(✓ / ✗)
- **臂姿态**:从 busy 姿态慢慢转向用户(2 秒过渡)
- **退出**:用户点按钮 → 设备发 `evt: approval` → Mac 通常回到 busy 继续

### Failed
- **触发**:`set_state failed`,通常在任务报错/异常退出后由 Mac 主动切入
- **屏角色**:Claude 沮丧/低头/裂痕表情,可叠加一次短促 emphatic 抖动(~0.3 秒,只在进入时一次,之后保持沮丧静态)
- **屏颜色**:**边框错误红 #ef4444**(常亮或极慢呼吸,**不快闪**),与 attention 的橙色慢呼吸通过**色相**而非**频率**区分(色相识别比频率快得多)
- **屏信息**:大字显示错误摘要(用 `set_text` 推送),**无按钮**(避免与 attention 混淆,这是与 attention 最关键的语义边界)
- **臂姿态**:从 busy 姿态保持但加入一次 emphatic 抖动(与屏角色抖动同步),之后静止
- **触摸行为**:屏上无按钮,用户若点屏 → 设备发 `evt: tap`(协议 4.3 已定义),由 Mac 决定下一步状态(通常切到 `idle` 表示用户已知晓)
- **退出**:**不自动回 idle**。Mac 收到 `evt: tap` 或在用户处理完后显式切到 `idle` 或 `busy`

### Celebrate
- **触发**:`set_state celebrate`
- **屏角色**:Claude 跳起 + 庆祝粒子,持续约 1.5 秒
- **屏颜色**:短暂彩虹脉冲(2 秒内回归)
- **臂姿态**:上下点头 2 次 + 左右晃,1.5 秒
- **预期持续**:1.5 秒后由 Mac 切回 idle

---

## 3. Agent A:lelamp_runtime 步骤计划

### ✅ Step 1:协议规范 + 测试脚手架

**目标**:写好协议文档,搭好基础模块结构

**任务**:
1. 写 `dev-protocol.md` —— 完整协议规范(扩展第 1 节,包含每条命令/事件的字段、数据类型、错误码、超时约定)
2. 新建 `lelamp/transport/` 包,设计 `DisplayController` 类的接口骨架(可以先用 mock 实现)
3. 写一个最小测试脚本,能自动找 USB CDC 设备并发送 ping 命令

**测试方法**:
- 协议文档 review:Agent B 看完能不能照着实现固件
- 测试脚本在 mock 模式下能跑通(打印发送的 JSON,模拟接收 ack)
- 等 Agent B 烧好固件后,真实运行能收到设备的 ack 响应

---

### ✅ Step 2:DisplayController 完整命令集

**目标**:封装所有协议命令为 Python API,事件接收异步化

**任务**:
1. 实现所有命令的发送方法(set_state / set_progress / set_text / show_prompt / set_brightness)
2. 实现事件接收(后台读串口,把事件放进队列)
3. 实现 `wait_for_approval(timeout)` 阻塞等待审批事件
4. 写一个 `test_display.py`,逐条测每个命令的效果

**测试方法**:
- 跑测试脚本,目视确认屏幕能正确响应每条命令
- 5 个状态各停留几秒,确认视觉切换正确
- 触摸屏上的按钮,确认 approval 事件能被 Python 收到
- 故意发非法命令(不存在的 cmd),确认收到 `ok: false` 响应

---

### Step 3:ArmController 封装

**目标**:把现有舵机控制包装成跟 DisplayController 对称的接口

**任务**:
1. 新建 `lelamp/transport/arm.py`,封装一个 `ArmController` 类
2. 接口包括:`play_keyframe(name)` / `set_state(state)`(状态→关键帧映射)
3. 复用 `lelamp/motion/` 现有的 motion_agent 与 play_keyframes
4. 跟 Agent B 约定 5 个 buddy 专用关键帧的"期望感觉"(为 Step 5 录制做参考)
5. 写 `test_arm.py`,跑 5 个状态的动作,目视验收

**测试方法**:
- 跑测试脚本,目视每个状态的灯臂动作是否合理
- 状态切换时动作是否流畅,有无卡顿或回弹
- 机械安全:速度合理,无过载,关节角度不超限

---

### Step 4:Demo Orchestrator 骨架

**目标**:能同时控制屏 + 臂,跑出第一个完整 demo(Watchman)的雏形

**任务**:
1. 新建 `lelamp/demo/` 模块
2. 设计 `DemoBase` 基类,管理 disp + arm 同步切换状态
3. 实现 `transition_to(state)`,内部同时通知屏和臂,保证两边对齐
4. 写第一个 demo:`watchman.py`,按时间线脚本化跑完整 Watchman 流程
5. 用配置化方式描述时间线(每个阶段持续多久、何时切状态、何时显示 prompt)

**测试方法**:
- 命令行启动 watchman demo,完整跑一遍
- 观察:屏幕变化与机械臂动作是否同步,节奏是否舒服
- 不需要真 Claude,完全脚本化跑
- 触摸 ✓ / ✗ 按钮,流程能正确分支

---

### Step 5:实现 buddy_* 程序化动作

**前置**:Agent B 已经把 6 个屏幕状态做好(包括视觉),Mac 端可对照屏上状态调试动作节奏

**任务**:
1. 在 `lelamp/transport/buddy_motions.py` 里**用代码生成**每个状态的关节轨迹(不走录制),输出符合 motion_agent segment 格式的轨迹片段:
   - `buddy_sleep` — 关节全归零位(完全垂下),静态
   - `buddy_idle` — 微呼吸:`base_pitch` ±2° 正弦,周期 2 秒,循环
   - `buddy_busy` — 凑屏倾斜姿态(`base_pitch` 偏向 Mac 屏方向)+ 偶尔小幅扰动
   - `buddy_attention` — 从 busy 慢转向用户:`base_yaw` 平滑插值到正前方,2 秒
   - `buddy_failed` — 进入时一次 emphatic 抖动(~0.3 秒,±2°),与屏角色抖动同步;之后保持 busy 姿态静止(对应 Section 2 的"沮丧静态")
   - `buddy_celebrate` — 点头 + 晃:`base_pitch` / `base_yaw` 复合脚本,1.5 秒
2. 在 `arm.py` 的 `set_state(state)` 里调用对应程序化动作
3. 跑 demo,确认动作配合屏幕表达成立

**为何不录制**:程序化动作可参数化(振幅/频率/时长),迭代成本低;这 6 个动作本身简单(呼吸/抖动/倾斜),录制反而引入噪声。

**测试方法**:
- 对镜头录视频,回放检查每个动作"能不能看懂意思"
- 不解释的情况下,看的人能否猜出当前是什么状态(sleep / busy / attention / failed 等)
- 整体节奏:不太快(显得慌),不太慢(显得呆)
- 调参方便性:振幅/频率改一个参数就能重跑,5 分钟内能完成一轮迭代

---

### Step 6:Watchman 完整打磨

**目标**:Watchman demo 达到可拍视频的质量

**任务**:
1. 反复跑 Watchman,每跑一遍调一个细节(节奏 / 时长 / 同步性)
2. 把时间线参数化,易于调整(JSON 或 YAML 配置)
3. 加故障处理:USB 断开 / 设备 reset / 触摸超时,Mac 端要优雅恢复
4. 编写"自动 demo loop"模式,能反复演给现场观众看不需要重启

**测试方法**:
- 连续跑 20 遍,无异常
- 拔 USB 中途 → 不崩,Python 端有清晰报错
- 重插 USB → 自动重连或有明确恢复指引
- 任何时刻按 Ctrl+C → 灯臂回到安全姿态后退出

---

## 4. Agent B:lelamp_display 步骤计划

### Step 1:工程初始化 + 烧官方例程

**前置**:
- 板子在手
- Mac 上装好 PlatformIO Core 或 Arduino IDE
- 装好 Aseprite

**任务**:
1. 新建项目目录 `~/playground/lelamp_display/`,git init
2. PlatformIO 工程初始化,板型选 ESP32-S3-DevKitC-1
3. 配置 platformio.ini(详细配置项见 Section 7.1)
4. 下载 Waveshare 官方例程包,先用 Arduino IDE 烧 `06_LVGL_Arduino_v9` 验证硬件
5. Aseprite 准备:新建 5 个 96×96 角色画布(`sleep` / `idle` / `busy` / `attention` / `celebrate`),应用统一调色板(见 Section 7.2)

**测试方法**:
- 烧好后屏幕亮起 + 显示 LVGL widget
- 触摸屏幕有响应
- Aseprite 5 个空白文件就位,调色板正确

---

### Step 2:迁移到 PlatformIO + USB CDC echo

**目标**:协议链路打通,Mac 发 ping 能收到 ack

**任务**:
1. 把 Arduino 例程迁移到 PlatformIO 工程结构
2. 解决迁移中的常见问题(LVGL 配置、PSRAM、partitions)
3. 在主循环里加 USB CDC 监听,逐行读取 JSON 并解析
4. 实现两条最简命令:`ping`(回 ack)、`set_text`(屏上显示传入的文本)
5. 开机时自动发送 `ready` 事件,包含固件版本号
6. 错误处理:JSON 解析失败时,回 `ok: false` 响应

**测试方法**:
- 用串口工具(screen / minicom)手动发 `{"cmd":"ping"}` + 回车
- 期望:屏幕回显 ack JSON
- 重启板子,期望串口立刻看到 ready 事件
- 跟 Agent A 联调:他的 `test_protocol.py` 能完整跑通

---

### Step 3:5 个 LVGL 状态屏 + 状态机

**目标**:每个状态有独立屏幕布局 + 颜色表达 + 占位视觉,可通过协议命令切换

**任务**:
1. 设计 5 个 LVGL screen 的布局:
   - 角色显示区(中央)
   - 状态信息区(底部:进度 / 文字 / 按钮)
   - **屏幕背景色或边框色**(承担"光晕"功能,见第 2 节各状态颜色规则)
2. 实现状态机:维护当前状态,收到 `set_state` 切换屏
3. 实现各状态特有命令的处理:
   - busy 屏:`set_progress` 更新进度环
   - attention 屏:`show_prompt` 显示工具名+命令+按钮
   - 通用:`set_text` 更新指定 label
   - 通用:`set_brightness` 调亮度
4. 触摸事件:attention 屏的按钮触摸 → 发 `evt: approval`(包含传入的 id)
5. 占位视觉先用大字 + 简单形状(真的角色 Step 4 才填)

**测试方法**:
- 手动发各种 `set_state` 命令,屏切换正确
- 进度环刷新平滑无闪烁
- 触摸按钮,串口收到 approval 事件,id 与 show_prompt 传入一致
- 屏幕颜色随状态变化(attention 时边框真的脉冲红)
- 跟 Agent A 联调:他的 `test_display.py` 完整跑通

---

### Step 4:像素角色视觉 + 集成

**目标**:5 个状态有真正的像素 Claude robot 动画,视觉风格统一

**任务**:
1. 在 Aseprite 画 5 个状态的像素 Claude robot:
   - 96×96 主帧
   - 每个状态 2-4 帧动画(总共最多 20 张图)
   - 风格参考:Anthropic buddy 的 bufo / GitHub Octocat / Quake idle
   - 严格使用 Section 7.2 的 16 色调色板
2. 导出为 LVGL 可用的图片资源(C 数组 或 PSRAM 加载)
3. 集成到对应 LVGL screen,用 LVGL 动画机制做帧切换
4. Idle 状态实现 7 变体随机循环(每隔 5-10 秒换一个动作:眨眼 / 伸懒腰 / 看你 / 打哈欠等)
5. 状态切换时加过渡动画(淡入淡出或滑动)

**测试方法**:
- 切到 idle:像素 Claude 各种 idle 行为随机出现,3 分钟无重复感
- 切到 busy:Claude 在敲键盘,看得出"工作中"
- 切到 attention:Claude 抬头看你,有"等待你"的意图感
- 切到 celebrate:跳跃庆祝,1.5 秒内结束
- 视觉效果"像活的",不是静态贴图

---

### Step 5:Touch / IMU 完善 + 稳定性

**目标**:交互流畅、设备长时间稳定

**任务**:
1. 触摸优化:防误触、按下视觉反馈、确认动画(按下后先显示 0.5 秒"已批准/已拒绝"再发 evt)
2. IMU 集成:检测拍打、摇晃、姿态翻转,作为 `evt: imu` 上报
3. JSON 解析的健壮性:畸形输入不崩溃,有清晰错误响应
4. USB CDC 断开重连:Mac 端拔线再插,固件自动恢复,不需要重启
5. 性能:60 帧每秒不掉帧,无内存泄漏

**测试方法**:
- 反复 50 次状态切换,无丢帧无崩溃
- 拔 USB 后,屏继续运行(idle 状态),不死机
- 重插 USB,Mac 端恢复通信
- 拍打板子,Mac 收到对应 IMU 事件
- 故意发畸形 JSON(缺字段、类型错误、超长字符串),设备稳定不崩

---

### Step 6:打磨视觉细节

**目标**:视觉感受达到可拍 demo 视频的质量

**任务**:
1. Idle 状态的"呼吸感":屏幕背景色微妙渐变 + 角色微动作 + 边框微弱光晕
2. 状态切换的过渡动画:不是硬切,是有过渡(2-300 毫秒淡入淡出或滑动)
3. 字体选择:程序员友好(等宽字体如 JetBrains Mono / Berkeley Mono),适合代码显示
4. 屏幕亮度自适应(可选):根据环境光自动调
5. 整体视觉感受打磨:对比 Anthropic Hardware Buddy 的 GIF,我们的视觉**应该更精致**(因为分辨率高得多)

**测试方法**:
- 跟 Agent A 联调,完整跑 Watchman demo,视觉表现到位
- 屏蔽其他干扰,把灯放桌上看 5 分钟,**自然感觉**(不刺眼、不无聊、不打扰)
- 给目标用户(同事程序员)看,不解释,他们能否说出"这是个 AI 助手"

---

## 5. 集成里程碑(双方必须对齐的时间点)

### Milestone 1:协议链路 alive(Step 2 末)

- Agent A 的测试脚本能发 ping + set_text
- Agent B 的固件能解析命令 + 回 ack + 上报 ready
- 验收:`test_protocol.py` 全绿

**任何一方卡住,在这里集合,不要绕过**。

---

### Milestone 2:5 个状态可视 + 可控(Step 3-4 末)

- Agent A 的 DisplayController 完整实现 + ArmController 跑通
- Agent B 的 5 状态屏 + 真角色动画 + 颜色表达
- 验收:Mac 端 cycle 5 状态,屏 + 臂(占位动作或真动作)都对应

---

### Milestone 3:Watchman demo 跑通(Step 5-6 末)

- Agent A 的 watchman demo 完整脚本化跑通
- Agent B 的固件长时间稳定
- 验收:启动 demo 命令,完整流程一气呵成,不需要人工干预

---

### Milestone 4:Demo-ready

- 节奏打磨完成,任何人看一遍能"懂"
- 故障情况都有优雅处理
- 准备进入视频拍摄阶段

---

## 6. 测试策略

### 单元测试(每步)

每个 agent 自己负责:
- Agent A:每个 transport 模块和 demo 模块都要有对应的测试脚本
- Agent B:用串口工具手动测每条命令,或写自动化的固件 self-test 模式

### 集成测试(Milestone 节点)

- 双方一起对着实物跑 demo 序列
- 录视频回看节奏问题
- 列出每个 milestone 的 acceptance criteria,逐条对照

### 故障测试(Step 5-6)

- 拔 USB 中途 → 不崩
- 设备 reset → 自动恢复
- Mac 进程崩 → 设备回到 idle 不卡死
- 触摸狂点 → 不丢事件不重复
- USB CDC 高频发命令 → 不丢帧

### 视觉验收(Step 6 之后)

- 三个核心场景各自录 30 秒视频,**自己反复看**:
  - 第一眼能看懂吗?
  - 节奏舒服吗?
  - 视觉上像"产品"吗?
- 给 3-5 个目标用户(同事/朋友里的程序员)看,**不解释**,看他们的第一反应
- 反应记下来,作为后续打磨依据

---

## 7. 附录

### 7.1 PlatformIO 关键配置项(Agent B Step 1 用)

需要在 `platformio.ini` 配置的关键参数:

| 参数 | 作用 |
|---|---|
| `platform = espressif32` | 平台 |
| `board = esp32-s3-devkitc-1` | 板型(Waveshare 兼容) |
| `framework = arduino` | 用 Arduino 框架 |
| `board_build.partitions = huge_app.csv` | 16MB Flash 大分区 |
| `board_build.psram_type = opi` | OPI PSRAM(8MB) |
| `board_build.memory_type = qio_opi` | 内存类型 |
| `board_build.flash_size = 16MB` | Flash 容量 |
| build flag `BOARD_HAS_PSRAM` | 启用 PSRAM |
| build flag `ARDUINO_USB_CDC_ON_BOOT=1` | 启用原生 USB CDC ⭐ 关键 |
| build flag `ARDUINO_USB_MODE=1` | USB 模式 |
| build flag `LV_CONF_INCLUDE_SIMPLE` | LVGL 配置 |

依赖库(`lib_deps`):
- GFX Library for Arduino(LCD 驱动)
- LVGL v9(UI 框架)
- SensorLib(IMU / RTC / 触摸驱动)
- ArduinoJson(JSON 解析)

### 7.2 像素角色配色方案(16 色调色板)

| 用途 | 颜色 |
|---|---|
| 背景 / 角色描边 | 深空灰 #1a1a1a |
| 角色阴影 | 中性灰 #4a4a4a |
| 角色高光 / 次要文字 | 浅灰 #9a9a9a |
| 主要文字 / 角色高亮 | 白 #f5f5f5 |
| Idle 暖光元素 | 工作灯暖白 #f5e6c8 |
| Claude robot 主色 | Claude 橙 #cc785c |
| Claude robot 阴影 | 暗橙 #8b4d3a |
| 完成 / 成功 | 状态绿 #4ade80 |
| Attention pending | 警告橙 #f59e0b |
| 失败 / 拒绝 | 错误红 #ef4444 |
| 网络 / 同步 | 连接蓝 #3b82f6 |
| Token / cost | 紫 #a855f7 |
| Info | 青 #06b6d4 |
| 透明 | alpha |
| 预留 | 白 / 黑 |

### 7.3 屏幕颜色作为"光晕"的设计指南

每个状态除了角色和信息,**屏幕整体色调也是状态表达的一部分**。建议实现方式:

- 整屏背景色:微妙的色调变化(idle 暖、busy 冷、sleep 暗)
- 边框光晕:屏幕边缘 8-16 像素宽的渐变带,可以脉冲(attention 红 / celebrate 彩虹)
- 渐变方向:从中心向外扩散,模拟"光从屏发出"的感觉
- 节奏:呼吸感(2-3 秒周期),不要快闪(干扰阅读)
- 强度:不抢戏,但余光能感知

### 7.4 关键资源链接

- Waveshare wiki:https://www.waveshare.net/wiki/ESP32-S3-Touch-LCD-4B
- Waveshare 例程包:wiki 底部"示例程序"链接
- LVGL 文档:https://docs.lvgl.io/9.3/
- ArduinoJson:https://arduinojson.org/
- Aseprite:https://www.aseprite.org/
- Anthropic Hardware Buddy 参考实现:`/Users/qliau/playground/claude-desktop-buddy`

### 7.5 双 Agent 工作流约定

每个 Agent 工作区里建议放一个 CLAUDE.md,内容指向本文档:

**`lelamp_runtime/CLAUDE.md` 追加段落**:
> ## AI Working Companion 实施
>
> 完整计划见 `dev-implementation-plan.md`。
> 当前 Agent 负责 Section 3(Agent A:lelamp_runtime)的 Step-by-Step 任务。
>
> 不要修改设备固件代码(那是 lelamp_display 的事)。
> 两边唯一的对接点是 `dev-protocol.md` 定义的 USB CDC JSON 协议。

**`lelamp_display/CLAUDE.md`**:
> ## AI Working Companion 设备固件
>
> 完整计划见 `~/playground/lelamp_runtime/dev-implementation-plan.md`(权威 SSOT)。
> 当前 Agent 负责 Section 4(Agent B:lelamp_display)的 Step-by-Step 任务。
>
> 不要修改 Mac 端 Python 代码(那是 lelamp_runtime 的事)。
> 两边唯一的对接点是 `~/playground/lelamp_runtime/dev-protocol.md` 定义的 USB CDC JSON 协议。

---

## 8. 文档维护

- 任何协议变更 → 更新 `dev-protocol.md`,**两边 Agent 都要同步**
- 任何 milestone 调整 → 更新本文档第 5 节
- 任何状态行为变化 → 更新本文档第 2 节
- 已完成的 step → 在对应 step 标题前加 ✅
- 颜色 / 视觉规则变更 → 更新第 2 节 + 7.2/7.3
