# Unified scheduler cutover runbook

This is a production change procedure, not a consequence of merging a pull
request. Complete it only after an explicit release decision and a successful
PR3 download canary plus PR4 direct-Codex/Lark canary.

1. Freeze a clean detached reviewed SHA and record its SHA/config hash.
2. Inventory every old foreign-download LaunchAgent, domestic-download
   LaunchAgent, digest cron entry, task runtime, and current deployment
   manifest. Stop if any additional production entrypoint can trigger the
   chain.
3. Pause the three old schedulers; wait until all are idle. Snapshot their
   state, plist/cron fragments, and hashes. Do not move PDFs, summaries,
   ResearchLibrary, Obsidian, browser profiles, or Lark profiles.
4. Run `zsxq-pipeline legacy plan`, manually inspect conflicts/counts, then
   run `legacy apply --apply` against the new runtime database.
5. Run the detached release's installer dry run, then `doctor`. Resolve any
   local CFT, Codex, Lark, config, or `.openclaw` migration debt before
   activation.
6. With all runtimes still idle, use the installer with explicit `--cutover`,
   the complete legacy scheduler inventory, and the exact digest cron line as
   `--legacy-crontab-line`. Verify the backed-up crontab, manifest, one new
   LaunchAgent, zero old loaded LaunchAgents, and zero remaining matching cron
   lines; there must be no overlap window.
7. Run one bounded manual `tick` on canary work. Verify source windows,
   artifact identities, publication recovery, and notification idempotency.
8. Soak: foreign and domestic sources each complete four real scheduled
   windows (eight total), plus one crash-recovery, Codex-unavailable, and
   Lark-unavailable exercise. Confirm no missed or duplicated PDF, summary,
   document, or notification.
9. Keep the production legacy runtime snapshot read-only through soak. The
   repository entrypoints may be retired only in a separate reviewed change
   after the unified canary has proved download, summary, publication, and
   notification recovery; rollback continues through prior unified releases.

## Stop and rollback

Stop before activation if the importer has unexplained conflicts, an old task
is not idle, a scheduler cannot be atomically disabled, capabilities differ
under launchd, or the source cannot safely support the required catch-up
range. Do not solve any of these by clearing state.

If post-activation evidence requires rollback, first wait for the unified
runtime to become idle, then run the installer's explicit `rollback --apply`.
It moves only the `current` code/scheduler entrypoint back to the prior
release. It must not overwrite the database, delete artifacts, revoke a Lark
document, or retract a notification.
