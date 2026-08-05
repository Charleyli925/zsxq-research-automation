#!/usr/bin/env bash
# Version-controlled outer runner for both ZSXQ download tasks.
#
# This script is invoked from a task-specific runtime directory.  The runtime
# directory owns config.env, logs, results, and notification state; this file
# remains in the Git checkout and is normally linked into the task directory by
# deploy/install_local_runtime.sh.

set -euo pipefail

# The scheduler wrapper passes the task-local directory explicitly because a
# symlinked runner may otherwise resolve to this release checkout.
TASK_DIR="$(cd "${ZSXQ_RUNTIME_TASK_DIR:-$(dirname "$0")}" && pwd)"
cd "$TASK_DIR"

STARTUP_DEBUG_LOG="$TASK_DIR/startup_debug.log"
STATUS_JSON_PATH="$TASK_DIR/run_status.json"
RUN_STARTED_AT="$(date '+%Y-%m-%dT%H:%M:%S%z')"
PYTHON_BIN="${PYTHON_BIN:-python3}"
RUN_ID="$($PYTHON_BIN -c 'import uuid; print(uuid.uuid4())' 2>/dev/null || date '+%s-%N')"

# Keep the EXIT fallback usable even if config.env is missing or malformed.
AUTOMATION_ROOT="${AUTOMATION_ROOT:-}"
TARGET_CHAT_ID="${TARGET_CHAT_ID:-}"
CODEX_SCRIPT_PATH="${CODEX_SCRIPT_PATH:-}"
LOG_FILE="${LOG_FILE:-cron.log}"
RESULT_JSON="${RESULT_JSON:-last_result.json}"
RESULT_MD="${RESULT_MD:-last_result.md}"
TIME_WINDOW_OVERRIDE_FILE="${TIME_WINDOW_OVERRIDE_FILE:-time_window_override.json}"
RESULT_HELPER_PATH="${RESULT_HELPER_PATH:-}"
NOTIFICATION_POLICY_PATH="${NOTIFICATION_POLICY_PATH:-}"
CODEX_STRUCTURED_REPORT_PATH="${CODEX_STRUCTURED_REPORT_PATH:-$TASK_DIR/canonical_result.json}"
NOTIFICATION_PIPELINE="${NOTIFICATION_PIPELINE:-foreign_download}"
LARK_CLI_BIN="${LARK_CLI_BIN:-lark-cli}"

WINDOW_MODE="state"
WINDOW_START=""
WINDOW_END=""
WINDOW_APPLY_ONCE="true"
WINDOW_NOTE=""
END_TIME=""
FAILURE_STAGE="startup"
FINALIZATION_COMPLETE=0
NOTIFICATION_ATTEMPTED=0
NOTIFICATION_RESULT='{"decision":"skipped","reason":"not_attempted"}'

refresh_runtime_paths() {
  OVERRIDE_FILE="$TASK_DIR/$TIME_WINDOW_OVERRIDE_FILE"
  RESULT_MD_PATH="$TASK_DIR/$RESULT_MD"
  RESULT_JSON_PATH="$TASK_DIR/$RESULT_JSON"
  LOG_PATH="$TASK_DIR/$LOG_FILE"
  NOTIFICATION_STATE_PATH="$TASK_DIR/notification_state.json"
  NOTIFICATION_OUTBOX_PATH="$TASK_DIR/notification_outbox.json"
  NOTIFICATION_AUDIT_PATH="$TASK_DIR/notification_messages.jsonl"
}

refresh_runtime_paths

log_startup() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$STARTUP_DEBUG_LOG"
}

write_minimal_failed_result() {
  local failure_rc="$1" failure_stage="$2" finished_at
  finished_at="$(date '+%Y-%m-%dT%H:%M:%S%z')"

  if "$PYTHON_BIN" - "$RESULT_JSON_PATH" "$failure_rc" "$failure_stage" \
    "$RUN_STARTED_AT" "$finished_at" "$WINDOW_MODE" "$WINDOW_START" "$WINDOW_END" \
    "$WINDOW_NOTE" "$LOG_PATH" "$RESULT_MD_PATH" "$CODEX_STRUCTURED_REPORT_PATH" "$RUN_ID" <<'PY'
import json
import sys
from pathlib import Path

(output_path, failure_rc, failure_stage, started_at, finished_at, window_mode,
 explicit_start, explicit_end, window_note, log_path, result_md_path,
 canonical_path, run_id) = sys.argv[1:]
path = Path(output_path)
path.parent.mkdir(parents=True, exist_ok=True)
payload = {
    "run_id": run_id,
    "execute_time": finished_at,
    "status": "failed",
    "exit_code": int(failure_rc),
    "window_mode": window_mode,
    "window_start": explicit_start or started_at,
    "window_end": explicit_end or finished_at,
    "requested_window_start": explicit_start,
    "requested_window_end": explicit_end,
    "explicit_window_start": explicit_start,
    "explicit_window_end": explicit_end,
    "window_note": window_note,
    "downloaded_count": 0,
    "downloaded_files": [],
    "archive_dir": None,
    "archive_dirs": [],
    "no_download_reason": "unknown",
    "core_reason_code": "task_failed",
    "core_reason_text": f"运行脚本在 {failure_stage} 阶段异常退出（exit {failure_rc}）",
    "window_new_docs_count": -1,
    "keyword_matched_docs_count": -1,
    "download_candidate_count": -1,
    "download_success_count": -1,
    "satisfied_candidate_count": 0,
    "satisfied_candidates": [],
    "missing_candidate_count": 0,
    "missing_candidates": [],
    "scan_mode": None,
    "api_probe_status": None,
    "scan_alert": None,
    "log_path": log_path,
    "result_md_path": result_md_path,
    "canonical_result_path": canonical_path,
    "wrapper_fallback": True,
    "wrapper_failure_stage": failure_stage,
}
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  then
    return 0
  fi

  mkdir -p "$(dirname "$RESULT_JSON_PATH")" 2>/dev/null || return 1
  printf '{"status":"failed","exit_code":%s,"wrapper_fallback":true}\n' "$failure_rc" > "$RESULT_JSON_PATH"
}

invoke_notification_policy() {
  local notification_rc=0 notification_output=''
  if [[ "$NOTIFICATION_ATTEMPTED" -eq 1 ]]; then
    return 0
  fi
  NOTIFICATION_ATTEMPTED=1

  if [[ ! -f "$NOTIFICATION_POLICY_PATH" ]]; then
    NOTIFICATION_RESULT='{"decision":"skipped","reason":"policy_unavailable"}'
  elif notification_output="$("$PYTHON_BIN" "$NOTIFICATION_POLICY_PATH" \
    --result "$RESULT_JSON_PATH" --pipeline "$NOTIFICATION_PIPELINE" \
    --state "$NOTIFICATION_STATE_PATH" --outbox "$NOTIFICATION_OUTBOX_PATH" \
    --audit "$NOTIFICATION_AUDIT_PATH" --chat-id "$TARGET_CHAT_ID" \
    --lark-cli "$LARK_CLI_BIN" 2>> "$LOG_PATH")"; then
    NOTIFICATION_RESULT="$notification_output"
  else
    notification_rc=$?
    NOTIFICATION_RESULT="{\"decision\":\"failed\",\"reason\":\"policy_rc_${notification_rc}\"}"
  fi
  printf '[%s] 通知策略结果：%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$NOTIFICATION_RESULT" >> "$LOG_PATH" 2>/dev/null || true
}

ensure_current_failed_canonical() {
  # A launcher can fail before it has a chance to write its own canonical
  # result (for example after a missing executable or a malformed job path).
  # Replace any prior-run success result so notifications and operators never
  # mistake it for this trigger's outcome.
  [[ -n "$RESULT_HELPER_PATH" && -f "$RESULT_HELPER_PATH" ]] || return 0
  [[ -n "$CODEX_STRUCTURED_REPORT_PATH" ]] || return 0
  "$PYTHON_BIN" "$RESULT_HELPER_PATH" ensure-current \
    --canonical "$CODEX_STRUCTURED_REPORT_PATH" \
    --run-id "$RUN_ID" \
    --run-started-at "$RUN_STARTED_AT" \
    --run-finished-at "$(date '+%Y-%m-%dT%H:%M:%S%z')" \
    --requested-window-start "$WINDOW_START" \
    --requested-window-end "$WINDOW_END" \
    --pre-last-successful-check-at "" \
    --process-exit-code "$1" >/dev/null 2>&1 || true
  "$PYTHON_BIN" "$RESULT_HELPER_PATH" render \
    --canonical "$CODEX_STRUCTURED_REPORT_PATH" \
    --output "$RESULT_JSON_PATH" \
    --window-mode "$WINDOW_MODE" \
    --explicit-window-start "$WINDOW_START" \
    --explicit-window-end "$WINDOW_END" \
    --window-note "$WINDOW_NOTE" \
    --log-path "$LOG_PATH" \
    --result-md-path "$RESULT_MD_PATH" >/dev/null 2>&1 || true
}

on_exit() {
  local original_rc=$? fallback_rc
  fallback_rc="$original_rc"
  trap - EXIT
  set +e
  if [[ "$FINALIZATION_COMPLETE" -ne 1 ]]; then
    [[ "$fallback_rc" -ne 0 ]] || fallback_rc=1
    write_minimal_failed_result "$fallback_rc" "$FAILURE_STAGE"
    ensure_current_failed_canonical "$fallback_rc"
    printf '[%s] wrapper fallback: stage=%s rc=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$FAILURE_STAGE" "$fallback_rc" >> "$LOG_PATH" 2>/dev/null || true
    invoke_notification_policy
  fi
  exit "$original_rc"
}

trap on_exit EXIT

log_startup "run.sh entered"
FAILURE_STAGE="source_config"
if [[ ! -f "$TASK_DIR/config.env" ]]; then
  printf '[ERROR] missing task configuration: %s\n' "$TASK_DIR/config.env" >&2
  exit 2
fi
# shellcheck disable=SC1091
source "$TASK_DIR/config.env"
# deployment.env is generated by deploy/install_local_runtime.sh.  It is
# deliberately sourced after config.env so a release checkout can update code
# paths without rewriting a file that may hold private identifiers.
if [[ -f "$TASK_DIR/deployment.env" ]]; then
  # shellcheck disable=SC1091
  source "$TASK_DIR/deployment.env"
fi
refresh_runtime_paths
FAILURE_STAGE="post_config"

if [[ -z "$AUTOMATION_ROOT" && -n "$CODEX_SCRIPT_PATH" && -f "$CODEX_SCRIPT_PATH" ]]; then
  AUTOMATION_ROOT="$(cd "$(dirname "$CODEX_SCRIPT_PATH")/.." && pwd)"
fi
if [[ -z "$AUTOMATION_ROOT" ]]; then
  printf '[ERROR] set AUTOMATION_ROOT or CODEX_SCRIPT_PATH in config.env\n' >&2
  exit 2
fi

RESULT_HELPER_PATH="${RESULT_HELPER_PATH:-$AUTOMATION_ROOT/scripts/zsxq_autodownload_result.py}"
NOTIFICATION_POLICY_PATH="${NOTIFICATION_POLICY_PATH:-$AUTOMATION_ROOT/scripts/zsxq_notification_policy.py}"
RUNTIME_DIR="${INVESTMENT_REPORTS_RUNTIME_DIR:-$AUTOMATION_ROOT/.runtime}"
if [[ -z "${INVESTMENT_REPORTS_RUNTIME_DIR:-}" && -d "$AUTOMATION_ROOT/logs" ]]; then
  RUNTIME_DIR="$AUTOMATION_ROOT"
fi
if [[ -z "${CODEX_STRUCTURED_REPORT_PATH:-}" || "$CODEX_STRUCTURED_REPORT_PATH" == "$TASK_DIR/canonical_result.json" ]]; then
  if [[ "$NOTIFICATION_PIPELINE" == "domestic_cicc" ]]; then
    CODEX_STRUCTURED_REPORT_PATH="$RUNTIME_DIR/logs/zsxq_domestic_cicc_last_run_structured.json"
  else
    CODEX_STRUCTURED_REPORT_PATH="$RUNTIME_DIR/logs/zsxq_last_run_structured.json"
  fi
fi

log_startup "runtime configuration loaded"
log_startup "CODEX_SCRIPT_PATH=${CODEX_SCRIPT_PATH:-unset}"
log_startup "NOTIFICATION_PIPELINE=$NOTIFICATION_PIPELINE"

CFT_EXECUTABLE_PATH="${CFT_EXECUTABLE_PATH:-/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing}"
CFT_REMOTE_DEBUG_PORT="${CFT_REMOTE_DEBUG_PORT:-9223}"
CFT_USER_DATA_DIR="${CFT_USER_DATA_DIR:-$HOME/.openclaw/browser-profiles/zsxq-cft}"
export LARKSUITE_CLI_CONFIG_DIR="${LARKSUITE_CLI_CONFIG_DIR:-$HOME/.lark-cli/openclaw}"

FAILURE_STAGE="parse_override"
if [[ -f "$OVERRIDE_FILE" ]]; then
  PARSED="$($PYTHON_BIN - "$OVERRIDE_FILE" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
enabled = bool(payload.get("enabled", False))
start = str(payload.get("window_start", "")).strip()
end = str(payload.get("window_end", "")).strip()
apply_once = bool(payload.get("apply_once", True))
note = str(payload.get("note", "")).strip()
if enabled and start and end:
    print("explicit", start, end, "true" if apply_once else "false", note, sep="\n")
else:
    print("state\n\n\ntrue\n")
PY
)"
  WINDOW_MODE="$(printf '%s\n' "$PARSED" | sed -n '1p')"
  WINDOW_START="$(printf '%s\n' "$PARSED" | sed -n '2p')"
  WINDOW_END="$(printf '%s\n' "$PARSED" | sed -n '3p')"
  WINDOW_APPLY_ONCE="$(printf '%s\n' "$PARSED" | sed -n '4p')"
  WINDOW_NOTE="$(printf '%s\n' "$PARSED" | sed -n '5p')"
fi

FAILURE_STAGE="check_dependencies"
if [[ ! -f "$RESULT_HELPER_PATH" ]]; then
  printf '[ERROR] result helper missing: %s\n' "$RESULT_HELPER_PATH" >&2
  exit 2
fi
if [[ ! -f "$CODEX_SCRIPT_PATH" ]]; then
  printf '[ERROR] Codex launcher missing: %s\n' "$CODEX_SCRIPT_PATH" >&2
  exit 2
fi

FAILURE_STAGE="run_codex"
printf '[%s] 开始执行 Codex 下载入口，mode=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$WINDOW_MODE" >> "$LOG_PATH"
set +e
if [[ "$WINDOW_MODE" == "explicit" ]]; then
  CFT_EXECUTABLE_PATH="$CFT_EXECUTABLE_PATH" \
  CFT_REMOTE_DEBUG_PORT="$CFT_REMOTE_DEBUG_PORT" \
  CFT_USER_DATA_DIR="$CFT_USER_DATA_DIR" \
  ZSXQ_RUN_ID="$RUN_ID" \
  ZSXQ_STATUS_JSON_PATH="$STATUS_JSON_PATH" \
  ZSXQ_WINDOW_START="$WINDOW_START" \
  ZSXQ_WINDOW_END="$WINDOW_END" \
  bash "$CODEX_SCRIPT_PATH" > "$RESULT_MD_PATH" 2>&1
  CODEX_RC=$?
else
  CFT_EXECUTABLE_PATH="$CFT_EXECUTABLE_PATH" \
  CFT_REMOTE_DEBUG_PORT="$CFT_REMOTE_DEBUG_PORT" \
  CFT_USER_DATA_DIR="$CFT_USER_DATA_DIR" \
  ZSXQ_RUN_ID="$RUN_ID" \
  ZSXQ_STATUS_JSON_PATH="$STATUS_JSON_PATH" \
  bash "$CODEX_SCRIPT_PATH" > "$RESULT_MD_PATH" 2>&1
  CODEX_RC=$?
fi
set -e

END_TIME="$(date '+%Y-%m-%d %H:%M:%S')"
RUN_FINISHED_AT="$(date '+%Y-%m-%dT%H:%M:%S%z')"
printf '[%s] Codex 下载入口结束，rc=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$CODEX_RC" >> "$LOG_PATH"

FAILURE_STAGE="ensure_current_result"
"$PYTHON_BIN" "$RESULT_HELPER_PATH" ensure-current \
  --canonical "$CODEX_STRUCTURED_REPORT_PATH" \
  --run-id "$RUN_ID" \
  --run-started-at "$RUN_STARTED_AT" \
  --run-finished-at "$RUN_FINISHED_AT" \
  --requested-window-start "$WINDOW_START" \
  --requested-window-end "$WINDOW_END" \
  --process-exit-code "$CODEX_RC" >/dev/null

FAILURE_STAGE="render_result"
"$PYTHON_BIN" "$RESULT_HELPER_PATH" render \
  --canonical "$CODEX_STRUCTURED_REPORT_PATH" \
  --output "$RESULT_JSON_PATH" \
  --window-mode "$WINDOW_MODE" \
  --explicit-window-start "$WINDOW_START" \
  --explicit-window-end "$WINDOW_END" \
  --window-note "$WINDOW_NOTE" \
  --log-path "$LOG_PATH" \
  --result-md-path "$RESULT_MD_PATH" >/dev/null

FAILURE_STAGE="validate_result"
REPORT_STATUS="$($PYTHON_BIN - "$RESULT_JSON_PATH" <<'PY'
import json
import sys
from pathlib import Path

try:
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except Exception:
    print("failed")
else:
    print(str(data.get("status", "failed")).strip() or "failed")
PY
)"

FAILURE_STAGE="consume_override"
if [[ "$REPORT_STATUS" == "success" && "$WINDOW_MODE" == "explicit" && "$WINDOW_APPLY_ONCE" == "true" && -f "$OVERRIDE_FILE" ]]; then
  "$PYTHON_BIN" - "$OVERRIDE_FILE" "$END_TIME" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
payload["enabled"] = False
payload["last_applied_at"] = sys.argv[2]
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
fi

FAILURE_STAGE="notification_policy"
invoke_notification_policy
FINALIZATION_COMPLETE=1

printf 'ZSXQ_NOTIFY status=%s rc=%s mode=%s decision=%s result=%s\n' \
  "$REPORT_STATUS" "$CODEX_RC" "$WINDOW_MODE" "$NOTIFICATION_RESULT" "$RESULT_MD_PATH" || true
exit "$CODEX_RC"
