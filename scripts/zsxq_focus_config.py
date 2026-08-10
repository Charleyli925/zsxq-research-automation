#!/usr/bin/env python3
"""Shared focus config helpers for the ZSXQ autodownload flow."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_REGION_KEYWORDS = ["中国", "美国", "韩国", "中美", "中韩", "美韩"]
DEFAULT_REGION_REQUIRED_KEYWORDS = ["OTC"]
DEFAULT_RUNTIME_NOTES = [
    "Prefer PDFs over other file types.",
    "Skip titles that are clearly unrelated to the current focus.",
    "Use keyword rules from `config/local/interest_keywords.json` first.",
    "Standalone keywords match directly.",
    "Region words only count when the title also has a region-required keyword.",
]
DEFAULT_CONFIG_NOTE = (
    "Standalone keywords match directly. Region words only count when the title also has a "
    "region-required keyword."
)


def clean_list(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def unique_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(value)
    return cleaned


def update_list(existing: list[str], incoming: list[str], action: str) -> list[str]:
    if action == "set":
        return unique_keep_order(incoming)
    if action == "add":
        return unique_keep_order(existing + incoming)
    blocked = {value.casefold() for value in incoming}
    return [value for value in existing if value.casefold() not in blocked]


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def normalize_persistent_config(payload: dict[str, Any]) -> dict[str, Any]:
    interest_topics = clean_list(payload.get("interest_topics"))

    standalone_keywords = clean_list(payload.get("standalone_keywords"))
    if not standalone_keywords:
        standalone_keywords = unique_keep_order(
            clean_list(payload.get("exact_match_keywords"))
            + clean_list(payload.get("direct_topic_keywords"))
            + [
                item
                for item in clean_list(payload.get("match_keywords"))
                if item not in clean_list(payload.get("region_keywords"))
            ]
        )

    region_keywords = unique_keep_order(clean_list(payload.get("region_keywords")) or list(DEFAULT_REGION_KEYWORDS))

    region_required_keywords = clean_list(payload.get("region_required_keywords"))
    if not region_required_keywords:
        paired_keywords = clean_list(payload.get("paired_topic_keywords"))
        standalone_keys = {item.casefold() for item in standalone_keywords}
        region_required_keywords = [
            item for item in paired_keywords if item.casefold() not in standalone_keys
        ]
    if not region_required_keywords:
        region_required_keywords = list(DEFAULT_REGION_REQUIRED_KEYWORDS)

    exclude_keywords = clean_list(payload.get("exclude_keywords"))
    notes = str(payload.get("notes") or "").strip()
    if notes == DEFAULT_CONFIG_NOTE:
        notes = ""

    return {
        "schema_version": 2,
        "interest_topics": unique_keep_order(interest_topics),
        "standalone_keywords": unique_keep_order(standalone_keywords),
        "region_keywords": region_keywords,
        "region_required_keywords": unique_keep_order(region_required_keywords),
        "exclude_keywords": unique_keep_order(exclude_keywords),
        "notes": notes,
    }


def load_persistent_config(path: Path) -> dict[str, Any]:
    return normalize_persistent_config(load_json(path))


def save_persistent_config(path: Path, payload: dict[str, Any]) -> None:
    normalized = normalize_persistent_config(payload)
    save_json(path, normalized)


def normalize_runtime_state(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "temporary_focus_enabled": bool(payload.get("temporary_focus_enabled", False)),
        "temporary_focus": unique_keep_order(clean_list(payload.get("temporary_focus"))),
        "temporary_notes_enabled": bool(payload.get("temporary_notes_enabled", False)),
        "temporary_notes": unique_keep_order(clean_list(payload.get("temporary_notes"))),
        "updated_at": str(payload.get("updated_at") or "").strip() or None,
    }


def load_runtime_state(path: Path) -> dict[str, Any]:
    return normalize_runtime_state(load_json(path))


def save_runtime_state(path: Path, payload: dict[str, Any]) -> None:
    normalized = normalize_runtime_state(payload)
    save_json(path, normalized)


def default_runtime_notes(config: dict[str, Any]) -> list[str]:
    notes = list(DEFAULT_RUNTIME_NOTES)
    config_note = str(config.get("notes") or "").strip()
    if config_note and config_note != DEFAULT_CONFIG_NOTE and config_note not in notes:
        notes.append(config_note)
    return notes


def effective_focus(config: dict[str, Any], runtime_state: dict[str, Any]) -> list[str]:
    if runtime_state.get("temporary_focus_enabled"):
        return list(runtime_state.get("temporary_focus") or [])
    return list(config.get("interest_topics") or [])


def effective_notes(config: dict[str, Any], runtime_state: dict[str, Any]) -> list[str]:
    if runtime_state.get("temporary_notes_enabled"):
        return list(runtime_state.get("temporary_notes") or [])
    return default_runtime_notes(config)


def classify_terms(terms: list[str], config: dict[str, Any]) -> dict[str, list[str]]:
    known_region_keys = {
        item.casefold() for item in list(config.get("region_keywords") or []) + list(DEFAULT_REGION_KEYWORDS)
    }
    known_region_required_keys = {
        item.casefold()
        for item in list(config.get("region_required_keywords") or []) + list(DEFAULT_REGION_REQUIRED_KEYWORDS)
    }

    buckets = {
        "standalone_keywords": [],
        "region_keywords": [],
        "region_required_keywords": [],
    }
    for term in terms:
        key = term.casefold()
        if key in known_region_keys:
            buckets["region_keywords"].append(term)
        elif key in known_region_required_keys:
            buckets["region_required_keywords"].append(term)
        else:
            buckets["standalone_keywords"].append(term)
    return {name: unique_keep_order(values) for name, values in buckets.items()}


def update_persistent_config(
    config: dict[str, Any],
    *,
    action: str,
    topics: list[str],
    keywords: list[str],
) -> dict[str, Any]:
    next_config = normalize_persistent_config(config)

    if topics:
        next_config["interest_topics"] = update_list(next_config["interest_topics"], topics, action)

    effective_terms = unique_keep_order(topics + keywords)
    if effective_terms:
        buckets = classify_terms(effective_terms, next_config)
        if action == "set":
            next_config["standalone_keywords"] = buckets["standalone_keywords"]
            next_config["region_keywords"] = buckets["region_keywords"] or list(DEFAULT_REGION_KEYWORDS)
            next_config["region_required_keywords"] = (
                buckets["region_required_keywords"] or list(DEFAULT_REGION_REQUIRED_KEYWORDS)
            )
        else:
            for field, terms in buckets.items():
                if terms:
                    next_config[field] = update_list(next_config[field], terms, action)

    return normalize_persistent_config(next_config)


def build_runtime_state(
    *,
    config: dict[str, Any],
    current_state: dict[str, Any],
    action: str,
    topics: list[str],
    keywords: list[str],
    notes: list[str],
) -> dict[str, Any]:
    next_state = normalize_runtime_state(current_state)

    focus_items = unique_keep_order(topics + keywords)
    if focus_items:
        base_focus = effective_focus(config, next_state)
        next_state["temporary_focus"] = update_list(base_focus, focus_items, action)
        next_state["temporary_focus_enabled"] = True

    if notes:
        base_notes = effective_notes(config, next_state)
        next_state["temporary_notes"] = update_list(base_notes, notes, action)
        next_state["temporary_notes_enabled"] = True

    next_state["updated_at"] = datetime.now().astimezone().isoformat()
    return normalize_runtime_state(next_state)
