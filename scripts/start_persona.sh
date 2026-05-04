#!/usr/bin/env bash
# 启动完整体验: buddy daemon (后台) + main_persona (前台)
#
# 用法:
#   ./scripts/start_persona.sh
#
# Ctrl-C 退出 main_persona 后,daemon 仍后台跑(让 cc hooks 持续工作)。
# 完整停止用 ./scripts/stop_all.sh

set -e

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

DAEMON_URL="http://127.0.0.1:9000"
DAEMON_LOG="/tmp/lelamp_daemon.log"

# ── 1. daemon 是否已在跑 ──────────────────────────────────────
if curl -sf "$DAEMON_URL/state" >/dev/null 2>&1; then
  echo "✓ daemon 已在运行 ($DAEMON_URL)"
else
  echo "▶ 启动 daemon (后台,日志: $DAEMON_LOG)..."
  nohup uv run python -m lelamp.companion --no-arm > "$DAEMON_LOG" 2>&1 &
  disown

  # 等 HTTP ready (最多 10 秒)
  for i in $(seq 1 20); do
    if curl -sf "$DAEMON_URL/state" >/dev/null 2>&1; then
      echo "✓ daemon 启动成功"
      break
    fi
    sleep 0.5
  done

  # 确认起来了
  if ! curl -sf "$DAEMON_URL/state" >/dev/null 2>&1; then
    echo "✗ daemon 启动失败,看 $DAEMON_LOG 末尾:"
    tail -20 "$DAEMON_LOG"
    exit 1
  fi
fi

# ── 2. main_persona (前台) ─────────────────────────────────────
echo ""
echo "▶ 启动 main_persona (Ctrl-C 退出)"
echo "  daemon 保留后台运行,完整停止用 ./scripts/stop_all.sh"
echo ""
exec uv run python main_persona.py
