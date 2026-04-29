# AI Working Companion

> 桌面上的 AI 工作伴侣,为 AI-native 程序员而生。

---

## 0. 文档目的

本文档是产品的 **single source of truth**,涵盖:产品愿景、目标人群、硬件选型、痛点分析、核心场景、MVP 功能、设计原则、商业路径、验证计划。

**适用阶段**:从概念到 MVP demo 视频上线。后续进入开发与量产阶段后,各模块拆分到独立文档。

**最近更新**:2026-04-26

---

## 1. 产品概念

### 一句话

> **AI Working Companion — 你 AI 团队的工作伴侣,在桌面替你盯进度、举手叫你做决定、守护你的深度工作。**

### 形态

桌面上的一个**有关节臂 + 屏幕 + 灯光**的小型设备,通过 USB-C 单线供电+通信连接 Mac(短期)。

外形参考:深空灰金属 + 极简光眼 + Pixar Luxo Jr 式关节臂 + 底部光环。**不萌、不卡通、不家居**,**像 Teenage Engineering / Nothing / 高端办公装备**。

### 核心价值主张

- **不是台灯**(虽然外形像)— 不为照明,为表达
- **不是桌宠**(虽然有性格)— 不为情感寄托,为工作协作
- **不是副屏**(虽然有显示)— 不为信息密度,为状态感知
- **是 AI 时代的桌面工作伴侣** — 一个新品类

---

## 2. 目标人群

### 用户画像:Alex

```
年龄:    28-40
职业:    独立开发者 / 创业 CTO / 高级工程师 / AI 产品经理
收入:    $80K-300K / 年薪 ¥40-150 万
工具栈:  Claude Code、Cursor、Copilot、Linear、Slack/Lark、Notion
已有装备: HHKB 键盘、4K 显示器、Aer 椅、AirPods Max、Framework 笔记本
工作模式: WFH 60% + 开放工区 40%,深度工作 4-6h/天
```

### 内心独白

> "我桌上 ¥3000 的键盘都不眨眼,但要让我买'桌宠',我会觉得幼稚。"

→ **这群人不会买"桌宠",但会买"AI 时代的程序员道具"**。

### 为什么选择这个人群

| 优势 | 说明 |
|---|---|
| 付费意愿强 | 已经为 Claude/Cursor 月付 $20-200 |
| 客单价能撑住 | 装备升级心智成熟($300-500 决策不沉重) |
| 自带传播力 | Twitter/X / 即刻 / V2EX 上爱晒装备 |
| 反馈质量高 | 工程师的 bug 报告与产品建议远高于普通用户 |
| 可识别可触达 | 渠道清晰(HN / PH / Indie Hackers / 即刻) |
| 场景天然契合 | 6-10h/天对着桌面,产品有持续在场机会 |

### 不目标的人群

- ❌ 大众消费者(中国"桌宠"市场已被验证不温不火)
- ❌ 萌系爱好者(产品语言不卡通)
- ❌ 设计师/创意从业者(他们的痛点不在 AI agent 工作流)
- ❌ 学生群体(预算与场景都不匹配)

---

## 3. 真实痛点(产品立论的核心)

### 时代背景:2025-2026 程序员工作模式的剧变

```
2024 年:  程序员用 AI 写代码(Copilot 补全)
2025 年:  程序员让 AI 跑 5-15 分钟多步任务(Claude Code agent mode)
2026 年:  程序员同时管理 2-5 个 AI agent(Claude + Cursor + Devin + ...)
```

**程序员的角色正在从"写代码者"变成"AI 团队的指挥官"**,但配套工具几乎是空白。

### 5 个真实新痛点

#### 痛点 1:Async Agent 监督焦虑 ⭐ 最强
你给 Claude Code 一个 10 分钟任务,出去倒咖啡。回来不知道:
- 完成了吗?成功还是失败?
- 卡住了吗?在等审批吗?
- 还在跑还是 hung 了?

**现状**:每隔 1-2 分钟切窗口看一眼,**心思一直分散,做不了别的事**。

#### 痛点 2:Multi-agent 协调地狱
- Claude 在搞 backend
- Cursor 在搞 frontend
- GitHub Action 在跑 CI
- Devin 在写测试

**现状**:切窗口切到崩溃,经常错过哪个 agent 等审批。

#### 痛点 3:Approval 决策疲劳
- 一天审批 30 次,第 20 次开始机械点 yes
- 直到某次批了不该批的命令(真实安全风险)

#### 痛点 4:Deep work 时 AI 后台运行
- 自己在写算法,AI 在后台跑
- 不想被打断,但又怕错过 AI 完成
- 屏幕角落浮窗依然分心

#### 痛点 5:Token / Cost 黑盒
- 一天烧了多少钱?月预算还剩多少?
- 控制台不在视线里,**只能等账单焦虑**

### 痛点的稀缺性

这些痛点的特点:
- ✅ **2025 下半年才真的出现**(Agent 工作流刚起飞)
- ✅ **大部分人在感受但还没"标准解法"**
- ✅ **未来 12-24 个月持续放大**(Agent 越多越痛)
- ✅ **没有现成竞品做这个角色**(GitHub/Cursor/Claude 自家都没做)

→ **存在 12-18 个月的产品窗口期**。

---

## 4. 产品定位探索的演进

(此节记录关键决策路径,避免后续重复讨论)

### 阶段一:从"智能台灯"到"桌面角色"

最初思考是"会陪你写代码的智能台灯",用"它本来就是灯"的实用性给产品找正当性。

**否定**:灯本质是**气氛表达**,不是照明工具。"它本来就是灯"是拐杖。

**修正**:不再以"实用台灯"为主轴,接受"它是个长得像灯的角色"。

### 阶段二:陪伴 vs 功能 — 是否矛盾?

两个方向看似冲突:
- 方向 1:陪伴/情绪表达(关节臂表情仪式感)
- 方向 2:副屏功能(时钟/通知/日历/状态)

**初步判断**:它们是竞争身份,不能并存。

**修正**:它们是同一物体的**两种模式**,不矛盾。关键是:
- **共享一个叙事内核**:"它一直活着。工作模式是它在帮你,生命模式是它在自处。"
- **核心模式**:**功能性需求,通过有生命感的方式表达**。例:通知不是 push 弹窗,是它"凑过来看你"。

### 阶段三:商业可行性的拷问

诚实评估:
- ❌ 副屏功能本身在中国/海外都验证为弱品类(Tidbyt 全球 8 年累计 5-10 万台)
- ❌ 单纯陪伴价值在中国市场付费意愿低(国产桌宠没跑出来过)
- ✅ **但三件事叠加可以撑商业**:功能锚点 + 生命感 + 美学/身份

**结论**:做**小众文化产品**,不追大众。对标 Playdate / Daylight Computer / Teenage Engineering。

### 阶段四:目标人群锁定

确定面向**程序员 / AI 重度用户 / AI-native 知识工作者**。原因:
- 付费意愿高、传播力强、场景持续在场
- 海外市场(英文圈)优先,国内圈层(即刻/V2EX/小红书)辅助

### 阶段五:产品 metaphor 与场景重设

第一版 demo(Approval Moment / Quiet Companion / Ship It Together)被自我否定 —— 解决的是 vitamin 不是 painkiller。

**重新挖掘真实痛点**(见第 3 节),发现 2025-2026 的新痛点:**Agent 工作流的失序**。

**新 metaphor**:**AI Working Companion** —— 你 AI 团队的工作伴侣。

---

## 5. 核心 Demo 场景(MVP 必须能演示这三个)

### Demo 1:The Watchman — 解决 Async 焦虑(最强卖点)

```
[场景]
Alex 给 Claude Code 一个 10 分钟任务:
"重构这个模块,用新 API,跑测试,推 staging"
他走开倒咖啡。

[灯的视觉语言 — 不看屏幕就懂]
头垂下 + 光熄        = 完成,全绿,你可以走了
头朝你 + 红环呼吸    = 卡住了,需要你
头朝屏 + 黄环慢转    = 还在跑,正常
头颤抖 + 红环快闪    = 失败了,看错误

[Alex 倒咖啡回来,瞄一眼灯]
"哦还在跑" → 继续做别的事
3 分钟后灯转向他 → "等审批" → 走过去 tap 屏幕
2 分钟后灯垂下 → "好了" → 不用再看屏幕

[字幕]
"Stop alt-tabbing every 30 seconds.
Your AI's status is in your peripheral vision."
```

**杀伤力**:用过 Claude Code agent mode 的人,visceral 级别理解。"我每天浪费 1 小时在切窗口检查 agent 进度" → 这个解决了它。

---

### Demo 2:The Foreman — 多 agent 协调

```
[场景]
Alex 同时跑:
  - Claude:重构 backend
  - Cursor:重写 frontend
  - Devin:写 e2e 测试
  - GitHub Action:跑 CI

[灯屏幕]
极简显示 4 个进度环,每个一个 agent。
某个 agent 卡住 → 那个环变红 → 灯整体倾向 Alex。

[Alex tap 那个环]
屏切到那个 agent 的详情,他给指令,继续。

[字幕]
"Managing 4 AI agents at once.
Without losing your mind."
```

**杀伤力**:展示**未来 12 个月所有 AI 重度用户都会面对的问题**。提前为这个时刻做了工具。

---

### Demo 3:Don't Look At Me, I'm Working — 深度工作守护

```
[场景]
Alex 进入深度工作,自己手写一段算法。
此时 Claude 在后台跑另一个任务。
Slack 来了 5 条消息,飞书来了 2 个会议提醒。

[灯]
姿态前倾"凑屏"(知道 Alex 在写)
主动屏蔽所有非紧急通知
只有 Claude 任务关键节点(完成/卡住/审批)才报告

[15 分钟后 Alex 抬头喘口气]
[灯] 缓慢转过来
[屏] "While you focused:
       5 Slack (1 important),
       2 Lark (one is your standup in 8min),
       Claude finished the refactor."

[Alex 一眼看完,做决定]

[字幕]
"You focus. It filters.
Reports back when you breathe."
```

**杀伤力**:focus mode 是程序员永恒话题。**比所有勿扰 app 做得更彻底**,因为有物理实体可以主动当看门人。

---

## 6. MVP 功能优先级

### 必做(MVP 核心,4 周内出 demo)

| 功能 | 对应 Demo | 技术依赖 |
|---|---|---|
| Claude Desktop App BLE 集成 | Demo 1 | Anthropic Hardware Buddy 协议 |
| 状态可视化(灯光/姿态/表情) | Demo 1, 2, 3 | LVGL + 舵机 + LED |
| 物理审批交互(tap 屏 / 拍灯) | Demo 1 | 触摸 + IMU |
| Async 任务进度环 | Demo 1, 2 | LVGL widgets |
| Deep work 通知过滤 | Demo 3 | Mac 端通知聚合脚本 |

### 应做(MVP 上线后 1-3 个月)

| 功能 | 价值 |
|---|---|
| Cursor / Copilot / Windsurf 集成 | 扩展同人群覆盖 |
| GitHub Actions / CI 状态 | Multi-agent 中的 CI 通道 |
| Pomodoro / Focus timer + 物理勿扰 | Deep work 强化 |
| Slack / Lark DM 过滤(只 DM 不噪音) | Demo 3 强化 |
| Calendar next event 倒计时 | 上下文管理 |
| Token / Cost 实时显示 | 痛点 5 |

### 不做(明确砍掉,不要重新讨论)

| 不做 | 原因 |
|---|---|
| ❌ 个人微信 / iMessage | API 不开放,投入产出比低 |
| ❌ 视频通话 / 自拍 vlog | 形态不对,4 寸屏不适合 |
| ❌ 音乐播放器 | Mac/手机已经做得很好 |
| ❌ 通用 AI 聊天 | 那是 Claude / ChatGPT app 的事 |
| ❌ 大众功能堆叠(天气小组件等) | 稀释定位,变成"另一个智能屏" |
| ❌ 摄像头本体 | 短期不需要,Mac 摄像头足够 |

> **关于"萌系/卡通"的精确边界**:见第 7 节"分层美学规则"。硬件外形与 active 状态严格保持极简办公感,但 idle / 屏保 / 角色包可以有可爱表达 —— 这跟 Anthropic Hardware Buddy 的 18 个 ASCII 宠物 + GIF 角色机制一致。

---

## 7. 设计语言(Design North Star)

### 视觉锚点

参考形象:Pixar Luxo Jr × Teenage Engineering × Nothing Phone

| 维度 | 决定 | 反例 |
|---|---|---|
| 主色 | 深空灰 / 哑光黑 + 暖白光点缀 | ❌ 任何亮色、马卡龙色 |
| 材质 | 金属 + 磨砂塑料 | ❌ 软胶、绒毛、亮面塑料 |
| Active 状态屏幕 | 极简(光眼 + 进度环,信息优先) | ❌ active 时显示卡通脸 |
| 关节臂 | 工业级,像麦克风臂或台灯臂 | ❌ 露出舵机、塑料关节感 |
| 屏 | 4 寸,active 大部分时间黑屏或极简显示 | ❌ 满屏 widget 信息密度爆炸 |
| 底座 | 沉稳金属 + 一圈光环 | ❌ 卡通造型、贴纸 |
| 包装 | Apple / TE 级别开箱体验 | ❌ 普通快递盒 |

### 角色化 UI 与执行原则(关键!)

**核心更正**:之前过度限制"active 状态不能有角色"。事实上 **任何状态都可以有角色动画**(Anthropic buddy 在 busy/attention 状态也有动画),区别在 **执行质量**。

#### 6 条执行原则(无论 active / idle 都适用)

| 原则 | 含义 | 反例 |
|---|---|---|
| **服务功能** | 动画本身要传达信息,不只是装饰 | 庆祝时整屏烟花 5 秒 → 看不到状态 |
| **单焦点** | 一屏一个主动画,不要群魔乱舞 | 4 个 widget 同时弹跳 |
| **节制频率** | 每秒 1-2 帧的呼吸感,不是 60fps 嗨 | 角色一直在跳 |
| **风格统一** | 像素风 / 线稿 / 几何,选一种贯穿 | 像素角色 + 3D 图标混搭 |
| **可读性** | 关键信息要 1 眼能看懂 | 文字被花哨边框淹没 |
| **品牌一致** | 跟硬件外形语言不冲突 | TE 风硬件 + Disney 角色 |

满足这 6 条 → active 状态完全可以有 "Claude robot 敲键盘"这种角色动画
不满足 → 即使 idle 也不应该用

#### 角色"画风"与硬件外形的搭配

| 硬件风格 | 兼容的角色画风 | 不兼容的画风 |
|---|---|---|
| Pixar Luxo Jr × TE × Nothing(深灰金属、极简) | ✅ **像素 8-bit / 单色线稿 / 几何 flat** | ❌ 3D 渲染软萌 / anime 风 / 真人化 |

**好例子**:
- ✅ Bufo 蛤蟆(Anthropic buddy)— 像素风,极简,程序员梗
- ✅ Octocat — 单色线稿,几何感
- ✅ Go gopher — flat 设计,简单形
- ✅ npm wombat — 极简,品牌化

**坏例子**:
- ❌ Loona / Aibo 3D 萌宠 — 风格不符
- ❌ Anime girl 真人化 — 完全错位

#### 各状态角色化设计

| 状态 | 角色行为 | 信息显示 |
|---|---|---|
| **Busy** | 像素 Claude 敲键盘,头顶偶尔 💭 | 进度条 + tokens used + elapsed |
| **Attention** | Claude 抬头看你,头顶 ❓ 闪 | 命令文本 + 大按钮 |
| **Celebrate** | Claude 跳起 ✨,1.5 秒 | "Pushed to main." 类似 commit msg |
| **Idle** | Claude 坐着,偶尔眨眼/伸懒腰/看你(7 变体) | 时间 / 下一会议(可选) |
| **Sleep** | Claude 闭眼呼吸,灯熄 | — |

→ **硬件是装备,屏上的角色是"它穿什么衣服"**。
   装备永远是 TE 风工业感,衣服可换可萌可酷。

### 角色包系统(差异化 feature)

借鉴 Anthropic Hardware Buddy 的角色机制:

```
出厂默认:    极简光眼(TE / Nothing 风)
可选下载:    Pixel Programmer / Bufo / Octocat / Go Gopher /
             Astro Cyberpunk / 空白冥想模式 / ...
未来:       联名(Cursor / GitHub / TE 限定),用户 UGC
```

**价值**:
- 降低初次购买心理门槛(怕严肃?选 Bufo)
- 持续内容更新(产品有"活的"感觉)
- 联名机会(Cursor/GitHub/Anthropic)
- 用户身份表达(类似手机壳)
- 不破坏定位(硬件仍是极简办公装备)

### 核心原则

| 原则 | 含义 |
|---|---|
| **永远在线** | 任何模式下,身体都"活着"(微动 / 呼吸光) |
| **功能行为有意图感** | 显示通知 = 它"凑过来看你",不是"屏弹一条" |
| **状态切换有过渡** | 从 idle 切副屏不是闪一下,是"它想了一下,转过头去看时钟" |
| **不喧宾夺主** | 副屏功能服务你,不"求关注" |
| **保留留白** | 大部分时间它就是个安静呼吸的存在 |
| **克制 > 信息密度** | 显示一条真重要的,胜过一屏 widget |
| **专注 > 多功能** | 三个 demo 做到 90 分,胜过十个 60 分 |

---

## 8. 硬件选型

### 主控板:**Waveshare ESP32-S3-Touch-LCD-4B**

| 维度 | 规格 |
|---|---|
| 主控 | ESP32-S3R8(LX7 双核 240MHz,原生 USB CDC + BLE 5) |
| Flash + PSRAM | 16MB + 8MB |
| 屏幕 | 4.0" 480×480 RGB IPS 触摸(GT911) |
| 麦克风 | 贴片麦 + ES7210 回声消除 + ES8311 codec |
| 喇叭 | 8Ω 2W(MX1.25 接口,**喇叭单买 ~10 元**) |
| IMU | QMI8658(6 轴) |
| RTC | PCF85063 |
| PMIC | AXP2101 |
| USB | 2× Type-C(UART 烧录 + 原生 USB CDC) |
| WiFi + BLE | 2.4G + BLE 5 |
| 电池 | PH2.0 接口(**LiPo 单买 ~20-30 元**) |

### 选型理由(为什么不是别的)

| 候选 | 否定理由 |
|---|---|
| M5 CoreS3 系列 | 缺货 |
| M5 Core2 v1.1 | 老 ESP32(LX6) + 屏小 2" + 单麦无回声消除 + 内置喇叭嵌灯里反成包袱 |
| 冠显 TY040(智能串口屏)| 主控不在自己手里,只能播预存内容,无法配合 LLM 动态性 |
| **Waveshare 4-LCD-4B** ✅ | 全部硬需求满足 + 屏大 + 原生 USB + BLE 5 + 回声消除 + 价格更便宜 |

### 配套清单

**必买**:
- [ ] Waveshare 主板(~¥220-280)
- [ ] 8Ω 2W 喇叭 + MX1.25 公头线(~¥10)
- [ ] LiPo 1000mAh + PH2.0 接头(~¥25-30,作为电源缓冲 + 优雅断电)

**可选**:
- [ ] 长款 USB-C 数据线
- [ ] 板子保护壳(嵌入灯里前可有可无)

### 三段式架构路径

```
阶段 1(现在 - 1 个月):Mac + Waveshare(USB-C 单线)
  Mac:Python 全栈 / Claude Desktop App BLE bridge
  设备:USB CDC 转发 IO,LVGL 渲染,IMU 事件回传

阶段 2(1-3 个月):Mac + Waveshare(改 WiFi 或 BLE)
  Coding Buddy 直接 BLE 接 Claude Desktop App
  脱离 USB 线,无线工作

阶段 3(3-6 个月):Pi Zero 2W 嵌入灯里 + Waveshare 当面板
  Python 栈整体搬到 Pi(如果走自己 brain 路线)
  或继续直 BLE(如果只做 Coding Buddy)

阶段 4(看市场反馈):自研板量产
```

### 协议中立原则

USB CDC JSON → BLE NUS JSON → WebSocket JSON 三种 transport,**承载的是同一套帧格式**。

> 建立的不是某块板的代码,是一套 IO 抽象。

---

## 9. 商业模型

### 价格锚点

**$329 / ¥1999 起**(预售价 $299 / ¥1799)

参考定位:

| 产品 | 价格 | 类比 |
|---|---|---|
| HHKB Studio | ¥3000+ | 程序员愿意花 |
| Stream Deck XL | ¥1500 | 程序员愿意花 |
| Tidbyt | $199 | 国外极客 niche |
| Eilik | ¥1199 | **错位对标**(它是萌宠,你不是) |
| **AI Working Companion** | **¥1999** | **本品** |

定位逻辑:
- 高于 Eilik → 建立"不是桌宠"的认知
- 低于 HHKB → 在程序员"年度装备升级"预算内

### 销售/传播路径

**海外为主战场**:
- Twitter / X(developer Twitter)
- Hacker News(Show HN)
- Indie Hackers
- Product Hunt
- YC alumni

**国内辅助**:
- 即刻(AI 圈 + 极客圈)
- 小红书(极客美学)
- V2EX

**不走大渠道**(避免比价 + 山寨)。

### 期望规模

| 时段 | 销量 | 收入 |
|---|---|---|
| 第一年 | 500-2000 台 | ¥75-300 万 |
| 第二年(若验证成功)| 5000-20000 台 | ¥750-3000 万 |

不追大爆,小而精。

---

## 10. 4 周验证计划

### 核心理念:**不要先做产品,先验证需求**

### Week 1-2:Demo 视频(不需要真硬件)

- 60 秒视频,讲三个 Demo(Watchman / Foreman / Don't Look At Me)
- **可用 Keynote / After Effects / Blender 渲染假产品**
- 配文一句话:"AI Working Companion. For your AI team."
- 找设计师朋友帮忙或外包(¥3000-5000)

### Week 3:落地页 + 邮箱收集

- 一个简洁页面(Framer / Webflow,1 天)
- 视频 + 三段文字 + "Notify me when available" 输入框
- 域名候选:`workingcompanion.ai` / `lume.codes` / `pair.dev`

### Week 4:发布与数据采集

- Twitter / X(找几个 1k+ 粉朋友帮转)
- Hacker News Show HN
- Product Hunt(upcoming)
- 即刻 / V2EX
- **不付费推广**,看自然传播

### 决策节点

| 4 周后指标 | 行动 |
|---|---|
| Twitter < 500 转 / 邮箱 < 100 | **果断停**,定位有问题 |
| 500-5000 转 / 100-500 邮箱 | **小批量做**,接 50-100 单深度反馈 |
| > 5000 转 / > 500 邮箱 | **认真量产**,开预售 |

---

## 11. 未来路径

```
v1.0:  Claude Code 集成(MVP)
v1.5:  + Cursor / Copilot / Windsurf 集成
v2.0:  + GitHub PR / Linear / CI 集成
v2.5:  + Pomodoro / Calendar / Focus mode
v3.0:  开放 SDK,允许第三方做 integration(社区化)
v4.0:  Pro 版 / 商务版(团队场景,公司采购)
```

**注意**:每一步都在程序员/AI 用户这个圈层内深耕,**不外扩到大众**。

---

## 12. 决策禁忌(给未来的自己)

任何后续讨论触发以下情况,**立刻警惕**:

| 危险信号 | 该怎么做 |
|---|---|
| "再加一个功能就更完整了" | 先问:这是程序员/AI 用户痛点吗?不是 → 砍 |
| "整个产品做成萌系定位会不会更多人喜欢" | NO。整体定位走萌系会失去目标人群。但 idle 屏保 / 角色包可以萌(见第 7 节分层规则) |
| "卖给设计师/学生/普通用户也行?" | NO。稀释定位 = 谁都不买 |
| "便宜一点能多卖" | NO。¥1999 锚住"装备"心智,降价变"玩具" |
| "国内市场可能更大?" | 海外 first。国内是验证后的次战场 |
| "做个 app 版/纯软件版?" | NO。物理具身是 unique value,丢了就没有产品 |
| "要不要先做硬件?" | NO。先做视频,先验证需求,先收预售 |

---

## 附录 A:产品 tagline 候选

主推:
- **"AI Working Companion."** ⭐(产品概念名,也是 tagline)
- **"For your AI team."**

支撑文案:
- "Stop alt-tabbing to check on your agents."
- "Async AI, ambient awareness."
- "The peripheral for your peripheral vision."
- "Run agents. Don't babysit them."
- "You focus. It filters."

---

## 附录 B:关键参考产品

学习目标:

| 产品 | 学什么 |
|---|---|
| **Playdate** | 小众文化产品的运营 + 限量饥饿 + 强美学 |
| **Daylight Computer** | 高客单价小众产品的叙事 + 立场鲜明 |
| **Teenage Engineering** | 工业设计语言 + 圈层身份感 |
| **Anthropic Hardware Buddy** | BLE 协议 + 状态机 + 极简交互 |
| **Tidbyt** | 桌面恒久在线产品的形态启发(注意:它的功能型定位是反面教材) |

避免学:

| 产品 | 别做成这样 |
|---|---|
| Eilik | 萌系桌宠,情感寄托型 |
| Cozmo / Vector | 玩具感强,游戏化 |
| 各类智能屏 | 信息密度高,工具感强但无人格 |
| 各类物联网网关 | 完全工具化,无叙事 |

---

## 附录 C:工程文档关联

- 硬件选型详细对比与三段架构 → 参见项目记忆 `project_lamp_io_hardware.md`
- 产品定位与目标人群 → 参见项目记忆 `project_lamp_product_positioning.md`
- 现有 lelamp_runtime 软件栈(motion / soul / memory)→ 详见 `dev-memory-system.md` 等

---

## 附录 D:下一步行动清单

### 立刻可做(板子未到)

- [ ] **写 60 秒 demo 视频脚本**(精确到每秒,画面 + 字幕 + 配乐建议)
- [ ] **找视觉设计资源**(独立设计师 / 工作室 / AI 工具)
- [ ] **设计 5 张 480×480 表情草图**(idle / busy / attention / celebrate / sleep)
- [ ] **准备落地页文案**
- [ ] **注册域名**

### 板子到货后

- [ ] Fork Anthropic Hardware Buddy 参考工程
- [ ] 改 PlatformIO 配 Waveshare 板,跑通编译
- [ ] BLE 跟 Claude Desktop App 配对
- [ ] LVGL 跑通基础例程
- [ ] 实现 5 种状态切换(无表情先跑骨架)

### Demo 视频上线后

- [ ] 收集邮箱 / 转发数据
- [ ] 决策节点:停 / 小批量 / 量产

---

## 文档维护规则

1. 任何与本文档冲突的新决定,**先更新本文档,再执行**
2. 重大转向(改人群、改定位、改形态)需要新增"演进"章节,不要直接改原文
3. 添加新功能前,先在第 6 节"不做"列表里检查是否已经被砍
4. 添加新场景前,先在第 5 节核心 Demo 里检查是否冲淡叙事

### 表达红线的注意事项

**风险** 和 **限制** 不一样:
- **风险**:产品被错误归类、目标人群流失、定位糊掉(应该写在文档里)
- **限制**:某种执行细节不能用(往往过度,应交给设计判断)

写文档时易犯的错:**把对"风险"的恐惧表达成对"限制"的硬性禁令**。

例:为了防止"产品被归类为桌宠"(风险),写成"任何状态都不能有可爱角色"(限制) → 这会误杀像素 Claude 敲键盘这种**完全不属于桌宠的好设计**。

**正确做法**:
- 只在文档里写**风险**和**对应的边界场景**(如 marketing 第一印象、产品名、包装)
- 具体 UI/动画/角色细节交给**执行原则**(第 7 节那 6 条)+ 设计判断
- 看到"❌ 不要 X"的硬规则时,问一句:"这是真红线,还是我在过度防御?"
