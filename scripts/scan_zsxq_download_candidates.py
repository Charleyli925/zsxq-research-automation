#!/usr/bin/env python3
"""
This file scans the current ZSXQ window and builds the exact PDF download plan.

Relation to other files:
- `zsxq_pipeline.download` reuses this scanner during a deterministic
  download transaction.
- `zsxq_keyword_matcher.py` is reused here so title matching stays consistent.
- `config/local/zsxq_foreign_reports_job.json` provides the group URL, tag URL, and archive root.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

try:
    from runtime_paths import REPO_ROOT
    from zsxq_focus_config import load_persistent_config
    from zsxq_keyword_matcher import match_title
    from zsxq_page_state import wait_for_zsxq_page_state
except ModuleNotFoundError:  # pragma: no cover - package import fallback
    from scripts.runtime_paths import REPO_ROOT
    from scripts.zsxq_focus_config import load_persistent_config
    from scripts.zsxq_keyword_matcher import match_title
    from scripts.zsxq_page_state import wait_for_zsxq_page_state


LOGIN_MARKERS = ("扫码登录", "微信登录", "手机号登录", "账号登录")
GROUP_ID_RE = re.compile(r"/group/(\d+)")
TAG_ID_RE = re.compile(r"/tags/.+/(\d+)$")
RETRYABLE_API_CODES = {"1059"}
TOPICS_API_MAX_RETRIES = 5
TOPICS_API_RETRY_DELAYS_SECONDS = (1.0, 3.0, 8.0, 15.0)
TAG_NAVIGATION_MAX_RETRIES = 3
TAG_PAGE_STATE_WAIT_MS = 12000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan ZSXQ topics and build a download plan.")
    parser.add_argument("--window-start", required=True)
    parser.add_argument("--window-end", required=True)
    parser.add_argument(
        "--job-config",
        default=str(REPO_ROOT / "config" / "local" / "zsxq_foreign_reports_job.json"),
    )
    parser.add_argument(
        "--keyword-file",
        default=str(REPO_ROOT / "config" / "local" / "interest_keywords.json"),
    )
    parser.add_argument("--cdp-endpoint", default="http://127.0.0.1:9223")
    parser.add_argument("--output", default="")
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def parse_iso_datetime(value: str) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    return datetime.fromisoformat(text)


def extract_group_id(group_url: str) -> str:
    match = GROUP_ID_RE.search(group_url)
    if not match:
        raise ValueError(f"cannot parse group id from {group_url}")
    return match.group(1)


def extract_tag_id(tag_url: str) -> str:
    match = TAG_ID_RE.search(tag_url)
    if not match:
        raise ValueError(f"cannot parse tag id from {tag_url}")
    return match.group(1)


def build_topic_url(group_url: str, topic_id: str) -> str:
    group_id = extract_group_id(group_url)
    return f"https://wx.zsxq.com/group/{group_id}/topic/{topic_id}"


def build_archived_name_set(archive_root: Path) -> set[str]:
    if not archive_root.exists():
        return set()
    return {path.name for path in archive_root.rglob("*") if path.is_file()}


def build_filename_key(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", Path(name).stem)
    collapsed = [char.casefold() for char in normalized if char.isalnum()]
    return "".join(collapsed) or normalized.casefold()


def normalize_api_code(value: Any) -> str:
    text = str(value or "").strip()
    return "" if text in {"0", "None", "null"} else text


def parse_topics_api_payload(payload: dict[str, Any]) -> tuple[str | None, list[dict[str, Any]]]:
    if not payload.get("ok"):
        return f"api_status_{payload.get('status')}", []

    try:
        data = json.loads(str(payload.get("text") or "{}"))
    except json.JSONDecodeError:
        return "api_invalid_json", []

    code = normalize_api_code(data.get("code"))
    if data.get("succeeded") is False or code:
        return f"api_error_{code or 'unknown'}", []

    topics = (((data or {}).get("resp_data") or {}).get("topics")) or []
    if not isinstance(topics, list):
        return "api_invalid_topics", []
    return None, [topic for topic in topics if isinstance(topic, dict)]


def is_retryable_topics_api_error(reason: str) -> bool:
    return reason.startswith("api_error_") and reason.removeprefix("api_error_") in RETRYABLE_API_CODES


def navigate_tag_page(
    page: Any,
    *,
    tag_url: str,
    group_url: str,
    group_name: str,
    tag_name: str,
    timeout_ms: int = 90000,
    attempts: int = TAG_NAVIGATION_MAX_RETRIES,
) -> str | None:
    """Open the tag with bounded retries and verify the rendered SPA state."""

    last_state = "unavailable"
    for attempt in range(1, max(1, int(attempts)) + 1):
        try:
            page.goto(tag_url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception:
            # Inspect the renderer because goto can raise after a successful
            # commit, especially around ERR_CONNECTION_CLOSED transitions.
            pass
        state = wait_for_zsxq_page_state(
            page,
            target_url=group_url or tag_url,
            group_name=group_name,
            tag_name=tag_name,
            timeout_ms=min(timeout_ms, TAG_PAGE_STATE_WAIT_MS),
        )
        last_state = str(state.get("state") or "unavailable")
        if last_state == "ready":
            return None
        if last_state == "login":
            return "need_reauth"
        if attempt < max(1, int(attempts)):
            page.wait_for_timeout(1000)
    return "api_unavailable" if last_state in {"unavailable", "unrecognized"} else last_state


def filter_topics(
    topics: list[dict[str, Any]],
    *,
    window_start: datetime,
    window_end: datetime,
    keyword_payload: dict[str, Any],
    archived_names: set[str],
    group_url: str,
) -> dict[str, Any]:
    results = {
        "topics": [],
        "matched_topics": [],
        "download_candidates": [],
        "skipped_duplicates": [],
    }
    archived_name_keys = {build_filename_key(name) for name in archived_names}
    seen_topic_ids: set[str] = set()
    planned_name_keys: dict[str, dict[str, Any]] = {}

    for topic in topics:
        topic_id = str(topic.get("topic_id") or "").strip()
        create_raw = str(topic.get("create_time") or "").strip()
        if not topic_id or not create_raw:
            continue
        # The paginated API can repeat the boundary topic on the next page.
        # Keep the scan plan immutable and one-row-per-topic so one physical PDF
        # never becomes two reconciliation candidates.
        if topic_id in seen_topic_ids:
            continue
        seen_topic_ids.add(topic_id)

        create_dt = parse_iso_datetime(create_raw)
        if create_dt < window_start or create_dt > window_end:
            continue

        files = []
        for item in (((topic.get("talk") or {}).get("files")) or []):
            filename = str(item.get("name") or "").strip()
            if not filename:
                continue
            files.append(
                {
                    "file_id": item.get("file_id"),
                    "name": filename,
                    "create_time": str(item.get("create_time") or "").strip() or None,
                }
            )

        topic_row = {
            "topic_id": topic_id,
            "topic_url": build_topic_url(group_url, topic_id),
            "create_time": create_dt.isoformat(),
            "title": str(topic.get("title") or "").strip(),
            "files": files,
        }
        results["topics"].append(topic_row)

        matched_files: list[dict[str, Any]] = []
        skipped_duplicates: list[dict[str, Any]] = []
        for file_row in files:
            filename = file_row["name"]
            stem = filename[:-4] if filename.lower().endswith(".pdf") else filename
            matched = match_title(stem, keyword_payload)
            if not matched.match_rule:
                continue

            candidate = {
                "topic_id": topic_id,
                "topic_url": topic_row["topic_url"],
                "topic_create_time": topic_row["create_time"],
                "filename": filename,
                "file_id": file_row["file_id"],
                "match_rule": matched.match_rule,
                "matched_keywords": matched.matched_keywords,
            }
            filename_key = build_filename_key(filename)
            if filename in archived_names or filename_key in archived_name_keys:
                candidate["duplicate_reason"] = "already_archived_filename"
                skipped_duplicates.append(candidate)
                results["skipped_duplicates"].append(candidate)
            elif filename_key in planned_name_keys:
                first_candidate = planned_name_keys[filename_key]
                candidate["duplicate_reason"] = "same_window_filename"
                candidate["duplicate_of_file_id"] = first_candidate.get("file_id")
                candidate["duplicate_of_topic_id"] = first_candidate.get("topic_id")
                skipped_duplicates.append(candidate)
                results["skipped_duplicates"].append(candidate)
            else:
                planned_name_keys[filename_key] = candidate
                matched_files.append(candidate)
                results["download_candidates"].append(candidate)

        if matched_files or skipped_duplicates:
            results["matched_topics"].append(
                {
                    "topic_id": topic_id,
                    "topic_url": topic_row["topic_url"],
                    "create_time": topic_row["create_time"],
                    "matched_files": matched_files,
                    "skipped_duplicates": skipped_duplicates,
                }
            )

    return results


def fetch_topics_from_browser(
    *,
    cdp_endpoint: str,
    tag_url: str,
    topics_api_url: str,
    window_start: datetime,
    group_url: str = "",
    group_name: str = "前沿信息收录",
    tag_name: str = "",
) -> tuple[str | None, list[dict[str, Any]]]:
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(cdp_endpoint, timeout=45_000)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()
            try:
                return fetch_topics_from_page(
                    page,
                    tag_url=tag_url,
                    topics_api_url=topics_api_url,
                    window_start=window_start,
                    group_url=group_url,
                    group_name=group_name,
                    tag_name=tag_name,
                )
            finally:
                close = getattr(page, "close", None)
                if callable(close):
                    close()
    except PlaywrightError:
        return "api_unavailable", []


def fetch_topics_from_page(
    page: Any,
    *,
    tag_url: str,
    topics_api_url: str,
    window_start: datetime,
    group_url: str = "",
    group_name: str = "前沿信息收录",
    tag_name: str = "",
) -> tuple[str | None, list[dict[str, Any]]]:
    """Scan through a caller-owned, authenticated browser page."""

    window_topics: list[dict[str, Any]] = []
    try:
        navigation_reason = navigate_tag_page(
            page,
            tag_url=tag_url,
            group_url=group_url,
            group_name=group_name,
            tag_name=tag_name,
        )
        if navigation_reason is not None:
            return navigation_reason, []

        url = topics_api_url
        while url:
            topics: list[dict[str, Any]] | None = None
            last_error: str | None = None
            for attempt in range(TOPICS_API_MAX_RETRIES):
                payload = page.evaluate(
                    """async (u) => {
                        const response = await fetch(u, { credentials: 'include' });
                        const text = await response.text();
                        return { ok: response.ok, status: response.status, text };
                    }""",
                    url,
                )
                error, topics = parse_topics_api_payload(payload)
                if error is None:
                    break
                last_error = error
                if not is_retryable_topics_api_error(error) or attempt == TOPICS_API_MAX_RETRIES - 1:
                    return error, []
                navigation_reason = navigate_tag_page(
                    page,
                    tag_url=tag_url,
                    group_url=group_url,
                    group_name=group_name,
                    tag_name=tag_name,
                )
                if navigation_reason is not None:
                    return navigation_reason, []
                delay_index = min(attempt, len(TOPICS_API_RETRY_DELAYS_SECONDS) - 1)
                time.sleep(TOPICS_API_RETRY_DELAYS_SECONDS[delay_index])

            if topics is None:
                return last_error or "api_unknown_error", []
            if not topics:
                break
            window_topics.extend(topics)
            last_topic = topics[-1]
            last_create = str(last_topic.get("create_time") or "").strip() if isinstance(last_topic, dict) else ""
            if not last_create or parse_iso_datetime(last_create) < window_start:
                break
            url = f"{topics_api_url}&end_time={quote(last_create, safe='')}"
    except PlaywrightError:
        return "api_unavailable", []
    return None, window_topics


def canonical_plan_hash(payload: dict[str, Any]) -> str:
    """Return a stable immutable-plan digest without self-referencing it."""

    canonical = dict(payload)
    canonical.pop("plan_hash", None)
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_scan_plan(
    *,
    window_start: datetime,
    window_end: datetime,
    job_config: dict[str, Any],
    keyword_payload: dict[str, Any],
    blocked_reason: str | None,
    raw_topics: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the versioned immutable plan shared by the CLI and pipeline."""

    group_url = str(job_config.get("group_url") or "").strip()
    archive_root = Path(((((job_config.get("download_settings") or {}).get("archive_root")) or "").strip()))
    filtered = filter_topics(
        raw_topics,
        window_start=window_start,
        window_end=window_end,
        keyword_payload=keyword_payload,
        archived_names=build_archived_name_set(archive_root),
        group_url=group_url,
    )
    result: dict[str, Any] = {
        "schema_version": 3,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "scan_mode": "api_first",
        "api_probe_status": "need_reauth" if blocked_reason == "need_reauth" else ("ok" if blocked_reason is None else "failed"),
        "blocked_reason": blocked_reason,
        "window_new_docs_count": len(filtered["topics"]),
        "keyword_matched_docs_count": len(filtered["matched_topics"]),
        "download_candidate_count": len(filtered["download_candidates"]),
        "skipped_duplicate_count": len(filtered["skipped_duplicates"]),
        **filtered,
    }
    result["plan_hash"] = canonical_plan_hash(result)
    return result


def scan_window(
    page: Any,
    *,
    window_start: datetime,
    window_end: datetime,
    job_config: dict[str, Any],
    keyword_payload: dict[str, Any],
) -> dict[str, Any]:
    """Create one immutable plan using the pipeline's existing CDP page."""

    group_url = str(job_config.get("group_url") or "").strip()
    tag_url = str(job_config.get("tag_url") or "").strip()
    tag_id = extract_tag_id(tag_url)
    blocked_reason, raw_topics = fetch_topics_from_page(
        page,
        tag_url=tag_url,
        topics_api_url=f"https://api.zsxq.com/v2/hashtags/{tag_id}/topics?count=20",
        window_start=window_start,
        group_url=group_url,
        group_name=str(job_config.get("group_name") or "前沿信息收录").strip(),
        tag_name=str(job_config.get("tag_name") or "").strip(),
    )
    return build_scan_plan(
        window_start=window_start,
        window_end=window_end,
        job_config=job_config,
        keyword_payload=keyword_payload,
        blocked_reason=blocked_reason,
        raw_topics=raw_topics,
    )


def main() -> int:
    args = parse_args()
    window_start = parse_iso_datetime(args.window_start)
    window_end = parse_iso_datetime(args.window_end)

    job_config = load_json(Path(args.job_config))
    keyword_payload = load_persistent_config(Path(args.keyword_file))

    group_url = str(job_config.get("group_url") or "").strip()
    tag_url = str(job_config.get("tag_url") or "").strip()
    tag_id = extract_tag_id(tag_url)
    topics_api_url = f"https://api.zsxq.com/v2/hashtags/{tag_id}/topics?count=20"

    blocked_reason, raw_topics = fetch_topics_from_browser(
        cdp_endpoint=args.cdp_endpoint,
        tag_url=tag_url,
        topics_api_url=topics_api_url,
        window_start=window_start,
        group_url=group_url,
        group_name=str(job_config.get("group_name") or "前沿信息收录").strip(),
        tag_name=str(job_config.get("tag_name") or "").strip(),
    )

    result = build_scan_plan(
        window_start=window_start,
        window_end=window_end,
        job_config=job_config,
        keyword_payload=keyword_payload,
        blocked_reason=blocked_reason,
        raw_topics=raw_topics,
    )

    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if blocked_reason in {None, "need_reauth"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
