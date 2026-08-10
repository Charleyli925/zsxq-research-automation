#!/usr/bin/env bash
# This file is the stable entry.
# It snapshots `run.worker.sh`, then runs that snapshot, so editing the repo will not tear a live batch in half.

set -euo pipefail

RUNTIME_TASK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_REALPATH="$(python3 - "${BASH_SOURCE[0]}" <<'PY'
from pathlib import Path
import sys

print(Path(sys.argv[1]).expanduser().resolve())
PY
)"
SOURCE_TASK_DIR="$(cd "$(dirname "$SCRIPT_REALPATH")" && pwd)"
cd "$RUNTIME_TASK_DIR"

CONFIG_SOURCE_PATH="$RUNTIME_TASK_DIR/config.env"
if [[ ! -f "$CONFIG_SOURCE_PATH" ]]; then
  CONFIG_SOURCE_PATH="$SOURCE_TASK_DIR/config.env"
fi

resolve_worker_path() {
  if [[ "${RELEASE_DEPLOYMENT_ACTIVE:-false}" == "true" ]]; then
    if [[ -n "${AUTOMATION_ROOT:-}" ]]; then
      local release_path="$AUTOMATION_ROOT/openclaw_tasks/zsxq_pdf_digest/run.worker.sh"
      if [[ -f "$release_path" ]]; then
        printf '%s\n' "$release_path"
        return 0
      fi
    fi
    # A malformed deployment.env must not make us prefer a stale task-local
    # worker.  The entrypoint's own release copy understands the blocker and
    # can emit the structured blocked result instead.
    local source_path="$SOURCE_TASK_DIR/run.worker.sh"
    if [[ -f "$source_path" ]]; then
      printf '%s\n' "$source_path"
      return 0
    fi
  fi
  local runtime_path="$RUNTIME_TASK_DIR/run.worker.sh"
  if [[ -f "$runtime_path" ]]; then
    printf '%s\n' "$runtime_path"
    return 0
  fi
  printf '%s\n' "$SOURCE_TASK_DIR/run.worker.sh"
}

resolve_task_asset_path() {
  local relative_path="$1"
  if [[ "${RELEASE_DEPLOYMENT_ACTIVE:-false}" == "true" && -n "${AUTOMATION_ROOT:-}" ]]; then
    local release_path="$AUTOMATION_ROOT/openclaw_tasks/zsxq_pdf_digest/$relative_path"
    if [[ -e "$release_path" ]]; then
      printf '%s\n' "$release_path"
      return 0
    fi
  fi
  local runtime_path="$RUNTIME_TASK_DIR/$relative_path"
  if [[ -e "$runtime_path" ]]; then
    printf '%s\n' "$runtime_path"
    return 0
  fi
  printf '%s\n' "$SOURCE_TASK_DIR/$relative_path"
}

SNAPSHOT_DIR="$(mktemp -d "${TMPDIR:-/tmp}/zsxq_pdf_digest_run.XXXXXX")"
cleanup() {
  if [[ -n "${SNAPSHOT_DIR:-}" && -d "$SNAPSHOT_DIR" ]]; then
    rm -rf "$SNAPSHOT_DIR"
  fi
}
trap cleanup EXIT

snapshot_file_if_present() {
  local source_path="$1"
  local target_name="$2"
  local executable="${3:-false}"
  if [[ ! -f "$source_path" ]]; then
    printf '\n'
    return 0
  fi
  local snapshot_path="$SNAPSHOT_DIR/$target_name"
  cp "$source_path" "$snapshot_path"
  if [[ "$executable" == "true" ]]; then
    chmod +x "$snapshot_path"
  fi
  printf '%s\n' "$snapshot_path"
}

resolve_sibling_file_if_present() {
  local source_path="$1"
  local sibling_name="$2"
  if [[ -z "$source_path" || ! -f "$source_path" ]]; then
    printf '\n'
    return 0
  fi
  local sibling_path
  sibling_path="$(cd "$(dirname "$source_path")" && pwd)/$sibling_name"
  if [[ -f "$sibling_path" ]]; then
    printf '%s\n' "$sibling_path"
    return 0
  fi
  printf '\n'
}

CONFIG_SNAPSHOT_PATH="$(snapshot_file_if_present "$CONFIG_SOURCE_PATH" "config.env")"
if [[ -z "$CONFIG_SNAPSHOT_PATH" ]]; then
  printf '缺少配置文件：%s\n' "$CONFIG_SOURCE_PATH" >&2
  exit 1
fi

# shellcheck disable=SC1091
source "$CONFIG_SNAPSHOT_PATH"
PYTHON_BIN="${PYTHON_BIN:-python3}"

DEPLOYMENT_SOURCE_PATH="$RUNTIME_TASK_DIR/deployment.env"
RELEASE_DEPLOYMENT_ACTIVE="false"
RELEASE_CONTRACT_ERROR=""
RELEASE_ROOT=""
RELEASE_GIT_SHA=""

load_release_deployment_env() {
  local source_path="$1"
  local canonical_path="$SNAPSHOT_DIR/deployment.env"
  local validation_output=""
  set +e
  validation_output="$($PYTHON_BIN - "$source_path" "$canonical_path" <<'PY'
from pathlib import Path
import re
import shlex
import sys

source_path = Path(sys.argv[1])
canonical_path = Path(sys.argv[2])
allowed = {
    "AUTOMATION_ROOT",
    "HELPER_SCRIPT_PATH",
    "SCANNER_SCRIPT_PATH",
    "RESEARCH_LIBRARY_INDEX_SCRIPT_PATH",
    "MARKITDOWN_SCRIPT_PATH",
    "CLEAN_MARKDOWN_SCRIPT_PATH",
    "OBSIDIAN_ARCHIVE_SCRIPT_PATH",
    "OBSIDIAN_INDEX_SCRIPT_PATH",
    "RUNTIME_GUARD_SCRIPT_PATH",
}
required = set(allowed)
assignments: dict[str, str] = {}
for line_number, raw_line in enumerate(source_path.read_text(encoding="utf-8").splitlines(), start=1):
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", line)
    if not match:
        raise SystemExit(f"deployment.env line {line_number} is not a plain assignment")
    key, raw_value = match.groups()
    if key not in allowed:
        raise SystemExit(f"deployment.env may not set {key}; only release source paths are allowed")
    try:
        values = shlex.split(raw_value, posix=True)
    except ValueError as exc:
        raise SystemExit(f"deployment.env line {line_number} is not shell-quoted safely: {exc}") from exc
    if len(values) != 1:
        raise SystemExit(f"deployment.env line {line_number} must contain one path value")
    value = values[0]
    if not value or "\x00" in value or any(token in value for token in ("$", chr(96), "\n", "\r")):
        raise SystemExit(f"deployment.env line {line_number} has an unsafe source path")
    assignments[key] = value

missing = sorted(required - set(assignments))
if missing:
    raise SystemExit("deployment.env is missing required release paths: " + ", ".join(missing))

canonical_path.write_text(
    "\n".join(f"{key}={shlex.quote(assignments[key])}" for key in sorted(assignments)) + "\n",
    encoding="utf-8",
)
print("ok")
PY
)"
  local validation_rc=$?
  set -e
  if [[ "$validation_rc" -ne 0 ]]; then
    printf '%s\n' "$validation_output"
    return 1
  fi
  # The Python validator writes canonical, literal-only assignments.  Sourcing
  # this snapshot cannot execute shell substitutions from task-local files.
  # shellcheck disable=SC1090
  source "$canonical_path"
  cat "$canonical_path" >> "$CONFIG_SNAPSHOT_PATH"
  return 0
}

if [[ -f "$DEPLOYMENT_SOURCE_PATH" ]]; then
  RELEASE_DEPLOYMENT_ACTIVE="true"
  DEPLOYMENT_VALIDATION_LOG="$SNAPSHOT_DIR/deployment-validation.log"
  if ! load_release_deployment_env "$DEPLOYMENT_SOURCE_PATH" >"$DEPLOYMENT_VALIDATION_LOG" 2>&1; then
    RELEASE_DEPLOYMENT_ERROR_OUTPUT="$(cat "$DEPLOYMENT_VALIDATION_LOG")"
    RELEASE_CONTRACT_ERROR="deployment_env_invalid: ${RELEASE_DEPLOYMENT_ERROR_OUTPUT}"
  fi
fi

if [[ "$RELEASE_DEPLOYMENT_ACTIVE" == "true" && -z "$RELEASE_CONTRACT_ERROR" ]]; then
  if [[ ! -d "${AUTOMATION_ROOT:-}" ]]; then
    RELEASE_CONTRACT_ERROR="release_root_missing: ${AUTOMATION_ROOT:-<unset>}"
  else
    RELEASE_ROOT="$(cd "$AUTOMATION_ROOT" && pwd -P)"
    if ! RELEASE_GIT_TOPLEVEL="$(git -C "$RELEASE_ROOT" rev-parse --show-toplevel 2>/dev/null)"; then
      RELEASE_CONTRACT_ERROR="release_root_not_git_checkout: $RELEASE_ROOT"
    elif [[ "$(cd "$RELEASE_GIT_TOPLEVEL" && pwd -P)" != "$RELEASE_ROOT" ]]; then
      RELEASE_CONTRACT_ERROR="release_root_not_checkout_root: $RELEASE_ROOT"
    elif ! RELEASE_GIT_SHA="$(git -C "$RELEASE_ROOT" rev-parse HEAD 2>/dev/null)"; then
      RELEASE_CONTRACT_ERROR="release_git_sha_unavailable: $RELEASE_ROOT"
    elif ! ENTRYPOINT_GIT_TOPLEVEL="$(git -C "$SOURCE_TASK_DIR" rev-parse --show-toplevel 2>/dev/null)"; then
      RELEASE_CONTRACT_ERROR="entrypoint_not_git_checkout: $SOURCE_TASK_DIR"
    elif [[ "$(cd "$ENTRYPOINT_GIT_TOPLEVEL" && pwd -P)" != "$RELEASE_ROOT" ]]; then
      RELEASE_CONTRACT_ERROR="release_root_entrypoint_mismatch: entrypoint=$ENTRYPOINT_GIT_TOPLEVEL deployment=$RELEASE_ROOT"
    fi
  fi
fi

WORKER_ENTRY_PATH="$(resolve_worker_path)"
if [[ ! -f "$WORKER_ENTRY_PATH" ]]; then
  printf '缺少 worker 入口：%s\n' "$WORKER_ENTRY_PATH" >&2
  exit 1
fi
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

SUMMARY_AGENT_AUTH_ARGS=(
  "summary_agent_auth" "${HOME}/.openclaw/agents/${SUMMARY_AGENT_ID}/agent/auth-profiles.json"
)
if [[ "$SUMMARY_PARALLEL_ENABLED" == "true" && "$SUMMARY_WORKER_COUNT" -gt 1 ]]; then
  for ((worker_index = 1; worker_index <= SUMMARY_WORKER_COUNT; worker_index++)); do
    SUMMARY_AGENT_AUTH_ARGS+=(
      "summary_worker_${worker_index}_agent_auth" "${HOME}/.openclaw/agents/${SUMMARY_WORKER_AGENT_ID_PREFIX}${worker_index}/agent/auth-profiles.json"
    )
  done
fi

build_workflow_fingerprint_manifest() {
  local output_path="$1"
  shift
  "$PYTHON_BIN" - "$output_path" "$@" <<'PY'
import hashlib
import json
import os
import sys
import time
from pathlib import Path

output_path = Path(sys.argv[1])
args = sys.argv[2:]
records = []
now_ms = int(time.time() * 1000)
release_root_raw = os.environ.get("ZSXQ_RELEASE_ROOT", "").strip()
release_root = Path(release_root_raw).resolve(strict=False) if release_root_raw else None
release_git_sha = os.environ.get("ZSXQ_RELEASE_GIT_SHA", "").strip()


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
            "release_relative_path": (
                str(path.relative_to(release_root))
                if release_root is not None and path.is_relative_to(release_root)
                else None
            ),
        }
    )

payload = {
    "git_sha": release_git_sha or None,
    "release_root": str(release_root) if release_root is not None else None,
    "records": records,
}
output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(str(output_path))
PY
}

WORKER_SNAPSHOT_PATH="$(snapshot_file_if_present "$WORKER_ENTRY_PATH" "run.worker.sh" "true")"
HELPER_SOURCE_PATH="$HELPER_SCRIPT_PATH"
SCANNER_SOURCE_PATH="$SCANNER_SCRIPT_PATH"
INDEX_SOURCE_PATH="${RESEARCH_LIBRARY_INDEX_SCRIPT_PATH:-}"
MARKITDOWN_SOURCE_PATH="${MARKITDOWN_SCRIPT_PATH:-}"
CLEAN_MARKDOWN_SOURCE_PATH="${CLEAN_MARKDOWN_SCRIPT_PATH:-}"
OBSIDIAN_ARCHIVE_SOURCE_PATH="${OBSIDIAN_ARCHIVE_SCRIPT_PATH:-}"
OBSIDIAN_INDEX_SOURCE_PATH="${OBSIDIAN_INDEX_SCRIPT_PATH:-}"
SUMMARY_PROMPT_SOURCE_PATH="$(resolve_task_asset_path "summary_prompt.md")"
SUMMARY_SYSTEM_PROMPT_SOURCE_PATH="$(resolve_task_asset_path "summary_system_prompt.md")"
EXTRACT_TEXT_SOURCE_PATH="$(resolve_task_asset_path "extract_pdf_text.py")"
KB_COMMON_SOURCE_PATH="$(resolve_sibling_file_if_present "$OBSIDIAN_ARCHIVE_SOURCE_PATH" "kb_common.py")"
if [[ -z "$KB_COMMON_SOURCE_PATH" ]]; then
  KB_COMMON_SOURCE_PATH="$(resolve_sibling_file_if_present "$OBSIDIAN_INDEX_SOURCE_PATH" "kb_common.py")"
fi
if [[ -z "$KB_COMMON_SOURCE_PATH" ]]; then
  KB_COMMON_SOURCE_PATH="$(resolve_sibling_file_if_present "$HELPER_SOURCE_PATH" "kb_common.py")"
fi
RUNTIME_PATHS_SOURCE_PATH="$(resolve_sibling_file_if_present "$INDEX_SOURCE_PATH" "runtime_paths.py")"
if [[ -z "$RUNTIME_PATHS_SOURCE_PATH" ]]; then
  RUNTIME_PATHS_SOURCE_PATH="$(resolve_sibling_file_if_present "$HELPER_SOURCE_PATH" "runtime_paths.py")"
fi
RUNTIME_GUARD_SOURCE_PATH="${RUNTIME_GUARD_SCRIPT_PATH:-}"
if [[ -z "$RUNTIME_GUARD_SOURCE_PATH" ]]; then
  RUNTIME_GUARD_SOURCE_PATH="$(resolve_sibling_file_if_present "$HELPER_SOURCE_PATH" "zsxq_runtime_guard.py")"
fi

if [[ "$RELEASE_DEPLOYMENT_ACTIVE" == "true" && -n "$RELEASE_ROOT" ]]; then
  # Do not let a runtime copy of these Python sidecars become an accidental
  # second code source while the release deployment is active.
  KB_COMMON_SOURCE_PATH="$RELEASE_ROOT/scripts/kb_common.py"
  RUNTIME_PATHS_SOURCE_PATH="$RELEASE_ROOT/scripts/runtime_paths.py"
fi

validate_release_dependency_paths() {
  local root_path="$1"
  shift
  "$PYTHON_BIN" - "$root_path" "$@" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1]).expanduser().resolve(strict=False)
args = sys.argv[2:]
errors: list[str] = []
if not root.is_dir():
    errors.append(f"release root does not exist: {root}")
if len(args) % 2:
    errors.append("release dependency argument list is malformed")
for index in range(0, len(args) - 1, 2):
    label = str(args[index]).strip()
    raw_path = str(args[index + 1]).strip()
    if not raw_path:
        errors.append(f"{label} source path is empty")
        continue
    path = Path(raw_path).expanduser().resolve(strict=False)
    if not path.is_file():
        errors.append(f"{label} source missing: {path}")
        continue
    try:
        path.relative_to(root)
    except ValueError:
        errors.append(f"{label} source crosses release boundary: {path}")
if errors:
    print("; ".join(errors))
    raise SystemExit(1)
PY
}

if [[ "$RELEASE_DEPLOYMENT_ACTIVE" == "true" && -z "$RELEASE_CONTRACT_ERROR" ]]; then
  if ! RELEASE_PATH_VALIDATION_OUTPUT="$(validate_release_dependency_paths "$RELEASE_ROOT" \
    "worker" "$WORKER_ENTRY_PATH" \
    "helper" "$HELPER_SOURCE_PATH" \
    "scanner" "$SCANNER_SOURCE_PATH" \
    "research_library_index" "$INDEX_SOURCE_PATH" \
    "markitdown" "$MARKITDOWN_SOURCE_PATH" \
    "clean_markdown" "$CLEAN_MARKDOWN_SOURCE_PATH" \
    "obsidian_archive" "$OBSIDIAN_ARCHIVE_SOURCE_PATH" \
    "obsidian_index" "$OBSIDIAN_INDEX_SOURCE_PATH" \
    "runtime_guard" "$RUNTIME_GUARD_SOURCE_PATH" \
    "kb_common" "$KB_COMMON_SOURCE_PATH" \
    "runtime_paths" "$RUNTIME_PATHS_SOURCE_PATH" \
    "summary_prompt" "$SUMMARY_PROMPT_SOURCE_PATH" \
    "summary_system_prompt" "$SUMMARY_SYSTEM_PROMPT_SOURCE_PATH" \
    "extract_text" "$EXTRACT_TEXT_SOURCE_PATH" 2>&1)"; then
    RELEASE_CONTRACT_ERROR="release_source_boundary_mismatch: ${RELEASE_PATH_VALIDATION_OUTPUT}"
  fi
fi

if [[ -n "$RELEASE_ROOT" ]]; then
  export ZSXQ_RELEASE_ROOT="$RELEASE_ROOT"
fi
if [[ -n "$RELEASE_GIT_SHA" ]]; then
  export ZSXQ_RELEASE_GIT_SHA="$RELEASE_GIT_SHA"
fi

HELPER_SNAPSHOT_PATH="$(snapshot_file_if_present "$HELPER_SOURCE_PATH" "manage_zsxq_digest_batch.py")"
SCANNER_SNAPSHOT_PATH="$(snapshot_file_if_present "$SCANNER_SOURCE_PATH" "scan_new_zsxq_pdfs.py")"
INDEX_SNAPSHOT_PATH="$(snapshot_file_if_present "$INDEX_SOURCE_PATH" "research_library_index.py")"
MARKITDOWN_SNAPSHOT_PATH="$(snapshot_file_if_present "$MARKITDOWN_SOURCE_PATH" "convert_with_markitdown.py")"
CLEAN_MARKDOWN_SNAPSHOT_PATH="$(snapshot_file_if_present "$CLEAN_MARKDOWN_SOURCE_PATH" "build_clean_markdown.py")"
OBSIDIAN_ARCHIVE_SNAPSHOT_PATH="$(snapshot_file_if_present "$OBSIDIAN_ARCHIVE_SOURCE_PATH" "archive_to_obsidian.py")"
OBSIDIAN_INDEX_SNAPSHOT_PATH="$(snapshot_file_if_present "$OBSIDIAN_INDEX_SOURCE_PATH" "update_obsidian_indexes.py")"
KB_COMMON_SNAPSHOT_PATH="$(snapshot_file_if_present "$KB_COMMON_SOURCE_PATH" "kb_common.py")"
RUNTIME_PATHS_SNAPSHOT_PATH="$(snapshot_file_if_present "$RUNTIME_PATHS_SOURCE_PATH" "runtime_paths.py")"
RUNTIME_GUARD_SNAPSHOT_PATH="$(snapshot_file_if_present "$RUNTIME_GUARD_SOURCE_PATH" "zsxq_runtime_guard.py" "true")"
SUMMARY_PROMPT_SNAPSHOT_PATH="$(snapshot_file_if_present "$SUMMARY_PROMPT_SOURCE_PATH" "summary_prompt.md")"
SUMMARY_SYSTEM_PROMPT_SNAPSHOT_PATH="$(snapshot_file_if_present "$SUMMARY_SYSTEM_PROMPT_SOURCE_PATH" "summary_system_prompt.md")"
EXTRACT_TEXT_SNAPSHOT_PATH="$(snapshot_file_if_present "$EXTRACT_TEXT_SOURCE_PATH" "extract_pdf_text.py" "true")"

validate_helper_contract_snapshot() {
  local helper_path="$1"
  local output=""
  set +e
  output="$($PYTHON_BIN "$helper_path" contract-version 2>&1)"
  local contract_rc=$?
  set -e
  if [[ "$contract_rc" -ne 0 ]]; then
    printf 'helper contract-version failed: %s\n' "$output"
    return 1
  fi
  if ! HELPER_CONTRACT_PAYLOAD="$($PYTHON_BIN - "$output" <<'PY'
import json
import sys

raw = sys.argv[1]
try:
    payload = json.loads(raw)
except Exception as exc:
    raise SystemExit(f"contract-version did not return JSON: {exc}") from exc
if payload.get("schema_version") != 1:
    raise SystemExit("helper contract schema version is unsupported")
if payload.get("contract_version") != "zsxq-digest-batch/v1":
    raise SystemExit("helper contract version is unsupported")
commands = payload.get("commands") if isinstance(payload.get("commands"), dict) else {}
recovery = commands.get("lookup-publish-recovery") if isinstance(commands.get("lookup-publish-recovery"), dict) else {}
arguments = set(recovery.get("arguments") or [])
required = {"--records-file", "--batch-hash", "--summary-hash", "--batch-file"}
missing = sorted(required - arguments)
if missing:
    raise SystemExit("helper lookup-publish-recovery lacks required arguments: " + ", ".join(missing))
print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
PY
)"; then
    printf '%s\n' "$HELPER_CONTRACT_PAYLOAD"
    return 1
  fi
  printf '%s\n' "$HELPER_CONTRACT_PAYLOAD" > "$SNAPSHOT_DIR/helper_contract.json"
}

if [[ "$RELEASE_DEPLOYMENT_ACTIVE" == "true" && -z "$RELEASE_CONTRACT_ERROR" ]]; then
  if [[ -z "$HELPER_SNAPSHOT_PATH" ]]; then
    RELEASE_CONTRACT_ERROR="release_contract_mismatch: helper snapshot is missing"
  elif ! RELEASE_CONTRACT_VALIDATION_OUTPUT="$(validate_helper_contract_snapshot "$HELPER_SNAPSHOT_PATH" 2>&1)"; then
    RELEASE_CONTRACT_ERROR="release_contract_mismatch: ${RELEASE_CONTRACT_VALIDATION_OUTPUT}"
  fi
fi
WORKFLOW_FINGERPRINT_MANIFEST_PATH="$(build_workflow_fingerprint_manifest \
  "$SNAPSHOT_DIR/workflow_fingerprint_manifest.json" \
  "config" "$CONFIG_SOURCE_PATH" \
  "deployment" "$DEPLOYMENT_SOURCE_PATH" \
  "worker" "$WORKER_ENTRY_PATH" \
  "helper" "$HELPER_SOURCE_PATH" \
  "scanner" "$SCANNER_SOURCE_PATH" \
  "research_library_index" "$INDEX_SOURCE_PATH" \
  "markitdown" "$MARKITDOWN_SOURCE_PATH" \
  "clean_markdown" "$CLEAN_MARKDOWN_SOURCE_PATH" \
  "obsidian_archive" "$OBSIDIAN_ARCHIVE_SOURCE_PATH" \
  "obsidian_index" "$OBSIDIAN_INDEX_SOURCE_PATH" \
  "kb_common" "$KB_COMMON_SOURCE_PATH" \
  "runtime_paths" "$RUNTIME_PATHS_SOURCE_PATH" \
  "runtime_guard" "$RUNTIME_GUARD_SOURCE_PATH" \
  "summary_prompt" "$SUMMARY_PROMPT_SOURCE_PATH" \
  "summary_system_prompt" "$SUMMARY_SYSTEM_PROMPT_SOURCE_PATH" \
  "extract_text" "$EXTRACT_TEXT_SOURCE_PATH" \
  "openclaw_config" "${HOME}/.openclaw/openclaw.json" \
  "device_auth" "${HOME}/.openclaw/identity/device-auth.json" \
  "main_agent_auth" "${HOME}/.openclaw/agents/main/agent/auth-profiles.json" \
  "${SUMMARY_AGENT_AUTH_ARGS[@]}")"

export ZSXQ_RUNTIME_TASK_DIR="$RUNTIME_TASK_DIR"
export ZSXQ_SOURCE_TASK_DIR="$SOURCE_TASK_DIR"
export ZSXQ_TASK_SCRIPT_REALPATH="$WORKER_ENTRY_PATH"
export ZSXQ_CONFIG_PATH="$CONFIG_SNAPSHOT_PATH"
export ZSXQ_WORKFLOW_FINGERPRINT_MANIFEST_PATH="$WORKFLOW_FINGERPRINT_MANIFEST_PATH"
export ZSXQ_RELEASE_DEPLOYMENT_ACTIVE="$RELEASE_DEPLOYMENT_ACTIVE"
if [[ -n "$RELEASE_CONTRACT_ERROR" ]]; then
  export ZSXQ_RELEASE_CONTRACT_ERROR="$RELEASE_CONTRACT_ERROR"
fi

if [[ -n "$HELPER_SNAPSHOT_PATH" ]]; then
  export ZSXQ_HELPER_SCRIPT_PATH="$HELPER_SNAPSHOT_PATH"
fi
if [[ -n "$SCANNER_SNAPSHOT_PATH" ]]; then
  export ZSXQ_SCANNER_SCRIPT_PATH="$SCANNER_SNAPSHOT_PATH"
fi
if [[ -n "$INDEX_SNAPSHOT_PATH" ]]; then
  export ZSXQ_RESEARCH_LIBRARY_INDEX_SCRIPT_PATH="$INDEX_SNAPSHOT_PATH"
fi
if [[ -n "$MARKITDOWN_SNAPSHOT_PATH" ]]; then
  export ZSXQ_MARKITDOWN_SCRIPT_PATH="$MARKITDOWN_SNAPSHOT_PATH"
fi
if [[ -n "$CLEAN_MARKDOWN_SNAPSHOT_PATH" ]]; then
  export ZSXQ_CLEAN_MARKDOWN_SCRIPT_PATH="$CLEAN_MARKDOWN_SNAPSHOT_PATH"
fi
if [[ -n "$OBSIDIAN_ARCHIVE_SNAPSHOT_PATH" ]]; then
  export ZSXQ_OBSIDIAN_ARCHIVE_SCRIPT_PATH="$OBSIDIAN_ARCHIVE_SNAPSHOT_PATH"
fi
if [[ -n "$OBSIDIAN_INDEX_SNAPSHOT_PATH" ]]; then
  export ZSXQ_OBSIDIAN_INDEX_SCRIPT_PATH="$OBSIDIAN_INDEX_SNAPSHOT_PATH"
fi
if [[ -n "$KB_COMMON_SNAPSHOT_PATH" ]]; then
  export ZSXQ_KB_COMMON_SCRIPT_PATH="$KB_COMMON_SNAPSHOT_PATH"
fi
if [[ -n "$RUNTIME_PATHS_SNAPSHOT_PATH" ]]; then
  export ZSXQ_RUNTIME_PATHS_SCRIPT_PATH="$RUNTIME_PATHS_SNAPSHOT_PATH"
fi
if [[ -n "$RUNTIME_GUARD_SNAPSHOT_PATH" ]]; then
  export ZSXQ_RUNTIME_GUARD_SCRIPT_PATH="$RUNTIME_GUARD_SNAPSHOT_PATH"
fi
if [[ -n "$SUMMARY_PROMPT_SNAPSHOT_PATH" ]]; then
  export ZSXQ_SUMMARY_PROMPT_PATH="$SUMMARY_PROMPT_SNAPSHOT_PATH"
fi
if [[ -n "$SUMMARY_SYSTEM_PROMPT_SNAPSHOT_PATH" ]]; then
  export ZSXQ_SUMMARY_SYSTEM_PROMPT_PATH="$SUMMARY_SYSTEM_PROMPT_SNAPSHOT_PATH"
fi
if [[ -n "$EXTRACT_TEXT_SNAPSHOT_PATH" ]]; then
  export ZSXQ_EXTRACT_TEXT_SCRIPT_PATH="$EXTRACT_TEXT_SNAPSHOT_PATH"
fi

"$WORKER_SNAPSHOT_PATH" "$@"
