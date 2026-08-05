#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTOMATION_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RUNTIME_DIR="${INVESTMENT_REPORTS_RUNTIME_DIR:-$AUTOMATION_ROOT/.runtime}"

export ZSXQ_PROMPT_FILE="$AUTOMATION_ROOT/prompts/openclaw_domestic_cicc_scheduler_prompt.md"
export ZSXQ_JOB_CONFIG_FILE="${ZSXQ_JOB_CONFIG_FILE:-$AUTOMATION_ROOT/config/local/zsxq_domestic_cicc_reports_job.json}"
export ZSXQ_KEYWORDS_FILE="${ZSXQ_KEYWORDS_FILE:-$AUTOMATION_ROOT/config/local/zsxq_domestic_cicc_keywords.json}"
export ZSXQ_STATE_FILE="${ZSXQ_STATE_FILE:-$RUNTIME_DIR/state/zsxq_domestic_cicc_reports_state.json}"
export ZSXQ_STRUCTURED_RESULT_PATH="${ZSXQ_STRUCTURED_RESULT_PATH:-$RUNTIME_DIR/logs/zsxq_domestic_cicc_last_run_structured.json}"
export ZSXQ_LOCK_WAIT_SECONDS="${ZSXQ_LOCK_WAIT_SECONDS:-900}"

exec bash "$AUTOMATION_ROOT/scripts/run_zsxq_task_via_codex.sh"
