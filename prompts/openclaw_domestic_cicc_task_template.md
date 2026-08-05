# OpenClaw Domestic CICC Task Template

This is the detailed browser-task prompt for `ZSXQ_国内研报_中金公司`.
`openclaw_domestic_cicc_scheduler_prompt.md` points here, and Codex follows this file during each run.

Use Playwright MCP browser tools to control the dedicated Chrome for Testing instance, not the user's daily Chrome.

## Read these files first

- `${AUTOMATION_ROOT}/config/local/zsxq_domestic_cicc_reports_job.json`
- `${AUTOMATION_ROOT}/config/local/zsxq_domestic_cicc_keywords.json`
- `${AUTOMATION_ROOT}/prompts/openclaw_domestic_cicc_runtime_prompt.md`
- `${INVESTMENT_REPORTS_RUNTIME_DIR}/state/zsxq_domestic_cicc_reports_state.json`

Important:
- `config/local/zsxq_domestic_cicc_keywords.json` is the real keyword source for this task.
- `prompts/openclaw_domestic_cicc_runtime_prompt.md` is only a readable snapshot for the current focus.
- The task must stay on `前沿信息收录 -> 国内研报`.
- The task must keep matching all `中金公司` / `CICC` reports, and must also match reports from other domestic brokers when their filenames contain any keyword in the canonical keyword file.
- The added focus covers internet / China ADR companies and themes plus dairy / animal-husbandry companies and themes.
- Canonical exclusions take priority over positive keywords: skip filenames containing `农林牧渔行业周报`, `农林牧渔行业研究周报`, or `农林牧渔周报`, even if the same filename also contains `生猪`, `猪价`, `养殖`, or another positive keyword.
- Never match broad `中金`.

## Task

1. Use the launcher-provided `run_id`, `window_start`, and `window_end`. The launcher freezes them only after acquiring the shared lock; do not recalculate them from the clock or state file.
2. Keep that frozen window and the launcher-provided scan-plan/run-manifest paths consistent for every scan and finalize command.
3. Before any browser inspection, run the local candidate-scan script exactly once:

```bash
python3 scripts/scan_zsxq_download_candidates.py --window-start "<window_start>" --window-end "<window_end>" --job-config "${AUTOMATION_ROOT}/config/local/zsxq_domestic_cicc_reports_job.json" --keyword-file "${AUTOMATION_ROOT}/config/local/zsxq_domestic_cicc_keywords.json" --output "<scan_plan_path>"
```

4. Read `<scan_plan_path>` and use it as the immutable allow-list and only source of truth for:
   - `window_new_docs_count`
   - `keyword_matched_docs_count`
   - `download_candidate_count`
   - `blocked_reason`
   - which topics/files must be downloaded
5. Fast-path rule:
   - if `blocked_reason == "need_reauth"`, stop and report `need_reauth`
   - if `download_candidate_count == 0`, do not do DOM exploration; go straight to final structured summary
   - if `download_candidate_count > 0`, immediately start downloading; do not pause to decide whether to continue
6. DOM fallback rule:
   - only if the scan script reports API failure (`blocked_reason` starts with `api_`) may you consider DOM fallback
   - before DOM fallback, run `python3 scripts/discover_zsxq_topics_api.py --tag-url "${ZSXQ_TAG_URL}"` once
   - only if that probe still cannot recover API-first, continue with DOM fallback
7. For each candidate topic, enter detail once and batch-process all matching PDF attachments in that topic:
   - use the canonical plan-bound Playwright helper for each file:
     `python3 scripts/download_zsxq_plan_file.py --job-config "<job_config_path>" --scan-plan "<scan_plan_path>" --file-id "<planned_file_id>" --cdp-endpoint "http://127.0.0.1:9223"`
   - the helper validates the immutable plan row, navigates directly to its exact `topic_url`, uses exact visible filename/button locators, waits for the real browser download event, and validates PDF bytes before staging
   - do not replace the helper with MCP `browser_click`, guessed screen coordinates, bounding-box clicks, `browser_evaluate`, or `browser_run_code`
   - if the helper returns `source_content_protected`, the website is explicitly withholding web download; record that exact reason and do not retry or attempt to bypass the source restriction
   - if the helper returns `downloaded`, run the run-aware finalizer immediately with `--wait-seconds 0`; treat the helper result as successful only when that exact candidate is reported downloaded or otherwise satisfied
   - if the helper returns a blocked source/browser reason, record it and continue without a per-file finalizer wait; the launcher performs the final deterministic reconciliation
   - if the candidate is still missing, do not silently continue or claim success; record `browser_download_unstable` for that candidate and continue only after its deterministic reconciliation is recorded
   - keep one browser tool call limited to one file download
   - do not write ad-hoc Python or JavaScript around Playwright `connect_over_cdp`, `expect_download`, or `save_as` just to copy files into `~/Downloads`
   - finish all matching files in this topic before returning to feed
   - if return flow is unstable, use once-per-topic recovery:
     `查看原主题 -> 返回 前沿信息收录 -> 国内研报`
8. After each completed browser-download micro-batch, run the same run-aware finalizer. It may be called more than once; every call appends to one aggregate manifest and must not advance the checkpoint:

```bash
python3 scripts/finalize_download_batch.py --config "${AUTOMATION_ROOT}/config/local/zsxq_domestic_cicc_reports_job.json" --keywords "${AUTOMATION_ROOT}/config/local/zsxq_domestic_cicc_keywords.json" --state "${INVESTMENT_REPORTS_RUNTIME_DIR}/state/zsxq_domestic_cicc_reports_state.json" --window-start "<window_start>" --window-end "<window_end>" --downloaded-after "<run_started_at>" --run-id "<run_id>" --scan-plan "<scan_plan_path>" --run-manifest "<run_manifest_path>" --wait-seconds 0 --skip-state-update
```

Only the launcher may make the final `--commit-state` call after deterministic reconciliation confirms that every planned candidate is accounted for. Never add `--commit-state` from inside the browser task.

9. Return a concise summary with:
   - archived folder path
   - downloaded filenames
   - skipped filenames
   - blocked reasons
   - `ZSXQ_REPORT_JSON` scan metadata: `window_new_docs_count`, `keyword_matched_docs_count`, `download_candidate_count`, `download_success_count`, `no_download_reason`, `core_reason`
   - Agent-reported success/counts are advisory only. The launcher derives downloaded files, archive directories, and final status from `<run_manifest_path>`.
   - Print the machine-readable line as plain text:
     `ZSXQ_REPORT_JSON:{...}`
   - Do not wrap `ZSXQ_REPORT_JSON:` or `ZSXQ_SCAN_ALERT:` in backticks, markdown code fences, bullets, or extra punctuation.

## Constraints

- Use Playwright MCP browser tools as the primary browser execution path.
- Use the dedicated Chrome for Testing session prepared by the launcher script.
- Do not switch to the user's normal Chrome profile/session.
- Never run OS-level process inspection or process termination commands in this task, including `ps`, `pgrep`, `pkill`, `kill`, `killall`, or `lsof`.
- Never terminate any `codex`, `openclaw`, `node`, `npx`, `playwright`, or Chrome process from inside this task.
- Candidate filtering must come from `scan_zsxq_download_candidates.py` first, DOM as fallback only.
- Finalize may archive only filenames/file IDs present in the immutable scan plan.
- Never update `last_successful_check_at` from a finalize timestamp; only the launcher may commit the frozen `window_end` once.
- Do not enter topic detail for topics that already fail time/keyword filtering on list/API data.
- Skip duplicates already present under `${RESEARCH_LIBRARY_ROOT}/pdfs`.
- Prefer the topic-tab recovery path when the file detail layer becomes sticky after download.
- Helper success is not archive proof; only run-manifest/finalizer reconciliation proves that the planned file landed or was already satisfied.
- If browser execution becomes unstable, only use page-level recovery or return a blocked reason. Do not use process-level recovery.
- If the site is logged out or login expired, stop and report blocker token `need_reauth`.
- In explicit mode, the summary must include the explicit window start/end.
- For no-download cases, always fill `no_download_reason` and `core_reason` with the best structured value instead of leaving them unknown when the reason is already known.
- Before the first real download attempt, do not run process inspection, `Downloads` inspection, tab listing, full-page snapshot, or ad-hoc Playwright JavaScript experiments.
- Before the first real download attempt, do not write custom API fetch code; use `scan_zsxq_download_candidates.py`.
- After the scan script returns candidates, do not ask whether to continue; continue directly into download.
