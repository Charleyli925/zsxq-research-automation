# Unified runtime deployment

Use `install_pipeline_runtime.py` from a clean detached release checkout. The
new template at `launchd/zsxq-pipeline.plist.template` is the only scheduler
installed by the unified runtime: `RunAtLoad=true`, `StartInterval=300`, no
`KeepAlive`, explicit HOME/PATH/config/release entrypoint, and unified logs.

```bash
python3 deploy/install_pipeline_runtime.py install \
  --release-root "$PWD" \
  --runtime-root "$HOME/Library/Application Support/zsxq-research-automation" \
  --config /absolute/path/to/pipeline.toml
```

Dry run is the default. `--apply` copies a release, migrates the configured
database, runs doctor, records a sanitized manifest, and switches `current`
only after those gates pass. `--cutover` is explicitly required before the
installer unloads supplied legacy agents or disables supplied cron fragments.
See `../docs/deployment.md` and `../docs/cutover-runbook.md`.

`install_local_runtime.sh` and the two old launchd templates are retained only
for pre-cutover migration/rollback evidence. Do not use them for a new unified
installation and do not run them beside the new LaunchAgent.
