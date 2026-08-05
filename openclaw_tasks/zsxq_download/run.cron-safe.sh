#!/usr/bin/env bash
# Version-controlled scheduler wrapper for a ZSXQ download runtime task.

set -euo pipefail

export PATH="$HOME/.npm-global/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# `$0` preserves the task-local symlink passed by launchd. BASH_SOURCE can
# resolve to the release checkout after one wrapper invokes another.
TASK_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$TASK_DIR"

if [[ ! -f "$TASK_DIR/config.env" ]]; then
  printf 'missing task configuration: %s\n' "$TASK_DIR/config.env" >&2
  exit 2
fi
# shellcheck disable=SC1091
source "$TASK_DIR/config.env"

LOG_FILE="${LOG_FILE:-cron.log}"
LOG_MAX_BYTES="${LOG_MAX_BYTES:-20971520}"
LOG_BACKUP_COUNT="${LOG_BACKUP_COUNT:-3}"
RUN_ENTRY_PATH="${TASK_RUN_ENTRY_PATH:-$TASK_DIR/run.sh}"
PID_FILE="$TASK_DIR/.run.pid"

rotate_log_if_needed() {
  if [[ ! "$LOG_MAX_BYTES" =~ ^[0-9]+$ || "$LOG_MAX_BYTES" -lt 1 ]]; then
    LOG_MAX_BYTES=20971520
  fi
  if [[ ! "$LOG_BACKUP_COUNT" =~ ^[0-9]+$ || "$LOG_BACKUP_COUNT" -lt 1 || "$LOG_BACKUP_COUNT" -gt 20 ]]; then
    LOG_BACKUP_COUNT=3
  fi
  [[ -f "$LOG_FILE" ]] || return 0
  local log_size index previous
  log_size="$(wc -c < "$LOG_FILE" | tr -d '[:space:]')"
  if [[ ! "$log_size" =~ ^[0-9]+$ || "$log_size" -le "$LOG_MAX_BYTES" ]]; then
    return 0
  fi
  for ((index = LOG_BACKUP_COUNT; index >= 2; index--)); do
    previous=$((index - 1))
    [[ -f "${LOG_FILE}.${previous}" ]] && mv -f "${LOG_FILE}.${previous}" "${LOG_FILE}.${index}"
  done
  mv -f "$LOG_FILE" "${LOG_FILE}.1"
}

if [[ -f "$PID_FILE" ]]; then
  existing_pid="$(cat "$PID_FILE" 2>/dev/null || true)"
  existing_command="$(ps -p "$existing_pid" -o command= 2>/dev/null || true)"
  if [[ -n "$existing_command" && "$existing_command" == *"$TASK_DIR/run.cron-safe.sh"* ]]; then
    printf '[%s] 已有运行中的任务，跳过重复触发\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"
    exit 0
  fi
  rm -f "$PID_FILE"
fi

printf '%s\n' "$$" > "$PID_FILE"
trap 'rm -f "$PID_FILE"' EXIT

rotate_log_if_needed
printf '========== %s cron run start ==========\n' "$(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG_FILE"
set +e
ZSXQ_RUNTIME_TASK_DIR="$TASK_DIR" bash "$RUN_ENTRY_PATH" >> "$LOG_FILE" 2>&1
run_rc=$?
set -e
printf '========== %s cron run end rc=%s ==========\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$run_rc" >> "$LOG_FILE"
exit "$run_rc"
