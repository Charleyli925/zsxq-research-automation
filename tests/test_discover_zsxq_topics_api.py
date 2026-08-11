from __future__ import annotations

import importlib.util
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "discover_zsxq_topics_api.py"
SPEC = importlib.util.spec_from_file_location("discover_zsxq_topics_api", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakePage:
    def __init__(self) -> None:
        self.closed = False
        self.mouse = SimpleNamespace(wheel=lambda *_args: None)

    def on(self, *_args) -> None:
        return None

    def goto(self, *_args, **_kwargs) -> None:
        return None

    def wait_for_timeout(self, *_args) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class FakePlaywright:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.chromium = self
        self.contexts = [self]

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        assert self.page.closed is True
        return None

    def connect_over_cdp(self, *_args, **_kwargs):
        return self

    def new_page(self) -> FakePage:
        return self.page


def test_diagnostic_closes_its_caller_owned_page(monkeypatch):
    page = FakePage()
    playwright = FakePlaywright(page)
    monkeypatch.setattr(MODULE, "sync_playwright", lambda: playwright)
    monkeypatch.setattr(
        MODULE,
        "parse_args",
        lambda: Namespace(
            cdp_endpoint="http://127.0.0.1:9223",
            tag_url="https://wx.zsxq.com/tags/fixture",
            scroll_rounds=0,
            output=None,
        ),
    )

    assert MODULE.main() == 1
    assert page.closed is True
