#!/usr/bin/env python3
"""Shared ZSXQ page-state detection for preflight and download helpers."""

from __future__ import annotations

import re
import time
from typing import Any
from urllib.parse import urlparse


DEFAULT_GROUP_NAME = "前沿信息收录"
LOGIN_MARKERS = ("扫码登录", "微信登录", "手机号登录", "账号登录")
GROUP_NAVIGATION_MARKERS = ("外资研报", "国内研报")
UNAVAILABLE_MARKERS = (
    "err_connection_",
    "err_network_",
    "err_timed_out",
    "this site can’t be reached",
    "this site can't be reached",
    "无法访问此网站",
    "网页无法打开",
)
POLL_INTERVAL_MS = 250


def _normalized(value: object) -> str:
    return " ".join(str(value or "").split())


def _group_id(value: str) -> str:
    match = re.search(r"/group/([^/?#]+)", value or "", flags=re.IGNORECASE)
    return match.group(1) if match else ""


def _is_zsxq_url(value: str) -> bool:
    try:
        hostname = (urlparse(value).hostname or "").casefold()
    except ValueError:
        return False
    return hostname == "wx.zsxq.com" or hostname.endswith(".zsxq.com")


def classify_zsxq_page_state(
    *,
    url: str,
    title: str,
    body_text: str,
    target_url: str,
    group_name: str = DEFAULT_GROUP_NAME,
    tag_name: str = "",
) -> dict[str, Any]:
    """Classify a rendered ZSXQ page using several independent signals.

    The group name is not guaranteed to be present in the SPA body. A valid
    group page may expose it only through the document title, while the body
    contains the group navigation labels instead.
    """

    normalized_url = _normalized(url)
    normalized_title = _normalized(title)
    normalized_body = _normalized(body_text)
    folded_url = normalized_url.casefold()
    folded_title = normalized_title.casefold()
    folded_body = normalized_body.casefold()
    combined = f"{folded_title}\n{folded_body}"

    signals: list[str] = []
    target_group_id = _group_id(target_url)
    current_group_id = _group_id(normalized_url)
    target_group_match = bool(
        target_group_id
        and current_group_id
        and target_group_id == current_group_id
    )

    if "login" in folded_url or any(marker in normalized_body for marker in LOGIN_MARKERS):
        return {
            "state": "login",
            "signals": ["login_marker"],
            "url": normalized_url,
            "title": normalized_title,
        }

    if group_name and group_name in normalized_title:
        signals.append("group_name_in_title")
    if group_name and group_name in normalized_body:
        signals.append("group_name_in_body")
    if target_group_match:
        signals.append("target_group_url")
    if target_group_match and "知识星球" in normalized_title:
        signals.append("target_group_zsxq_title")
    if target_group_match and all(
        marker in normalized_body for marker in GROUP_NAVIGATION_MARKERS
    ):
        signals.append("target_group_navigation")
    if tag_name and tag_name in normalized_body and target_group_match:
        signals.append("target_tag_in_body")

    unavailable = any(marker in combined for marker in UNAVAILABLE_MARKERS)
    if unavailable and not any(
        signal
        in {
            "group_name_in_title",
            "group_name_in_body",
            "target_group_zsxq_title",
            "target_group_navigation",
        }
        for signal in signals
    ):
        return {
            "state": "unavailable",
            "signals": ["browser_error_page"],
            "url": normalized_url,
            "title": normalized_title,
        }

    ready = (
        "group_name_in_title" in signals
        or "group_name_in_body" in signals
        or "target_group_zsxq_title" in signals
        or "target_group_navigation" in signals
    )
    if ready and _is_zsxq_url(normalized_url):
        return {
            "state": "ready",
            "signals": signals,
            "url": normalized_url,
            "title": normalized_title,
        }

    return {
        "state": "unrecognized",
        "signals": signals,
        "url": normalized_url,
        "title": normalized_title,
    }


def observe_zsxq_page_state(
    page: Any,
    *,
    target_url: str,
    group_name: str = DEFAULT_GROUP_NAME,
    tag_name: str = "",
) -> dict[str, Any]:
    """Read one best-effort snapshot without failing on a settling page."""

    url = ""
    title = ""
    body_text = ""
    read_errors: list[str] = []
    try:
        url = str(page.url or "")
    except Exception as exc:
        read_errors.append(f"url: {exc}")
    try:
        title = str(page.title() or "")
    except Exception as exc:
        read_errors.append(f"title: {exc}")
    try:
        body_text = str(page.locator("body").inner_text(timeout=2000) or "")
    except Exception as exc:
        read_errors.append(f"body: {exc}")

    result = classify_zsxq_page_state(
        url=url,
        title=title,
        body_text=body_text,
        target_url=target_url,
        group_name=group_name,
        tag_name=tag_name,
    )
    if read_errors:
        result["read_errors"] = [
            _normalized(error)[:300] for error in read_errors
        ]
    return result


def wait_for_zsxq_page_state(
    page: Any,
    *,
    target_url: str,
    group_name: str = DEFAULT_GROUP_NAME,
    tag_name: str = "",
    timeout_ms: int,
    poll_interval_ms: int = POLL_INTERVAL_MS,
) -> dict[str, Any]:
    """Poll until the page is ready or clearly requires authentication."""

    timeout_ms = max(0, int(timeout_ms))
    deadline = time.monotonic() + timeout_ms / 1000
    last_result = observe_zsxq_page_state(
        page,
        target_url=target_url,
        group_name=group_name,
        tag_name=tag_name,
    )
    while last_result["state"] not in {"ready", "login"}:
        now = time.monotonic()
        if now >= deadline:
            break
        remaining_ms = max(1, int((deadline - now) * 1000))
        page.wait_for_timeout(min(poll_interval_ms, remaining_ms))
        last_result = observe_zsxq_page_state(
            page,
            target_url=target_url,
            group_name=group_name,
            tag_name=tag_name,
        )
    return last_result
