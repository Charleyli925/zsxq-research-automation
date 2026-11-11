# Runtime recovery contract

`launchd` merely wakes `zsxq-pipeline tick`; SQLite is the recovery authority.
Every wake takes `runtime/.pipeline.lock` with non-blocking `fcntl.flock`.
There is no PID file or stale-owner cleanup: a crash closes the descriptor and
the next tick can safely proceed.

## What survives interruption

- New downloads use an attachment-specific directory and durable receipt under
  `.zsxq-downloads` in the configured staging root. Identity is file ID,
  filename and topic URL. The intent precedes browser I/O; validated bytes are
  atomically installed, checksummed and reused after a crash. Archive intent
  precedes a no-overwrite copy. Both staged and already archived receipts are
  reconciled independently of the new run's download time range. Unknown loose
  staging files remain untouched and cannot be adopted just by matching names.
  Production scans deduplicate completed attachment IDs, not filenames. Two
  different IDs with the same name need separate byte/receipt proof; one file
  can no longer falsely satisfy both planned attachments.
- A later candidate's browser exception does not discard earlier completed
  downloads. The finalizer still accounts for them; remaining candidates keep
  their retry or blocked states.
- Each tick attempts at most one window per source. Sources and windows rotate
  by persisted last-attempt time (including exceptions and restarts). Login
  failures retain the oldest login-blocked window for one deduplicated alert.
  A quota of one therefore still makes progress across sources. Incomplete
  downloads now produce a durable, deduplicated blocked notification too.
- Schema 4 retains membership of every scanned window independently from a
  document's latest source window. On first wake, saved plans restore old
  memberships before checkpoint-based skipping. An overlapping scan cannot
  erase an earlier window's missing attachment.

- A schedule cursor advances only in the same transaction that persists a
  `scheduled` source window. A crash cannot silently skip a configured slot.
- Missed source slots coalesce into one window from the last successful
  checkpoint to now. An existing checkpoint is never shortened by the maximum
  lookback, even after a week offline. The configured lookback only bounds the
  first bootstrap when no successful source checkpoint exists.
- A download checkpoint advances only after immutable-plan reconciliation.
  Downloaded PDFs, stage claims, extracted text, summaries, publications, and
  notifications retain their independent identities and recovery paths.
- The dedicated CFT session bounds same-origin automation pages before attach
  and retries one transient CDP/page-creation failure through a fresh
  Playwright transport. If recovery still fails, the blocked plan keeps the
  sanitized browser exception and cleanup counters, the source window remains
  scheduled, and one reason-specific alert is deduplicated per window. The
  worker never terminates a live browser process automatically.
- Each valid model result is atomically cached as soon as that report's
  worker finishes, before waiting for the rest of its batch. A restart can
  reuse it even if the main thread had not acknowledged its SQLite stage.
- Codex failure does not prevent a later source download; Lark failure does
  not discard local summary artifacts; notification delivery drains from its
  own outbox even when no PDF is due.

## Login and temporary service outages

A new worker resumes `scheduled`, `blocked_auth`, `partial`, and abandoned
`running` download windows under the same runtime lock. It retains the original
time boundaries and skips only the prefix already covered by a successful
checkpoint. A candidate that is still waiting for a retry or a lease is missing
work, so it cannot accidentally make the whole window checkpoint-eligible.

A ZSXQ `need_reauth` scan keeps that source window as `blocked_auth`. If login
expires during a candidate download, the worker stops that source's remaining
candidates, saves the authentication retry time, and preserves already archived
files. Other sources and local processing can continue. The next tick rescans
the same pending window; after the user signs in to the dedicated browser, the
same candidate identities can continue without waiting for the next daily slot.

Explicit Lark login failures, such as HTTP 401, missing user identity, or an
expired access/refresh token, keep the publication stage or original outbox
message in `retry_wait`. The user still completes interactive authorization;
the worker never logs in on their behalf. On the next due attempt, publication
reuses the saved summary, and notification reuses its original idempotency key.
Bot readiness and user readiness are different: document publication uses the
user identity, while group notifications use the bot identity.

Temporary network failures, model HTTP 429, and repairable login failures use
persistent cooldowns of 5, 10, 20, 40, then at most 60 minutes. Reopening SQLite
or reclaiming an interrupted claim does not erase that deadline. Lifetime
attempt counts remain audit information; a temporary outage is not changed into
a permanent release failure merely because the machine was offline or an old
configuration failure used several attempts. Queue quotas still bound the
work performed on each wake-up.

After repairing a login, automatic continuation may wait for the saved cooldown
and the normal queue; it is not guaranteed to start at the instant login is
completed. Missing scopes, HTTP 403/resource permissions, model/configuration
errors, schema violations, and content quarantine keep their explicit blocked
states. Repair and inspect those causes before using a fingerprint-bound retry
plan. Older terminal records created by a previous release also require one
reviewed recovery plan; installing new code does not erase historical failures.

## Health and recovery commands

```bash
zsxq-pipeline status --config /absolute/path/to/pipeline.toml --json
zsxq-pipeline doctor --config /absolute/path/to/pipeline.toml
zsxq-pipeline tick --config /absolute/path/to/pipeline.toml
zsxq-pipeline outbox drain --config /absolute/path/to/pipeline.toml
```

`busy` means another process currently owns the lock; it does not mean the
previous work failed. Inspect durable stage counters and source windows rather
than an old PID, log timestamp, directory mtime, or last-result projection.

For a browser-blocked window, inspect its newest immutable plan before taking
manual action. `blocked_detail` distinguishes endpoint failure, connection
failure, and page-target exhaustion; do not clear the browser profile or
delete login/session data to force recovery.

On macOS, an operator-accessible dedicated browser should use
`cft_headless = false` and `cft_background = true`. A scheduler cold start then
uses Launch Services without activating CFT, but creates a real window that can
be selected from the Dock for login or inspection. Changing an already-running
headless process requires an explicit idle-gated browser restart; the worker
does not terminate a live profile merely because configuration changed.

Local CDP health must be checked through the package's proxy-free HTTP client
or an explicitly direct probe. Ambient macOS HTTP proxy settings are not valid
evidence for `127.0.0.1:9223`; a proxied timeout must never trigger a browser
restart.

`intent -> remote_written -> success` remains the publication recovery path.
Before creating or appending a document body, the worker persists a write-in-
flight marker on the intent. If power is lost after submission but before its
receipt is saved, a new worker cannot assume the write failed: it marks the
publication as needing reconciliation and does not send the body again. Saved
member identities also prevent a changed batch from placing an unresolved or
already-written report into a new group and bypassing its original receipt.
Timeouts and document-write transport errors with unknown remote outcomes
retain this guard. Explicit authentication refusals and a failed OAuth token
exchange can be retried after repair. Once a remote reference is recorded as
`remote_written`, recovery only verifies content/title/permissions and cannot
repeat create or append.

Inspect the remote document for an uncertain write before resuming it. The
operator must preserve or attach its actual receipt if it exists, or confirm
that no write was applied before clearing the intent marker. Do not delete a
publication row, text/summary cache, notification outbox, or prior manifest to
force a retry. Other terminal failures use a narrow fingerprint-bound
`retry plan`, reviewed count, and `retry apply --apply`.

## Sleep, wake, and rollback

A powered-off machine cannot run at the intended time. The user LaunchAgent
runs once after the user logs into macOS, then checks about every five minutes
while the machine is available. The next tick uses the durable checkpoint to
recover the entire missed business range. The bootstrap lookback applies only
when there is no previous checkpoint. A release rollback
only re-points `current` after an idle check. It preserves SQLite, local
artifacts, and any successful remote side effects, so old code must continue
to honor the same durable identities. Do not roll back unresolved write-in-
flight intents to a release that ignores their reconciliation markers.

Schema 4 is forward-only. After upgrading, selecting a schema-3 release is
not a valid rollback: it rejects the newer database. Use a schema-4-compatible
corrective release; never restore an old business database to hide failures.

## Local projection repairs

The accepted summary title travels through the real publication entry and
the Obsidian adapter. It must not fall back to the import filename. New
summary/note paths include the PDF hash so different PDFs with the same name
cannot overwrite one another. Existing verified note links are preserved.

For historical `source_changed` rows caused by the old title handoff, use
`scripts/repair_summary_projections.py plan --config ... --plan ...`. The
read-only plan requires unchanged accepted summary bytes, a matching PDF hash
and a verified committed summary cache. Review its exact count/exclusions,
then use `apply --config ... --plan ... --expected-count N --apply` from the
approved release. Apply takes the pipeline lock, revalidates the entire plan,
backs up the catalog and affected notes, and reprojects the original cached
summaries. It never calls a model, changes a remote document or sends a
notification. The per-report completion journal allows inspection after an
interruption; generate a fresh plan for the remaining failures before retrying.

Old `staging_collision` terminal stages still require a fingerprint-bound
download retry plan. Requeue only inspected rows after the receipt-enabled
release is active. Leave old loose staging files intact; new downloads use
separate attachment directories. Uncertain remote publication intents and
unrelated historical terminal failures are not part of this recovery.
