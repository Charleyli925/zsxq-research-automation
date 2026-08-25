# Changelog

All notable changes will be documented here.

## [0.1.0] - Unreleased

- Created a standalone source-controlled project.
- Separated source, example configuration, private deployment configuration,
  runtime state, and report content.
- Added portable path defaults, dependency declarations, tests, CI, and
  source-of-truth documentation.
- Unified production execution behind one LaunchAgent/worker CLI, retired the
  former task wrappers and multi-scheduler installer, and moved OCR into the
  package-owned pipeline.
- Restored configured ResearchLibrary and Obsidian projections in the unified
  processing path and added lint/retired-entrypoint gates.
- Routed every ResearchLibrary and Obsidian sidecar through one explicit SQLite
  index inside the runtime root so the unified LaunchAgent avoids macOS
  Documents-folder TCC denials.
- Kept the publish-stage lease recoverable until every local projection is
  complete, so a sidecar failure retries locally without writing the verified
  Feishu document a second time.
- Required active ResearchLibrary and Obsidian projection roots to live inside
  the runtime, with user-facing Documents locations represented by symlinks,
  so launchd never depends on protected-folder TCC prompts.
- Propagated partial processor outcomes into the top-level tick result instead
  of allowing projection failures to appear as a successful scheduler run.
- Restored concise source-window notifications for exact non-empty download
  counts, one summary start, bounded 25/50/75 percent summary/publication
  milestones, and one terminal batch completion.
- Bounded dedicated CFT page targets, retried transient CDP page creation with
  a fresh transport, forced local CDP HTTP to bypass ambient system proxies,
  closed the API-discovery diagnostic page, persisted exact blocked-browser
  evidence, and added one deduplicated retry alert per source window and reason.
- Added a headed-background CFT mode on macOS so scheduled cold starts do not
  take focus while the isolated window remains available from the Dock, and
  preserved exact NFD filesystem paths across summary identity, cache, and
  publication stages to prevent Unicode-equivalent names from leaking leases.
