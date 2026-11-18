# Unified local deployment

The production entrypoint is one user LaunchAgent that runs a finite
`zsxq-pipeline tick` every 300 seconds. It is not a daemon: `RunAtLoad=true`
provides one login-time wake-up and durable SQLite state decides what is due.
There is no `KeepAlive` loop.

The local TOML config is outside Git. Start from
`config/examples/pipeline.example.toml`; it contains the preserved foreign and
domestic logical run times, `Asia/Shanghai`, bounded tick quotas, and only
placeholder paths/identities. Keep credentials, browser profiles, report
content, and the real target chat outside the checkout.
Set `pipeline.research_library_root`, `pipeline.research_library_database`, and
`pipeline.obsidian_vault_root` to paths inside `runtime.root` when local
projections are required. macOS background jobs cannot reliably open every
SQLite, configuration, summary, or Obsidian file under `~/Documents`. Keep the
active projection directories and the one index database in the runtime. To
preserve an existing human-facing layout, move those directories after an
idle backup and replace their former Documents paths with symlinks to the same
runtime-owned entities. The config parser rejects projection paths outside the
runtime, and `doctor` fails closed if a configured destination or database
parent is unavailable.

## Release install

Production code must come from a clean detached reviewed SHA, never a
developer checkout. The installer copies that release to
`~/Library/Application Support/zsxq-research-automation/releases/<sha>/`,
runs schema migration and `doctor`, writes a capability/config-hash manifest,
and only then atomically changes `current`.

```bash
git fetch --tags origin
git checkout --detach <verified-tag-or-sha>

python3 deploy/install_pipeline_runtime.py install \
  --release-root "$PWD" \
  --runtime-root "$HOME/Library/Application Support/zsxq-research-automation" \
  --config "/absolute/path/to/pipeline.toml"

# After reviewing the dry-run JSON and confirming every scheduler is idle:
python3 deploy/install_pipeline_runtime.py install --apply \
  --release-root "$PWD" \
  --runtime-root "$HOME/Library/Application Support/zsxq-research-automation" \
  --config "/absolute/path/to/pipeline.toml"
```

The installer records SHA, schema version, Python, Playwright/Codex/lark/CFT
capability booleans, and a configuration hash. It never copies or prints
configuration content, auth profiles, reports, or credentials. A failed
doctor leaves `current` and the active LaunchAgent unchanged and restores the
preflight database backup under held writer gates. Installation holds both
maintenance and pipeline gates through activation and rejects outstanding
claimed tasks, including the worker startup gap.

`--cutover` is a separate explicit operation. Supply every old LaunchAgent,
cron fragment, exact active crontab line, wrapper, and legacy runtime root only
after the runbook's idle/snapshot checks have passed. The installer backs up
the complete user crontab, requires each `--legacy-crontab-line` to match
exactly once, removes only those approved lines in the activation transaction,
and restores them if activation fails. Do not use `--skip-launchd` for a
production cutover. `rollback --apply` only moves the code entrypoint back to
a prior schema-compatible unified release; an older schema reader is rejected.
It never rewrites SQLite, artifacts, Lark documents,
or notifications.

## Operations

All operational commands share the validated config and SQLite database.
Schema 5 uses the supervisor/worker gates described in
[durable-task-runtime.md](durable-task-runtime.md):

```bash
zsxq-pipeline doctor --config /absolute/path/to/pipeline.toml
zsxq-pipeline status --config /absolute/path/to/pipeline.toml --json
zsxq-pipeline tick --config /absolute/path/to/pipeline.toml --budget-seconds 120
zsxq-pipeline run-stage --config /absolute/path/to/pipeline.toml --stage process
zsxq-pipeline outbox drain --config /absolute/path/to/pipeline.toml
```

`doctor` probes only configured local capabilities; it does not create a Lark
document or run a summary. `tick` returns `busy` without mutating business
state if another tick/manual stage owns the advisory lock.

## Verify the summary client before recovering a backlog

`codex.command` selects the executable for both report summaries and the daily
digest. Pin it to the absolute path of an installed CLI that has completed a
live request with the configured model, reasoning, route, and output schema.
A working desktop chat does not verify a separately installed command line
client. Record the selected executable and its `--version` before changing it;
keep the model and reasoning explicit and do not fall back to another model.

`doctor` checks local capabilities and the summary preflight runs
`codex exec --help`. Neither sends a model request or verifies Lark user
authorization. Before requeueing terminal failures:

1. Confirm one real report can produce a schema-valid local summary through
   the selected executable. Check the response and usage events from `--json`;
   only `--output-last-message` is accepted as the summary artifact.
2. Verify the production Lark profile with `auth status --json --verify`.
   Document creation requires the user identity; a healthy bot identity does
   not verify the user identity or its refresh token.
3. Take the runtime lock and back up the local config and pipeline database.
   Build a retry plan for the exact document/workflow/error combinations that
   the verified repair addresses. Preserve the candidate fingerprints and
   expected count; keep unrelated failures and successful publications out.
4. Apply the reviewed plan, then verify a recovered report through summary,
   document creation, and remote readback. Existing summary artifacts and
   successful publication receipts are reused during recovery.

A WebSocket upgrade diagnostic alone does not establish the final failure:
inspect the CLI exit code and the final response. `model_not_found` is a
configuration or model-access failure and stops for repair. Explicit login
expiry and temporary network/429 failures retain cooldown retries as described
in [runtime-recovery.md](runtime-recovery.md). An OAuth token endpoint EOF is a
network failure; the word `token` in a URL or usage field is not evidence of an
expired login. A document-body write with an uncertain remote outcome requires
reconciliation before another write.

## Research full-text index

After each successful Obsidian archive, the archive helper updates the report's
search row. Periodic reconciliation runs as an independent library task, with
100 updates per pass and a 150-second subprocess deadline. Source-evidence
errors retain their accepted bytes and back off six hours. They do not occupy
the browser or compute slots. Last audit output is stored in SQLite runtime
metadata. A local projection failure is separate from publication success.

For a one-time rebuild after migration or a major metadata rule change, take a
SQLite backup and run the following while holding the pipeline runtime lock:

```bash
python3 scripts/kb_search.py "example" --rebuild \
  --db-path /absolute/runtime/state/research_library.sqlite \
  --vault-root /absolute/runtime/projections/ResearchVault \
  --config-root /absolute/runtime/projections/ResearchLibrary/config
```

The query uses the existing SQLite index. It does not scan the home directory.
The `--rebuild` option refreshes the whole FTS table and its file signatures;
normal publication and daily maintenance update only affected rows.

The notification outbox has its own worker slot and deadline. Long processing
cannot hold its resource. Terminal batch notices wait for the cohort's document
notices; faults deduplicate by source/phase rather than overlapping windows.

For a resolved terminal failure, first create a narrow, reviewed retry plan
matching one stage/workflow/error code, then apply exactly its expected row
count. There is deliberately no broad "retry all" command:

```bash
zsxq-pipeline retry plan --config /absolute/path/to/pipeline.toml \
  --stage publish --workflow-version publish:lark:new --error-code permission_grant_failed \
  --output /absolute/path/to/retry-plan.json
zsxq-pipeline retry apply --config /absolute/path/to/pipeline.toml \
  --plan /absolute/path/to/retry-plan.json --expected-count 1 --apply
```

The historical task wrappers and multi-scheduler installer have been retired
from the repository. Production rollback selects a prior unified release; it
does not restore the former cron or per-source LaunchAgents.

## Release acceptance for schema 5

Review [the production-copy rehearsal](recovery-rehearsal-2026-11-17.md) and merge
the Draft PR only after CI passes. Merging is not a release decision. At the
explicit release boundary, stop new wake-ups, wait for all writers and claimed
tasks to settle, capture fresh SQLite/config snapshots and regenerate exact
recovery plans. Install the clean detached reviewed SHA, then verify one source
page, one real PDF, one local summary, one publication readback, one projection
and one notification before admitting the full reviewed recovery cohort.

Observe contiguous coverage advancing and actual oldest debt/queue counts
decreasing over multiple scheduled cycles. Reject duplicate remote bodies,
repeated incident opens for the same fault, new source_changed evidence, or
completed notices with missing members. Keep writer gates closed if migration
or capability preflight fails. After schema migration, code rollback requires a
compatible release; never point schema-3 code at schema-5 data or restore an old
database over remote writes.
