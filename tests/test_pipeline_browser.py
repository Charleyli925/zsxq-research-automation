from __future__ import annotations

import os
from pathlib import Path

import pytest

from zsxq_pipeline.browser import BrowserSession, BrowserSessionError, CftLaunchOptions


class FakePage:
    def __init__(self, *, body: str = "已登录 前沿信息收录") -> None:
        self.body = body
        self.closed = False
        self.visited: list[str] = []

    def goto(self, url: str, **_kwargs) -> None:
        self.visited.append(url)

    def locator(self, _selector: str):
        page = self

        class Locator:
            def inner_text(self, **_kwargs) -> str:
                return page.body

        return Locator()

    def title(self) -> str:
        return "知识星球"

    def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self, page: FakePage) -> None:
        self.page = page

    def new_page(self) -> FakePage:
        return self.page


class FakeManager:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.exited = False
        self.chromium = self

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        self.exited = True

    def connect_over_cdp(self, endpoint: str, *, timeout: int):
        assert endpoint == "http://127.0.0.1:9223"
        assert timeout == 45_000

        class Browser:
            contexts = [FakeContext(self.page)]

        return Browser()


def test_session_lends_one_page_and_disconnects_without_closing_the_browser():
    page = FakePage()
    manager = FakeManager(page)
    with BrowserSession("http://127.0.0.1:9223", playwright_factory=lambda: manager) as session:
        assert session.page is page
        result = session.doctor(start_url="https://wx.zsxq.com/group/fixture", group_name="前沿信息收录")
        assert result.ok is True
        assert result.code == "ok"

    assert page.closed is True
    assert manager.exited is True


def test_doctor_reports_login_without_downloading_or_mutating_profile():
    page = FakePage(body="请扫码登录")
    manager = FakeManager(page)
    with BrowserSession("http://127.0.0.1:9223", playwright_factory=lambda: manager) as session:
        result = session.doctor(start_url="https://wx.zsxq.com/group/fixture")

    assert result.ok is False
    assert result.code == "need_reauth"
    assert page.visited == ["https://wx.zsxq.com/group/fixture"]


def test_cft_startup_removes_only_a_confirmed_dead_singleton_owner(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    dead_pid = 999_999_999
    (profile / "SingletonLock").symlink_to(f"host-{dead_pid}")
    (profile / "SingletonCookie").write_text("stale", encoding="utf-8")
    (profile / "SingletonSocket").write_text("stale", encoding="utf-8")

    assert BrowserSession._remove_confirmed_dead_singletons(profile) is True
    assert not any((profile / name).exists() or (profile / name).is_symlink() for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"))

    (profile / "SingletonLock").symlink_to(f"host-{os.getpid()}")
    assert BrowserSession._remove_confirmed_dead_singletons(profile) is False
    assert (profile / "SingletonLock").is_symlink()


def test_cft_startup_requires_an_executable_only_when_cdp_is_unavailable(tmp_path, monkeypatch):
    options = CftLaunchOptions(
        executable_path=tmp_path / "missing-cft",
        user_data_dir=tmp_path / "profile",
    )
    session = BrowserSession("http://127.0.0.1:9223", cft_launch_options=options)
    monkeypatch.setattr(session, "_cdp_ready", lambda: False)

    with pytest.raises(BrowserSessionError, match="blocked_browser_missing"):
        session._ensure_cft_ready()
