#!/usr/bin/env bash
# Compatibility runner for the two existing ZSXQ LaunchAgents.
#
# It owns only task-local logs/results and notification rendering.  The
# versioned Python pipeline owns scan plans, browser actions, archive
# reconciliation, and SQLite state; this wrapper never invokes an agent.

set -euo pipefail

TASK_DIR="$(cd "${ZSXQ_RUNTIME_TASK_DIR:-$(dirname "$0")}" && pwd -P)"
cd "$TASK_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"
RUN_ID="$($PYTHON_BIN -c 'import uuid; print(uuid.uuid4())' 2>/dev/null || date '+%s-%N')"
RUN_STARTED_AT="$(date '+%Y-%m-%dT%H:%M:%S%z')"
STATUS_JSON_PATH="$TASK_DIR/run_status.json"
RESULT_JSON_PATH="$TASK_DIR/${RESULT_JSON:-last_result.json}"
RESULT_MD_PATH="$TASK_DIR/${RESULT_MD:-last_result.md}"
LOG_PATH="$TASK_DIR/${LOG_FILE:-cron.log}"
OVERRIDE_FILE="$TASK_DIR/${TIME_WINDOW_OVERRIDE_FILE:-time_window_override.json}"
WINDOW_START=""
WINDOW_END=""
WINDOW_NOTE=""
WINDOW_MODE="state"

write_failure_result() {
  local exit_code="$1" reason_code="$2"
  "$PYTHON_BIN" - "$CANONICAL_RESULT_PATH" "$RUN_ID" "$RUN_STARTED_AT" "$exit_code" "$reason_code" \
    "$WINDOW_START" "$WINDOW_END" <<'PY'
import json
import sys
from datetime import datetime
from pathlib import Path

path, run_id, started_at, exit_code, reason_code, window_start, window_end = sys.argv[1:]
finished_at = datetime.now().astimezone().isoformat()
payload = {
    "schema_version": 1,
    "run_id": run_id,
    "status": "failed",
    "process_exit_code": int(exit_code),
    "codex_exit_code": int(exit_code),
    "reason_code": reason_code,
    "core_reason_code": reason_code,
    "reason_text": "确定性下载链未完成；请检查同一 run 的结构化日志。",
    "core_reason_text": "确定性下载链未完成；请检查同一 run 的结构化日志。",
    "window_start": window_start or started_at,
    "window_end": window_end or finished_at,
    "downloaded_count": 0,
    "downloaded_files": [],
    "download_candidate_count": -1,
    "download_success_count": -1,
}
target = Path(path)
target.parent.mkdir(parents=True, exist_ok=True)
target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

if [[ ! -f "$TASK_DIR/config.env" ]]; then
  printf '[ERROR] missing task configuration: %s\n' "$TASK_DIR/config.env" >&2
  exit 2
fi
# shellcheck disable=SC1091
source "$TASK_DIR/config.env"
if [[ -f "$TASK_DIR/deployment.env" ]]; then
  # shellcheck disable=SC1091
  source "$TASK_DIR/deployment.env"
fi

AUTOMATION_ROOT="${AUTOMATION_ROOT:-}"
if [[ -n "${INVESTMENT_REPORTS_RUNTIME_DIR:-}" ]]; then
  RUNTIME_DIR="$INVESTMENT_REPORTS_RUNTIME_DIR"
elif [[ -n "$AUTOMATION_ROOT" ]]; then
  RUNTIME_DIR="$AUTOMATION_ROOT/.runtime"
else
  # A malformed private task config must not cause a fallback write below /.
  RUNTIME_DIR="$TASK_DIR/.runtime"
fi
if [[ -n "${DOWNLOAD_RUNNER_PATH:-}" ]]; then
  DOWNLOAD_RUNNER_PATH="$DOWNLOAD_RUNNER_PATH"
elif [[ -n "$AUTOMATION_ROOT" ]]; then
  DOWNLOAD_RUNNER_PATH="$AUTOMATION_ROOT/scripts/run_zsxq_download_pipeline.py"
else
  DOWNLOAD_RUNNER_PATH=""
fi
CANONICAL_RESULT_PATH="${PIPELINE_RESULT_PATH:-$RUNTIME_DIR/logs/zsxq_pipeline_last_run.json}"
NOTIFICATION_POLICY_PATH="${NOTIFICATION_POLICY_PATH:-$AUTOMATION_ROOT/scripts/zsxq_notification_policy.py}"
NOTIFICATION_STATE_PATH="$TASK_DIR/notification_state.json"
NOTIFICATION_OUTBOX_PATH="$TASK_DIR/notification_outbox.json"
NOTIFICATION_AUDIT_PATH="$TASK_DIR/notification_messages.jsonl"
CFT_CDP_ENDPOINT="${CFT_CDP_ENDPOINT:-http://127.0.0.1:${CFT_REMOTE_DEBUG_PORT:-9223}}"

copy_result_to_task() {
  if [[ "$CANONICAL_RESULT_PATH" != "$RESULT_JSON_PATH" ]]; then
    cp "$CANONICAL_RESULT_PATH" "$RESULT_JSON_PATH"
  fi
}

fail_initialization() {
  local reason_code="$1"
  write_failure_result 2 "$reason_code"
  copy_result_to_task
  exit 2
}

for required in AUTOMATION_ROOT DOWNLOAD_RUNNER_PATH ZSXQ_SOURCE_NAME ZSXQ_JOB_CONFIG_FILE ZSXQ_KEYWORDS_FILE ZSXQ_LEGACY_STATE_FILE CFT_EXECUTABLE_PATH CFT_USER_DATA_DIR; do
  if [[ -z "${!required:-}" ]]; then
    printf '[ERROR] required deterministic download setting is missing: %s\n' "$required" >&2
    required_reason="$(printf '%s' "$required" | tr '[:upper:]' '[:lower:]')"
    fail_initialization "missing_$required_reason"
  fi
done
if [[ ! -f "$DOWNLOAD_RUNNER_PATH" ]]; then
  printf '[ERROR] deterministic download runner is missing: %s\n' "$DOWNLOAD_RUNNER_PATH" >&2
  fail_initialization "missing_download_runner"
fi

CFT_START_URL="${CFT_START_URL:-}"
if [[ -z "$CFT_START_URL" ]]; then
  CFT_START_URL="$($PYTHON_BIN - "$ZSXQ_JOB_CONFIG_FILE" <<'PY'
import json
import sys
from pathlib import Path

try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    payload = {}
print(str(payload.get("tag_url") or payload.get("group_url") or "").strip())
PY
)"
fi

if [[ -f "$OVERRIDE_FILE" ]]; then
  override_values="$("$PYTHON_BIN" - "$OVERRIDE_FILE" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if bool(payload.get("enabled")) and str(payload.get("window_start") or "").strip() and str(payload.get("window_end") or "").strip():
    print("explicit")
    print(str(payload["window_start"]).strip())
    print(str(payload["window_end"]).strip())
    print(str(payload.get("note") or "").strip())
else:
    print("state")
    print("")
    print("")
    print("")
PY
)"
  WINDOW_MODE="$(printf '%s\n' "$override_values" | sed -n '1p')"
  WINDOW_START="$(printf '%s\n' "$override_values" | sed -n '2p')"
  WINDOW_END="$(printf '%s\n' "$override_values" | sed -n '3p')"
  WINDOW_NOTE="$(printf '%s\n' "$override_values" | sed -n '4p')"
fi

runner_args=(
  --runtime-root "$RUNTIME_DIR"
  --source "$ZSXQ_SOURCE_NAME"
  --job-config "$ZSXQ_JOB_CONFIG_FILE"
  --keyword-file "$ZSXQ_KEYWORDS_FILE"
  --legacy-state "$ZSXQ_LEGACY_STATE_FILE"
  --cdp-endpoint "$CFT_CDP_ENDPOINT"
  --cft-executable "$CFT_EXECUTABLE_PATH"
  --cft-user-data-dir "$CFT_USER_DATA_DIR"
  --cft-start-url "$CFT_START_URL"
  --cft-headless "${ZSXQ_CFT_HEADLESS:-true}"
  --cft-window-size "${ZSXQ_CFT_WINDOW_SIZE:-1440,1200}"
  --result-path "$CANONICAL_RESULT_PATH"
  --run-id "$RUN_ID"
  --timeout-seconds "${ZSXQ_PLAYWRIGHT_ACTION_TIMEOUT_SECONDS:-30}"
  --navigation-attempts "${ZSXQ_NAVIGATION_ATTEMPTS:-3}"
)
if [[ "$WINDOW_MODE" == "explicit" ]]; then
  runner_args+=(--window-start "$WINDOW_START" --window-end "$WINDOW_END")
fi

printf '[%s] deterministic download start source=%s mode=%s run_id=%s\n' \
  "$(date '+%Y-%m-%d %H:%M:%S')" "$ZSXQ_SOURCE_NAME" "$WINDOW_MODE" "$RUN_ID" >> "$LOG_PATH"
set +e
PYTHONPATH="$AUTOMATION_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" \
  "$PYTHON_BIN" "$DOWNLOAD_RUNNER_PATH" "${runner_args[@]}" > "$RESULT_MD_PATH" 2>&1
run_rc=$?
set -e

if [[ ! -f "$CANONICAL_RESULT_PATH" ]]; then
  write_failure_result "$run_rc" "pipeline_runner_failed"
fi
copy_result_to_task

"$PYTHON_BIN" - "$STATUS_JSON_PATH" "$CANONICAL_RESULT_PATH" "$RUN_ID" "$RUN_STARTED_AT" "$run_rc" <<'PY'
import json
import sys
from datetime import datetime
from pathlib import Path

status_path, result_path, run_id, started_at, rc = sys.argv[1:]
payload = json.loads(Path(result_path).read_text(encoding="utf-8"))
status = {
    "run_id": run_id,
    "started_at": started_at,
    "finished_at": datetime.now().astimezone().isoformat(),
    "status": payload.get("status", "failed"),
    "message": payload.get("reason_code", "unknown"),
    "process_exit_code": int(rc),
    "scan_plan_path": payload.get("scan_plan_path"),
    "scan_plan_hash": payload.get("scan_plan_hash"),
}
Path(status_path).write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

if [[ -f "$NOTIFICATION_POLICY_PATH" ]]; then
  "$PYTHON_BIN" "$NOTIFICATION_POLICY_PATH" \
    --result "$RESULT_JSON_PATH" --pipeline "${NOTIFICATION_PIPELINE:-foreign_download}" \
    --state "$NOTIFICATION_STATE_PATH" --outbox "$NOTIFICATION_OUTBOX_PATH" \
    --audit "$NOTIFICATION_AUDIT_PATH" --chat-id "${TARGET_CHAT_ID:-}" \
    --lark-cli "${LARK_CLI_BIN:-lark-cli}" >> "$LOG_PATH" 2>&1 || true
fi

printf '[%s] deterministic download end rc=%s result=%s\n' \
  "$(date '+%Y-%m-%d %H:%M:%S')" "$run_rc" "$RESULT_JSON_PATH" >> "$LOG_PATH"
exit "$run_rc"
