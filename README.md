# investment-reports-automation

Investment-report automation for collecting authorized source files, building
an immutable download plan, archiving verified PDFs, extracting text,
summarizing reports, publishing notifications, and maintaining a local
research knowledge base.

The repository is the canonical source for code, prompts, tests, and
configuration examples. Credentials, browser profiles, downloaded reports,
runtime state, and machine-specific configuration stay outside Git.

## Pipeline

```text
source scan
  -> immutable candidate plan
  -> plan-bound download helper
  -> archive + manifest reconciliation
  -> text extraction / OCR
  -> direct Codex summary + durable local Markdown / quarantine
  -> ResearchLibrary readable-summary projection (best effort)
  -> Lark document publication + notification outbox
  -> verified document -> Obsidian reading projection (best effort)
```

The important invariant is that browser success is not archive proof. A run is
complete only after the finalizer and manifest account for every planned file.

## Repository layout

- `scripts/`: download, reconciliation, report-processing, and knowledge-base tools
- `openclaw_tasks/`: version-controlled compatibility task entrypoints; the
  digest wrapper keeps its existing cron schedule but invokes the Python
  pipeline directly
- `src/zsxq_pipeline/`: durable state, extraction, direct Codex summary, and
  direct lark-cli publication adapters
- `deploy/`: sanitized macOS LaunchAgent templates and release deployment tools
- `prompts/`: browser and summary task contracts
- `config/examples/`: sanitized configuration examples
- `tests/`: unit and workflow tests built around synthetic data
- `docs/`: architecture, deployment, and operating conventions
- `.runtime/`: local state and logs; ignored by Git

## Local setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e ".[dev]"
```

Playwright connects to an existing, authorized Chrome for Testing profile.
Install a local browser only if your deployment requires one:

```bash
.venv/bin/playwright install chromium
```

Prepare local configuration:

```bash
mkdir -p config/local
cp config/examples/job.example.json config/local/zsxq_foreign_reports_job.json
cp config/examples/keywords.example.json config/local/interest_keywords.json
cp openclaw_tasks/zsxq_pdf_digest/config.env.example \
  openclaw_tasks/zsxq_pdf_digest/config.env
```

Replace every placeholder before a real run. `config/local/` and `config.env`
are ignored by Git.

The PDF digest does not require the OpenClaw binary, an agent registry, an
agent session, or agent auth files. It calls `codex exec` with an ephemeral,
read-only structured-output contract after text extraction. It creates,
fetches, and authorizes Lark documents as the local `user` identity, then
sends any group notification as the `bot` identity. A legacy-named
`LARKSUITE_CLI_CONFIG_DIR` is only an existing lark-cli profile location, not
a runtime dependency on OpenClaw.

## Validation

```bash
python scripts/check_repository_hygiene.py
bash -n scripts/*.sh
bash -n openclaw_tasks/zsxq_pdf_digest/*.sh
.venv/bin/python -m pytest -q
```

Tests must not require a real ZSXQ login, Feishu identity, downloaded report,
or production directory.

The preflight below only checks locally installed command capabilities; it
does not create a document, send a group message, or run a real model canary.

```bash
codex exec --help
lark-cli docs +create --help
lark-cli docs +update --help
lark-cli docs +fetch --help
lark-cli im +messages-send --help
```

## Source-of-truth policy

- GitHub `main` is the latest accepted source.
- Changes use a branch and pull request after the initial baseline.
- Releases use semantic version tags.
- Production runs a reviewed tag or commit SHA from a separate deployment
  checkout, never an uncommitted development working tree.
- Runtime truth remains in task logs, manifests, and state outside Git.

See [source-of-truth.md](docs/source-of-truth.md) and
[deployment.md](docs/deployment.md). The runtime recovery contract and the
post-reboot checks are documented in
[runtime-recovery.md](docs/runtime-recovery.md). The required branch,
validation, and Draft-PR process is documented in
[development-workflow.md](docs/development-workflow.md).

## Safety and data rights

Use the automation only with accounts, groups, files, and publishing targets
you are authorized to access. The repository does not include credentials,
session data, downloaded reports, or mechanisms for bypassing source-side
download restrictions.

## Current licensing status

This repository is private and no open-source license is granted yet. Choose
and add a license only when a reviewed subset is ready for public release.
