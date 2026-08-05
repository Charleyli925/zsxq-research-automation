# Codex Chrome Runbook

Use Chrome DevTools MCP against the dedicated Chrome for Testing session (fixed debug port + persistent profile).

## Required inputs

- Job config: `${AUTOMATION_ROOT}/config/local/zsxq_foreign_reports_job.json`
- Keywords: `${AUTOMATION_ROOT}/config/local/interest_keywords.json`
- Runtime prompt override: `${AUTOMATION_ROOT}/prompts/openclaw_runtime_prompt.md`
- State: `${INVESTMENT_REPORTS_RUNTIME_DIR}/state/zsxq_foreign_reports_state.json`

## Browser task

1. Determine window mode:
   - default mode: `last_successful_check_at -> current run time`
   - explicit mode: if runtime instruction provides `window_start/window_end`, use those values.
2. Read the keywords file and the runtime prompt override.
3. Control the dedicated Chrome for Testing browser with Chrome DevTools MCP.
4. Open the tag URL from the job config.
5. Scan the visible `外资研报` feed from newest to older items.
6. Only process posts whose publish time is inside the active window.
7. Within those posts, only download PDFs whose titles match the current focus.
8. For each matched PDF:
   - open `查看详情`
   - open the target PDF row
   - click `下载`
   - one browser tool call should handle one file download only
   - do not write ad-hoc Python or JavaScript around Playwright `connect_over_cdp`, `expect_download`, or `save_as` just to copy files into `~/Downloads`
   - if the detail layer does not return cleanly after download, click `查看原主题`
   - in the newly opened topic tab, click `返回 前沿信息收录`
   - on the group page, click `外资研报` again before continuing
9. After downloads finish, run:

```bash
python3 scripts/finalize_download_batch.py --downloaded-after "<run_started_at>"
```

If this run is in explicit mode, run:

```bash
python3 scripts/finalize_download_batch.py --window-start "<window_start>" --window-end "<window_end>" --downloaded-after "<run_started_at>"
```

10. Report the archived folder path, file count, and skipped titles.

## Guardrails

- Use the dedicated Chrome for Testing session only.
- Do not open a fresh Playwright browser.
- Skip duplicates already present in the archive root.
- Prefer the `查看原主题 -> 返回 前沿信息收录 -> 外资研报` recovery path after each download when navigation becomes unstable.
- If login is expired, report blocked token `need_reauth` (or `blocked_login`) instead of guessing.
- In explicit mode, include explicit window bounds in the summary.
