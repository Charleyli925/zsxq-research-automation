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
Set `pipeline.research_library_root` and `pipeline.obsidian_vault_root` to the
existing writable absolute paths when those local projections are required;
set `pipeline.research_library_database` to one path inside `runtime.root`.
macOS background jobs cannot safely open SQLite databases under
`~/Documents`, even when ordinary Markdown writes work there. Keep one index
database in the runtime and, when preserving an older library layout, replace
the old database path with a symlink to that same file after an idle backup.
`doctor` fails closed if a configured destination or database parent is
unavailable.

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
doctor leaves `current` and the active LaunchAgent unchanged.

`--cutover` is a separate explicit operation. Supply every old LaunchAgent,
cron fragment, exact active crontab line, wrapper, and legacy runtime root only
after the runbook's idle/snapshot checks have passed. The installer backs up
the complete user crontab, requires each `--legacy-crontab-line` to match
exactly once, removes only those approved lines in the activation transaction,
and restores them if activation fails. Do not use `--skip-launchd` for a
production cutover. `rollback --apply` only moves the code entrypoint back to
the prior unified release; it never rewrites SQLite, artifacts, Lark documents,
or notifications.

## Operations

All operational commands share the same validated config, runtime lock, and
SQLite database:

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

Before a tick enters potentially long PDF/model processing, it drains durable
notifications left by the prior tick. It drains again after processing when
the soft deadline still has time remaining. Therefore an over-budget process
may defer its own new notification until the next wake-up, but a sustained
processing backlog cannot starve the outbox.

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
