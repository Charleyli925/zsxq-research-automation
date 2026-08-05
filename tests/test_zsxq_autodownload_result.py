from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.zsxq_autodownload_result import (
    build_canonical_result,
    build_last_result,
    derive_reason_code,
    ensure_current_result,
    parse_machine_report_text,
    parse_preflight_reason_code,
    parse_scan_alert_text,
)


class ZsxqAutodownloadResultTests(unittest.TestCase):
    def test_ensure_current_result_replaces_stale_success_after_failed_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            canonical_path = Path(tmp_dir) / "canonical.json"
            canonical_path.write_text(
                json.dumps(
                    {
                        "run_id": "old-run",
                        "run_started_at": "2026-08-01T08:00:00+08:00",
                        "run_finished_at": "2026-08-01T08:10:00+08:00",
                        "codex_exit_code": 0,
                        "status": "success",
                    }
                ),
                encoding="utf-8",
            )

            result, replaced = ensure_current_result(
                canonical_path=canonical_path,
                run_id="new-run",
                run_started_at="2026-08-04T12:00:00+08:00",
                run_finished_at="2026-08-04T12:00:01+08:00",
                requested_window_start="",
                requested_window_end="",
                pre_last_successful_check_at="2026-08-01T08:10:00+08:00",
                process_exit_code=2,
            )

            persisted = json.loads(canonical_path.read_text(encoding="utf-8"))

        self.assertTrue(replaced)
        self.assertEqual(result["run_id"], "new-run")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["codex_exit_code"], 2)
        self.assertTrue(result["recovered_stale_result"])
        self.assertEqual(persisted, result)

    def test_ensure_current_result_keeps_matching_current_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            canonical_path = Path(tmp_dir) / "canonical.json"
            expected = {
                "run_id": "current-run",
                "run_started_at": "2026-08-04T12:00:00+08:00",
                "run_finished_at": "2026-08-04T12:01:00+08:00",
                "codex_exit_code": 0,
                "status": "success",
            }
            canonical_path.write_text(json.dumps(expected), encoding="utf-8")

            result, replaced = ensure_current_result(
                canonical_path=canonical_path,
                run_id="current-run",
                run_started_at=expected["run_started_at"],
                run_finished_at=expected["run_finished_at"],
                requested_window_start="",
                requested_window_end="",
                pre_last_successful_check_at="",
                process_exit_code=0,
            )

        self.assertFalse(replaced)
        self.assertEqual(result, expected)

    def test_parse_preflight_reason_code_uses_launcher_blocked_line(self) -> None:
        raw = (
            "prompt example: blocked_browser_cdp_unresponsive\n"
            "[BLOCKED] blocked_browser_cdp_unresponsive: Timeout 45000ms exceeded\n"
        )

        self.assertEqual(
            parse_preflight_reason_code(raw),
            "blocked_browser_cdp_unresponsive",
        )

    def test_legacy_connect_over_cdp_timeout_is_classified_as_cdp_unresponsive(self) -> None:
        reason = derive_reason_code(
            codex_rc=22,
            downloaded_count=0,
            no_download_reason="blocked_browser",
            candidate_reason="",
            raw_text="BrowserType.connect_over_cdp: Timeout 45000ms exceeded",
        )

        self.assertEqual(reason, "blocked_browser_cdp_unresponsive")

    def test_parse_helpers_ignore_backtick_examples(self) -> None:
        text = (
            "Do not wrap `ZSXQ_REPORT_JSON:` or `ZSXQ_SCAN_ALERT:` in backticks.\n"
            "`ZSXQ_SCAN_ALERT: api_unavailable_dom_fallback`\n"
            '`ZSXQ_REPORT_JSON:{"window_new_docs_count":1}`\n'
            'ZSXQ_REPORT_JSON:{"window_new_docs_count":0,"no_download_reason":"no_window_updates","core_reason":"window_has_no_updates"}\n'
        )

        report = parse_machine_report_text(text)
        alert = parse_scan_alert_text(text)

        self.assertEqual(report["window_new_docs_count"], 0)
        self.assertEqual(alert, "")

    def test_parse_helpers_ignore_unframed_prompt_examples(self) -> None:
        text = (
            "Correct format example:\n"
            'ZSXQ_REPORT_JSON:{"window_new_docs_count":1,"keyword_matched_docs_count":0,'
            '"download_candidate_count":0,"download_success_count":0,'
            '"no_download_reason":"no_keyword_match","core_reason":"window_has_updates_but_no_keyword_match"}\n'
            "Runtime warning: connection closed before task output.\n"
        )

        report = parse_machine_report_text(text)

        self.assertEqual(report, {})

    def test_parse_helpers_ignore_prompt_and_tool_output_examples_in_codex_log(self) -> None:
        text = (
            "user\n"
            "Correct format example:\n"
            'ZSXQ_REPORT_JSON:{"window_new_docs_count":1,"no_download_reason":"no_keyword_match"}\n'
            "codex\n"
            "我先看模板。\n"
            "exec\n"
            "/bin/zsh -lc 'sed -n ...'\n"
            'ZSXQ_REPORT_JSON:{"window_new_docs_count":2,"no_download_reason":"no_window_updates"}\n'
            "codex\n"
            'ZSXQ_REPORT_JSON:{"window_new_docs_count":192,"keyword_matched_docs_count":168,'
            '"download_candidate_count":401,"download_success_count":301,'
            '"no_download_reason":"download_incomplete","core_reason":"download_candidates_not_completed"}\n'
        )

        report = parse_machine_report_text(text)

        self.assertEqual(report["window_new_docs_count"], 192)
        self.assertEqual(report["download_success_count"], 301)
        self.assertEqual(report["core_reason"], "download_candidates_not_completed")

    def test_build_canonical_result_prefers_state_for_downloaded_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_path = tmp_path / "state.json"
            raw_output_path = tmp_path / "raw.log"
            state_path.write_text(
                json.dumps(
                    {
                        "last_successful_check_at": "2026-04-07T12:06:38+08:00",
                        "last_window_start": "2026-04-07T08:08:01+08:00",
                        "last_window_end": "2026-04-07T12:06:38+08:00",
                        "last_batch_dir": "/tmp/batch",
                        "last_batch_file_count": 2,
                        "last_batch_files": ["a.pdf", "b.pdf"],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            raw_output_path.write_text(
                'ZSXQ_REPORT_JSON:{"window_new_docs_count":7,"keyword_matched_docs_count":3,'
                '"download_candidate_count":2,"download_success_count":2,'
                '"no_download_reason":"unknown","core_reason":"download_completed",'
                '"window_start":"2026-04-07T08:08:01+08:00","window_end":"2026-04-07T12:06:38+08:00"}\n',
                encoding="utf-8",
            )

            result = build_canonical_result(
                state_path=state_path,
                raw_output_path=raw_output_path,
                run_started_at="2026-04-07T12:00:00+08:00",
                run_finished_at="2026-04-07T12:07:47+08:00",
                requested_window_start="",
                requested_window_end="",
                pre_last_successful_check_at="2026-04-07T08:08:01+08:00",
                codex_rc=0,
            )

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["downloaded_count"], 2)
            self.assertEqual(result["downloaded_files"], ["a.pdf", "b.pdf"])
            self.assertEqual(result["archive_dir"], "/tmp/batch")
            self.assertEqual(result["reason_code"], "download_completed")

    def test_build_canonical_result_detects_documents_permission_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_path = tmp_path / "state.json"
            raw_output_path = tmp_path / "raw.log"
            state_path.write_text("{}\n", encoding="utf-8")
            raw_output_path.write_text(
                "operation not permitted\nrun_zsxq_task_via_codex.sh\n",
                encoding="utf-8",
            )

            result = build_canonical_result(
                state_path=state_path,
                raw_output_path=raw_output_path,
                run_started_at="2026-04-07T12:00:00+08:00",
                run_finished_at="2026-04-07T12:07:47+08:00",
                requested_window_start="",
                requested_window_end="",
                pre_last_successful_check_at="2026-04-07T08:08:01+08:00",
                codex_rc=126,
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason_code"], "blocked_documents_permission")

    def test_build_canonical_result_nonzero_exit_ignores_report_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_path = tmp_path / "state.json"
            raw_output_path = tmp_path / "raw.log"
            state_path.write_text("{}\n", encoding="utf-8")
            raw_output_path.write_text(
                'ZSXQ_REPORT_JSON:{"window_new_docs_count":1,"keyword_matched_docs_count":0,'
                '"download_candidate_count":0,"download_success_count":0,'
                '"no_download_reason":"no_keyword_match",'
                '"core_reason":"window_has_updates_but_no_keyword_match"}\n',
                encoding="utf-8",
            )

            result = build_canonical_result(
                state_path=state_path,
                raw_output_path=raw_output_path,
                run_started_at="2026-06-13T22:35:51+08:00",
                run_finished_at="2026-06-13T22:40:32+08:00",
                requested_window_start="2026-06-12T16:01:26+08:00",
                requested_window_end="2026-06-13T22:40:32+08:00",
                pre_last_successful_check_at="2026-06-12T16:01:26+08:00",
                codex_rc=101,
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["reason_code"], "task_failed")
            self.assertEqual(result["reason_text"], "任务执行失败，请查看日志定位具体原因")
            self.assertEqual(result["window_new_docs_count"], -1)

    def test_build_canonical_result_detects_cloud_requirements_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_path = tmp_path / "state.json"
            raw_output_path = tmp_path / "raw.log"
            state_path.write_text("{}\n", encoding="utf-8")
            raw_output_path.write_text(
                "Error: timed out waiting for cloud requirements after 15s\n",
                encoding="utf-8",
            )

            result = build_canonical_result(
                state_path=state_path,
                raw_output_path=raw_output_path,
                run_started_at="2026-04-13T12:00:01+08:00",
                run_finished_at="2026-04-13T12:00:20+08:00",
                requested_window_start="",
                requested_window_end="",
                pre_last_successful_check_at="2026-04-10T16:03:18.643108+08:00",
                codex_rc=1,
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["reason_code"], "cloud_requirements_timeout")
            self.assertEqual(
                result["reason_text"],
                "Codex 云端依赖检查超时，任务还没进入知识星球扫描就提前退出了",
            )

    def test_build_canonical_result_detects_hard_codex_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_path = tmp_path / "state.json"
            raw_output_path = tmp_path / "raw.log"
            state_path.write_text("{}\n", encoding="utf-8")
            raw_output_path.write_text(
                'ZSXQ_EXEC_TIMEOUT_JSON:{"timeout_seconds": 5400}\n',
                encoding="utf-8",
            )

            result = build_canonical_result(
                state_path=state_path,
                raw_output_path=raw_output_path,
                run_started_at="2026-08-01T00:00:00+08:00",
                run_finished_at="2026-08-01T01:30:00+08:00",
                requested_window_start="",
                requested_window_end="",
                pre_last_successful_check_at="2026-07-31T20:30:00+08:00",
                codex_rc=124,
            )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["reason_code"], "codex_exec_timeout")
            self.assertIn("硬超时", result["reason_text"])

    def test_build_last_result_uses_canonical_status_directly(self) -> None:
        canonical_result = {
            "run_finished_at": "2026-04-07T12:07:47+08:00",
            "status": "partial",
            "codex_exit_code": 0,
            "effective_window_start": "2026-04-07T08:08:01+08:00",
            "effective_window_end": "2026-04-07T12:06:38+08:00",
            "requested_window_start": "",
            "requested_window_end": "",
            "downloaded_count": 0,
            "downloaded_files": [],
            "archive_dir": None,
            "no_download_reason": "download_incomplete",
            "reason_code": "download_candidates_not_completed",
            "reason_text": "检测到候选文档，但下载没有完成",
            "window_new_docs_count": 7,
            "keyword_matched_docs_count": 3,
            "download_candidate_count": 2,
            "download_success_count": 0,
            "scan_mode": "api_first",
            "api_probe_status": "ok",
            "scan_alert": None,
        }

        last_result, summary = build_last_result(
            canonical_result=canonical_result,
            window_mode="state",
            explicit_window_start="",
            explicit_window_end="",
            window_note="",
            log_path="/tmp/cron.log",
            result_md_path="/tmp/result.md",
            canonical_result_path="/tmp/canonical.json",
        )

        self.assertEqual(last_result["status"], "partial")
        self.assertIn("部分完成", summary)

    def test_build_canonical_result_normalizes_legacy_no_download_codes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_path = tmp_path / "state.json"
            raw_output_path = tmp_path / "raw.log"
            state_path.write_text(
                json.dumps(
                    {
                        "last_successful_check_at": "2026-04-07T16:01:38.758829+08:00",
                        "last_window_start": "2026-04-07T15:49:27.275551+08:00",
                        "last_window_end": "2026-04-07T16:01:38.758829+08:00",
                        "last_batch_dir": None,
                        "last_batch_file_count": 0,
                        "last_batch_files": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            raw_output_path.write_text(
                'ZSXQ_REPORT_JSON:{"window_new_docs_count":0,"keyword_matched_docs_count":0,'
                '"download_candidate_count":0,"download_success_count":0,'
                '"no_download_reason":"no_new_docs","core_reason":"window_no_updates",'
                '"scan_mode":"api_first","api_probe_status":"ok"}\n',
                encoding="utf-8",
            )

            result = build_canonical_result(
                state_path=state_path,
                raw_output_path=raw_output_path,
                run_started_at="2026-04-07T16:00:00+08:00",
                run_finished_at="2026-04-07T16:02:08+08:00",
                requested_window_start="",
                requested_window_end="",
                pre_last_successful_check_at="2026-04-07T15:49:27.275551+08:00",
                codex_rc=0,
            )

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["no_download_reason"], "no_window_updates")
            self.assertEqual(result["reason_code"], "window_has_no_updates")
            self.assertEqual(result["reason_text"], "本时间窗口内没有新更新")

    def test_build_canonical_result_does_not_take_scan_alert_from_prompt_example(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            state_path = tmp_path / "state.json"
            raw_output_path = tmp_path / "raw.log"
            state_path.write_text("{}\n", encoding="utf-8")
            raw_output_path.write_text(
                "Do not wrap `ZSXQ_REPORT_JSON:` or `ZSXQ_SCAN_ALERT:` in backticks.\n"
                "`ZSXQ_SCAN_ALERT: api_unavailable_dom_fallback`\n"
                'ZSXQ_REPORT_JSON:{"window_new_docs_count":0,"keyword_matched_docs_count":0,'
                '"download_candidate_count":0,"download_success_count":0,'
                '"no_download_reason":"no_window_updates","core_reason":"window_has_no_updates",'
                '"scan_mode":"api_first","api_probe_status":"ok"}\n',
                encoding="utf-8",
            )

            result = build_canonical_result(
                state_path=state_path,
                raw_output_path=raw_output_path,
                run_started_at="2026-04-14T20:00:00+08:00",
                run_finished_at="2026-04-14T20:02:01+08:00",
                requested_window_start="",
                requested_window_end="",
                pre_last_successful_check_at="2026-04-14T16:13:42.956157+08:00",
                codex_rc=0,
            )

            self.assertIsNone(result["scan_alert"])
            self.assertEqual(result["api_probe_status"], "ok")

    def test_manifest_truth_prevents_agent_overreport_from_turning_partial_run_green(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            run_id = "33333333-3333-4333-8333-333333333333"
            state_path = tmp_path / "state.json"
            state_path.write_text(json.dumps({"last_successful_check_at": "2026-07-14T08:00:00+08:00"}), encoding="utf-8")
            raw_path = tmp_path / "raw.log"
            raw_path.write_text(
                'ZSXQ_REPORT_JSON:{"download_candidate_count":4,"download_success_count":4,'
                '"no_download_reason":"downloaded","core_reason":"downloads_completed"}\n',
                encoding="utf-8",
            )
            candidates = [
                {"file_id": f"file-{index}", "filename": f"报告{index}.pdf"}
                for index in range(1, 5)
            ]
            plan_path = tmp_path / "plan.json"
            plan = {
                "window_start": "2026-07-14T08:00:00+08:00",
                "window_end": "2026-07-14T12:00:00+08:00",
                "window_new_docs_count": 4,
                "keyword_matched_docs_count": 4,
                "download_candidate_count": 4,
                "download_candidates": candidates,
                "scan_mode": "api_first",
                "api_probe_status": "ok",
                "blocked_reason": None,
            }
            plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            manifest_path = tmp_path / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "scan_snapshot": plan,
                        "downloaded_count": 3,
                        "downloaded_entries": [
                            {"filename": f"报告{index}.pdf", "candidate_identity": f"file_id:file-{index}"}
                            for index in range(1, 4)
                        ],
                        "archive_dirs": ["/tmp/batch"],
                        "missing_candidates": [candidates[-1]],
                        "invariant_errors": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = build_canonical_result(
                state_path=state_path,
                raw_output_path=raw_path,
                run_started_at="2026-07-14T12:00:01+08:00",
                run_finished_at="2026-07-14T12:05:00+08:00",
                requested_window_start=plan["window_start"],
                requested_window_end=plan["window_end"],
                pre_last_successful_check_at=plan["window_start"],
                codex_rc=0,
                run_manifest_path=manifest_path,
                scan_plan_path=plan_path,
                run_id=run_id,
            )

            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["downloaded_count"], 3)
            self.assertEqual(result["download_success_count"], 3)
            self.assertEqual(result["reason_code"], "download_candidates_not_completed")
            self.assertFalse(result["state_updated"])

    def test_manifest_partial_preserves_known_content_protection_reason(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            run_id = "35353535-3535-4353-8353-353535353535"
            window_start = "2026-07-14T08:00:00+08:00"
            window_end = "2026-07-14T12:00:00+08:00"
            candidate = {
                "file_id": "protected-1",
                "filename": "受保护报告.pdf",
            }
            plan = {
                "window_start": window_start,
                "window_end": window_end,
                "window_new_docs_count": 1,
                "keyword_matched_docs_count": 1,
                "download_candidate_count": 1,
                "download_candidates": [candidate],
                "scan_mode": "api_first",
                "api_probe_status": "ok",
                "blocked_reason": None,
            }
            state_path = tmp_path / "state.json"
            state_path.write_text(
                json.dumps({"last_successful_check_at": window_start}),
                encoding="utf-8",
            )
            raw_path = tmp_path / "raw.log"
            raw_path.write_text(
                'ZSXQ_REPORT_JSON:{"download_candidate_count":1,'
                '"download_success_count":0,"no_download_reason":"source_content_protected",'
                '"core_reason":"all_candidates_source_content_protected"}\n',
                encoding="utf-8",
            )
            plan_path = tmp_path / "plan.json"
            plan_path.write_text(
                json.dumps(plan, ensure_ascii=False),
                encoding="utf-8",
            )
            manifest_path = tmp_path / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "scan_snapshot": plan,
                        "downloaded_count": 0,
                        "downloaded_entries": [],
                        "satisfied_count": 0,
                        "satisfied_entries": [],
                        "archive_dirs": [],
                        "missing_candidates": [candidate],
                        "invariant_errors": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = build_canonical_result(
                state_path=state_path,
                raw_output_path=raw_path,
                run_started_at="2026-07-14T12:00:01+08:00",
                run_finished_at="2026-07-14T12:05:00+08:00",
                requested_window_start=window_start,
                requested_window_end=window_end,
                pre_last_successful_check_at=window_start,
                codex_rc=0,
                run_manifest_path=manifest_path,
                scan_plan_path=plan_path,
                run_id=run_id,
            )

            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["reason_code"], "source_content_protected")
            self.assertEqual(result["no_download_reason"], "source_content_protected")
            self.assertIn("内容保护", result["reason_text"])

    def test_manifest_truth_uses_aggregate_and_frozen_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            run_id = "44444444-4444-4444-8444-444444444444"
            window_start = "2026-07-14T08:00:00+08:00"
            window_end = "2026-07-14T12:00:00+08:00"
            candidates = [{"file_id": f"f-{i}", "filename": f"报告{i}.pdf"} for i in range(1, 5)]
            plan = {
                "window_start": window_start,
                "window_end": window_end,
                "window_new_docs_count": 4,
                "keyword_matched_docs_count": 4,
                "download_candidate_count": 4,
                "download_candidates": candidates,
                "scan_mode": "api_first",
                "api_probe_status": "ok",
                "blocked_reason": None,
            }
            state_path = tmp_path / "state.json"
            state_path.write_text(json.dumps({"last_successful_check_at": window_end, "last_run_id": run_id}), encoding="utf-8")
            raw_path = tmp_path / "raw.log"
            raw_path.write_text("", encoding="utf-8")
            plan_path = tmp_path / "plan.json"
            plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            manifest_path = tmp_path / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "scan_snapshot": plan,
                        "downloaded_count": 4,
                        "downloaded_entries": [
                            {"filename": row["filename"], "candidate_identity": f"file_id:{row['file_id']}"}
                            for row in candidates
                        ],
                        "archive_dirs": ["/tmp/batch-a", "/tmp/batch-b"],
                        "missing_candidates": [],
                        "invariant_errors": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = build_canonical_result(
                state_path=state_path, raw_output_path=raw_path,
                run_started_at="2026-07-14T12:00:01+08:00", run_finished_at="2026-07-14T12:05:00+08:00",
                requested_window_start=window_start, requested_window_end=window_end,
                pre_last_successful_check_at=window_start, codex_rc=0,
                run_manifest_path=manifest_path, scan_plan_path=plan_path, run_id=run_id,
            )

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["downloaded_count"], 4)
            self.assertEqual(result["archive_dirs"], ["/tmp/batch-a", "/tmp/batch-b"])
            self.assertEqual(result["effective_window_end"], window_end)
            self.assertTrue(result["invariants_ok"])

    def test_manifest_content_duplicate_is_reconciled_without_inflating_new_downloads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            run_id = "55555555-5555-4555-8555-555555555555"
            window_start = "2026-07-14T08:00:00+08:00"
            window_end = "2026-07-14T12:00:00+08:00"
            candidate = {"file_id": "f-1", "filename": "完整长标题.pdf"}
            plan = {
                "window_start": window_start,
                "window_end": window_end,
                "window_new_docs_count": 1,
                "keyword_matched_docs_count": 1,
                "download_candidate_count": 1,
                "download_candidates": [candidate],
                "scan_mode": "api_first",
                "api_probe_status": "ok",
                "blocked_reason": None,
            }
            state_path = tmp_path / "state.json"
            state_path.write_text(
                json.dumps({"last_successful_check_at": window_end, "last_run_id": run_id}),
                encoding="utf-8",
            )
            raw_path = tmp_path / "raw.log"
            raw_path.write_text("", encoding="utf-8")
            plan_path = tmp_path / "plan.json"
            plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            manifest_path = tmp_path / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "run_id": run_id,
                        "scan_snapshot": plan,
                        "downloaded_count": 0,
                        "downloaded_entries": [],
                        "satisfied_count": 1,
                        "satisfied_entries": [
                            {
                                "filename": candidate["filename"],
                                "candidate_identity": "file_id:f-1",
                                "disposition": "already_archived_content_duplicate",
                            }
                        ],
                        "archive_dirs": [],
                        "missing_candidates": [],
                        "invariant_errors": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = build_canonical_result(
                state_path=state_path,
                raw_output_path=raw_path,
                run_started_at="2026-07-14T12:00:01+08:00",
                run_finished_at="2026-07-14T12:05:00+08:00",
                requested_window_start=window_start,
                requested_window_end=window_end,
                pre_last_successful_check_at=window_start,
                codex_rc=0,
                run_manifest_path=manifest_path,
                scan_plan_path=plan_path,
                run_id=run_id,
            )

            self.assertEqual(result["status"], "success")
            self.assertEqual(result["downloaded_count"], 0)
            self.assertEqual(result["download_success_count"], 1)
            self.assertEqual(result["satisfied_candidate_count"], 1)
            self.assertEqual(result["no_download_reason"], "already_archived_duplicates")
            self.assertTrue(result["invariants_ok"])


if __name__ == "__main__":
    unittest.main()
