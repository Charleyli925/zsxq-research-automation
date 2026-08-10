# Private deployment

## Prerequisites

- Python 3.11 or newer
- Chrome for Testing with a dedicated authorized profile
- Codex and OpenClaw, when those adapters are enabled
- lark-cli with a configured identity, when Feishu publishing is enabled
- optional PDF tools: `pdftotext`, `pdfinfo`, `tesseract`, `ocrmypdf`

## Configuration

1. Copy examples into `config/local/`.
2. Copy `config.env.example` to `config.env`.
3. Store secrets in Keychain or the owning CLI, not in either file.
4. Set machine paths through environment variables.
5. Run preflight without notifications before enabling a scheduler.

Useful variables:

- `INVESTMENT_REPORTS_DATA_HOME`
- `INVESTMENT_REPORTS_RUNTIME_DIR`
- `RESEARCH_LIBRARY_ROOT`
- `OBSIDIAN_VAULT_ROOT`
- `OPENCLAW_TASKS_ROOT`
- `CODEX_BIN`
- `CFT_START_URL`
- `ZSXQ_TAG_URL`

## Release checkout

Production should use a dedicated checkout:

```bash
git fetch --tags origin
git checkout --detach <verified-tag-or-sha>
```

Keep the development clone elsewhere. Record the deployed SHA before
reloading a scheduler.

### Install the versioned local runtime

The two download tasks and the PDF digest are one release deployment unit. Run
the installer only from the clean, detached release checkout, after all three
tasks are idle:

```bash
bash deploy/install_local_runtime.sh --dry-run
bash deploy/install_local_runtime.sh --apply \
  --foreign-label com.example.zsxq-autodownload \
  --domestic-label com.example.zsxq-domestic-cicc
```

For an existing installation, pass the labels already used by its two
LaunchAgents. Use `--digest-task-dir` when the cron task is outside the
default `ZSXQ_pdf_digest` location. The installer refuses a dirty or branch
checkout by default, does not touch `config.env`, and refuses the entire
deployment if any one of the three task directories is active.

It writes a Git-ignored `deployment.env` beside each task. For the digest,
that file contains only release-owned source paths (`AUTOMATION_ROOT`, helper,
scanner, sidecars, and runtime guard); it must never contain a chat ID,
credential, browser profile, or runtime state. The digest continues to use its
existing cron schedule; the installer only links its `run.sh` and
`run.cron-safe.sh` to the same release checkout as the download tasks. It
backs up replaced local wrappers and writes the common SHA plus all three task
directories to `.deployment/investment-reports-automation.json` under the
tasks root. Use `--skip-launchd` only when preparing a machine before the user
LaunchAgent session is available.

After an explicit release decision, use this sequence:

```bash
bash deploy/install_local_runtime.sh --dry-run
# Verify the three task directories and SHA in the output, then ensure all are idle.
bash deploy/install_local_runtime.sh --apply
bash "${OPENCLAW_TASKS_ROOT}/ZSXQ_pdf_digest/run.sh" --preflight-only --no-notify
```

Do not use this installer to deploy an unmerged branch, change a scheduler,
or repair a backlog automatically.

## Preflight

```bash
bash openclaw_tasks/zsxq_pdf_digest/run.sh --preflight-only --no-notify
```

Do not enable a schedule until configuration paths, worker registration,
browser login, PDF tooling, and publishing identity all pass.

## Resilient scheduling on macOS

The download jobs are finite jobs, not daemons. Install them as user
LaunchAgents with both their normal `StartCalendarInterval` and
`RunAtLoad=true`. `RunAtLoad` gives each job one catch-up attempt after the
user logs in following a restart or power loss. Do not set unconditional
`KeepAlive=true`: a persistent source-side or login failure would otherwise
create a hot restart loop.

The PDF digest is checked every ten minutes. Its runtime lock prevents an
overlapping check from starting a second worker. After a restart, the next
check re-discovers every PDF that has not been acknowledged in the research
library index, so an interrupted batch is resumed rather than discarded.

Schedulers only trigger work; they are not the source of completion truth.
Use the run ID, checkpoint, immutable scan plan, run manifest, and final
canonical result together. A prior run's result must never be reused as the
result of a new trigger.

See [runtime-recovery.md](runtime-recovery.md) for the recovery contract and
the operational checks.
