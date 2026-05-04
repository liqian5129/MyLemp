#!/usr/bin/env bash
# 优雅停止所有 lelamp 进程: daemon + main_persona(若在跑)。
#
# 用法:
#   ./scripts/stop_all.sh

set -e

DAEMON_URL="http://127.0.0.1:9000"

# ── 1. 优雅关 daemon (HTTP /shutdown) ─────────────────────────
if curl -sf "$DAEMON_URL/state" >/dev/null 2>&1; then
  echo "▶ 优雅关闭 daemon..."
  curl -sf -X POST "$DAEMON_URL/shutdown" -d '{}' >/dev/null 2>&1 || true
  sleep 2
  if curl -sf "$DAEMON_URL/state" >/dev/null 2>&1; then
    echo "  优雅关闭超时,强杀..."
    pkill -f "lelamp.companion" 2>/dev/null || true
    sleep 1
  fi
  echo "✓ daemon 已停止"
else
  echo "(daemon 未运行)"
fi

# ── 2. main_persona 若在跑,提示用户(不强杀,避免丢正在朗读的话)─
if pgrep -f "main_persona.py" >/dev/null; then
  echo ""
  echo "▶ main_persona 仍在跑,请到它的终端按 Ctrl-C 优雅退出"
  echo "  (会触发 goodnight arc + TTS / 摄像头 / ASR 收尾)"
  echo ""
  echo "  若需强杀: pkill -f main_persona.py"
fi
