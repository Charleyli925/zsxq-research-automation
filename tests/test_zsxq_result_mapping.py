from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.zsxq_result_mapping import (
    REASON_TEXT_BY_CODE,
    classify_report_status,
    core_reason_from_no_download_reason,
    no_download_reason_from_core_reason,
    normalize_no_download_reason,
    normalize_reason_code,
)


class ZsxqResultMappingTests(unittest.TestCase):
    def test_no_window_updates_maps_to_structured_reason(self) -> None:
        self.assertEqual(
            core_reason_from_no_download_reason("no_window_updates"),
            "window_has_no_updates",
        )
        self.assertEqual(
            REASON_TEXT_BY_CODE["window_has_no_updates"],
            "本时间窗口内没有新更新",
        )

    def test_no_window_updates_is_success_not_partial(self) -> None:
        report_status = classify_report_status(
            codex_rc=0,
            downloaded_count=0,
            no_download_reason="no_window_updates",
            core_reason="window_has_no_updates",
            scan_alert="",
        )
        self.assertEqual(report_status, "success")

    def test_legacy_no_new_docs_aliases_are_normalized(self) -> None:
        self.assertEqual(normalize_no_download_reason("no_new_docs"), "no_new_documents")
        self.assertEqual(normalize_no_download_reason("no_window_new_docs"), "no_new_documents")
        self.assertEqual(normalize_reason_code("window_no_updates"), "window_has_no_updates")
        self.assertEqual(normalize_reason_code("download_success"), "download_completed")
        self.assertEqual(normalize_reason_code("downloads_completed"), "download_completed")
        self.assertEqual(
            normalize_reason_code("source_web_download_protected"),
            "source_content_protected",
        )
        self.assertEqual(
            normalize_reason_code("all_candidates_source_content_protected"),
            "source_content_protected",
        )
        self.assertEqual(
            normalize_reason_code("scan_api_ok_no_download_candidates"),
            "window_has_no_new_documents",
        )
        self.assertEqual(
            core_reason_from_no_download_reason("no_new_docs"),
            "window_has_no_new_documents",
        )
        self.assertEqual(
            no_download_reason_from_core_reason("window_no_updates"),
            "no_window_updates",
        )

    def test_preflight_failure_codes_have_distinct_user_facing_reasons(self) -> None:
        self.assertIn("9223", REASON_TEXT_BY_CODE["blocked_browser_endpoint_unavailable"])
        self.assertIn("Playwright", REASON_TEXT_BY_CODE["blocked_browser_cdp_unresponsive"])
        self.assertIn("知识星球页面加载失败", REASON_TEXT_BY_CODE["zsxq_page_unavailable"])
        self.assertIn("内容保护", REASON_TEXT_BY_CODE["source_content_protected"])
        self.assertEqual(
            no_download_reason_from_core_reason("blocked_browser_cdp_unresponsive"),
            "blocked_browser",
        )


if __name__ == "__main__":
    unittest.main()
