#!/usr/bin/env bash
# Claude Code PostToolUse hook → daemon tool_completed
# 关键作用:用户在 attention 屏批了 yes 后,Claude Code 执行 tool,完成时此 hook fire。
# daemon 收到后清 prompt → 派生从 attention 回到 busy(若仍 running)。
#
# 没有这个 hook,attention 会一直死挂到 Stop 才被 task_completed 顺手清掉。
set -e
T_START=$(date +%s.%N)
echo "$T_START post_tool_use START" >> /tmp/lelamp_hook_trace.log
source "$(dirname "$0")/_post_event.sh"
INPUT=$(read_hook_input)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""')
TOOL=$(echo "$INPUT" | jq -r '.tool_name // ""')
DATA=$(jq -n --arg t "$TOOL" '{tool: $t}')
post_event "tool_completed" "$DATA" "$SESSION_ID"
echo "$(date +%s.%N) post_tool_use END  tool=$TOOL" >> /tmp/lelamp_hook_trace.log
exit 0
