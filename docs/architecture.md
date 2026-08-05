# Architecture

## 1. Download planning

`scan_zsxq_download_candidates.py` freezes a time window and produces an
immutable allow-list. Keyword matching, duplicates, and source-state failures
are decided before download execution.

## 2. Plan-bound download

`download_zsxq_plan_file.py` accepts one file ID from the plan, navigates to
the exact topic, waits for asynchronous page state, validates the PDF bytes,
and atomically places the file in staging.

The helper never attempts to bypass a source-side restriction. A stable
restriction is returned as a structured blocked reason.

## 3. Reconciliation

`finalize_download_batch.py` archives only planned files and appends evidence
to the run manifest. The launcher advances the checkpoint only after all
planned candidates are downloaded, already satisfied, or deterministically
blocked.

## 4. Report processing

The digest task scans new PDFs, extracts text with deterministic fallbacks,
applies quality gates, writes summary artifacts, and quarantines unusable
files without fabricating summaries.

## 5. Publishing and indexing

Publishing uses lark-cli through an explicitly configured identity. Report
metadata and generated notes are indexed in a local SQLite/Obsidian knowledge
base. Publishing records and indexes are runtime data rather than source code.

## Boundaries

| Layer | Git-tracked | Local only |
|---|---|---|
| Code | scripts, task entrypoints, tests | installed executables |
| Configuration | examples and schemas | real IDs, paths, watchlists |
| Authentication | none | Keychain, browser profile, CLI auth |
| Content | synthetic fixtures only | PDFs, text, summaries |
| Runtime | contracts only | logs, state, manifests, caches |
