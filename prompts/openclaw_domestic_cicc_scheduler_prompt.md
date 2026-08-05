This is the short entry prompt used by `scripts/run_zsxq_domestic_cicc_task_via_codex.sh`.
It points Codex to the full domestic CICC task prompt below, so this file stays short and stable.

Read and execute:
${AUTOMATION_ROOT}/prompts/openclaw_domestic_cicc_task_template.md

Hard constraints:
- Use Playwright MCP browser tools as the primary browser driver with dedicated Chrome for Testing + persistent profile.
- Target only `前沿信息收录 -> 国内研报`.
- Apply the canonical keyword file: keep all `中金公司` / `CICC` reports, and also accept any domestic-broker report whose filename matches the configured internet, China ADR, dairy, or animal-husbandry keywords.
- Apply canonical exclusions before positive keywords: skip filenames containing `农林牧渔行业周报`, `农林牧渔行业研究周报`, or `农林牧渔周报`.
- Do not add broad `中金`.
- Do not use chrome-devtools tools as the main execution path in this scheduled task.
- Before the first real download attempt, do not inspect processes, `Downloads`, browser tabs, or full-page snapshots.
- Before the first real download attempt, do not write ad-hoc API scripts; run `python3 scripts/scan_zsxq_download_candidates.py ...` first.
- Never run OS-level process inspection or process termination commands in this task, including `ps`, `pgrep`, `pkill`, `kill`, `killall`, or `lsof`.
- Never terminate any `codex`, `openclaw`, `node`, `npx`, `playwright`, or Chrome process from inside this task.
- If the scan script reports download candidates, start downloading immediately. Do not pause to decide whether to continue.
- Stay on the same topic when helpful, but keep one Playwright tool call limited to one file download.
- Download each immutable-plan file through `scripts/download_zsxq_plan_file.py` with the exact job config, scan plan, and planned file ID.
- Do not replace the canonical helper with MCP `browser_click`, guessed coordinates, bounding boxes, `browser_evaluate`, or `browser_run_code`.
- Treat `source_content_protected` as a source-side web-download restriction; report it without retrying or bypassing it.
- Helper success is not archive proof. Only when the helper returns `downloaded`, run the per-file finalizer immediately with `--wait-seconds 0`; the helper has already completed and validated the file, while blocked results are covered by launcher reconciliation.
- Do not write ad-hoc Python or JavaScript around Playwright `connect_over_cdp`, `expect_download`, or `save_as` just to copy files into `~/Downloads`.
- If UI navigation becomes unstable after a download, follow:
  查看原主题 -> 返回 前沿信息收录 -> 点击 国内研报 再继续。
- If the page still becomes unstable, stop with a clear blocked reason instead of trying process-level recovery.
- Use only the launcher-provided UUID, frozen window, scan-plan path, and run-manifest path.
- Finalize each completed micro-batch with those run-bound values and `--skip-state-update`.
- Never pass `--commit-state`; only the launcher commits the frozen scan end after full reconciliation.
- Output machine-readable lines in plain text only.
  Do not wrap `ZSXQ_REPORT_JSON:` or `ZSXQ_SCAN_ALERT:` in backticks, code fences, bullets, or extra explanation.
- Always return:
  1. archived folder path
  2. downloaded filenames
  3. skipped filenames
  4. blocked reasons, if any
- If login is invalid or expired, do not fake success.
  Return structured blocked reason token `need_reauth` and stop.
