import importlib.util
import tempfile
from pathlib import Path
import unittest
from unittest import mock


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "download_zsxq_plan_file.py"
)
SPEC = importlib.util.spec_from_file_location("download_zsxq_plan_file", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class FakeLocator:
    def __init__(self, page: "FakePage", text: str) -> None:
        self.page = page
        self.text = text

    def count(self) -> int:
        return 1

    def nth(self, _index: int) -> "FakeLocator":
        return self

    def is_visible(self) -> bool:
        return self.page.is_text_visible(self.text)


class FakeBody:
    def __init__(self, page: "FakePage") -> None:
        self.page = page

    def inner_text(self, timeout: int) -> str:
        del timeout
        return self.page.body_text()


class FakePage:
    def __init__(self, clock: FakeClock, transition_at: float | None) -> None:
        self.clock = clock
        self.transition_at = transition_at

    def get_by_text(self, text: str, exact: bool) -> FakeLocator:
        assert exact
        return FakeLocator(self, text)

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.clock.now += timeout_ms / 1000

    def body_text(self) -> str:
        if self.transition_at is None or self.clock.now < self.transition_at:
            return MODULE.CONTENT_PROTECTION_MARKER
        return "本文档受知识星球分享保护 下载文件"

    def is_text_visible(self, text: str) -> bool:
        return (
            self.transition_at is not None
            and self.clock.now >= self.transition_at
            and text == "下载文件"
        )


class RetryNavigationPage:
    def __init__(self) -> None:
        self.goto_calls = 0
        self.wait_calls = 0

    def goto(self, *_args, **_kwargs) -> None:
        self.goto_calls += 1
        if self.goto_calls == 1:
            raise RuntimeError("net::ERR_CONNECTION_CLOSED")

    def wait_for_timeout(self, _timeout_ms: int) -> None:
        self.wait_calls += 1


class DownloadZsxqPlanFileTests(unittest.TestCase):
    def test_select_candidate_requires_exact_plan_membership(self) -> None:
        plan = {
            "download_candidates": [
                {
                    "file_id": 123,
                    "filename": "planned.pdf",
                    "topic_url": "https://example.invalid/topic/1",
                }
            ]
        }
        row = MODULE.select_candidate(plan, file_id="123")
        self.assertEqual(row["filename"], "planned.pdf")
        with self.assertRaisesRegex(ValueError, "exactly one planned candidate"):
            MODULE.select_candidate(plan, file_id="999")

    def test_choose_staging_prefers_task_specific_extra_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            selected = MODULE.choose_staging_dir(
                {
                    "download_settings": {
                        "staging_dir": str(root / "downloads"),
                        "extra_staging_dirs": [str(root / "task-staging")],
                    }
                }
            )
            self.assertEqual(selected, (root / "task-staging").resolve())

    def test_unwrap_scan_plan_accepts_run_manifest_snapshot(self) -> None:
        snapshot = {"download_candidates": [{"file_id": 1}]}
        self.assertIs(
            MODULE.unwrap_scan_plan({"scan_snapshot": snapshot}),
            snapshot,
        )

    def test_validate_pdf_rejects_non_pdf_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.pdf"
            path.write_bytes(b"x" * 2048)
            with self.assertRaisesRegex(RuntimeError, "not a PDF"):
                MODULE.validate_pdf(path)

    def test_page_state_markers_are_distinct(self) -> None:
        self.assertNotIn(
            MODULE.GROUP_MARKER,
            MODULE.LOGIN_MARKERS,
        )

    def test_transient_content_protection_yields_to_download_control(self) -> None:
        clock = FakeClock()
        page = FakePage(clock, transition_at=2.0)
        with mock.patch.object(
            MODULE.time,
            "monotonic",
            side_effect=clock.monotonic,
        ):
            state, control = MODULE.wait_for_file_detail_state(
                page,
                FakeBody(page),
                timeout_ms=10000,
                protection_confirmation_ms=3000,
            )

        self.assertEqual(state, "downloadable")
        self.assertIsNotNone(control)
        self.assertEqual(control.text, "下载文件")
        self.assertGreaterEqual(clock.now, 2.0)
        self.assertLess(clock.now, 3.0)

    def test_persistent_content_protection_is_confirmed_after_grace_period(self) -> None:
        clock = FakeClock()
        page = FakePage(clock, transition_at=None)
        with mock.patch.object(
            MODULE.time,
            "monotonic",
            side_effect=clock.monotonic,
        ):
            state, control = MODULE.wait_for_file_detail_state(
                page,
                FakeBody(page),
                timeout_ms=10000,
                protection_confirmation_ms=3000,
            )

        self.assertEqual(state, "protected")
        self.assertIsNone(control)
        self.assertGreaterEqual(clock.now, 3.0)

    def test_download_control_texts_prefer_current_label(self) -> None:
        clock = FakeClock()
        page = FakePage(clock, transition_at=0.0)
        control = MODULE.current_download_control(page)

        self.assertIsNotNone(control)
        self.assertEqual(control.text, "下载文件")

    def test_candidate_navigation_retries_transient_connection_close(self) -> None:
        page = RetryNavigationPage()
        unavailable = {
            "state": "unavailable",
            "signals": ["browser_error_page"],
            "url": "https://wx.zsxq.com/group/12345678901234",
            "title": "This site can’t be reached",
        }
        ready = {
            "state": "ready",
            "signals": ["group_name_in_title"],
            "url": "https://wx.zsxq.com/group/12345678901234/topic/1",
            "title": "前沿信息收录-知识星球",
        }
        with mock.patch.object(
            MODULE,
            "wait_for_zsxq_page_state",
            side_effect=[unavailable, ready],
        ):
            state, navigation_error = MODULE.navigate_to_candidate_page(
                page,
                topic_url="https://wx.zsxq.com/group/12345678901234/topic/1",
                group_url="https://wx.zsxq.com/group/12345678901234",
                group_name="前沿信息收录",
                tag_name="外资研报",
                timeout_ms=20000,
                navigation_attempts=3,
            )

        self.assertEqual(state["state"], "ready")
        self.assertEqual(navigation_error, "")
        self.assertEqual(page.goto_calls, 2)
        self.assertEqual(page.wait_calls, 1)


if __name__ == "__main__":
    unittest.main()
