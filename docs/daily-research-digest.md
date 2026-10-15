# Daily macro and industry research digest

The existing `tick` worker can run an opt-in cross-report editorial job at
21:00 in the configured IANA timezone, including weekends. It uses the existing
runtime lock and LaunchAgent; it does not install another scheduler. It runs
before download/summary work so a continuous backlog cannot starve the digest.
Each successful checkpoint is durable. The soft tick budget is checked between
checkpoints; an in-flight model request has its own configured timeout.

## Inputs and editorial contract

The ResearchLibrary SQLite database is opened with `mode=ro` and
`PRAGMA query_only=ON`. Ingestion timestamps, not report cover dates, define the
window. Initial execution starts at local midnight; subsequent executions scan
the retained window and skip completed PDF hashes. This intentionally keeps
unread, failed and late-indexed inputs eligible. The reader has no arbitrary
candidate/page/character cap. Identical hashes are considered one report.

The model reads staged summaries and original PDFs. Missing/changed/unreadable
inputs remain pending. Each input must be included, excluded with a reason, or
pending; unread reports cannot be silently classified as excluded. All selected
claims cite original PDF pages. Cover dates are verified separately from archive
dates. Macro, policy, rates, supply/demand, price, inventories and cross-company
evidence are in scope; standalone company earnings/target prices are not.

Approved presentation is deliberately short: viewpoint, key data, original
chart and one-line institution/report/date/page attribution. There are no
"why worth reading", follow-up questions, reading-log or long-source appendices.
Necessary units, periods and forecast labels stay adjacent to the data. Full
paths, input dispositions and verification details remain in private run files.
Optional `rules_path` points to an operator-approved JSON file, snapshotted for
each run. Remote comments do not automatically become executable instructions.

## Model and evidence boundary

Both the example per-report summary config and the new digest config select
`gpt-6.1-sol` with `high` reasoning. The digest invokes `codex exec` with explicit
`--model` and `model_reasoning_effort` flags and does not silently fall back.
The existing per-report summary adapter keeps its separate configured model.
Model names in a config are not proof of access: run a live request using the
exact executable, credentials and ignored-user-config mode before cutover.
Some ChatGPT sign-in environments may reject an otherwise documented model.

Research uses a scratch workspace with network access disabled. Trusted state,
input PDFs and publication files are outside that writable workspace. Returned
JSON cannot specify executable commands, file paths, destinations or XML.
The host renders complete source pages and chart crops using Poppler. A separate
read-only model request receives those actual images via `--image` and must
accept the claims, dates, units, forecast labels and crop completeness before
publication. A rejection returns to research with feedback. Verified chart
hashes are checked again immediately before upload.

This is a model-based evidence check, not a mathematical guarantee that a
report or its figures are correct. Retained model events and exact source hashes
make the decision inspectable.

## Publication, retries and recovery

Publication always uses `lark-cli --as user`, XML and original PNG uploads into
personal cloud space. It sends no IM and changes no permissions. Operations are
serial: create, item text, item images, source line. The final fetch checks the
complete text/image sequence against write receipts. Processed hashes advance
only after successful read-back, or after a completed no-selection run.

An intent is fsynced before **every** remote mutation. If a write may have
succeeded but its receipt is lost, the next tick reports `needs_reconciliation`.
It does not automatically create another document or append another image.
Read-only/model failures preserve the checkpoint and use a 15-minute retry delay.
Late inputs and pending reports are picked up on the next daily snapshot; a
completed same-day document is not automatically rewritten under user comments.

```sh
zsxq-pipeline daily-digest status --config /absolute/path/pipeline.toml
zsxq-pipeline run-stage --stage daily-digest --config /absolute/path/pipeline.toml
```

After inspecting an ambiguous remote write, supply its recovered JSON receipt
(creation needs `document_id`, image insertion needs `file_token`). Applied
receipts require a successful read-back of the full published prefix:

```sh
zsxq-pipeline daily-digest reconcile --config /absolute/path/pipeline.toml \
  --day 2026-10-17 --operation-index 0 --receipt /absolute/path/receipt.json \
  --reason 'Verified the existing document title and content'
# Repeat with --apply only after reviewing this exact operation.
```

If the operator has verified that the mutation did not occur, use
`--confirmed-not-applied` instead of `--receipt`, with an inspection reason and
`--apply`. This only clears that exact intent so the next tick can retry it.
The date and index must still match the active job. Reconciliation takes the
same runtime lock and never overwrites a document.

## Deployment and migration

1. Review and merge the PR, then perform the repository's explicit idle-runtime
   release procedure. The feature defaults to `enabled = false`.
2. Verify Poppler (`pdftoppm`, `pdftotext`), user Lark access and an actual
   `gpt-6.1-sol/high` Codex call in the production launch environment.
3. Under the runtime lock, update both `[model].name` and `[codex].model` to
   `gpt-6.1-sol`, keeping `[codex].reasoning = "high"`. Never deploy an unsupported
   model into a functioning summary queue solely because a config parses.
4. Copy the approved editorial JSON and prior chat task's `state.json` into
   private runtime paths. Configure `rules_path` and `seed_state`. The seed
   imports completed hashes and the last published date, preventing a duplicate
   first edition. Seeds with pending reports require an explicit ingestion
   boundary including those reports before import.
5. Disable the earlier chat automation and enable `[daily_digest]` together as
   the controlled cutover. Do not run both publishers concurrently.
6. Verify one complete report with actual image uploads and a final read-back.
   Record its URL and run directory. Never mark the live cutover validated from
   mocked tests or configuration changes alone.

Private files are under `runtime.root/daily-digest/YYYY-MM-DD/`: immutable input
metadata/PDF copies, approved rules and prompt hashes, explicit model/effort,
structured draft, full-page and crop images, visual verification, write intents
and receipts, and final fetched XML. Model call logs live under `model/`.
No research material, credentials or account-specific identifiers belong in Git.
