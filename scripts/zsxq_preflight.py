#!/usr/bin/env python3
"""Bounded manual diagnostic for the direct ZSXQ browser session."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from zsxq_pipeline.browser import BrowserSession, BrowserSessionError  # noqa: E402


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
    payload: dict[str, Any] = {
        "ok": False,
        "reason_code": reason_code,
        "detail": " ".join(str(detail or "").split())[:800],
    }
    if attempts:
        payload["attempts"] = attempts
    emit(payload)
    print(f"[BLOCKED] {reason_code}: {payload['detail']}")
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cdp-endpoint", required=True)
    parser.add_argument("--start-url", required=True)
    parser.add_argument("--job-config", required=True)
    parser.add_argument("--connect-timeout-ms", type=int, default=45_000)
    parser.add_argument("--navigation-timeout-ms", type=int, default=30_000)
    parser.add_argument("--state-wait-ms", type=int, default=12_000)
    parser.add_argument("--navigation-attempts", type=int, default=3)
    parser.add_argument("--retry-delay-ms", type=int, default=1_000)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        job_config = load_job_config(Path(args.job_config).expanduser().resolve())
    except Exception as exc:
        return fail("blocked_browser_configuration_invalid", exc, 22)

    group_name = str(job_config.get("group_name") or "前沿信息收录").strip()
    tag_name = str(job_config.get("tag_name") or "").strip()
    attempts: list[dict[str, Any]] = []
    navigation_attempts = max(1, int(args.navigation_attempts))
    timeout_ms = max(1_000, int(max(args.navigation_timeout_ms, args.state_wait_ms)))

    try:
        with BrowserSession(
            args.cdp_endpoint,
            connect_timeout_ms=max(1_000, int(args.connect_timeout_ms)),
        ) as session:
            for attempt_number in range(1, navigation_attempts + 1):
                diagnosis = session.doctor(
                    start_url=args.start_url,
                    group_name=group_name,
                    tag_name=tag_name,
                    timeout_ms=timeout_ms,
                )
                attempts.append(
                    {
                        "attempt": attempt_number,
                        "state": diagnosis.page_state,
                        "reason_code": diagnosis.code,
                    }
                )
                if diagnosis.ok:
                    emit(
                        {
                            "ok": True,
                            "reason_code": "ready",
                            "source": "browser_session_doctor",
                            "attempt": attempt_number,
                        }
                    )
                    print("[INFO] browser session preflight ready")
                    return 0
                if diagnosis.code == "need_reauth":
                    return fail("need_reauth", "knowledge planet login page detected", 20, attempts=attempts)
                if attempt_number < navigation_attempts:
                    time.sleep(max(0, int(args.retry_delay_ms)) / 1000)
    except BrowserSessionError as exc:
        return fail(exc.code, exc.detail, 22, attempts=attempts)

    last = attempts[-1] if attempts else {}
    reason_code = str(last.get("reason_code") or "zsxq_page_unavailable")
    if reason_code not in {"zsxq_page_unavailable", "zsxq_page_state_unrecognized"}:
        reason_code = "zsxq_page_unavailable"
    return fail(reason_code, "knowledge planet page did not become ready", 25, attempts=attempts)


if __name__ == "__main__":
    raise SystemExit(main())
