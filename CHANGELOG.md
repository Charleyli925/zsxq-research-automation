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
