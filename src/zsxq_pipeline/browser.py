"""One bounded, caller-owned Playwright/CDP session for ZSXQ downloads.

The session reuses the dedicated Chrome for Testing profile and can start its
CDP endpoint when a release-owned task configuration explicitly supplies the
browser executable and profile path. It never clears cookies, storage, or
profile data. Cleanup is limited to a confirmed-dead Chrome singleton marker
and surplus page targets proven to share the configured dedicated start
origin; targets from every other origin are preserved.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


# macOS system proxy settings can route even 127.0.0.1 through an HTTP proxy.
# CDP is explicitly restricted to a local endpoint below, so every readiness,
# target-list, create, and close request must bypass ambient proxies.
_DIRECT_CDP_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class BrowserSessionError(RuntimeError):
    """A CDP session cannot safely serve an immutable download transaction."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = str(code).strip() or "browser_unavailable"
        self.detail = " ".join(str(detail).split())[:1200]
        super().__init__(f"{self.code}: {self.detail}")


@dataclass(frozen=True, slots=True)
class CftLaunchOptions:
    """Explicit local ownership needed to start, but never reset, CFT."""

    executable_path: Path
    user_data_dir: Path
    start_url: str = ""
    headless: bool = True
    window_size: str = "1440,1200"
    startup_timeout_seconds: float = 25.0

    def __post_init__(self) -> None:
        if not self.executable_path.is_absolute():
            raise ValueError("CFT executable_path must be absolute")
        if not self.user_data_dir.is_absolute():
            raise ValueError("CFT user_data_dir must be absolute")
        if float(self.startup_timeout_seconds) <= 0:
            raise ValueError("CFT startup_timeout_seconds must be positive")
        if not str(self.window_size).strip():
            raise ValueError("CFT window_size is required")


@dataclass(frozen=True, slots=True)
class BrowserDoctorResult:
    """A non-secret browser readiness result suitable for status snapshots."""

    ok: bool
    code: str
    page_state: str

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "code": self.code, "page_state": self.page_state}


@dataclass(frozen=True, slots=True)
class _TargetCleanupResult:
    """Sanitized evidence from one bounded dedicated-target cleanup pass."""

    observed_pages: int = 0
    owned_pages: int = 0
    closed_pages: int = 0
    failed_closes: int = 0
    error: str = ""

    def diagnostic(self, phase: str) -> str:
        detail = (
            f"{phase}[observed={self.observed_pages},owned={self.owned_pages},"
            f"closed={self.closed_pages},failed={self.failed_closes}"
        )
        if self.error:
            detail += f",error={self.error}"
        return detail + "]"


class BrowserSession:
    """Lend one page with bounded reconnect and dedicated-profile recovery."""

    def __init__(
        self,
        cdp_endpoint: str,
        *,
        connect_timeout_ms: int = 45_000,
        cft_launch_options: CftLaunchOptions | None = None,
        playwright_factory: Callable[[], Any] = sync_playwright,
        connect_attempts: int = 2,
        owned_page_limit: int = 4,
        retry_delay_seconds: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        endpoint = str(cdp_endpoint).strip()
        if not endpoint:
            raise ValueError("cdp_endpoint is required")
        if int(connect_timeout_ms) < 1_000:
            raise ValueError("connect_timeout_ms must be at least 1000")
        if not 1 <= int(connect_attempts) <= 5:
            raise ValueError("connect_attempts must be between 1 and 5")
        if int(owned_page_limit) < 1:
            raise ValueError("owned_page_limit must be positive")
        if not 0 <= float(retry_delay_seconds) <= 5:
            raise ValueError("retry_delay_seconds must be between 0 and 5")
        self.cdp_endpoint = endpoint
        self.connect_timeout_ms = int(connect_timeout_ms)
        self.cft_launch_options = cft_launch_options
        self._playwright_factory = playwright_factory
        self.connect_attempts = int(connect_attempts)
        self.owned_page_limit = int(owned_page_limit)
        self.retry_delay_seconds = float(retry_delay_seconds)
        self._sleep = sleep
        self._manager: Any | None = None
        self._browser: Any | None = None
        self._page: Any | None = None

    @property
    def page(self) -> Any:
        if self._page is None:
            raise BrowserSessionError("browser_not_connected", "BrowserSession has not been entered")
        return self._page

    def __enter__(self) -> "BrowserSession":
        if self.cft_launch_options is not None:
            self._ensure_cft_ready()

        cleanup_evidence: list[tuple[str, _TargetCleanupResult]] = []
        cleanup_evidence.append(("preconnect", self._compact_owned_targets(limit=self.owned_page_limit)))
        last_error: BrowserSessionError | None = None
        last_cause: Exception | None = None
        for attempt in range(1, self.connect_attempts + 1):
            try:
                self._connect_page_once()
                return self
            except BrowserSessionError as exc:
                last_error = exc
                last_cause = exc
            except (PlaywrightError, OSError, RuntimeError) as exc:
                last_error = BrowserSessionError("blocked_browser_cdp_unresponsive", str(exc))
                last_cause = exc
            self.close()
            if attempt >= self.connect_attempts:
                break
            # A live CDP endpoint can still carry too many or unhealthy page
            # targets.  Collapse only pages proven to belong to this dedicated
            # CFT origin, preserve one keepalive target, then reconnect through
            # a fresh Playwright transport.  Cookies/storage/profile bytes are
            # never touched.
            if self.cft_launch_options is not None:
                self._ensure_cft_ready()
            cleanup_evidence.append((f"retry-{attempt}", self._compact_owned_targets(limit=1)))
            self._ensure_keepalive_page()
            if self.retry_delay_seconds:
                self._sleep(self.retry_delay_seconds)

        assert last_error is not None  # connect_attempts is validated positive
        diagnostics = "; ".join(result.diagnostic(phase) for phase, result in cleanup_evidence)
        detail = f"{last_error.detail[:700]}; connect_attempts={self.connect_attempts}; {diagnostics}"
        raise BrowserSessionError(last_error.code, detail) from last_cause

    def _connect_page_once(self) -> None:
        """Create one fresh transport and one caller-owned page."""

        self._manager = self._playwright_factory()
        playwright = self._manager.__enter__()
        self._browser = playwright.chromium.connect_over_cdp(
            self.cdp_endpoint,
            timeout=self.connect_timeout_ms,
        )
        contexts = list(getattr(self._browser, "contexts", []) or [])
        if not contexts:
            raise BrowserSessionError("blocked_browser_cdp_unresponsive", "dedicated browser has no persistent context")
        self._page = contexts[0].new_page()

    def _endpoint_base(self) -> tuple[str, int]:
        parsed = urllib.parse.urlparse(self.cdp_endpoint)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise BrowserSessionError(
                "blocked_browser_configuration_invalid",
                "CFT startup requires a local http(s) CDP endpoint",
            )
        try:
            port = int(parsed.port or 0)
        except ValueError as exc:
            raise BrowserSessionError("blocked_browser_configuration_invalid", "CDP endpoint has an invalid port") from exc
        if not 1 <= port <= 65535:
            raise BrowserSessionError("blocked_browser_configuration_invalid", "CDP endpoint must include a valid port")
        return f"{parsed.scheme}://{parsed.netloc}", port

    @staticmethod
    def _http_json(url: str, *, timeout_seconds: float = 1.5) -> Any:
        with _DIRECT_CDP_OPENER.open(url, timeout=timeout_seconds) as response:  # nosec B310 - fixed local CDP endpoint
            return json.loads(response.read().decode("utf-8", errors="replace"))

    @staticmethod
    def _http_put(url: str, *, timeout_seconds: float = 2.0) -> bytes:
        request = urllib.request.Request(url, method="PUT")
        with _DIRECT_CDP_OPENER.open(request, timeout=timeout_seconds) as response:  # nosec B310 - fixed local CDP endpoint
            return response.read()

    def _cdp_ready(self) -> bool:
        base, _port = self._endpoint_base()
        try:
            payload = self._http_json(f"{base}/json/version")
        except (OSError, ValueError, urllib.error.URLError, TimeoutError):
            return False
        return isinstance(payload, dict) and bool(str(payload.get("webSocketDebuggerUrl") or "").strip())

    @staticmethod
    def _same_origin(left: str, right: str) -> bool:
        try:
            left_url = urllib.parse.urlparse(str(left).strip())
            right_url = urllib.parse.urlparse(str(right).strip())
            left_port = left_url.port or (443 if left_url.scheme == "https" else 80)
            right_port = right_url.port or (443 if right_url.scheme == "https" else 80)
        except ValueError:
            return False
        return (
            left_url.scheme in {"http", "https"}
            and left_url.scheme == right_url.scheme
            and (left_url.hostname or "").casefold() == (right_url.hostname or "").casefold()
            and left_port == right_port
        )

    def _is_owned_page_target(self, target: Any) -> bool:
        options = self.cft_launch_options
        if options is None or not options.start_url or not isinstance(target, dict):
            return False
        if str(target.get("type") or "").strip() != "page":
            return False
        url = str(target.get("url") or "").strip()
        return url == "about:blank" or self._same_origin(url, options.start_url)

    def _compact_owned_targets(self, *, limit: int) -> _TargetCleanupResult:
        """Bound page targets from the explicitly configured dedicated origin.

        Raw CDP HTTP is used deliberately: it remains available when a
        Playwright transport or ``context.new_page`` is the failing layer.
        Targets from other origins and every non-page target are preserved.
        """

        options = self.cft_launch_options
        if options is None or not options.start_url:
            return _TargetCleanupResult()
        base, _port = self._endpoint_base()
        try:
            payload = self._http_json(f"{base}/json/list")
        except (OSError, ValueError, urllib.error.URLError, TimeoutError) as exc:
            return _TargetCleanupResult(error=" ".join(str(exc).split())[:120] or type(exc).__name__)
        if not isinstance(payload, list):
            return _TargetCleanupResult(error="CDP target list was not an array")
        pages = [item for item in payload if isinstance(item, dict) and str(item.get("type") or "") == "page"]
        owned = [item for item in pages if self._is_owned_page_target(item)]
        effective_limit = max(1, int(limit))
        if len(owned) <= effective_limit:
            return _TargetCleanupResult(observed_pages=len(pages), owned_pages=len(owned))

        preferred = [
            item
            for item in owned
            if str(item.get("url") or "").strip().startswith(str(options.start_url).strip())
        ]
        ordered = preferred + [item for item in owned if item not in preferred]
        keep_ids: set[str] = set()
        for item in ordered:
            target_id = str(item.get("id") or "").strip()
            if target_id:
                keep_ids.add(target_id)
            if len(keep_ids) >= effective_limit:
                break

        closed = 0
        failed = 0
        for item in owned:
            target_id = str(item.get("id") or "").strip()
            if not target_id or target_id in keep_ids:
                if not target_id:
                    failed += 1
                continue
            encoded = urllib.parse.quote(target_id, safe="")
            try:
                self._http_put(f"{base}/json/close/{encoded}")
            except (OSError, urllib.error.URLError, urllib.error.HTTPError, TimeoutError):
                failed += 1
            else:
                closed += 1
        return _TargetCleanupResult(
            observed_pages=len(pages),
            owned_pages=len(owned),
            closed_pages=closed,
            failed_closes=failed,
        )

    @staticmethod
    def _remove_confirmed_dead_singletons(user_data_dir: Path) -> bool:
        """Remove singleton markers only when their owner PID is known dead."""

        lock_path = user_data_dir / "SingletonLock"
        try:
            target = os.readlink(lock_path)
            owner_pid = int(target.rsplit("-", 1)[-1])
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return False
        try:
            os.kill(owner_pid, 0)
        except ProcessLookupError:
            pass
        except PermissionError:
            return False
        else:
            return False
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                (user_data_dir / name).unlink()
            except FileNotFoundError:
                pass
            except OSError:
                return False
        return True

    def _launch_cft(self) -> None:
        options = self.cft_launch_options
        if options is None:  # pragma: no cover - internal guard
            return
        base, port = self._endpoint_base()
        del base
        executable = options.executable_path.expanduser()
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise BrowserSessionError("blocked_browser_missing", f"Chrome for Testing executable not found: {executable}")
        profile = options.user_data_dir.expanduser()
        profile.mkdir(parents=True, exist_ok=True)
        self._remove_confirmed_dead_singletons(profile)
        command = [
            str(executable),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        if options.headless:
            command.extend(
                [
                    "--headless=new",
                    f"--window-size={options.window_size}",
                    "--disable-extensions",
                    "--disable-component-extensions-with-background-pages",
                ]
            )
        if options.start_url:
            command.append(options.start_url)
        log_path = Path(tempfile.gettempdir()) / "zsxq-cft-keepalive.log"
        with log_path.open("ab") as log_handle:
            subprocess.Popen(  # noqa: S603 - command is explicit release configuration
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
            )

    def _ensure_keepalive_page(self) -> None:
        options = self.cft_launch_options
        if options is None or not options.start_url:
            return
        base, _port = self._endpoint_base()
        try:
            tabs = self._http_json(f"{base}/json/list")
        except (OSError, ValueError, urllib.error.URLError, TimeoutError):
            return
        if any(str(tab.get("url") or "").startswith(options.start_url) for tab in tabs if isinstance(tab, dict)):
            return
        encoded = urllib.parse.quote(options.start_url, safe="")
        try:
            self._http_put(f"{base}/json/new?{encoded}")
        except (OSError, urllib.error.URLError, urllib.error.HTTPError):
            # Newer Chrome builds may refuse this optional endpoint. The real
            # browser session remains the authority for readiness.
            return

    def _ensure_cft_ready(self) -> None:
        if self._cdp_ready():
            self._ensure_keepalive_page()
            return
        self._launch_cft()
        options = self.cft_launch_options
        assert options is not None  # narrowed above
        deadline = time.monotonic() + float(options.startup_timeout_seconds)
        while time.monotonic() < deadline:
            if self._cdp_ready():
                self._ensure_keepalive_page()
                return
            time.sleep(0.25)
        raise BrowserSessionError(
            "blocked_browser_endpoint_unavailable",
            f"CFT CDP endpoint did not become ready within {float(options.startup_timeout_seconds):g}s",
        )

    def close(self) -> None:
        """Close only the page/CDP client, never the user-owned CFT process."""

        page, manager = self._page, self._manager
        self._page = None
        self._browser = None
        self._manager = None
        if page is not None:
            try:
                page.close()
            except Exception:
                pass
        if manager is not None:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                pass

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def doctor(
        self,
        *,
        start_url: str,
        group_name: str = "",
        tag_name: str = "",
        timeout_ms: int = 30_000,
    ) -> BrowserDoctorResult:
        """Run a bounded page-state check without downloading or changing state."""

        page = self.page
        try:
            page.goto(start_url, wait_until="domcontentloaded", timeout=max(1_000, int(timeout_ms)))
            text = str(page.locator("body").inner_text(timeout=max(1_000, int(timeout_ms))))
            if any(marker in text for marker in ("扫码登录", "微信登录", "手机号登录", "账号登录")):
                return BrowserDoctorResult(False, "need_reauth", "login")
            title = str(page.title())
            expected = [item for item in (group_name, tag_name) if item]
            if expected and not any(item in text or item in title for item in expected):
                return BrowserDoctorResult(False, "zsxq_page_state_unrecognized", "unrecognized")
            return BrowserDoctorResult(True, "ok", "ready")
        except (PlaywrightError, OSError, RuntimeError):
            return BrowserDoctorResult(False, "zsxq_page_unavailable", "unavailable")
