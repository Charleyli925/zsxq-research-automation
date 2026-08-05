import importlib.util
from pathlib import Path
import unittest
from unittest import mock


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "zsxq_page_state.py"
)
SPEC = importlib.util.spec_from_file_location("zsxq_page_state", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


GROUP_URL = "https://wx.zsxq.com/group/12345678901234"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class FakeBody:
    def __init__(self, page: "TransitionPage") -> None:
        self.page = page

    def inner_text(self, timeout: int) -> str:
        del timeout
        return self.page.body_text


class TransitionPage:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.url = GROUP_URL

    @property
    def body_text(self) -> str:
        return "" if self.clock.now < 0.5 else "最新 外资研报 国内研报"

    def title(self) -> str:
        return "加载中" if self.clock.now < 0.5 else "前沿信息收录-知识星球"

    def locator(self, selector: str) -> FakeBody:
        self.assert_body_selector(selector)
        return FakeBody(self)

    def assert_body_selector(self, selector: str) -> None:
        if selector != "body":
            raise AssertionError(selector)

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.clock.now += timeout_ms / 1000


class ZsxqPageStateTests(unittest.TestCase):
    def test_title_signal_accepts_group_page_when_body_omits_group_name(self) -> None:
        result = MODULE.classify_zsxq_page_state(
            url=GROUP_URL,
            title="前沿信息收录-知识星球",
            body_text="最新 外资研报 国内研报",
            target_url=GROUP_URL,
            group_name="前沿信息收录",
            tag_name="外资研报",
        )

        self.assertEqual(result["state"], "ready")
        self.assertIn("group_name_in_title", result["signals"])

    def test_group_navigation_is_a_ready_signal_without_exact_group_marker(self) -> None:
        result = MODULE.classify_zsxq_page_state(
            url=GROUP_URL,
            title="知识星球",
            body_text="最新 外资研报 国内研报",
            target_url=GROUP_URL,
            group_name="前沿信息收录",
        )

        self.assertEqual(result["state"], "ready")
        self.assertIn("target_group_navigation", result["signals"])

    def test_target_url_does_not_make_browser_error_page_ready(self) -> None:
        result = MODULE.classify_zsxq_page_state(
            url=GROUP_URL,
            title="This site can’t be reached",
            body_text="ERR_CONNECTION_CLOSED",
            target_url=GROUP_URL,
            group_name="前沿信息收录",
        )

        self.assertEqual(result["state"], "unavailable")

    def test_visible_login_marker_takes_priority_over_stale_title(self) -> None:
        result = MODULE.classify_zsxq_page_state(
            url=GROUP_URL,
            title="前沿信息收录-知识星球",
            body_text="请使用微信扫码登录",
            target_url=GROUP_URL,
            group_name="前沿信息收录",
        )

        self.assertEqual(result["state"], "login")

    def test_unrelated_zsxq_page_is_not_accepted(self) -> None:
        result = MODULE.classify_zsxq_page_state(
            url="https://wx.zsxq.com/dweb2/index/group",
            title="知识星球",
            body_text="我的星球",
            target_url=GROUP_URL,
            group_name="前沿信息收录",
        )

        self.assertEqual(result["state"], "unrecognized")

    def test_wait_polls_until_spa_exposes_ready_signals(self) -> None:
        clock = FakeClock()
        page = TransitionPage(clock)
        with mock.patch.object(
            MODULE.time,
            "monotonic",
            side_effect=clock.monotonic,
        ):
            result = MODULE.wait_for_zsxq_page_state(
                page,
                target_url=GROUP_URL,
                group_name="前沿信息收录",
                timeout_ms=2000,
            )

        self.assertEqual(result["state"], "ready")
        self.assertGreaterEqual(clock.now, 0.5)


if __name__ == "__main__":
    unittest.main()
