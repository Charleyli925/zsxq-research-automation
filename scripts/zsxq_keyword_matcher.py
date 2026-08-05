#!/usr/bin/env python3
"""Decide whether a report title matches the current focus."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:
    from zsxq_focus_config import normalize_persistent_config
except ModuleNotFoundError:  # pragma: no cover - package import fallback
    from scripts.zsxq_focus_config import normalize_persistent_config


@dataclass
class TitleMatchResult:
    """The match result for one title."""

    matched_keywords: list[str]
    match_rule: str | None


def _as_clean_list(payload: dict[str, Any], key: str) -> list[str]:
    raw = payload.get(key, [])
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _find_matches(title: str, keywords: list[str]) -> list[str]:
    lowered_title = title.casefold()
    matched = [keyword for keyword in keywords if keyword.casefold() in lowered_title]
    return sorted(set(matched))


def match_title(title: str, keyword_payload: dict[str, Any]) -> TitleMatchResult:
    """Apply the current title-matching rules."""

    normalized = normalize_persistent_config(keyword_payload)
    excluded_matches = _find_matches(title, _as_clean_list(normalized, "exclude_keywords"))
    if excluded_matches:
        return TitleMatchResult(matched_keywords=[], match_rule=None)

    standalone_matches = _find_matches(title, _as_clean_list(normalized, "standalone_keywords"))
    if standalone_matches:
        return TitleMatchResult(matched_keywords=standalone_matches, match_rule="standalone")

    region_matches = _find_matches(title, _as_clean_list(normalized, "region_keywords"))
    region_required_matches = _find_matches(title, _as_clean_list(normalized, "region_required_keywords"))
    if region_matches and region_required_matches:
        merged = sorted(set(region_matches + region_required_matches))
        return TitleMatchResult(matched_keywords=merged, match_rule="region_plus_topic")

    return TitleMatchResult(matched_keywords=[], match_rule=None)
