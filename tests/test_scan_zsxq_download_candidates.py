"""
这个文件测试候选扫描脚本里的纯规则部分。

它不连浏览器，只验证：
- topic URL 怎么拼
- 标题命中后会不会进入下载候选
- 已归档文件会不会被跳过
"""

from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "scan_zsxq_download_candidates.py"
sys.path.insert(0, str(SCRIPT_PATH.parent))
SPEC = importlib.util.spec_from_file_location("scan_zsxq_download_candidates", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ScanZsxqDownloadCandidatesTests(unittest.TestCase):
    def test_build_topic_url(self) -> None:
        url = MODULE.build_topic_url(
            "https://wx.zsxq.com/group/12345678901234",
            "98765432109876",
        )
        self.assertEqual(url, "https://wx.zsxq.com/group/12345678901234/topic/98765432109876")

    def test_filter_topics_splits_downloads_and_duplicates(self) -> None:
        topics = [
            {
                "topic_id": "1001",
                "create_time": "2026-03-19T15:00:00+08:00",
                "title": "#外资研报",
                "talk": {
                    "files": [
                        {"file_id": 1, "name": "腾讯深度报告.pdf", "create_time": "2026-03-19T15:00:00+08:00"},
                        {"file_id": 2, "name": "普通市场简报.pdf", "create_time": "2026-03-19T15:00:00+08:00"},
                        {"file_id": 3, "name": "谷歌产业观察.pdf", "create_time": "2026-03-19T15:00:00+08:00"},
                    ]
                },
            }
        ]
        keyword_payload = {
            "schema_version": 2,
            "standalone_keywords": ["腾讯", "谷歌"],
            "region_keywords": [],
            "region_required_keywords": [],
            "exclude_keywords": [],
        }

        result = MODULE.filter_topics(
            topics,
            window_start=datetime.fromisoformat("2026-03-19T12:20:28+08:00"),
            window_end=datetime.fromisoformat("2026-03-19T16:05:12+08:00"),
            keyword_payload=keyword_payload,
            archived_names={"谷歌产业观察.pdf"},
            group_url="https://wx.zsxq.com/group/12345678901234",
        )

        self.assertEqual(len(result["topics"]), 1)
        self.assertEqual(len(result["matched_topics"]), 1)
        self.assertEqual(len(result["download_candidates"]), 1)
        self.assertEqual(result["download_candidates"][0]["filename"], "腾讯深度报告.pdf")
        self.assertEqual(result["download_candidates"][0]["match_rule"], "standalone")
        self.assertEqual(len(result["skipped_duplicates"]), 1)
        self.assertEqual(result["skipped_duplicates"][0]["filename"], "谷歌产业观察.pdf")

    def test_filter_topics_deduplicates_repeated_api_boundary_topic(self) -> None:
        topic = {
            "topic_id": "1001",
            "create_time": "2026-07-14T10:24:08+08:00",
            "title": "#国内研报",
            "talk": {
                "files": [
                    {
                        "file_id": 181282255554542,
                        "name": "国盛证券-基本面高频数据跟踪猪价有所反弹.pdf",
                        "create_time": "2026-07-14T10:24:08+08:00",
                    }
                ]
            },
        }
        keyword_payload = {
            "schema_version": 2,
            "standalone_keywords": ["猪价"],
            "region_keywords": [],
            "region_required_keywords": [],
            "exclude_keywords": [],
        }

        result = MODULE.filter_topics(
            [topic, dict(topic)],
            window_start=datetime.fromisoformat("2026-07-12T15:57:07+08:00"),
            window_end=datetime.fromisoformat("2026-07-15T15:57:07+08:00"),
            keyword_payload=keyword_payload,
            archived_names=set(),
            group_url="https://wx.zsxq.com/group/12345678901234",
        )

        self.assertEqual(len(result["topics"]), 1)
        self.assertEqual(len(result["matched_topics"]), 1)
        self.assertEqual(len(result["download_candidates"]), 1)
        self.assertEqual(result["download_candidates"][0]["file_id"], 181282255554542)

    def test_filter_topics_deduplicates_same_filename_across_topics(self) -> None:
        topics = [
            {
                "topic_id": "newer-topic",
                "create_time": "2026-07-26T10:35:17+08:00",
                "title": "#外资研报",
                "talk": {
                    "files": [
                        {
                            "file_id": 181285882858542,
                            "name": "高盛-互联网行业深度报告.pdf",
                        }
                    ]
                },
            },
            {
                "topic_id": "older-topic",
                "create_time": "2026-07-25T11:07:14+08:00",
                "title": "#外资研报",
                "talk": {
                    "files": [
                        {
                            "file_id": 181285141554222,
                            "name": "高盛-互联网行业深度报告.pdf",
                        }
                    ]
                },
            },
        ]
        keyword_payload = {
            "schema_version": 2,
            "standalone_keywords": ["互联网"],
            "region_keywords": [],
            "region_required_keywords": [],
            "exclude_keywords": [],
        }

        result = MODULE.filter_topics(
            topics,
            window_start=datetime.fromisoformat("2026-07-25T00:00:00+08:00"),
            window_end=datetime.fromisoformat("2026-07-27T00:00:00+08:00"),
            keyword_payload=keyword_payload,
            archived_names=set(),
            group_url="https://wx.zsxq.com/group/12345678901234",
        )

        self.assertEqual(len(result["download_candidates"]), 1)
        self.assertEqual(
            result["download_candidates"][0]["file_id"],
            181285882858542,
        )
        self.assertEqual(len(result["skipped_duplicates"]), 1)
        self.assertEqual(
            result["skipped_duplicates"][0]["duplicate_reason"],
            "same_window_filename",
        )
        self.assertEqual(
            result["skipped_duplicates"][0]["duplicate_of_file_id"],
            181285882858542,
        )

    def test_filter_topics_skips_archived_sanitized_download_name(self) -> None:
        topics = [
            {
                "topic_id": "1001",
                "create_time": "2026-05-06T09:58:15+08:00",
                "title": "#外资研报",
                "talk": {
                    "files": [
                        {
                            "file_id": 1,
                            "name": "260506-GS-AMD（Advanced Micro Devices Inc.）：人工智能服务器.pdf",
                            "create_time": "2026-05-06T09:58:15+08:00",
                        },
                    ]
                },
            }
        ]
        keyword_payload = {
            "schema_version": 2,
            "standalone_keywords": ["人工智能", "服务器"],
            "region_keywords": [],
            "region_required_keywords": [],
            "exclude_keywords": [],
        }

        result = MODULE.filter_topics(
            topics,
            window_start=datetime.fromisoformat("2026-05-06T09:00:00+08:00"),
            window_end=datetime.fromisoformat("2026-05-06T10:00:00+08:00"),
            keyword_payload=keyword_payload,
            archived_names={"260506-GS-AMD（Advanced-Micro-Devices-Inc-）：人工智能服务器.pdf"},
            group_url="https://wx.zsxq.com/group/12345678901234",
        )

        self.assertEqual(len(result["download_candidates"]), 0)
        self.assertEqual(len(result["skipped_duplicates"]), 1)

    def test_parse_topics_api_payload_rejects_business_error(self) -> None:
        error, topics = MODULE.parse_topics_api_payload(
            {
                "ok": True,
                "status": 200,
                "text": (
                    '{"succeeded":false,"code":1059,"error":"内部错误",'
                    '"resp_data":{"topics":[]}}'
                ),
            }
        )

        self.assertEqual(error, "api_error_1059")
        self.assertEqual(topics, [])

    def test_tag_navigation_retries_transient_connection_close(self) -> None:
        class RetryPage:
            def __init__(self) -> None:
                self.goto_calls = 0
                self.wait_calls = 0

            def goto(self, *_args, **_kwargs) -> None:
                self.goto_calls += 1
                if self.goto_calls == 1:
                    raise RuntimeError("net::ERR_CONNECTION_CLOSED")

            def wait_for_timeout(self, _timeout_ms: int) -> None:
                self.wait_calls += 1

        page = RetryPage()
        with mock.patch.object(
            MODULE,
            "wait_for_zsxq_page_state",
            side_effect=[
                {"state": "unavailable"},
                {"state": "ready"},
            ],
        ):
            reason = MODULE.navigate_tag_page(
                page,
                tag_url="https://wx.zsxq.com/tags/foo/123",
                group_url="https://wx.zsxq.com/group/12345678901234",
                group_name="前沿信息收录",
                tag_name="外资研报",
            )

        self.assertIsNone(reason)
        self.assertEqual(page.goto_calls, 2)
        self.assertEqual(page.wait_calls, 1)

    def test_fetch_topics_retries_retryable_business_error(self) -> None:
        good_topic = {
            "topic_id": "1001",
            "create_time": "2026-04-13T09:20:19.030+08:00",
            "talk": {"files": []},
        }
        payloads = [
            {
                "ok": True,
                "status": 200,
                "text": '{"succeeded":false,"code":1059,"error":"内部错误","resp_data":{"topics":[]}}',
            },
            {
                "ok": True,
                "status": 200,
                "text": '{"succeeded":false,"code":1059,"error":"内部错误","resp_data":{"topics":[]}}',
            },
            {
                "ok": True,
                "status": 200,
                "text": '{"succeeded":false,"code":1059,"error":"内部错误","resp_data":{"topics":[]}}',
            },
            {
                "ok": True,
                "status": 200,
                "text": '{"succeeded":false,"code":1059,"error":"内部错误","resp_data":{"topics":[]}}',
            },
            {
                "ok": True,
                "status": 200,
                "text": (
                    '{"succeeded":true,"resp_data":{"topics":['
                    '{"topic_id":"1001","create_time":"2026-04-13T09:20:19.030+08:00","talk":{"files":[]}}'
                    ']}}'
                ),
            },
            {
                "ok": True,
                "status": 200,
                "text": '{"succeeded":true,"resp_data":{"topics":[]}}',
            },
        ]

        class FakePage:
            def __init__(self, payload_list: list[dict[str, object]]) -> None:
                self._payloads = list(payload_list)
                self.url = "https://wx.zsxq.com/tags/foo/123"

            def goto(self, *_args, **_kwargs) -> None:
                return None

            def title(self) -> str:
                return "前沿信息收录-知识星球"

            def wait_for_timeout(self, _timeout_ms: int) -> None:
                return None

            def locator(self, _selector: str):
                class FakeLocator:
                    def inner_text(self, timeout: int) -> str:
                        return "已登录"

                return FakeLocator()

            def evaluate(self, _script: str, _url: str) -> dict[str, object]:
                return self._payloads.pop(0)

        class FakeContext:
            def __init__(self, payload_list: list[dict[str, object]]) -> None:
                self.pages = [FakePage(payload_list)]

            def new_page(self):
                return self.pages[0]

        class FakeBrowser:
            def __init__(self, payload_list: list[dict[str, object]]) -> None:
                self.contexts = [FakeContext(payload_list)]

        class FakeChromium:
            def __init__(self, payload_list: list[dict[str, object]]) -> None:
                self._payloads = payload_list

            def connect_over_cdp(self, *_args, **_kwargs):
                return FakeBrowser(self._payloads)

        class FakePlaywright:
            def __init__(self, payload_list: list[dict[str, object]]) -> None:
                self.chromium = FakeChromium(payload_list)

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:
                return False

        with (
            mock.patch.object(MODULE, "sync_playwright", return_value=FakePlaywright(payloads)),
            mock.patch.object(MODULE.time, "sleep", return_value=None),
        ):
            blocked_reason, topics = MODULE.fetch_topics_from_browser(
                cdp_endpoint="http://127.0.0.1:9223",
                tag_url="https://wx.zsxq.com/tags/foo/123",
                topics_api_url="https://api.zsxq.com/v2/hashtags/123/topics?count=20",
                window_start=datetime.fromisoformat("2026-04-13T08:00:00+08:00"),
            )

        self.assertIsNone(blocked_reason)
        self.assertEqual(topics, [good_topic])


if __name__ == "__main__":
    unittest.main()
