# DashScope (Qwen) Token 用量分析

> 数据抓取时间：2026-04-19 约 14:20
> 统计窗口：2026-04-19 09:00 – 14:20（约 6 小时，实际有调用的区间约 2.5 小时）
> 模型：qwen3（大语言模型 Tab）
> 触发背景：账户欠费（Arrearage）后回看用量面板

## 用量快照

| 指标 | 数值 |
|---|---|
| 调用模型数 | 1 个 |
| 调用成功总次数 | 591 次 |
| Token 总数 | 5,760 K tokens |
| 平均单次请求 Token | 9,747 tokens |
| 调用频率峰值 | ~12 次/分钟 |
| 调用频率常态 | 3-6 次/分钟 |

注：控制台显示 "9,747" = **9.7k tokens**（不是 97k）。逗号是千位分隔符。

## 每次请求的 Token 构成

固定前缀（每次调用都重传）：

| 部分 | 字符数 | 说明 |
|---|---|---|
| `SOUL_TOOLS` JSON Schema | ~12.6k | 16 个工具定义 + 长描述 |
| `PERSONALITY_PROMPT` | ~7.9k | body / vision / examples 等 11 段 |
| `MOTION_EXAMPLES` | ~2.5k | compose_motion few-shot |
| **固定合计** | **~23k 字符** | ≈ 9-10k tokens（Qwen 中文+JSON ≈ 0.4 tok/char） |

动态部分（随对话变化）：

| 部分 | 字符数 | 来源 |
|---|---|---|
| `[RECENT]` 记忆流 | ≤ 7.5k | top-15 条 × 最多 500 字 |
| `[TODAY]` 今日叙事 | ~500-1000 | 后台 review 滚动摘要 |
| `[FACTS]` 长期事实 | ~200-500 | 身份/称谓/偏好 |
| `[STATE]/[SCENE]/[TRIGGER]` | ~500 | 状态、场景、触发描述 |

平均 9.7k/次 说明大多数调用主要由固定前缀主导，RECENT/TODAY 未满。

## 调用来源分布

| 来源 | 代码位置 | 频率 |
|---|---|---|
| ReAct `_think` 循环 | `soul_agent.py:1613` | 每次心跳/语音/环境触发，`max_steps=15` 每步一次 |
| Boot scan 多图场景总结 | `soul_agent.py:1286` | 冷启动 5 张图一次 |
| 后台 Review | `soul_agent.py:1022` | 周期性长期记忆归并 |
| OmniEar 音频分析 | `omni_ear.py` → `qwen3-omni-flash` | 每次 VAD 段结束一次（计入"全模态"Tab） |

心跳节奏：`soul_agent.py:1331` 动态间隔 30/60/120 秒（按连续看不到人的次数降频）。但一次心跳触发会进入 ReAct 多步循环，每步都会整包重发。

## 总量验证

```
固定前缀 ~10k tokens × 591 次 ≈ 5.91M tokens
实际总量 5.76M tokens  ✔ 吻合
```

## 优化方向（按性价比排序）

### P0 — 最快见效

1. **分触发场景使用不同工具集**
   心跳/环境触发只暴露 `look / body_move / speak / wait / set_light_mood / update_scene_memory` 6 个必要工具，砍掉 `register_voice / forget_fact / set_reminder / cancel_reminder / list_reminders / recall_memory / session_search` 7 个低频工具。预计省 **40% tools schema ≈ 4-5k 字符/次**。

2. **确认 Qwen prompt cache 是否命中**
   固定前缀 ~23k 字符是完美的缓存目标。需要抓 response 的 `usage.prompt_tokens_details.cached_tokens` 字段确认命中率。命中后这部分价格降至 10%。

### P1 — 中等收益

3. **裁剪 RECENT**
   `episodic.py:37` `RETRIEVE_TOP_N=15` → 10；`episodic.py:147` 单条 500 字上限 → 200。

4. **PERSONALITY_PROMPT 瘦身**
   `<scene_memory>` / `<longterm_memory>` / `<reminders>` 各占 400-600 字，压到 1/3 可以省 ~1k 字符。

5. **MOTION_EXAMPLES 按需注入**
   仅在触发里出现动作/表演类关键词时拼入，平时不发。

### P2 — 架构级

6. **心跳前置过滤**
   心跳触发进 `_think` 之前先判定：无人在场 + 场景无变化 + 最近刚说过话 → 直接跳过，不调 LLM。当前只做了动态降频，但仍然在调。

7. **ReAct max_steps 缩短**
   `soul_agent.py:1510` `max_steps=15` → 6-8。配合工具执行规划（speak 前必须 look 等），很少需要超过 6 步。

## 下次复测指标

实施 P0 后预期：
- 平均单次 Token：9.7k → **5-6k**
- 总量：5.76M / 2.5h → **3-3.5M / 2.5h**
- 成本：下降 ~40%，叠加 prompt cache 命中后可再降 50%+
