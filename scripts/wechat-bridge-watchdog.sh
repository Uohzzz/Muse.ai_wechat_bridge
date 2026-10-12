#!/usr/bin/env bash
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

# 微信桥看门狗（纯脚本版，2026-10-12）：健康时零唤醒零 token，
# 只有 serve 掉线被拉起 / 拉起失败 / 登录失效时才 wake 通知用户。
# BRIDGE 目录可用 WECHAT_BRIDGE_DIR 覆盖（沙箱测试用）。
BRIDGE="${WECHAT_BRIDGE_DIR:-$HOME/workspace/wechat-bridge}"
STATE="$BRIDGE/state"
NOW=$(TZ=Asia/Shanghai date '+%Y-%m-%d %H:%M:%S CST')

serve_alive() {
  local pid=""
  if [ -f "$STATE/serve.pid" ]; then
    pid=$(tr -d '[:space:]' < "$STATE/serve.pid" || true)
  fi
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo "$pid"
    return 0
  fi
  return 1
}

# 1) 登录失效优先
if [ -f "$STATE/NEEDS_RELOGIN" ]; then
  if [ ! -f "$STATE/notified_relogin" ]; then
    echo "$NOW NEEDS_RELOGIN detected" >> "$STATE/watchdog.log"
    touch "$STATE/notified_relogin"
    wake "微信桥登录已失效，需要重新扫码" "{\"kind\": \"relogin\"}"
  fi
  silent "NEEDS_RELOGIN 已通知过，等待用户重新扫码"
fi

# 2) serve 存活则静默
if PID=$(serve_alive); then
  silent "serve 存活（pid $PID）"
fi

# 3) serve 已挂：拉起并复检
( cd "$BRIDGE" && setsid nohup python3 bridge.py serve >> "$STATE/serve.out" 2>&1 & )
sleep 3
if PID=$(serve_alive); then
  echo "$NOW restarted serve pid=$PID" >> "$STATE/watchdog.log"
  wake "微信桥 serve 已自动拉起" "{\"kind\": \"restarted\", \"pid\": \"$PID\"}"
fi
echo "$NOW restart serve FAILED" >> "$STATE/watchdog.log"
wake "微信桥 serve 拉起失败" "{\"kind\": \"restart_failed\"}"
