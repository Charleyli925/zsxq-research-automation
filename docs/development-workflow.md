# Automated development workflow

Every repository modification follows a branch-to-Draft-PR workflow. The
automation removes repetitive Git steps but deliberately preserves human
approval for merging and releasing local runtime code.

## Start a change

```bash
python scripts/git_workflow.py begin --slug short-feature-name
```

This refuses a dirty worktree, fetches `origin`, fast-forwards `main`, and
creates `codex/short-feature-name`. If that exact branch is already checked
out, it continues it instead. A request that belongs to an existing Draft PR
continues its branch; an independent request gets a new branch.

## Publish a change

After implementation, list every changed repository file explicitly:

```bash
python scripts/git_workflow.py publish \
  --title "Describe the change" \
  --include scripts/example.py \
  --include tests/test_example.py
```

The command verifies that this list exactly equals the non-ignored changed
file set. It then runs shell syntax checks, repository hygiene, and the
smallest relevant local pytest selection; stages only those files; commits;
fetches/rebases against the latest base when necessary; pushes; and creates a
Draft PR. It refuses a pre-staged index, ignored configuration files,
unrelated untracked files, or an incomplete list. The `--skip-checks` option
is an emergency escape hatch, not normal workflow. If a previously pushed
Draft PR must be rebased onto a newer `main`, it uses only
`--force-with-lease` for that same branch, never an unguarded force push.

GitHub CI runs the complete suite on the Draft PR. A person reviews and merges
only after all required checks pass.

## Keep local state current and clean

After a PR has been merged, synchronize the local source checkout:

```bash
python scripts/git_workflow.py sync-main
```

This requires a clean worktree and uses only fast-forward updates, so it does
not overwrite local work. Runtime configuration and downloaded content remain
Git-ignored. Any non-ignored, unrelated file is a real cleanliness failure:
identify its owner and move it to its own workspace or intentionally exclude
it locally only after that ownership is known.

Synchronizing the source checkout is not production deployment. The local
scheduled runtime must still be updated from a clean, detached verified
tag/SHA through [the deployment workflow](deployment.md).
