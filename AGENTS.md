# Development workflow

This repository uses GitHub as the source of truth. Treat the following as the
default workflow for every change that modifies repository files.

1. Decide branch scope before editing. Continue the current `codex/*` branch
   only when the request belongs to its open Draft PR; otherwise run
   `python scripts/git_workflow.py begin --slug <short-feature-name>` before
   changing files. The command refuses a dirty worktree and starts new work
   from the latest `origin/main`.
2. Keep machine configuration, runtime state, downloaded content, credentials,
   and unrelated user files outside the repository. Never delete, move, stage,
   or add an ignore rule for an unknown user file merely to make Git appear
   clean. Resolve its ownership first.
3. After implementation, pass every changed repository-relative file explicitly
   to `python scripts/git_workflow.py publish --title <title> --include <file>`.
   Repeat `--include` for all files. The command rejects incomplete scopes,
   runs focused local validation, stages only those files, commits, rebases
   onto the latest base when needed, pushes, and opens a Draft PR. GitHub CI
   runs the complete suite.
4. Do not use `git add -A`, direct pushes to `main`, automatic merges, or
   automatic production deployment. GitHub CI must pass, then a human reviews
   and merges the PR.
5. After an approved merge, run `python scripts/git_workflow.py sync-main` to
   fast-forward the local source checkout. Production uses a clean detached
   tag/SHA checkout and is updated only after an explicit release decision and
   an idle-runtime check.

Analysis-only requests do not require a branch or PR. If the user explicitly
asks not to publish, keep changes local and state that the normal publish gate
was intentionally skipped.
