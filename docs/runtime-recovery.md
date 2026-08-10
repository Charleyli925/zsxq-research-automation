# Runtime recovery contract

`launchd` merely wakes `zsxq-pipeline tick`; SQLite is the recovery authority.
Every wake takes `runtime/.pipeline.lock` with non-blocking `fcntl.flock`.
There is no PID file or stale-owner cleanup: a crash closes the descriptor and
the next tick can safely proceed.

## What survives interruption

- A schedule cursor advances only in the same transaction that persists a
  `scheduled` source window. A crash cannot silently skip a configured slot.
- Missed source slots coalesce into one window from the last successful
  checkpoint to now. If the bounded maximum lookback shortens that range, the
  window is explicitly marked truncated.
- A download checkpoint advances only after immutable-plan reconciliation.
  Downloaded PDFs, stage claims, extracted text, summaries, publications, and
  notifications retain their independent identities and recovery paths.
- Codex failure does not prevent a later source download; Lark failure does
  not discard local summary artifacts; notification delivery drains from its
  own outbox even when no PDF is due.

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

`intent -> remote_written -> success` remains the only publication recovery
path. Do not delete a `remote_written` row, text/summary cache, notification
outbox, or prior manifest to force a retry. Repair the cause, create a narrow
`retry plan`, review its fingerprints/count, and use `retry apply --apply`.

## Sleep, wake, and rollback

A powered-off machine cannot run at the intended time. On the next tick the
durable checkpoint makes the missed business range visible; configured
lookback limits are reported rather than silently ignored. A release rollback
only re-points `current` after an idle check. It preserves SQLite, local
artifacts, and any successful remote side effects, so old code must continue
to honor the same durable identities.
