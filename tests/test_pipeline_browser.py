from __future__ import annotations

import json
import os

import pytest

import zsxq_pipeline.browser as browser_module
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
    def __init__(self, page: FakePage, *, fail_new_page: bool = False) -> None:
        self.page = page
        self.fail_new_page = fail_new_page

    def new_page(self) -> FakePage:
        if self.fail_new_page:
            raise RuntimeError("Target.createTarget timed out")
        return self.page


class FakeManager:
    def __init__(self, page: FakePage, *, fail_new_page: bool = False) -> None:
        self.page = page
        self.fail_new_page = fail_new_page
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
            contexts = [FakeContext(self.page, fail_new_page=self.fail_new_page)]

        return Browser()


class FakeHttpResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


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


def test_repeated_sessions_close_every_caller_owned_page():
    pages: list[FakePage] = []
    managers: list[FakeManager] = []

    def factory() -> FakeManager:
        page = FakePage()
        manager = FakeManager(page)
        pages.append(page)
        managers.append(manager)
        return manager

    for _ in range(25):
        with BrowserSession("http://127.0.0.1:9223", playwright_factory=factory) as session:
            assert session.page.closed is False

    assert len(pages) == 25
    assert all(page.closed for page in pages)
    assert all(manager.exited for manager in managers)


def test_doctor_reports_login_without_downloading_or_mutating_profile():
    page = FakePage(body="请扫码登录")
    manager = FakeManager(page)
    with BrowserSession("http://127.0.0.1:9223", playwright_factory=lambda: manager) as session:
        result = session.doctor(start_url="https://wx.zsxq.com/group/fixture")

    assert result.ok is False
    assert result.code == "need_reauth"
    assert page.visited == ["https://wx.zsxq.com/group/fixture"]


def test_session_retries_a_transient_page_creation_failure_with_a_fresh_transport():
    first_page = FakePage()
    second_page = FakePage()
    first = FakeManager(first_page, fail_new_page=True)
    second = FakeManager(second_page)
    managers = iter((first, second))

    with BrowserSession(
        "http://127.0.0.1:9223",
        playwright_factory=lambda: next(managers),
        retry_delay_seconds=0,
    ) as session:
        assert session.page is second_page

    assert first.exited is True
    assert second.exited is True
    assert second_page.closed is True


def test_session_rechecks_full_cft_readiness_before_connection_retry(tmp_path, monkeypatch):
    first = FakeManager(FakePage(), fail_new_page=True)
    second = FakeManager(FakePage())
    managers = iter((first, second))
    options = CftLaunchOptions(
        executable_path=tmp_path / "cft",
        user_data_dir=tmp_path / "profile",
    )
    assert options.startup_timeout_seconds == 25.0
    session = BrowserSession(
        "http://127.0.0.1:9223",
        cft_launch_options=options,
        playwright_factory=lambda: next(managers),
        retry_delay_seconds=0,
    )
    readiness_checks: list[bool] = []
    monkeypatch.setattr(session, "_ensure_cft_ready", lambda: readiness_checks.append(True))

    with session:
        pass

    assert readiness_checks == [True, True]


def test_session_failure_preserves_the_exact_error_and_retry_diagnostics():
    managers = iter(
        (
            FakeManager(FakePage(), fail_new_page=True),
            FakeManager(FakePage(), fail_new_page=True),
        )
    )
    with pytest.raises(BrowserSessionError) as captured:
        with BrowserSession(
            "http://127.0.0.1:9223",
            playwright_factory=lambda: next(managers),
            retry_delay_seconds=0,
        ):
            pass

    assert captured.value.code == "blocked_browser_cdp_unresponsive"
    assert "Target.createTarget timed out" in captured.value.detail
    assert "connect_attempts=2" in captured.value.detail
    assert "preconnect[observed=0,owned=0,closed=0,failed=0]" in captured.value.detail


def test_dedicated_target_compaction_closes_only_surplus_owned_pages(tmp_path, monkeypatch):
    options = CftLaunchOptions(
        executable_path=tmp_path / "cft",
        user_data_dir=tmp_path / "profile",
        start_url="https://wx.zsxq.com/tags/foreign",
    )
    session = BrowserSession("http://127.0.0.1:9223", cft_launch_options=options)
    targets = [
        {"id": "topic-1", "type": "page", "url": "https://wx.zsxq.com/group/a/topic/1"},
        {"id": "foreign", "type": "page", "url": options.start_url},
        {"id": "blank", "type": "page", "url": "about:blank"},
        {"id": "topic-2", "type": "page", "url": "https://wx.zsxq.com/group/a/topic/2"},
        {"id": "other", "type": "page", "url": "https://example.com/private-draft"},
        {"id": "worker", "type": "service_worker", "url": "https://wx.zsxq.com/sw.js"},
    ]
    closed: list[str] = []
    monkeypatch.setattr(session, "_http_json", lambda _url: targets)
    monkeypatch.setattr(session, "_http_put", lambda url: closed.append(url) or b"")

    result = session._compact_owned_targets(limit=2)

    assert result.observed_pages == 5
    assert result.owned_pages == 4
    assert result.closed_pages == 2
    assert all("other" not in url and "worker" not in url and "foreign" not in url for url in closed)
    assert {url.rsplit("/", 1)[-1] for url in closed} == {"blank", "topic-2"}


def test_keepalive_creation_uses_the_supported_cdp_put_endpoint(tmp_path, monkeypatch):
    options = CftLaunchOptions(
        executable_path=tmp_path / "cft",
        user_data_dir=tmp_path / "profile",
        start_url="https://wx.zsxq.com/group/fixture",
    )
    session = BrowserSession("http://127.0.0.1:9223", cft_launch_options=options)
    requested: list[str] = []
    monkeypatch.setattr(session, "_http_json", lambda _url: [])
    monkeypatch.setattr(session, "_http_put", lambda url: requested.append(url) or b"")

    session._ensure_keepalive_page()

    assert requested == ["http://127.0.0.1:9223/json/new?https%3A%2F%2Fwx.zsxq.com%2Fgroup%2Ffixture"]


def test_local_cdp_http_never_uses_ambient_system_proxy(monkeypatch):
    calls: list[tuple[object, float]] = []

    class DirectOpener:
        def open(self, request, *, timeout: float):
            calls.append((request, timeout))
            if isinstance(request, str):
                return FakeHttpResponse(json.dumps({"webSocketDebuggerUrl": "ws://direct"}).encode())
            return FakeHttpResponse(b"Target is closing")

    monkeypatch.setattr(browser_module, "_DIRECT_CDP_OPENER", DirectOpener())
    monkeypatch.setattr(
        browser_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("ambient urlopen must not be used")),
    )

    payload = BrowserSession._http_json("http://127.0.0.1:9223/json/version")
    closed = BrowserSession._http_put("http://127.0.0.1:9223/json/close/fixture")

    assert payload["webSocketDebuggerUrl"] == "ws://direct"
    assert closed == b"Target is closing"
    assert calls[0] == ("http://127.0.0.1:9223/json/version", 1.5)
    assert calls[1][0].method == "PUT"


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


def test_headed_cft_launches_as_a_background_macos_window(tmp_path, monkeypatch):
    executable = tmp_path / "Google Chrome for Testing.app" / "Contents" / "MacOS" / "Google Chrome for Testing"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture")
    executable.chmod(0o755)
    options = CftLaunchOptions(
        executable_path=executable,
        user_data_dir=tmp_path / "profile",
        start_url="https://wx.zsxq.com/group/fixture",
        headless=False,
        background=True,
    )
    commands: list[list[str]] = []
    monkeypatch.setattr(browser_module.sys, "platform", "darwin")
    monkeypatch.setattr(browser_module.subprocess, "Popen", lambda command, **_kwargs: commands.append(command))

    BrowserSession("http://127.0.0.1:9223", cft_launch_options=options)._launch_cft()

    command = commands[0]
    assert command[:5] == ["/usr/bin/open", "-g", "-n", str(executable.parents[2]), "--args"]
    assert "--remote-debugging-port=9223" in command
    assert f"--user-data-dir={tmp_path / 'profile'}" in command
    assert "--headless=new" not in command
    assert command[-1] == options.start_url


def test_headless_cft_keeps_direct_background_only_launch(tmp_path, monkeypatch):
    executable = tmp_path / "Google Chrome for Testing.app" / "Contents" / "MacOS" / "Google Chrome for Testing"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fixture")
    executable.chmod(0o755)
    options = CftLaunchOptions(executable_path=executable, user_data_dir=tmp_path / "profile", headless=True)
    commands: list[list[str]] = []
    monkeypatch.setattr(browser_module.sys, "platform", "darwin")
    monkeypatch.setattr(browser_module.subprocess, "Popen", lambda command, **_kwargs: commands.append(command))

    BrowserSession("http://127.0.0.1:9223", cft_launch_options=options)._launch_cft()

    assert commands[0][0] == str(executable)
    assert "--headless=new" in commands[0]
