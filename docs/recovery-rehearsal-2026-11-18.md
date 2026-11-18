# November 18 overseas report scan and recovery rehearsal

## Production observations

All times below use Asia/Shanghai. The production checkout was `e0a889ab` with schema 3.
At 13:20:14, the notification outbox recorded a sent `download_blocked` message
for an older window. The corresponding topics scan recorded business code `1059`. The
window covered November 5 at 20:49 through November 12 at 12:01. Its stored state
was `blocked`, which the deployed scheduler's retry selector skips.

At 13:25:49 a different overseas window scanned successfully. By 15:35:45,
284 of 285 archived overseas scan plans that day recorded success. All 285
belonged to three overlapping historical windows. Five known download stages
remained in `blocked_release` with `staging_collision`. The latest eligible
overseas delivery checkpoint was still November 5 at 20:49. These records
explain how the scheduler could report repeated activity while making little
progress on new reports.

At 15:42:41, one live read used the existing source session and the failed
window's upper bound. The request returned HTTP 200. All 20 topic IDs matched
the first 20 entries of the archived successful scan for that window, in order.
The historical error record contains neither the failed page number nor the
provider's internal reason for code 1059.

At 15:47:17 the same deployed worker sent a second warning for another
historical overseas window, ending November 12 at 16:04. Its saved diagnostic
was `api_unavailable`. The legacy scanner uses this classification for both an
unavailable tag page and a browser request failure. The run record did not
identify which occurred. Another overseas window succeeded at 15:52:40. A
16:11 readback still found both failed windows blocked and all five collisions
outstanding.

## Replaying the same window

Two isolated copies of the same SQLite backup used the affected window's 125
archived topics and its production selection rules. The test transport returned
business code 1059 on page two, then served the remaining topics. This chosen
failure position exercises a committed first page. The live check above covered
one real list page. The replay exercised the scanner, cursor storage, attachment
membership, task queue and notification outbox locally.

| Observation | Previous PR commit `237e9863` | Updated PR |
| --- | --- | --- |
| First request | Started at the latest topics | Started at the stored historical upper bound |
| After page two returned 1059 | One committed page, five-minute retry | One committed page, five-minute retry, HTTP status and failed page recorded |
| Independent queued download from the same source | Held by source cooldown | Eligible under normal browser limits |
| Outbox after the transient failure and recovery | Open and resolved events created | No events created |
| Resumed cursor and membership | Resumed the failed page | Resumed the failed page, recorded all 503 matching attachments including the five unresolved items |

The updated scan committed eight pages including the empty terminal page.
The five terminal stages retained their original attempts and state during
discovery. Publication records kept their fingerprints and foreign keys passed
validation. Regression cases also exercise repeated 1059s, overdue first
retries, in-flight notification delivery, HTTP 401/429, inclusive time boundaries
and a gap of one microsecond between two historical windows.

For the second warning, a separate replay used that window's 126 archived
topics from the same backup and simulated a page-two transport timeout. One
page remained committed, and the source kept its five-minute browser cooldown.
After the retry, the scanner finished all eight page commits and retained the
503 attachment identities including the five existing collisions. No warning
or recovery notice entered the outbox. A navigation failure before page one
is also covered in regression tests. Both failure types surface a persistent
or overdue incident even if the next attempt cannot run.

## Full historical recovery on a fresh production copy

Migration to schema 5 preserved 23,238 document rows, 40,747 stage attempts,
1,332 publications and 3,153 outbox entries. Bootstrap imported 635 historical
plans and left two invalid or unmatched plans unproved. A second import returned
the same result. Publication-row fingerprints and foreign keys passed.

| Historical terminal cohort | Rows | Action |
| --- | ---: | --- |
| Attachment staging collisions with source evidence | 289 | Regenerate the reviewed exact retry plan on the idle release |
| Publication receipts | 528 | Reconstruct membership and check the remote receipt before a retry |
| Summary identity or provider review | 206 | Verify the PDF, accepted cache and provider outcome |
| Extraction or tool review | 22 | Verify content and the local extractor |
| Protected or quarantined content | 88 | Keep protections in place |
| **Total** | **1,133** | Each stage has an individual recorded classification |

The copy recovered all 289 evidenced collisions with synthetic PDF transport
and the actual receipt, finalizer, manifest and SQLite code. Repeating each
download retained 289 distinct archive files and stable artifact bindings. All
five stages from this incident had a succeeded download stage and an artifact
after this step. The unowned staging fixture and publication receipts passed
their integrity checks.

Projection recovery verified and repaired 1,720 accepted summaries on the copy.
A fresh plan found zero candidates remaining and zero exclusions. The recovery
inventory, fingerprints, archives and source records are retained privately.

The public regression fixtures run with the following command.

```bash
python -m pytest -q tests/test_pipeline_tasks.py -k '1059 or navigation_unavailable or persistent_or_overdue or source_auth_and_throttle or exact_endpoints or subsecond_legacy or incidents_dedupe'
```

For a new private snapshot, run `scripts/rehearse_pipeline_recovery.py` with
`--repair-projections`, then run `scripts/rehearse_download_recovery.py` on
that snapshot. The [runtime guide](durable-task-runtime.md#terminal-task-recovery)
lists the full commands and required inputs.

## Release acceptance

After human review, merge and release approval, take a fresh idle snapshot and
regenerate the exact recovery plans. Use the published release procedure to
install the reviewed SHA. Verify a real scheduled listing, an attachment and
its archived bytes, the accepted summary, publication readback and the outbox
receipt. Track the five evidenced collisions individually when applying the
reviewed recovery cohort. Compare the checkpoint and oldest outstanding item
across subsequent scheduled cycles before closing the production incident.
The exact-cohort workflow and remaining review requirements are in the
[recovery runbook](recovery-rehearsal-2026-11-17.md#release-and-historical-recovery-sequence).
