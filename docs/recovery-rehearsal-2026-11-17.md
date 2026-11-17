# Production-copy recovery rehearsal — 2026-11-17

This records a private, offline snapshot rehearsal, not a production release.
Production remained on `e0a889ab4303f5ceceb3fd945293ae7af4f1b64b`. No production
stage was requeued, no remote document was written and no message was sent by
this work. A separate read-only inspection fetched four existing remote
documents to inform the publication recovery gate.

## Database and discovery migration

Read-only SQLite backup produced independent pipeline/catalog copies. Production
continues scheduling, so the counts below belong to the captured snapshot.

| Durable records | Before migration | After schema-5 migration/bootstrap |
|---|---:|---:|
| Documents | 23,141 | 23,141 |
| Stage attempts | 40,266 | 40,266 |
| Publications | 1,321 | 1,321 |
| Notification outbox | 3,127 | 3,127 |

Foreign-key checks passed. All publication rows retained identical fingerprints.
Bootstrap imported 632 valid saved discovery plans; a second invocation made no
additional changes. Two invalid or unmatched legacy plans remained unproved,
so they cannot advance coverage. The contiguous evidence boundary is October 23
for both active sources; later isolated successes do not prove the intervening
gaps. The next scan starts from that conservative boundary and immediately
queues attachments from each newly committed page.

The queue includes both active sources. Download completion is independent from
scanning and only unsettled attachments are dispatched; successful old downloads
are not scheduled again. An upgrade also records the old-window notification
ceiling to prevent a retrospective completion-message burst.

## Complete historical terminal inventory

Every terminal row has its stage ID, document ID, workflow, state, error code,
lifetime attempts, timestamp and fingerprint in the private inventory. Review
cohorts include exact members and required evidence. Nothing is silently dropped.

| Cohort | Rows | Recovery decision |
|---|---:|---|
| `staging_collision`, exact source attachment evidence present | 289 | Eligible for reviewed retry with the receipt-isolated downloader |
| Historical publication failures | 528 | Reconstruct/verify accepted summary and publication membership first |
| Summary/provider/cache failures | 206 | Validate identity/cache and current provider; retry only the resolved cause |
| Extraction/tool interruption failures | 22 | Verify the PDF and local extractor before an exact retry |
| Protected downloads or quarantined content | 86 | Preserve quarantine; do not bypass protection or blanket retry |
| **Total** | **1,131** | Every row accounted for |

Of the 528 publication rows, 526 belong to the legacy `zsxq_digest` source, which
is not a configured production source. They have no accepted summary-stage
binding in the current pipeline; toggling them to queued would neither establish
the missing identity nor make the normal source selector run them. Recovery must
reconstruct their original report/summary/target evidence, then explicitly map
the source or produce a reviewed import plan. They are not part of the 289-file
safe download cohort. The remaining two have accepted summaries and are held by
an uncertain publication intent.

The four unresolved publication receipts comprise two `remote_written` and two
`intent` rows. All four referenced/target documents were readable during the
read-only check. Three old receipts lack exact member identities, so remote
existence is insufficient to declare them complete. The fourth intent has two
known summaries: their expected lines were absent from the fetched target at
revision 10. Preserve its write guard until a reviewer confirms the exact target,
complete revision and unchanged evidence; then select a receipt-preserving or
confirmed-not-applied reconciliation. Do not automatically append again based
only on the earlier timeout or this snapshot.

## Download recovery rehearsal

All **289** eligible rows were requeued on the copy using their exact unchanged
stage fingerprints. Each used its immutable source attachment identity. The
transport was replaced by a distinct synthetic PDF in its receipt directory,
simulating interruption after payload installation but before receipt completion.

The real downloader recovery, finalizer, archive manifest and SQLite confirmation
ran for all 289 candidates. All succeeded. A second pass for all 289 retained
the same artifact bindings and **289 distinct archived files**. An unowned file
in the staging root was unchanged. Publication receipt fingerprints were unchanged.
These results verify local recovery and idempotency; they do not verify live
browser timing, current permissions or actual source attachment availability.

## Projection recovery rehearsal

The snapshot had **1,625** `source_changed` candidates. For every candidate the
repair verified the accepted Markdown bytes, current PDF hash and matching
committed summary cache. There were **zero excluded candidates**. PDF/Markdown/
note evidence was cloned into the isolated directory before repair. Only cache
storage addresses were relocated; summary identities/content hashes were retained.

The actual repair tool rebuilt all **1,625** local projections and verified their
indexed state. A fresh plan returned **zero remaining candidates**. It invoked
no model, no browser, no Lark writer and no notification sender. Migration,
evidence copying and this full projection replay took about **358 seconds** (the earlier complete replay took 341 seconds).

## Release and historical recovery sequence

1. Review the final Draft PR and green CI. Make a separate release decision.
2. Stop new wake-ups and wait for both scheduler and independent workers to
   settle. Claimed task rows must also settle; an expired lease alone is not
   enough. Save fresh SQLite/config snapshots and current remote receipt evidence.
3. Regenerate the historical inventory and exact retry/projection plans from
   the idle production state. The rehearsal fingerprints are evidence, not
   permission to apply stale plans against a moving database.
4. Install a clean detached reviewed SHA under held maintenance/pipeline gates.
   Verify local capabilities and the schema before activation. An incompatible
   old schema reader is not a rollback option.
5. Verify one live page, one real attachment, a local accepted summary, a
   publication readback, a local projection and a document notification. Then
   admit the reviewed 289-file cohort and verified projection repair cohort.
6. Treat the remaining historical cohorts separately using their exact evidence
   requirements above. Resolve remote uncertainty before any publish retry;
   never rerun the model for a publication-only recovery with an accepted cache.
7. Observe multiple scheduled cycles: source coverage advances, oldest active
   debt and queue depth fall, no repeated remote body appears, no new
   `source_changed` is introduced and completion counts include every member.

Rollback after activation preserves the current business database and remote
receipts. Use a reviewed schema-compatible code release. Restoring the pre-upgrade
database after remote writes could erase receipts and duplicate documents, so
that is not an automatic rollback procedure.

## Reproduction and limits

Commands and guarantees are in [durable-task-runtime.md](durable-task-runtime.md).
Private artifacts include the snapshot report, complete historical inventory,
exact retry plans, projection source/copy plans, repair backups/completion list,
download replay report and remote readback hashes. They remain outside Git.
CI uses synthetic fixtures for process death, independent process groups,
writer locks, stale tokens, pagination, missing-PDF denominators, source fairness,
provider-free publish retries, terminal retry guards and deployment gates.
Live throughput and sustained queue reduction remain post-release acceptance
criteria; this PR does not claim they have already been observed in production.
