#!/usr/bin/env bash
# Claude Code SessionStart hook → daemon agent_session_start
set -e
source "$(dirname "$0")/_post_event.sh"
INPUT=$(read_hook_input)
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // ""')
post_event "agent_session_start" "{}" "$SESSION_ID"
exit 0
