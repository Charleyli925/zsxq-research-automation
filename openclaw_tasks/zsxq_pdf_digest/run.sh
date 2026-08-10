#!/usr/bin/env bash
# Compatibility entry point for the digest cron task.
#
# The scheduler still invokes this file, but the digest implementation lives in
# the Python pipeline.  This wrapper deliberately has no OpenClaw agent,
# session, registry, or credential handling: it only loads the task's local
# business configuration and starts one ``zsxq-pipeline process`` invocation.

set -euo pipefail

RUNTIME_TASK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
SCRIPT_REALPATH="$(python3 - "${BASH_SOURCE[0]}" <<'PY'
from pathlib import Path
import sys

print(Path(sys.argv[1]).expanduser().resolve())
PY
)"
SOURCE_TASK_DIR="$(cd "$(dirname "$SCRIPT_REALPATH")" && pwd -P)"
SOURCE_ROOT="$(cd "$SOURCE_TASK_DIR/../.." && pwd -P)"
cd "$RUNTIME_TASK_DIR"

CONFIG_PATH="$RUNTIME_TASK_DIR/config.env"
if [[ ! -f "$CONFIG_PATH" ]]; then
  CONFIG_PATH="$SOURCE_TASK_DIR/config.env"
fi
if [[ ! -f "$CONFIG_PATH" ]]; then
  printf '缺少配置文件：%s\n' "$CONFIG_PATH" >&2
  exit 2
fi

# The file is intentionally user-owned and Git-ignored.  Export its settings
# so the Python process receives the same compatibility variables as the old
# task did; it never reads OpenClaw-specific configuration.
set -a
# shellcheck disable=SC1090
source "$CONFIG_PATH"
set +a

# A pre-migration config may still carry its approved reasoning level under
# the old name.  Preserve that value for this one compatibility boundary; the
# new template no longer writes or requires any agent configuration.
if [[ -z "${CODEX_REASONING:-}" && -n "${SUMMARY_AGENT_THINKING:-}" ]]; then
  export CODEX_REASONING="$SUMMARY_AGENT_THINKING"
fi

# The resolved entrypoint, not a user-owned config value, is the release
# authority.  Letting config.env select another valid checkout could run a
# stale tree while the scheduler appears to be on the installed release.
AUTOMATION_ROOT="$SOURCE_ROOT"
PYTHON_BIN="${PYTHON_BIN:-python3}"
if [[ ! -d "$AUTOMATION_ROOT/src/zsxq_pipeline" ]]; then
  printf '找不到 zsxq_pipeline 源码：%s\n' "$AUTOMATION_ROOT/src/zsxq_pipeline" >&2
  exit 2
fi

export AUTOMATION_ROOT
export PYTHONPATH="$AUTOMATION_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

exec "$PYTHON_BIN" -m zsxq_pipeline.cli process --runtime-root "$RUNTIME_TASK_DIR" "$@"
