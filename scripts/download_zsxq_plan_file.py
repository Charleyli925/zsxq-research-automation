#!/usr/bin/env python3
"""Download one immutable-plan ZSXQ attachment through the dedicated CFT session."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from zsxq_page_state import (  # noqa: E402
    DEFAULT_GROUP_NAME,
    LOGIN_MARKERS,
    wait_for_zsxq_page_state,
)

CONTENT_PROTECTION_MARKER = "星主已开启「内容保护」"
GROUP_MARKER = DEFAULT_GROUP_NAME
DOWNLOAD_CONTROL_TEXTS = ("下载文件", "下载")
CONTENT_PROTECTION_CONFIRMATION_MS = 5000
DETAIL_STATE_POLL_INTERVAL_MS = 250
DEFAULT_NAVIGATION_ATTEMPTS = 3


def load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def select_candidate(
    scan_plan: dict[str, Any],
    *,
    file_id: str = "",
    filename: str = "",
) -> dict[str, Any]:
    rows = [
        row
        for row in scan_plan.get("download_candidates") or []
        if isinstance(row, dict)
    ]
    matches = []
    for row in rows:
        row_file_id = str(row.get("file_id") or "").strip()
        row_filename = str(row.get("filename") or row.get("name") or "").strip()
        if file_id and row_file_id == file_id:
            matches.append(row)
        elif not file_id and filename and row_filename == filename:
            matches.append(row)
    if len(matches) != 1:
        selector = f"file_id={file_id}" if file_id else f"filename={filename}"
        raise ValueError(f"expected exactly one planned candidate for {selector}; got {len(matches)}")
    return matches[0]


def unwrap_scan_plan(payload: dict[str, Any]) -> dict[str, Any]:
    snapshot = payload.get("scan_snapshot")
    if isinstance(snapshot, dict):
        return snapshot
    return payload


def choose_staging_dir(job_config: dict[str, Any]) -> Path:
    settings = job_config.get("download_settings")
    if not isinstance(settings, dict):
        raise ValueError("job config is missing download_settings")
    extra_dirs = settings.get("extra_staging_dirs") or []
    if extra_dirs:
        return Path(str(extra_dirs[0])).expanduser().resolve()
    staging_dir = str(settings.get("staging_dir") or "").strip()
    if not staging_dir:
        raise ValueError("job config is missing a staging directory")
    return Path(staging_dir).expanduser().resolve()


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def current_visible_exact_text_locator(page: Any, text: str) -> Any | None:
    visible: list[int] = []
    locator = page.get_by_text(text, exact=True)
    visible = [
        index
        for index in range(locator.count())
        if locator.nth(index).is_visible()
    ]
    if len(visible) > 1:
        raise RuntimeError(
            f"expected at most one visible exact-text target for {text!r}; "
            f"got {len(visible)}"
        )
    return locator.nth(visible[0]) if visible else None


def visible_exact_text_locator(page: Any, text: str, timeout_ms: int) -> Any:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        target = current_visible_exact_text_locator(page, text)
        if target is not None:
            return target
        page.wait_for_timeout(250)
    raise RuntimeError(f"expected one visible exact-text target for {text!r}; got 0")


def current_download_control(page: Any) -> Any | None:
    for text in DOWNLOAD_CONTROL_TEXTS:
        target = current_visible_exact_text_locator(page, text)
        if target is not None:
            return target
    return None


def wait_for_file_detail_state(
    page: Any,
    body: Any,
    timeout_ms: int,
    *,
    protection_confirmation_ms: int = CONTENT_PROTECTION_CONFIRMATION_MS,
) -> tuple[str, Any | None]:
    """Wait for the async file-permission check to reach a stable state.

    ZSXQ briefly renders the content-protection message while it resolves the
    member's download permission. A visible download control always wins. The
    protection message is terminal only after it remains unchanged for a grace
    period without a download control appearing.
    """

    deadline = time.monotonic() + timeout_ms / 1000
    protection_seen_at: float | None = None
    detail_text = ""

    while time.monotonic() < deadline:
        detail_text = body.inner_text(timeout=timeout_ms)
        download_control = current_download_control(page)
        if download_control is not None:
            return "downloadable", download_control

        now = time.monotonic()
        if CONTENT_PROTECTION_MARKER in detail_text:
            if protection_seen_at is None:
                protection_seen_at = now
            elif (now - protection_seen_at) * 1000 >= protection_confirmation_ms:
                return "protected", None
        else:
            protection_seen_at = None

        remaining_ms = max(0, int((deadline - now) * 1000))
        if remaining_ms:
            page.wait_for_timeout(
                min(DETAIL_STATE_POLL_INTERVAL_MS, remaining_ms)
            )

    download_control = current_download_control(page)
    if download_control is not None:
        return "downloadable", download_control
    if (
        CONTENT_PROTECTION_MARKER in detail_text
        and protection_seen_at is not None
        and (time.monotonic() - protection_seen_at) * 1000
        >= protection_confirmation_ms
    ):
        return "protected", None
    raise RuntimeError("file detail did not resolve to a stable download state")


def validate_pdf(path: Path) -> None:
    if not path.is_file():
        raise RuntimeError("download did not create a file")
    if path.stat().st_size < 1024:
        raise RuntimeError("downloaded file is unexpectedly small")
    with path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise RuntimeError("downloaded file is not a PDF")


def navigate_to_candidate_page(
    page: Any,
    *,
    topic_url: str,
    group_url: str,
    group_name: str,
    tag_name: str,
    timeout_ms: int,
    navigation_attempts: int,
) -> tuple[dict[str, Any], str]:
    """Navigate with bounded retries and return the rendered page state."""

    last_state: dict[str, Any] = {
        "state": "unrecognized",
        "signals": [],
        "url": "",
        "title": "",
    }
    last_navigation_error = ""
    for attempt in range(1, max(1, int(navigation_attempts)) + 1):
        last_navigation_error = ""
        try:
            page.goto(topic_url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            # A renderer can still commit after goto raises, so classify the
            # visible page before deciding whether another attempt is needed.
            last_navigation_error = " ".join(str(exc).split())[:800]

        last_state = wait_for_zsxq_page_state(
            page,
            target_url=group_url or topic_url,
            group_name=group_name,
            tag_name=tag_name,
            timeout_ms=timeout_ms,
        )
        if last_state["state"] in {"ready", "login"}:
            return last_state, last_navigation_error
        if attempt < max(1, int(navigation_attempts)):
            page.wait_for_timeout(min(1000, max(250, timeout_ms // 10)))

    return last_state, last_navigation_error


def download_candidate(
    candidate: dict[str, Any],
    *,
    cdp_endpoint: str,
    staging_dir: Path,
    timeout_ms: int,
    group_url: str = "",
    group_name: str = GROUP_MARKER,
    tag_name: str = "",
    navigation_attempts: int = DEFAULT_NAVIGATION_ATTEMPTS,
) -> dict[str, Any]:
    filename = str(candidate.get("filename") or candidate.get("name") or "").strip()
    file_id = str(candidate.get("file_id") or "").strip()
    topic_url = str(candidate.get("topic_url") or "").strip()
    if not filename or not file_id or not topic_url:
        raise ValueError("planned candidate is missing filename, file_id, or topic_url")
    if Path(filename).name != filename or not filename.lower().endswith(".pdf"):
        raise ValueError(f"unsafe planned filename: {filename!r}")

    staging_dir.mkdir(parents=True, exist_ok=True)
    destination = staging_dir / filename
    if destination.exists():
        return {
            "status": "blocked",
            "reason_code": "staging_collision",
            "file_id": file_id,
            "filename": filename,
            "topic_url": topic_url,
            "path": str(destination),
        }

    page = None
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(cdp_endpoint, timeout=timeout_ms)
        if not browser.contexts:
            raise RuntimeError("dedicated browser has no persistent context")
        context = browser.contexts[0]
        page = context.new_page()
        try:
            page_state, navigation_error = navigate_to_candidate_page(
                page,
                topic_url=topic_url,
                group_url=group_url,
                group_name=group_name,
                tag_name=tag_name,
                timeout_ms=timeout_ms,
                navigation_attempts=navigation_attempts,
            )
            body = page.locator("body")
            if page_state["state"] == "login":
                return {
                    "status": "blocked",
                    "reason_code": "need_reauth",
                    "file_id": file_id,
                    "filename": filename,
                    "topic_url": topic_url,
                }
            if page_state["state"] != "ready":
                reason_code = (
                    "zsxq_page_unavailable"
                    if page_state["state"] == "unavailable"
                    else "zsxq_page_state_unrecognized"
                )
                return {
                    "status": "blocked",
                    "reason_code": reason_code,
                    "file_id": file_id,
                    "filename": filename,
                    "topic_url": topic_url,
                    "page_state": page_state,
                    "message": navigation_error,
                }

            attachment = visible_exact_text_locator(page, filename, timeout_ms)
            attachment.click(timeout=timeout_ms)
            page.get_by_text("文件详情", exact=True).wait_for(
                state="visible",
                timeout=timeout_ms,
            )

            detail_state, download_control = wait_for_file_detail_state(
                page,
                body,
                timeout_ms,
            )
            if detail_state == "protected":
                return {
                    "status": "blocked",
                    "reason_code": "source_content_protected",
                    "file_id": file_id,
                    "filename": filename,
                    "topic_url": topic_url,
                    "message": "星主已开启内容保护，网页端不提供下载",
                }

            if download_control is None:
                raise RuntimeError("downloadable detail state has no download control")
            with page.expect_download(timeout=timeout_ms) as download_info:
                download_control.click(timeout=timeout_ms)
            download = download_info.value
            failure = download.failure()
            if failure:
                raise RuntimeError(f"browser download failed: {failure}")

            temp_handle, temp_name = tempfile.mkstemp(
                prefix=".zsxq-download-",
                suffix=".pdf",
                dir=staging_dir,
            )
            os.close(temp_handle)
            temp_path = Path(temp_name)
            try:
                download.save_as(temp_path)
                validate_pdf(temp_path)
                temp_path.rename(destination)
            finally:
                temp_path.unlink(missing_ok=True)

            return {
                "status": "downloaded",
                "reason_code": "download_completed",
                "file_id": file_id,
                "filename": filename,
                "topic_url": topic_url,
                "path": str(destination),
                "size_bytes": destination.stat().st_size,
            }
        except PlaywrightTimeoutError as exc:
            return {
                "status": "blocked",
                "reason_code": "playwright_action_timeout",
                "file_id": file_id,
                "filename": filename,
                "topic_url": topic_url,
                "message": " ".join(str(exc).split())[:800],
            }
        finally:
            if page is not None:
                page.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scan-plan", required=True)
    parser.add_argument("--job-config", required=True)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--file-id", default="")
    selector.add_argument("--filename", default="")
    parser.add_argument("--cdp-endpoint", default="http://127.0.0.1:9223")
    parser.add_argument("--timeout-ms", type=int, default=20000)
    parser.add_argument(
        "--navigation-attempts",
        type=int,
        default=DEFAULT_NAVIGATION_ATTEMPTS,
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        scan_plan = unwrap_scan_plan(
            load_json_object(Path(args.scan_plan).expanduser().resolve())
        )
        job_config = load_json_object(Path(args.job_config).expanduser().resolve())
        candidate = select_candidate(
            scan_plan,
            file_id=str(args.file_id).strip(),
            filename=str(args.filename).strip(),
        )
        result = download_candidate(
            candidate,
            cdp_endpoint=args.cdp_endpoint,
            staging_dir=choose_staging_dir(job_config),
            timeout_ms=max(1000, int(args.timeout_ms)),
            group_url=str(job_config.get("group_url") or "").strip(),
            group_name=str(job_config.get("group_name") or GROUP_MARKER).strip(),
            tag_name=str(job_config.get("tag_name") or "").strip(),
            navigation_attempts=max(1, int(args.navigation_attempts)),
        )
    except Exception as exc:
        emit(
            {
                "status": "failed",
                "reason_code": "download_helper_failed",
                "message": " ".join(str(exc).split())[:1200],
            }
        )
        return 1

    emit(result)
    return 0 if result.get("status") in {"downloaded", "blocked"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
