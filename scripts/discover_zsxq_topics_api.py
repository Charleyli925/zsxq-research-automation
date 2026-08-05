#!/usr/bin/env python3
"""
This file probes the current ZSXQ topic-list API from a live logged-in browser session.

Relation to other files:
- `prompts/openclaw_task_template.md` asks Codex to run this script only when API-first
  candidate scanning fails and DOM fallback is about to start.
- This script does not download files, archive files, or update task state.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright


DEFAULT_CDP_ENDPOINT = "http://127.0.0.1:9223"
DEFAULT_TAG_URL = os.environ.get("ZSXQ_TAG_URL", "")
TOPICS_URL_RE = re.compile(r"^https://api\.zsxq\.com/v\d+/hashtags/\d+/topics\?")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Probe ZSXQ topics API endpoint from current browser network responses."
    )
    parser.add_argument("--cdp-endpoint", default=DEFAULT_CDP_ENDPOINT)
    parser.add_argument("--tag-url", default=DEFAULT_TAG_URL)
    parser.add_argument("--scroll-rounds", type=int, default=6)
    parser.add_argument("--output")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.tag_url:
        raise SystemExit("--tag-url is required unless ZSXQ_TAG_URL is set")
    seen_urls: list[str] = []
    validated_urls: list[str] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(args.cdp_endpoint, timeout=30_000)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.new_page()

            def on_response(resp) -> None:
                url = resp.url
                if not TOPICS_URL_RE.match(url):
                    return
                if url not in seen_urls:
                    seen_urls.append(url)
                content_type = (resp.headers.get("content-type") or "").lower()
                if "json" not in content_type:
                    return
                try:
                    payload = resp.json()
                except Exception:
                    return
                if not isinstance(payload, dict):
                    return
                resp_data = payload.get("resp_data")
                if not isinstance(resp_data, dict):
                    return
                topics = resp_data.get("topics")
                if not isinstance(topics, list):
                    return
                if topics:
                    first = topics[0]
                    if not isinstance(first, dict):
                        return
                    if "topic_id" not in first or "create_time" not in first:
                        return
                if url not in validated_urls:
                    validated_urls.append(url)

            page.on("response", on_response)
            page.goto(args.tag_url, wait_until="domcontentloaded", timeout=90_000)
            page.wait_for_timeout(2_000)
            for _ in range(max(0, args.scroll_rounds)):
                page.mouse.wheel(0, 4_500)
                page.wait_for_timeout(1_200)
    except PlaywrightError as exc:
        result = {
            "found": False,
            "preferred_endpoint": None,
            "validated_endpoints": [],
            "seen_endpoints": [],
            "supports_end_time": False,
            "error": f"playwright_error: {exc}",
        }
        output = json.dumps(result, ensure_ascii=False, indent=2)
        if args.output:
            Path(args.output).write_text(output + "\n", encoding="utf-8")
        print(output)
        return 2

    preferred = validated_urls[-1] if validated_urls else None
    supports_end_time = False
    if preferred:
        supports_end_time = "end_time" in parse_qs(urlparse(preferred).query)

    result = {
        "found": bool(preferred),
        "preferred_endpoint": preferred,
        "validated_endpoints": validated_urls,
        "seen_endpoints": seen_urls,
        "supports_end_time": supports_end_time,
        "error": None,
    }
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0 if preferred else 1


if __name__ == "__main__":
    raise SystemExit(main())
