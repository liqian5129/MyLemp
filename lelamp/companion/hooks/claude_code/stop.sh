#!/usr/bin/env bash
# Claude Code Stop hook → daemon task_completed
# 触发设备短暂 celebrate(1.5s)后回 idle。
set -e
source "$(dirname "$0")/_post_event.sh"
INPUT=$(read_hook_input)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""')
post_event "task_completed" "{}" "$SESSION_ID"
exit 0
