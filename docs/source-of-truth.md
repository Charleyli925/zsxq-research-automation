# Source of truth

## Code

The GitHub repository is authoritative for code, tests, prompts, and example
configuration. `main` contains accepted changes. Tags identify deployable
versions.

Local edits are not authoritative until they are committed, pushed, checked,
and merged.

Every repository modification starts from a clean, up-to-date `main` checkout
on a `codex/*` feature branch. It is verified locally with the smallest
relevant test set, staged by exact file list, committed, pushed, and opened as
a Draft PR. GitHub CI runs the complete suite; human review and merge remain
mandatory. See [development-workflow.md](development-workflow.md).

## Deployment

A deployment records the exact tag or commit SHA it runs. Machine-specific
configuration refers to that code but does not copy or fork it.

Development and production checkouts are separate so a scheduler cannot
execute an uncommitted edit.

## Runtime state

Logs, run status, manifests, retry ledgers, quarantine entries, and publish
records describe execution state. They must not be committed or mistaken for
source truth.

## Research content

Downloaded PDFs, extracted text, summaries, and Obsidian notes remain in the
local research library. Git stores neither licensed report content nor
personal research data.
