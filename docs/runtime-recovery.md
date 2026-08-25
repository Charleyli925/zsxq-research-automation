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
- The dedicated CFT session bounds same-origin automation pages before attach
  and retries one transient CDP/page-creation failure through a fresh
  Playwright transport. If recovery still fails, the blocked plan keeps the
  sanitized browser exception and cleanup counters, the source window remains
  scheduled, and one reason-specific alert is deduplicated per window. The
  worker never terminates a live browser process automatically.
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
