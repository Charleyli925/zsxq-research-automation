# Architecture

## 1. Download planning

`zsxq-pipeline download` opens one authenticated Chrome for Testing CDP
session, freezes a time window, and writes a versioned immutable allow-list
under the runtime root. `scan_zsxq_download_candidates.py` remains a
diagnostic-compatible scanner; keyword matching, duplicates, and source-state
failures are still decided before download execution.

## 2. Plan-bound download

`DownloadPipeline` uses that one browser session to visit each exact planned
topic sequentially. `download_zsxq_plan_file.py` retains a one-file diagnostic
CLI, while its page-level function waits for asynchronous state, fails closed
on source content protection, validates PDF bytes, and atomically places the
file in staging. No model, agent, browser MCP, or dynamic package installation
is involved.

## 3. Reconciliation

`finalize_download_batch.py` archives only planned files and appends evidence
to the run manifest. `DownloadPipeline` maps that reconciliation into SQLite
document/stage/artifact records and advances the durable checkpoint only after
every planned candidate is downloaded, already satisfied, or deterministically
blocked. Legacy JSON state is a finalizer input mirror and compatibility view,
not checkpoint truth.

## 4. Digest processing

The legacy digest cron wrapper remains the scheduler entry point, but it is a
thin compatibility shell around `zsxq-pipeline process`. The implementation
is in `src/zsxq_pipeline` and does not invoke the OpenClaw binary or consume
the former model registry, session, credential artifact, or model binding.

For each eligible PDF the pipeline:

1. extracts and quality-gates local text with deterministic fallbacks;
2. caches that text by `pdf_sha256 + extractor_version`;
3. calls `codex exec` only with the extracted text, fixed prompt, and strict
   JSON schema;
4. atomically writes JSON and Markdown summary artifacts before committing
   summary state; and
5. quarantines content failures without fabricating a summary.

The model process is ephemeral, ignores user configuration and rules, runs in
a read-only sandbox, and receives no PDF, browser, publishing tool, or
repository write access. Its summary identity includes the PDF hash,
extractor version, prompt version/hash, configured model, and reasoning.
There is no provider fallback at runtime.

## 5. Publication and notification

Local Markdown summary artifacts are the publication input truth. Up to two
summary jobs may complete in parallel, but publication groups and Lark writes
are serial and deterministically ordered by source/date/file identity.

`LarkPublisher` uses `lark-cli` as `user` to create or append a document, set
its title, fetch it to verify the expected body, and grant the target chat
view access. The state transition is `intent -> remote_written -> success`:
the remote-write record is committed before title, fetch, or permission
verification, so a restart verifies and grants the existing document instead
of creating or appending it twice.

`LarkNotifier` uses `lark-cli` as `bot` with a stable idempotency key. The
notification outbox is independent from publication state: a failed message
is retried later and never rolls back a successful document. Document links
are delivered before a terminal batch summary.

## 6. Durable state

`pipeline.sqlite3` records source windows, source documents, immutable
artifact identities, short stage leases/attempts, publication transitions, and
an idempotent notification outbox. It deliberately does not store report text
or summary bodies; those remain readable files in the research library.

The key durable contracts are:

- `(source, source_file_id)` keeps source-document identity distinct from
  `pdf_sha256`, so one PDF may legitimately appear in more than one source.
- A stage is unique by `(document, stage, workflow_version)` and is claimed
  through a short `BEGIN IMMEDIATE` lease transaction.
- A publication may recover only from its recorded `remote_written` target;
  it must fetch and verify rather than write again.
- Retry eligibility follows an error category, not free-form error text. Auth,
  release-contract, invariant, and content failures are terminal until an
  explicit future workflow resolves them.

## Boundaries

| Layer | Git-tracked | Local only |
| --- | --- | --- |
| Code | scripts, task entrypoints, pipeline adapters, tests | installed executables |
| Configuration | examples and schemas | real IDs, paths, watchlists |
| Authentication | none | Codex account state, Keychain, lark-cli profile, browser profile |
| Content | synthetic fixtures only | PDFs, extracted text, summaries |
| Runtime | contracts only | logs, state, manifests, caches |
