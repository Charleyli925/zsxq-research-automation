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
  local runtime_path="$RUNTIME_TASK_DIR/run.worker.sh"
  if [[ -f "$runtime_path" ]]; then
    printf '%s\n' "$runtime_path"
    return 0
  fi
  printf '%s\n' "$SOURCE_TASK_DIR/run.worker.sh"
}

resolve_task_asset_path() {
  local relative_path="$1"
  local runtime_path="$RUNTIME_TASK_DIR/$relative_path"
  if [[ -e "$runtime_path" ]]; then
    printf '%s\n' "$runtime_path"
    return 0
  fi
  printf '%s\n' "$SOURCE_TASK_DIR/$relative_path"
}

WORKER_ENTRY_PATH="$(resolve_worker_path)"
if [[ ! -f "$WORKER_ENTRY_PATH" ]]; then
  printf '缺少 worker 入口：%s\n' "$WORKER_ENTRY_PATH" >&2
  exit 1
fi

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
import sys
import time
from pathlib import Path

output_path = Path(sys.argv[1])
args = sys.argv[2:]
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

payload = {"records": records}
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
WORKFLOW_FINGERPRINT_MANIFEST_PATH="$(build_workflow_fingerprint_manifest \
  "$SNAPSHOT_DIR/workflow_fingerprint_manifest.json" \
  "config" "$CONFIG_SOURCE_PATH" \
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
