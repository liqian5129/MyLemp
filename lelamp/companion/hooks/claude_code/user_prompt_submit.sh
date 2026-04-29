#!/usr/bin/env bash
# Claude Code UserPromptSubmit hook → daemon task_started
# 把 prompt 前 40 字符当 session topic 候选;daemon 端会做"粘性"处理
# (只在 session 开始后第一个 prompt 时落地为 subtitle,后续 prompts 不覆盖)
set -e
source "$(dirname "$0")/_post_event.sh"
INPUT=$(read_hook_input)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""')
PROMPT=$(echo "$INPUT" | jq -r '.prompt // ""' | head -c 40)
DATA=$(jq -n --arg s "$PROMPT" '{summary: $s}')
post_event "task_started" "$DATA" "$SESSION_ID"
exit 0
