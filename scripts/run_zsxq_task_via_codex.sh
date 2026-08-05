#!/usr/bin/env bash

# This is the main Codex-side entry script.
# OpenClaw or a manual trigger calls this file first.
# This file prepares runtime prompt constraints and asks Codex to use
# Playwright MCP as the primary browser driver for this scheduled task.

set -euo pipefail

# Basic paths used by the whole run.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Standalone repository root.
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_ID="${ZSXQ_RUN_ID:-$(python3 -c 'import uuid; print(uuid.uuid4())')}"
CODEX_BIN="${CODEX_BIN:-$(command -v codex || true)}"
CODEX_MODEL="${ZSXQ_CODEX_MODEL:-gpt-5.6-sol}"
CODEX_MODEL_PROVIDER="${ZSXQ_CODEX_MODEL_PROVIDER:-openai}"
CODEX_REASONING_EFFORT="${ZSXQ_CODEX_REASONING_EFFORT:-low}"
PROMPT_FILE="${ZSXQ_PROMPT_FILE:-$ROOT/prompts/openclaw_scheduler_prompt.md}"
RUNTIME_DIR="${INVESTMENT_REPORTS_RUNTIME_DIR:-$ROOT/.runtime}"
# Preserve the checkpoint/log location used by deployments created before the
# .runtime layout was introduced. This avoids a false first run or full rescan.
if [[ -z "${INVESTMENT_REPORTS_RUNTIME_DIR:-}" ]] && { [[ -f "$ROOT/state/zsxq_foreign_reports_state.json" ]] || [[ -f "$ROOT/state/zsxq_domestic_cicc_reports_state.json" ]]; }; then
  RUNTIME_DIR="$ROOT"
fi
LOG_DIR="${ZSXQ_LOG_DIR:-$RUNTIME_DIR/logs}"
STARTUP_LOG="$LOG_DIR/launcher_debug.log"
LOCK_DIR="${ZSXQ_LOCK_DIR:-/tmp/zsxq_codex_task.lock}"
LOCK_WAIT_SECONDS="${ZSXQ_LOCK_WAIT_SECONDS:-0}"
LOCK_WAIT_INTERVAL_SECONDS="${ZSXQ_LOCK_WAIT_INTERVAL_SECONDS:-15}"
LOCK_STALE_SECONDS="${ZSXQ_LOCK_STALE_SECONDS:-300}"
LOCK_TOKEN="${RUN_ID}.$$.$RANDOM"
CODEX_TIMEOUT_SECONDS="${ZSXQ_CODEX_TIMEOUT_SECONDS:-5400}"
CODEX_TERMINATE_GRACE_SECONDS="${ZSXQ_CODEX_TERMINATE_GRACE_SECONDS:-10}"
CODEX_MODELS_CACHE="${ZSXQ_CODEX_MODELS_CACHE:-$HOME/.codex/models_cache.json}"
PLAYWRIGHT_ACTION_TIMEOUT_MS="${ZSXQ_PLAYWRIGHT_ACTION_TIMEOUT_MS:-20000}"
PREFLIGHT_CONNECT_TIMEOUT_MS="${ZSXQ_PREFLIGHT_CONNECT_TIMEOUT_MS:-45000}"
PREFLIGHT_NAVIGATION_TIMEOUT_MS="${ZSXQ_PREFLIGHT_NAVIGATION_TIMEOUT_MS:-30000}"
PREFLIGHT_STATE_WAIT_MS="${ZSXQ_PREFLIGHT_STATE_WAIT_MS:-12000}"
PREFLIGHT_NAVIGATION_ATTEMPTS="${ZSXQ_PREFLIGHT_NAVIGATION_ATTEMPTS:-3}"
PREFLIGHT_RETRY_DELAY_MS="${ZSXQ_PREFLIGHT_RETRY_DELAY_MS:-1000}"
STATE_FILE="${ZSXQ_STATE_FILE:-$RUNTIME_DIR/state/zsxq_foreign_reports_state.json}"
STRUCTURED_RESULT_PATH="${ZSXQ_STRUCTURED_RESULT_PATH:-$LOG_DIR/zsxq_last_run_structured.json}"
if [[ ! -f "$STATE_FILE" && "$STATE_FILE" == "$ROOT/.runtime/state/"* && -f "$ROOT/state/$(basename "$STATE_FILE")" ]]; then
  STATE_FILE="$ROOT/state/$(basename "$STATE_FILE")"
fi
if [[ "$STRUCTURED_RESULT_PATH" == "$ROOT/.runtime/logs/"* && -f "$ROOT/logs/$(basename "$STRUCTURED_RESULT_PATH")" ]]; then
  STRUCTURED_RESULT_PATH="$ROOT/logs/$(basename "$STRUCTURED_RESULT_PATH")"
fi
RESULT_HELPER="$ROOT/scripts/zsxq_autodownload_result.py"
FINALIZE_SCRIPT="$ROOT/scripts/finalize_download_batch.py"
PREFLIGHT_SCRIPT="$ROOT/scripts/zsxq_preflight.py"
RUNTIME_GUARD="$ROOT/scripts/zsxq_runtime_guard.py"
JOB_CONFIG_FILE="${ZSXQ_JOB_CONFIG_FILE:-$ROOT/config/local/zsxq_foreign_reports_job.json}"
KEYWORDS_FILE="${ZSXQ_KEYWORDS_FILE:-$ROOT/config/local/interest_keywords.json}"
# Older private deployments kept their real configuration directly under
# config/. Keep that layout working when no explicit override was supplied.
if [[ ! -f "$JOB_CONFIG_FILE" && "$JOB_CONFIG_FILE" == "$ROOT/config/local/"* && -f "$ROOT/config/$(basename "$JOB_CONFIG_FILE")" ]]; then
  JOB_CONFIG_FILE="$ROOT/config/$(basename "$JOB_CONFIG_FILE")"
fi
if [[ ! -f "$KEYWORDS_FILE" && "$KEYWORDS_FILE" == "$ROOT/config/local/"* && -f "$ROOT/config/$(basename "$KEYWORDS_FILE")" ]]; then
  KEYWORDS_FILE="$ROOT/config/$(basename "$KEYWORDS_FILE")"
fi
SCAN_PLAN_IS_TEMP="false"
if [[ -z "${ZSXQ_SCAN_PLAN_PATH:-}" ]]; then
  SCAN_PLAN_IS_TEMP="true"
fi
SCAN_PLAN_PATH="${ZSXQ_SCAN_PLAN_PATH:-/tmp/zsxq_download_candidates_${RUN_ID}.json}"
RUN_MANIFEST_PATH="${ZSXQ_RUN_MANIFEST_PATH:-$RUNTIME_DIR/state/zsxq_autodownload_runs/${RUN_ID}.json}"
CFT_EXECUTABLE_PATH="${CFT_EXECUTABLE_PATH:-/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing}"
CFT_REMOTE_DEBUG_PORT="${CFT_REMOTE_DEBUG_PORT:-9223}"
CFT_USER_DATA_DIR="${CFT_USER_DATA_DIR:-$HOME/.openclaw/browser-profiles/zsxq-cft}"
CFT_START_URL="${CFT_START_URL:-}"
CFT_START_URL_SOURCE="environment"
if [[ -z "$CFT_START_URL" && -f "$JOB_CONFIG_FILE" ]]; then
  CFT_START_URL="$(python3 - "$JOB_CONFIG_FILE" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    payload = {}
print(str(payload.get("tag_url") or payload.get("group_url") or "").strip())
PY
)"
  CFT_START_URL_SOURCE="job_config"
fi
CFT_HEADLESS="${ZSXQ_CFT_HEADLESS:-true}"
CFT_WINDOW_SIZE="${ZSXQ_CFT_WINDOW_SIZE:-1440,1200}"
CFT_CDP_ENDPOINT="http://127.0.0.1:${CFT_REMOTE_DEBUG_PORT}"
RUNTIME_PROMPT_FILE="$PROMPT_FILE"
OPENCLAW_TASKS_ROOT="${OPENCLAW_TASKS_ROOT:-$HOME/.openclaw/workspace/tasks}"
STATUS_JSON_PATH="${ZSXQ_STATUS_JSON_PATH:-$OPENCLAW_TASKS_ROOT/ZSXQ_autodownload/run_status.json}"

# These two values are optional.
# If they exist, this run uses a user-specified time window instead of the normal state-based window.
WINDOW_START="${ZSXQ_WINDOW_START:-}"
WINDOW_END="${ZSXQ_WINDOW_END:-}"
WINDOW_MODE="state"
if [[ -n "$WINDOW_START" || -n "$WINDOW_END" ]]; then
  WINDOW_MODE="explicit"
fi
TMP_PROMPT_FILE=""
RAW_CODEX_OUTPUT_FILE=""
RECOVERY_SUMMARY_FILE=""
RUN_STARTED_AT="$(date -Iseconds)"
PRE_LAST_SUCCESSFUL_CHECK_AT=""
HEARTBEAT_PID=""
STRUCTURED_RESULT_WRITTEN="false"

mkdir -p "$LOG_DIR"
mkdir -p "$(dirname "$RUN_MANIFEST_PATH")"

startup_log() {
  printf '[%s] %s\n' "$(date -Iseconds)" "$1" >> "$STARTUP_LOG"
}

startup_log "run_zsxq_task_via_codex.sh entered"
startup_log "pwd=$(pwd)"
startup_log "PATH=$PATH"
startup_log "ROOT=$ROOT"
startup_log "CODEX_BIN=$CODEX_BIN"
startup_log "CODEX_MODEL=$CODEX_MODEL"
startup_log "CODEX_MODEL_PROVIDER=$CODEX_MODEL_PROVIDER"
startup_log "CODEX_REASONING_EFFORT=$CODEX_REASONING_EFFORT"
startup_log "PROMPT_FILE=$PROMPT_FILE"
startup_log "JOB_CONFIG_FILE=$JOB_CONFIG_FILE"
startup_log "KEYWORDS_FILE=$KEYWORDS_FILE"
startup_log "STATE_FILE=$STATE_FILE"
startup_log "RUN_ID=$RUN_ID"
startup_log "SCAN_PLAN_PATH=$SCAN_PLAN_PATH"
startup_log "RUN_MANIFEST_PATH=$RUN_MANIFEST_PATH"
startup_log "LOCK_WAIT_SECONDS=$LOCK_WAIT_SECONDS"
startup_log "LOCK_STALE_SECONDS=$LOCK_STALE_SECONDS"
startup_log "CODEX_TIMEOUT_SECONDS=$CODEX_TIMEOUT_SECONDS"
startup_log "CODEX_TERMINATE_GRACE_SECONDS=$CODEX_TERMINATE_GRACE_SECONDS"
startup_log "CODEX_MODELS_CACHE=$CODEX_MODELS_CACHE"
startup_log "PLAYWRIGHT_ACTION_TIMEOUT_MS=$PLAYWRIGHT_ACTION_TIMEOUT_MS"
startup_log "PREFLIGHT_NAVIGATION_ATTEMPTS=$PREFLIGHT_NAVIGATION_ATTEMPTS"
startup_log "PREFLIGHT_NAVIGATION_TIMEOUT_MS=$PREFLIGHT_NAVIGATION_TIMEOUT_MS"
startup_log "PREFLIGHT_STATE_WAIT_MS=$PREFLIGHT_STATE_WAIT_MS"
startup_log "CFT_EXECUTABLE_PATH=$CFT_EXECUTABLE_PATH"
startup_log "CFT_REMOTE_DEBUG_PORT=$CFT_REMOTE_DEBUG_PORT"
startup_log "CFT_USER_DATA_DIR=$CFT_USER_DATA_DIR"
startup_log "CFT_START_URL_SOURCE=$CFT_START_URL_SOURCE"
startup_log "CFT_HEADLESS=$CFT_HEADLESS"
startup_log "CFT_WINDOW_SIZE=$CFT_WINDOW_SIZE"

write_status() {
  local status="$1"
  local phase="$2"
  local message="$3"
  local exit_code="${4:-}"
  python3 - "$STATUS_JSON_PATH" "$status" "$phase" "$message" "$RUN_STARTED_AT" "$STARTUP_LOG" "${LOG_FILE:-}" "$exit_code" "$STRUCTURED_RESULT_PATH" "$RUN_ID" "$RUN_MANIFEST_PATH" <<'PY'
import json
import sys
from datetime import datetime
from pathlib import Path

path = Path(sys.argv[1])
status = sys.argv[2]
phase = sys.argv[3]
message = sys.argv[4]
run_started_at = sys.argv[5]
startup_log_path = sys.argv[6]
codex_log_path = sys.argv[7]
exit_code_raw = sys.argv[8]
canonical_result_path = sys.argv[9]
run_id = sys.argv[10]
run_manifest_path = sys.argv[11]

payload = {
    "status": status,
    "phase": phase,
    "message": message,
    "run_started_at": run_started_at,
    "last_heartbeat_at": datetime.now().astimezone().isoformat(),
    "log_path": codex_log_path or startup_log_path,
    "canonical_result_path": canonical_result_path,
    "run_id": run_id,
    "run_manifest_path": run_manifest_path,
}
payload["status"] = status
payload["phase"] = phase
payload["message"] = message
if exit_code_raw:
    try:
        payload["codex_exit_code"] = int(exit_code_raw)
    except Exception:
        payload["codex_exit_code"] = exit_code_raw

path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
}

if [[ ! -f "$RESULT_HELPER" ]]; then
  write_status "failed" "launcher_init" "result helper missing" "2"
  echo "[ERROR] result helper missing: $RESULT_HELPER" >&2
  exit 2
fi

if [[ ! -f "$RUNTIME_GUARD" ]]; then
  write_status "failed" "launcher_init" "runtime guard missing" "2"
  echo "[ERROR] runtime guard missing: $RUNTIME_GUARD" >&2
  exit 2
fi

start_heartbeat() {
  stop_heartbeat
  (
    while true; do
      sleep 15
      write_status "running" "codex_exec" "codex exec is still running" || true
    done
  ) &
  HEARTBEAT_PID=$!
}

stop_heartbeat() {
  if [[ -n "${HEARTBEAT_PID:-}" ]]; then
    kill "$HEARTBEAT_PID" 2>/dev/null || true
    wait "$HEARTBEAT_PID" 2>/dev/null || true
    HEARTBEAT_PID=""
  fi
}

write_status "running" "launcher_init" "Codex launcher entered"

write_stub_result() {
  local codex_rc="$1"
  local no_download_reason="$2"
  local reason_code="$3"
  local status="$4"
  local scan_alert="${5:-}"
  local scan_mode="${6:-}"
  local api_probe_status="${7:-}"
  local run_finished_at="${8:-$RUN_STARTED_AT}"
  python3 "$RESULT_HELPER" stub \
    --output "$STRUCTURED_RESULT_PATH" \
    --run-id "$RUN_ID" \
    --run-started-at "$RUN_STARTED_AT" \
    --run-finished-at "$run_finished_at" \
    --requested-window-start "$WINDOW_START" \
    --requested-window-end "$WINDOW_END" \
    --pre-last-successful-check-at "$PRE_LAST_SUCCESSFUL_CHECK_AT" \
    --codex-rc "$codex_rc" \
    --no-download-reason "$no_download_reason" \
    --reason-code "$reason_code" \
    --status "$status" \
    --scan-alert "$scan_alert" \
    --scan-mode "$scan_mode" \
    --api-probe-status "$api_probe_status" >/dev/null
  STRUCTURED_RESULT_WRITTEN="true"
}

if [[ ! -f "$JOB_CONFIG_FILE" ]]; then
  write_stub_result "2" "unknown" "task_failed" "failed"
  write_status "failed" "launcher_init" "job configuration file is missing" "2"
  printf '[ERROR] job configuration file is missing: %s\n' "$JOB_CONFIG_FILE" >&2
  exit 2
fi

if [[ ! -f "$KEYWORDS_FILE" ]]; then
  write_stub_result "2" "unknown" "task_failed" "failed"
  write_status "failed" "launcher_init" "keyword configuration file is missing" "2"
  printf '[ERROR] keyword configuration file is missing: %s\n' "$KEYWORDS_FILE" >&2
  exit 2
fi

if [[ -z "$CFT_START_URL" ]]; then
  write_stub_result "2" "unknown" "task_failed" "failed"
  write_status "failed" "launcher_init" "browser start URL is missing" "2"
  printf '%s\n' "CFT_START_URL is missing and the job config has no tag_url/group_url" >&2
  exit 2
fi

if [[ ! -x "$CODEX_BIN" ]]; then
  startup_log "codex binary missing or not executable"
  write_stub_result "2" "unknown" "task_failed" "failed"
  write_status "failed" "launcher_init" "codex binary missing or not executable" "2"
  echo "[ERROR] codex binary not found or not executable: $CODEX_BIN" >&2
  exit 2
fi

if [[ ! -f "$PROMPT_FILE" ]]; then
  startup_log "prompt file missing"
  write_stub_result "2" "unknown" "task_failed" "failed"
  write_status "failed" "launcher_init" "prompt file missing" "2"
  echo "[ERROR] prompt file missing: $PROMPT_FILE" >&2
  exit 2
fi

if [[ ! -x "$CFT_EXECUTABLE_PATH" ]]; then
  startup_log "cft executable missing or not executable"
  write_stub_result "21" "blocked_browser" "blocked_browser_missing" "blocked_browser"
  write_status "blocked_browser" "launcher_init" "Chrome for Testing executable not found" "21"
  echo "[BLOCKED] blocked_browser_missing: Chrome for Testing executable not found at $CFT_EXECUTABLE_PATH" >&2
  exit 21
fi

if [[ -n "$WINDOW_START" && -z "$WINDOW_END" ]]; then
  write_stub_result "2" "unknown" "task_failed" "failed"
  write_status "failed" "launcher_init" "window end is missing" "2"
  echo "[ERROR] ZSXQ_WINDOW_END is required when ZSXQ_WINDOW_START is set." >&2
  exit 2
fi

if [[ -z "$WINDOW_START" && -n "$WINDOW_END" ]]; then
  write_stub_result "2" "unknown" "task_failed" "failed"
  write_status "failed" "launcher_init" "window start is missing" "2"
  echo "[ERROR] ZSXQ_WINDOW_START is required when ZSXQ_WINDOW_END is set." >&2
  exit 2
fi

if [[ -f "$STATE_FILE" ]]; then
  PRE_LAST_SUCCESSFUL_CHECK_AT="$(python3 - "$STATE_FILE" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
if not path.exists():
    print("")
    raise SystemExit(0)

try:
    data = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    print("")
    raise SystemExit(0)

print(str(data.get("last_successful_check_at", "")).strip())
PY
)"
fi

write_structured_stub() {
  local codex_rc="$1"
  local no_download_reason="$2"
  local reason_code="$3"
  local status="$4"
  local scan_alert="${5:-}"
  local scan_mode="${6:-}"
  local api_probe_status="${7:-}"
  local run_finished_at="${8:-$RUN_STARTED_AT}"
  write_stub_result "$codex_rc" "$no_download_reason" "$reason_code" "$status" "$scan_alert" "$scan_mode" "$api_probe_status" "$run_finished_at"
}

raw_output_has_report_marker() {
  local raw_output_path="$1"
  python3 - "$raw_output_path" "$ROOT" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
automation_root = Path(sys.argv[2])
sys.path.insert(0, str(automation_root))
if not path.exists():
    raise SystemExit(1)

from scripts.zsxq_autodownload_result import parse_machine_report_text

if parse_machine_report_text(path.read_text(encoding="utf-8", errors="replace")):
    raise SystemExit(0)
raise SystemExit(1)
PY
}

raw_output_has_cloud_requirements_timeout() {
  local raw_output_path="$1"
  python3 - "$raw_output_path" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
if not path.exists():
    raise SystemExit(1)

text = path.read_text(encoding="utf-8", errors="replace")
markers = (
    "timed out waiting for cloud requirements after",
    "Timed out waiting for cloud requirements after",
)
raise SystemExit(0 if any(marker in text for marker in markers) else 1)
PY
}

ensure_cft_running() {
  CFT_EXECUTABLE_PATH="$CFT_EXECUTABLE_PATH" \
  CFT_USER_DATA_DIR="$CFT_USER_DATA_DIR" \
  CFT_START_URL="$CFT_START_URL" \
  CFT_REMOTE_DEBUG_PORT="$CFT_REMOTE_DEBUG_PORT" \
  CFT_HEADLESS="$CFT_HEADLESS" \
  CFT_WINDOW_SIZE="$CFT_WINDOW_SIZE" \
  python3 - <<'PY'
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

exe_path = os.environ["CFT_EXECUTABLE_PATH"]
user_data_dir = os.environ["CFT_USER_DATA_DIR"]
start_url = os.environ["CFT_START_URL"]
port = int(os.environ["CFT_REMOTE_DEBUG_PORT"])
headless = os.environ.get("CFT_HEADLESS", "true").strip().casefold() not in {
    "0",
    "false",
    "no",
    "off",
}
window_size = os.environ.get("CFT_WINDOW_SIZE", "1440,1200").strip()
base = f"http://127.0.0.1:{port}"


def http_json(url: str) -> list | dict:
    with urllib.request.urlopen(url, timeout=1.5) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def cdp_ready() -> bool:
    try:
        data = http_json(base + "/json/version")
    except Exception:
        return False
    ws = str(data.get("webSocketDebuggerUrl", "")).strip() if isinstance(data, dict) else ""
    return bool(ws)


def remove_stale_singletons() -> None:
    lock_path = Path(user_data_dir) / "SingletonLock"
    owner_is_alive = False
    try:
        lock_target = os.readlink(lock_path)
        owner_pid = int(lock_target.rsplit("-", 1)[-1])
        os.kill(owner_pid, 0)
        owner_is_alive = True
    except (FileNotFoundError, OSError, TypeError, ValueError):
        owner_is_alive = False
    if owner_is_alive:
        return
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        path = Path(user_data_dir) / name
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def ensure_keepalive_page() -> None:
    try:
        tabs = http_json(base + "/json/list")
    except Exception:
        return
    target = start_url
    for tab in tabs if isinstance(tabs, list) else []:
        url = str(tab.get("url", "")).strip()
        if url.startswith(target):
            return
    encoded = urllib.parse.quote(start_url, safe="")
    try:
        urllib.request.urlopen(base + f"/json/new?{encoded}", timeout=2.0).read()
    except urllib.error.HTTPError:
        # Some versions may block /json/new; keep run going.
        pass
    except Exception:
        pass


Path(user_data_dir).mkdir(parents=True, exist_ok=True)

if not cdp_ready():
    # Start detached CFT once; do not tie lifecycle to this task process.
    remove_stale_singletons()
    log_path = Path("/tmp/zsxq_cft_keepalive.log")
    command = [
        exe_path,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        command.extend(
            [
                "--headless=new",
                f"--window-size={window_size}",
                "--disable-extensions",
                "--disable-component-extensions-with-background-pages",
            ]
        )
    command.append(start_url)
    with log_path.open("ab") as logf:
        subprocess.Popen(
            command,
            stdout=logf,
            stderr=logf,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    deadline = time.time() + 25
    while time.time() < deadline:
        if cdp_ready():
            break
        time.sleep(0.5)

if not cdp_ready():
    print("[BLOCKED] blocked_browser_endpoint_unavailable: CFT CDP endpoint not ready")
    sys.exit(22)

ensure_keepalive_page()
print(f"[INFO] cft keepalive ready mode={'headless' if headless else 'headed'}")
PY
}

ensure_keepalive_page() {
  CFT_REMOTE_DEBUG_PORT="$CFT_REMOTE_DEBUG_PORT" \
  CFT_START_URL="$CFT_START_URL" \
  python3 - <<'PY'
import json
import os
import urllib.error
import urllib.parse
import urllib.request

port = int(os.environ["CFT_REMOTE_DEBUG_PORT"])
start_url = os.environ["CFT_START_URL"]
base = f"http://127.0.0.1:{port}"
target = start_url

try:
    with urllib.request.urlopen(base + "/json/list", timeout=1.5) as resp:
        tabs = json.loads(resp.read().decode("utf-8", errors="replace"))
except Exception:
    raise SystemExit(0)

for tab in tabs if isinstance(tabs, list) else []:
    url = str(tab.get("url", "")).strip()
    if url.startswith(target):
        raise SystemExit(0)

encoded = urllib.parse.quote(start_url, safe="")
try:
    urllib.request.urlopen(base + f"/json/new?{encoded}", timeout=2.0).read()
except urllib.error.HTTPError:
    pass
except Exception:
    pass
PY
}

playwright_preflight() {
  python3 "$PREFLIGHT_SCRIPT" \
    --cdp-endpoint "$CFT_CDP_ENDPOINT" \
    --start-url "$CFT_START_URL" \
    --job-config "$JOB_CONFIG_FILE" \
    --connect-timeout-ms "$PREFLIGHT_CONNECT_TIMEOUT_MS" \
    --navigation-timeout-ms "$PREFLIGHT_NAVIGATION_TIMEOUT_MS" \
    --state-wait-ms "$PREFLIGHT_STATE_WAIT_MS" \
    --navigation-attempts "$PREFLIGHT_NAVIGATION_ATTEMPTS" \
    --retry-delay-ms "$PREFLIGHT_RETRY_DELAY_MS"
}

preflight_diagnostic_field() {
  local field="$1"
  python3 -c '
import json
import sys

marker = "ZSXQ_PREFLIGHT_DIAG_JSON:"
field = sys.argv[1]
payload = {}
for line in sys.stdin:
    normalized = line.strip()
    if not normalized.startswith(marker):
        continue
    try:
        candidate = json.loads(normalized[len(marker):])
    except Exception:
        continue
    if isinstance(candidate, dict):
        payload = candidate
print(str(payload.get(field, "")))
' "$field"
}

# Avoid overlapping runs when scheduler fires close together.
if ! [[ "$LOCK_WAIT_SECONDS" =~ ^[0-9]+$ ]]; then
  LOCK_WAIT_SECONDS="0"
fi
if ! [[ "$LOCK_WAIT_INTERVAL_SECONDS" =~ ^[0-9]+$ ]] || (( LOCK_WAIT_INTERVAL_SECONDS < 1 )); then
  LOCK_WAIT_INTERVAL_SECONDS="15"
fi
if ! [[ "$LOCK_STALE_SECONDS" =~ ^[0-9]+$ ]]; then
  LOCK_STALE_SECONDS="300"
fi
if ! [[ "$CODEX_TIMEOUT_SECONDS" =~ ^[0-9]+$ ]] || (( CODEX_TIMEOUT_SECONDS < 1 )); then
  CODEX_TIMEOUT_SECONDS="5400"
fi
if ! [[ "$CODEX_TERMINATE_GRACE_SECONDS" =~ ^[0-9]+$ ]]; then
  CODEX_TERMINATE_GRACE_SECONDS="10"
fi
if ! [[ "$PLAYWRIGHT_ACTION_TIMEOUT_MS" =~ ^[0-9]+$ ]] || (( PLAYWRIGHT_ACTION_TIMEOUT_MS < 1000 )); then
  PLAYWRIGHT_ACTION_TIMEOUT_MS="20000"
fi

try_acquire_lock() {
  local lock_output lock_rc
  set +e
  lock_output="$(python3 "$RUNTIME_GUARD" lock-acquire \
    --lock-dir "$LOCK_DIR" \
    --token "$LOCK_TOKEN" \
    --run-id "$RUN_ID" \
    --owner-pid "$$" \
    --task "$JOB_CONFIG_FILE" \
    --stale-seconds "$LOCK_STALE_SECONDS" 2>&1)"
  lock_rc=$?
  set -e
  startup_log "lock acquire rc=$lock_rc result=$lock_output"
  return "$lock_rc"
}

LOCK_ACQUIRED="false"
LOCK_ACQUIRE_RC=0
if try_acquire_lock; then
  LOCK_ACQUIRED="true"
else
  LOCK_ACQUIRE_RC=$?
  if [[ "$LOCK_ACQUIRE_RC" -ne 23 ]]; then
    startup_log "lock helper failed, rc=$LOCK_ACQUIRE_RC"
    write_structured_stub "2" "unknown" "task_failed" "failed"
    write_status "failed" "launcher_init" "shared lock guard failed" "2"
    exit 2
  fi
  if (( LOCK_WAIT_SECONDS > 0 )); then
    startup_log "lock exists, waiting up to ${LOCK_WAIT_SECONDS}s"
    write_status "waiting" "launcher_init" "another run is still in progress, waiting for lock"
    LOCK_WAIT_DEADLINE=$((SECONDS + LOCK_WAIT_SECONDS))
    while (( SECONDS < LOCK_WAIT_DEADLINE )); do
      sleep "$LOCK_WAIT_INTERVAL_SECONDS"
      if try_acquire_lock; then
        LOCK_ACQUIRED="true"
        startup_log "lock acquired after waiting"
        break
      else
        LOCK_ACQUIRE_RC=$?
        if [[ "$LOCK_ACQUIRE_RC" -ne 23 ]]; then
          startup_log "lock helper failed while waiting, rc=$LOCK_ACQUIRE_RC"
          write_structured_stub "2" "unknown" "task_failed" "failed"
          write_status "failed" "launcher_init" "shared lock guard failed" "2"
          exit 2
        fi
      fi
      write_status "waiting" "launcher_init" "another run is still in progress, waiting for lock"
    done
  fi
fi

if [[ "$LOCK_ACQUIRED" != "true" ]]; then
  startup_log "lock exists, skipping trigger"
  write_structured_stub "23" "busy_locked" "busy_locked" "busy"
  write_status "busy" "launcher_init" "another run is still in progress, trigger skipped" "23"
  echo "[INFO] another run is in progress, skip this trigger."
  exit 23
fi

# Always remove the lock and the temporary prompt, even if the run fails halfway.
cleanup() {
  local original_rc=$?
  set +e
  stop_heartbeat
  if [[ "$STRUCTURED_RESULT_WRITTEN" != "true" ]]; then
    local fallback_rc="$original_rc"
    if [[ "$fallback_rc" -eq 0 ]]; then
      fallback_rc=1
    fi
    write_structured_stub "$fallback_rc" "unknown" "task_failed" "failed" "" "" "" "$(date -Iseconds)"
    write_status "failed" "unexpected_exit" "launcher exited before producing a current result" "$fallback_rc"
  fi
  if [[ "$LOCK_ACQUIRED" == "true" ]]; then
    local lock_release_output lock_release_rc
    set +e
    lock_release_output="$(python3 "$RUNTIME_GUARD" lock-release \
      --lock-dir "$LOCK_DIR" \
      --token "$LOCK_TOKEN" 2>&1)"
    lock_release_rc=$?
    set -e
    startup_log "lock release rc=$lock_release_rc result=$lock_release_output"
  fi
  if [[ -n "${TMP_PROMPT_FILE:-}" && -f "$TMP_PROMPT_FILE" ]]; then
    rm -f "$TMP_PROMPT_FILE"
  fi
  if [[ -n "${RAW_CODEX_OUTPUT_FILE:-}" && -f "$RAW_CODEX_OUTPUT_FILE" ]]; then
    rm -f "$RAW_CODEX_OUTPUT_FILE"
  fi
  if [[ -n "${RECOVERY_SUMMARY_FILE:-}" && -f "$RECOVERY_SUMMARY_FILE" ]]; then
    rm -f "$RECOVERY_SUMMARY_FILE"
  fi
  if [[ "$SCAN_PLAN_IS_TEMP" == "true" && -f "$SCAN_PLAN_PATH" ]]; then
    rm -f "$SCAN_PLAN_PATH"
  fi
  return "$original_rc"
}
trap cleanup EXIT

# A newer desktop/CLI can write a models cache that an older scheduled CLI
# cannot deserialize. Detect that exact version skew before starting Codex and
# move only the incompatible cache aside so the selected CLI can rebuild it.
MODEL_CACHE_PREP_OUTPUT=""
set +e
MODEL_CACHE_PREP_OUTPUT="$(python3 "$RUNTIME_GUARD" prepare-model-cache \
  --codex-bin "$CODEX_BIN" \
  --cache-file "$CODEX_MODELS_CACHE" 2>&1)"
MODEL_CACHE_PREP_RC=$?
set -e
if [[ "$MODEL_CACHE_PREP_RC" -ne 0 ]]; then
  startup_log "model cache compatibility check failed rc=$MODEL_CACHE_PREP_RC result=$MODEL_CACHE_PREP_OUTPUT"
  write_structured_stub "2" "unknown" "task_failed" "failed"
  write_status "failed" "launcher_init" "Codex model cache compatibility check failed" "2"
  exit 2
fi
startup_log "model cache compatibility result=$MODEL_CACHE_PREP_OUTPUT"

# Freeze one immutable scan window only after the shared lock is held. A run
# that waited behind another run must not reuse the checkpoint it saw earlier.
if [[ -f "$STATE_FILE" ]]; then
  PRE_LAST_SUCCESSFUL_CHECK_AT="$(python3 - "$STATE_FILE" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    print("")
else:
    print(str(payload.get("last_successful_check_at") or "").strip())
PY
)"
fi
if [[ "$WINDOW_MODE" == "state" ]]; then
  WINDOW_START="$PRE_LAST_SUCCESSFUL_CHECK_AT"
  WINDOW_END="$(date -Iseconds)"
fi
if [[ -z "$WINDOW_START" || -z "$WINDOW_END" ]]; then
  RUN_FINISHED_AT="$(date -Iseconds)"
  write_structured_stub "2" "unknown" "task_failed" "failed" "" "" "" "$RUN_FINISHED_AT"
  write_status "failed" "launcher_init" "could not freeze scan window" "2"
  echo "[ERROR] could not freeze scan window from state" >&2
  exit 2
fi
# Never let a failed scan accidentally reuse a previous run's candidate list.
rm -f "$SCAN_PLAN_PATH"

TMP_PROMPT_FILE="$(mktemp /tmp/zsxq_scheduler_prompt.XXXXXX)"
startup_log "created temporary prompt file: $TMP_PROMPT_FILE"
cp "$PROMPT_FILE" "$TMP_PROMPT_FILE"
cat >> "$TMP_PROMPT_FILE" <<EOF

Runtime override for this run only:
- run_id: $RUN_ID
- The launcher has frozen the only valid scan window: $WINDOW_START -> $WINDOW_END.
- Use Playwright MCP browser tools as the primary driver for all browser actions.
- Do not use chrome-devtools tools as the main execution path in this scheduled task.
- Before the first real download attempt, do not inspect processes, Downloads, browser tabs, or full-page snapshots.
- Before the first real download attempt, do not write ad-hoc Playwright or fetch code.
- First run exactly:
  python3 scripts/scan_zsxq_download_candidates.py --window-start "$WINDOW_START" --window-end "$WINDOW_END" --job-config "$JOB_CONFIG_FILE" --keyword-file "$KEYWORDS_FILE" --output "$SCAN_PLAN_PATH"
- Treat "$SCAN_PLAN_PATH" as the immutable allow-list and source of scan counts for this run.
- If it contains candidates, download immediately. Never archive a file absent from that plan.
- For every planned file, use the canonical plan-bound Playwright helper:
  python3 scripts/download_zsxq_plan_file.py --job-config "$JOB_CONFIG_FILE" --scan-plan "$SCAN_PLAN_PATH" --file-id "<planned_file_id>" --cdp-endpoint "$CFT_CDP_ENDPOINT"
- Do not replace the helper with MCP browser_click, guessed coordinates, bounding boxes, browser_evaluate, or browser_run_code.
- Treat source_content_protected as a source-side web-download restriction; report it without retrying or bypassing it.
- Helper success is not archive proof. Only when the helper returns downloaded, reconcile that exact candidate immediately through the per-file finalizer with --wait-seconds 0. The helper has already completed save_as, PDF validation, and an atomic staging rename, so a per-file polling delay is unnecessary. Blocked results skip the per-file finalizer and are covered by final launcher reconciliation.
- After a completed download micro-batch, you may run this same command more than once:
  python3 scripts/finalize_download_batch.py --config "$JOB_CONFIG_FILE" --keywords "$KEYWORDS_FILE" --state "$STATE_FILE" --window-start "$WINDOW_START" --window-end "$WINDOW_END" --downloaded-after "$RUN_STARTED_AT" --run-id "$RUN_ID" --scan-plan "$SCAN_PLAN_PATH" --run-manifest "$RUN_MANIFEST_PATH" --wait-seconds 0 --skip-state-update
- Never run finalize with --commit-state. The launcher alone commits the checkpoint after deterministic reconciliation.
- At the end print one plain single-line ZSXQ_REPORT_JSON object. Its counts are advisory scan metadata only; the launcher derives downloaded files and success from "$RUN_MANIFEST_PATH".
- Required keys: window_new_docs_count, keyword_matched_docs_count, download_candidate_count, download_success_count, no_download_reason, core_reason.
- Use -1 for unknown counts and "unknown" for unknown reasons.
- Canonical no-download pairs:
  - no documents: no_download_reason=no_new_documents, core_reason=window_has_no_new_documents
  - no keyword match: no_download_reason=no_keyword_match, core_reason=window_has_updates_but_no_keyword_match
  - no updates: no_download_reason=no_window_updates, core_reason=window_has_no_updates
- If API probing fails and DOM fallback is used, print exactly: ZSXQ_SCAN_ALERT: api_unavailable_dom_fallback
- Also include scan_mode and api_probe_status when available.
EOF
RUNTIME_PROMPT_FILE="$TMP_PROMPT_FILE"

TS="$(date +"%Y-%m-%d_%H-%M-%S")"
LOG_FILE="$LOG_DIR/run_${TS}.log"

echo "[INFO] start at $(date -Iseconds)" | tee -a "$LOG_FILE"
echo "[INFO] cwd: $ROOT" | tee -a "$LOG_FILE"
echo "[INFO] browser executable: $CFT_EXECUTABLE_PATH" | tee -a "$LOG_FILE"
echo "[INFO] browser profile: $CFT_USER_DATA_DIR" | tee -a "$LOG_FILE"
echo "[INFO] prompt: $RUNTIME_PROMPT_FILE" | tee -a "$LOG_FILE"
echo "[INFO] run_started_at: $RUN_STARTED_AT" | tee -a "$LOG_FILE"
echo "[INFO] run_id: $RUN_ID" | tee -a "$LOG_FILE"
if [[ "$WINDOW_MODE" == "explicit" ]]; then
  echo "[INFO] mode: explicit window ($WINDOW_START -> $WINDOW_END)" | tee -a "$LOG_FILE"
else
  echo "[INFO] mode: frozen state window ($WINDOW_START -> $WINDOW_END)" | tee -a "$LOG_FILE"
fi
startup_log "run log created: $LOG_FILE"
startup_log "about to ensure cft running"
write_status "running" "ensure_cft_running" "checking dedicated browser"

set +e
CFT_READY_OUTPUT="$(ensure_cft_running 2>&1)"
CFT_READY_RC=$?
set -e
if [[ "$CFT_READY_RC" -ne 0 ]]; then
  startup_log "ensure_cft_running failed, rc=$CFT_READY_RC"
  RUN_FINISHED_AT="$(date -Iseconds)"
  write_structured_stub "$CFT_READY_RC" "blocked_browser" "blocked_browser_endpoint_unavailable" "blocked_browser" "" "" "" "$RUN_FINISHED_AT"
  write_status "blocked_browser" "ensure_cft_running" "dedicated browser not ready" "$CFT_READY_RC"
  echo "$CFT_READY_OUTPUT" | tee -a "$LOG_FILE"
  exit "$CFT_READY_RC"
fi
echo "$CFT_READY_OUTPUT" | tee -a "$LOG_FILE"
startup_log "ensure_cft_running passed"

startup_log "about to run playwright_preflight"
write_status "running" "playwright_preflight" "verifying browser session and login"
set +e
PLAYWRIGHT_PREFLIGHT_OUTPUT="$(playwright_preflight 2>&1)"
PLAYWRIGHT_PREFLIGHT_RC=$?
set -e
if [[ "$PLAYWRIGHT_PREFLIGHT_RC" -ne 0 ]]; then
  startup_log "playwright_preflight failed, rc=$PLAYWRIGHT_PREFLIGHT_RC"
  RUN_FINISHED_AT="$(date -Iseconds)"
  PREFLIGHT_REASON_CODE="$(preflight_diagnostic_field reason_code <<<"$PLAYWRIGHT_PREFLIGHT_OUTPUT")"
  case "$PREFLIGHT_REASON_CODE" in
    need_reauth)
      write_structured_stub "$PLAYWRIGHT_PREFLIGHT_RC" "need_reauth" "need_reauth" "blocked_login" "" "" "" "$RUN_FINISHED_AT"
      write_status "blocked_login" "playwright_preflight" "login needs manual refresh" "$PLAYWRIGHT_PREFLIGHT_RC"
      ;;
    zsxq_page_unavailable|zsxq_page_state_unrecognized)
      write_structured_stub "$PLAYWRIGHT_PREFLIGHT_RC" "$PREFLIGHT_REASON_CODE" "$PREFLIGHT_REASON_CODE" "blocked_site" "" "" "" "$RUN_FINISHED_AT"
      write_status "blocked_site" "playwright_preflight" "knowledge planet page check failed" "$PLAYWRIGHT_PREFLIGHT_RC"
      ;;
    blocked_browser_endpoint_unavailable|blocked_browser_cdp_unresponsive)
      write_structured_stub "$PLAYWRIGHT_PREFLIGHT_RC" "blocked_browser" "$PREFLIGHT_REASON_CODE" "blocked_browser" "" "" "" "$RUN_FINISHED_AT"
      write_status "blocked_browser" "playwright_preflight" "browser session check failed" "$PLAYWRIGHT_PREFLIGHT_RC"
      ;;
    *)
      PREFLIGHT_REASON_CODE="blocked_browser_unavailable_or_interrupted"
      write_structured_stub "$PLAYWRIGHT_PREFLIGHT_RC" "blocked_browser" "$PREFLIGHT_REASON_CODE" "blocked_browser" "" "" "" "$RUN_FINISHED_AT"
      write_status "blocked_browser" "playwright_preflight" "browser session check failed" "$PLAYWRIGHT_PREFLIGHT_RC"
      ;;
  esac
  startup_log "playwright_preflight diagnosis=$PREFLIGHT_REASON_CODE"
  echo "$PLAYWRIGHT_PREFLIGHT_OUTPUT" | tee -a "$LOG_FILE"
  exit "$PLAYWRIGHT_PREFLIGHT_RC"
fi
echo "$PLAYWRIGHT_PREFLIGHT_OUTPUT" | tee -a "$LOG_FILE"
startup_log "playwright_preflight passed"
write_status "running" "playwright_preflight" "browser session and login look ready"

CODEX_ATTEMPT=1
CODEX_MAX_ATTEMPTS=2
while true; do
  if [[ -n "${RAW_CODEX_OUTPUT_FILE:-}" && -f "$RAW_CODEX_OUTPUT_FILE" ]]; then
    rm -f "$RAW_CODEX_OUTPUT_FILE"
  fi
  RAW_CODEX_OUTPUT_FILE="$(mktemp /tmp/zsxq_codex_output.XXXXXX.log)"
  startup_log "created raw codex output file: $RAW_CODEX_OUTPUT_FILE"

  # The runtime guard gives the whole Codex/MCP process group a hard deadline.
  # It streams output unchanged and terminates descendants before returning 124.
  startup_log "about to run codex exec (attempt $CODEX_ATTEMPT/$CODEX_MAX_ATTEMPTS, timeout=${CODEX_TIMEOUT_SECONDS}s)"
  write_status "running" "codex_exec" "codex exec is running"
  start_heartbeat
  set +e
  python3 "$RUNTIME_GUARD" exec-timeout \
    --timeout-seconds "$CODEX_TIMEOUT_SECONDS" \
    --terminate-grace-seconds "$CODEX_TERMINATE_GRACE_SECONDS" \
    --stdin-file "$RUNTIME_PROMPT_FILE" \
    -- \
    "$CODEX_BIN" exec \
    --cd "$ROOT" \
    --model "$CODEX_MODEL" \
    -c "model_provider=\"$CODEX_MODEL_PROVIDER\"" \
    -c "model_reasoning_effort=\"$CODEX_REASONING_EFFORT\"" \
    --sandbox danger-full-access \
    --dangerously-bypass-approvals-and-sandbox \
    -c 'mcp_servers.playwright.command="npx"' \
    -c "mcp_servers.playwright.args=[\"-y\",\"@playwright/mcp@latest\",\"--cdp-endpoint\",\"$CFT_CDP_ENDPOINT\",\"--timeout-navigation\",\"120000\",\"--timeout-action\",\"$PLAYWRIGHT_ACTION_TIMEOUT_MS\"]" \
    - 2>&1 \
    | tee -a "$LOG_FILE" \
    | tee "$RAW_CODEX_OUTPUT_FILE"
  CODEX_RC=${PIPESTATUS[0]}
  set -e
  stop_heartbeat
  startup_log "codex exec finished, rc=$CODEX_RC (attempt $CODEX_ATTEMPT/$CODEX_MAX_ATTEMPTS)"

  if [[ "$CODEX_RC" -eq 124 ]]; then
    write_status "failed" "codex_exec" "codex exec exceeded hard timeout and was terminated" "$CODEX_RC"
  fi

  if [[ "$CODEX_RC" -eq 0 ]]; then
    break
  fi
  if raw_output_has_report_marker "$RAW_CODEX_OUTPUT_FILE"; then
    break
  fi
  if ! raw_output_has_cloud_requirements_timeout "$RAW_CODEX_OUTPUT_FILE"; then
    break
  fi
  if [[ "$CODEX_ATTEMPT" -ge "$CODEX_MAX_ATTEMPTS" ]]; then
    break
  fi

  echo "[WARN] codex cloud requirements timed out, retrying once" | tee -a "$LOG_FILE"
  startup_log "codex exec hit cloud requirements timeout, retrying once"
  write_status "running" "codex_exec" "codex cloud requirements timed out, retrying once" "$CODEX_RC"
  sleep 5
  CODEX_ATTEMPT=$((CODEX_ATTEMPT + 1))
done

write_status "running" "summarizing" "codex exec finished, summarizing result" "$CODEX_RC"

ensure_cft_running >/dev/null 2>&1 || true
ensure_keepalive_page >/dev/null 2>&1 || true

RUN_FINISHED_AT="$(date -Iseconds)"
RECOVERY_SUMMARY_FILE="$(mktemp /tmp/zsxq_finalize_reconcile.XXXXXX.json)"
if [[ -f "$SCAN_PLAN_PATH" ]]; then
  FINALIZE_RECONCILE_CMD=(
    python3 "$FINALIZE_SCRIPT"
    --config "$JOB_CONFIG_FILE"
    --keywords "$KEYWORDS_FILE"
    --state "$STATE_FILE"
    --window-start "$WINDOW_START"
    --window-end "$WINDOW_END"
    --downloaded-after "$RUN_STARTED_AT"
    --run-id "$RUN_ID"
    --scan-plan "$SCAN_PLAN_PATH"
    --run-manifest "$RUN_MANIFEST_PATH"
    --wait-seconds "${ZSXQ_FINALIZE_WAIT_SECONDS:-45}"
  )
  if [[ "$CODEX_RC" -eq 0 ]]; then
    FINALIZE_RECONCILE_CMD+=(--commit-state)
  else
    FINALIZE_RECONCILE_CMD+=(--skip-state-update)
  fi
  set +e
  "${FINALIZE_RECONCILE_CMD[@]}" >"$RECOVERY_SUMMARY_FILE" 2>>"$LOG_FILE"
  FINALIZE_RECONCILE_RC=$?
  set -e
  cat "$RECOVERY_SUMMARY_FILE" >>"$LOG_FILE" 2>/dev/null || true
  startup_log "final deterministic reconciliation rc=$FINALIZE_RECONCILE_RC"
else
  startup_log "final deterministic reconciliation skipped: scan plan missing"
fi

RUN_FINISHED_AT="$(date -Iseconds)"
python3 "$RESULT_HELPER" build \
  --state "$STATE_FILE" \
  --raw-output "$RAW_CODEX_OUTPUT_FILE" \
  --output "$STRUCTURED_RESULT_PATH" \
  --run-id "$RUN_ID" \
  --run-manifest "$RUN_MANIFEST_PATH" \
  --scan-plan "$SCAN_PLAN_PATH" \
  --run-started-at "$RUN_STARTED_AT" \
  --run-finished-at "$RUN_FINISHED_AT" \
  --requested-window-start "$WINDOW_START" \
  --requested-window-end "$WINDOW_END" \
  --pre-last-successful-check-at "$PRE_LAST_SUCCESSFUL_CHECK_AT" \
  --codex-rc "$CODEX_RC" >/dev/null
STRUCTURED_RESULT_WRITTEN="true"

FINAL_RESULT_INFO="$(python3 - "$STRUCTURED_RESULT_PATH" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
print(str(payload.get("status", "failed")).strip() or "failed")
print(str(payload.get("reason_text", "")).strip())
PY
)"
FINAL_STATUS="$(printf '%s\n' "$FINAL_RESULT_INFO" | sed -n '1p')"
FINAL_REASON="$(printf '%s\n' "$FINAL_RESULT_INFO" | sed -n '2p')"
FINAL_STATUS="${FINAL_STATUS:-failed}"
FINAL_MESSAGE="result ready"
if [[ -n "$FINAL_REASON" ]]; then
  FINAL_MESSAGE="result ready: $FINAL_REASON"
fi
write_status "$FINAL_STATUS" "done" "$FINAL_MESSAGE" "$CODEX_RC"

echo "[INFO] structured report: $STRUCTURED_RESULT_PATH" | tee -a "$LOG_FILE"
echo "[INFO] finished at $RUN_FINISHED_AT" | tee -a "$LOG_FILE"
exit "$CODEX_RC"
