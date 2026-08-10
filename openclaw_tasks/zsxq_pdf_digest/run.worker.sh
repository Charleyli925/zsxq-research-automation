#!/usr/bin/env bash
# This file is the real worker for one summary batch.
# `run.sh` only starts a stable snapshot of this worker, so a live edit will not break a running batch.

set -euo pipefail

# 运行态文件继续写回任务目录；source 目录和脚本真实路径允许由 launcher 显式传入。
WORKER_BOOT_PATH="${BASH_SOURCE[0]}"
if [[ -n "${ZSXQ_RUNTIME_TASK_DIR:-}" ]]; then
  RUNTIME_TASK_DIR="$ZSXQ_RUNTIME_TASK_DIR"
else
  RUNTIME_TASK_DIR="$(cd "$(dirname "$WORKER_BOOT_PATH")" && pwd)"
fi
TASK_SCRIPT_REALPATH="${ZSXQ_TASK_SCRIPT_REALPATH:-$(python3 - "$WORKER_BOOT_PATH" <<'PY'
from pathlib import Path
import sys

print(Path(sys.argv[1]).expanduser().resolve())
PY
)}"
if [[ -n "${ZSXQ_SOURCE_TASK_DIR:-}" ]]; then
  SOURCE_TASK_DIR="$ZSXQ_SOURCE_TASK_DIR"
else
  SOURCE_TASK_DIR="$(cd "$(dirname "$TASK_SCRIPT_REALPATH")" && pwd)"
fi
cd "$RUNTIME_TASK_DIR"

CONFIG_PATH="${ZSXQ_CONFIG_PATH:-$RUNTIME_TASK_DIR/config.env}"
if [[ ! -f "$CONFIG_PATH" ]]; then
  CONFIG_PATH="$SOURCE_TASK_DIR/config.env"
fi

# shellcheck disable=SC1091
source "$CONFIG_PATH"
PYTHON_BIN="${PYTHON_BIN:-python3}"

resolve_task_asset_path() {
  local relative_path="$1"
  local runtime_path="$RUNTIME_TASK_DIR/$relative_path"
  if [[ -e "$runtime_path" ]]; then
    printf '%s\n' "$runtime_path"
    return 0
  fi
  printf '%s\n' "$SOURCE_TASK_DIR/$relative_path"
}

LOG_PATH="$RUNTIME_TASK_DIR/$LOG_FILE"
STATE_PATH="$RUNTIME_TASK_DIR/$STATE_FILE"
BATCH_JSON_PATH="$RUNTIME_TASK_DIR/$BATCH_JSON"
RESULT_JSON_PATH="$RUNTIME_TASK_DIR/$RESULT_JSON"
RESULT_MD_PATH="$RUNTIME_TASK_DIR/$RESULT_MD"
USAGE_JSON_PATH="$RUNTIME_TASK_DIR/${USAGE_JSON:-last_usage_summary.json}"
FAILURE_STATE_PATH="$RUNTIME_TASK_DIR/${FAILURE_STATE_JSON:-failure_backoff.json}"
STAGE_RETRY_LEDGER_PATH="$RUNTIME_TASK_DIR/${STAGE_RETRY_LEDGER_JSON:-stage_retry_ledger.json}"
RUN_STATUS_JSON_PATH="$RUNTIME_TASK_DIR/${RUN_STATUS_JSON:-run_status.json}"
NOTIFICATION_JSONL_PATH="$RUNTIME_TASK_DIR/${NOTIFICATION_JSONL:-notification_messages.jsonl}"
NOTIFICATION_OUTBOX_PATH="$RUNTIME_TASK_DIR/${NOTIFICATION_OUTBOX_JSON:-notification_outbox.json}"
SUMMARY_PROMPT_SOURCE_PATH="$(resolve_task_asset_path "summary_prompt.md")"
SUMMARY_SYSTEM_PROMPT_SOURCE_PATH="$(resolve_task_asset_path "summary_system_prompt.md")"
HELPER_SOURCE_PATH="$HELPER_SCRIPT_PATH"
SCANNER_SOURCE_PATH="$SCANNER_SCRIPT_PATH"
EXTRACT_TEXT_SOURCE_PATH="$(resolve_task_asset_path "extract_pdf_text.py")"
INDEX_SCRIPT_SOURCE_PATH="${RESEARCH_LIBRARY_INDEX_SCRIPT_PATH:-}"
MARKITDOWN_SCRIPT_SOURCE_PATH="${MARKITDOWN_SCRIPT_PATH:-}"
CLEAN_MARKDOWN_SCRIPT_SOURCE_PATH="${CLEAN_MARKDOWN_SCRIPT_PATH:-}"
OBSIDIAN_ARCHIVE_SCRIPT_SOURCE_PATH="${OBSIDIAN_ARCHIVE_SCRIPT_PATH:-}"
OBSIDIAN_INDEX_SCRIPT_SOURCE_PATH="${OBSIDIAN_INDEX_SCRIPT_PATH:-}"
RUNTIME_GUARD_SCRIPT_SOURCE_PATH="${RUNTIME_GUARD_SCRIPT_PATH:-}"
SUMMARY_PROMPT_PATH="${ZSXQ_SUMMARY_PROMPT_PATH:-$SUMMARY_PROMPT_SOURCE_PATH}"
SUMMARY_SYSTEM_PROMPT_PATH="${ZSXQ_SUMMARY_SYSTEM_PROMPT_PATH:-$SUMMARY_SYSTEM_PROMPT_SOURCE_PATH}"
HELPER_PATH="${ZSXQ_HELPER_SCRIPT_PATH:-$HELPER_SOURCE_PATH}"
SCANNER_PATH="${ZSXQ_SCANNER_SCRIPT_PATH:-$SCANNER_SOURCE_PATH}"
EXTRACT_TEXT_SCRIPT_PATH="${ZSXQ_EXTRACT_TEXT_SCRIPT_PATH:-$EXTRACT_TEXT_SOURCE_PATH}"
INDEX_SCRIPT_PATH="${ZSXQ_RESEARCH_LIBRARY_INDEX_SCRIPT_PATH:-$INDEX_SCRIPT_SOURCE_PATH}"
MARKITDOWN_SCRIPT_PATH_RESOLVED="${ZSXQ_MARKITDOWN_SCRIPT_PATH:-$MARKITDOWN_SCRIPT_SOURCE_PATH}"
CLEAN_MARKDOWN_SCRIPT_PATH_RESOLVED="${ZSXQ_CLEAN_MARKDOWN_SCRIPT_PATH:-$CLEAN_MARKDOWN_SCRIPT_SOURCE_PATH}"
OBSIDIAN_ARCHIVE_SCRIPT_PATH_RESOLVED="${ZSXQ_OBSIDIAN_ARCHIVE_SCRIPT_PATH:-$OBSIDIAN_ARCHIVE_SCRIPT_SOURCE_PATH}"
OBSIDIAN_INDEX_SCRIPT_PATH_RESOLVED="${ZSXQ_OBSIDIAN_INDEX_SCRIPT_PATH:-$OBSIDIAN_INDEX_SCRIPT_SOURCE_PATH}"
RUNTIME_GUARD_SCRIPT_PATH_RESOLVED="${ZSXQ_RUNTIME_GUARD_SCRIPT_PATH:-$RUNTIME_GUARD_SCRIPT_SOURCE_PATH}"
RELEASE_CONTRACT_ERROR="${ZSXQ_RELEASE_CONTRACT_ERROR:-}"
RESEARCH_LIBRARY_ROOT="${RESEARCH_LIBRARY_ROOT:-$HOME/Library/Application Support/investment-reports-automation/ResearchLibrary}"
OBSIDIAN_VAULT_ROOT="${OBSIDIAN_VAULT_ROOT:-$HOME/Library/Application Support/investment-reports-automation/ResearchVault}"
OBSIDIAN_INDEX_RESULT_JSON_VALUE="${OBSIDIAN_INDEX_RESULT_JSON:-state/obsidian_index_update_last_result.json}"
OBSIDIAN_INDEX_TIMEOUT_SECONDS="${OBSIDIAN_INDEX_TIMEOUT_SECONDS:-900}"
OBSIDIAN_INDEX_TERMINATE_GRACE_SECONDS="${OBSIDIAN_INDEX_TERMINATE_GRACE_SECONDS:-10}"
if [[ ! "$OBSIDIAN_INDEX_TIMEOUT_SECONDS" =~ ^[0-9]+$ || "$OBSIDIAN_INDEX_TIMEOUT_SECONDS" -lt 1 ]]; then
  OBSIDIAN_INDEX_TIMEOUT_SECONDS=900
fi
if [[ ! "$OBSIDIAN_INDEX_TERMINATE_GRACE_SECONDS" =~ ^[0-9]+$ || "$OBSIDIAN_INDEX_TERMINATE_GRACE_SECONDS" -lt 1 ]]; then
  OBSIDIAN_INDEX_TERMINATE_GRACE_SECONDS=10
fi
if [[ "$OBSIDIAN_INDEX_RESULT_JSON_VALUE" = /* ]]; then
  OBSIDIAN_INDEX_RESULT_JSON_PATH="$OBSIDIAN_INDEX_RESULT_JSON_VALUE"
else
  OBSIDIAN_INDEX_RESULT_JSON_PATH="$RUNTIME_TASK_DIR/$OBSIDIAN_INDEX_RESULT_JSON_VALUE"
fi
RUN_AT="${ZSXQ_RUN_AT_OVERRIDE:-$(date -Iseconds)}"
RUN_ID="$($PYTHON_BIN - <<'PY'
import uuid
print(uuid.uuid4())
PY
)"
LOCK_TOKEN="${RUN_ID}.$$.$RANDOM"
QUIET_WINDOW_SECONDS="$((QUIET_WINDOW_MINUTES * 60))"
LARK_CLI_BIN="${LARK_CLI_BIN:-lark-cli}"
LARKSUITE_CLI_CONFIG_DIR="${LARKSUITE_CLI_CONFIG_DIR:-$HOME/.lark-cli/openclaw}"
LARK_CLI_SEND_AS="${LARK_CLI_SEND_AS:-bot}"
LARK_CLI_NOTIFICATIONS="${LARK_CLI_NOTIFICATIONS:-true}"
PUBLISH_LARK_CLI_AS="${PUBLISH_LARK_CLI_AS:-user}"
PUBLISH_LARK_CLI_PARENT_POSITION="${PUBLISH_LARK_CLI_PARENT_POSITION:-my_library}"
PUBLISH_LARK_CLI_DOC_URL_BASE="${PUBLISH_LARK_CLI_DOC_URL_BASE:-https://www.feishu.cn/docx}"
PUBLISH_MAX_FILES_PER_DOC="${PUBLISH_MAX_FILES_PER_DOC:-20}"
PUBLISH_LEGACY_RECORD_FILE_COUNT="${PUBLISH_LEGACY_RECORD_FILE_COUNT:-10}"
PUBLISH_FETCH_VERIFY_ATTEMPTS="${PUBLISH_FETCH_VERIFY_ATTEMPTS:-4}"
PUBLISH_FETCH_VERIFY_DELAY_SECONDS="${PUBLISH_FETCH_VERIFY_DELAY_SECONDS:-3}"
if [[ ! "$PUBLISH_FETCH_VERIFY_ATTEMPTS" =~ ^[0-9]+$ || "$PUBLISH_FETCH_VERIFY_ATTEMPTS" -lt 1 || "$PUBLISH_FETCH_VERIFY_ATTEMPTS" -gt 8 ]]; then
  PUBLISH_FETCH_VERIFY_ATTEMPTS=4
fi
if [[ ! "$PUBLISH_FETCH_VERIFY_DELAY_SECONDS" =~ ^[0-9]+$ || "$PUBLISH_FETCH_VERIFY_DELAY_SECONDS" -gt 30 ]]; then
  PUBLISH_FETCH_VERIFY_DELAY_SECONDS=3
fi
PUBLISH_RECORDS_JSONL_VALUE="${PUBLISH_RECORDS_JSONL:-publish_records.jsonl}"
if [[ "$PUBLISH_RECORDS_JSONL_VALUE" = /* ]]; then
  PUBLISH_RECORDS_JSONL_PATH="$PUBLISH_RECORDS_JSONL_VALUE"
else
  PUBLISH_RECORDS_JSONL_PATH="$RUNTIME_TASK_DIR/$PUBLISH_RECORDS_JSONL_VALUE"
fi
NOTIFICATION_STATUS_BUCKET_SECONDS="${NOTIFICATION_STATUS_BUCKET_SECONDS:-3600}"
AUTO_RETRY_MAX_SAME_BATCH="${AUTO_RETRY_MAX_SAME_BATCH:-3}"
AUTO_RETRY_BASE_MINUTES="${AUTO_RETRY_BASE_MINUTES:-30}"
AUTO_RETRY_MAX_COOLDOWN_MINUTES="${AUTO_RETRY_MAX_COOLDOWN_MINUTES:-180}"
AUTO_RETRY_TRANSIENT_MAX_SAME_BATCH="${AUTO_RETRY_TRANSIENT_MAX_SAME_BATCH:-4}"
AUTO_RETRY_TRANSIENT_BASE_MINUTES="${AUTO_RETRY_TRANSIENT_BASE_MINUTES:-5}"
AUTO_RETRY_TRANSIENT_MAX_COOLDOWN_MINUTES="${AUTO_RETRY_TRANSIENT_MAX_COOLDOWN_MINUTES:-20}"
PER_FILE_RETRY_MAX_ATTEMPTS="${PER_FILE_RETRY_MAX_ATTEMPTS:-4}"
PER_FILE_RETRY_DELAYS_MINUTES="${PER_FILE_RETRY_DELAYS_MINUTES:-5,10,20}"
NOTIFICATION_OUTBOX_MAX_PER_RUN="${NOTIFICATION_OUTBOX_MAX_PER_RUN:-10}"
SEND_DOCUMENT_NOTIFICATION_EACH_BATCH="${SEND_DOCUMENT_NOTIFICATION_EACH_BATCH:-true}"
MANUAL_MODE="false"
DRY_RUN="false"
SEND_NOTIFICATIONS="true"
PREFLIGHT_ONLY="false"
MANUAL_FILES=()
MANUAL_FOLDERS=()
SUMMARY_AGENT_ID="${SUMMARY_AGENT_ID:-zsxq_pdf_digest_summary}"
SUMMARY_AGENT_THINKING="${SUMMARY_AGENT_THINKING:-medium}"
SUMMARY_AGENT_TIMEOUT_SECONDS="${SUMMARY_AGENT_TIMEOUT_SECONDS:-600}"
SUMMARY_TIMEOUT_RETRY_COUNT="${SUMMARY_TIMEOUT_RETRY_COUNT:-1}"
SUMMARY_PARALLEL_ENABLED="${SUMMARY_PARALLEL_ENABLED:-false}"
SUMMARY_WORKER_COUNT="${SUMMARY_WORKER_COUNT:-1}"
if [[ ! "$SUMMARY_WORKER_COUNT" =~ ^[0-9]+$ || "$SUMMARY_WORKER_COUNT" -lt 1 ]]; then
  SUMMARY_WORKER_COUNT=1
fi
SUMMARY_WORKER_AGENT_ID_PREFIX="${SUMMARY_WORKER_AGENT_ID_PREFIX:-${SUMMARY_AGENT_ID}_w}"
RESET_AGENT_SESSION_ON_RUN="${RESET_AGENT_SESSION_ON_RUN:-true}"
SUMMARY_AGENT_SESSIONS_DIR="${HOME}/.openclaw/agents/${SUMMARY_AGENT_ID}/sessions"
ACTIVE_WORKERS_DIR=""
PID_FILE="$RUNTIME_TASK_DIR/.run.pid"
LOCK_DIR="$RUNTIME_TASK_DIR/.run.lock"
LOCK_HELD="false"
TEMP_DIR=""
DOC_URLS=()
HEARTBEAT_PID=""
RUN_STATUS_ACTIVE_RUN="false"
RUN_STATUS_FINALIZED="false"
RUN_STATUS_LAST_PHASE="init"
RUN_STATUS_LAST_MESSAGE="summary task not started"
RUN_STATUS_LAST_OPERATIONAL_STATE="starting"
DISCOVERED_PDF_COUNT=""
DEFERRED_RETRY_FILE_COUNT=0
PREFLIGHT_JSON_PATH="$RUNTIME_TASK_DIR/${PREFLIGHT_JSON:-last_preflight.json}"
MARKITDOWN_PREFLIGHT_JSON_PATH="$RUNTIME_TASK_DIR/last_markitdown_preflight.json"
QUARANTINE_JSON_PATH="$RUNTIME_TASK_DIR/${QUARANTINE_JSON:-quarantine.json}"
QUARANTINE_REPORT_PATH="$RUNTIME_TASK_DIR/${QUARANTINE_REPORT_MD:-quarantine_report.md}"
TEXT_CACHE_DIR_RAW="${TEXT_CACHE_DIR:-text_cache}"
if [[ "$TEXT_CACHE_DIR_RAW" = /* ]]; then
  TEXT_CACHE_DIR_PATH="$TEXT_CACHE_DIR_RAW"
else
  TEXT_CACHE_DIR_PATH="$RUNTIME_TASK_DIR/$TEXT_CACHE_DIR_RAW"
fi
SUMMARY_CACHE_DIR_RAW="${SUMMARY_CACHE_DIR:-summary_cache}"
if [[ "$SUMMARY_CACHE_DIR_RAW" = /* ]]; then
  SUMMARY_CACHE_DIR_PATH="$SUMMARY_CACHE_DIR_RAW"
else
  SUMMARY_CACHE_DIR_PATH="$RUNTIME_TASK_DIR/$SUMMARY_CACHE_DIR_RAW"
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --file)
      MANUAL_MODE="true"
      MANUAL_FILES+=("$2")
      shift 2
      ;;
    --folder)
      MANUAL_MODE="true"
      MANUAL_FOLDERS+=("$2")
      shift 2
      ;;
    --dry-run)
      DRY_RUN="true"
      SEND_NOTIFICATIONS="false"
      shift
      ;;
    --no-notify)
      SEND_NOTIFICATIONS="false"
      shift
      ;;
    --preflight-only)
      PREFLIGHT_ONLY="true"
      SEND_NOTIFICATIONS="false"
      shift
      ;;
    *)
      echo "未知参数：$1" >&2
      exit 2
      ;;
  esac
done

acquire_run_lock() {
  if [[ ! -f "$RUNTIME_GUARD_SCRIPT_PATH_RESOLVED" ]]; then
    printf '[%s] runtime lock guard missing: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$RUNTIME_GUARD_SCRIPT_PATH_RESOLVED" >> "$LOG_PATH"
    return 2
  fi

  local lock_output lock_rc
  set +e
  lock_output="$($PYTHON_BIN "$RUNTIME_GUARD_SCRIPT_PATH_RESOLVED" lock-acquire \
    --lock-dir "$LOCK_DIR" \
    --token "$LOCK_TOKEN" \
    --run-id "$RUN_ID" \
    --owner-pid "$$" \
    --task "zsxq_pdf_digest" \
    --stale-seconds "${SUMMARY_LOCK_STALE_SECONDS:-300}" 2>&1)"
  lock_rc=$?
  set -e
  printf '[%s] runtime lock acquire rc=%s result=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$lock_rc" "$lock_output" >> "$LOG_PATH"

  if [[ "$lock_rc" -eq 0 ]]; then
    printf '%s\n' "$$" > "$PID_FILE"
    LOCK_HELD="true"
    return 0
  fi
  return "$lock_rc"
}

release_run_lock() {
  if [[ "$LOCK_HELD" == "true" ]]; then
    local lock_output lock_rc pid_value
    set +e
    lock_output="$($PYTHON_BIN "$RUNTIME_GUARD_SCRIPT_PATH_RESOLVED" lock-release \
      --lock-dir "$LOCK_DIR" \
      --token "$LOCK_TOKEN" 2>&1)"
    lock_rc=$?
    set -e
    printf '[%s] runtime lock release rc=%s result=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$lock_rc" "$lock_output" >> "$LOG_PATH"
    pid_value="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [[ "$pid_value" == "$$" ]]; then
      rm -f "$PID_FILE"
    fi
    LOCK_HELD="false"
  fi
}

cleanup() {
  stop_heartbeat
  release_run_lock
  if [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]]; then
    rm -rf "$TEMP_DIR"
  fi
}

cleanup_and_finalize() {
  local exit_code="${1:-0}"
  if [[ "$RUN_STATUS_ACTIVE_RUN" == "true" && "$RUN_STATUS_FINALIZED" != "true" ]]; then
    write_run_status \
      "failed" \
      "${RUN_STATUS_LAST_PHASE:-unexpected_exit}" \
      "summary task exited unexpectedly" \
      "$exit_code" \
      "unexpected_exit"
    RUN_STATUS_FINALIZED="true"
  fi
  cleanup
}

append_unique_doc_url() {
  local value="${1:-}"
  if [[ -z "$value" ]]; then
    return 0
  fi

  local existing
  for existing in "${DOC_URLS[@]:-}"; do
    if [[ "$existing" == "$value" ]]; then
      return 0
    fi
  done

  DOC_URLS+=("$value")
}

build_doc_urls_json() {
  python3 - "$@" <<'PY'
import json
import sys

urls = []
for item in sys.argv[1:]:
    value = str(item or "").strip()
    if value and value not in urls:
        urls.append(value)

print(json.dumps(urls, ensure_ascii=False))
PY
}

summary_parallel_should_run() {
  [[ "${SUMMARY_PARALLEL_ENABLED:-false}" == "true" && "${SUMMARY_WORKER_COUNT:-1}" -gt 1 && "$DRY_RUN" != "true" ]]
}

summary_agent_id_for_worker() {
  local worker_id="${1:-1}"
  if summary_parallel_should_run; then
    printf '%s%s\n' "$SUMMARY_WORKER_AGENT_ID_PREFIX" "$worker_id"
    return 0
  fi
  printf '%s\n' "$SUMMARY_AGENT_ID"
}

summary_sessions_dir_for_agent() {
  local agent_id="$1"
  printf '%s\n' "${HOME}/.openclaw/agents/${agent_id}/sessions"
}

summary_auth_target_paths() {
  printf '%s\n' "${HOME}/.openclaw/agents/${SUMMARY_AGENT_ID}/agent/auth-profiles.json"
  if summary_parallel_should_run; then
    local worker_index
    for ((worker_index = 1; worker_index <= SUMMARY_WORKER_COUNT; worker_index++)); do
      printf '%s\n' "${HOME}/.openclaw/agents/${SUMMARY_WORKER_AGENT_ID_PREFIX}${worker_index}/agent/auth-profiles.json"
    done
  fi
}

summary_auth_fingerprint_args() {
  printf '%s\n' "summary_agent_auth" "${HOME}/.openclaw/agents/${SUMMARY_AGENT_ID}/agent/auth-profiles.json"
  if summary_parallel_should_run; then
    local worker_index
    for ((worker_index = 1; worker_index <= SUMMARY_WORKER_COUNT; worker_index++)); do
      printf '%s\n' "summary_worker_${worker_index}_agent_auth" "${HOME}/.openclaw/agents/${SUMMARY_WORKER_AGENT_ID_PREFIX}${worker_index}/agent/auth-profiles.json"
    done
  fi
}

build_scan_root_args() {
  SCAN_ROOT_ARGS=(--root "$WATCH_ROOT")

  if [[ -n "${WATCH_ROOTS:-}" ]]; then
    SCAN_ROOT_ARGS=()
    local roots_raw="$WATCH_ROOTS"
    local root
    local root_index=0
    IFS=':' read -r -a roots <<< "$roots_raw"
    for root in "${roots[@]}"; do
      if [[ -n "$root" ]]; then
        if [[ "$root_index" -eq 0 ]]; then
          SCAN_ROOT_ARGS+=(--root "$root")
        else
          SCAN_ROOT_ARGS+=(--extra-root "$root")
        fi
        root_index=$((root_index + 1))
      fi
    done
  fi

  if [[ -n "${WATCH_EXTRA_ROOTS:-}" ]]; then
    local extra_roots_raw="$WATCH_EXTRA_ROOTS"
    local extra_root
    IFS=':' read -r -a extra_roots <<< "$extra_roots_raw"
    for extra_root in "${extra_roots[@]}"; do
      if [[ -n "$extra_root" ]]; then
        SCAN_ROOT_ARGS+=(--extra-root "$extra_root")
      fi
    done
  fi

  if [[ "${#SCAN_ROOT_ARGS[@]}" -eq 0 ]]; then
    SCAN_ROOT_ARGS=(--root "$WATCH_ROOT")
  fi
}

print_doc_links() {
  if [[ "$#" -le 0 ]]; then
    printf '文档链接：未生成\n'
    return 0
  fi

  if [[ "$#" -eq 1 ]]; then
    printf '文档链接：%s\n' "$1"
    return 0
  fi

  local index=1
  local url
  printf '文档链接：\n'
  for url in "$@"; do
    printf -- '- 文档%s：%s\n' "$index" "$url"
    index=$((index + 1))
  done
}

notification_status_bucket() {
  "$PYTHON_BIN" - "$RUN_AT" "$NOTIFICATION_STATUS_BUCKET_SECONDS" <<'PY'
import sys
from datetime import datetime

run_at = sys.argv[1]
bucket_seconds = int(sys.argv[2] or 3600)
try:
    dt = datetime.fromisoformat(run_at)
    bucket = int(dt.timestamp()) // bucket_seconds
except Exception:
    bucket = 0
print(str(bucket))
PY
}

build_notification_identity() {
  local event_name="${1:-general}"
  local seed="${2:-}"
  local message="${3:-}"
  "$PYTHON_BIN" - "$event_name" "$seed" "$RUN_AT" "$RESULT_JSON_PATH" "$message" <<'PY'
import hashlib
import json
import re
import sys
from pathlib import Path

event_name = re.sub(r"[^A-Za-z0-9_-]+", "-", sys.argv[1].strip() or "general").strip("-") or "general"
seed = sys.argv[2]
run_at = sys.argv[3]
result_path = Path(sys.argv[4]).expanduser()
message = sys.argv[5]


def normalize_file_seed(path: Path) -> dict[str, object] | None:
    try:
        if not path.exists() or not path.is_file():
            return None
    except OSError:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    files = []
    for item in payload.get("files", []):
        if not isinstance(item, dict):
            continue
        file_path = str(item.get("path", "")).strip()
        if not file_path:
            continue
        files.append(
            {
                "path": file_path,
                "size_bytes": int(item.get("size_bytes", 0) or 0),
                "modified_at": str(item.get("modified_at", "")).strip(),
            }
        )
    return {
        "files": sorted(files, key=lambda item: item["path"]),
        "new_pdf_count": int(payload.get("new_pdf_count", len(files)) or 0),
    }


seed_path = Path(seed).expanduser() if seed else None
normalized_seed = normalize_file_seed(seed_path) if seed_path is not None else None
if normalized_seed is None:
    normalized_seed = {"seed": seed or run_at}

if event_name == "doc-completed":
    # Keep document-ready identity stable even if last_result.json for this
    # run already exists during a recovery.  Its separate scope also prevents
    # a terminal summary from superseding a pending one-document notice.
    terminal_signature = {"phase": "document_ready"}
    scope_seed = {"namespace": "document", "seed": normalized_seed}
else:
    terminal_signature = {}
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except Exception:
        result = {}
    if isinstance(result, dict) and str(result.get("execute_time", "")).strip() == run_at:
        for key in ("status", "run_outcome", "operational_state", "pipeline_health", "message"):
            value = result.get(key)
            if value not in (None, ""):
                terminal_signature[key] = value
    if not terminal_signature:
        reason_lines = [line.strip() for line in message.splitlines() if line.strip().startswith("原因：")]
        terminal_signature = {"reason": reason_lines[:3]}
    scope_seed = normalized_seed

scope_raw = json.dumps(scope_seed, ensure_ascii=False, sort_keys=True)
scope_digest = hashlib.sha256(scope_raw.encode("utf-8")).hexdigest()[:24]
raw = json.dumps(
    {"event": event_name, "seed": normalized_seed, "terminal": terminal_signature},
    ensure_ascii=False,
    sort_keys=True,
)
digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
event_key = event_name[:17]
short_digest = digest[:16]
print("\x1f".join([f"zsxq-pdf-digest-{event_key}-{short_digest}", f"digest-batch-{scope_digest}"]))
PY
}

sync_result_notification_records() {
  if [[ ! -f "$RESULT_JSON_PATH" || ! -f "$NOTIFICATION_JSONL_PATH" ]]; then
    return 0
  fi

  "$PYTHON_BIN" - "$RESULT_JSON_PATH" "$NOTIFICATION_JSONL_PATH" "$RUN_AT" <<'PY' || true
import json
import sys
from pathlib import Path

result_path = Path(sys.argv[1])
notification_path = Path(sys.argv[2])
run_at = sys.argv[3]

try:
    payload = json.loads(result_path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)

if str(payload.get("execute_time", "")).strip() != run_at:
    raise SystemExit(0)

records = []
for line in notification_path.read_text(encoding="utf-8", errors="replace").splitlines():
    if not line.strip():
        continue
    try:
        record = json.loads(line)
    except Exception:
        continue
    if str(record.get("run_at", "")).strip() != run_at:
        continue
    records.append(record)

payload["notification_messages"] = records
message_ids = [
    str(item.get("message_id") or "").strip()
    for item in records
    if item.get("channel") == "lark-cli" and str(item.get("message_id") or "").strip()
]
if message_ids:
    payload["last_notification_message_id"] = message_ids[-1]
else:
    payload.pop("last_notification_message_id", None)

tmp_path = result_path.with_suffix(result_path.suffix + ".tmp")
tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp_path.replace(result_path)
PY
}

record_notification_result() {
  local channel="$1"
  local event_name="$2"
  local idempotency_key="$3"
  local message_format="$4"
  local status="$5"
  local message_id="${6:-}"
  local error_message="${7:-}"

  "$PYTHON_BIN" - "$NOTIFICATION_JSONL_PATH" "$RUN_AT" "$channel" "$event_name" "$idempotency_key" "$message_format" "$status" "$message_id" "$error_message" <<'PY' || true
import json
import re
import sys
from datetime import datetime
from pathlib import Path

path = Path(sys.argv[1])


def redact(value: str) -> str:
    text = str(value or "")
    text = re.sub(r"cli_[A-Za-z0-9_-]+", "cli_***", text)
    text = re.sub(r"(appSecret[\"'=:\s]+)[^\"',\s}]+", r"\1***", text, flags=re.IGNORECASE)
    return text[:1000]


record = {
    "sent_at": datetime.now().astimezone().isoformat(),
    "run_at": sys.argv[2],
    "channel": sys.argv[3],
    "event": sys.argv[4],
    "idempotency_key": sys.argv[5],
    "format": sys.argv[6],
    "status": sys.argv[7],
    "message_id": sys.argv[8] or None,
    "error": redact(sys.argv[9]) or None,
}
path.parent.mkdir(parents=True, exist_ok=True)
with path.open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
PY

  printf '[%s] 通知发送记录：channel=%s event=%s status=%s idempotency_key=%s message_id=%s\n' \
    "$(date '+%Y-%m-%d %H:%M:%S')" \
    "$channel" \
    "$event_name" \
    "$status" \
    "$idempotency_key" \
    "${message_id:-}" >> "$LOG_PATH"
  if [[ -n "$error_message" ]]; then
    printf '[%s] 通知发送错误：channel=%s event=%s error=%s\n' \
      "$(date '+%Y-%m-%d %H:%M:%S')" \
      "$channel" \
      "$event_name" \
      "$error_message" >> "$LOG_PATH"
  fi
  sync_result_notification_records
}

notification_success_already_recorded() {
  local idempotency_key="$1"
  if [[ -z "$idempotency_key" || ! -f "$NOTIFICATION_JSONL_PATH" ]]; then
    return 1
  fi

  "$PYTHON_BIN" - "$NOTIFICATION_JSONL_PATH" "$idempotency_key" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
target_key = sys.argv[2]
for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
    if not line.strip():
        continue
    try:
        record = json.loads(line)
    except Exception:
        continue
    if record.get("idempotency_key") == target_key and record.get("status") == "success":
        raise SystemExit(0)
raise SystemExit(1)
PY
}

extract_lark_message_id() {
  local output_path="$1"
  "$PYTHON_BIN" - "$output_path" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)


def find_message_id(value):
    if isinstance(value, dict):
        message_id = str(value.get("message_id", "")).strip()
        if message_id:
            return message_id
        for child in value.values():
            found = find_message_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_message_id(child)
            if found:
                return found
    return ""


print(find_message_id(payload))
PY
}

summarize_command_output() {
  local output_path="$1"
  local error_path="$2"
  "$PYTHON_BIN" - "$output_path" "$error_path" <<'PY'
import re
import sys
from pathlib import Path

parts = []
for raw_path in sys.argv[1:]:
    path = Path(raw_path)
    if path.exists():
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            parts.append(text)
summary = "\n".join(parts).strip()
summary = re.sub(r"cli_[A-Za-z0-9_-]+", "cli_***", summary)
summary = re.sub(r"(appSecret[\"'=:\s]+)[^\"',\s}]+", r"\1***", summary, flags=re.IGNORECASE)
print(summary[:1000] or "command failed")
PY
}

record_notification_outbox_delivery() {
  local idempotency_key="$1"
  local status="$2"
  local message_id="${3:-}"
  local error_message="${4:-}"
  if [[ ! -f "$HELPER_PATH" || ! -f "$NOTIFICATION_OUTBOX_PATH" ]]; then
    return 0
  fi
  "$PYTHON_BIN" "$HELPER_PATH" notification-outbox-record \
    --outbox-file "$NOTIFICATION_OUTBOX_PATH" \
    --idempotency-key "$idempotency_key" \
    --run-at "$RUN_AT" \
    --status "$status" \
    --message-id "$message_id" \
    --error "$error_message" >> "$LOG_PATH" 2>&1 || true
}

send_lark_cli_message() {
  local message="$1"
  local event_name="$2"
  local message_format="$3"
  local idempotency_key="$4"

  if [[ "$LARK_CLI_NOTIFICATIONS" != "true" ]]; then
    return 1
  fi
  if ! command -v "$LARK_CLI_BIN" >/dev/null 2>&1; then
    record_notification_result "lark-cli" "$event_name" "$idempotency_key" "$message_format" "skipped" "" "lark-cli command not found: $LARK_CLI_BIN"
    record_notification_outbox_delivery "$idempotency_key" "failed" "" "lark-cli command not found: $LARK_CLI_BIN"
    return 1
  fi

  local output_path error_path
  output_path="$(mktemp "${TMPDIR:-/tmp}/zsxq_lark_notify.out.XXXXXX")"
  error_path="$(mktemp "${TMPDIR:-/tmp}/zsxq_lark_notify.err.XXXXXX")"

  local args=(im +messages-send --chat-id "$TARGET_CHAT_ID" --idempotency-key "$idempotency_key" --as "$LARK_CLI_SEND_AS" --json)
  if [[ "$message_format" == "markdown" ]]; then
    args+=(--markdown "$message")
  else
    args+=(--text "$message")
  fi

  set +e
  if [[ -n "${LARKSUITE_CLI_CONFIG_DIR:-}" ]]; then
    LARKSUITE_CLI_CONFIG_DIR="$LARKSUITE_CLI_CONFIG_DIR" "$LARK_CLI_BIN" "${args[@]}" > "$output_path" 2> "$error_path"
  else
    "$LARK_CLI_BIN" "${args[@]}" > "$output_path" 2> "$error_path"
  fi
  local send_rc=$?
  set -e

  if [[ "$send_rc" -eq 0 ]]; then
    local message_id
    message_id="$(extract_lark_message_id "$output_path")"
    record_notification_result "lark-cli" "$event_name" "$idempotency_key" "$message_format" "success" "$message_id" ""
    record_notification_outbox_delivery "$idempotency_key" "success" "$message_id" ""
    rm -f "$output_path" "$error_path"
    return 0
  fi

  local error_summary
  error_summary="$(summarize_command_output "$output_path" "$error_path")"
  record_notification_result "lark-cli" "$event_name" "$idempotency_key" "$message_format" "failed" "" "$error_summary"
  record_notification_outbox_delivery "$idempotency_key" "failed" "" "$error_summary"
  rm -f "$output_path" "$error_path"
  return 1
}

drain_notification_outbox() {
  if [[ "$SEND_NOTIFICATIONS" != "true" || "$LARK_CLI_NOTIFICATIONS" != "true" || ! -f "$HELPER_PATH" ]]; then
    return 0
  fi

  local processed=0
  while [[ "$processed" -lt "$NOTIFICATION_OUTBOX_MAX_PER_RUN" ]]; do
    local due_path message_path
    due_path="$(mktemp "${TMPDIR:-/tmp}/zsxq_notify_due.XXXXXX")"
    message_path="$(mktemp "${TMPDIR:-/tmp}/zsxq_notify_message.XXXXXX")"
    if ! "$PYTHON_BIN" "$HELPER_PATH" notification-outbox-next-due \
      --outbox-file "$NOTIFICATION_OUTBOX_PATH" \
      --run-at "$RUN_AT" > "$due_path" 2>> "$LOG_PATH"; then
      rm -f "$due_path" "$message_path"
      return 0
    fi

    local due_fields
    due_fields="$($PYTHON_BIN - "$due_path" "$message_path" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
item = payload.get("item") if payload.get("found") else None
if not isinstance(item, dict):
    print("false")
    raise SystemExit(0)
Path(sys.argv[2]).write_text(str(item.get("message", "")), encoding="utf-8")
print("\x1f".join([
    "true",
    str(item.get("idempotency_key", "")),
    str(item.get("event", "general")),
    str(item.get("format", "text")),
]))
PY
)"
    local found idempotency_key event_name message_format
    IFS=$'\x1f' read -r found idempotency_key event_name message_format <<< "$due_fields"
    if [[ "$found" != "true" || -z "$idempotency_key" ]]; then
      rm -f "$due_path" "$message_path"
      return 0
    fi

    send_lark_cli_message "$(cat "$message_path")" "$event_name" "$message_format" "$idempotency_key" || true
    rm -f "$due_path" "$message_path"
    processed=$((processed + 1))
  done
}

send_chat_message() {
  local message="${1:-}"
  local event_name="${2:-general}"
  local message_format="${3:-text}"
  local idempotency_seed="${4:-}"
  if [[ -z "$message" || "$SEND_NOTIFICATIONS" != "true" || "$LARK_CLI_NOTIFICATIONS" != "true" ]]; then
    return 0
  fi
  local notification_identity idempotency_key supersede_scope
  notification_identity="$(build_notification_identity "$event_name" "$idempotency_seed" "$message")"
  IFS=$'\x1f' read -r idempotency_key supersede_scope <<< "$notification_identity"

  if [[ ! -f "$HELPER_PATH" ]]; then
    send_lark_cli_message "$message" "$event_name" "$message_format" "$idempotency_key" || true
    return 0
  fi

  local message_path
  message_path="$(mktemp "${TMPDIR:-/tmp}/zsxq_notify_enqueue.XXXXXX")"
  printf '%s' "$message" > "$message_path"
  "$PYTHON_BIN" "$HELPER_PATH" notification-outbox-enqueue \
    --outbox-file "$NOTIFICATION_OUTBOX_PATH" \
    --idempotency-key "$idempotency_key" \
    --supersede-scope "$supersede_scope" \
    --event "$event_name" \
    --format "$message_format" \
    --message-file "$message_path" \
    --run-id "$RUN_ID" \
    --run-at "$RUN_AT" >> "$LOG_PATH" 2>&1 || true
  rm -f "$message_path"
  drain_notification_outbox || true
  return 0
}

render_compact_terminal_result() {
  if [[ ! -f "$RESULT_JSON_PATH" ]]; then
    return 0
  fi
  "$PYTHON_BIN" - "$RESULT_JSON_PATH" "$RESULT_MD_PATH" "$RUN_STATUS_JSON_PATH" "$STAGE_RETRY_LEDGER_PATH" "$QUARANTINE_JSON_PATH" "$RUN_AT" "${ORIGINAL_BATCH_JSON_PATH:-$BATCH_JSON_PATH}" <<'PY' || true
import json
import sys
from pathlib import Path


def load(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def clean(value: object, limit: int = 180) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


result_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
status_payload = load(Path(sys.argv[3]))
ledger = load(Path(sys.argv[4]))
quarantine = load(Path(sys.argv[5]))
run_at = sys.argv[6]
batch = load(Path(sys.argv[7]))
result = load(result_path)

status = str(result.get("status", "")).strip()
outcome = str(result.get("run_outcome", "")).strip()
if status == "success" and outcome not in {"noop", "waiting"}:
    icon, label = "✅", "本轮完成"
elif status in {"partial_success", "completed_with_quarantine"} or outcome in {"partial_success", "completed_with_quarantine"}:
    icon, label = "⚠️", "本轮部分完成"
else:
    icon, label = "❌", "本轮失败"

new_count = int(result.get("new_pdf_count", 0) or 0)
discovered = result.get("discovered_pdf_count")
total = int(discovered if discovered is not None else new_count)
summary_count = int(result.get("summary_ready_count", 0) or 0)
published_count = int(result.get("published_count", 0) or 0)
quarantined_count = int(result.get("quarantined_count", 0) or 0)
if total > 0 and published_count == 0 and quarantined_count >= total:
    icon, label = "🟠", "本轮未发布"
remaining = status_payload.get("remaining_count")
try:
    remaining_count = int(remaining) if remaining is not None else max(total - published_count - quarantined_count, 0)
except Exception:
    remaining_count = max(total - published_count - quarantined_count, 0)
if icon == "✅":
    abnormal_count = 0
else:
    abnormal_count = max(quarantined_count + max(remaining_count, 0), total - published_count, 0)

lines = [
    f"## {icon} 知识星球研报｜{label}",
    "",
    f"下载/待处理 **{total}**｜总结 **{summary_count}**｜发布 **{published_count}**｜异常 **{abnormal_count}**",
]

doc_urls = []
for raw in result.get("doc_urls", []) or []:
    value = str(raw or "").strip()
    if value and value not in doc_urls:
        doc_urls.append(value)
if doc_urls:
    lines.extend(["", "文档：" + " · ".join(f"[飞书文档 {index}]({url})" for index, url in enumerate(doc_urls, 1))])

if abnormal_count > 0:
    abnormal_files = []
    for entry in (ledger.get("entries") or {}).values():
        if not isinstance(entry, dict) or str(entry.get("last_failed_at", "")).strip() != run_at:
            continue
        name = clean(entry.get("filename") or Path(str(entry.get("path", ""))).name, 100)
        if name and name not in abnormal_files:
            abnormal_files.append(name)
    for entry in (quarantine.get("entries") or []):
        if not isinstance(entry, dict) or str(entry.get("last_quarantined_at", "")).strip() != run_at:
            continue
        name = clean(entry.get("filename") or Path(str(entry.get("path", ""))).name, 100)
        if name and name not in abnormal_files:
            abnormal_files.append(name)
    if not abnormal_files:
        source_files = result.get("files") or [item.get("filename") for item in batch.get("files", []) if isinstance(item, dict)]
        abnormal_files = [clean(name, 100) for name in source_files if clean(name, 100)][:3]
    if abnormal_files:
        visible = abnormal_files[:3]
        suffix = f" 等 {len(abnormal_files)} 份" if len(abnormal_files) > 3 else ""
        lines.extend(["", "异常文件：" + "、".join(f"`{name.replace('`', '')}`" for name in visible) + suffix])
if icon != "✅":
    reason = clean(result.get("message"), 180)
    if reason:
        lines.append(f"原因：{reason}")
    next_retry_at = clean(result.get("next_retry_at"), 80)
    if next_retry_at:
        lines.append(f"重试：已排队，下一次不早于 {next_retry_at}")

rendered = "\n".join(lines).rstrip() + "\n"
output_path.parent.mkdir(parents=True, exist_ok=True)
tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
tmp_path.write_text(rendered, encoding="utf-8")
tmp_path.replace(output_path)
PY
}

finish_with_result() {
  local exit_code="${1:-0}"
  local event_name="${2:-result}"
  local message_format="${3:-text}"
  local idempotency_seed="${4:-}"
  if [[ -f "$RESULT_MD_PATH" ]]; then
    case "$event_name" in
      waiting-*|backoff-*|busy|dry-run|preflight-success|no-new|blocked-release)
        ;;
      *)
        render_compact_terminal_result
        send_chat_message "$(cat "$RESULT_MD_PATH")" "$event_name" "markdown" "$idempotency_seed"
        ;;
    esac
    cat "$RESULT_MD_PATH"
  fi
  exit "$exit_code"
}

clear_failure_backoff_state() {
  rm -f "$FAILURE_STATE_PATH"
}

run_task_preflight() {
  set +e
  TEXT_EXTRACT_CACHE_DIR="$TEXT_CACHE_DIR_PATH" \
  "$PYTHON_BIN" "$EXTRACT_TEXT_SCRIPT_PATH" --preflight-only > "$PREFLIGHT_JSON_PATH" 2>>"$LOG_PATH"
  local extract_preflight_rc=$?
  local markitdown_preflight_rc=0
  if [[ -n "$MARKITDOWN_SCRIPT_PATH_RESOLVED" && -f "$MARKITDOWN_SCRIPT_PATH_RESOLVED" ]]; then
    "$PYTHON_BIN" "$MARKITDOWN_SCRIPT_PATH_RESOLVED" --preflight-only > "$MARKITDOWN_PREFLIGHT_JSON_PATH" 2>>"$LOG_PATH"
    markitdown_preflight_rc=$?
  else
    printf '{"ok": true, "checks": [{"name": "markitdown_script", "ok": false, "severity": "warning", "detail": "markitdown script not configured; existing extractor fallback remains available", "code": "markitdown_script_missing"}]}\n' > "$MARKITDOWN_PREFLIGHT_JSON_PATH"
    markitdown_preflight_rc=0
  fi
  set -e

  "$PYTHON_BIN" - "$PREFLIGHT_JSON_PATH" "$PYTHON_BIN" "$HELPER_PATH" "$SUMMARY_PROMPT_PATH" "$SUMMARY_SYSTEM_PROMPT_PATH" "$SCANNER_PATH" "$EXTRACT_TEXT_SCRIPT_PATH" "$TEXT_CACHE_DIR_PATH" "$SUMMARY_CACHE_DIR_PATH" "$extract_preflight_rc" "$MARKITDOWN_SCRIPT_PATH_RESOLVED" "$MARKITDOWN_PREFLIGHT_JSON_PATH" "$markitdown_preflight_rc" "$SUMMARY_AGENT_ID" "$SUMMARY_PARALLEL_ENABLED" "$SUMMARY_WORKER_COUNT" "$SUMMARY_WORKER_AGENT_ID_PREFIX" "${OPENCLAW_CONFIG_PATH:-$HOME/.openclaw/openclaw.json}" <<'PY'
import json
import shutil
import sys
from pathlib import Path

report_path = Path(sys.argv[1])
python_bin = sys.argv[2]
helper_path = Path(sys.argv[3])
summary_prompt_path = Path(sys.argv[4])
summary_system_prompt_path = Path(sys.argv[5])
scanner_path = Path(sys.argv[6])
extract_path = Path(sys.argv[7])
cache_dir = Path(sys.argv[8])
summary_cache_dir = Path(sys.argv[9])
extract_preflight_rc = int(sys.argv[10])
markitdown_script_raw = sys.argv[11].strip()
markitdown_script_path = Path(markitdown_script_raw) if markitdown_script_raw else None
markitdown_report_path = Path(sys.argv[12])
markitdown_preflight_rc = int(sys.argv[13])
summary_agent_id = sys.argv[14].strip()
summary_parallel_enabled = sys.argv[15].strip().lower() == "true"
try:
    summary_worker_count = int(sys.argv[16])
except Exception:
    summary_worker_count = 1
summary_worker_agent_prefix = sys.argv[17].strip()
openclaw_config_path = Path(sys.argv[18]).expanduser()

try:
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}
except Exception:
    report = {}

checks = list(report.get("checks") or [])

def append_check(name: str, ok: bool, severity: str, detail: str, code: str) -> None:
    checks.append(
        {
            "name": name,
            "ok": ok,
            "severity": severity,
            "detail": detail,
            "code": code,
        }
    )

if extract_preflight_rc != 0:
    append_check(
        "extract_preflight_process",
        False,
        "fatal",
        f"extractor preflight process exited with code {extract_preflight_rc}",
        "extract_preflight_process_failed",
    )

if markitdown_preflight_rc != 0:
    append_check(
        "markitdown_preflight_process",
        False,
        "fatal",
        f"MarkItDown preflight process exited with code {markitdown_preflight_rc}",
        "markitdown_preflight_process_failed",
    )

append_check(
    "python",
    shutil.which(python_bin) is not None,
    "fatal",
    f"{python_bin} available" if shutil.which(python_bin) is not None else f"{python_bin} command not found",
    "python_missing",
)
append_check(
    "openclaw",
    shutil.which("openclaw") is not None,
    "fatal",
    "openclaw available" if shutil.which("openclaw") is not None else "openclaw command not found",
    "openclaw_missing",
)
if summary_parallel_enabled and summary_worker_count > 1 and openclaw_config_path.exists():
    expected_summary_agents = [
        f"{summary_worker_agent_prefix}{worker_index}"
        for worker_index in range(1, summary_worker_count + 1)
    ]
    try:
        openclaw_config = json.loads(openclaw_config_path.read_text(encoding="utf-8"))
        agent_entries = openclaw_config.get("agents", {}).get("list", [])
        registered_agent_ids = {
            str(entry.get("id", "")).strip()
            for entry in agent_entries
            if isinstance(entry, dict)
        }
        missing_agent_ids = [
            agent_id
            for agent_id in expected_summary_agents
            if agent_id and agent_id not in registered_agent_ids
        ]
        append_check(
            "summary_agents_registered",
            not missing_agent_ids,
            "fatal",
            (
                "summary agent(s) registered: " + ", ".join(expected_summary_agents)
                if not missing_agent_ids
                else "summary worker agent(s) not registered in OpenClaw config: "
                + ", ".join(missing_agent_ids)
            ),
            "summary_agents_registered" if not missing_agent_ids else "summary_agent_unregistered",
        )
    except Exception as exc:
        append_check(
            "summary_agents_registered",
            False,
            "fatal",
            f"failed to read OpenClaw config for summary agent check: {openclaw_config_path}: {exc}",
            "summary_agent_registration_check_failed",
        )
elif summary_parallel_enabled and summary_worker_count > 1:
    append_check(
        "summary_agents_registered",
        True,
        "warning",
        f"OpenClaw config not found; skipped summary agent registration check: {openclaw_config_path}",
        "summary_agent_registration_check_skipped",
    )
else:
    append_check(
        "summary_agents_registered",
        True,
        "warning",
        f"single summary agent path uses {summary_agent_id}; worker registration check not required",
        "summary_agent_registration_not_required",
    )
append_check(
    "helper_script",
    helper_path.exists(),
    "fatal",
    f"helper script found: {helper_path}" if helper_path.exists() else f"helper script missing: {helper_path}",
    "helper_script_missing",
)
append_check(
    "summary_prompt",
    summary_prompt_path.exists(),
    "fatal",
    f"summary prompt found: {summary_prompt_path}" if summary_prompt_path.exists() else f"summary prompt missing: {summary_prompt_path}",
    "summary_prompt_missing",
)
append_check(
    "summary_system_prompt",
    summary_system_prompt_path.exists(),
    "fatal",
    (
        f"summary system prompt found: {summary_system_prompt_path}"
        if summary_system_prompt_path.exists()
        else f"summary system prompt missing: {summary_system_prompt_path}"
    ),
    "summary_system_prompt_missing",
)
append_check(
    "scanner_script",
    scanner_path.exists(),
    "fatal",
    f"scanner script found: {scanner_path}" if scanner_path.exists() else f"scanner script missing: {scanner_path}",
    "scanner_script_missing",
)
append_check(
    "extract_script",
    extract_path.exists(),
    "fatal",
    f"extract script found: {extract_path}" if extract_path.exists() else f"extract script missing: {extract_path}",
    "extract_script_missing",
)
if markitdown_script_path is not None:
    append_check(
        "markitdown_script",
        markitdown_script_path.exists(),
        "fatal",
        (
            f"markitdown script found: {markitdown_script_path}"
            if markitdown_script_path.exists()
            else f"markitdown script missing: {markitdown_script_path}"
        ),
        "markitdown_script_missing",
    )
try:
    markitdown_report = json.loads(markitdown_report_path.read_text(encoding="utf-8"))
except Exception:
    markitdown_report = {"ok": False, "checks": []}
for check in markitdown_report.get("checks", []):
    if not isinstance(check, dict):
        continue
    severity = str(check.get("severity", "") or ("fatal" if markitdown_script_path is not None else "warning"))
    append_check(
        str(check.get("name", "markitdown")),
        bool(check.get("ok", False)),
        severity,
        str(check.get("detail", "")),
        str(check.get("code", "markitdown_preflight_failed")),
    )
try:
    cache_dir.mkdir(parents=True, exist_ok=True)
    writable = cache_dir.is_dir()
except Exception:
    writable = False
append_check(
    "text_cache_dir",
    writable,
    "fatal",
    f"text cache dir ready: {cache_dir}" if writable else f"text cache dir not writable: {cache_dir}",
    "text_cache_dir_unwritable",
)

try:
    summary_cache_dir.mkdir(parents=True, exist_ok=True)
    summary_writable = summary_cache_dir.is_dir()
except Exception:
    summary_writable = False
append_check(
    "summary_cache_dir",
    summary_writable,
    "fatal",
    f"summary cache dir ready: {summary_cache_dir}" if summary_writable else f"summary cache dir not writable: {summary_cache_dir}",
    "summary_cache_dir_unwritable",
)

fatal_failures = [check for check in checks if not bool(check.get("ok")) and check.get("severity") == "fatal"]
warnings = [check for check in checks if not bool(check.get("ok")) and check.get("severity") == "warning"]
report["checks"] = checks
report["fatal_failures"] = fatal_failures
report["warnings"] = warnings
report["ok"] = (extract_preflight_rc == 0) and (markitdown_preflight_rc == 0) and not fatal_failures
report["text_cache_dir"] = str(cache_dir)
report["summary_cache_dir"] = str(summary_cache_dir)
report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("true" if report["ok"] else "false")
PY
}

summarize_preflight_report() {
  "$PYTHON_BIN" - "$PREFLIGHT_JSON_PATH" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    print("preflight report missing")
    raise SystemExit(0)

try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    print("preflight report unreadable")
    raise SystemExit(0)

fatal = data.get("fatal_failures") or []
warnings = data.get("warnings") or []
if fatal:
    parts = [str(item.get("detail") or item.get("code") or "").strip() for item in fatal[:3]]
    print("；".join(part for part in parts if part) or "preflight failed")
    raise SystemExit(0)

if not bool(data.get("ok", False)):
    print("preflight failed without a structured failure item")
    raise SystemExit(0)

if warnings:
    parts = [str(item.get("detail") or item.get("code") or "").strip() for item in warnings[:2]]
    print("预检通过，但有告警：" + "；".join(part for part in parts if part))
    raise SystemExit(0)

print("ok")
PY
}

quarantine_chunk_failures() {
  local chunk_json_path="$1"
  "$PYTHON_BIN" "$HELPER_PATH" update-quarantine \
    --batch-file "$chunk_json_path" \
    --quarantine-file "$QUARANTINE_JSON_PATH" \
    --run-at "$RUN_AT" >> "$LOG_PATH"
  refresh_quarantine_report
}

clear_quarantine_entries_for_batch() {
  local batch_path="$1"
  "$PYTHON_BIN" "$HELPER_PATH" clear-quarantine \
    --batch-file "$batch_path" \
    --quarantine-file "$QUARANTINE_JSON_PATH" \
    --run-at "$RUN_AT" >> "$LOG_PATH"
  refresh_quarantine_report
}

refresh_quarantine_report() {
  "$PYTHON_BIN" "$HELPER_PATH" inspect-quarantine \
    --quarantine-file "$QUARANTINE_JSON_PATH" \
    --output "$QUARANTINE_REPORT_PATH" >> "$LOG_PATH" || true
}

workflow_retry_version() {
  local manifest_json
  manifest_json="$(build_workflow_fingerprint_manifest_json)"
  "$PYTHON_BIN" - "$manifest_json" <<'PY'
import hashlib
import json
import sys

raw = str(sys.argv[1] or "").strip()
try:
    payload = json.loads(raw)
    records = payload.get("records", []) if isinstance(payload, dict) else []
    stable = [
        {
            "label": str(item.get("label", "")),
            "exists": bool(item.get("exists", False)),
            "sha256": str(item.get("sha256", "")),
        }
        for item in records
        if isinstance(item, dict) and str(item.get("label", "")).strip()
    ]
    normalized = json.dumps(sorted(stable, key=lambda item: item["label"]), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
except Exception:
    normalized = raw
print(hashlib.sha256(normalized.encode("utf-8")).hexdigest())
PY
}

record_stage_retry_for_batch() {
  local batch_path="$1"
  local stage="$2"
  local error_code="${3:-}"
  local error_type="${4:-}"
  local retryable="${5:-}"
  local message="${6:-}"
  local -a args=(
    record-stage-retry
    --batch-file "$batch_path"
    --ledger-file "$STAGE_RETRY_LEDGER_PATH"
    --stage "$stage"
    --run-at "$RUN_AT"
    --workflow-version "$WORKFLOW_RETRY_VERSION"
    --max-attempts "$PER_FILE_RETRY_MAX_ATTEMPTS"
    --retry-delays-minutes "$PER_FILE_RETRY_DELAYS_MINUTES"
  )
  [[ -n "$error_code" ]] && args+=(--error-code "$error_code")
  [[ -n "$error_type" ]] && args+=(--error-type "$error_type")
  [[ -n "$retryable" ]] && args+=(--retryable "$retryable")
  [[ -n "$message" ]] && args+=(--message "$message")
  "$PYTHON_BIN" "$HELPER_PATH" "${args[@]}" >> "$LOG_PATH" 2>&1 || true
}

resolve_stage_retry_for_batch() {
  local batch_path="$1"
  local stage="$2"
  "$PYTHON_BIN" "$HELPER_PATH" resolve-stage-retry \
    --batch-file "$batch_path" \
    --ledger-file "$STAGE_RETRY_LEDGER_PATH" \
    --stage "$stage" \
    --run-at "$RUN_AT" \
    --workflow-version "$WORKFLOW_RETRY_VERSION" >> "$LOG_PATH" 2>&1 || true
}

is_release_contract_mismatch() {
  local raw="${1:-}"
  local lowered=""
  lowered="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]')"
  case "$lowered" in
    *"unrecognized arguments"*|*"helper contract"*|*"contract-version"*|*"contract version"*|*"schema version"*|*"release_contract_mismatch"*)
      return 0
      ;;
    *"lookup-publish-recovery"*)
      if [[ "$lowered" == *"invalid choice"* || "$lowered" == *"required arguments"* || "$lowered" == *"missing"* ]]; then
        return 0
      fi
      ;;
  esac
  return 1
}

finish_blocked_release() {
  local batch_path="$1"
  local new_pdf_count="$2"
  local raw_error="$3"
  local record_retry="${4:-true}"
  local error_summary=""
  error_summary="$(printf '%s' "$raw_error" | tr '\n' ' ' | sed 's/[[:space:]][[:space:]]*/ /g' | cut -c1-600)"
  if [[ -z "$error_summary" ]]; then
    error_summary="release contract mismatch"
  fi

  if [[ -z "${WORKFLOW_RETRY_VERSION:-}" ]]; then
    WORKFLOW_RETRY_VERSION="$(workflow_retry_version)"
  fi
  if [[ "$record_retry" == "true" && "$new_pdf_count" =~ ^[0-9]+$ && "$new_pdf_count" -gt 0 && -f "$batch_path" ]]; then
    record_stage_retry_for_batch \
      "$batch_path" \
      "publish" \
      "release_contract_mismatch" \
      "release_contract_mismatch" \
      "false" \
      "$error_summary"
  fi
  clear_failure_backoff_state
  {
    printf '知识星球研报总结：⛔ 发布版本已阻塞\n'
    printf '执行时间：%s\n' "$DISPLAY_RUN_AT"
    printf '状态：blocked\n'
    printf '结果：检测到 release helper / worker 合同不兼容，已停止本轮，不会自动重试\n'
    printf '失败原因：%s\n' "$error_summary"
    printf '待处理：%s 篇\n' "$new_pdf_count"
    printf '日志位置：%s\n' "$LOG_PATH"
  } > "$RESULT_MD_PATH"
  write_result_json "blocked" "$error_summary" "$new_pdf_count" "" "1" "$batch_path" "[]" "blocked_release" "blocked_release"
  complete_run_status "blocked" "blocked_release" "release helper contract mismatch" "1" "blocked_release" "$batch_path" "" "" "" "" "" "$new_pdf_count" "$new_pdf_count"
  finish_with_result 1 "blocked-release" "text" "$batch_path"
}

build_workflow_fingerprint_manifest_json() {
  local manifest_path="${ZSXQ_WORKFLOW_FINGERPRINT_MANIFEST_PATH:-}"
  if [[ -n "$manifest_path" && -f "$manifest_path" ]]; then
    cat "$manifest_path"
    return 0
  fi

  local -a auth_fingerprint_args=()
  local auth_fingerprint_arg
  while IFS= read -r auth_fingerprint_arg; do
    auth_fingerprint_args+=("$auth_fingerprint_arg")
  done < <(summary_auth_fingerprint_args)

  "$PYTHON_BIN" - \
    "config" "$CONFIG_PATH" \
    "worker" "$TASK_SCRIPT_REALPATH" \
    "helper" "$HELPER_SOURCE_PATH" \
    "scanner" "$SCANNER_SOURCE_PATH" \
    "summary_prompt" "$SUMMARY_PROMPT_SOURCE_PATH" \
    "summary_system_prompt" "$SUMMARY_SYSTEM_PROMPT_SOURCE_PATH" \
    "extract_text" "$EXTRACT_TEXT_SOURCE_PATH" \
    "openclaw_config" "${HOME}/.openclaw/openclaw.json" \
    "device_auth" "${HOME}/.openclaw/identity/device-auth.json" \
    "main_agent_auth" "${HOME}/.openclaw/agents/main/agent/auth-profiles.json" \
    "${auth_fingerprint_args[@]}" <<'PY'
import hashlib
import json
import sys
import time
from pathlib import Path

args = sys.argv[1:]
records = []
now_ms = int(time.time() * 1000)


def auth_state_sha256(label: str, path: Path) -> str | None:
    if label == "device_auth":
        return ""
    if not label.endswith("_agent_auth"):
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    profiles = payload.get("profiles") if isinstance(payload, dict) else {}
    states = []
    if isinstance(profiles, dict):
        for name, profile in sorted(profiles.items()):
            if not str(name).startswith("openai-codex:") or not isinstance(profile, dict):
                continue
            expires = profile.get("expires")
            if isinstance(expires, (int, float)):
                state = "valid" if int(expires) > now_ms else "expired"
            else:
                state = "unknown"
            states.append({"profile": str(name), "state": state})
    raw = json.dumps(states, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

for index in range(0, len(args), 2):
    label = str(args[index] or "").strip()
    raw_path = str(args[index + 1] or "").strip() if index + 1 < len(args) else ""
    if not label or not raw_path:
        continue
    path = Path(raw_path).expanduser().resolve(strict=False)
    exists = path.exists()
    sha256 = ""
    if exists and path.is_file():
      auth_digest = auth_state_sha256(label, path)
      if auth_digest is not None:
        sha256 = auth_digest
      else:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
          for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
        sha256 = digest.hexdigest()
    records.append(
        {
            "label": label,
            "path": str(path),
            "exists": exists,
            "sha256": sha256,
        }
    )

print(json.dumps({"records": records}, ensure_ascii=False))
PY
}

reset_agent_session() {
  local sessions_dir="$1"
  local agent_id="$2"
  python3 - "$sessions_dir" "$agent_id" <<'PY'
import json
import sys
from pathlib import Path

sessions_dir = Path(sys.argv[1]).expanduser()
agent_id = sys.argv[2].strip()
session_key = f"agent:{agent_id}:main"
store_path = sessions_dir / "sessions.json"
payload = {
    "reset": False,
    "session_key": session_key,
    "session_id": "",
}

if store_path.exists():
    data = json.loads(store_path.read_text(encoding="utf-8"))
    entry = data.pop(session_key, None)
    if entry:
        payload["reset"] = True
        payload["session_id"] = str(entry.get("sessionId") or "").strip()
        store_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

session_id = payload["session_id"]
if session_id:
    for suffix in (".jsonl", ".json"):
        session_path = sessions_dir / f"{session_id}{suffix}"
        if session_path.exists():
            session_path.unlink()

print(json.dumps(payload, ensure_ascii=False))
PY
}

prepare_fresh_agent_session() {
  local stage_label="$1"
  local chunk_index="$2"
  local chunk_total="$3"
  local sessions_dir="$4"
  local agent_id="$5"
  local reset_output=""
  reset_output="$(reset_agent_session "$sessions_dir" "$agent_id")"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${chunk_total} 批${stage_label}前已重置专用 agent 会话（agent=${agent_id}）：${reset_output}" >> "$LOG_PATH"
}

cleanup_agent_session_after_call() {
  local sessions_dir="$1"
  local agent_id="$2"
  reset_agent_session "$sessions_dir" "$agent_id" >/dev/null 2>&1 || true
}

sync_openai_codex_auth_from_main() {
  if [[ "${SYNC_MAIN_OPENAI_CODEX_AUTH:-true}" != "true" ]]; then
    return 0
  fi

  local main_auth_path="${OPENCLAW_MAIN_AGENT_AUTH_PATH:-$HOME/.openclaw/agents/main/agent/auth-profiles.json}"
  local -a target_auth_paths=()
  local target_auth_path
  while IFS= read -r target_auth_path; do
    target_auth_paths+=("$target_auth_path")
  done < <(summary_auth_target_paths)

  "$PYTHON_BIN" - "$main_auth_path" "${target_auth_paths[@]}" <<'PY' >> "$LOG_PATH" 2>&1 || true
import json
import sys
from copy import deepcopy
from pathlib import Path


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


main_path = Path(sys.argv[1]).expanduser()
target_paths = []
seen = set()
for raw_path in sys.argv[2:]:
    path = Path(raw_path).expanduser()
    resolved = str(path.resolve(strict=False))
    if resolved == str(main_path.resolve(strict=False)) or resolved in seen:
        continue
    seen.add(resolved)
    target_paths.append(path)

main_payload = load_json(main_path)
main_profiles = main_payload.get("profiles")
if not isinstance(main_profiles, dict):
    print(f"[auth-sync] skipped: main auth profiles missing at {main_path}")
    raise SystemExit(0)

openai_profiles = {
    str(name): deepcopy(profile)
    for name, profile in main_profiles.items()
    if str(name).startswith("openai-codex:") and isinstance(profile, dict)
}
if not openai_profiles:
    print(f"[auth-sync] skipped: main has no openai-codex profiles at {main_path}")
    raise SystemExit(0)

synced = []
unchanged = []
for target_path in target_paths:
    target_payload = load_json(target_path)
    if "profiles" not in target_payload or not isinstance(target_payload.get("profiles"), dict):
        target_payload["profiles"] = {}
    profiles = target_payload["profiles"]
    before = json.dumps(target_payload, ensure_ascii=False, sort_keys=True)
    for name in list(profiles.keys()):
        if str(name).startswith("openai-codex:"):
            profiles.pop(name, None)
    profiles.update(deepcopy(openai_profiles))
    after = json.dumps(target_payload, ensure_ascii=False, sort_keys=True)
    if before == after:
        unchanged.append(str(target_path))
        continue
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(json.dumps(target_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    synced.append(str(target_path))

if synced:
    print(f"[auth-sync] synced openai-codex auth from main to {len(synced)} agent file(s)")
if unchanged:
    print(f"[auth-sync] openai-codex auth already current for {len(unchanged)} agent file(s)")
PY
}

trap 'cleanup_and_finalize $?' EXIT

write_run_status() {
  local status="$1"
  local phase="$2"
  local message="$3"
  local exit_code="${4:-}"
  local operational_state="${5:-}"
  local batch_source_path="${6:-$BATCH_JSON_PATH}"
  local chunk_index="${7:-}"
  local chunk_total="${8:-}"
  local current_file="${9:-}"
  local waiting_reason="${10:-}"
  local next_retry_at="${11:-}"
  local new_pdf_count="${12:-${NEW_PDF_COUNT:-}}"
  local remaining_count="${13:-}"
  local active_workers_dir="${ACTIVE_WORKERS_DIR:-}"
  local summary_ready_count="${SUMMARY_READY_FILE_COUNT:-0}"
  local published_count="${PROCESSED_FILE_COUNT:-0}"
  local quarantined_count="${QUARANTINED_FILE_COUNT:-0}"
  local summary_only_count=$((summary_ready_count - published_count))
  if [[ "$summary_only_count" -lt 0 ]]; then
    summary_only_count=0
  fi

  RUN_STATUS_LAST_PHASE="$phase"
  RUN_STATUS_LAST_MESSAGE="$message"
  RUN_STATUS_LAST_OPERATIONAL_STATE="${operational_state:-$status}"

  python3 - "$RUN_STATUS_JSON_PATH" "$status" "$phase" "$message" "$RUN_AT" "$LOG_PATH" "$RESULT_JSON_PATH" "$RESULT_MD_PATH" "$USAGE_JSON_PATH" "$batch_source_path" "$PREFLIGHT_JSON_PATH" "$QUARANTINE_JSON_PATH" "$QUARANTINE_REPORT_PATH" "$MANUAL_MODE" "$summary_ready_count" "$published_count" "$summary_only_count" "$quarantined_count" "$exit_code" "$operational_state" "$chunk_index" "$chunk_total" "$current_file" "$waiting_reason" "$next_retry_at" "$new_pdf_count" "$remaining_count" "$active_workers_dir" "$RUN_ID" "${DISCOVERED_PDF_COUNT:-}" "${DEFERRED_RETRY_FILE_COUNT:-0}" <<'PY'
import json
import sys
from datetime import datetime
from pathlib import Path


def parse_int(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except Exception:
        return None


status_path = Path(sys.argv[1])
status = sys.argv[2]
phase = sys.argv[3]
message = sys.argv[4]
run_started_at = sys.argv[5]
cron_log_path = sys.argv[6]
result_json_path = sys.argv[7]
result_md_path = sys.argv[8]
usage_json_path = sys.argv[9]
batch_json_path = sys.argv[10]
preflight_json_path = sys.argv[11]
quarantine_json_path = sys.argv[12]
quarantine_report_path = sys.argv[13]
manual_mode = sys.argv[14].lower() == "true"
summary_ready_count = parse_int(sys.argv[15]) or 0
published_count = parse_int(sys.argv[16]) or 0
summary_only_count = parse_int(sys.argv[17]) or 0
quarantined_count = parse_int(sys.argv[18]) or 0
exit_code_raw = str(sys.argv[19]).strip()
operational_state = str(sys.argv[20]).strip()
chunk_index = parse_int(sys.argv[21])
chunk_total = parse_int(sys.argv[22])
current_file = str(sys.argv[23]).strip()
waiting_reason = str(sys.argv[24]).strip()
next_retry_at = str(sys.argv[25]).strip()
new_pdf_count = parse_int(sys.argv[26])
remaining_count = parse_int(sys.argv[27])
active_workers_dir = str(sys.argv[28]).strip() if len(sys.argv) > 28 else ""
run_id = str(sys.argv[29]).strip() if len(sys.argv) > 29 else ""
discovered_pdf_count = parse_int(sys.argv[30]) if len(sys.argv) > 30 else None
deferred_retry_count = parse_int(sys.argv[31]) if len(sys.argv) > 31 else 0


def state_semantics(raw_status: str, raw_operational_state: str) -> tuple[str, str]:
    if raw_status == "running":
        return "running", "healthy"
    if raw_status == "waiting":
        return "waiting", "healthy"
    if raw_status == "blocked" or raw_operational_state == "blocked_release":
        return "blocked", "blocked"
    if raw_status in {"failed", "env_failed"}:
        return "failed", "blocked"
    if raw_status == "partial_success":
        return "partial_success", "blocked" if raw_operational_state in {"env_failed", "ack_failed", "preflight_failed"} else "degraded"
    if raw_operational_state == "completed_with_quarantine":
        return "completed_with_quarantine", "degraded"
    if raw_status == "paused":
        return "waiting", "degraded"
    if raw_operational_state in {"idle_no_new_pdf", "preflight_only", "dry_run"}:
        return "noop", "healthy"
    if raw_status == "success":
        return "success", "healthy"
    return raw_status or "unknown", "degraded"

payload = {}
if status_path.exists():
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}

payload["status"] = status
payload["run_id"] = run_id or None
payload["discovered_pdf_count"] = discovered_pdf_count
payload["deferred_retry_count"] = deferred_retry_count or 0
payload["phase"] = phase
payload["message"] = message
payload["run_started_at"] = str(run_started_at).strip()
payload["last_heartbeat_at"] = datetime.now().astimezone().isoformat()
payload["cron_log_path"] = cron_log_path
payload["result_json_path"] = result_json_path
payload["result_md_path"] = result_md_path
payload["usage_json_path"] = usage_json_path
payload["preflight_json_path"] = preflight_json_path
payload["quarantine_json_path"] = quarantine_json_path
payload["quarantine_report_path"] = quarantine_report_path
payload["manual_mode"] = manual_mode
payload["operational_state"] = operational_state or None
payload["run_outcome"], payload["pipeline_health"] = state_semantics(status, operational_state)
payload["batch_json_path"] = batch_json_path or None
payload["summary_ready_count"] = summary_ready_count
payload["published_count"] = published_count
payload["summary_only_count"] = summary_only_count
payload["quarantined_count"] = quarantined_count
payload["current_chunk_index"] = chunk_index
payload["current_chunk_total"] = chunk_total
payload["current_file"] = current_file or None
payload["waiting_reason"] = waiting_reason or None
payload["next_retry_at"] = next_retry_at or None
if new_pdf_count is not None:
    payload["new_pdf_count"] = new_pdf_count
else:
    payload["new_pdf_count"] = None
if remaining_count is not None:
    payload["remaining_count"] = remaining_count
else:
    payload["remaining_count"] = None
if exit_code_raw:
    try:
        payload["exit_code"] = int(exit_code_raw)
    except Exception:
        payload["exit_code"] = exit_code_raw
else:
    payload.pop("exit_code", None)

if active_workers_dir:
    workers = []
    directory = Path(active_workers_dir)
    if directory.exists():
        for status_file in sorted(directory.glob("worker-*.json")):
            try:
                worker = json.loads(status_file.read_text(encoding="utf-8"))
            except Exception:
                continue
            if isinstance(worker, dict):
                workers.append(worker)
    payload["active_workers"] = workers
else:
    payload.pop("active_workers", None)

status_path.parent.mkdir(parents=True, exist_ok=True)
tmp_path = status_path.with_suffix(status_path.suffix + ".tmp")
tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp_path.replace(status_path)
PY
}

complete_run_status() {
  write_run_status "$@"
  RUN_STATUS_FINALIZED="true"
}

write_worker_status() {
  local worker_id="$1"
  local agent_id="$2"
  local phase="$3"
  local chunk_json_path="$4"
  local chunk_index="$5"
  local chunk_total="$6"
  local current_file="$7"
  local message="${8:-}"

  if [[ -z "${ACTIVE_WORKERS_DIR:-}" ]]; then
    return 0
  fi

  "$PYTHON_BIN" - "$ACTIVE_WORKERS_DIR" "$worker_id" "$agent_id" "$phase" "$chunk_json_path" "$chunk_index" "$chunk_total" "$current_file" "$message" <<'PY'
import json
import sys
from datetime import datetime
from pathlib import Path


def parse_int(value: str) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except Exception:
        return None


directory = Path(sys.argv[1])
directory.mkdir(parents=True, exist_ok=True)
worker_id = str(sys.argv[2]).strip()
payload = {
    "worker_id": worker_id,
    "agent_id": str(sys.argv[3]).strip(),
    "phase": str(sys.argv[4]).strip(),
    "chunk_json_path": str(sys.argv[5]).strip() or None,
    "chunk_index": parse_int(sys.argv[6]),
    "chunk_total": parse_int(sys.argv[7]),
    "current_file": str(sys.argv[8]).strip() or None,
    "message": str(sys.argv[9]).strip() or None,
    "last_heartbeat_at": datetime.now().astimezone().isoformat(),
}
target = directory / f"worker-{worker_id}.json"
tmp = target.with_suffix(".json.tmp")
tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp.replace(target)
PY
}

clear_worker_status() {
  local worker_id="$1"
  if [[ -z "${ACTIVE_WORKERS_DIR:-}" ]]; then
    return 0
  fi
  rm -f "$ACTIVE_WORKERS_DIR/worker-${worker_id}.json"
}

start_heartbeat() {
  stop_heartbeat
  local heartbeat_args=("$@")
  (
    heartbeat_sleep_pid=""
    heartbeat_shutdown() {
      # A background shell does not automatically forward TERM to its sleep
      # child.  Kill and reap that child explicitly so a completed run does
      # not leave inherited stdout/stderr descriptors open.
      trap - TERM INT EXIT
      if [[ -n "${heartbeat_sleep_pid:-}" ]]; then
        kill "$heartbeat_sleep_pid" 2>/dev/null || true
        wait "$heartbeat_sleep_pid" 2>/dev/null || true
      fi
      exit 0
    }
    trap heartbeat_shutdown TERM INT EXIT
    while true; do
      sleep 15 &
      heartbeat_sleep_pid=$!
      wait "$heartbeat_sleep_pid" 2>/dev/null || true
      heartbeat_sleep_pid=""
      write_run_status "${heartbeat_args[@]}" || true
    done
  ) &
  HEARTBEAT_PID=$!
}

stop_heartbeat() {
  if [[ -n "${HEARTBEAT_PID:-}" ]]; then
    local heartbeat_pid="$HEARTBEAT_PID"
    kill "$heartbeat_pid" 2>/dev/null || true
    wait "$heartbeat_pid" 2>/dev/null || true
    HEARTBEAT_PID=""
  fi
}

write_result_json() {
  local status="$1"
  local message="$2"
  local new_pdf_count="$3"
  local doc_url="${4:-}"
  local exit_code="${5:-0}"
  local batch_source_path="${6:-$BATCH_JSON_PATH}"
  local doc_urls_json="${7:-[]}"
  local operational_state="${8:-$status}"
  local phase="${9:-}"
  local waiting_reason="${10:-}"
  local next_retry_at="${11:-}"
  local summary_ready_count="${SUMMARY_READY_FILE_COUNT:-0}"
  local published_count="${PROCESSED_FILE_COUNT:-0}"
  local quarantined_count="${QUARANTINED_FILE_COUNT:-0}"
  local summary_only_count=$((summary_ready_count - published_count))
  if [[ "$summary_only_count" -lt 0 ]]; then
    summary_only_count=0
  fi
  python3 - "$RESULT_JSON_PATH" "$RUN_AT" "$status" "$message" "$new_pdf_count" "$doc_url" "$exit_code" "$LOG_PATH" "$batch_source_path" "$RESULT_MD_PATH" "$MANUAL_MODE" "$USAGE_JSON_PATH" "$doc_urls_json" "$summary_ready_count" "$published_count" "$summary_only_count" "$operational_state" "$phase" "$RUN_STATUS_JSON_PATH" "$waiting_reason" "$next_retry_at" "$RUN_ID" "$quarantined_count" "${DISCOVERED_PDF_COUNT:-}" "${DEFERRED_RETRY_FILE_COUNT:-0}" <<'PY'
import json
import sys
from pathlib import Path

result_path = Path(sys.argv[1])
batch_path = Path(sys.argv[9])
batch_files = []
doc_urls = []
if batch_path.exists():
    try:
        batch_data = json.loads(batch_path.read_text(encoding="utf-8"))
        for item in batch_data.get("files", []):
            name = str(item.get("filename", "")).strip()
            if name:
                batch_files.append(name)
    except Exception:
        batch_files = []

try:
    parsed_doc_urls = json.loads(sys.argv[13])
    if isinstance(parsed_doc_urls, list):
        for item in parsed_doc_urls:
            value = str(item or "").strip()
            if value and value not in doc_urls:
                doc_urls.append(value)
except Exception:
    doc_urls = []

doc_url = sys.argv[6] or None
if doc_url and doc_url not in doc_urls:
    doc_urls.append(doc_url)

status = sys.argv[3]
operational_state = sys.argv[17]
summary_ready_count = int(sys.argv[14])
published_count = int(sys.argv[15])
quarantined_count = int(sys.argv[23])
discovered_pdf_count = int(sys.argv[24]) if str(sys.argv[24]).strip() else None
deferred_retry_count = int(sys.argv[25]) if str(sys.argv[25]).strip() else 0
if status == "partial_success" and summary_ready_count == 0 and published_count == 0 and quarantined_count == 0:
    status = "failed"

if status == "blocked" or operational_state == "blocked_release":
    run_outcome, pipeline_health = "blocked", "blocked"
elif status in {"failed", "env_failed"}:
    run_outcome, pipeline_health = "failed", "blocked"
elif status == "partial_success":
    run_outcome = "partial_success"
    pipeline_health = "blocked" if operational_state in {"env_failed", "ack_failed", "preflight_failed"} else "degraded"
elif operational_state == "completed_with_quarantine":
    run_outcome, pipeline_health = "completed_with_quarantine", "degraded"
elif status == "waiting":
    run_outcome, pipeline_health = "waiting", "healthy"
elif status == "paused":
    run_outcome, pipeline_health = "waiting", "degraded"
elif operational_state in {"idle_no_new_pdf", "preflight_only", "dry_run"}:
    run_outcome, pipeline_health = "noop", "healthy"
else:
    run_outcome, pipeline_health = "success", "healthy"

payload = {
    "execute_time": sys.argv[2],
    "run_id": sys.argv[22] or None,
    "status": status,
    "run_outcome": run_outcome,
    "pipeline_health": pipeline_health,
    "message": sys.argv[4],
    "new_pdf_count": int(sys.argv[5]),
    "discovered_pdf_count": discovered_pdf_count,
    "deferred_retry_count": deferred_retry_count,
    "files": batch_files,
    "doc_url": doc_url,
    "doc_urls": doc_urls,
    "exit_code": int(sys.argv[7]),
    "log_path": sys.argv[8],
    "batch_json_path": sys.argv[9],
    "result_md_path": sys.argv[10],
    "manual_mode": sys.argv[11].lower() == "true",
    "usage_json_path": sys.argv[12] or None,
    "summary_ready_count": summary_ready_count,
    "published_count": published_count,
    "quarantined_count": quarantined_count,
    "summary_only_count": int(sys.argv[16]),
    "operational_state": sys.argv[17] or None,
    "phase": sys.argv[18] or None,
    "run_status_json_path": sys.argv[19] or None,
    "waiting_reason": sys.argv[20] or None,
    "next_retry_at": sys.argv[21] or None,
    "status_from_exit_code": "success" if int(sys.argv[7]) == 0 else "failed",
}
result_path.parent.mkdir(parents=True, exist_ok=True)
tmp_path = result_path.with_suffix(result_path.suffix + ".tmp")
tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp_path.replace(result_path)
PY
}

write_usage_summary() {
  local output_path="$1"
  shift || true
  python3 - "$output_path" "$@" <<'PY'
import json
import sys
from pathlib import Path

output_path = Path(sys.argv[1])
chunk_paths = [Path(p) for p in sys.argv[2:] if p.strip()]
chunks = []
totals = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}
agent_ids = []

for chunk_path in chunk_paths:
    if not chunk_path.exists():
        continue
    data = json.loads(chunk_path.read_text(encoding="utf-8"))
    usage = data.get("usage") or {}
    agent_id = str(data.get("agent_id") or "").strip()
    chunk = {
        "chunk_file": data.get("chunk_file"),
        "chunk_index": data.get("chunk_index"),
        "agent_id": agent_id,
        "doc_url": data.get("doc_url"),
        "text_chars": int(data.get("text_chars") or 0),
        "prompt_tokens": int(data.get("prompt_tokens") or 0),
        "usage": {
            "input": int(usage.get("input") or 0),
            "output": int(usage.get("output") or 0),
            "cacheRead": int(usage.get("cacheRead") or 0),
            "cacheWrite": int(usage.get("cacheWrite") or 0),
            "total": int(usage.get("total") or 0),
        },
        "system_prompt_chars": int(data.get("system_prompt_chars") or 0),
        "skills_prompt_chars": int(data.get("skills_prompt_chars") or 0),
        "tools_list_chars": int(data.get("tools_list_chars") or 0),
        "tools_schema_chars": int(data.get("tools_schema_chars") or 0),
        "workspace_dir": data.get("workspace_dir"),
        "model": data.get("model"),
        "provider": data.get("provider"),
        "session_id": data.get("session_id"),
    }
    chunks.append(chunk)
    if agent_id and agent_id not in agent_ids:
        agent_ids.append(agent_id)
    for key in totals:
        totals[key] += chunk["usage"][key]

payload = {
    "agent_id": agent_ids[0] if len(agent_ids) == 1 else "",
    "agent_ids": agent_ids,
    "chunk_count": len(chunks),
    "totals": totals,
    "chunks": chunks,
}
output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

extract_batch_count() {
  local batch_path="${1:-$BATCH_JSON_PATH}"
  python3 - "$batch_path" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    print(0)
    raise SystemExit(0)

data = json.loads(path.read_text(encoding="utf-8"))
print(int(data.get("new_pdf_count", 0)))
PY
}

extract_first_filename() {
  local batch_path="${1:-$BATCH_JSON_PATH}"
  python3 - "$batch_path" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(0)

data = json.loads(path.read_text(encoding="utf-8"))
files = data.get("files", [])
if files:
    print(str(files[0].get("filename", "")).strip())
PY
}

extract_file_lines() {
  local batch_path="${1:-$BATCH_JSON_PATH}"
  python3 - "$batch_path" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(0)

data = json.loads(path.read_text(encoding="utf-8"))
for item in data.get("files", []):
    name = str(item.get("filename", "")).strip()
    if name:
        print(f"- {name}")
PY
}

extract_text_diagnostics_lines() {
  local batch_path="$1"
  python3 - "$batch_path" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(0)

data = json.loads(path.read_text(encoding="utf-8"))
for item in data.get("files", []):
    name = str(item.get("filename", "")).strip() or "未命名文件"
    source = str(item.get("text_source", "")).strip()
    chars = int(item.get("extracted_text_chars", 0) or 0)
    error = str(item.get("text_extract_error", "")).strip()
    warning = str(item.get("text_extract_warning", "")).strip()
    if not any([source, chars, error, warning]):
        continue
    print(f"- {name}")
    if source:
        print(f"  source: {source}")
    if chars:
        print(f"  chars: {chars}")
    if error:
        print(f"  error: {error}")
    if warning:
        print(f"  warning: {warning}")
PY
}

extract_doc_url_from_file() {
  local result_path="$1"
  python3 - "$result_path" <<'PY'
import re
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(0)

text = path.read_text(encoding="utf-8", errors="replace")
match = re.search(r"https?://[^\s)]+feishu\.cn/[^\s)]+", text)
if not match:
    match = re.search(r"https?://[^\s)]+", text)
if match:
    print(match.group(0))
PY
}

build_ack_batch_from_chunks() {
  local output_path="$1"
  shift
  python3 - "$output_path" "$@" <<'PY'
import json
import sys
from pathlib import Path

output = Path(sys.argv[1])
chunk_paths = [Path(p) for p in sys.argv[2:] if p.strip()]
if not chunk_paths:
    raise SystemExit(1)

base = json.loads(chunk_paths[0].read_text(encoding="utf-8"))
files = []
for chunk_path in chunk_paths:
    data = json.loads(chunk_path.read_text(encoding="utf-8"))
    files.extend(data.get("files", []))

base["files"] = files
base["new_pdf_count"] = len(files)
output.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(str(output))
PY
}

get_failure_backoff_status() {
  local batch_path="$1"
  local workflow_manifest_json=""
  workflow_manifest_json="$(build_workflow_fingerprint_manifest_json)"
  "$PYTHON_BIN" - "$FAILURE_STATE_PATH" "$batch_path" "$RUN_AT" "$workflow_manifest_json" <<'PY'
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def parse_workflow_records(raw_text: str) -> list[dict[str, object]]:
    text = str(raw_text or "").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except Exception:
        return []
    records = payload.get("records") if isinstance(payload, dict) else payload
    normalized = []
    if not isinstance(records, list):
        return normalized
    for item in records:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "")).strip()
        if not path:
            continue
        normalized.append(
            {
                "label": str(item.get("label", "")).strip(),
                "path": path,
                "exists": bool(item.get("exists", False)),
                "sha256": str(item.get("sha256", "")).strip(),
            }
        )
    return normalized


def compute_batch_key(batch_path: Path, workflow_records: list[dict[str, object]]) -> str:
    if not batch_path.exists():
        return ""
    try:
        data = json.loads(batch_path.read_text(encoding="utf-8"))
    except Exception:
        return ""

    files = []
    for item in data.get("files", []):
        path = str(item.get("path", "")).strip()
        if not path:
            continue
        files.append(
            {
                "path": path,
                "size_bytes": int(item.get("size_bytes", 0) or 0),
                "modified_at": str(item.get("modified_at", "")).strip(),
            }
        )

    if not files:
        return ""

    stable_workflow_records = [
        {
            "label": str(item.get("label", "")).strip(),
            "exists": bool(item.get("exists", False)),
            "sha256": str(item.get("sha256", "")).strip(),
        }
        for item in workflow_records
        if str(item.get("label", "")).strip()
    ]
    payload = {
        "files": sorted(files, key=lambda item: item["path"]),
        "workflow": sorted(stable_workflow_records, key=lambda item: item["label"]),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


state_path = Path(sys.argv[1]).expanduser()
batch_path = Path(sys.argv[2]).expanduser()
run_at = datetime.fromisoformat(sys.argv[3])
workflow_records = parse_workflow_records(sys.argv[4])
batch_key = compute_batch_key(batch_path, workflow_records)

skip = False
reason = ""
failure_count = 0
next_retry_at = ""
last_error = ""
max_attempts = 0
retry_policy = ""

if batch_key and state_path.exists():
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except Exception:
        state = {}

    if str(state.get("batch_key", "")).strip() != batch_key:
        try:
            state_path.unlink()
        except OSError:
            pass
    else:
        failure_count = int(state.get("failure_count", 0) or 0)
        next_retry_at = str(state.get("next_retry_at", "") or "").strip()
        last_error = normalize(state.get("last_error", ""))
        max_attempts = int(state.get("max_attempts", 0) or 0)
        retry_policy = str(state.get("retry_policy", "") or "").strip()
        paused = bool(state.get("paused", False))
        if paused:
            skip = True
            reason = "paused"
        elif next_retry_at:
            retry_at = datetime.fromisoformat(next_retry_at)
            if run_at < retry_at:
                skip = True
                reason = "cooldown"

fields = [
    "true" if skip else "false",
    reason,
    str(failure_count),
    next_retry_at,
    last_error,
    str(max_attempts),
    retry_policy,
]
print("\x1f".join(fields))
PY
}

record_failure_backoff_state() {
  local batch_path="$1"
  local error_message="$2"
  local workflow_manifest_json=""
  workflow_manifest_json="$(build_workflow_fingerprint_manifest_json)"
  "$PYTHON_BIN" - "$FAILURE_STATE_PATH" "$batch_path" "$RUN_AT" "$error_message" "$AUTO_RETRY_BASE_MINUTES" "$AUTO_RETRY_MAX_COOLDOWN_MINUTES" "$AUTO_RETRY_MAX_SAME_BATCH" "$AUTO_RETRY_TRANSIENT_BASE_MINUTES" "$AUTO_RETRY_TRANSIENT_MAX_COOLDOWN_MINUTES" "$AUTO_RETRY_TRANSIENT_MAX_SAME_BATCH" "$workflow_manifest_json" <<'PY'
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def is_transient_network_failure(error_message: str) -> bool:
    lowered = normalize(error_message).lower()
    non_transient_markers = (
        "keychain",
        "permission",
        "forbidden",
        "unauthorized",
        "scope",
        "command not found",
        "配置失败",
        "权限失败",
        "格式失败",
    )
    if any(marker in lowered for marker in non_transient_markers):
        return False
    transient_markers = (
        "eof",
        "stream disconnected",
        "connection reset",
        "connection refused",
        "network is unreachable",
        "tls handshake timeout",
        "timed out",
        "timeout",
        "rate limit",
        "too many requests",
        "连接中断",
        "连接重置",
        "网络超时",
        "连接超时",
    )
    return any(marker in lowered for marker in transient_markers)


def parse_workflow_records(raw_text: str) -> list[dict[str, object]]:
    text = str(raw_text or "").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except Exception:
        return []
    records = payload.get("records") if isinstance(payload, dict) else payload
    normalized = []
    if not isinstance(records, list):
        return normalized
    for item in records:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "")).strip()
        if not path:
            continue
        normalized.append(
            {
                "label": str(item.get("label", "")).strip(),
                "path": path,
                "exists": bool(item.get("exists", False)),
                "sha256": str(item.get("sha256", "")).strip(),
            }
        )
    return normalized


def compute_batch_key(batch_path: Path, workflow_records: list[dict[str, object]]) -> tuple[str, int]:
    if not batch_path.exists():
        return "", 0
    try:
        data = json.loads(batch_path.read_text(encoding="utf-8"))
    except Exception:
        return "", 0

    files = []
    for item in data.get("files", []):
        path = str(item.get("path", "")).strip()
        if not path:
            continue
        files.append(
            {
                "path": path,
                "size_bytes": int(item.get("size_bytes", 0) or 0),
                "modified_at": str(item.get("modified_at", "")).strip(),
            }
        )

    if not files:
        return "", 0

    stable_workflow_records = [
        {
            "label": str(item.get("label", "")).strip(),
            "exists": bool(item.get("exists", False)),
            "sha256": str(item.get("sha256", "")).strip(),
        }
        for item in workflow_records
        if str(item.get("label", "")).strip()
    ]
    payload = {
        "files": sorted(files, key=lambda item: item["path"]),
        "workflow": sorted(stable_workflow_records, key=lambda item: item["label"]),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest(), len(files)


state_path = Path(sys.argv[1]).expanduser()
batch_path = Path(sys.argv[2]).expanduser()
run_at_raw = sys.argv[3].strip()
error_message = normalize(sys.argv[4])
default_base_minutes = max(int(sys.argv[5]), 1)
default_max_cooldown_minutes = max(int(sys.argv[6]), default_base_minutes)
default_max_attempts = max(int(sys.argv[7]), 1)
transient_base_minutes = max(int(sys.argv[8]), 1)
transient_max_cooldown_minutes = max(int(sys.argv[9]), transient_base_minutes)
transient_max_attempts = max(int(sys.argv[10]), 1)
workflow_records = parse_workflow_records(sys.argv[11])

if is_transient_network_failure(error_message):
    retry_policy = "transient_network"
    base_minutes = transient_base_minutes
    max_cooldown_minutes = transient_max_cooldown_minutes
    max_attempts = transient_max_attempts
else:
    retry_policy = "default"
    base_minutes = default_base_minutes
    max_cooldown_minutes = default_max_cooldown_minutes
    max_attempts = default_max_attempts

run_at = datetime.fromisoformat(run_at_raw)
batch_key, file_count = compute_batch_key(batch_path, workflow_records)
if not batch_key:
    print("\x1f\x1f\x1f\x1f")
    raise SystemExit(0)

try:
    previous = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
except Exception:
    previous = {}

previous_policy = str(previous.get("retry_policy", "default") or "default").strip()
if str(previous.get("batch_key", "")).strip() == batch_key and previous_policy == retry_policy:
    failure_count = int(previous.get("failure_count", 0) or 0) + 1
    first_failed_at = str(previous.get("first_failed_at", "")).strip() or run_at_raw
else:
    failure_count = 1
    first_failed_at = run_at_raw

paused = failure_count >= max_attempts
next_retry_at = ""
delay_minutes = 0
if not paused:
    delay_minutes = min(base_minutes * (2 ** (failure_count - 1)), max_cooldown_minutes)
    next_retry_at = (run_at + timedelta(minutes=delay_minutes)).isoformat()

payload = {
    "batch_key": batch_key,
    "batch_file_count": file_count,
    "failure_count": failure_count,
    "max_attempts": max_attempts,
    "retry_policy": retry_policy,
    "base_minutes": base_minutes,
    "max_cooldown_minutes": max_cooldown_minutes,
    "delay_minutes": delay_minutes or None,
    "paused": paused,
    "next_retry_at": next_retry_at or None,
    "first_failed_at": first_failed_at,
    "last_failed_at": run_at_raw,
    "last_error": error_message[:600],
    "updated_at": datetime.now().astimezone().isoformat(),
}
state_path.parent.mkdir(parents=True, exist_ok=True)
tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp_path.replace(state_path)

fields = [
    str(failure_count),
    "true" if paused else "false",
    next_retry_at,
    str(max_attempts),
    retry_policy,
]
print("\x1f".join(fields))
PY
}

extract_agent_error_summary_from_file() {
  local result_path="$1"
  python3 - "$result_path" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(0)

text = path.read_text(encoding="utf-8", errors="replace")
lines = text.splitlines(keepends=True)
offsets = []
cursor = 0
for line in lines:
    stripped = line.lstrip()
    if stripped.startswith("{"):
        offsets.append(cursor + (len(line) - len(stripped)))
    cursor += len(line)

for start in reversed(offsets):
    snippet = text[start:].strip()
    try:
        data = json.loads(snippet)
    except Exception:
        continue
    if isinstance(data, dict):
        summary = str(data.get("summary") or data.get("status") or "").strip()
        if summary:
            print(summary[:200])
            raise SystemExit(0)
        payloads = ((data.get("result") or {}).get("payloads") or [])
        for item in payloads:
            if isinstance(item, dict):
                payload_text = str(item.get("text") or "").strip()
                if payload_text:
                    print(payload_text.splitlines()[0][:200])
                    raise SystemExit(0)

for raw in text.splitlines():
    line = raw.strip()
    if line and not line.startswith("[plugins]"):
        print(line[:200])
        break
PY
}

check_chunk_text_ready() {
  local chunk_json_path="$1"
  "$PYTHON_BIN" "$HELPER_PATH" check-text-ready --batch-file "$chunk_json_path"
}

inspect_chunk_output() {
  local result_path="$1"
  "$PYTHON_BIN" "$HELPER_PATH" inspect-output --result-file "$result_path"
}

run_agent_turn() {
  local agent_id="$1"
  local thinking="$2"
  local timeout_seconds="$3"
  local result_path="$4"
  local prompt_text="$5"

  OPENCLAW_PROMPT_TEXT="$prompt_text" "$PYTHON_BIN" - "$agent_id" "$thinking" "$timeout_seconds" "$result_path" <<'PY'
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

agent_id = sys.argv[1]
thinking = sys.argv[2]
timeout_seconds = int(sys.argv[3] or "0")
result_path = Path(sys.argv[4])
prompt_text = os.environ.get("OPENCLAW_PROMPT_TEXT", "")

command = ["openclaw", "--no-color", "agent", "--agent", agent_id]
if thinking:
    command.extend(["--thinking", thinking])
if timeout_seconds > 0:
    command.extend(["--timeout", str(timeout_seconds)])
    wait_grace_seconds = min(60, timeout_seconds // 10)
    wait_timeout_seconds = timeout_seconds + wait_grace_seconds
else:
    wait_grace_seconds = 0
    wait_timeout_seconds = None
command.extend(["--message", prompt_text, "--json"])

with result_path.open("w", encoding="utf-8") as handle:
    process = subprocess.Popen(
        command,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        raise SystemExit(process.wait(timeout=wait_timeout_seconds))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        handle.write(
            "\n"
            + json.dumps(
                {
                    "status": "error",
                    "summary": f"local timeout after {timeout_seconds}s",
                    "error_type": "local_timeout",
                    "timeout_seconds": timeout_seconds,
                    "wait_grace_seconds": wait_grace_seconds,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        handle.flush()
        raise SystemExit(124)
PY
}

json_get_value() {
  local json_path="$1"
  local key="$2"
  python3 - "$json_path" "$key" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
key = sys.argv[2]

if not path.exists():
    raise SystemExit(1)

data = json.loads(path.read_text(encoding="utf-8"))
value = data.get(key)
if value is None:
    print("")
elif isinstance(value, list):
    for item in value:
        print(str(item))
else:
    print(str(value))
PY
}

build_manual_batch() {
  local -a python_args
  python_args=("$BATCH_JSON_PATH" "$WATCH_ROOT")
  if [[ "${#MANUAL_FILES[@]}" -gt 0 ]]; then
    python_args+=("${MANUAL_FILES[@]}")
  fi
  python_args+=("--folder-sep")
  if [[ "${#MANUAL_FOLDERS[@]}" -gt 0 ]]; then
    python_args+=("${MANUAL_FOLDERS[@]}")
  fi

  python3 - "${python_args[@]}" <<'PY'
import json
import sys
from datetime import datetime
from pathlib import Path

batch_path = Path(sys.argv[1])
root = Path(sys.argv[2]).expanduser().resolve()
args = sys.argv[3:]
split_index = args.index("--folder-sep") if "--folder-sep" in args else len(args)
manual_files = args[:split_index]
manual_folders = args[split_index + 1 :] if split_index < len(args) else []

paths: dict[str, Path] = {}
for raw in manual_files:
    path = Path(raw).expanduser().resolve()
    if path.is_file() and path.suffix.lower() == ".pdf":
        paths[str(path)] = path

for raw in manual_folders:
    folder = Path(raw).expanduser().resolve()
    if folder.is_file() and folder.suffix.lower() == ".pdf":
        paths[str(folder)] = folder
        continue
    if not folder.is_dir():
        continue
    for path in sorted(folder.rglob("*.pdf")):
        if path.is_file():
            paths[str(path.resolve())] = path.resolve()

files = []
for path_str in sorted(paths.keys()):
    path = paths[path_str]
    stat = path.stat()
    try:
        relative = path.relative_to(root).as_posix()
    except ValueError:
        relative = path.name
    files.append(
        {
            "path": path_str,
            "filename": path.name,
            "relative_path": relative,
            "size_bytes": int(stat.st_size),
            "modified_at": datetime.fromtimestamp(int(stat.st_mtime)).astimezone().isoformat(),
        }
    )

payload = {
    "generated_at": datetime.now().astimezone().isoformat(),
    "root": str(root),
    "state_file": None,
    "batch_file": str(batch_path),
    "first_run": False,
    "manual_mode": True,
    "new_pdf_count": len(files),
    "latest_modified_at": files[-1]["modified_at"] if files else None,
    "files": files,
}
batch_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(len(files))
PY
}

init_research_library_index() {
  mkdir -p \
    "$RESEARCH_LIBRARY_ROOT/batches" \
    "$RESEARCH_LIBRARY_ROOT/pdfs" \
    "$RESEARCH_LIBRARY_ROOT/markdown/raw" \
    "$RESEARCH_LIBRARY_ROOT/markdown/clean" \
    "$RESEARCH_LIBRARY_ROOT/summaries" \
    "$RESEARCH_LIBRARY_ROOT/cache/text_extract" \
    "$RESEARCH_LIBRARY_ROOT/cache/ocr" \
    "$RESEARCH_LIBRARY_ROOT/cache/markitdown" \
    "$RESEARCH_LIBRARY_ROOT/cache/summary" \
    "$RESEARCH_LIBRARY_ROOT/state" \
    "$RESEARCH_LIBRARY_ROOT/logs" \
    "$RESEARCH_LIBRARY_ROOT/config" \
    "$RESEARCH_LIBRARY_ROOT/prompts" \
    "$OBSIDIAN_VAULT_ROOT/10_Reports" \
    "$OBSIDIAN_VAULT_ROOT/_system" || true

  if [[ -z "$INDEX_SCRIPT_PATH" || ! -f "$INDEX_SCRIPT_PATH" ]]; then
    return 0
  fi
  RESEARCH_LIBRARY_ROOT="$RESEARCH_LIBRARY_ROOT" \
  "$PYTHON_BIN" "$INDEX_SCRIPT_PATH" --library-root "$RESEARCH_LIBRARY_ROOT" init >> "$LOG_PATH" 2>&1 || true
}

run_markdown_preprocess() {
  local chunk_json_path="$1"
  if [[ -n "$MARKITDOWN_SCRIPT_PATH_RESOLVED" && -f "$MARKITDOWN_SCRIPT_PATH_RESOLVED" ]]; then
    RESEARCH_LIBRARY_ROOT="$RESEARCH_LIBRARY_ROOT" \
    "$PYTHON_BIN" "$MARKITDOWN_SCRIPT_PATH_RESOLVED" \
      --batch-file "$chunk_json_path" \
      --library-root "$RESEARCH_LIBRARY_ROOT" >> "$LOG_PATH" 2>&1 || true
  fi

  if [[ -n "$CLEAN_MARKDOWN_SCRIPT_PATH_RESOLVED" && -f "$CLEAN_MARKDOWN_SCRIPT_PATH_RESOLVED" ]]; then
    RESEARCH_LIBRARY_ROOT="$RESEARCH_LIBRARY_ROOT" \
    "$PYTHON_BIN" "$CLEAN_MARKDOWN_SCRIPT_PATH_RESOLVED" \
      --batch-file "$chunk_json_path" \
      --library-root "$RESEARCH_LIBRARY_ROOT" >> "$LOG_PATH" 2>&1 || true
  fi
}

record_batch_index_status() {
  local chunk_json_path="$1"
  local index_status="$2"
  local error_message="${3:-}"
  if [[ -n "$INDEX_SCRIPT_PATH" && -f "$INDEX_SCRIPT_PATH" ]]; then
    RESEARCH_LIBRARY_ROOT="$RESEARCH_LIBRARY_ROOT" \
    "$PYTHON_BIN" "$INDEX_SCRIPT_PATH" \
      --library-root "$RESEARCH_LIBRARY_ROOT" \
      upsert-from-batch \
      --batch-file "$chunk_json_path" \
      --index-status "$index_status" \
      --error-message "$error_message" >> "$LOG_PATH" 2>&1 || true
  fi
}

record_batch_event() {
  local chunk_json_path="$1"
  local event_status="$2"
  local error_message="${3:-}"
  local doc_url="${4:-}"
  if [[ -n "$INDEX_SCRIPT_PATH" && -f "$INDEX_SCRIPT_PATH" ]]; then
    RESEARCH_LIBRARY_ROOT="$RESEARCH_LIBRARY_ROOT" \
    "$PYTHON_BIN" "$INDEX_SCRIPT_PATH" \
      --library-root "$RESEARCH_LIBRARY_ROOT" \
      record-events-from-batch \
      --batch-file "$chunk_json_path" \
      --status "$event_status" \
      --error-message "$error_message" \
      --feishu-doc-url "$doc_url" \
      --run-id "$RUN_AT" \
      --chunk-file "$chunk_json_path" >> "$LOG_PATH" 2>&1 || true
  fi
}

record_text_extract_events() {
  local chunk_json_path="$1"
  if [[ -n "$INDEX_SCRIPT_PATH" && -f "$INDEX_SCRIPT_PATH" ]]; then
    RESEARCH_LIBRARY_ROOT="$RESEARCH_LIBRARY_ROOT" \
    "$PYTHON_BIN" "$INDEX_SCRIPT_PATH" \
      --library-root "$RESEARCH_LIBRARY_ROOT" \
      record-text-extract-events \
      --batch-file "$chunk_json_path" \
      --run-id "$RUN_AT" \
      --chunk-file "$chunk_json_path" >> "$LOG_PATH" 2>&1 || true
  fi
}

record_text_extract_started_events() {
  local chunk_json_path="$1"
  if [[ -n "$INDEX_SCRIPT_PATH" && -f "$INDEX_SCRIPT_PATH" ]]; then
    RESEARCH_LIBRARY_ROOT="$RESEARCH_LIBRARY_ROOT" \
    "$PYTHON_BIN" "$INDEX_SCRIPT_PATH" \
      --library-root "$RESEARCH_LIBRARY_ROOT" \
      record-text-extract-started-events \
      --batch-file "$chunk_json_path" \
      --run-id "$RUN_AT" \
      --chunk-file "$chunk_json_path" >> "$LOG_PATH" 2>&1 || true
  fi
}

archive_obsidian_notes() {
  local chunk_json_path="$1"
  local doc_url="${2:-}"
  if [[ -n "$OBSIDIAN_ARCHIVE_SCRIPT_PATH_RESOLVED" && -f "$OBSIDIAN_ARCHIVE_SCRIPT_PATH_RESOLVED" ]]; then
    RESEARCH_LIBRARY_ROOT="$RESEARCH_LIBRARY_ROOT" \
    "$PYTHON_BIN" "$OBSIDIAN_ARCHIVE_SCRIPT_PATH_RESOLVED" \
      --batch-file "$chunk_json_path" \
      --library-root "$RESEARCH_LIBRARY_ROOT" \
      --vault-root "$OBSIDIAN_VAULT_ROOT" \
      --feishu-doc-url "$doc_url" >> "$LOG_PATH" 2>&1 || true
  fi
}

update_obsidian_indexes() {
  local chunk_json_path="$1"
  if [[ -z "$OBSIDIAN_INDEX_SCRIPT_PATH_RESOLVED" || ! -f "$OBSIDIAN_INDEX_SCRIPT_PATH_RESOLVED" ]]; then
    return 0
  fi

  mkdir -p "$(dirname "$OBSIDIAN_INDEX_RESULT_JSON_PATH")" || true
  local index_output index_rc
  set +e
  index_output="$(
    OBSIDIAN_VAULT_ROOT="$OBSIDIAN_VAULT_ROOT" \
    "$PYTHON_BIN" "$RUNTIME_GUARD_SCRIPT_PATH_RESOLVED" exec-timeout \
      --timeout-seconds "$OBSIDIAN_INDEX_TIMEOUT_SECONDS" \
      --terminate-grace-seconds "$OBSIDIAN_INDEX_TERMINATE_GRACE_SECONDS" \
      -- \
      "$PYTHON_BIN" "$OBSIDIAN_INDEX_SCRIPT_PATH_RESOLVED" \
        --batch-file "$chunk_json_path" \
        --vault-root "$OBSIDIAN_VAULT_ROOT" \
        --incremental-only \
        --result-file "$OBSIDIAN_INDEX_RESULT_JSON_PATH" 2>&1
  )"
  index_rc=$?
  set -e

  if [[ "$index_rc" -ne 0 ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Obsidian 主题/公司索引更新 warning：rc=${index_rc} ${index_output}" >> "$LOG_PATH"
  else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Obsidian 主题/公司索引更新结果：${index_output}" >> "$LOG_PATH"
  fi
  return 0
}

rebuild_obsidian_indexes_once() {
  if [[ -z "$OBSIDIAN_INDEX_SCRIPT_PATH_RESOLVED" || ! -f "$OBSIDIAN_INDEX_SCRIPT_PATH_RESOLVED" ]]; then
    return 0
  fi

  local batch_source_path="${ORIGINAL_BATCH_JSON_PATH:-${BATCH_JSON_PATH:-}}"
  local index_output index_rc
  write_run_status \
    "running" \
    "obsidian_index" \
    "rebuilding Obsidian indexes once for completed publish batch" \
    "" \
    "obsidian_index" \
    "$batch_source_path" \
    "" \
    "${CHUNK_TOTAL:-}" \
    "" \
    "" \
    "" \
    "${NEW_PDF_COUNT:-}"
  start_heartbeat \
    "running" \
    "obsidian_index" \
    "rebuilding Obsidian indexes once for completed publish batch" \
    "" \
    "obsidian_index" \
    "$batch_source_path" \
    "" \
    "${CHUNK_TOTAL:-}" \
    "" \
    "" \
    "" \
    "${NEW_PDF_COUNT:-}"

  mkdir -p "$(dirname "$OBSIDIAN_INDEX_RESULT_JSON_PATH")" || true
  set +e
  index_output="$(
    OBSIDIAN_VAULT_ROOT="$OBSIDIAN_VAULT_ROOT" \
    "$PYTHON_BIN" "$RUNTIME_GUARD_SCRIPT_PATH_RESOLVED" exec-timeout \
      --timeout-seconds "$OBSIDIAN_INDEX_TIMEOUT_SECONDS" \
      --terminate-grace-seconds "$OBSIDIAN_INDEX_TERMINATE_GRACE_SECONDS" \
      -- \
      "$PYTHON_BIN" "$OBSIDIAN_INDEX_SCRIPT_PATH_RESOLVED" \
        --rebuild-all \
        --vault-root "$OBSIDIAN_VAULT_ROOT" \
        --result-file "$OBSIDIAN_INDEX_RESULT_JSON_PATH" 2>&1
  )"
  index_rc=$?
  set -e
  stop_heartbeat

  if [[ "$index_rc" -ne 0 ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Obsidian 整批全量索引更新 warning：rc=${index_rc} ${index_output}" >> "$LOG_PATH"
  else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Obsidian 整批全量索引更新结果：${index_output}" >> "$LOG_PATH"
  fi
  return 0
}

record_summary_metadata() {
  local chunk_json_path="$1"
  record_batch_index_status "$chunk_json_path" "summary_created" ""
  archive_obsidian_notes "$chunk_json_path" ""
}

record_publish_metadata() {
  local chunk_json_path="$1"
  local doc_url="$2"
  if [[ -n "$INDEX_SCRIPT_PATH" && -f "$INDEX_SCRIPT_PATH" ]]; then
    RESEARCH_LIBRARY_ROOT="$RESEARCH_LIBRARY_ROOT" \
    "$PYTHON_BIN" "$INDEX_SCRIPT_PATH" \
      --library-root "$RESEARCH_LIBRARY_ROOT" \
      upsert-from-batch \
      --batch-file "$chunk_json_path" \
      --feishu-doc-url "$doc_url" \
      --index-status "feishu_published" >> "$LOG_PATH" 2>&1 || true
  fi
  record_batch_event "$chunk_json_path" "feishu_published" "" "$doc_url"
  archive_obsidian_notes "$chunk_json_path" "$doc_url"
  update_obsidian_indexes "$chunk_json_path"
}

extract_latest_modified_epoch() {
  python3 - "$BATCH_JSON_PATH" <<'PY'
import json
import sys
from datetime import datetime
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    print(0)
    raise SystemExit(0)

data = json.loads(path.read_text(encoding="utf-8"))
latest = str(data.get("latest_modified_at") or "").strip()
if not latest:
    print(0)
    raise SystemExit(0)

print(int(datetime.fromisoformat(latest).timestamp()))
PY
}

render_summary_prompt() {
  local chunk_json_path="$1"
  local output_path="$2"
  "$PYTHON_BIN" "$HELPER_PATH" render-prompt \
    --template "$SUMMARY_PROMPT_PATH" \
    --batch-file "$chunk_json_path" \
    --system-prompt-file "$SUMMARY_SYSTEM_PROMPT_PATH" \
    --output "$output_path"
}

build_lark_cli_create_markdown() {
  local chunk_json_path="$1"
  local summary_markdown_path="$2"
  local output_path="$3"
  "$PYTHON_BIN" "$HELPER_PATH" build-lark-cli-create-markdown \
    --batch-file "$chunk_json_path" \
    --summary-markdown "$summary_markdown_path" \
    --output "$output_path"
}

build_lark_cli_doc_title() {
  local chunk_json_path="$1"
  "$PYTHON_BIN" "$HELPER_PATH" build-doc-title \
    --batch-file "$chunk_json_path"
}

build_publish_key_payload() {
  local chunk_json_path="$1"
  local summary_markdown_path="$2"
  local target_doc_url="${3:-}"
  "$PYTHON_BIN" "$HELPER_PATH" build-publish-key \
    --batch-file "$chunk_json_path" \
    --summary-markdown "$summary_markdown_path" \
    --target-doc-url "$target_doc_url"
}

lookup_publish_record() {
  local publish_key="$1"
  "$PYTHON_BIN" "$HELPER_PATH" lookup-publish-record \
    --records-file "$PUBLISH_RECORDS_JSONL_PATH" \
    --publish-key "$publish_key"
}

lookup_publish_recovery() {
  local batch_hash="$1"
  local summary_hash="$2"
  local chunk_json_path="$3"
  "$PYTHON_BIN" "$HELPER_PATH" lookup-publish-recovery \
    --records-file "$PUBLISH_RECORDS_JSONL_PATH" \
    --batch-hash "$batch_hash" \
    --summary-hash "$summary_hash" \
    --batch-file "$chunk_json_path"
}

lookup_latest_same_day_doc() {
  local chunk_json_path="$1"
  local incoming_file_count="$2"
  "$PYTHON_BIN" "$HELPER_PATH" lookup-latest-same-day-doc \
    --records-file "$PUBLISH_RECORDS_JSONL_PATH" \
    --batch-file "$chunk_json_path" \
    --incoming-file-count "$incoming_file_count" \
    --max-file-count "$PUBLISH_MAX_FILES_PER_DOC" \
    --legacy-file-count "$PUBLISH_LEGACY_RECORD_FILE_COUNT"
}

record_publish_transition() {
  local status="$1"
  local publish_key="$2"
  local chunk_json_path="$3"
  local summary_markdown_path="$4"
  local target_doc_url="${5:-}"
  local doc_url="${6:-}"
  local mode="$7"
  local publisher="$8"
  local error_message="${9:-}"
  "$PYTHON_BIN" "$HELPER_PATH" append-publish-record \
    --records-file "$PUBLISH_RECORDS_JSONL_PATH" \
    --publish-key "$publish_key" \
    --batch-file "$chunk_json_path" \
    --summary-markdown "$summary_markdown_path" \
    --target-doc-url "$target_doc_url" \
    --doc-url "$doc_url" \
    --mode "$mode" \
    --publisher "$publisher" \
    --status "$status" \
    --error "$error_message"
}

record_publish_success() {
  record_publish_transition "success" "$@"
}

parse_lark_cli_doc_url() {
  local output_path="$1"
  local error_path="$2"
  local fallback_doc_url="${3:-}"
  "$PYTHON_BIN" "$HELPER_PATH" parse-lark-cli-doc-url \
    --output-file "$output_path" \
    --error-file "$error_path" \
    --fallback-doc-url "$fallback_doc_url" \
    --doc-url-base "$PUBLISH_LARK_CLI_DOC_URL_BASE"
}

classify_lark_cli_failure() {
  local raw="${1:-}"
  LARK_CLI_FAILURE_TEXT="$raw" "$PYTHON_BIN" - <<'PY'
import os
import re

text = re.sub(r"\s+", " ", os.environ.get("LARK_CLI_FAILURE_TEXT", "")).strip()
lowered = text.lower()
if any(part in lowered for part in ("keychain", "master key", "config init", "config keychain-downgrade")):
    print("lark-cli 配置失败")
elif any(part in lowered for part in ("permission", "forbidden", "unauthorized", "auth", "scope", "tenant_access_token")) or any(part in text for part in ("权限", "授权", "登录")):
    print("权限失败")
elif any(part in lowered for part in ("markdown", "format", "content", "invalid block")) or any(part in text for part in ("格式", "内容")):
    print("Markdown / 内容格式失败")
elif any(part in lowered for part in ("timeout", "network", "connection", "openapi", "api", "http", "rate limit")) or any(part in text for part in ("网络", "接口")):
    print("网络或 API 失败")
else:
    print("网络或 API 失败")
PY
}

grant_lark_cli_doc_chat_view() {
  local doc_url="$1"
  local work_dir="$2"

  if [[ -z "${TARGET_CHAT_ID:-}" ]]; then
    printf '权限失败：目标群 ID 为空，无法给飞书文档授权\n'
    return 1
  fi

  local doc_token
  doc_token="${doc_url%%\?*}"
  doc_token="${doc_token##*/}"
  if [[ -z "$doc_token" ]]; then
    printf '文档 URL 解析失败：无法从文档链接提取 token：%s\n' "$doc_url"
    return 1
  fi

  local params_json data_json output_path error_path
  params_json="$("$PYTHON_BIN" - "$doc_token" <<'PY'
import json
import sys

print(json.dumps({"token": sys.argv[1], "type": "docx"}, ensure_ascii=False))
PY
)"
  data_json="$("$PYTHON_BIN" - "$TARGET_CHAT_ID" <<'PY'
import json
import sys

print(
    json.dumps(
        {
            "type": "chat",
            "member_type": "openchat",
            "member_id": sys.argv[1],
            "perm": "view",
        },
        ensure_ascii=False,
    )
)
PY
)"
  output_path="$work_dir/lark-cli-docs.permission.out.json"
  error_path="$work_dir/lark-cli-docs.permission.err.log"

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] lark-cli 文档授权目标群开始：doc=${doc_url} chat=${TARGET_CHAT_ID}" >> "$LOG_PATH"
  set +e
  if [[ -n "${LARKSUITE_CLI_CONFIG_DIR:-}" ]]; then
    LARKSUITE_CLI_CONFIG_DIR="$LARKSUITE_CLI_CONFIG_DIR" "$LARK_CLI_BIN" drive permission.members create --as "$PUBLISH_LARK_CLI_AS" --params "$params_json" --data "$data_json" --yes --json > "$output_path" 2> "$error_path"
  else
    "$LARK_CLI_BIN" drive permission.members create --as "$PUBLISH_LARK_CLI_AS" --params "$params_json" --data "$data_json" --yes --json > "$output_path" 2> "$error_path"
  fi
  local grant_rc=$?
  set -e

  if [[ "$grant_rc" -ne 0 ]]; then
    local grant_error failure_type
    grant_error="$(summarize_command_output "$output_path" "$error_path")"
    failure_type="$(classify_lark_cli_failure "$grant_error")"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] lark-cli 文档授权目标群失败：${failure_type}：${grant_error}" >> "$LOG_PATH"
    printf '%s：%s\n' "$failure_type" "$grant_error"
    return 1
  fi

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] lark-cli 文档授权目标群成功：doc=${doc_url} chat=${TARGET_CHAT_ID}" >> "$LOG_PATH"
  return 0
}

rename_lark_cli_doc_title() {
  local doc_url="$1"
  local doc_title="$2"
  local work_dir="$3"

  if [[ -z "$doc_title" ]]; then
    printf '文档标题更新失败：标题为空\n'
    return 1
  fi

  local doc_token
  doc_token="${doc_url%%\?*}"
  doc_token="${doc_token##*/}"
  if [[ -z "$doc_token" ]]; then
    printf '文档标题更新失败：无法从文档链接提取 token：%s\n' "$doc_url"
    return 1
  fi

  local params_json data_json output_path error_path
  params_json="$("$PYTHON_BIN" - "$doc_token" <<'PY'
import json
import sys

print(json.dumps({"file_token": sys.argv[1], "type": "docx"}, ensure_ascii=False))
PY
)"
  data_json="$("$PYTHON_BIN" - "$doc_title" <<'PY'
import json
import sys

print(json.dumps({"new_title": sys.argv[1]}, ensure_ascii=False))
PY
)"
  output_path="$work_dir/lark-cli-docs.title.out.json"
  error_path="$work_dir/lark-cli-docs.title.err.log"

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] lark-cli 文档标题更新开始：doc=${doc_url} title=${doc_title}" >> "$LOG_PATH"
  set +e
  if [[ -n "${LARKSUITE_CLI_CONFIG_DIR:-}" ]]; then
    LARKSUITE_CLI_CONFIG_DIR="$LARKSUITE_CLI_CONFIG_DIR" "$LARK_CLI_BIN" drive files patch --as "$PUBLISH_LARK_CLI_AS" --params "$params_json" --data "$data_json" --json > "$output_path" 2> "$error_path"
  else
    "$LARK_CLI_BIN" drive files patch --as "$PUBLISH_LARK_CLI_AS" --params "$params_json" --data "$data_json" --json > "$output_path" 2> "$error_path"
  fi
  local rename_rc=$?
  set -e

  if [[ "$rename_rc" -ne 0 ]]; then
    local rename_error failure_type
    rename_error="$(summarize_command_output "$output_path" "$error_path")"
    failure_type="$(classify_lark_cli_failure "$rename_error")"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] lark-cli 文档标题更新失败：${failure_type}：${rename_error}" >> "$LOG_PATH"
    printf '%s：%s\n' "$failure_type" "$rename_error"
    return 1
  fi

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] lark-cli 文档标题更新成功：doc=${doc_url} title=${doc_title}" >> "$LOG_PATH"
  return 0
}

verify_lark_cli_doc_title() {
  local doc_url="$1"
  local expected_title="$2"
  local work_dir="$3"

  local output_path error_path title_path
  output_path="$work_dir/lark-cli-docs.inspect.out.json"
  error_path="$work_dir/lark-cli-docs.inspect.err.log"
  title_path="$work_dir/lark-cli-docs.inspect.title.txt"

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] lark-cli 文档标题校验开始：doc=${doc_url}" >> "$LOG_PATH"
  set +e
  if [[ -n "${LARKSUITE_CLI_CONFIG_DIR:-}" ]]; then
    LARKSUITE_CLI_CONFIG_DIR="$LARKSUITE_CLI_CONFIG_DIR" "$LARK_CLI_BIN" drive +inspect --as "$PUBLISH_LARK_CLI_AS" --url "$doc_url" --json > "$output_path" 2> "$error_path"
  else
    "$LARK_CLI_BIN" drive +inspect --as "$PUBLISH_LARK_CLI_AS" --url "$doc_url" --json > "$output_path" 2> "$error_path"
  fi
  local inspect_rc=$?
  set -e

  if [[ "$inspect_rc" -ne 0 ]]; then
    local inspect_error
    inspect_error="$(summarize_command_output "$output_path" "$error_path")"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] lark-cli 文档标题校验失败：${inspect_error}" >> "$LOG_PATH"
    printf '文档标题校验失败：%s\n' "$inspect_error"
    return 1
  fi

  "$PYTHON_BIN" - "$output_path" > "$title_path" <<'PY'
import json
import sys
from pathlib import Path
from typing import Any


def find_key(value: Any, target_key: str) -> str:
    if isinstance(value, dict):
        candidate = value.get(target_key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        for item in value.values():
            found = find_key(item, target_key)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_key(item, target_key)
            if found:
                return found
    return ""


path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
print(find_key(payload, "title") or find_key(payload, "name"))
PY
  local actual_title
  actual_title="$(cat "$title_path")"
  if [[ "$actual_title" != "$expected_title" ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] lark-cli 文档标题校验失败：expected=${expected_title} actual=${actual_title}" >> "$LOG_PATH"
    printf '文档标题校验失败：期望 "%s"，实际 "%s"\n' "$expected_title" "$actual_title"
    return 1
  fi

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] lark-cli 文档标题校验通过：title=${actual_title}" >> "$LOG_PATH"
  return 0
}

verify_lark_cli_doc_content() {
  local fetch_output_path="$1"
  local summary_markdown_path="$2"
  local doc_url="$3"

  "$PYTHON_BIN" - "$fetch_output_path" "$summary_markdown_path" "$doc_url" <<'PY'
import json
import re
import sys
from pathlib import Path
from typing import Any


def find_document_content(value: Any) -> str:
    if isinstance(value, dict):
        document = value.get("document")
        if isinstance(document, dict):
            content = document.get("content")
            if isinstance(content, str):
                return content
        content = value.get("content")
        if isinstance(content, str):
            return content
        for item in value.values():
            found = find_document_content(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = find_document_content(item)
            if found:
                return found
    return ""


def compact_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = re.sub(r"\[[^\]]+\]\(([^)]+)\)", r" \1 ", value)
    value = re.sub(r"[#>*_`~\-•]+", " ", value)
    value = re.sub(r"\s+", "", value)
    return value


def clean_markdown_line(line: str) -> str:
    line = re.sub(r"^\s{0,3}(#{1,6}|[-*+]|>\s*)\s*", "", line.strip())
    line = re.sub(r"^\d+[.)]\s*", "", line)
    line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)
    line = re.sub(r"[*_`]+", "", line)
    return line.strip()


fetch_path = Path(sys.argv[1])
summary_path = Path(sys.argv[2])
doc_url = sys.argv[3].strip()

payload = json.loads(fetch_path.read_text(encoding="utf-8"))
actual = find_document_content(payload)
expected = summary_path.read_text(encoding="utf-8", errors="replace").strip()
if not expected:
    print(f"本地摘要为空，无法校验飞书正文：{summary_path}")
    raise SystemExit(1)

expected_compact = compact_text(expected)
actual_compact = compact_text(actual)

generic_lines = {
    "核心结论",
    "核心问题与回答",
    "摘要",
    "本地文件",
}
anchors: list[str] = []
for raw_line in expected.splitlines():
    line = clean_markdown_line(raw_line)
    if not line or line in generic_lines or len(line) < 12:
        continue
    anchors.append(line)
    if len(anchors) >= 8:
        break

matched = [anchor for anchor in anchors if compact_text(anchor) in actual_compact]
min_chars = min(200, max(60, len(expected_compact) // 8))
min_matches = 1 if len(anchors) <= 2 else 2

if len(actual_compact) < min_chars or len(matched) < min_matches:
    print(
        "飞书文档正文校验失败：fetch 内容未包含本地摘要锚点；"
        f"doc={doc_url} expected_chars={len(expected_compact)} "
        f"fetched_chars={len(actual_compact)} matched_anchors={len(matched)}/{len(anchors)}"
    )
    raise SystemExit(1)

print(
    "飞书文档正文校验通过："
    f"doc={doc_url} fetched_chars={len(actual_compact)} matched_anchors={len(matched)}/{len(anchors)}"
)
PY
}

write_lark_cli_validation_result() {
  local chunk_json_path="$1"
  local doc_url="$2"
  local mode="$3"
  local output_path="$4"
  "$PYTHON_BIN" - "$chunk_json_path" "$doc_url" "$mode" "$output_path" <<'PY'
import json
import sys
from pathlib import Path

chunk_path = Path(sys.argv[1])
doc_url = sys.argv[2].strip()
mode = sys.argv[3].strip()
output_path = Path(sys.argv[4])
batch = json.loads(chunk_path.read_text(encoding="utf-8"))
files = [item for item in batch.get("files", []) if isinstance(item, dict)]
handled_paths = [str(item.get("path", "")).strip() for item in files if str(item.get("path", "")).strip()]
payload = {
    "status": "success",
    "publisher": "lark-cli",
    "mode": mode,
    "doc_url": doc_url,
    "handled_count": len(handled_paths),
    "handled_paths": handled_paths,
    "handled_files": [str(item.get("filename", "")).strip() for item in files],
    "chunk_index": int(batch.get("chunk_index", 1)),
    "chunk_total": int(batch.get("chunk_total", 1)),
}
output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(payload, ensure_ascii=False))
PY
}

fetch_and_verify_lark_cli_doc_content() {
  local doc_url="$1"
  local summary_markdown_path="$2"
  local work_dir="$3"
  local fetch_output_path="$work_dir/lark-cli-docs.fetch.out.json"
  local fetch_error_path="$work_dir/lark-cli-docs.fetch.err.log"
  local attempt=1 fetch_rc content_validation_output content_validation_rc

  while true; do
    set +e
    if [[ -n "${LARKSUITE_CLI_CONFIG_DIR:-}" ]]; then
      LARKSUITE_CLI_CONFIG_DIR="$LARKSUITE_CLI_CONFIG_DIR" "$LARK_CLI_BIN" docs +fetch --api-version v2 --as "$PUBLISH_LARK_CLI_AS" --doc "$doc_url" --json > "$fetch_output_path" 2> "$fetch_error_path"
    else
      "$LARK_CLI_BIN" docs +fetch --api-version v2 --as "$PUBLISH_LARK_CLI_AS" --doc "$doc_url" --json > "$fetch_output_path" 2> "$fetch_error_path"
    fi
    fetch_rc=$?
    set -e
    if [[ "$fetch_rc" -ne 0 ]]; then
      local fetch_error
      fetch_error="$(summarize_command_output "$fetch_output_path" "$fetch_error_path")"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] lark-cli docs +fetch 校验失败：${fetch_error}" >> "$LOG_PATH"
      printf 'docs +fetch 校验失败：%s\n' "$fetch_error"
      return 1
    fi

    set +e
    content_validation_output="$(verify_lark_cli_doc_content "$fetch_output_path" "$summary_markdown_path" "$doc_url" 2>&1)"
    content_validation_rc=$?
    set -e
    if [[ "$content_validation_rc" -eq 0 ]]; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] ${content_validation_output}" >> "$LOG_PATH"
      return 0
    fi
    if [[ "$attempt" -ge "$PUBLISH_FETCH_VERIFY_ATTEMPTS" ]]; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] lark-cli docs +fetch 正文校验失败：${content_validation_output}" >> "$LOG_PATH"
      printf 'docs +fetch 正文校验失败：%s\n' "$content_validation_output"
      return 1
    fi

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] lark-cli docs +fetch 正文尚未可见，${PUBLISH_FETCH_VERIFY_DELAY_SECONDS}s 后重试（${attempt}/${PUBLISH_FETCH_VERIFY_ATTEMPTS}）：${content_validation_output}" >> "$LOG_PATH"
    sleep "$PUBLISH_FETCH_VERIFY_DELAY_SECONDS"
    attempt=$((attempt + 1))
  done
}

run_lark_cli_docs_publish() {
  local chunk_json_path="$1"
  local summary_markdown_path="$2"
  local target_doc_url="${3:-}"
  local validation_path="$4"
  local work_dir="$5"
  local publish_key="$6"
  local publisher="$7"
  local resume_doc_url="${8:-}"
  local resume_mode="${9:-}"

  if [[ ! -f "$summary_markdown_path" || ! -s "$summary_markdown_path" ]]; then
    printf '本地 summary Markdown 路径不存在或内容为空：%s\n' "$summary_markdown_path"
    return 1
  fi
  if ! command -v "$LARK_CLI_BIN" >/dev/null 2>&1; then
    printf '网络或 API 失败：lark-cli command not found: %s\n' "$LARK_CLI_BIN"
    return 1
  fi

  mkdir -p "$work_dir"
  local mode content_path output_path error_path parse_path doc_title doc_url
  output_path="$work_dir/lark-cli-docs.out.json"
  error_path="$work_dir/lark-cli-docs.err.log"
  parse_path="$work_dir/lark-cli-docs.parse.json"

  local args=()
  if [[ -n "$resume_doc_url" ]]; then
    doc_url="$resume_doc_url"
    mode="$resume_mode"
    if [[ "$mode" != "create" && "$mode" != "append" ]]; then
      mode="append"
      if [[ -z "$target_doc_url" ]]; then
        mode="create"
      fi
    fi
    if [[ "$mode" == "create" ]]; then
      doc_title="$(build_lark_cli_doc_title "$chunk_json_path")"
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 恢复 remote_written 发布：mode=${mode} doc=${doc_url}；先 fetch 正文锚点，不重复 create/append" >> "$LOG_PATH"
    fetch_and_verify_lark_cli_doc_content "$doc_url" "$summary_markdown_path" "$work_dir" || return 1
  elif [[ -z "$target_doc_url" ]]; then
    mode="create"
    content_path="$work_dir/lark-cli-create.markdown.md"
    build_lark_cli_create_markdown "$chunk_json_path" "$summary_markdown_path" "$content_path" >/dev/null
    doc_title="$(build_lark_cli_doc_title "$chunk_json_path")"
    args=(docs +create --api-version v2 --as "$PUBLISH_LARK_CLI_AS" --content - --doc-format markdown --parent-position "$PUBLISH_LARK_CLI_PARENT_POSITION" --json)
  else
    mode="append"
    content_path="$summary_markdown_path"
    args=(docs +update --api-version v2 --as "$PUBLISH_LARK_CLI_AS" --doc "$target_doc_url" --command append --content - --doc-format markdown --json)
  fi

  if [[ -z "$resume_doc_url" ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] lark-cli docs ${mode} 开始：summary=${summary_markdown_path} target_doc=${target_doc_url:-新建}" >> "$LOG_PATH"
    set +e
    if [[ -n "${LARKSUITE_CLI_CONFIG_DIR:-}" ]]; then
      LARKSUITE_CLI_CONFIG_DIR="$LARKSUITE_CLI_CONFIG_DIR" "$LARK_CLI_BIN" "${args[@]}" < "$content_path" > "$output_path" 2> "$error_path"
    else
      "$LARK_CLI_BIN" "${args[@]}" < "$content_path" > "$output_path" 2> "$error_path"
    fi
    local docs_rc=$?
    set -e
    if [[ "$docs_rc" -ne 0 ]]; then
      local command_error failure_type
      command_error="$(summarize_command_output "$output_path" "$error_path")"
      failure_type="$(classify_lark_cli_failure "$command_error")"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] lark-cli docs ${mode} 失败：${failure_type}：${command_error}" >> "$LOG_PATH"
      printf '%s：%s\n' "$failure_type" "$command_error"
      return 1
    fi

    parse_lark_cli_doc_url "$output_path" "$error_path" "$target_doc_url" > "$parse_path"
    doc_url="$(json_get_value "$parse_path" "doc_url")"
    if [[ -z "$doc_url" ]]; then
      local parse_error
      parse_error="$(summarize_command_output "$output_path" "$error_path")"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] lark-cli docs ${mode} 文档 URL 解析失败：${parse_error}" >> "$LOG_PATH"
      printf '文档 URL 解析失败：%s\n' "$parse_error"
      return 1
    fi

    local remote_record_output remote_record_rc
    set +e
    remote_record_output="$(record_publish_transition "remote_written" "$publish_key" "$chunk_json_path" "$summary_markdown_path" "$target_doc_url" "$doc_url" "$mode" "$publisher" 2>&1)"
    remote_record_rc=$?
    set -e
    if [[ "$remote_record_rc" -ne 0 ]]; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] remote_written 发布记录写入失败：${remote_record_output}" >> "$LOG_PATH"
      printf '远端已写入，但 remote_written 记录失败：%s\n' "$remote_record_output"
      return 1
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] remote_written 发布记录已写入：mode=${mode} doc=${doc_url} key=${publish_key}" >> "$LOG_PATH"
  fi

  if [[ "$mode" == "create" ]]; then
    rename_lark_cli_doc_title "$doc_url" "$doc_title" "$work_dir" || return 1
    verify_lark_cli_doc_title "$doc_url" "$doc_title" "$work_dir" || return 1
  fi

  if [[ -z "$resume_doc_url" ]]; then
    fetch_and_verify_lark_cli_doc_content "$doc_url" "$summary_markdown_path" "$work_dir" || return 1
  fi

  if [[ "$mode" == "create" ]]; then
    grant_lark_cli_doc_chat_view "$doc_url" "$work_dir" || return 1
  fi

  write_lark_cli_validation_result "$chunk_json_path" "$doc_url" "$mode" "$validation_path" >/dev/null
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] lark-cli docs ${mode} 成功，fetch 校验通过：${doc_url}" >> "$LOG_PATH"
  printf '%s\n' "$doc_url"
  return 0
}

mark_publish_group_success() {
  local publish_group_json_path="$1"
  local publish_group_summary_markdown_path="$2"
  local target_doc_url="${3:-}"
  local doc_url="$4"
  local publish_group_file_count="$5"
  local publisher="$6"
  local publish_mode="$7"
  local publish_key="${8:-}"

  # The success transition is the commit point for local acknowledgement.  If
  # it cannot be made durable, leave the batch unacknowledged so the next run
  # can resume from remote_written instead of silently losing the transaction.
  if [[ -n "$publish_key" ]]; then
    local record_publish_output record_publish_rc
    set +e
    record_publish_output="$(record_publish_success "$publish_key" "$publish_group_json_path" "$publish_group_summary_markdown_path" "$target_doc_url" "$doc_url" "$publish_mode" "$publisher" 2>&1)"
    record_publish_rc=$?
    set -e
    if [[ "$record_publish_rc" -ne 0 ]]; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 发布 success 记录写入失败：${record_publish_output}" >> "$LOG_PATH"
      printf '发布 success 记录写入失败：%s\n' "$record_publish_output"
      return 1
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 发布 success 记录已写入：publisher=${publisher} mode=${publish_mode} key=${publish_key}" >> "$LOG_PATH"
  fi

  CURRENT_DOC_URL="$doc_url"
  LAST_DOC_URL_SEEN="$CURRENT_DOC_URL"
  append_unique_doc_url "$CURRENT_DOC_URL"
  PROCESSED_FILE_COUNT=$((PROCESSED_FILE_COUNT + publish_group_file_count))
  CURRENT_GROUP_FILE_COUNT=$((CURRENT_GROUP_FILE_COUNT + publish_group_file_count))
  SUCCESSFUL_CHUNK_JSONS+=("$publish_group_json_path")
  CURRENT_GROUP_CHUNK_JSONS+=("$publish_group_json_path")
  # The document is user-visible only after its remote verification and the
  # durable success transition above.  Notify here, before non-critical local
  # metadata/index work, so every completed document is delivered promptly.
  send_document_batch_notice "$publish_group_json_path" "$CURRENT_DOC_URL" "$publish_group_file_count"
  record_publish_metadata "$publish_group_json_path" "$CURRENT_DOC_URL"
  resolve_stage_retry_for_batch "$publish_group_json_path" "publish"
  if [[ "$MANUAL_MODE" != "true" ]]; then
    clear_quarantine_entries_for_batch "$publish_group_json_path"
  fi
  return 0
}

validate_summary_result() {
  local chunk_json_path="$1"
  local result_path="$2"
  "$PYTHON_BIN" "$HELPER_PATH" validate-summary \
    --batch-file "$chunk_json_path" \
    --result-file "$result_path"
}

persist_chunk_summary() {
  local chunk_json_path="$1"
  local result_path="$2"
  local summary_json_path="$3"
  local summary_markdown_path="$4"
  RESEARCH_LIBRARY_ROOT="$RESEARCH_LIBRARY_ROOT" \
  "$PYTHON_BIN" "$HELPER_PATH" persist-summary \
    --batch-file "$chunk_json_path" \
    --result-file "$result_path" \
    --summary-cache-dir "$SUMMARY_CACHE_DIR_PATH" \
    --output-json "$summary_json_path" \
    --output-markdown "$summary_markdown_path"
}

materialize_cached_chunk_summary() {
  local chunk_json_path="$1"
  local summary_json_path="$2"
  local summary_markdown_path="$3"
  RESEARCH_LIBRARY_ROOT="$RESEARCH_LIBRARY_ROOT" \
  "$PYTHON_BIN" "$HELPER_PATH" materialize-summary-cache \
    --batch-file "$chunk_json_path" \
    --summary-cache-dir "$SUMMARY_CACHE_DIR_PATH" \
    --output-json "$summary_json_path" \
    --output-markdown "$summary_markdown_path"
}

write_summary_result_manifest() {
  local manifest_path="$1"
  local status="$2"
  local chunk_json_path="$3"
  local chunk_index="$4"
  local chunk_total="$5"
  local chunk_file_count="$6"
  local current_file="$7"
  local summary_json_path="${8:-}"
  local summary_markdown_path="${9:-}"
  local inspect_path="${10:-}"
  local agent_id="${11:-}"
  local worker_id="${12:-}"
  local cache_hit="${13:-false}"
  local failure_reason="${14:-}"
  local exit_code="${15:-0}"
  local fatal_env_failure="${16:-false}"

  "$PYTHON_BIN" - "$manifest_path" "$status" "$chunk_json_path" "$chunk_index" "$chunk_total" "$chunk_file_count" "$current_file" "$summary_json_path" "$summary_markdown_path" "$inspect_path" "$agent_id" "$worker_id" "$cache_hit" "$failure_reason" "$exit_code" "$fatal_env_failure" <<'PY'
import json
import sys
from datetime import datetime
from pathlib import Path


def parse_int(value: str, default: int = 0) -> int:
    try:
        return int(str(value or "").strip())
    except Exception:
        return default


manifest_path = Path(sys.argv[1])
payload = {
    "status": sys.argv[2],
    "chunk_json_path": sys.argv[3],
    "chunk_index": parse_int(sys.argv[4]),
    "chunk_total": parse_int(sys.argv[5]),
    "chunk_file_count": parse_int(sys.argv[6]),
    "current_file": sys.argv[7],
    "summary_json_path": sys.argv[8],
    "summary_markdown_path": sys.argv[9],
    "inspect_path": sys.argv[10],
    "agent_id": sys.argv[11],
    "worker_id": sys.argv[12],
    "cache_hit": sys.argv[13].lower() == "true",
    "failure_reason": sys.argv[14],
    "exit_code": parse_int(sys.argv[15]),
    "fatal_env_failure": sys.argv[16].lower() == "true",
    "completed_at": datetime.now().astimezone().isoformat(),
}
manifest_path.parent.mkdir(parents=True, exist_ok=True)
tmp_path = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
tmp_path.replace(manifest_path)
PY
}

run_summary_agent_job() {
  local chunk_json_path="$1"
  local chunk_index="$2"
  local chunk_total="$3"
  local current_chunk_file="$4"
  local chunk_file_count="$5"
  local chunk_summary_prompt_path="$6"
  local chunk_summary_result_path="$7"
  local chunk_summary_validation_path="$8"
  local chunk_summary_json_path="$9"
  local chunk_summary_markdown_path="${10}"
  local chunk_summary_inspect_path="${11}"
  local worker_id="${12}"
  local worker_agent_id="${13}"
  local result_manifest_path="${14}"

  local worker_sessions_dir
  worker_sessions_dir="$(summary_sessions_dir_for_agent "$worker_agent_id")"
  local summary_stage_success="false"
  local failure_reason=""
  local failure_exit_code=1
  local fatal_env_failure="false"
  local summary_attempt=1
  local summary_general_retries_left=$((CHUNK_RETRY_COUNT))
  local summary_timeout_retries_left=$((SUMMARY_TIMEOUT_RETRY_COUNT))
  local summary_max_attempts=$((1 + summary_general_retries_left + summary_timeout_retries_left))

  while [[ "$summary_attempt" -le "$summary_max_attempts" ]]; do
    write_worker_status "$worker_id" "$worker_agent_id" "local_summary" "$chunk_json_path" "$chunk_index" "$chunk_total" "$current_chunk_file" "generating local summary" || true
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] worker ${worker_id} 开始生成第 ${chunk_index}/${chunk_total} 批本地摘要，agent=${worker_agent_id}，文件数=${chunk_file_count}，第 ${summary_attempt}/${summary_max_attempts} 次尝试" >> "$LOG_PATH"

    prepare_fresh_agent_session "本地摘要" "$chunk_index" "$chunk_total" "$worker_sessions_dir" "$worker_agent_id"
    local prompt_text
    prompt_text="$(cat "$chunk_summary_prompt_path")"
    set +e
    run_agent_turn "$worker_agent_id" "$SUMMARY_AGENT_THINKING" "$SUMMARY_AGENT_TIMEOUT_SECONDS" "$chunk_summary_result_path" "$prompt_text"
    local summary_agent_rc=$?
    set -e
    local summary_agent_timed_out="false"
    if [[ "$summary_agent_rc" -eq 124 ]]; then
      summary_agent_timed_out="true"
    fi

    local summary_inspect_output
    summary_inspect_output="$(inspect_chunk_output "$chunk_summary_result_path" || true)"
    if [[ -n "$summary_inspect_output" ]]; then
      printf '%s\n' "$summary_inspect_output" > "$chunk_summary_inspect_path"
      python3 - "$chunk_summary_inspect_path" "$chunk_json_path" "$worker_agent_id" <<'PY'
import json
import sys
from pathlib import Path

inspect_path = Path(sys.argv[1])
chunk_path = Path(sys.argv[2])
data = json.loads(inspect_path.read_text(encoding="utf-8"))
chunk = json.loads(chunk_path.read_text(encoding="utf-8"))
data["chunk_file"] = str(chunk_path)
data["chunk_index"] = int(chunk.get("chunk_index", 1))
data["agent_id"] = sys.argv[3]
inspect_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
    fi
    cleanup_agent_session_after_call "$worker_sessions_dir" "$worker_agent_id"

    if [[ "$summary_agent_rc" -ne 0 ]]; then
      local agent_error_summary summary_failure_summary
      agent_error_summary="$(extract_agent_error_summary_from_file "$chunk_summary_result_path" || true)"
      if [[ "$summary_agent_timed_out" != "true" && "$agent_error_summary" == local\ timeout\ after* ]]; then
        summary_agent_timed_out="true"
      fi
      if [[ "$summary_agent_timed_out" == "true" ]]; then
        if [[ -n "$agent_error_summary" ]]; then
          failure_reason="本地摘要 agent 超时：${agent_error_summary}"
        else
          failure_reason="本地摘要 agent 超时：local timeout after ${SUMMARY_AGENT_TIMEOUT_SECONDS}s"
        fi
      else
        failure_reason="本地摘要 agent 执行失败"
        if [[ -n "$agent_error_summary" ]]; then
          failure_reason="本地摘要 agent 执行失败：${agent_error_summary}"
        fi
      fi
      failure_exit_code="$summary_agent_rc"
      summary_failure_summary="$(summarize_failure_text "$failure_reason")"

      if [[ "$summary_failure_summary" == "登录令牌坏了，需要重新登录 OpenAI Codex" ]]; then
        fatal_env_failure="true"
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] worker ${worker_id} 检测到登录令牌失效，停止本轮后续 chunk 处理" >> "$LOG_PATH"
        break
      fi

      if [[ "$summary_agent_timed_out" == "true" && "$summary_timeout_retries_left" -gt 0 ]]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] worker ${worker_id} 第 ${chunk_index}/${chunk_total} 批本地摘要命中超时兜底，准备重试：${failure_reason}" >> "$LOG_PATH"
        summary_timeout_retries_left=$((summary_timeout_retries_left - 1))
        summary_attempt=$((summary_attempt + 1))
        sleep 5
        continue
      fi

      if [[ "$summary_agent_timed_out" != "true" && "$summary_general_retries_left" -gt 0 ]]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] worker ${worker_id} 第 ${chunk_index}/${chunk_total} 批本地摘要失败，准备重试：${failure_reason}" >> "$LOG_PATH"
        summary_general_retries_left=$((summary_general_retries_left - 1))
        summary_attempt=$((summary_attempt + 1))
        sleep 5
        continue
      fi

      break
    fi

    local summary_validation_output summary_validation_rc
    set +e
    summary_validation_output="$(validate_summary_result "$chunk_json_path" "$chunk_summary_result_path" 2>&1)"
    summary_validation_rc=$?
    set -e

    if [[ "$summary_validation_rc" -ne 0 ]]; then
      failure_reason="本地摘要校验失败：${summary_validation_output}"
      failure_exit_code=1

      if [[ "$summary_general_retries_left" -gt 0 ]]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] worker ${worker_id} 第 ${chunk_index}/${chunk_total} 批本地摘要校验失败，准备重试：${failure_reason}" >> "$LOG_PATH"
        summary_general_retries_left=$((summary_general_retries_left - 1))
        summary_attempt=$((summary_attempt + 1))
        sleep 5
        continue
      fi

      break
    fi

    printf '%s\n' "$summary_validation_output" > "$chunk_summary_validation_path"

    local persist_summary_output persist_summary_rc
    set +e
    persist_summary_output="$(persist_chunk_summary "$chunk_json_path" "$chunk_summary_result_path" "$chunk_summary_json_path" "$chunk_summary_markdown_path" 2>&1)"
    persist_summary_rc=$?
    set -e

    if [[ "$persist_summary_rc" -ne 0 ]]; then
      failure_reason="本地摘要落盘失败：${persist_summary_output}"
      failure_exit_code=1
      break
    fi

    summary_stage_success="true"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] worker ${worker_id} 第 ${chunk_index}/${chunk_total} 批本地摘要已落盘：${chunk_summary_markdown_path}" >> "$LOG_PATH"
    break
  done

  if [[ "$summary_stage_success" == "true" ]]; then
    write_summary_result_manifest "$result_manifest_path" "success" "$chunk_json_path" "$chunk_index" "$chunk_total" "$chunk_file_count" "$current_chunk_file" "$chunk_summary_json_path" "$chunk_summary_markdown_path" "$chunk_summary_inspect_path" "$worker_agent_id" "$worker_id" "false" "" "0" "false"
  else
    write_summary_result_manifest "$result_manifest_path" "summary_failed" "$chunk_json_path" "$chunk_index" "$chunk_total" "$chunk_file_count" "$current_chunk_file" "$chunk_summary_json_path" "$chunk_summary_markdown_path" "$chunk_summary_inspect_path" "$worker_agent_id" "$worker_id" "false" "$failure_reason" "$failure_exit_code" "$fatal_env_failure"
  fi
  clear_worker_status "$worker_id" || true
  return 0
}

compact_finished_summary_jobs() {
  if [[ "${#SUMMARY_ACTIVE_PIDS[@]}" -eq 0 ]]; then
    return 0
  fi

  local -a remaining_pids=()
  local -a remaining_manifests=()
  local -a remaining_worker_ids=()
  local index pid manifest_path
  for index in "${!SUMMARY_ACTIVE_PIDS[@]}"; do
    pid="${SUMMARY_ACTIVE_PIDS[$index]}"
    manifest_path="${SUMMARY_ACTIVE_MANIFESTS[$index]}"
    if [[ -f "$manifest_path" ]]; then
      wait "$pid" 2>/dev/null || true
    else
      remaining_pids+=("$pid")
      remaining_manifests+=("$manifest_path")
      remaining_worker_ids+=("${SUMMARY_ACTIVE_WORKER_IDS[$index]}")
    fi
  done
  if [[ "${#remaining_pids[@]}" -gt 0 ]]; then
    SUMMARY_ACTIVE_PIDS=("${remaining_pids[@]}")
    SUMMARY_ACTIVE_MANIFESTS=("${remaining_manifests[@]}")
    SUMMARY_ACTIVE_WORKER_IDS=("${remaining_worker_ids[@]}")
  else
    SUMMARY_ACTIVE_PIDS=()
    SUMMARY_ACTIVE_MANIFESTS=()
    SUMMARY_ACTIVE_WORKER_IDS=()
  fi
}

wait_for_summary_capacity() {
  while true; do
    compact_finished_summary_jobs
    if [[ "${#SUMMARY_ACTIVE_PIDS[@]}" -lt "$SUMMARY_WORKER_COUNT" ]]; then
      return 0
    fi
    write_run_status "running" "parallel_summary" "waiting for summary worker slot" "" "parallel_summary" "$ORIGINAL_BATCH_JSON_PATH" "" "$CHUNK_TOTAL" "" "" "" "$NEW_PDF_COUNT"
    sleep 1
  done
}

wait_for_all_summary_jobs() {
  while [[ "${#SUMMARY_ACTIVE_PIDS[@]}" -gt 0 ]]; do
    compact_finished_summary_jobs
    if [[ "${#SUMMARY_ACTIVE_PIDS[@]}" -gt 0 ]]; then
      write_run_status "running" "parallel_summary" "waiting for summary workers to finish" "" "parallel_summary" "$ORIGINAL_BATCH_JSON_PATH" "" "$CHUNK_TOTAL" "" "" "" "$NEW_PDF_COUNT"
      sleep 1
    fi
  done
}

launch_summary_job() {
  local chunk_json_path="$1"
  local chunk_index="$2"
  local chunk_total="$3"
  local current_chunk_file="$4"
  local chunk_file_count="$5"
  local chunk_summary_prompt_path="$6"
  local chunk_summary_result_path="$7"
  local chunk_summary_validation_path="$8"
  local chunk_summary_json_path="$9"
  local chunk_summary_markdown_path="${10}"
  local chunk_summary_inspect_path="${11}"
  local result_manifest_path="${12}"

  wait_for_summary_capacity
  local worker_id
  worker_id=""
  local candidate used active_worker_id
  for ((candidate = 1; candidate <= SUMMARY_WORKER_COUNT; candidate++)); do
    used="false"
    for active_worker_id in "${SUMMARY_ACTIVE_WORKER_IDS[@]:-}"; do
      if [[ "$active_worker_id" == "$candidate" ]]; then
        used="true"
        break
      fi
    done
    if [[ "$used" != "true" ]]; then
      worker_id="$candidate"
      break
    fi
  done
  if [[ -z "$worker_id" ]]; then
    worker_id="1"
  fi
  local worker_agent_id
  worker_agent_id="$(summary_agent_id_for_worker "$worker_id")"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${chunk_total} 批进入摘要 worker 队列：worker=${worker_id} agent=${worker_agent_id}" >> "$LOG_PATH"
  (
    run_summary_agent_job "$chunk_json_path" "$chunk_index" "$chunk_total" "$current_chunk_file" "$chunk_file_count" "$chunk_summary_prompt_path" "$chunk_summary_result_path" "$chunk_summary_validation_path" "$chunk_summary_json_path" "$chunk_summary_markdown_path" "$chunk_summary_inspect_path" "$worker_id" "$worker_agent_id" "$result_manifest_path"
  ) &
  SUMMARY_ACTIVE_PIDS+=("$!")
  SUMMARY_ACTIVE_MANIFESTS+=("$result_manifest_path")
  SUMMARY_ACTIVE_WORKER_IDS+=("$worker_id")
}

consume_summary_result_manifest() {
  local result_manifest_path="$1"
  local status chunk_json_path chunk_index chunk_file_count current_file inspect_path cache_hit failure_reason failure_exit_code fatal_env_failure
  status="$(json_get_value "$result_manifest_path" "status")"
  chunk_json_path="$(json_get_value "$result_manifest_path" "chunk_json_path")"
  chunk_index="$(json_get_value "$result_manifest_path" "chunk_index")"
  chunk_file_count="$(json_get_value "$result_manifest_path" "chunk_file_count")"
  current_file="$(json_get_value "$result_manifest_path" "current_file")"
  inspect_path="$(json_get_value "$result_manifest_path" "inspect_path")"
  cache_hit="$(json_get_value "$result_manifest_path" "cache_hit")"
  failure_reason="$(json_get_value "$result_manifest_path" "failure_reason")"
  failure_exit_code="$(json_get_value "$result_manifest_path" "exit_code")"
  fatal_env_failure="$(json_get_value "$result_manifest_path" "fatal_env_failure")"

  if [[ "$status" == "success" ]]; then
    SUMMARY_READY_FILE_COUNT=$((SUMMARY_READY_FILE_COUNT + chunk_file_count))
    record_summary_metadata "$chunk_json_path"
    resolve_stage_retry_for_batch "$chunk_json_path" "summary"
    if [[ -n "$inspect_path" && -f "$inspect_path" ]]; then
      CHUNK_USAGE_JSONS+=("$inspect_path")
    fi
    PUBLISH_READY_CHUNK_JSONS+=("$chunk_json_path")
    PUBLISH_READY_FILE_COUNT=$((PUBLISH_READY_FILE_COUNT + chunk_file_count))
    if [[ "$cache_hit" == "True" || "$cache_hit" == "true" ]]; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批命中本地摘要缓存，已进入飞书发布队列，文件数=${chunk_file_count}" >> "$LOG_PATH"
    else
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批本地摘要已进入飞书发布队列，文件数=${chunk_file_count}" >> "$LOG_PATH"
    fi
    return 0
  fi

  FAILURE_REASON="${failure_reason:-本地摘要失败}"
  FAILURE_EXIT_CODE="${failure_exit_code:-1}"
  FAILED_CHUNK_INDEX="$chunk_index"
  FAILED_FILE_COUNT=$((FAILED_FILE_COUNT + chunk_file_count))
  if [[ -n "$FAILED_FILES_SUMMARY" ]]; then
    FAILED_FILES_SUMMARY+=$'\n'
  fi
  FAILED_FILES_SUMMARY+="第 ${chunk_index}/${CHUNK_TOTAL} 批失败：${FAILURE_REASON}"
  record_batch_index_status "$chunk_json_path" "summary_failed" "$FAILURE_REASON"
  record_batch_event "$chunk_json_path" "summary_failed" "$FAILURE_REASON" ""
  if [[ "$fatal_env_failure" == "True" || "$fatal_env_failure" == "true" ]]; then
    record_stage_retry_for_batch "$chunk_json_path" "summary" "summary_env_failure" "env_failure" "true" "$FAILURE_REASON"
  else
    record_stage_retry_for_batch "$chunk_json_path" "summary" "summary_failed" "transient_failure" "true" "$FAILURE_REASON"
  fi
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批本地摘要最终失败：${FAILURE_REASON}" >> "$LOG_PATH"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 本批失败文件：" >> "$LOG_PATH"
  extract_file_lines "$chunk_json_path" >> "$LOG_PATH"
  if [[ "$fatal_env_failure" == "True" || "$fatal_env_failure" == "true" ]]; then
    FATAL_ENV_FAILURE="true"
    FATAL_ENV_FAILURE_SUMMARY="$FAILURE_REASON"
    return 1
  fi
  return 0
}

flush_pending_summary_results() {
  if [[ "${#PENDING_SUMMARY_MANIFESTS[@]}" -eq 0 ]]; then
    return 0
  fi

  wait_for_all_summary_jobs

  local manifest_path flush_failed
  flush_failed="false"
  for manifest_path in "${PENDING_SUMMARY_MANIFESTS[@]}"; do
    if [[ ! -f "$manifest_path" ]]; then
      FAILURE_REASON="摘要 worker 未产出结果：${manifest_path}"
      FAILURE_EXIT_CODE=1
      FATAL_ENV_FAILURE="true"
      FATAL_ENV_FAILURE_SUMMARY="$FAILURE_REASON"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] ${FAILURE_REASON}" >> "$LOG_PATH"
      flush_failed="true"
      break
    fi
    if ! consume_summary_result_manifest "$manifest_path"; then
      flush_failed="true"
      break
    fi
  done

  PENDING_SUMMARY_MANIFESTS=()
  PENDING_SUMMARY_FILE_COUNT=0

  if ! publish_ready_chunks "false"; then
    return 1
  fi
  if [[ "$flush_failed" == "true" ]]; then
    return 1
  fi
  return 0
}

build_publish_groups() {
  local output_dir="$1"
  local group_start_index="${2:-0}"
  local group_total="${3:-0}"
  shift 3
  "$PYTHON_BIN" "$HELPER_PATH" build-publish-groups \
    --output-dir "$output_dir" \
    --doc-group-size "$DOC_GROUP_SIZE" \
    --doc-group-threshold "$DOC_GROUP_THRESHOLD" \
    --total-file-count "$NEW_PDF_COUNT" \
    --group-start-index "$group_start_index" \
    --group-total "$group_total" \
    "$@"
}

format_display_time() {
  local raw="$1"
  if [[ -z "$raw" ]]; then
    echo "未知时间"
    return
  fi
  python3 - "$raw" <<'PY'
from datetime import datetime
import sys
text = sys.argv[1].strip()
formats = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S",
]
for fmt in formats:
    try:
        dt = datetime.strptime(text, fmt)
        print(dt.strftime("%m-%d %H:%M"))
        raise SystemExit(0)
    except Exception:
        pass
try:
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    print(dt.strftime("%m-%d %H:%M"))
except Exception:
    print(text)
PY
}

summarize_failure_text() {
  local raw="${1:-}"
  FAILURE_TEXT="$raw" "$PYTHON_BIN" - <<'PY'
import os
import json
import re
from collections import Counter

text = os.environ.get("FAILURE_TEXT", "")


def normalize(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def classify(reason: str) -> str:
    text = normalize(reason)
    if not text:
        return "未知错误"
    lowered = text.lower()
    if re.fullmatch(r'(权限失败|lark-cli 配置失败|网络或 API 失败|Markdown / 内容格式失败)?[:：]?[{}\[\],]*', text):
        return ""
    if any(
        marker in lowered
        for marker in (
            "keychain get failed",
            "keychain not initialized",
            "master key may have been cleaned up",
            "config keychain-downgrade",
        )
    ):
        return "lark-cli 读不到本机密钥，需要重新执行 keychain-downgrade"
    if re.match(r'^"(?:message|error)"\s*:', text):
        try:
            payload = json.loads("{" + text.rstrip(",") + "}")
        except Exception:
            payload = {}
        structured_message = normalize(payload.get("message") or payload.get("error"))
        structured_lowered = structured_message.lower()
        if structured_message:
            if "lark-cli update" in structured_lowered and "available, current" in structured_lowered:
                return ""
            if "api call failed" in structured_lowered and "eof" in structured_lowered:
                return "飞书 API 网络连接中断（EOF）"
            return structured_message[:120]
    if re.fullmatch(r'"[^"]+"\s*:\s*.*[,]?', text) or text in {"true", "false", "null"}:
        return ""
    if (
        ("未提供" in text or "缺少" in text)
        and (
            "feishu_doc" in text
            or "feishu_create_doc" in text
            or "feishu_update_doc" in text
            or "飞书文档" in text
        )
    ):
        return "当前会话缺少飞书文档写入工具"
    if (
        text.startswith("Starting processing with ")
        or text.startswith("SubprocessOutputError")
        or text.startswith("For further information visit ")
        or re.match(r"^\d+\s+page already has text!", text)
        or re.match(r"^\d+\s+\[tesseract\]", text)
    ):
        return ""
    if "missing ZSXQ_SUMMARY_JSON line" in text:
        return "本地摘要结果格式不完整"
    if (
        "oauth token refresh failed" in lowered
        or "failed to refresh openai codex token" in lowered
        or "refresh_token_reused" in lowered
        or "please try signing in again" in lowered
        or "please try again or re-auth" in lowered
        or "please try again or re-authenticate" in lowered
    ):
        return "登录令牌坏了，需要重新登录 OpenAI Codex"
    timeout_match = re.search(r"local timeout after (\d+)s", text)
    if timeout_match:
        return f"agent 调用超过 {timeout_match.group(1)} 秒，被本地超时兜底终止"
    if "direct_extract_garbled_and_ocr_failed" in text and "Tagged PDF" in text:
        return "正文可提取，但被误判进入 OCR；OCR 在 Tagged PDF 上失败"
    if "direct_extract_garbled_and_ocr_failed" in text and "Choose only one of --force-ocr" in text:
        return "正文可提取，但 OCR 参数冲突"
    if "direct_extract_garbled_and_ocr_failed" in text and "ocrmypdf_failed" in text:
        return "正文可提取，但误判进入 OCR，OCR 兜底失败"
    if "本地摘要 agent 执行失败" in text:
        return "本地摘要 agent 执行失败"
    if "OpenClaw agent 执行失败" in text:
        return "总结 agent 执行失败"
    if "状态回写失败" in text:
        return "状态回写失败"
    if "分批结果为空" in text:
        return "分批结果为空"
    if "Traceback" in text:
        text = text.split("Traceback", 1)[0].strip(" :;，。")
    if ";" in text:
        text = text.split(";", 1)[0].strip()
    return text[:120] or "未知错误"


items = []
for raw_line in text.splitlines():
    line = normalize(raw_line)
    if not line:
        continue
    line = re.sub(r"^第 \d+/\d+ 批失败：", "", line)
    summary = classify(line)
    if summary:
        items.append(summary)

if not items:
    print("未知错误")
    raise SystemExit(0)

counter = Counter(items)
if len(counter) == 1:
    reason = next(iter(counter.keys()))
    if reason.startswith("lark-cli 读不到"):
        print(reason)
    elif len(items) > 1:
        print(f"共 {len(items)} 批失败，主要原因：{reason}")
    else:
        print(reason)
else:
    top_parts = [f"{reason} x{count}" for reason, count in counter.most_common(2)]
    print(f"共 {len(items)} 批失败，主要原因：{'；'.join(top_parts)}")
PY
}

print_retry_status_lines() {
  local failure_count="${1:-0}"
  local next_retry_at="${2:-}"
  local paused="${3:-false}"
  local max_attempts="${4:-$AUTO_RETRY_MAX_SAME_BATCH}"
  local retry_policy="${5:-default}"

  if [[ -z "$failure_count" || "$failure_count" -le 0 ]]; then
    return 0
  fi

  if [[ -z "$max_attempts" || "$max_attempts" -le 0 ]]; then
    max_attempts="$AUTO_RETRY_MAX_SAME_BATCH"
  fi
  printf '重试情况：已失败 %s/%s 次\n' "$failure_count" "$max_attempts"
  if [[ "$retry_policy" == "transient_network" ]]; then
    printf '退避策略：临时网络故障，按 5/10/20 分钟快速重试\n'
  fi
  if [[ "$paused" == "true" ]]; then
    printf '后续：自动重试已暂停；待处理文件、授权状态或任务脚本变化后会自动恢复，也可手动执行 run.sh\n'
    return 0
  fi
  if [[ -n "$next_retry_at" ]]; then
    printf '后续：将于 %s 后自动重试\n' "$(format_display_time "$next_retry_at")"
  fi
}

record_failure_backoff_note() {
  local batch_path="$1"
  local error_message="$2"
  if [[ "$MANUAL_MODE" == "true" ]]; then
    return 0
  fi

  local failure_count paused next_retry_at max_attempts retry_policy
  IFS=$'\x1f' read -r failure_count paused next_retry_at max_attempts retry_policy < <(record_failure_backoff_state "$batch_path" "$error_message")
  if [[ -z "$failure_count" ]]; then
    return 0
  fi
  if [[ "$paused" == "true" ]]; then
    printf '同一批 PDF 已连续失败 %s 次，自动重试已暂停；待处理文件、授权状态或任务脚本变化后会自动恢复，也可手动执行 run.sh。' "$failure_count"
    return 0
  fi
  if [[ -n "$next_retry_at" ]]; then
    printf '已记录第 %s 次失败，下次自动重试不早于 %s。' "$failure_count" "$(format_display_time "$next_retry_at")"
  fi
}

send_progress_update() {
  local chunk_json_path="$1"
  if [[ "$SEND_PROGRESS_EACH_FILE" != "true" ]]; then
    return 0
  fi

  local mode_label progress_total group_total current_index overall_total current_name progress_message
  overall_total="$NEW_PDF_COUNT"
  current_index="$PROCESSED_FILE_COUNT"
  mode_label="自动总结"
  if [[ "$MANUAL_MODE" == "true" ]]; then
    mode_label="手动重跑"
  fi
  current_name="$(python3 - "$chunk_json_path" <<'PY'
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding='utf-8'))
files = data.get('files', [])
print(files[0].get('filename', '') if files else '')
PY
)"

  if [[ "$NEW_PDF_COUNT" -gt "$DOC_GROUP_THRESHOLD" ]]; then
    group_total="$DOC_GROUP_SIZE"
    if [[ "$GROUP_INDEX" -gt 0 ]]; then
      local processed_before_group=$(((GROUP_INDEX - 1) * DOC_GROUP_SIZE))
      local remaining=$((NEW_PDF_COUNT - processed_before_group))
    if [[ "$remaining" -lt "$DOC_GROUP_SIZE" ]]; then
      group_total="$remaining"
    fi
  fi
    progress_total="$CURRENT_GROUP_FILE_COUNT/$group_total"
    progress_message="${mode_label}｜本组已成功 ${progress_total}｜总成功 ${current_index}/${overall_total}｜刚完成：${current_name}"
  else
    progress_total="$current_index/$overall_total"
    progress_message="${mode_label}｜已成功 ${progress_total}｜刚完成：${current_name}"
  fi

  send_chat_message "$progress_message" "progress-${chunk_index}" "text" "$chunk_json_path"
}

send_document_batch_notice() {
  local publish_group_json_path="$1"
  local doc_url="$2"
  local publish_group_file_count="$3"

  if [[ "$SEND_DOCUMENT_NOTIFICATION_EACH_BATCH" != "true" || -z "$doc_url" ]]; then
    return 0
  fi

  local publish_index publish_total cumulative_count total_count message
  publish_index="$(json_get_value "$publish_group_json_path" "chunk_index")"
  publish_total="$(json_get_value "$publish_group_json_path" "chunk_total")"
  publish_index="${publish_index:-$GROUP_INDEX}"
  publish_total="${publish_total:-$EXPECTED_DOC_COUNT}"
  cumulative_count="${PROCESSED_FILE_COUNT:-0}"
  total_count="${NEW_PDF_COUNT:-$cumulative_count}"

  message="$( {
    printf '## ✅ 知识星球研报｜文档 %s/%s 已发布\n' "$publish_index" "$publish_total"
    printf '\n'
    printf '本批 **%s** 篇｜累计发布 **%s/%s**\n' "$publish_group_file_count" "$cumulative_count" "$total_count"
    printf '\n'
    printf '文档：[立即查看飞书文档](%s)\n' "$doc_url"
  } )"

  send_chat_message "$message" "doc-completed" "markdown" "$publish_group_json_path"
}

send_group_report_if_ready() {
  local force_send="${1:-false}"
  local threshold_met="false"
  if [[ "$NEW_PDF_COUNT" -gt "$DOC_GROUP_THRESHOLD" ]]; then
    threshold_met="true"
  fi

  if [[ "$threshold_met" != "true" && "$force_send" != "true" ]]; then
    return 0
  fi

  if [[ "$CURRENT_GROUP_FILE_COUNT" -le 0 ]]; then
    return 0
  fi

  if [[ "$force_send" != "true" && "$CURRENT_GROUP_FILE_COUNT" -lt "$DOC_GROUP_SIZE" ]]; then
    return 0
  fi

  # 逐文档通知已在 durable publish success 后立即发送；这里仅推进分组状态。
  CURRENT_GROUP_FILE_COUNT=0
  CURRENT_GROUP_CHUNK_JSONS=()
  CURRENT_DOC_URL=""
  GROUP_INDEX=$((GROUP_INDEX + 1))
}

send_summary_start_notice() {
  local expected_doc_count start_message
  expected_doc_count=1
  if [[ "$NEW_PDF_COUNT" -gt "$DOC_GROUP_THRESHOLD" ]]; then
    expected_doc_count=$(((NEW_PDF_COUNT + DOC_GROUP_SIZE - 1) / DOC_GROUP_SIZE))
  fi
  start_message="$( {
    printf '知识星球研报总结：开始处理\n'
    printf '执行时间：%s\n' "$DISPLAY_RUN_AT"
    printf '结果：已通过安静期检查，开始生成 Markdown 和摘要\n'
    printf '待处理：%s 篇\n' "$NEW_PDF_COUNT"
    printf '预计飞书文档：%s 份\n' "$expected_doc_count"
    printf '日志位置：%s\n' "$LOG_PATH"
  } )"
  send_chat_message "$start_message" "start" "text" "$BATCH_JSON_PATH"
}

publish_ready_chunks() {
  local force_publish="${1:-false}"
  if [[ "$DRY_RUN" == "true" || "${#PUBLISH_READY_CHUNK_JSONS[@]}" -eq 0 ]]; then
    return 0
  fi

  if [[ "$force_publish" != "true" ]]; then
    if [[ "${INCREMENTAL_PUBLISH_ENABLED:-true}" != "true" ]]; then
      return 0
    fi
    if [[ "$NEW_PDF_COUNT" -le "$DOC_GROUP_THRESHOLD" ]]; then
      return 0
    fi
    if [[ "${PUBLISH_READY_FILE_COUNT:-0}" -lt "$DOC_GROUP_SIZE" ]]; then
      return 0
    fi
  fi

  PUBLISH_FLUSH_INDEX=$((PUBLISH_FLUSH_INDEX + 1))
  local publish_group_dir
  publish_group_dir="$TEMP_DIR/publish_groups_${PUBLISH_FLUSH_INDEX}"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 触发飞书发布 flush：ready_files=${PUBLISH_READY_FILE_COUNT:-0} force=${force_publish}" >> "$LOG_PATH"

  set +e
  BUILD_PUBLISH_GROUPS_OUTPUT="$(build_publish_groups "$publish_group_dir" "$GROUP_INDEX" "$EXPECTED_DOC_COUNT" "${PUBLISH_READY_CHUNK_JSONS[@]}" 2>&1)"
  BUILD_PUBLISH_GROUPS_RC=$?
  set -e
  if [[ "$BUILD_PUBLISH_GROUPS_RC" -ne 0 ]]; then
    FAILURE_REASON="飞书发布分组生成失败：$(summarize_failure_text "$BUILD_PUBLISH_GROUPS_OUTPUT")"
    FAILURE_EXIT_CODE=1
    FATAL_ENV_FAILURE="true"
    FATAL_ENV_FAILURE_SUMMARY="$FAILURE_REASON"
    FAILED_FILE_COUNT=$((FAILED_FILE_COUNT + PUBLISH_READY_FILE_COUNT))
    if [[ -n "$FAILED_FILES_SUMMARY" ]]; then
      FAILED_FILES_SUMMARY+=$'\n'
    fi
    FAILED_FILES_SUMMARY+="$FAILURE_REASON"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 无法生成飞书发布分组：${FAILURE_REASON}" >> "$LOG_PATH"
    return 1
  fi

  printf '%s\n' "$BUILD_PUBLISH_GROUPS_OUTPUT" >> "$LOG_PATH"
  shopt -s nullglob
  local publish_group_batch_files=("$publish_group_dir"/publish-group-*.batch.json)
  shopt -u nullglob
  local local_publish_group_total="${#publish_group_batch_files[@]}"

  local publish_group_json_path
  for publish_group_json_path in "${publish_group_batch_files[@]}"; do
    local publish_index publish_total publish_group_file_count publish_current_file publish_group_name
    local publish_group_summary_json_path publish_group_summary_markdown_path publish_group_validation_path
    publish_index="$(json_get_value "$publish_group_json_path" "chunk_index")"
    publish_total="$(json_get_value "$publish_group_json_path" "chunk_total")"
    if [[ -z "$publish_total" ]]; then
      publish_total="$local_publish_group_total"
    fi
    publish_group_file_count="$(extract_batch_count "$publish_group_json_path")"
    publish_current_file="$(extract_first_filename "$publish_group_json_path")"
    publish_group_name="$(basename "${publish_group_json_path%.batch.json}")"
    publish_group_summary_json_path="$publish_group_dir/${publish_group_name}.summary.json"
    publish_group_summary_markdown_path="$publish_group_dir/${publish_group_name}.summary.md"
    publish_group_validation_path="$TEMP_DIR/${publish_group_name}.publish.validation.json"

    local publish_success publish_target_doc_url publish_mode publish_key_payload_path publish_record_lookup_path publish_key
    local publish_batch_hash publish_summary_hash publish_recovery_lookup_path publish_recovery_error_path same_day_lookup_path
    local resume_doc_url resume_mode publish_publisher publish_preflight_error same_day_lookup_allowed
    publish_success="false"
    publish_target_doc_url="$CURRENT_DOC_URL"
    publish_mode="create"
    if [[ -n "$publish_target_doc_url" ]]; then
      publish_mode="append"
    fi
    publish_key_payload_path="$TEMP_DIR/${publish_group_name}.publish-key.json"
    publish_record_lookup_path="$TEMP_DIR/${publish_group_name}.publish-record.json"
    publish_recovery_lookup_path="$TEMP_DIR/${publish_group_name}.publish-recovery.json"
    publish_recovery_error_path="$TEMP_DIR/${publish_group_name}.publish-recovery.error.txt"
    same_day_lookup_path="$TEMP_DIR/${publish_group_name}.same-day-doc.json"
    publish_key=""
    publish_batch_hash=""
    publish_summary_hash=""
    resume_doc_url=""
    resume_mode=""
    publish_publisher="lark-cli"
    publish_preflight_error=""
    same_day_lookup_allowed="false"

    # Only the first publish group of a run may continue a document left by an
    # earlier run.  Later group resets keep the existing new-document grouping
    # semantics for large batches.
    if [[ "$SAME_DAY_RESUME_ATTEMPTED" != "true" ]]; then
      SAME_DAY_RESUME_ATTEMPTED="true"
      if [[ -z "$CURRENT_DOC_URL" ]]; then
        same_day_lookup_allowed="true"
      fi
    fi

    # Recovery is keyed without the target document so a remote-written
    # transaction wins over any capacity decision that may have changed since
    # the previous attempt.
    set +e
    PUBLISH_KEY_OUTPUT="$(build_publish_key_payload "$publish_group_json_path" "$publish_group_summary_markdown_path" "" 2>&1)"
    PUBLISH_KEY_RC=$?
    set -e
    if [[ "$PUBLISH_KEY_RC" -eq 0 ]]; then
      printf '%s\n' "$PUBLISH_KEY_OUTPUT" > "$publish_key_payload_path"
      publish_batch_hash="$(json_get_value "$publish_key_payload_path" "batch_hash")"
      publish_summary_hash="$(json_get_value "$publish_key_payload_path" "summary_hash")"
      set +e
      lookup_publish_recovery "$publish_batch_hash" "$publish_summary_hash" "$publish_group_json_path" > "$publish_recovery_lookup_path" 2> "$publish_recovery_error_path"
      LOOKUP_RECOVERY_RC=$?
      set -e
      if [[ "$LOOKUP_RECOVERY_RC" -ne 0 ]]; then
        PUBLISH_RECOVERY_ERROR_TEXT="$(cat "$publish_recovery_error_path" 2>/dev/null || true)"
        printf '%s\n' "$PUBLISH_RECOVERY_ERROR_TEXT" >> "$LOG_PATH"
        publish_preflight_error="发布恢复记录查询失败：$(summarize_failure_text "$PUBLISH_RECOVERY_ERROR_TEXT")"
      elif [[ "$(json_get_value "$publish_recovery_lookup_path" "found")" == "True" ]]; then
        local recovery_status
        recovery_status="$(json_get_value "$publish_recovery_lookup_path" "status")"
        resume_doc_url="$(json_get_value "$publish_recovery_lookup_path" "doc_url")"
        resume_mode="$(json_get_value "$publish_recovery_lookup_path" "mode")"
        publish_target_doc_url="$(json_get_value "$publish_recovery_lookup_path" "target_doc_url")"
        publish_key="$(json_get_value "$publish_recovery_lookup_path" "publish_key")"
        publish_publisher="$(json_get_value "$publish_recovery_lookup_path" "publisher")"
        publish_publisher="${publish_publisher:-lark-cli}"
        publish_mode="$resume_mode"
        if [[ "$publish_mode" != "create" && "$publish_mode" != "append" ]]; then
          publish_mode="append"
          [[ -z "$publish_target_doc_url" ]] && publish_mode="create"
        fi

        if [[ "$recovery_status" == "success" && -n "$resume_doc_url" ]]; then
          if mark_publish_group_success "$publish_group_json_path" "$publish_group_summary_markdown_path" "$publish_target_doc_url" "$resume_doc_url" "$publish_group_file_count" "local-record" "$publish_mode" ""; then
            publish_success="true"
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${publish_index}/${publish_total} 组命中已完成发布记录，复用文档：${resume_doc_url}" >> "$LOG_PATH"
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] 本组处理文件：" >> "$LOG_PATH"
            extract_file_lines "$publish_group_json_path" >> "$LOG_PATH"
            send_progress_update "$publish_group_json_path"
            send_group_report_if_ready "false"
            continue
          fi
          publish_preflight_error="已完成发布记录的本地确认失败"
        elif [[ "$recovery_status" != "remote_written" || -z "$resume_doc_url" ]]; then
          publish_preflight_error="发布恢复记录缺少有效的 remote_written 文档"
        fi
      fi
    else
      publish_preflight_error="发布恢复 key 生成失败：$(summarize_failure_text "$PUBLISH_KEY_OUTPUT")"
    fi

    if [[ -z "$publish_preflight_error" && -z "$resume_doc_url" ]]; then
      if [[ "$same_day_lookup_allowed" == "true" ]]; then
        set +e
        lookup_latest_same_day_doc "$publish_group_json_path" "$publish_group_file_count" > "$same_day_lookup_path" 2>> "$LOG_PATH"
        LOOKUP_SAME_DAY_RC=$?
        set -e
        if [[ "$LOOKUP_SAME_DAY_RC" -ne 0 ]]; then
          publish_preflight_error="同日飞书文档容量查询失败"
        elif [[ "$(json_get_value "$same_day_lookup_path" "found")" == "True" ]]; then
          publish_target_doc_url="$(json_get_value "$same_day_lookup_path" "doc_url")"
          publish_mode="append"
          echo "[$(date '+%Y-%m-%d %H:%M:%S')] 续写同日飞书文档：doc=${publish_target_doc_url} current=$(json_get_value "$same_day_lookup_path" "current_file_count") incoming=${publish_group_file_count} max=${PUBLISH_MAX_FILES_PER_DOC}" >> "$LOG_PATH"
        else
          publish_target_doc_url=""
          publish_mode="create"
          echo "[$(date '+%Y-%m-%d %H:%M:%S')] 同日文档不续写：$(json_get_value "$same_day_lookup_path" "reason")" >> "$LOG_PATH"
        fi
      fi

      if [[ -z "$publish_preflight_error" ]]; then
        set +e
        PUBLISH_KEY_OUTPUT="$(build_publish_key_payload "$publish_group_json_path" "$publish_group_summary_markdown_path" "$publish_target_doc_url" 2>&1)"
        PUBLISH_KEY_RC=$?
        set -e
        if [[ "$PUBLISH_KEY_RC" -ne 0 ]]; then
          publish_preflight_error="发布去重 key 生成失败：$(summarize_failure_text "$PUBLISH_KEY_OUTPUT")"
        else
          printf '%s\n' "$PUBLISH_KEY_OUTPUT" > "$publish_key_payload_path"
          publish_key="$(json_get_value "$publish_key_payload_path" "publish_key")"
        fi
      fi

      if [[ -z "$publish_preflight_error" ]]; then
        set +e
        lookup_publish_record "$publish_key" > "$publish_record_lookup_path" 2>> "$LOG_PATH"
        LOOKUP_PUBLISH_RC=$?
        set -e
        if [[ "$LOOKUP_PUBLISH_RC" -ne 0 ]]; then
          publish_preflight_error="发布成功记录查询失败"
        elif [[ "$(json_get_value "$publish_record_lookup_path" "found")" == "True" ]]; then
          REUSED_DOC_URL="$(json_get_value "$publish_record_lookup_path" "doc_url")"
          if [[ -n "$REUSED_DOC_URL" ]]; then
            if mark_publish_group_success "$publish_group_json_path" "$publish_group_summary_markdown_path" "$publish_target_doc_url" "$REUSED_DOC_URL" "$publish_group_file_count" "local-record" "$publish_mode" ""; then
              publish_success="true"
              echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${publish_index}/${publish_total} 组命中旧版成功去重记录，复用文档：${REUSED_DOC_URL}" >> "$LOG_PATH"
              echo "[$(date '+%Y-%m-%d %H:%M:%S')] 本组处理文件：" >> "$LOG_PATH"
              extract_file_lines "$publish_group_json_path" >> "$LOG_PATH"
              send_progress_update "$publish_group_json_path"
              send_group_report_if_ready "false"
              continue
            fi
            publish_preflight_error="旧版成功发布记录的本地确认失败"
          fi
        fi
      fi

      if [[ -z "$publish_preflight_error" ]]; then
        local intent_output intent_rc
        set +e
        intent_output="$(record_publish_transition "intent" "$publish_key" "$publish_group_json_path" "$publish_group_summary_markdown_path" "$publish_target_doc_url" "" "$publish_mode" "$publish_publisher" 2>&1)"
        intent_rc=$?
        set -e
        if [[ "$intent_rc" -ne 0 ]]; then
          publish_preflight_error="发布 intent 记录写入失败：$(summarize_failure_text "$intent_output")"
        else
          echo "[$(date '+%Y-%m-%d %H:%M:%S')] 发布 intent 记录已写入：mode=${publish_mode} target=${publish_target_doc_url:-新建} key=${publish_key}" >> "$LOG_PATH"
        fi
      fi
    fi

    local lark_cli_work_dir
    lark_cli_work_dir="$TEMP_DIR/${publish_group_name}.lark-cli"
    if [[ -n "$publish_preflight_error" ]]; then
      LARK_CLI_PUBLISH_OUTPUT="$publish_preflight_error"
      LARK_CLI_PUBLISH_RC=1
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 发布前置检查失败：${publish_preflight_error}" >> "$LOG_PATH"
    else
      write_run_status "running" "publish_doc" "publishing summary group with lark-cli docs" "" "publish_doc" "$publish_group_json_path" "$publish_index" "$publish_total" "$publish_current_file" "" "" "$NEW_PDF_COUNT"
      start_heartbeat "running" "publish_doc" "publishing summary group with lark-cli docs" "" "publish_doc" "$publish_group_json_path" "$publish_index" "$publish_total" "$publish_current_file" "" "" "$NEW_PDF_COUNT"
      set +e
      LARK_CLI_PUBLISH_OUTPUT="$(run_lark_cli_docs_publish "$publish_group_json_path" "$publish_group_summary_markdown_path" "$publish_target_doc_url" "$publish_group_validation_path" "$lark_cli_work_dir" "$publish_key" "$publish_publisher" "$resume_doc_url" "$resume_mode" 2>&1)"
      LARK_CLI_PUBLISH_RC=$?
      set -e
      stop_heartbeat
    fi
    if [[ "$LARK_CLI_PUBLISH_RC" -eq 0 ]]; then
      CURRENT_DOC_URL="$(json_get_value "$publish_group_validation_path" "doc_url")"
      if [[ -z "$CURRENT_DOC_URL" ]]; then
        CURRENT_DOC_URL="$LARK_CLI_PUBLISH_OUTPUT"
      fi
      if mark_publish_group_success "$publish_group_json_path" "$publish_group_summary_markdown_path" "$publish_target_doc_url" "$CURRENT_DOC_URL" "$publish_group_file_count" "$publish_publisher" "$publish_mode" "$publish_key"; then
        publish_success="true"
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${publish_index}/${publish_total} 组 lark-cli docs 发布完成，文档：${CURRENT_DOC_URL}" >> "$LOG_PATH"
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 本组处理文件：" >> "$LOG_PATH"
        extract_file_lines "$publish_group_json_path" >> "$LOG_PATH"
        send_progress_update "$publish_group_json_path"
        send_group_report_if_ready "false"
        continue
      fi
      LARK_CLI_PUBLISH_OUTPUT="远端校验已通过，但 success 发布记录未能持久化"
      LARK_CLI_PUBLISH_RC=1
    fi

    FAILURE_REASON="lark-cli docs 发布失败：$(summarize_failure_text "$LARK_CLI_PUBLISH_OUTPUT")"
    FAILURE_EXIT_CODE=1
    FATAL_ENV_FAILURE="true"
    FATAL_ENV_FAILURE_SUMMARY="$FAILURE_REASON"
    PUBLISH_RETRY_ERROR_CODE="publish_failed"
    PUBLISH_RETRY_ERROR_TYPE="transient_failure"
    PUBLISH_RETRYABLE="true"
    if is_release_contract_mismatch "$LARK_CLI_PUBLISH_OUTPUT"; then
      FATAL_RELEASE_CONTRACT_MISMATCH="true"
      PUBLISH_RETRY_ERROR_CODE="release_contract_mismatch"
      PUBLISH_RETRY_ERROR_TYPE="release_contract_mismatch"
      PUBLISH_RETRYABLE="false"
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${publish_index}/${publish_total} 组 lark-cli docs 发布失败：${LARK_CLI_PUBLISH_OUTPUT}" >> "$LOG_PATH"

    if [[ "$publish_success" != "true" ]]; then
      FAILED_CHUNK_INDEX="$publish_index"
      FAILED_FILE_COUNT=$((FAILED_FILE_COUNT + publish_group_file_count))
      if [[ -n "$FAILED_FILES_SUMMARY" ]]; then
        FAILED_FILES_SUMMARY+=$'\n'
      fi
      FAILED_FILES_SUMMARY+="第 ${publish_index}/${publish_total} 个发布分组失败：${FAILURE_REASON}"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${publish_index}/${publish_total} 个发布分组飞书发布最终失败：${FAILURE_REASON}" >> "$LOG_PATH"
      record_batch_index_status "$publish_group_json_path" "feishu_failed" "$FAILURE_REASON"
      record_batch_event "$publish_group_json_path" "feishu_failed" "$FAILURE_REASON" ""
      record_stage_retry_for_batch "$publish_group_json_path" "publish" "$PUBLISH_RETRY_ERROR_CODE" "$PUBLISH_RETRY_ERROR_TYPE" "$PUBLISH_RETRYABLE" "$FAILURE_REASON"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 本组本地摘要已保留：" >> "$LOG_PATH"
      printf '%s\n' "$publish_group_summary_markdown_path" >> "$LOG_PATH"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 本组失败文件：" >> "$LOG_PATH"
      extract_file_lines "$publish_group_json_path" >> "$LOG_PATH"
      return 1
    fi
  done

  PUBLISH_READY_CHUNK_JSONS=()
  PUBLISH_READY_FILE_COUNT=0
  return 0
}

DISPLAY_RUN_AT="$(format_display_time "$RUN_AT")"

LOCK_ACQUIRE_RC=0
if acquire_run_lock; then
  LOCK_ACQUIRE_RC=0
else
  LOCK_ACQUIRE_RC=$?
  if [[ "$LOCK_ACQUIRE_RC" -eq 23 ]]; then
    BUSY_REPORT="$(printf '【ZSXQ PDF 总结跳过】\n执行时间：%s\n状态：busy\n结果：已有运行中的 PDF 总结任务，本次先跳过\n日志位置：%s' "$RUN_AT" "$LOG_PATH")"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 已有运行中的 PDF 总结任务，当前跳过" >> "$LOG_PATH"
    printf '%s\n' "$BUSY_REPORT"
    exit 0
  fi

  RUN_STATUS_ACTIVE_RUN="true"
  printf '【ZSXQ PDF 总结失败】\n执行时间：%s\n状态：env_failed\n结果：运行锁初始化失败（rc=%s）\n日志位置：%s\n' "$RUN_AT" "$LOCK_ACQUIRE_RC" "$LOG_PATH" > "$RESULT_MD_PATH"
  write_result_json "env_failed" "运行锁初始化失败" "0" "" "1" "$BATCH_JSON_PATH" "[]" "lock_failed" "completed"
  complete_run_status "env_failed" "completed" "runtime lock initialization failed" "1" "lock_failed"
  finish_with_result 1 "lock-failed" "text" "$BATCH_JSON_PATH"
fi

RUN_STATUS_ACTIVE_RUN="true"
sync_openai_codex_auth_from_main
drain_notification_outbox || true
write_usage_summary "$USAGE_JSON_PATH"
write_run_status "running" "run_sh_init" "summary task started" "" "starting"
init_research_library_index

if [[ "$PREFLIGHT_ONLY" == "true" ]]; then
  write_run_status "running" "preflight" "running task preflight" "" "preflight_only"
  PREFLIGHT_OK="$(run_task_preflight)"
  PREFLIGHT_SUMMARY="$(summarize_preflight_report)"
  if [[ "$PREFLIGHT_OK" == "true" ]]; then
    printf '【ZSXQ PDF 预检通过】\n执行时间：%s\n状态：success\n结果：%s\n预检报告：%s\n' "$RUN_AT" "$PREFLIGHT_SUMMARY" "$PREFLIGHT_JSON_PATH" > "$RESULT_MD_PATH"
    write_result_json "success" "预检通过" "0" "" "0" "$BATCH_JSON_PATH" "[]" "preflight_only" "completed"
    complete_run_status "success" "completed" "preflight completed" "0" "preflight_only"
    finish_with_result 0 "preflight-success" "text" "$BATCH_JSON_PATH"
  fi

  printf '【ZSXQ PDF 预检失败】\n执行时间：%s\n状态：env_failed\n结果：%s\n预检报告：%s\n日志位置：%s\n' "$RUN_AT" "$PREFLIGHT_SUMMARY" "$PREFLIGHT_JSON_PATH" "$LOG_PATH" > "$RESULT_MD_PATH"
    write_result_json "env_failed" "$PREFLIGHT_SUMMARY" "0" "" "1" "$BATCH_JSON_PATH" "[]" "preflight_failed" "completed"
  complete_run_status "env_failed" "completed" "preflight failed" "1" "preflight_failed"
  finish_with_result 1 "preflight-failed" "text" "$BATCH_JSON_PATH"
fi

zsxq_download_active_for_dir() {
  local task_dir="$1"
  local pid_file="$task_dir/.run.pid"
  if [[ -f "$pid_file" ]]; then
    local pid
    pid="$(cat "$pid_file" 2>/dev/null || true)"
    if [[ -n "$pid" ]] && ps -p "$pid" >/dev/null 2>&1; then
      echo "true"
      return 0
    fi
  fi

  python3 - "$task_dir/run_status.json" <<'PY'
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

status_path = Path(sys.argv[1])

if status_path.exists():
    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    status = str(data.get("status", "")).strip()
    heartbeat_raw = str(data.get("last_heartbeat_at", "")).strip()
    if status == "running" and heartbeat_raw:
        try:
            heartbeat = datetime.fromisoformat(heartbeat_raw)
            if datetime.now().astimezone() - heartbeat <= timedelta(minutes=3):
                print("true")
                raise SystemExit(0)
        except Exception:
            pass

print("false")
PY
}

zsxq_download_active() {
  local dirs_raw="${DOWNLOAD_TASK_DIRS:-${DOWNLOAD_TASK_DIR:-}}"
  local old_ifs="$IFS"
  local dirs=()
  IFS=':,'
  read -r -a dirs <<< "$dirs_raw" || true
  IFS="$old_ifs"

  local task_dir
  for task_dir in "${dirs[@]}"; do
    if [[ -z "$task_dir" ]]; then
      continue
    fi
    if [[ "$(zsxq_download_active_for_dir "$task_dir")" == "true" ]]; then
      echo "true"
      return 0
    fi
  done

  echo "false"
}

if [[ "$MANUAL_MODE" == "true" ]]; then
  write_run_status "running" "build_manual_batch" "building manual batch" "" "building_manual_batch"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 手动模式，构建指定 PDF 批次" >> "$LOG_PATH"
  NEW_PDF_COUNT="$(build_manual_batch)"
else
  write_run_status "running" "scan_pending_batch" "scanning for pending PDFs" "" "scan_pending_batch"
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 自动模式，开始扫描新增 PDF" >> "$LOG_PATH"
  build_scan_root_args
  "$PYTHON_BIN" "$SCANNER_PATH" \
    "${SCAN_ROOT_ARGS[@]}" \
    --state-file "$STATE_PATH" \
    --batch-file "$BATCH_JSON_PATH" >> "$LOG_PATH"
  NEW_PDF_COUNT="$(extract_batch_count)"
fi

if [[ -n "$RELEASE_CONTRACT_ERROR" ]]; then
  finish_blocked_release "$BATCH_JSON_PATH" "$NEW_PDF_COUNT" "$RELEASE_CONTRACT_ERROR"
fi

if [[ "$NEW_PDF_COUNT" -eq 0 ]]; then
  clear_failure_backoff_state
  printf '【ZSXQ PDF 总结跳过】\n执行时间：%s\n状态：success\n结果：没有新增 PDF\n日志位置：%s\n' "$RUN_AT" "$LOG_PATH" > "$RESULT_MD_PATH"
  write_result_json "success" "没有新增 PDF" "0" "" "0" "$BATCH_JSON_PATH" "[]" "idle_no_new_pdf" "completed"
  complete_run_status "success" "completed" "no new PDFs to summarize" "0" "idle_no_new_pdf" "$BATCH_JSON_PATH" "" "" "" "" "" "0" "0"
  cat "$RESULT_MD_PATH"
  exit 0
fi

if [[ "$MANUAL_MODE" != "true" ]]; then
  LATEST_MODIFIED_EPOCH="$(extract_latest_modified_epoch)"
  NOW_EPOCH="$(date +%s)"
  AGE_SECONDS=$((NOW_EPOCH - LATEST_MODIFIED_EPOCH))
  if [[ "$LATEST_MODIFIED_EPOCH" -gt 0 && "$AGE_SECONDS" -lt "$QUIET_WINDOW_SECONDS" ]]; then
    WAIT_MINUTES=$(((QUIET_WINDOW_SECONDS - AGE_SECONDS + 59) / 60))
    printf '【ZSXQ PDF 总结等待中】\n执行时间：%s\n状态：waiting\n结果：检测到新增 PDF，但仍处于静默等待期\n等待规则：最近一次新文件写入后静默 %s 分钟再开始\n预计还需等待：约 %s 分钟\n日志位置：%s\n' "$RUN_AT" "$QUIET_WINDOW_MINUTES" "$WAIT_MINUTES" "$LOG_PATH" > "$RESULT_MD_PATH"
    write_result_json "waiting" "检测到新增 PDF，但仍处于静默等待期" "$NEW_PDF_COUNT" "" "0" "$BATCH_JSON_PATH" "[]" "waiting_quiet_window" "waiting_quiet_window" "quiet_window" ""
    complete_run_status "waiting" "waiting_quiet_window" "waiting for quiet window before summarizing" "0" "waiting_quiet_window" "$BATCH_JSON_PATH" "" "" "" "quiet_window" "" "$NEW_PDF_COUNT"
    finish_with_result 0 "waiting-quiet-window" "text" "$BATCH_JSON_PATH"
  fi

  if [[ "$(zsxq_download_active)" == "true" ]]; then
    printf '【ZSXQ PDF 总结等待中】\n执行时间：%s\n状态：waiting\n结果：ZSXQ 自动下载任务仍在运行，当前先不总结\n日志位置：%s\n' "$RUN_AT" "$LOG_PATH" > "$RESULT_MD_PATH"
    write_result_json "waiting" "ZSXQ 自动下载任务仍在运行，当前先不总结" "$NEW_PDF_COUNT" "" "0" "$BATCH_JSON_PATH" "[]" "waiting_download_task" "waiting_download_task" "download_task_running" ""
    complete_run_status "waiting" "waiting_download_task" "waiting for autodownload task to finish" "0" "waiting_download_task" "$BATCH_JSON_PATH" "" "" "" "download_task_running" "" "$NEW_PDF_COUNT"
    finish_with_result 0 "waiting-download-task" "text" "$BATCH_JSON_PATH"
  fi

  BACKOFF_SKIP="false"
  BACKOFF_REASON=""
  BACKOFF_FAILURE_COUNT="0"
  BACKOFF_NEXT_RETRY_AT=""
  BACKOFF_LAST_ERROR=""
  BACKOFF_MAX_ATTEMPTS=""
  BACKOFF_RETRY_POLICY=""
  IFS=$'\x1f' read -r BACKOFF_SKIP BACKOFF_REASON BACKOFF_FAILURE_COUNT BACKOFF_NEXT_RETRY_AT BACKOFF_LAST_ERROR BACKOFF_MAX_ATTEMPTS BACKOFF_RETRY_POLICY < <(get_failure_backoff_status "$BATCH_JSON_PATH")
  if [[ "$BACKOFF_SKIP" == "true" ]]; then
    BACKOFF_ERROR_SUMMARY="$(summarize_failure_text "$BACKOFF_LAST_ERROR")"
    if [[ "$BACKOFF_REASON" == "paused" ]]; then
      {
        printf '【ZSXQ PDF 总结已暂停】\n'
        printf '执行时间：%s\n' "$RUN_AT"
        printf '状态：paused\n'
        printf '结果：同一批 PDF 已连续失败 %s 次，自动重试已暂停\n' "$BACKOFF_FAILURE_COUNT"
        if [[ -n "$BACKOFF_ERROR_SUMMARY" ]]; then
          printf '失败原因：%s\n' "$BACKOFF_ERROR_SUMMARY"
        fi
        print_retry_status_lines "$BACKOFF_FAILURE_COUNT" "$BACKOFF_NEXT_RETRY_AT" "true" "$BACKOFF_MAX_ATTEMPTS" "$BACKOFF_RETRY_POLICY"
        printf '日志位置：%s\n' "$LOG_PATH"
      } > "$RESULT_MD_PATH"
      write_result_json "paused" "同一批 PDF 连续失败，自动重试已暂停" "$NEW_PDF_COUNT" "" "0" "$BATCH_JSON_PATH" "[]" "backoff_paused" "completed" "" "$BACKOFF_NEXT_RETRY_AT"
      complete_run_status "paused" "completed" "automatic retries paused for current batch" "0" "backoff_paused" "$BATCH_JSON_PATH" "" "" "" "" "$BACKOFF_NEXT_RETRY_AT" "$NEW_PDF_COUNT"
      finish_with_result 0 "backoff-paused" "text" "$BATCH_JSON_PATH"
    fi

    {
      printf '【ZSXQ PDF 总结退避中】\n'
      printf '执行时间：%s\n' "$RUN_AT"
      printf '状态：paused\n'
      printf '结果：同一批 PDF 最近刚失败过，本轮先等待退避窗口\n'
      if [[ -n "$BACKOFF_ERROR_SUMMARY" ]]; then
        printf '失败原因：%s\n' "$BACKOFF_ERROR_SUMMARY"
      fi
      print_retry_status_lines "$BACKOFF_FAILURE_COUNT" "$BACKOFF_NEXT_RETRY_AT" "false" "$BACKOFF_MAX_ATTEMPTS" "$BACKOFF_RETRY_POLICY"
      printf '日志位置：%s\n' "$LOG_PATH"
    } > "$RESULT_MD_PATH"
    write_result_json "paused" "同一批 PDF 最近失败过，当前处于退避窗口" "$NEW_PDF_COUNT" "" "0" "$BATCH_JSON_PATH" "[]" "backoff_cooldown" "completed" "" "$BACKOFF_NEXT_RETRY_AT"
    complete_run_status "paused" "completed" "waiting for retry cooldown window" "0" "backoff_cooldown" "$BATCH_JSON_PATH" "" "" "" "" "$BACKOFF_NEXT_RETRY_AT" "$NEW_PDF_COUNT"
    finish_with_result 0 "backoff-cooldown" "text" "$BATCH_JSON_PATH"
  fi
fi

write_run_status "running" "preflight" "running task preflight" "" "preflight" "$BATCH_JSON_PATH" "" "" "" "" "" "$NEW_PDF_COUNT"
PREFLIGHT_OK="$(run_task_preflight)"
if [[ "$PREFLIGHT_OK" != "true" ]]; then
  PREFLIGHT_SUMMARY="$(summarize_preflight_report)"
  {
    printf '【ZSXQ PDF 总结失败】\n'
    printf '执行时间：%s\n' "$RUN_AT"
    printf '状态：env_failed\n'
    printf '新增 PDF 数量：%s\n' "$NEW_PDF_COUNT"
    printf '失败原因：系统环境预检未通过\n'
    printf '预检摘要：%s\n' "$PREFLIGHT_SUMMARY"
    printf '预检报告：%s\n' "$PREFLIGHT_JSON_PATH"
    printf '日志位置：%s\n' "$LOG_PATH"
  } > "$RESULT_MD_PATH"
  write_result_json "env_failed" "系统环境预检未通过" "$NEW_PDF_COUNT" "" "1" "$BATCH_JSON_PATH" "[]" "preflight_failed" "completed"
  complete_run_status "env_failed" "completed" "task preflight failed" "1" "preflight_failed" "$BATCH_JSON_PATH" "" "" "" "" "" "$NEW_PDF_COUNT"
  finish_with_result 1 "preflight-failed" "text" "$BATCH_JSON_PATH"
fi
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 预检通过：$(summarize_preflight_report)" >> "$LOG_PATH"

if [[ ! -f "$HELPER_PATH" ]]; then
  FAILURE_BACKOFF_NOTE="$(record_failure_backoff_note "$BATCH_JSON_PATH" "缺少批次辅助脚本")"
  {
    printf '【ZSXQ PDF 总结失败】\n'
    printf '执行时间：%s\n' "$RUN_AT"
    printf '状态：failed\n'
    printf '新增 PDF 数量：%s\n' "$NEW_PDF_COUNT"
    printf '失败原因：缺少批次辅助脚本 %s\n' "$HELPER_PATH"
    if [[ -n "$FAILURE_BACKOFF_NOTE" ]]; then
      printf '自动退避：%s\n' "$FAILURE_BACKOFF_NOTE"
    fi
    printf '日志位置：%s\n' "$LOG_PATH"
  } > "$RESULT_MD_PATH"
  write_result_json "failed" "缺少批次辅助脚本" "$NEW_PDF_COUNT" "" "1" "$BATCH_JSON_PATH" "[]" "dependency_missing" "completed"
  complete_run_status "failed" "completed" "helper script missing" "1" "dependency_missing" "$BATCH_JSON_PATH" "" "" "" "" "" "$NEW_PDF_COUNT"
  finish_with_result 1 "dependency-missing-helper" "text" "$BATCH_JSON_PATH"
fi

if [[ ! -f "$SUMMARY_SYSTEM_PROMPT_PATH" ]]; then
  FAILURE_BACKOFF_NOTE="$(record_failure_backoff_note "$BATCH_JSON_PATH" "缺少摘要系统提示文件")"
  {
    printf '【ZSXQ PDF 总结失败】\n'
    printf '执行时间：%s\n' "$RUN_AT"
    printf '状态：failed\n'
    printf '新增 PDF 数量：%s\n' "$NEW_PDF_COUNT"
    printf '失败原因：缺少摘要系统提示文件 %s\n' "$SUMMARY_SYSTEM_PROMPT_PATH"
    if [[ -n "$FAILURE_BACKOFF_NOTE" ]]; then
      printf '自动退避：%s\n' "$FAILURE_BACKOFF_NOTE"
    fi
    printf '日志位置：%s\n' "$LOG_PATH"
  } > "$RESULT_MD_PATH"
  write_result_json "failed" "缺少摘要系统提示文件" "$NEW_PDF_COUNT" "" "1" "$BATCH_JSON_PATH" "[]" "dependency_missing" "completed"
  complete_run_status "failed" "completed" "summary system prompt missing" "1" "dependency_missing" "$BATCH_JSON_PATH" "" "" "" "" "" "$NEW_PDF_COUNT"
  finish_with_result 1 "dependency-missing-summary-system-prompt" "text" "$BATCH_JSON_PATH"
fi

if [[ ! -f "$EXTRACT_TEXT_SCRIPT_PATH" ]]; then
  FAILURE_BACKOFF_NOTE="$(record_failure_backoff_note "$BATCH_JSON_PATH" "缺少文本提取脚本")"
  {
    printf '【ZSXQ PDF 总结失败】\n'
    printf '执行时间：%s\n' "$RUN_AT"
    printf '状态：failed\n'
    printf '新增 PDF 数量：%s\n' "$NEW_PDF_COUNT"
    printf '失败原因：缺少文本提取脚本 %s\n' "$EXTRACT_TEXT_SCRIPT_PATH"
    if [[ -n "$FAILURE_BACKOFF_NOTE" ]]; then
      printf '自动退避：%s\n' "$FAILURE_BACKOFF_NOTE"
    fi
    printf '日志位置：%s\n' "$LOG_PATH"
  } > "$RESULT_MD_PATH"
  write_result_json "failed" "缺少文本提取脚本" "$NEW_PDF_COUNT" "" "1" "$BATCH_JSON_PATH" "[]" "dependency_missing" "completed"
  complete_run_status "failed" "completed" "extract text script missing" "1" "dependency_missing" "$BATCH_JSON_PATH" "" "" "" "" "" "$NEW_PDF_COUNT"
  finish_with_result 1 "dependency-missing-extract-script" "text" "$BATCH_JSON_PATH"
fi

TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/zsxq_pdf_digest.XXXXXX")"
ORIGINAL_BATCH_JSON_PATH="$TEMP_DIR/original_batch.json"
CHUNK_DIR="$TEMP_DIR/chunks"
cp "$BATCH_JSON_PATH" "$ORIGINAL_BATCH_JSON_PATH"
WORKFLOW_RETRY_VERSION="$(workflow_retry_version)"
export ZSXQ_SUMMARY_CACHE_VERSION="summary-v2:${WORKFLOW_RETRY_VERSION}"
DISCOVERED_PDF_COUNT="$NEW_PDF_COUNT"

if [[ "$MANUAL_MODE" != "true" ]]; then
  FILTERED_BATCH_JSON_PATH="$TEMP_DIR/retry_eligible_batch.json"
  FILTER_STATUS_JSON_PATH="$TEMP_DIR/retry_filter_status.json"
  "$PYTHON_BIN" "$HELPER_PATH" filter-stage-retries \
    --batch-file "$ORIGINAL_BATCH_JSON_PATH" \
    --ledger-file "$STAGE_RETRY_LEDGER_PATH" \
    --output "$FILTERED_BATCH_JSON_PATH" \
    --stage "any" \
    --run-at "$RUN_AT" \
    --workflow-version "$WORKFLOW_RETRY_VERSION" > "$FILTER_STATUS_JSON_PATH"
  DEFERRED_RETRY_FILE_COUNT="$(json_get_value "$FILTER_STATUS_JSON_PATH" "deferred_count")"
  FILE_RETRY_NEXT_AT="$(json_get_value "$FILTER_STATUS_JSON_PATH" "next_retry_at")"
  FILE_RETRY_ALL_TERMINAL_BLOCKED="$(json_get_value "$FILTER_STATUS_JSON_PATH" "all_deferred_terminal_blocked")"
  cp "$FILTERED_BATCH_JSON_PATH" "$ORIGINAL_BATCH_JSON_PATH"
  NEW_PDF_COUNT="$(extract_batch_count "$ORIGINAL_BATCH_JSON_PATH")"

  if [[ "$NEW_PDF_COUNT" -eq 0 ]]; then
    if [[ "$FILE_RETRY_ALL_TERMINAL_BLOCKED" == "True" && -z "$FILE_RETRY_NEXT_AT" ]]; then
      finish_blocked_release "$ORIGINAL_BATCH_JSON_PATH" "$DISCOVERED_PDF_COUNT" "all pending files are retry_exhausted or blocked_release; manual release recovery is required" "false"
    fi
    {
      printf '知识星球研报总结：当前无可执行文件\n'
      printf '执行时间：%s\n' "$DISPLAY_RUN_AT"
      printf '发现：%s 篇；逐文件策略暂缓：%s 篇\n' "$DISCOVERED_PDF_COUNT" "$DEFERRED_RETRY_FILE_COUNT"
      if [[ -n "$FILE_RETRY_NEXT_AT" ]]; then
        printf '下一次可重试：%s\n' "$(format_display_time "$FILE_RETRY_NEXT_AT")"
      fi
    } > "$RESULT_MD_PATH"
    write_result_json "waiting" "当前文件均处于逐文件重试冷却或待转换状态" "$DISCOVERED_PDF_COUNT" "" "0" "$ORIGINAL_BATCH_JSON_PATH" "[]" "waiting_file_retry" "waiting_file_retry" "per_file_retry" "$FILE_RETRY_NEXT_AT"
    complete_run_status "waiting" "waiting_file_retry" "all pending files are deferred by per-file retry policy" "0" "waiting_file_retry" "$ORIGINAL_BATCH_JSON_PATH" "" "" "" "per_file_retry" "$FILE_RETRY_NEXT_AT" "$DISCOVERED_PDF_COUNT" "$DISCOVERED_PDF_COUNT"
    finish_with_result 0 "waiting-file-retry" "text" "$ORIGINAL_BATCH_JSON_PATH"
  fi
fi

if [[ "$RESET_AGENT_SESSION_ON_RUN" == "true" && "$DRY_RUN" != "true" ]]; then
  if summary_parallel_should_run; then
    for ((worker_index = 1; worker_index <= SUMMARY_WORKER_COUNT; worker_index++)); do
      worker_agent_id="$(summary_agent_id_for_worker "$worker_index")"
      worker_sessions_dir="$(summary_sessions_dir_for_agent "$worker_agent_id")"
      SESSION_RESET_OUTPUT="$(reset_agent_session "$worker_sessions_dir" "$worker_agent_id")"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 已重置摘要 worker agent 会话（worker=${worker_index} agent=${worker_agent_id}）：${SESSION_RESET_OUTPUT}" >> "$LOG_PATH"
    done
  else
    SESSION_RESET_OUTPUT="$(reset_agent_session "$SUMMARY_AGENT_SESSIONS_DIR" "$SUMMARY_AGENT_ID")"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 已重置摘要 agent 会话：${SESSION_RESET_OUTPUT}" >> "$LOG_PATH"
  fi
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 发现 ${NEW_PDF_COUNT} 个新增 PDF，开始分批处理，chunk_size=${BATCH_CHUNK_SIZE}，summary_agent=${SUMMARY_AGENT_ID}，parallel=${SUMMARY_PARALLEL_ENABLED}，workers=${SUMMARY_WORKER_COUNT}" >> "$LOG_PATH"
write_run_status "running" "split_batch" "splitting pending batch into chunks" "" "split_batch" "$ORIGINAL_BATCH_JSON_PATH" "" "" "" "" "" "$NEW_PDF_COUNT"
"$PYTHON_BIN" "$HELPER_PATH" split \
  --batch-file "$ORIGINAL_BATCH_JSON_PATH" \
  --output-dir "$CHUNK_DIR" \
  --chunk-size "$BATCH_CHUNK_SIZE" >> "$LOG_PATH"

shopt -s nullglob
CHUNK_FILES=("$CHUNK_DIR"/chunk-*.json)
shopt -u nullglob
CHUNK_TOTAL="${#CHUNK_FILES[@]}"

if [[ "$CHUNK_TOTAL" -eq 0 ]]; then
  FAILURE_BACKOFF_NOTE="$(record_failure_backoff_note "$ORIGINAL_BATCH_JSON_PATH" "分批结果为空")"
  {
    printf '【ZSXQ PDF 总结失败】\n'
    printf '执行时间：%s\n' "$RUN_AT"
    printf '状态：failed\n'
    printf '新增 PDF 数量：%s\n' "$NEW_PDF_COUNT"
    printf '失败原因：分批结果为空\n'
    if [[ -n "$FAILURE_BACKOFF_NOTE" ]]; then
      printf '自动退避：%s\n' "$FAILURE_BACKOFF_NOTE"
    fi
    printf '日志位置：%s\n' "$LOG_PATH"
  } > "$RESULT_MD_PATH"
  write_result_json "failed" "分批结果为空" "$NEW_PDF_COUNT" "" "1" "$ORIGINAL_BATCH_JSON_PATH" "[]" "split_failed" "completed"
  complete_run_status "failed" "completed" "split batch produced no chunks" "1" "split_failed" "$ORIGINAL_BATCH_JSON_PATH" "" "" "" "" "" "$NEW_PDF_COUNT"
  finish_with_result 1 "split-failed" "text" "$ORIGINAL_BATCH_JSON_PATH"
fi

DOC_GROUP_SIZE="${DOC_GROUP_SIZE:-10}"
DOC_GROUP_THRESHOLD="${DOC_GROUP_THRESHOLD:-15}"
SEND_PROGRESS_EACH_FILE="${SEND_PROGRESS_EACH_FILE:-false}"
INCREMENTAL_PUBLISH_ENABLED="${INCREMENTAL_PUBLISH_ENABLED:-true}"
EXPECTED_DOC_COUNT=1
if [[ "$NEW_PDF_COUNT" -gt "$DOC_GROUP_THRESHOLD" ]]; then
  EXPECTED_DOC_COUNT=$(((NEW_PDF_COUNT + DOC_GROUP_SIZE - 1) / DOC_GROUP_SIZE))
fi

# 启动、等待、冷却和逐文件进度保持静默；每份文档发布成功后即时通知，
# 整轮结束后再发送成功、部分完成或失败的终态汇总。

CURRENT_DOC_URL=""
LAST_DOC_URL_SEEN=""
SAME_DAY_RESUME_ATTEMPTED="false"
PROCESSED_FILE_COUNT=0
SUMMARY_READY_FILE_COUNT=0
FAILED_FILE_COUNT=0
FAILURE_REASON=""
FAILURE_EXIT_CODE=1
FAILED_CHUNK_INDEX=0
FAILED_FILES_SUMMARY=""
PROCESSED_ACK_BATCH_PATH=""
CURRENT_GROUP_FILE_COUNT=0
CURRENT_GROUP_CHUNK_JSONS=()
GROUP_INDEX=1
GROUP_REPORTS=()
QUARANTINED_CHUNK_JSONS=()
QUARANTINED_FILE_COUNT=0
DRY_RUN_READY_FILE_COUNT=0
FATAL_ENV_FAILURE="false"
FATAL_ENV_FAILURE_SUMMARY=""
FATAL_RELEASE_CONTRACT_MISMATCH="false"

SUCCESSFUL_CHUNK_JSONS=()
PUBLISH_READY_CHUNK_JSONS=()
PUBLISH_READY_FILE_COUNT=0
PUBLISH_FLUSH_INDEX=0
CHUNK_USAGE_JSONS=()

if summary_parallel_should_run; then
  ACTIVE_WORKERS_DIR="$TEMP_DIR/active_workers"
  SUMMARY_JOB_DIR="$TEMP_DIR/summary_jobs"
  mkdir -p "$ACTIVE_WORKERS_DIR" "$SUMMARY_JOB_DIR"
  SUMMARY_ACTIVE_PIDS=()
  SUMMARY_ACTIVE_MANIFESTS=()
  SUMMARY_ACTIVE_WORKER_IDS=()
  PENDING_SUMMARY_MANIFESTS=()
  PENDING_SUMMARY_FILE_COUNT=0
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 启用并发摘要 worker：count=${SUMMARY_WORKER_COUNT} agent_prefix=${SUMMARY_WORKER_AGENT_ID_PREFIX}" >> "$LOG_PATH"

  for chunk_json_path in "${CHUNK_FILES[@]}"; do
    if [[ "$FATAL_ENV_FAILURE" == "true" ]]; then
      break
    fi

    chunk_index="$(json_get_value "$chunk_json_path" "chunk_index")"
    chunk_file_count="$(extract_batch_count "$chunk_json_path")"
    current_chunk_file="$(extract_first_filename "$chunk_json_path")"
    chunk_name="$(basename "${chunk_json_path%.json}")"
    chunk_text_ready_path="$TEMP_DIR/${chunk_name}.text-ready.json"
    chunk_text_dir="$TEMP_DIR/${chunk_name}.texts"
    chunk_summary_cache_status_path="$TEMP_DIR/${chunk_name}.summary-cache.json"
    chunk_summary_prompt_path="$TEMP_DIR/${chunk_name}.summary.prompt.md"
    chunk_summary_result_path="$TEMP_DIR/${chunk_name}.summary.result.md"
    chunk_summary_validation_path="$TEMP_DIR/${chunk_name}.summary.validation.json"
    chunk_summary_json_path="$TEMP_DIR/${chunk_name}.summary.json"
    chunk_summary_markdown_path="$TEMP_DIR/${chunk_name}.summary.md"
    chunk_summary_inspect_path="$TEMP_DIR/${chunk_name}.summary.inspect.json"
    chunk_summary_manifest_path="$SUMMARY_JOB_DIR/${chunk_name}.summary.manifest.json"

    extract_stage_success="false"
    extract_failure_reason=""
    extract_failure_is_content="false"
    extract_failure_has_env="false"
    extract_retryable_failure_count=1
    extract_attempt=1
    extract_max_attempts=$((TEXT_EXTRACT_RETRY_COUNT + 1))

    while [[ "$extract_attempt" -le "$extract_max_attempts" ]]; do
      write_run_status "running" "text_extract" "extracting text for current chunk" "" "text_extract" "$chunk_json_path" "$chunk_index" "$CHUNK_TOTAL" "$current_chunk_file" "" "" "$NEW_PDF_COUNT"
      start_heartbeat "running" "text_extract" "extracting text for current chunk" "" "text_extract" "$chunk_json_path" "$chunk_index" "$CHUNK_TOTAL" "$current_chunk_file" "" "" "$NEW_PDF_COUNT"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批开始文本提取，第 ${extract_attempt}/${extract_max_attempts} 次尝试" >> "$LOG_PATH"
      if [[ "$extract_attempt" -eq 1 ]]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批尝试 MarkItDown/clean.md 预处理" >> "$LOG_PATH"
        run_markdown_preprocess "$chunk_json_path"
      fi
      record_text_extract_started_events "$chunk_json_path"
      set +e
      OCR_TEXT_MAX_CHARS="${OCR_TEXT_MAX_CHARS:-120000}" \
      TEXT_EXTRACT_MAX_CHARS="${TEXT_EXTRACT_MAX_CHARS:-${OCR_TEXT_MAX_CHARS:-120000}}" \
      LOCAL_OCR_FALLBACK_ENABLE="${LOCAL_OCR_FALLBACK_ENABLE:-true}" \
      TEXT_EXTRACT_CACHE_DIR="$TEXT_CACHE_DIR_PATH" \
      "$PYTHON_BIN" "$EXTRACT_TEXT_SCRIPT_PATH" --batch-file "$chunk_json_path" --output-dir "$chunk_text_dir" >> "$LOG_PATH" 2>&1
      EXTRACT_RC=$?
      set -e
      stop_heartbeat

      if [[ "$EXTRACT_RC" -ne 0 ]]; then
        extract_failure_reason="文本提取脚本异常退出（rc=${EXTRACT_RC}）"
      else
        TEXT_READY_OUTPUT="$(check_chunk_text_ready "$chunk_json_path")"
        printf '%s\n' "$TEXT_READY_OUTPUT" > "$chunk_text_ready_path"
        TEXT_READY_OK="$(json_get_value "$chunk_text_ready_path" "ok")"
        if [[ "$TEXT_READY_OK" == "True" || "$TEXT_READY_OK" == "true" ]]; then
          extract_stage_success="true"
          break
        fi
        extract_failure_reason="$(json_get_value "$chunk_text_ready_path" "message")"
        TEXT_READY_CONTENT_ONLY="$(json_get_value "$chunk_text_ready_path" "all_nonretryable_content_failures")"
        TEXT_READY_HAS_ENV="$(json_get_value "$chunk_text_ready_path" "has_env_failure")"
        extract_retryable_failure_count="$(json_get_value "$chunk_text_ready_path" "retryable_failure_count")"
        if [[ ! "$extract_retryable_failure_count" =~ ^[0-9]+$ ]]; then
          extract_retryable_failure_count=0
        fi
        if [[ "$TEXT_READY_CONTENT_ONLY" == "True" || "$TEXT_READY_CONTENT_ONLY" == "true" ]]; then
          extract_failure_is_content="true"
        fi
        if [[ "$TEXT_READY_HAS_ENV" == "True" || "$TEXT_READY_HAS_ENV" == "true" ]]; then
          extract_failure_has_env="true"
        fi
      fi

      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批文本提取失败：${extract_failure_reason}" >> "$LOG_PATH"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 文本提取诊断：" >> "$LOG_PATH"
      extract_text_diagnostics_lines "$chunk_json_path" >> "$LOG_PATH"

      if [[ "$extract_retryable_failure_count" -eq 0 ]]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批为不可重试内容失败，本轮不做盲重试" >> "$LOG_PATH"
        break
      fi

      if [[ "$extract_attempt" -lt "$extract_max_attempts" ]]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批准备重试文本提取" >> "$LOG_PATH"
      fi
      extract_attempt=$((extract_attempt + 1))
    done

    if [[ "$extract_stage_success" != "true" ]]; then
      FAILURE_REASON="$extract_failure_reason"
      FAILURE_EXIT_CODE=1
      FAILED_CHUNK_INDEX="$chunk_index"
      FAILED_FILE_COUNT=$((FAILED_FILE_COUNT + chunk_file_count))
      if [[ -n "$FAILED_FILES_SUMMARY" ]]; then
        FAILED_FILES_SUMMARY+=$'\n'
      fi
      FAILED_FILES_SUMMARY+="第 ${chunk_index}/${CHUNK_TOTAL} 批失败：${FAILURE_REASON}"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批跳过 agent，原因：${FAILURE_REASON}" >> "$LOG_PATH"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 本批失败文件：" >> "$LOG_PATH"
      extract_file_lines "$chunk_json_path" >> "$LOG_PATH"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 本批提取诊断：" >> "$LOG_PATH"
      extract_text_diagnostics_lines "$chunk_json_path" >> "$LOG_PATH"
      record_batch_event "$chunk_json_path" "text_extract_failed" "$extract_failure_reason" ""
      if [[ "$DRY_RUN" != "true" && "$MANUAL_MODE" != "true" && "$extract_failure_is_content" == "true" ]]; then
        record_stage_retry_for_batch "$chunk_json_path" "text_extract"
        record_batch_index_status "$chunk_json_path" "needs_transform" "$extract_failure_reason"
        record_batch_event "$chunk_json_path" "needs_transform" "$extract_failure_reason" ""
        quarantine_chunk_failures "$chunk_json_path"
        QUARANTINED_CHUNK_JSONS+=("$chunk_json_path")
        QUARANTINED_FILE_COUNT=$((QUARANTINED_FILE_COUNT + chunk_file_count))
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批已加入隔离区，不再继续自动重试当前文件" >> "$LOG_PATH"
      elif [[ "$DRY_RUN" != "true" && "$MANUAL_MODE" != "true" ]]; then
        if [[ "$extract_failure_has_env" == "true" ]]; then
          record_stage_retry_for_batch "$chunk_json_path" "text_extract" "text_extract_env_failure" "env_failure" "true" "$extract_failure_reason"
        else
          record_stage_retry_for_batch "$chunk_json_path" "text_extract" "text_extract_failed" "transient_failure" "true" "$extract_failure_reason"
        fi
      fi
      if [[ "$extract_failure_has_env" == "true" ]]; then
        FATAL_ENV_FAILURE="true"
        FATAL_ENV_FAILURE_SUMMARY="$extract_failure_reason"
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 检测到环境型失败，停止本轮后续 chunk 处理" >> "$LOG_PATH"
        break
      fi
      continue
    fi

    record_text_extract_events "$chunk_json_path"
    resolve_stage_retry_for_batch "$chunk_json_path" "text_extract"

    set +e
    SUMMARY_CACHE_OUTPUT="$(materialize_cached_chunk_summary "$chunk_json_path" "$chunk_summary_json_path" "$chunk_summary_markdown_path" 2>&1)"
    SUMMARY_CACHE_RC=$?
    set -e

    if [[ "$SUMMARY_CACHE_RC" -eq 0 ]]; then
      printf '%s\n' "$SUMMARY_CACHE_OUTPUT" > "$chunk_summary_cache_status_path"
      SUMMARY_CACHE_OK="$(json_get_value "$chunk_summary_cache_status_path" "ok" || true)"
      if [[ "$SUMMARY_CACHE_OK" == "True" || "$SUMMARY_CACHE_OK" == "true" ]]; then
        write_summary_result_manifest "$chunk_summary_manifest_path" "success" "$chunk_json_path" "$chunk_index" "$CHUNK_TOTAL" "$chunk_file_count" "$current_chunk_file" "$chunk_summary_json_path" "$chunk_summary_markdown_path" "" "$SUMMARY_AGENT_ID" "cache" "true" "" "0" "false"
        PENDING_SUMMARY_MANIFESTS+=("$chunk_summary_manifest_path")
        PENDING_SUMMARY_FILE_COUNT=$((PENDING_SUMMARY_FILE_COUNT + chunk_file_count))
        if [[ "$NEW_PDF_COUNT" -gt "$DOC_GROUP_THRESHOLD" && $((PUBLISH_READY_FILE_COUNT + PENDING_SUMMARY_FILE_COUNT)) -ge "$DOC_GROUP_SIZE" ]]; then
          if ! flush_pending_summary_results; then
            break
          fi
        fi
        continue
      fi
      SUMMARY_CACHE_REASON="$(json_get_value "$chunk_summary_cache_status_path" "reason" || true)"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批未命中本地摘要缓存：${SUMMARY_CACHE_REASON:-未知原因}" >> "$LOG_PATH"
    else
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批读取本地摘要缓存异常：${SUMMARY_CACHE_OUTPUT}" >> "$LOG_PATH"
    fi

    set +e
    SUMMARY_RENDER_OUTPUT="$(render_summary_prompt "$chunk_json_path" "$chunk_summary_prompt_path" 2>&1)"
    SUMMARY_RENDER_RC=$?
    set -e
    if [[ "$SUMMARY_RENDER_RC" -ne 0 ]]; then
      FAILURE_REASON="本地摘要提示词生成失败：$(summarize_failure_text "$SUMMARY_RENDER_OUTPUT")"
      FAILURE_EXIT_CODE=1
      FATAL_ENV_FAILURE="true"
      FATAL_ENV_FAILURE_SUMMARY="$FAILURE_REASON"
      FAILED_CHUNK_INDEX="$chunk_index"
      FAILED_FILE_COUNT=$((FAILED_FILE_COUNT + chunk_file_count))
      if [[ -n "$FAILED_FILES_SUMMARY" ]]; then
        FAILED_FILES_SUMMARY+=$'\n'
      fi
      FAILED_FILES_SUMMARY+="第 ${chunk_index}/${CHUNK_TOTAL} 批失败：${FAILURE_REASON}"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批无法生成本地摘要提示词：${FAILURE_REASON}" >> "$LOG_PATH"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 本批失败文件：" >> "$LOG_PATH"
      extract_file_lines "$chunk_json_path" >> "$LOG_PATH"
      break
    fi

    launch_summary_job "$chunk_json_path" "$chunk_index" "$CHUNK_TOTAL" "$current_chunk_file" "$chunk_file_count" "$chunk_summary_prompt_path" "$chunk_summary_result_path" "$chunk_summary_validation_path" "$chunk_summary_json_path" "$chunk_summary_markdown_path" "$chunk_summary_inspect_path" "$chunk_summary_manifest_path"
    PENDING_SUMMARY_MANIFESTS+=("$chunk_summary_manifest_path")
    PENDING_SUMMARY_FILE_COUNT=$((PENDING_SUMMARY_FILE_COUNT + chunk_file_count))

    if [[ "$NEW_PDF_COUNT" -gt "$DOC_GROUP_THRESHOLD" && $((PUBLISH_READY_FILE_COUNT + PENDING_SUMMARY_FILE_COUNT)) -ge "$DOC_GROUP_SIZE" ]]; then
      if ! flush_pending_summary_results; then
        break
      fi
    fi
  done

  if ! flush_pending_summary_results; then
    :
  fi
else
for chunk_json_path in "${CHUNK_FILES[@]}"; do
  chunk_index="$(json_get_value "$chunk_json_path" "chunk_index")"
  chunk_file_count="$(extract_batch_count "$chunk_json_path")"
  current_chunk_file="$(extract_first_filename "$chunk_json_path")"
  chunk_name="$(basename "${chunk_json_path%.json}")"
  chunk_text_ready_path="$TEMP_DIR/${chunk_name}.text-ready.json"
  chunk_text_dir="$TEMP_DIR/${chunk_name}.texts"
  chunk_summary_cache_status_path="$TEMP_DIR/${chunk_name}.summary-cache.json"
  chunk_summary_prompt_path="$TEMP_DIR/${chunk_name}.summary.prompt.md"
  chunk_summary_result_path="$TEMP_DIR/${chunk_name}.summary.result.md"
  chunk_summary_validation_path="$TEMP_DIR/${chunk_name}.summary.validation.json"
  chunk_summary_json_path="$TEMP_DIR/${chunk_name}.summary.json"
  chunk_summary_markdown_path="$TEMP_DIR/${chunk_name}.summary.md"
  chunk_summary_inspect_path="$TEMP_DIR/${chunk_name}.summary.inspect.json"

  extract_stage_success="false"
  extract_failure_reason=""
  extract_failure_is_content="false"
  extract_failure_has_env="false"
  extract_retryable_failure_count=1
  extract_attempt=1
  extract_max_attempts=$((TEXT_EXTRACT_RETRY_COUNT + 1))

  while [[ "$extract_attempt" -le "$extract_max_attempts" ]]; do
    write_run_status "running" "text_extract" "extracting text for current chunk" "" "text_extract" "$chunk_json_path" "$chunk_index" "$CHUNK_TOTAL" "$current_chunk_file" "" "" "$NEW_PDF_COUNT"
    start_heartbeat "running" "text_extract" "extracting text for current chunk" "" "text_extract" "$chunk_json_path" "$chunk_index" "$CHUNK_TOTAL" "$current_chunk_file" "" "" "$NEW_PDF_COUNT"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批开始文本提取，第 ${extract_attempt}/${extract_max_attempts} 次尝试" >> "$LOG_PATH"
    if [[ "$extract_attempt" -eq 1 ]]; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批尝试 MarkItDown/clean.md 预处理" >> "$LOG_PATH"
      run_markdown_preprocess "$chunk_json_path"
    fi
    record_text_extract_started_events "$chunk_json_path"
    set +e
    OCR_TEXT_MAX_CHARS="${OCR_TEXT_MAX_CHARS:-120000}" \
    TEXT_EXTRACT_MAX_CHARS="${TEXT_EXTRACT_MAX_CHARS:-${OCR_TEXT_MAX_CHARS:-120000}}" \
    LOCAL_OCR_FALLBACK_ENABLE="${LOCAL_OCR_FALLBACK_ENABLE:-true}" \
    TEXT_EXTRACT_CACHE_DIR="$TEXT_CACHE_DIR_PATH" \
    "$PYTHON_BIN" "$EXTRACT_TEXT_SCRIPT_PATH" --batch-file "$chunk_json_path" --output-dir "$chunk_text_dir" >> "$LOG_PATH" 2>&1
    EXTRACT_RC=$?
    set -e
    stop_heartbeat

    if [[ "$EXTRACT_RC" -ne 0 ]]; then
      extract_failure_reason="文本提取脚本异常退出（rc=${EXTRACT_RC}）"
    else
      TEXT_READY_OUTPUT="$(check_chunk_text_ready "$chunk_json_path")"
      printf '%s\n' "$TEXT_READY_OUTPUT" > "$chunk_text_ready_path"
      TEXT_READY_OK="$(json_get_value "$chunk_text_ready_path" "ok")"
      if [[ "$TEXT_READY_OK" == "True" || "$TEXT_READY_OK" == "true" ]]; then
        extract_stage_success="true"
        break
      fi
      extract_failure_reason="$(json_get_value "$chunk_text_ready_path" "message")"
      TEXT_READY_CONTENT_ONLY="$(json_get_value "$chunk_text_ready_path" "all_nonretryable_content_failures")"
      TEXT_READY_HAS_ENV="$(json_get_value "$chunk_text_ready_path" "has_env_failure")"
      extract_retryable_failure_count="$(json_get_value "$chunk_text_ready_path" "retryable_failure_count")"
      if [[ ! "$extract_retryable_failure_count" =~ ^[0-9]+$ ]]; then
        extract_retryable_failure_count=0
      fi
      if [[ "$TEXT_READY_CONTENT_ONLY" == "True" || "$TEXT_READY_CONTENT_ONLY" == "true" ]]; then
        extract_failure_is_content="true"
      fi
      if [[ "$TEXT_READY_HAS_ENV" == "True" || "$TEXT_READY_HAS_ENV" == "true" ]]; then
        extract_failure_has_env="true"
      fi
    fi

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批文本提取失败：${extract_failure_reason}" >> "$LOG_PATH"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 文本提取诊断：" >> "$LOG_PATH"
    extract_text_diagnostics_lines "$chunk_json_path" >> "$LOG_PATH"

    if [[ "$extract_retryable_failure_count" -eq 0 ]]; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批为不可重试内容失败，本轮不做盲重试" >> "$LOG_PATH"
      break
    fi

    if [[ "$extract_attempt" -lt "$extract_max_attempts" ]]; then
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批准备重试文本提取" >> "$LOG_PATH"
    fi
    extract_attempt=$((extract_attempt + 1))
  done

  if [[ "$extract_stage_success" != "true" ]]; then
    FAILURE_REASON="$extract_failure_reason"
    FAILURE_EXIT_CODE=1
    FAILED_CHUNK_INDEX="$chunk_index"
    FAILED_FILE_COUNT=$((FAILED_FILE_COUNT + chunk_file_count))
    if [[ -n "$FAILED_FILES_SUMMARY" ]]; then
      FAILED_FILES_SUMMARY+=$'\n'
    fi
    FAILED_FILES_SUMMARY+="第 ${chunk_index}/${CHUNK_TOTAL} 批失败：${FAILURE_REASON}"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批跳过 agent，原因：${FAILURE_REASON}" >> "$LOG_PATH"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 本批失败文件：" >> "$LOG_PATH"
    extract_file_lines "$chunk_json_path" >> "$LOG_PATH"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 本批提取诊断：" >> "$LOG_PATH"
    extract_text_diagnostics_lines "$chunk_json_path" >> "$LOG_PATH"
    record_batch_event "$chunk_json_path" "text_extract_failed" "$extract_failure_reason" ""
    if [[ "$DRY_RUN" != "true" && "$MANUAL_MODE" != "true" && "$extract_failure_is_content" == "true" ]]; then
      record_stage_retry_for_batch "$chunk_json_path" "text_extract"
      record_batch_index_status "$chunk_json_path" "needs_transform" "$extract_failure_reason"
      record_batch_event "$chunk_json_path" "needs_transform" "$extract_failure_reason" ""
      quarantine_chunk_failures "$chunk_json_path"
      QUARANTINED_CHUNK_JSONS+=("$chunk_json_path")
      QUARANTINED_FILE_COUNT=$((QUARANTINED_FILE_COUNT + chunk_file_count))
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批已加入隔离区，不再继续自动重试当前文件" >> "$LOG_PATH"
    elif [[ "$DRY_RUN" != "true" && "$MANUAL_MODE" != "true" ]]; then
      if [[ "$extract_failure_has_env" == "true" ]]; then
        record_stage_retry_for_batch "$chunk_json_path" "text_extract" "text_extract_env_failure" "env_failure" "true" "$extract_failure_reason"
      else
        record_stage_retry_for_batch "$chunk_json_path" "text_extract" "text_extract_failed" "transient_failure" "true" "$extract_failure_reason"
      fi
    fi
    if [[ "$extract_failure_has_env" == "true" ]]; then
      FATAL_ENV_FAILURE="true"
      FATAL_ENV_FAILURE_SUMMARY="$extract_failure_reason"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 检测到环境型失败，停止本轮后续 chunk 处理" >> "$LOG_PATH"
      break
    fi
    continue
  fi

  record_text_extract_events "$chunk_json_path"
  resolve_stage_retry_for_batch "$chunk_json_path" "text_extract"

  summary_stage_success="false"
  summary_stage_from_cache="false"

  set +e
  SUMMARY_CACHE_OUTPUT="$(materialize_cached_chunk_summary "$chunk_json_path" "$chunk_summary_json_path" "$chunk_summary_markdown_path" 2>&1)"
  SUMMARY_CACHE_RC=$?
  set -e

  if [[ "$SUMMARY_CACHE_RC" -eq 0 ]]; then
    printf '%s\n' "$SUMMARY_CACHE_OUTPUT" > "$chunk_summary_cache_status_path"
    SUMMARY_CACHE_OK="$(json_get_value "$chunk_summary_cache_status_path" "ok" || true)"
    if [[ "$SUMMARY_CACHE_OK" == "True" || "$SUMMARY_CACHE_OK" == "true" ]]; then
      summary_stage_success="true"
      summary_stage_from_cache="true"
      SUMMARY_READY_FILE_COUNT=$((SUMMARY_READY_FILE_COUNT + chunk_file_count))
      record_summary_metadata "$chunk_json_path"
      resolve_stage_retry_for_batch "$chunk_json_path" "summary"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批命中本地摘要缓存" >> "$LOG_PATH"
      if [[ "$DRY_RUN" == "true" ]]; then
        DRY_RUN_READY_FILE_COUNT=$((DRY_RUN_READY_FILE_COUNT + chunk_file_count))
      fi
    else
      SUMMARY_CACHE_REASON="$(json_get_value "$chunk_summary_cache_status_path" "reason" || true)"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批未命中本地摘要缓存：${SUMMARY_CACHE_REASON:-未知原因}" >> "$LOG_PATH"
    fi
  else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批读取本地摘要缓存异常：${SUMMARY_CACHE_OUTPUT}" >> "$LOG_PATH"
  fi

  if [[ "$summary_stage_success" != "true" ]]; then
    set +e
    SUMMARY_RENDER_OUTPUT="$(render_summary_prompt "$chunk_json_path" "$chunk_summary_prompt_path" 2>&1)"
    SUMMARY_RENDER_RC=$?
    set -e
    if [[ "$SUMMARY_RENDER_RC" -ne 0 ]]; then
      FAILURE_REASON="本地摘要提示词生成失败：$(summarize_failure_text "$SUMMARY_RENDER_OUTPUT")"
      FAILURE_EXIT_CODE=1
      FATAL_ENV_FAILURE="true"
      FATAL_ENV_FAILURE_SUMMARY="$FAILURE_REASON"
      FAILED_CHUNK_INDEX="$chunk_index"
      FAILED_FILE_COUNT=$((FAILED_FILE_COUNT + chunk_file_count))
      if [[ -n "$FAILED_FILES_SUMMARY" ]]; then
        FAILED_FILES_SUMMARY+=$'\n'
      fi
      FAILED_FILES_SUMMARY+="第 ${chunk_index}/${CHUNK_TOTAL} 批失败：${FAILURE_REASON}"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批无法生成本地摘要提示词：${FAILURE_REASON}" >> "$LOG_PATH"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 本批失败文件：" >> "$LOG_PATH"
      extract_file_lines "$chunk_json_path" >> "$LOG_PATH"
      break
    fi

    if [[ "$DRY_RUN" == "true" ]]; then
      DRY_RUN_READY_FILE_COUNT=$((DRY_RUN_READY_FILE_COUNT + chunk_file_count))
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批 dry-run 到摘要提示词生成结束，未调用 agent" >> "$LOG_PATH"
      continue
    fi

    summary_attempt=1
    summary_general_retries_left=$((CHUNK_RETRY_COUNT))
    summary_timeout_retries_left=$((SUMMARY_TIMEOUT_RETRY_COUNT))
    summary_max_attempts=$((1 + summary_general_retries_left + summary_timeout_retries_left))

    while [[ "$summary_attempt" -le "$summary_max_attempts" ]]; do
      write_run_status "running" "local_summary" "generating local summary for current chunk" "" "local_summary" "$chunk_json_path" "$chunk_index" "$CHUNK_TOTAL" "$current_chunk_file" "" "" "$NEW_PDF_COUNT"
      start_heartbeat "running" "local_summary" "generating local summary for current chunk" "" "local_summary" "$chunk_json_path" "$chunk_index" "$CHUNK_TOTAL" "$current_chunk_file" "" "" "$NEW_PDF_COUNT"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 开始生成第 ${chunk_index}/${CHUNK_TOTAL} 批本地摘要，文件数=${chunk_file_count}，第 ${summary_attempt}/${summary_max_attempts} 次尝试" >> "$LOG_PATH"

      prepare_fresh_agent_session "本地摘要" "$chunk_index" "$CHUNK_TOTAL" "$SUMMARY_AGENT_SESSIONS_DIR" "$SUMMARY_AGENT_ID"
      PROMPT_TEXT="$(cat "$chunk_summary_prompt_path")"
      set +e
      run_agent_turn "$SUMMARY_AGENT_ID" "$SUMMARY_AGENT_THINKING" "$SUMMARY_AGENT_TIMEOUT_SECONDS" "$chunk_summary_result_path" "$PROMPT_TEXT"
      SUMMARY_AGENT_RC=$?
      set -e
      stop_heartbeat
      SUMMARY_AGENT_TIMED_OUT="false"
      if [[ "$SUMMARY_AGENT_RC" -eq 124 ]]; then
        SUMMARY_AGENT_TIMED_OUT="true"
      fi

      SUMMARY_INSPECT_OUTPUT="$(inspect_chunk_output "$chunk_summary_result_path" || true)"
      if [[ -n "$SUMMARY_INSPECT_OUTPUT" ]]; then
        printf '%s\n' "$SUMMARY_INSPECT_OUTPUT" > "$chunk_summary_inspect_path"
        python3 - "$chunk_summary_inspect_path" "$chunk_json_path" "$SUMMARY_AGENT_ID" <<'PY'
import json
import sys
from pathlib import Path

inspect_path = Path(sys.argv[1])
chunk_path = Path(sys.argv[2])
data = json.loads(inspect_path.read_text(encoding="utf-8"))
chunk = json.loads(chunk_path.read_text(encoding="utf-8"))
data["chunk_file"] = str(chunk_path)
data["chunk_index"] = int(chunk.get("chunk_index", 1))
data["agent_id"] = sys.argv[3]
inspect_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
      fi
      cleanup_agent_session_after_call "$SUMMARY_AGENT_SESSIONS_DIR" "$SUMMARY_AGENT_ID"

      if [[ "$SUMMARY_AGENT_RC" -ne 0 ]]; then
        AGENT_ERROR_SUMMARY="$(extract_agent_error_summary_from_file "$chunk_summary_result_path" || true)"
        if [[ "$SUMMARY_AGENT_TIMED_OUT" != "true" && "$AGENT_ERROR_SUMMARY" == local\ timeout\ after* ]]; then
          SUMMARY_AGENT_TIMED_OUT="true"
        fi
        if [[ "$SUMMARY_AGENT_TIMED_OUT" == "true" ]]; then
          FAILURE_REASON="本地摘要 agent 超时"
          if [[ -n "$AGENT_ERROR_SUMMARY" ]]; then
            FAILURE_REASON="本地摘要 agent 超时：${AGENT_ERROR_SUMMARY}"
          else
            FAILURE_REASON="本地摘要 agent 超时：local timeout after ${SUMMARY_AGENT_TIMEOUT_SECONDS}s"
          fi
        else
          FAILURE_REASON="本地摘要 agent 执行失败"
          if [[ -n "$AGENT_ERROR_SUMMARY" ]]; then
            FAILURE_REASON="本地摘要 agent 执行失败：${AGENT_ERROR_SUMMARY}"
          fi
        fi
        FAILURE_EXIT_CODE="$SUMMARY_AGENT_RC"
        SUMMARY_FAILURE_SUMMARY="$(summarize_failure_text "$FAILURE_REASON")"

        if [[ "$SUMMARY_FAILURE_SUMMARY" == "登录令牌坏了，需要重新登录 OpenAI Codex" ]]; then
          FATAL_ENV_FAILURE="true"
          FATAL_ENV_FAILURE_SUMMARY="$FAILURE_REASON"
          echo "[$(date '+%Y-%m-%d %H:%M:%S')] 检测到登录令牌失效，停止本轮后续 chunk 处理" >> "$LOG_PATH"
          break
        fi

        if [[ "$SUMMARY_AGENT_TIMED_OUT" == "true" && "$summary_timeout_retries_left" -gt 0 ]]; then
          echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批本地摘要命中超时兜底，准备重试：${FAILURE_REASON}" >> "$LOG_PATH"
          summary_timeout_retries_left=$((summary_timeout_retries_left - 1))
          summary_attempt=$((summary_attempt + 1))
          sleep 5
          continue
        fi

        if [[ "$SUMMARY_AGENT_TIMED_OUT" != "true" && "$summary_general_retries_left" -gt 0 ]]; then
          echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批本地摘要失败，准备重试：${FAILURE_REASON}" >> "$LOG_PATH"
          summary_general_retries_left=$((summary_general_retries_left - 1))
          summary_attempt=$((summary_attempt + 1))
          sleep 5
          continue
        fi

        break
      fi

      set +e
      SUMMARY_VALIDATION_OUTPUT="$(validate_summary_result "$chunk_json_path" "$chunk_summary_result_path" 2>&1)"
      SUMMARY_VALIDATION_RC=$?
      set -e

      if [[ "$SUMMARY_VALIDATION_RC" -ne 0 ]]; then
        FAILURE_REASON="本地摘要校验失败：${SUMMARY_VALIDATION_OUTPUT}"
        FAILURE_EXIT_CODE=1

        if [[ "$summary_general_retries_left" -gt 0 ]]; then
          echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批本地摘要校验失败，准备重试：${FAILURE_REASON}" >> "$LOG_PATH"
          summary_general_retries_left=$((summary_general_retries_left - 1))
          summary_attempt=$((summary_attempt + 1))
          sleep 5
          continue
        fi

        break
      fi

      printf '%s\n' "$SUMMARY_VALIDATION_OUTPUT" > "$chunk_summary_validation_path"

      set +e
      PERSIST_SUMMARY_OUTPUT="$(persist_chunk_summary "$chunk_json_path" "$chunk_summary_result_path" "$chunk_summary_json_path" "$chunk_summary_markdown_path" 2>&1)"
      PERSIST_SUMMARY_RC=$?
      set -e

      if [[ "$PERSIST_SUMMARY_RC" -ne 0 ]]; then
        FAILURE_REASON="本地摘要落盘失败：${PERSIST_SUMMARY_OUTPUT}"
        FAILURE_EXIT_CODE=1
        break
      fi

      SUMMARY_READY_FILE_COUNT=$((SUMMARY_READY_FILE_COUNT + chunk_file_count))
      summary_stage_success="true"
      record_summary_metadata "$chunk_json_path"
      echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批本地摘要已落盘：${chunk_summary_markdown_path}" >> "$LOG_PATH"
      break
    done
  fi

  if [[ -f "$chunk_summary_inspect_path" ]]; then
    CHUNK_USAGE_JSONS+=("$chunk_summary_inspect_path")
  fi

  if [[ "$summary_stage_success" != "true" ]]; then
    FAILED_CHUNK_INDEX="$chunk_index"
    FAILED_FILE_COUNT=$((FAILED_FILE_COUNT + chunk_file_count))
    if [[ -n "$FAILED_FILES_SUMMARY" ]]; then
      FAILED_FILES_SUMMARY+=$'\n'
    fi
    FAILED_FILES_SUMMARY+="第 ${chunk_index}/${CHUNK_TOTAL} 批失败：${FAILURE_REASON}"
    record_batch_index_status "$chunk_json_path" "summary_failed" "$FAILURE_REASON"
    record_batch_event "$chunk_json_path" "summary_failed" "$FAILURE_REASON" ""
    if [[ "$FATAL_ENV_FAILURE" == "true" ]]; then
      record_stage_retry_for_batch "$chunk_json_path" "summary" "summary_env_failure" "env_failure" "true" "$FAILURE_REASON"
    else
      record_stage_retry_for_batch "$chunk_json_path" "summary" "summary_failed" "transient_failure" "true" "$FAILURE_REASON"
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批本地摘要最终失败：${FAILURE_REASON}" >> "$LOG_PATH"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 本批失败文件：" >> "$LOG_PATH"
    extract_file_lines "$chunk_json_path" >> "$LOG_PATH"
    if [[ "$FATAL_ENV_FAILURE" == "true" ]]; then
      break
    fi
    continue
  fi

  resolve_stage_retry_for_batch "$chunk_json_path" "summary"

  if [[ "$DRY_RUN" == "true" ]]; then
    continue
  fi

  PUBLISH_READY_CHUNK_JSONS+=("$chunk_json_path")
  PUBLISH_READY_FILE_COUNT=$((PUBLISH_READY_FILE_COUNT + chunk_file_count))
  echo "[$(date '+%Y-%m-%d %H:%M:%S')] 第 ${chunk_index}/${CHUNK_TOTAL} 批本地摘要已进入飞书发布队列，文件数=${chunk_file_count}" >> "$LOG_PATH"
  if ! publish_ready_chunks "false"; then
    break
  fi
done
fi

if [[ "$FATAL_ENV_FAILURE" != "true" ]]; then
  if ! publish_ready_chunks "true"; then
    :
  fi
fi

if [[ "$DRY_RUN" != "true" && "${PROCESSED_FILE_COUNT:-0}" -gt 0 ]]; then
  rebuild_obsidian_indexes_once || true
fi

if [[ "${#CHUNK_USAGE_JSONS[@]}" -gt 0 ]]; then
  write_usage_summary "$USAGE_JSON_PATH" "${CHUNK_USAGE_JSONS[@]}"
else
  write_usage_summary "$USAGE_JSON_PATH"
fi

if [[ "$DRY_RUN" == "true" ]]; then
  DRY_RUN_REMAINING_COUNT=$((NEW_PDF_COUNT - DRY_RUN_READY_FILE_COUNT))
  {
    printf '知识星球研报总结：🧪 Dry Run\n'
    printf '执行时间：%s\n' "$DISPLAY_RUN_AT"
    printf '结果：本轮只跑到文本提取、本地摘要缓存检查和摘要提示词生成，不会创建飞书文档，也不会发群消息\n'
    printf '可继续摘要批次：%s/%s\n' "$DRY_RUN_READY_FILE_COUNT" "$NEW_PDF_COUNT"
    if [[ "$DRY_RUN_REMAINING_COUNT" -gt 0 ]]; then
      printf '未通过文本准备的文件：%s\n' "$DRY_RUN_REMAINING_COUNT"
      printf '失败原因：%s\n' "$(summarize_failure_text "${FAILED_FILES_SUMMARY:-${FAILURE_REASON:-未知错误}}")"
    fi
    printf '预检报告：%s\n' "$PREFLIGHT_JSON_PATH"
    printf '日志位置：%s\n' "$LOG_PATH"
  } > "$RESULT_MD_PATH"
  write_result_json "dry_run" "dry run only" "$NEW_PDF_COUNT" "" "0" "$ORIGINAL_BATCH_JSON_PATH" "[]" "dry_run" "completed"
  complete_run_status "dry_run" "completed" "dry run completed" "0" "dry_run" "$ORIGINAL_BATCH_JSON_PATH" "" "" "" "" "" "$NEW_PDF_COUNT" "$DRY_RUN_REMAINING_COUNT"
  finish_with_result 0 "dry-run" "text" "$ORIGINAL_BATCH_JSON_PATH"
fi

REPORT_DOC_URL="${CURRENT_DOC_URL:-$LAST_DOC_URL_SEEN}"
append_unique_doc_url "$REPORT_DOC_URL"
REPORT_DOC_URLS_JSON="$(build_doc_urls_json "${DOC_URLS[@]:-}")"
SUMMARY_ONLY_COUNT=$((SUMMARY_READY_FILE_COUNT - PROCESSED_FILE_COUNT))
if [[ "$SUMMARY_ONLY_COUNT" -lt 0 ]]; then
  SUMMARY_ONLY_COUNT=0
fi

ACK_CHUNK_COUNT=$(( ${#SUCCESSFUL_CHUNK_JSONS[@]} + ${#QUARANTINED_CHUNK_JSONS[@]} ))

if [[ "$PROCESSED_FILE_COUNT" -gt 0 ]]; then
  send_group_report_if_ready "true"
fi

if [[ "$ACK_CHUNK_COUNT" -gt 0 ]]; then
  PROCESSED_ACK_BATCH_PATH="$TEMP_DIR/processed_ack_batch.json"
  build_ack_batch_from_chunks "$PROCESSED_ACK_BATCH_PATH" "${SUCCESSFUL_CHUNK_JSONS[@]:-}" "${QUARANTINED_CHUNK_JSONS[@]:-}" >> "$LOG_PATH"
fi

if [[ "$ACK_CHUNK_COUNT" -gt 0 && "$DRY_RUN" != "true" ]]; then
  write_run_status "running" "ack_processed_batch" "acknowledging processed PDFs back to watcher state" "" "ack_processed_batch" "$PROCESSED_ACK_BATCH_PATH" "" "" "" "" "" "$NEW_PDF_COUNT"
  set +e
  build_scan_root_args
  "$PYTHON_BIN" "$SCANNER_PATH" \
    "${SCAN_ROOT_ARGS[@]}" \
    --state-file "$STATE_PATH" \
    --batch-file "$PROCESSED_ACK_BATCH_PATH" \
    --ack-batch >> "$LOG_PATH"
  ACK_RC=$?
  set -e
  if [[ "$ACK_RC" -ne 0 ]]; then
    FAILURE_BACKOFF_NOTE="$(record_failure_backoff_note "$ORIGINAL_BATCH_JSON_PATH" "状态回写失败")"
    {
      printf '知识星球研报总结：❌ 失败\n'
      printf '执行时间：%s\n' "$DISPLAY_RUN_AT"
      printf '结果：本地摘要已落地 %s 篇，飞书已发布 %s 篇，但状态确认失败\n' "$SUMMARY_READY_FILE_COUNT" "$PROCESSED_FILE_COUNT"
      if [[ "$SUMMARY_ONLY_COUNT" -gt 0 ]]; then
        printf '未发布但已落本地摘要：%s 篇\n' "$SUMMARY_ONLY_COUNT"
      fi
      print_doc_links "${DOC_URLS[@]:-}"
      printf '失败原因：状态回写失败\n'
      if [[ -n "$FAILURE_BACKOFF_NOTE" ]]; then
        printf '自动退避：%s\n' "$FAILURE_BACKOFF_NOTE"
      fi
    } > "$RESULT_MD_PATH"
    write_result_json "failed" "本地摘要和飞书发布已完成，但状态确认失败" "$PROCESSED_FILE_COUNT" "$REPORT_DOC_URL" "$ACK_RC" "$PROCESSED_ACK_BATCH_PATH" "$REPORT_DOC_URLS_JSON" "ack_failed" "completed"
    complete_run_status "failed" "completed" "processed batch ack failed" "$ACK_RC" "ack_failed" "$PROCESSED_ACK_BATCH_PATH" "" "" "" "" "" "$NEW_PDF_COUNT"
    finish_with_result "$ACK_RC" "ack-failed" "text" "$PROCESSED_ACK_BATCH_PATH"
  fi
fi

HANDLED_COUNT=$((PROCESSED_FILE_COUNT + QUARANTINED_FILE_COUNT))

if [[ "$FATAL_RELEASE_CONTRACT_MISMATCH" == "true" ]]; then
  REMAINING_COUNT=$((NEW_PDF_COUNT - HANDLED_COUNT))
  if [[ "$REMAINING_COUNT" -lt 0 ]]; then
    REMAINING_COUNT=0
  fi
  BLOCKED_RELEASE_SUMMARY="$(summarize_failure_text "${FATAL_ENV_FAILURE_SUMMARY:-release contract mismatch}")"
  clear_failure_backoff_state
  {
    printf '知识星球研报总结：⛔ 发布版本已阻塞\n'
    printf '执行时间：%s\n' "$DISPLAY_RUN_AT"
    printf '结果：本地摘要已落地 %s 篇，已发布 %s 篇，剩余 %s 篇需要在修复 release 合同后人工确认恢复\n' "$SUMMARY_READY_FILE_COUNT" "$PROCESSED_FILE_COUNT" "$REMAINING_COUNT"
    printf '失败原因：%s\n' "$BLOCKED_RELEASE_SUMMARY"
    printf '日志位置：%s\n' "$LOG_PATH"
  } > "$RESULT_MD_PATH"
  write_result_json "blocked" "$BLOCKED_RELEASE_SUMMARY" "$NEW_PDF_COUNT" "$REPORT_DOC_URL" "$FAILURE_EXIT_CODE" "${PROCESSED_ACK_BATCH_PATH:-$ORIGINAL_BATCH_JSON_PATH}" "$REPORT_DOC_URLS_JSON" "blocked_release" "blocked_release"
  complete_run_status "blocked" "blocked_release" "release contract mismatch blocked automatic retry" "$FAILURE_EXIT_CODE" "blocked_release" "${PROCESSED_ACK_BATCH_PATH:-$ORIGINAL_BATCH_JSON_PATH}" "" "" "" "" "" "$NEW_PDF_COUNT" "$REMAINING_COUNT"
  finish_with_result "$FAILURE_EXIT_CODE" "blocked-release" "text" "${PROCESSED_ACK_BATCH_PATH:-$ORIGINAL_BATCH_JSON_PATH}"
fi

if [[ "$FATAL_ENV_FAILURE" == "true" ]]; then
  REMAINING_COUNT=$((NEW_PDF_COUNT - HANDLED_COUNT))
  FATAL_FAILURE_SUMMARY="$(summarize_failure_text "${FATAL_ENV_FAILURE_SUMMARY:-${FAILED_FILES_SUMMARY:-未知错误}}")"
  FATAL_BACKOFF_COUNT="0"
  FATAL_BACKOFF_PAUSED="false"
  FATAL_BACKOFF_NEXT_RETRY=""
  FATAL_BACKOFF_MAX_ATTEMPTS=""
  FATAL_BACKOFF_RETRY_POLICY=""
  IFS=$'\x1f' read -r FATAL_BACKOFF_COUNT FATAL_BACKOFF_PAUSED FATAL_BACKOFF_NEXT_RETRY FATAL_BACKOFF_MAX_ATTEMPTS FATAL_BACKOFF_RETRY_POLICY < <(record_failure_backoff_state "${PROCESSED_ACK_BATCH_PATH:-$ORIGINAL_BATCH_JSON_PATH}" "${FATAL_ENV_FAILURE_SUMMARY:-${FAILED_FILES_SUMMARY:-处理未完成}}")
  {
    printf '知识星球研报总结：❌ 停止处理\n'
    printf '执行时间：%s\n' "$DISPLAY_RUN_AT"
    printf '结果：%s，本轮停止后续处理\n' "$FATAL_FAILURE_SUMMARY"
    printf '本地摘要已落地：%s 篇\n' "$SUMMARY_READY_FILE_COUNT"
    printf '已完成：%s 篇\n' "$PROCESSED_FILE_COUNT"
    if [[ "$SUMMARY_ONLY_COUNT" -gt 0 ]]; then
      printf '已生成本地摘要但未发布：%s 篇\n' "$SUMMARY_ONLY_COUNT"
    fi
    if [[ "$QUARANTINED_FILE_COUNT" -gt 0 ]]; then
      printf '已隔离：%s 篇\n' "$QUARANTINED_FILE_COUNT"
      printf '隔离清单：%s\n' "$QUARANTINE_JSON_PATH"
      printf '排障清单：%s\n' "$QUARANTINE_REPORT_PATH"
    fi
    printf '待后续重跑：%s 篇\n' "$REMAINING_COUNT"
    print_doc_links "${DOC_URLS[@]:-}"
    printf '失败原因：%s\n' "$FATAL_FAILURE_SUMMARY"
    print_retry_status_lines "$FATAL_BACKOFF_COUNT" "$FATAL_BACKOFF_NEXT_RETRY" "$FATAL_BACKOFF_PAUSED" "$FATAL_BACKOFF_MAX_ATTEMPTS" "$FATAL_BACKOFF_RETRY_POLICY"
    printf '预检报告：%s\n' "$PREFLIGHT_JSON_PATH"
  } > "$RESULT_MD_PATH"
  if [[ "$HANDLED_COUNT" -gt 0 ]]; then
    write_result_json "partial_success" "$FATAL_FAILURE_SUMMARY" "$NEW_PDF_COUNT" "$REPORT_DOC_URL" "$FAILURE_EXIT_CODE" "${PROCESSED_ACK_BATCH_PATH:-$ORIGINAL_BATCH_JSON_PATH}" "$REPORT_DOC_URLS_JSON" "env_failed" "completed" "" "$FATAL_BACKOFF_NEXT_RETRY"
    complete_run_status "partial_success" "completed" "environment failure stopped later chunks" "$FAILURE_EXIT_CODE" "env_failed" "${PROCESSED_ACK_BATCH_PATH:-$ORIGINAL_BATCH_JSON_PATH}" "" "" "" "" "$FATAL_BACKOFF_NEXT_RETRY" "$NEW_PDF_COUNT" "$REMAINING_COUNT"
  else
    write_result_json "env_failed" "$FATAL_FAILURE_SUMMARY" "$NEW_PDF_COUNT" "$REPORT_DOC_URL" "$FAILURE_EXIT_CODE" "${PROCESSED_ACK_BATCH_PATH:-$ORIGINAL_BATCH_JSON_PATH}" "$REPORT_DOC_URLS_JSON" "env_failed" "completed" "" "$FATAL_BACKOFF_NEXT_RETRY"
    complete_run_status "env_failed" "completed" "environment failure stopped run" "$FAILURE_EXIT_CODE" "env_failed" "${PROCESSED_ACK_BATCH_PATH:-$ORIGINAL_BATCH_JSON_PATH}" "" "" "" "" "$FATAL_BACKOFF_NEXT_RETRY" "$NEW_PDF_COUNT" "$REMAINING_COUNT"
  fi
  finish_with_result "$FAILURE_EXIT_CODE" "env-failed" "text" "${PROCESSED_ACK_BATCH_PATH:-$ORIGINAL_BATCH_JSON_PATH}"
fi

if [[ "$HANDLED_COUNT" -ne "$NEW_PDF_COUNT" ]]; then
  REMAINING_COUNT=$((NEW_PDF_COUNT - HANDLED_COUNT))
  FAILURE_REASON_SUMMARY="$(summarize_failure_text "${FAILED_FILES_SUMMARY:-${FAILURE_REASON:-未知错误}}")"
  FILE_STAGE_RETRY_STATUS_PATH="$TEMP_DIR/stage_retry_status.json"
  "$PYTHON_BIN" "$HELPER_PATH" stage-retry-status \
    --ledger-file "$STAGE_RETRY_LEDGER_PATH" \
    --workflow-version "$WORKFLOW_RETRY_VERSION" > "$FILE_STAGE_RETRY_STATUS_PATH" 2>> "$LOG_PATH" || true
  FILE_STAGE_NEXT_RETRY="$(json_get_value "$FILE_STAGE_RETRY_STATUS_PATH" "next_retry_at" || true)"
  clear_failure_backoff_state
  TERMINAL_STATUS="failed"
  TERMINAL_OPERATIONAL_STATE="failed"
  TERMINAL_EVENT="failed"
  TERMINAL_TITLE="❌ 失败"
  if [[ "$HANDLED_COUNT" -gt 0 ]]; then
    TERMINAL_STATUS="partial_success"
    TERMINAL_OPERATIONAL_STATE="partial_success"
    TERMINAL_EVENT="partial-success"
    TERMINAL_TITLE="⚠️ 部分完成"
  fi
  {
    printf '知识星球研报总结：%s\n' "$TERMINAL_TITLE"
    printf '执行时间：%s\n' "$DISPLAY_RUN_AT"
    printf '结果：本轮共 %s 篇，本地摘要已落地 %s 篇，已发布 %s 篇，还剩 %s 篇\n' "$NEW_PDF_COUNT" "$SUMMARY_READY_FILE_COUNT" "$PROCESSED_FILE_COUNT" "$REMAINING_COUNT"
    if [[ "$SUMMARY_ONLY_COUNT" -gt 0 ]]; then
      printf '已生成本地摘要但未发布：%s 篇\n' "$SUMMARY_ONLY_COUNT"
    fi
    if [[ "$QUARANTINED_FILE_COUNT" -gt 0 ]]; then
      printf '已隔离：%s 篇\n' "$QUARANTINED_FILE_COUNT"
      printf '隔离清单：%s\n' "$QUARANTINE_JSON_PATH"
      printf '排障清单：%s\n' "$QUARANTINE_REPORT_PATH"
    fi
    print_doc_links "${DOC_URLS[@]:-}"
    printf '失败原因：%s\n' "$FAILURE_REASON_SUMMARY"
    if [[ -n "$FILE_STAGE_NEXT_RETRY" ]]; then
      printf '逐文件重试：不早于 %s（5/10/20 分钟退避）\n' "$(format_display_time "$FILE_STAGE_NEXT_RETRY")"
    else
      printf '逐文件状态：已记录到 %s\n' "$STAGE_RETRY_LEDGER_PATH"
    fi
  } > "$RESULT_MD_PATH"
  write_result_json "$TERMINAL_STATUS" "$FAILURE_REASON_SUMMARY" "$NEW_PDF_COUNT" "$REPORT_DOC_URL" "$FAILURE_EXIT_CODE" "${PROCESSED_ACK_BATCH_PATH:-$ORIGINAL_BATCH_JSON_PATH}" "$REPORT_DOC_URLS_JSON" "$TERMINAL_OPERATIONAL_STATE" "completed" "" "$FILE_STAGE_NEXT_RETRY"
  complete_run_status "$TERMINAL_STATUS" "completed" "run completed with remaining failed files" "$FAILURE_EXIT_CODE" "$TERMINAL_OPERATIONAL_STATE" "${PROCESSED_ACK_BATCH_PATH:-$ORIGINAL_BATCH_JSON_PATH}" "" "" "" "" "$FILE_STAGE_NEXT_RETRY" "$NEW_PDF_COUNT" "$REMAINING_COUNT"
  finish_with_result "$FAILURE_EXIT_CODE" "$TERMINAL_EVENT" "text" "${PROCESSED_ACK_BATCH_PATH:-$ORIGINAL_BATCH_JSON_PATH}"
fi

if [[ "$QUARANTINED_FILE_COUNT" -gt 0 ]]; then
  clear_failure_backoff_state
  QUARANTINE_TERMINAL_STATUS="completed_with_quarantine"
  QUARANTINE_TITLE="🟠 已隔离待转换"
  if [[ "$PROCESSED_FILE_COUNT" -gt 0 || "$SUMMARY_READY_FILE_COUNT" -gt 0 ]]; then
    QUARANTINE_TERMINAL_STATUS="partial_success"
    QUARANTINE_TITLE="⚠️ 部分完成"
  fi
  {
    printf '知识星球研报总结：%s\n' "$QUARANTINE_TITLE"
    printf '执行时间：%s\n' "$DISPLAY_RUN_AT"
    printf '结果：本地摘要已落地 %s 篇，已发布 %s 篇，另有 %s 篇因正文不可用已移入隔离区\n' "$SUMMARY_READY_FILE_COUNT" "$PROCESSED_FILE_COUNT" "$QUARANTINED_FILE_COUNT"
    print_doc_links "${DOC_URLS[@]:-}"
    printf '隔离清单：%s\n' "$QUARANTINE_JSON_PATH"
    printf '排障清单：%s\n' "$QUARANTINE_REPORT_PATH"
  } > "$RESULT_MD_PATH"
  write_result_json "$QUARANTINE_TERMINAL_STATUS" "部分文件已移入隔离区" "$NEW_PDF_COUNT" "$REPORT_DOC_URL" "0" "${PROCESSED_ACK_BATCH_PATH:-$ORIGINAL_BATCH_JSON_PATH}" "$REPORT_DOC_URLS_JSON" "completed_with_quarantine" "completed"
  complete_run_status "$QUARANTINE_TERMINAL_STATUS" "completed" "run completed with quarantined files" "0" "completed_with_quarantine" "${PROCESSED_ACK_BATCH_PATH:-$ORIGINAL_BATCH_JSON_PATH}" "" "" "" "" "" "$NEW_PDF_COUNT" "0"
  finish_with_result 0 "completed-with-quarantine" "text" "${PROCESSED_ACK_BATCH_PATH:-$ORIGINAL_BATCH_JSON_PATH}"
fi

clear_failure_backoff_state
{
  printf '知识星球研报总结：✅ 成功\n'
  printf '执行时间：%s\n' "$DISPLAY_RUN_AT"
  printf '结果：已完成 %s 篇研报总结；本地摘要和飞书文档都已生成\n' "$NEW_PDF_COUNT"
  printf '生成文档：%s 份\n' "${#DOC_URLS[@]}"
  print_doc_links "${DOC_URLS[@]:-}"
} > "$RESULT_MD_PATH"

write_result_json "success" "本地摘要和飞书文档已生成" "$NEW_PDF_COUNT" "$REPORT_DOC_URL" "0" "$ORIGINAL_BATCH_JSON_PATH" "$REPORT_DOC_URLS_JSON" "completed" "completed"
complete_run_status "success" "completed" "summary task completed successfully" "0" "completed" "$ORIGINAL_BATCH_JSON_PATH" "" "" "" "" "" "$NEW_PDF_COUNT" "0"
render_compact_terminal_result
send_chat_message "$(cat "$RESULT_MD_PATH")" "completed" "markdown" "$ORIGINAL_BATCH_JSON_PATH"
sync_result_notification_records
cat "$RESULT_MD_PATH"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 本次处理文件：" >> "$LOG_PATH"
extract_file_lines "$ORIGINAL_BATCH_JSON_PATH" >> "$LOG_PATH"
{
  printf '[%s] ' "$(date '+%Y-%m-%d %H:%M:%S')"
  print_doc_links "${DOC_URLS[@]:-}"
} >> "$LOG_PATH"
