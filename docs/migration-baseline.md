# Migration baseline

This repository starts from a clean snapshot of the active automation source.
The legacy parent repository history was intentionally not imported because it
mixed unrelated projects, generated outputs, runtime logs, and potentially
sensitive historical content.

The initial baseline includes the current source and tests but excludes:

- logs and task state
- browser and authentication profiles
- downloaded or derived report content
- real chat, group, tag, and topic identifiers
- machine-specific paths and private configuration

The existing production directory remains unchanged during this migration.
Production should be rewired only after the private repository baseline and
deployment preflight are verified.
