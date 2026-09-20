#!/bin/bash
# 宿主看门狗：Chrome CDP 假活 / 轮询心跳过期时重启 arkham-chrome。
# 由 systemd timer 每 2 分钟跑一次。DRY_RUN=1 只打印动作。
set -u

BOT_HEALTH="${BOT_HEALTH:-http://127.0.0.1:8000/health}"
CDP_VERSION="${CDP_VERSION:-http://127.0.0.1:9222/json/version}"
HEARTBEAT="${HEARTBEAT:-/root/bighunter/data/arkham-heartbeat.json}"
STALE_SEC="${STALE_SEC:-600}"
RESTART_COOLDOWN="${RESTART_COOLDOWN:-300}"
BOT_GRACE_SEC="${BOT_GRACE_SEC:-180}"
CONTAINER="${CONTAINER:-whalescope-bot}"
CHROME_UNIT="${CHROME_UNIT:-arkham-chrome.service}"
STATE_DIR="${STATE_DIR:-/run/arkham-watchdog}"
STAMP="${STATE_DIR}/last-chrome-restart"
DRY_RUN="${DRY_RUN:-0}"

log() { logger -t arkham-watchdog "$*" 2>/dev/null || true; echo "$*"; }

now_ts() { date +%s; }

bot_started_ago() {
  local started
  started=$(docker inspect -f '{{.State.StartedAt}}' "$CONTAINER" 2>/dev/null) || return 1
  python3 -c "
from datetime import datetime, timezone
import sys
raw = sys.argv[1].replace('Z', '+00:00')
dt = datetime.fromisoformat(raw)
print(int(datetime.now(timezone.utc).timestamp() - dt.timestamp()))
" "$started" 2>/dev/null
}

heartbeat_ts() {
  if [[ -f "$HEARTBEAT" ]]; then
    python3 -c "
import json, sys
try:
    print(int(json.load(open(sys.argv[1])).get('ts') or 0))
except Exception:
    print(0)
" "$HEARTBEAT"
    return
  fi
  echo 0
}

recently_restarted() {
  [[ -f "$STAMP" ]] || return 1
  local age=$(( $(now_ts) - $(stat -c %Y "$STAMP") ))
  (( age < RESTART_COOLDOWN ))
}

restart_chrome() {
  local reason=$1
  if recently_restarted; then
    log "skip restart (cooldown ${RESTART_COOLDOWN}s): $reason"
    echo "action=skip-cooldown reason=$reason"
    return 0
  fi
  log "restart $CHROME_UNIT: $reason"
  echo "action=restart-chrome reason=$reason"
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi
  mkdir -p "$STATE_DIR"
  date +%s > "$STAMP"
  systemctl restart "$CHROME_UNIT"
}

http_ok() {
  curl -sf --max-time 3 "$1" >/dev/null
}

if ! http_ok "$CDP_VERSION"; then
  restart_chrome "cdp-http-down"
  exit 0
fi

if ! http_ok "$BOT_HEALTH"; then
  log "bot health down, leave chrome alone"
  echo "action=ok reason=bot-down"
  exit 0
fi

age=$(bot_started_ago || echo 99999)
if (( age < BOT_GRACE_SEC )); then
  log "bot started ${age}s ago, grace ${BOT_GRACE_SEC}s"
  echo "action=ok reason=bot-grace"
  exit 0
fi

hb=$(heartbeat_ts)
now=$(now_ts)
if (( hb <= 0 )); then
  restart_chrome "heartbeat-missing"
  exit 0
fi
stale=$(( now - hb ))
if (( stale > STALE_SEC )); then
  restart_chrome "heartbeat-stale age=${stale}s"
  exit 0
fi

echo "action=ok age=${stale}s"
exit 0
