#!/usr/bin/env bash
# Install version-controlled ZSXQ task wrappers into a local macOS runtime.
#
# This intentionally defaults to a dry run.  --apply only accepts a clean,
# detached Git checkout, so a scheduler can never silently run a dirty daily
# development clone.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
RELEASE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd -P)"
DEPLOY_PYTHON_BIN="${PYTHON_BIN:-python3}"

APPLY=0
SKIP_LAUNCHD=0
ALLOW_DIRTY=0
ALLOW_BRANCH=0
TASKS_ROOT="${OPENCLAW_TASKS_ROOT:-$HOME/.openclaw/workspace/tasks}"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
FOREIGN_TASK_DIR=""
DOMESTIC_TASK_DIR=""
FOREIGN_LABEL="com.investment-reports.zsxq-autodownload"
DOMESTIC_LABEL="com.investment-reports.zsxq-domestic-cicc"

usage() {
  cat <<'EOF'
Usage: bash deploy/install_local_runtime.sh [options]

Installs the version-controlled ZSXQ download wrappers and renders two user
LaunchAgents.  The default is a read-only dry run.

  --apply                         Perform the installation.
  --dry-run                       Show validated targets without changing them (default).
  --tasks-root PATH               Parent directory containing the two task directories.
  --foreign-task-dir PATH         Foreign-download task directory.
  --domestic-task-dir PATH        Domestic-CICC task directory.
  --launch-agents-dir PATH        Destination for rendered user LaunchAgents.
  --foreign-label LABEL           LaunchAgent label for the foreign task.
  --domestic-label LABEL          LaunchAgent label for the domestic task.
  --skip-launchd                  Install files but do not reload LaunchAgents.
  --allow-dirty                   Permit a dirty checkout (emergency/testing only).
  --allow-branch                  Permit a branch checkout (emergency/testing only).
  --help                          Show this help.

Both task directories must already contain their private config.env.  The
installer never edits config.env; it writes a Git-ignored deployment.env with
the checked-out code paths and creates recoverable backups of old wrappers.
EOF
}

die() {
  printf 'install_local_runtime: %s\n' "$*" >&2
  exit 2
}

need_value() {
  [[ $# -ge 2 && -n "${2:-}" ]] || die "missing value for $1"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --apply)
      APPLY=1
      shift
      ;;
    --dry-run)
      APPLY=0
      shift
      ;;
    --tasks-root|--foreign-task-dir|--domestic-task-dir|--launch-agents-dir|--foreign-label|--domestic-label)
      need_value "$1" "${2:-}"
      case "$1" in
        --tasks-root) TASKS_ROOT="$2" ;;
        --foreign-task-dir) FOREIGN_TASK_DIR="$2" ;;
        --domestic-task-dir) DOMESTIC_TASK_DIR="$2" ;;
        --launch-agents-dir) LAUNCH_AGENTS_DIR="$2" ;;
        --foreign-label) FOREIGN_LABEL="$2" ;;
        --domestic-label) DOMESTIC_LABEL="$2" ;;
      esac
      shift 2
      ;;
    --skip-launchd)
      SKIP_LAUNCHD=1
      shift
      ;;
    --allow-dirty)
      ALLOW_DIRTY=1
      shift
      ;;
    --allow-branch)
      ALLOW_BRANCH=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

command -v "$DEPLOY_PYTHON_BIN" >/dev/null 2>&1 || die "Python not found: $DEPLOY_PYTHON_BIN"
command -v git >/dev/null 2>&1 || die "git is required"

resolve_existing_dir() {
  local path="$1"
  [[ -d "$path" ]] || die "directory does not exist: $path"
  (cd "$path" && pwd -P)
}

TASKS_ROOT="$(resolve_existing_dir "$TASKS_ROOT")"
FOREIGN_TASK_DIR="${FOREIGN_TASK_DIR:-$TASKS_ROOT/ZSXQ_autodownload}"
DOMESTIC_TASK_DIR="${DOMESTIC_TASK_DIR:-$TASKS_ROOT/ZSXQ_国内研报_中金公司}"
FOREIGN_TASK_DIR="$(resolve_existing_dir "$FOREIGN_TASK_DIR")"
DOMESTIC_TASK_DIR="$(resolve_existing_dir "$DOMESTIC_TASK_DIR")"

for task_dir in "$FOREIGN_TASK_DIR" "$DOMESTIC_TASK_DIR"; do
  case "$task_dir/" in
    "$TASKS_ROOT/"*) ;;
    *) die "task directory must be below --tasks-root: $task_dir" ;;
  esac
  [[ -f "$task_dir/config.env" ]] || die "private task configuration is missing: $task_dir/config.env"
done

[[ "$FOREIGN_LABEL" =~ ^[A-Za-z0-9._-]+$ ]] || die "invalid foreign LaunchAgent label"
[[ "$DOMESTIC_LABEL" =~ ^[A-Za-z0-9._-]+$ ]] || die "invalid domestic LaunchAgent label"
[[ "$FOREIGN_LABEL" != "$DOMESTIC_LABEL" ]] || die "LaunchAgent labels must be different"

git -C "$RELEASE_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1 \
  || die "installer must run from a Git checkout"
DEPLOYED_SHA="$(git -C "$RELEASE_ROOT" rev-parse HEAD)"
DEPLOYED_REF="$(git -C "$RELEASE_ROOT" describe --tags --always --dirty 2>/dev/null || printf '%s' "$DEPLOYED_SHA")"

if [[ "$APPLY" -eq 1 ]]; then
  if [[ "$ALLOW_DIRTY" -ne 1 ]] && [[ -n "$(git -C "$RELEASE_ROOT" status --porcelain --untracked-files=all)" ]]; then
    die "refusing dirty checkout; commit/stash changes or use --allow-dirty for an emergency"
  fi
  if [[ "$ALLOW_BRANCH" -ne 1 ]] && git -C "$RELEASE_ROOT" symbolic-ref -q --short HEAD >/dev/null; then
    die "refusing branch checkout; deploy a detached verified tag or SHA (or use --allow-branch for an emergency)"
  fi
fi

FOREIGN_RUNNER_SOURCE="$RELEASE_ROOT/openclaw_tasks/zsxq_download/run.sh"
FOREIGN_CRON_SOURCE="$RELEASE_ROOT/openclaw_tasks/zsxq_download/run.cron-safe.sh"
FOREIGN_LAUNCHER_SOURCE="$RELEASE_ROOT/scripts/run_zsxq_task_via_codex.sh"
DOMESTIC_RUNNER_SOURCE="$FOREIGN_RUNNER_SOURCE"
DOMESTIC_CRON_SOURCE="$FOREIGN_CRON_SOURCE"
DOMESTIC_LAUNCHER_SOURCE="$RELEASE_ROOT/scripts/run_zsxq_domestic_cicc_task_via_codex.sh"
FOREIGN_TEMPLATE="$RELEASE_ROOT/deploy/launchd/zsxq-autodownload.plist.template"
DOMESTIC_TEMPLATE="$RELEASE_ROOT/deploy/launchd/zsxq-domestic-cicc.plist.template"
for source_path in \
  "$FOREIGN_RUNNER_SOURCE" \
  "$FOREIGN_CRON_SOURCE" \
  "$FOREIGN_LAUNCHER_SOURCE" \
  "$DOMESTIC_LAUNCHER_SOURCE" \
  "$FOREIGN_TEMPLATE" \
  "$DOMESTIC_TEMPLATE"; do
  [[ -f "$source_path" ]] || die "required release file is missing: $source_path"
done

task_is_running() {
  local task_dir pid_file pid command_line
  task_dir="$1"
  pid_file="$task_dir/.run.pid"
  if [[ -f "$pid_file" ]]; then
    pid="$(tr -d '[:space:]' < "$pid_file" 2>/dev/null || true)"
    if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
      command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
      if [[ "$command_line" == *"$task_dir"* ]]; then
        return 0
      fi
    fi
  fi
  while IFS= read -r command_line; do
    if [[ "$command_line" == *"$task_dir/"* ]] && \
      [[ "$command_line" == *"run.cron-safe.sh"* || "$command_line" == *"run.sh"* ]]; then
      return 0
    fi
  done < <(ps -axo command= 2>/dev/null)
  return 1
}

RUNNING_TASKS=()
for task_dir in "$FOREIGN_TASK_DIR" "$DOMESTIC_TASK_DIR"; do
  if task_is_running "$task_dir"; then
    RUNNING_TASKS+=("$task_dir")
  fi
done
if [[ ${#RUNNING_TASKS[@]} -gt 0 && "$APPLY" -eq 1 ]]; then
  die "refusing to replace wrappers while a task is running: ${RUNNING_TASKS[*]}"
fi

render_plist() {
  local template="$1" output_path="$2" label="$3" task_dir="$4"
  "$DEPLOY_PYTHON_BIN" - "$template" "$output_path" "$label" "$task_dir" "$HOME" <<'PY'
from pathlib import Path
from xml.sax.saxutils import escape
import sys

template_path, output_path, label, task_dir, home_dir = sys.argv[1:]
content = Path(template_path).read_text(encoding="utf-8")
replacements = {
    "__LABEL__": escape(label),
    "__TASK_DIR__": escape(task_dir),
    "__HOME__": escape(home_dir),
}
for token, value in replacements.items():
    content = content.replace(token, value)
if "__" in content:
    raise SystemExit(f"unrendered template token in {template_path}")
Path(output_path).write_text(content, encoding="utf-8")
PY
  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$output_path" >/dev/null
  fi
}

write_deployment_env() {
  local task_dir pipeline launcher_source canonical_name output_path
  task_dir="$1"
  pipeline="$2"
  launcher_source="$3"
  canonical_name="$4"
  output_path="$task_dir/deployment.env"
  "$DEPLOY_PYTHON_BIN" - "$output_path" "$RELEASE_ROOT" "$pipeline" "$launcher_source" "$canonical_name" <<'PY'
from pathlib import Path
import os
import shlex
import sys

output_path, release_root, pipeline, launcher_source, canonical_name = sys.argv[1:]
output = Path(output_path)
body = "\n".join(
    [
        "# Generated by deploy/install_local_runtime.sh; safe to regenerate.",
        "# Keep secrets and chat identifiers in config.env, not here.",
        f"AUTOMATION_ROOT={shlex.quote(release_root)}",
        f"CODEX_SCRIPT_PATH={shlex.quote(launcher_source)}",
        f"NOTIFICATION_PIPELINE={shlex.quote(pipeline)}",
        "RESULT_HELPER_PATH=\"${AUTOMATION_ROOT}/scripts/zsxq_autodownload_result.py\"",
        "NOTIFICATION_POLICY_PATH=\"${AUTOMATION_ROOT}/scripts/zsxq_notification_policy.py\"",
        "CODEX_STRUCTURED_REPORT_PATH=\"${INVESTMENT_REPORTS_RUNTIME_DIR:-${AUTOMATION_ROOT}/.runtime}/logs/" + canonical_name + "\"",
        "",
    ]
)
temporary = output.with_name(f".{output.name}.tmp-{os.getpid()}")
temporary.write_text(body, encoding="utf-8")
os.replace(temporary, output)
PY
}

install_link() {
  local source_path="$1" destination_path="$2" backup_dir="$3"
  if [[ -L "$destination_path" ]] && [[ "$(readlink "$destination_path")" == "$source_path" ]]; then
    printf 'already linked: %s\n' "$destination_path"
    return 0
  fi
  if [[ -d "$destination_path" && ! -L "$destination_path" ]]; then
    die "refusing to replace directory: $destination_path"
  fi
  if [[ -e "$destination_path" || -L "$destination_path" ]]; then
    mkdir -p "$backup_dir"
    mv "$destination_path" "$backup_dir/$(basename "$destination_path")"
    printf 'backup: %s\n' "$backup_dir/$(basename "$destination_path")"
  fi
  ln -s "$source_path" "$destination_path"
  printf 'linked: %s -> %s\n' "$destination_path" "$source_path"
}

write_deployment_record() {
  local record_dir record_path
  record_dir="$TASKS_ROOT/.deployment"
  record_path="$record_dir/investment-reports-automation.json"
  mkdir -p "$record_dir"
  "$DEPLOY_PYTHON_BIN" - "$record_path" "$RELEASE_ROOT" "$DEPLOYED_SHA" "$DEPLOYED_REF" \
    "$FOREIGN_TASK_DIR" "$FOREIGN_LABEL" "$DOMESTIC_TASK_DIR" "$DOMESTIC_LABEL" <<'PY'
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

(record_path, release_root, sha, ref, foreign_dir, foreign_label,
 domestic_dir, domestic_label) = sys.argv[1:]
payload = {
    "installed_at": datetime.now(timezone.utc).isoformat(),
    "release_root": release_root,
    "git_sha": sha,
    "git_ref": ref,
    "tasks": {
        "foreign_download": {"task_dir": foreign_dir, "label": foreign_label},
        "domestic_cicc": {"task_dir": domestic_dir, "label": domestic_label},
    },
}
target = Path(record_path)
temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.replace(temporary, target)
PY
}

printf 'release: %s (%s)\n' "$DEPLOYED_REF" "$DEPLOYED_SHA"
printf 'foreign task: %s [%s]\n' "$FOREIGN_TASK_DIR" "$FOREIGN_LABEL"
printf 'domestic task: %s [%s]\n' "$DOMESTIC_TASK_DIR" "$DOMESTIC_LABEL"
if [[ ${#RUNNING_TASKS[@]} -gt 0 ]]; then
  printf 'running task detected; --apply would refuse: %s\n' "${RUNNING_TASKS[*]}"
fi

if [[ "$APPLY" -ne 1 ]]; then
  printf 'dry run complete; re-run with --apply after both tasks are idle.\n'
  exit 0
fi

if [[ "$SKIP_LAUNCHD" -ne 1 ]]; then
  command -v launchctl >/dev/null 2>&1 || die "launchctl is required unless --skip-launchd is used"
  mkdir -p "$LAUNCH_AGENTS_DIR"
  FOREIGN_PLIST="$LAUNCH_AGENTS_DIR/$FOREIGN_LABEL.plist"
  DOMESTIC_PLIST="$LAUNCH_AGENTS_DIR/$DOMESTIC_LABEL.plist"
  FOREIGN_PLIST_TMP="$FOREIGN_PLIST.tmp.$$"
  DOMESTIC_PLIST_TMP="$DOMESTIC_PLIST.tmp.$$"
  trap 'rm -f "${FOREIGN_PLIST_TMP:-}" "${DOMESTIC_PLIST_TMP:-}"' EXIT
  render_plist "$FOREIGN_TEMPLATE" "$FOREIGN_PLIST_TMP" "$FOREIGN_LABEL" "$FOREIGN_TASK_DIR"
  render_plist "$DOMESTIC_TEMPLATE" "$DOMESTIC_PLIST_TMP" "$DOMESTIC_LABEL" "$DOMESTIC_TASK_DIR"
fi

BACKUP_STAMP="$(date '+%Y%m%dT%H%M%S%z')"
for task_dir in "$FOREIGN_TASK_DIR" "$DOMESTIC_TASK_DIR"; do
  backup_dir="$task_dir/.deployment-backups/$BACKUP_STAMP"
  install_link "$FOREIGN_RUNNER_SOURCE" "$task_dir/run.sh" "$backup_dir"
  install_link "$FOREIGN_CRON_SOURCE" "$task_dir/run.cron-safe.sh" "$backup_dir"
done
write_deployment_env "$FOREIGN_TASK_DIR" "foreign_download" "$FOREIGN_LAUNCHER_SOURCE" "zsxq_last_run_structured.json"
write_deployment_env "$DOMESTIC_TASK_DIR" "domestic_cicc" "$DOMESTIC_LAUNCHER_SOURCE" "zsxq_domestic_cicc_last_run_structured.json"
write_deployment_record

if [[ "$SKIP_LAUNCHD" -ne 1 ]]; then
  mv "$FOREIGN_PLIST_TMP" "$FOREIGN_PLIST"
  mv "$DOMESTIC_PLIST_TMP" "$DOMESTIC_PLIST"
  USER_DOMAIN="gui/$(id -u)"
  launchctl bootout "$USER_DOMAIN/$FOREIGN_LABEL" >/dev/null 2>&1 || true
  launchctl bootout "$USER_DOMAIN/$DOMESTIC_LABEL" >/dev/null 2>&1 || true
  launchctl bootstrap "$USER_DOMAIN" "$FOREIGN_PLIST"
  launchctl bootstrap "$USER_DOMAIN" "$DOMESTIC_PLIST"
  printf 'reloaded LaunchAgents: %s, %s\n' "$FOREIGN_LABEL" "$DOMESTIC_LABEL"
else
  printf 'LaunchAgent reload skipped.\n'
fi

printf 'installation complete. Verify with launchctl print and the task run_status.json files.\n'
