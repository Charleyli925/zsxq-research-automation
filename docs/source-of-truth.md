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
unit: their wrappers and the digest's worker/helper/scanner/prompt/sidecars
must resolve to the same release checkout.

Development and production checkouts are separate so a scheduler cannot
execute an uncommitted edit.

For the digest, ownership is deliberately split:

- `config.env` owns business configuration: chat ID, local data roots,
  identity selection, model/runtime tuning, and scheduler-facing options.
- installer-generated `deployment.env` owns only release source paths. It
  cannot contain chat IDs, credentials, browser profile paths, logs, state,
  or downloaded content.
- `.deployment/investment-reports-automation.json` records the deployed Git
  SHA, release root, task directories, and each task's scheduler type.

The runtime verifies this boundary before taking an immutable snapshot. A
cross-release source path or helper contract mismatch is a blocked release,
not a transient retry.

## Runtime state

Logs, run status, manifests, retry ledgers, quarantine entries, and publish
records describe execution state. They must not be committed or mistaken for
source truth.

Recovery audits may be appended to a retry ledger, but prior failure entries
and `remote_written` publish records remain historical evidence. Never clear a
ledger or delete a publish record merely to make work eligible again.

The new `pipeline.sqlite3` is the durable state authority only for pipeline
workers that explicitly adopt `zsxq_pipeline` in a later change. It separates
source-document identity, PDF content identity, stage state, publication state,
and notification idempotency in one transaction boundary. During the migration
period, existing JSON/JSONL files and `processed_files.sqlite` remain their
current compatibility/material-view roles; no existing runtime may infer that a
new database alone proves a batch has completed.

`zsxq-pipeline legacy plan` reads legacy files and records a SHA256 snapshot of
each source. `legacy apply --apply` rechecks those hashes before writing only a
new pipeline database. It never changes legacy state, caches, research files,
or the existing knowledge-base index.

## Research content

Downloaded PDFs, extracted text, summaries, and Obsidian notes remain in the
local research library. Git stores neither licensed report content nor
personal research data.
