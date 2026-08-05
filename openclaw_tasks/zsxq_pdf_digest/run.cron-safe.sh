#!/usr/bin/env bash
# This file only wraps run.sh and writes a simple cron log.
# The real overlap lock now lives in run.sh, so cron and manual runs share one rule.

set -euo pipefail

export PATH="$HOME/.npm-global/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
if [[ -z "${TMPDIR:-}" ]]; then
  export TMPDIR="$(getconf DARWIN_USER_TEMP_DIR 2>/dev/null || printf '/tmp')"
fi

RUNTIME_TASK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_REALPATH="$(python3 - "${BASH_SOURCE[0]}" <<'PY'
from pathlib import Path
import sys

print(Path(sys.argv[1]).expanduser().resolve())
PY
)"
SOURCE_TASK_DIR="$(cd "$(dirname "$SCRIPT_REALPATH")" && pwd)"
cd "$RUNTIME_TASK_DIR"

CONFIG_PATH="$RUNTIME_TASK_DIR/config.env"
if [[ ! -f "$CONFIG_PATH" ]]; then
  CONFIG_PATH="$SOURCE_TASK_DIR/config.env"
fi

# shellcheck disable=SC1091
source "$CONFIG_PATH"

RUN_ENTRY_PATH="$RUNTIME_TASK_DIR/run.sh"
if [[ ! -e "$RUN_ENTRY_PATH" ]]; then
  RUN_ENTRY_PATH="$SOURCE_TASK_DIR/run.sh"
fi
PAUSE_FILE="${SUMMARY_PAUSE_FILE:-$RUNTIME_TASK_DIR/.paused}"
LOG_MAX_BYTES="${LOG_MAX_BYTES:-20971520}"
LOG_BACKUP_COUNT="${LOG_BACKUP_COUNT:-3}"

rotate_log_if_needed() {
  if [[ ! "$LOG_MAX_BYTES" =~ ^[0-9]+$ ]] || [[ "$LOG_MAX_BYTES" -lt 1 ]]; then
    LOG_MAX_BYTES=20971520
  fi
  if [[ ! "$LOG_BACKUP_COUNT" =~ ^[0-9]+$ ]] || [[ "$LOG_BACKUP_COUNT" -lt 1 ]] || [[ "$LOG_BACKUP_COUNT" -gt 20 ]]; then
    LOG_BACKUP_COUNT=3
  fi
  if [[ ! -f "$LOG_FILE" ]]; then
    return 0
  fi

  local log_size index previous
  log_size="$(wc -c < "$LOG_FILE" | tr -d '[:space:]')"
  if [[ ! "$log_size" =~ ^[0-9]+$ ]] || [[ "$log_size" -le "$LOG_MAX_BYTES" ]]; then
    return 0
  fi

  for ((index = LOG_BACKUP_COUNT; index >= 2; index--)); do
    previous=$((index - 1))
    if [[ -f "${LOG_FILE}.${previous}" ]]; then
      mv -f "${LOG_FILE}.${previous}" "${LOG_FILE}.${index}"
    fi
  done
  mv -f "$LOG_FILE" "${LOG_FILE}.1"
}

summary_task_active_by_pid() {
  local pid_file="$RUNTIME_TASK_DIR/.run.pid"
  if [[ -f "$pid_file" ]]; then
    local pid
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && ps -p "$pid" >/dev/null 2>&1; then
      return 0
    fi
  fi
  return 1
}

summary_task_active() {
  python3 - "$RUNTIME_TASK_DIR/run_status.json" <<'PY'
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

status_path = Path(sys.argv[1])
if not status_path.exists():
    print("false")
    raise SystemExit(0)

try:
    data = json.loads(status_path.read_text(encoding="utf-8"))
except Exception:
    print("false")
    raise SystemExit(0)

status = str(data.get("status") or "").strip()
heartbeat_raw = str(data.get("last_heartbeat_at") or "").strip()
if status != "running" or not heartbeat_raw:
    print("false")
    raise SystemExit(0)

try:
    heartbeat = datetime.fromisoformat(heartbeat_raw)
except Exception:
    print("false")
    raise SystemExit(0)

active = datetime.now().astimezone() - heartbeat <= timedelta(minutes=3)
print("true" if active else "false")
PY
}

PID_ACTIVE="false"
STATUS_ACTIVE="false"
if summary_task_active_by_pid; then
  PID_ACTIVE="true"
elif [[ "$(summary_task_active)" == "true" ]]; then
  STATUS_ACTIVE="true"
else
  # Do not rename a log while a long-running worker still has it open. Rotate
  # only between runs so subprocess output cannot continue into an unlinked
  # backup inode.
  rotate_log_if_needed
fi
echo "========== $(date '+%Y-%m-%d %H:%M:%S') cron run start ==========" >> "$LOG_FILE"
if [[ -f "$PAUSE_FILE" ]]; then
  # A small file is enough to stop scheduled retries without touching the real task code.
  echo "========== $(date '+%Y-%m-%d %H:%M:%S') cron run skipped: summary task paused ==========" >> "$LOG_FILE"
  echo "========== $(date '+%Y-%m-%d %H:%M:%S') cron run end rc=0 ==========" >> "$LOG_FILE"
  exit 0
fi
if [[ "$PID_ACTIVE" == "true" ]]; then
  echo "========== $(date '+%Y-%m-%d %H:%M:%S') cron run found active pid; delegating to worker for busy notification ==========" >> "$LOG_FILE"
  set +e
  "$RUN_ENTRY_PATH" >> "$LOG_FILE" 2>&1
  RUN_RC=$?
  set -e
  echo "========== $(date '+%Y-%m-%d %H:%M:%S') cron run end rc=$RUN_RC ==========" >> "$LOG_FILE"
  exit "$RUN_RC"
fi
if [[ "$STATUS_ACTIVE" == "true" ]]; then
  echo "========== $(date '+%Y-%m-%d %H:%M:%S') cron run skipped: summary task already running ==========" >> "$LOG_FILE"
  echo "========== $(date '+%Y-%m-%d %H:%M:%S') cron run end rc=0 ==========" >> "$LOG_FILE"
  exit 0
fi
set +e
"$RUN_ENTRY_PATH" >> "$LOG_FILE" 2>&1
RUN_RC=$?
set -e
echo "========== $(date '+%Y-%m-%d %H:%M:%S') cron run end rc=$RUN_RC ==========" >> "$LOG_FILE"
exit "$RUN_RC"
