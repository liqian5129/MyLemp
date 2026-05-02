#!/usr/bin/env bash
# Claude Code PreToolUse hook → daemon tool_started
# 关键作用:此时工具已通过 permission(若需要)、即将执行 → 清掉 daemon 的 prompt
# 让 attention 状态回到 busy,屏视觉跟着切回工作状态
#
# v0.4.0:把 tool_input 也透传给 daemon,daemon 端用于 activity log 摘要
# (file_path / command / pattern 等字段)
set -e
T_START=$(date +%s.%N)
echo "$T_START pre_tool_use START" >> /tmp/lelamp_hook_trace.log
source "$(dirname "$0")/_post_event.sh"
INPUT=$(read_hook_input)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""')
TOOL=$(echo "$INPUT" | jq -r '.tool_name // ""')
# tool_input 整个对象透传(daemon 端 _summarize_tool_input 按 tool 类型挑字段)
DATA=$(echo "$INPUT" | jq -c '{tool: .tool_name, tool_input: (.tool_input // {})}')
T_BEFORE_POST=$(date +%s.%N)
post_event "tool_started" "$DATA" "$SESSION_ID"
T_END=$(date +%s.%N)
echo "$T_END pre_tool_use END  tool=$TOOL  parse=$(echo "$T_BEFORE_POST - $T_START" | bc)s  post=$(echo "$T_END - $T_BEFORE_POST" | bc)s" >> /tmp/lelamp_hook_trace.log
exit 0
