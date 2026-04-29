#!/usr/bin/env bash
# 共享工具:从 stdin 读 Claude Code hook JSON,POST 给 companion daemon。
# Claude Code 通过 stdin 传 JSON,字段如 session_id / hook_event_name / 各事件特有字段。
#
# 用法(其他 hook 脚本里 source 它):
#   source "$(dirname "$0")/_post_event.sh"
#   post_event "task_started" '{"summary":"..."}'
#
# 失败静默:不让 Claude Code 因为 daemon 挂了就崩。

DAEMON_URL="${LELAMP_DAEMON_URL:-http://127.0.0.1:9000}"

# 读 stdin JSON 一次,缓存到变量(后续多次提取字段时复用)
read_hook_input() {
  if [ -t 0 ]; then
    echo "{}"
  else
    cat
  fi
}

post_event() {
  local event_type="$1"
  local data="$2"
  [ -z "$data" ] && data="{}"
  local session_id="${3:-}"

  local payload
  payload=$(jq -n \
    --arg type "$event_type" \
    --arg agent "claude-code" \
    --arg session "$session_id" \
    --argjson data "$data" \
    '{type: $type, agent: $agent, session_id: $session, data: $data}')

  curl -s -X POST "${DAEMON_URL}/event" \
    -H "Content-Type: application/json" \
    --max-time 2 \
    -d "$payload" \
    >/dev/null 2>&1 || true
}
