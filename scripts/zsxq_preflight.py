#!/usr/bin/env python3
"""Verify that the dedicated ZSXQ browser is reachable and authenticated."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from zsxq_page_state import observe_zsxq_page_state, wait_for_zsxq_page_state


def load_job_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("job config must be a JSON object")
    return payload


def emit(payload: dict[str, Any]) -> None:
    print("ZSXQ_PREFLIGHT_DIAG_JSON:" + json.dumps(payload, ensure_ascii=False))


def fail(
    reason_code: str,
    detail: object,
    exit_code: int,
    *,
    attempts: list[dict[str, Any]] | None = None,
) -> int:
    normalized_detail = " ".join(str(detail or "").split())[:800]
    payload: dict[str, Any] = {
        "ok": False,
        "reason_code": reason_code,
        "detail": normalized_detail,
    }
    if attempts:
        payload["attempts"] = attempts
    emit(payload)
    print(f"[BLOCKED] {reason_code}: {normalized_detail}")
    return exit_code


def compact_attempt(
    attempt_number: int,
    state: dict[str, Any],
    *,
    navigation_error: object = "",
) -> dict[str, Any]:
    payload = {
        "attempt": attempt_number,
        "state": state.get("state"),
        "signals": state.get("signals") or [],
        "url": str(state.get("url") or "")[:300],
        "title": str(state.get("title") or "")[:200],
    }
    normalized_error = " ".join(str(navigation_error or "").split())
    if normalized_error:
        payload["navigation_error"] = normalized_error[:500]
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp-endpoint", required=True)
    parser.add_argument("--start-url", required=True)
    parser.add_argument("--job-config", required=True)
    parser.add_argument("--connect-timeout-ms", type=int, default=45000)
    parser.add_argument("--navigation-timeout-ms", type=int, default=30000)
    parser.add_argument("--state-wait-ms", type=int, default=12000)
    parser.add_argument("--navigation-attempts", type=int, default=3)
    parser.add_argument("--retry-delay-ms", type=int, default=1000)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        job_config = load_job_config(Path(args.job_config).expanduser().resolve())
    except Exception as exc:
        return fail("blocked_browser_configuration_invalid", exc, 22)

    group_name = str(job_config.get("group_name") or "前沿信息收录").strip()
    group_url = str(job_config.get("group_url") or args.start_url).strip()
    tag_name = str(job_config.get("tag_name") or "").strip()
    attempts: list[dict[str, Any]] = []
    navigation_attempts = max(1, int(args.navigation_attempts))

    try:
        with sync_playwright() as playwright:
            try:
                browser = playwright.chromium.connect_over_cdp(
                    args.cdp_endpoint,
                    timeout=max(1000, int(args.connect_timeout_ms)),
                )
            except Exception as exc:
                return fail("blocked_browser_cdp_unresponsive", exc, 22)
            if not browser.contexts:
                return fail(
                    "blocked_browser_cdp_unresponsive",
                    "dedicated browser has no persistent context",
                    22,
                )
            context = browser.contexts[0]

            # A healthy existing group tab is sufficient proof and avoids
            # unnecessary navigation when the source is briefly unstable.
            for page in reversed(context.pages):
                state = observe_zsxq_page_state(
                    page,
                    target_url=group_url,
                    group_name=group_name,
                    tag_name=tag_name,
                )
                if state["state"] == "ready":
                    emit(
                        {
                            "ok": True,
                            "reason_code": "ready",
                            "source": "existing_page",
                            "state": state,
                        }
                    )
                    print("[INFO] playwright preflight ready from existing page")
                    return 0

            for attempt_number in range(1, navigation_attempts + 1):
                page = context.new_page()
                navigation_error: object = ""
                try:
                    try:
                        page.goto(
                            args.start_url,
                            wait_until="domcontentloaded",
                            timeout=max(1000, int(args.navigation_timeout_ms)),
                        )
                    except Exception as exc:
                        # A navigation exception can race with a successful
                        # renderer commit, so inspect the page before retrying.
                        navigation_error = exc

                    state = wait_for_zsxq_page_state(
                        page,
                        target_url=group_url,
                        group_name=group_name,
                        tag_name=tag_name,
                        timeout_ms=max(0, int(args.state_wait_ms)),
                    )
                    attempts.append(
                        compact_attempt(
                            attempt_number,
                            state,
                            navigation_error=navigation_error,
                        )
                    )
                    if state["state"] == "ready":
                        emit(
                            {
                                "ok": True,
                                "reason_code": "ready",
                                "source": "navigation",
                                "attempt": attempt_number,
                                "state": state,
                            }
                        )
                        print("[INFO] playwright preflight ready")
                        return 0
                    if state["state"] == "login":
                        page.close()
                        return fail(
                            "need_reauth",
                            "knowledge planet login page detected",
                            20,
                            attempts=attempts,
                        )
                except Exception as exc:
                    navigation_error = exc
                    state = {
                        "state": "unavailable",
                        "signals": ["page_inspection_failed"],
                        "url": "",
                        "title": "",
                    }
                    attempts.append(
                        compact_attempt(
                            attempt_number,
                            state,
                            navigation_error=navigation_error,
                        )
                    )
                page.close()
                if attempt_number < navigation_attempts:
                    time.sleep(max(0, int(args.retry_delay_ms)) / 1000)
    except Exception as exc:
        return fail(
            "blocked_browser_cdp_unresponsive",
            exc,
            22,
            attempts=attempts,
        )

    saw_unrecognized = any(
        attempt.get("state") == "unrecognized" for attempt in attempts
    )
    if saw_unrecognized:
        return fail(
            "zsxq_page_state_unrecognized",
            "group page loaded but no authenticated group signal was recognized",
            25,
            attempts=attempts,
        )
    return fail(
        "zsxq_page_unavailable",
        f"group page navigation failed after {navigation_attempts} attempts",
        24,
        attempts=attempts,
    )


if __name__ == "__main__":
    sys.exit(main())
