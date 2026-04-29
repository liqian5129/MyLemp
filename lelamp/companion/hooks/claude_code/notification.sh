#!/usr/bin/env bash
# Claude Code Notification hook → daemon awaiting_approval(MVP:informational)
#
# 注意:此版本**不真正阻塞** Claude Code 的工具执行。
# Notification hook 是事后通知,只能让设备显示 "Claude 在等你输入"。
# 真正的设备 tap = 批准,需要 PreToolUse hook + 配置 permission rules,留给下一轮。
#
# 当前行为:
#   - 屏切到 attention,显示通知 message 作为 prompt
#   - 用户仍需要在终端按 y / n
#   - 设备 tap 不影响 Claude(但事件被记录)
set -e
source "$(dirname "$0")/_post_event.sh"
INPUT=$(read_hook_input)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""')
MSG=$(echo "$INPUT" | jq -r '.message // "Claude is waiting for your input"')

# 用 message 当 prompt 文本展示;tool 字段没拿到就显示 generic
DATA=$(jq -n \
  --arg id "notif_$(date +%s%N)" \
  --arg tool "Claude" \
  --arg cmd "$MSG" \
  '{id: $id, tool: $tool, command: $cmd}')

post_event "awaiting_approval" "$DATA" "$SESSION_ID"
exit 0
