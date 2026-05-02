#!/usr/bin/env bash
# Claude Code Notification hook → daemon awaiting_approval
#
# Claude Code Notification 触发场景很多(权限请求 / 等待输入 / UI 通知...),
# 我们**只**关心权限类:filter message 含 "permission" / "权限" 才触发 attention,
# 其他通知静默忽略,避免误报。
#
# 注意:此 hook 是事后通知,**不阻塞** Claude Code 的工具执行(用户仍要在终端按 y/n)。
# 设备 tap 不影响 Claude。真审批留给将来 PreToolUse + permission rules 重做。
set -e
source "$(dirname "$0")/_post_event.sh"
INPUT=$(read_hook_input)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""')
MSG=$(echo "$INPUT" | jq -r '.message // ""')

# 记录所有 notification 的实际 message,供调试用
echo "$(date +%s.%N) notification msg='$MSG'" >> /tmp/lelamp_hook_trace.log

# 只 filter 权限/确认类通知;纯 idle 提示("Claude is waiting...")才忽略
shopt -s nocasematch
if [[ "$MSG" == *waiting* && "$MSG" != *permission* && "$MSG" != *proceed* && "$MSG" != *confirm* && "$MSG" != *权限* ]]; then
  echo "$(date +%s.%N) notification SKIP(waiting only)" >> /tmp/lelamp_hook_trace.log
  exit 0
fi

DATA=$(jq -n \
  --arg id "notif_$(date +%s%N)" \
  --arg tool "Claude" \
  --arg cmd "$MSG" \
  '{id: $id, tool: $tool, command: $cmd}')

post_event "awaiting_approval" "$DATA" "$SESSION_ID"
exit 0
