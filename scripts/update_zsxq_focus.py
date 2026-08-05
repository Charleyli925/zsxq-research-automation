#!/usr/bin/env python3
"""Update persistent and temporary focus settings for ZSXQ autodownload."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

try:
    from runtime_paths import DEFAULT_RUNTIME_ROOT, REPO_ROOT
    from zsxq_focus_config import (
        build_runtime_state,
        load_persistent_config,
        load_runtime_state,
        rebuild_runtime_prompt,
        save_persistent_config,
        save_runtime_state,
        unique_keep_order,
        update_persistent_config,
    )
except ModuleNotFoundError:  # pragma: no cover - package import fallback
    from scripts.runtime_paths import DEFAULT_RUNTIME_ROOT, REPO_ROOT
    from scripts.zsxq_focus_config import (
        build_runtime_state,
        load_persistent_config,
        load_runtime_state,
        rebuild_runtime_prompt,
        save_persistent_config,
        save_runtime_state,
        unique_keep_order,
        update_persistent_config,
    )

KEYWORDS_PATH = REPO_ROOT / "config/local/interest_keywords.json"
RUNTIME_STATE_PATH = DEFAULT_RUNTIME_ROOT / "state/zsxq_focus_runtime_state.json"
RUNTIME_PROMPT_PATH = DEFAULT_RUNTIME_ROOT / "prompts/openclaw_runtime_prompt.md"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Update ZSXQ focus settings.")
    parser.add_argument("--scope", choices=["temporary", "persistent"], required=True)
    parser.add_argument("--action", choices=["set", "add", "remove"], required=True)
    parser.add_argument("--topic", action="append", default=[], help="Repeatable. Accepts comma-separated values.")
    parser.add_argument("--keyword", action="append", default=[], help="Repeatable. Accepts comma-separated values.")
    parser.add_argument("--note", action="append", default=[], help="Repeatable. Accepts comma-separated values.")
    return parser.parse_args()


def split_items(values: list[str]) -> list[str]:
    items: list[str] = []
    for raw in values:
        for part in re.split(r"[,，\n]", raw):
            value = part.strip()
            if value:
                items.append(value)
    return items


def update_note_text(existing: str, incoming_notes: list[str], action: str) -> str:
    incoming = " ".join(incoming_notes).strip()
    current = str(existing or "").strip()
    if not incoming:
        return current
    if action == "set":
        return incoming
    if action == "add":
        if not current:
            return incoming
        if incoming in current:
            return current
        return f"{current} {incoming}".strip()
    return current.replace(incoming, "").strip()


def main() -> int:
    args = parse_args()
    topics = unique_keep_order(split_items(args.topic))
    keywords = unique_keep_order(split_items(args.keyword))
    notes = unique_keep_order(split_items(args.note))

    if not topics and not keywords and not notes:
        raise SystemExit("At least one of --topic / --keyword / --note is required.")

    result: dict[str, Any] = {"scope": args.scope, "action": args.action}
    persistent_config = load_persistent_config(KEYWORDS_PATH)
    runtime_state = load_runtime_state(RUNTIME_STATE_PATH)

    if args.scope == "temporary":
        next_runtime_state = build_runtime_state(
            config=persistent_config,
            current_state=runtime_state,
            action=args.action,
            topics=topics,
            keywords=keywords,
            notes=notes,
        )
        save_runtime_state(RUNTIME_STATE_PATH, next_runtime_state)
    else:
        next_persistent_config = update_persistent_config(
            persistent_config,
            action=args.action,
            topics=topics,
            keywords=keywords,
        )
        save_persistent_config(KEYWORDS_PATH, next_persistent_config)
        result["interest_topics"] = next_persistent_config["interest_topics"]
        result["standalone_keywords"] = next_persistent_config["standalone_keywords"]
        result["region_keywords"] = next_persistent_config["region_keywords"]
        result["region_required_keywords"] = next_persistent_config["region_required_keywords"]

        if notes:
            merged_notes = dict(next_persistent_config)
            merged_notes["notes"] = update_note_text(merged_notes.get("notes", ""), notes, args.action)
            save_persistent_config(KEYWORDS_PATH, merged_notes)

    runtime_snapshot = rebuild_runtime_prompt(
        config_path=KEYWORDS_PATH,
        runtime_state_path=RUNTIME_STATE_PATH,
        runtime_prompt_path=RUNTIME_PROMPT_PATH,
    )
    result["runtime_focus"] = runtime_snapshot["focus"]
    result["runtime_notes"] = runtime_snapshot["notes"]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
