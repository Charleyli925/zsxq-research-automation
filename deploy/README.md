# Local runtime deployment

`install_local_runtime.sh` turns a reviewed, detached repository checkout into
the local runtime for the two ZSXQ download tasks. It is deliberately
conservative:

- dry-run is the default;
- `--apply` rejects dirty and branch checkouts;
- it refuses to replace wrappers while a task is running;
- `config.env`, browser state, results, logs, and downloaded files are left in
  the task directories;
- old local wrappers are moved into `.deployment-backups/` before links are
  installed;
- a generated, Git-ignored `deployment.env` pins code paths to the deployed
  checkout without copying private configuration into Git.

## First deployment or upgrade

```bash
git fetch --tags origin
git checkout --detach <verified-tag-or-sha>

bash deploy/install_local_runtime.sh --dry-run
bash deploy/install_local_runtime.sh --apply \
  --foreign-label com.example.zsxq-autodownload \
  --domestic-label com.example.zsxq-domestic-cicc
```

Use the existing labels when upgrading an existing machine. The defaults assume
these task folders beneath `OPENCLAW_TASKS_ROOT` (or
`$HOME/.openclaw/workspace/tasks`):

- `ZSXQ_autodownload`
- `ZSXQ_国内研报_中金公司`

Pass `--foreign-task-dir` or `--domestic-task-dir` if the local names differ.
The installation needs a private `config.env` in each folder; start from the
sanitized examples in `openclaw_tasks/zsxq_download/`.

## What the installer manages

The generated LaunchAgents retain the normal four daily schedules and set
`RunAtLoad=true`, which causes one catch-up trigger at the next login after a
reboot. `ThrottleInterval=60` protects against launchd restart churn. The
task-level lock prevents an overlapping trigger from starting a second run.

After an install, inspect the runtime rather than assuming a scheduled job is
healthy:

```bash
launchctl print gui/$(id -u)/com.example.zsxq-autodownload
launchctl print gui/$(id -u)/com.example.zsxq-domestic-cicc
```

For a machine that is not currently in a GUI launchd session, use
`--skip-launchd` to prepare links and `deployment.env`, then rerun without it
after logging in. The `--allow-dirty` and `--allow-branch` flags are emergency
escape hatches, not normal release workflow.
