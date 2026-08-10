# Source of truth

## Code

The GitHub repository is authoritative for code, tests, prompts, and example
configuration. `main` contains accepted changes. Tags identify deployable
versions.

Local edits are not authoritative until they are committed, pushed, checked,
and merged.

Every repository modification starts from a clean, up-to-date `main` checkout
on a `codex/*` feature branch. It is verified locally with the smallest
relevant test set, staged by exact file list, committed, pushed, and opened as
a Draft PR. GitHub CI runs the complete suite; human review and merge remain
mandatory. See [development-workflow.md](development-workflow.md).

## Deployment

A deployment records the exact tag or commit SHA it runs. Machine-specific
configuration refers to that code but does not copy or fork it. The foreign
download task, domestic-CICC task, and `ZSXQ_pdf_digest` are one deployment
unit. For the digest, its cron wrappers, direct Python pipeline, packaged
prompts/extractor, and local sidecar adapters must resolve to the same release
checkout.

Development and production checkouts are separate so a scheduler cannot
execute an uncommitted edit.

For the digest, ownership is deliberately split:

- `config.env` owns business configuration: chat ID, local data roots,
  identity selection, model/runtime tuning, and scheduler-facing options. It
  cannot select an alternative digest source checkout.
- installer-generated `deployment.env` remains release metadata for the local
  task installation. It cannot contain chat IDs, credentials, browser profile
  paths, logs, state, or downloaded content.
- `.deployment/investment-reports-automation.json` records the deployed Git
  SHA, release root, task directories, and each task's scheduler type.

The digest wrapper resolves its own linked source path and puts only that
release's `src/` tree on `PYTHONPATH`; it does not snapshot a shell worker or
accept a source-root override from `config.env`. A release mismatch is a
deployment fault, not a transient retry.

## Runtime state

Logs, run status, manifests, retry ledgers, quarantine entries, and publish
records describe execution state. They must not be committed or mistaken for
source truth.

Recovery audits may be appended to a retry ledger, but prior failure entries
and `remote_written` publish records remain historical evidence. Never clear a
ledger or delete a publish record merely to make work eligible again.

For the direct digest pipeline, `pipeline.sqlite3` is the durable state
authority. It separates source-document identity, PDF content identity, stage
state, publication state, and notification idempotency in one transaction
boundary. Existing JSON/JSONL files and `processed_files.sqlite` remain
compatibility/material views; `run_status.json` and `last_result.*` are
operator-facing exports, not a second retry or completion authority.

`zsxq-pipeline legacy plan` reads legacy files and records a SHA256 snapshot of
each source. `legacy apply --apply` rechecks those hashes before writing only a
new pipeline database. It never changes legacy state, caches, research files,
or the existing knowledge-base index.

## Research content

Downloaded PDFs, extracted text, summaries, and Obsidian notes remain in the
local research library. Git stores neither licensed report content nor
personal research data.
