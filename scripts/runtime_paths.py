#!/usr/bin/env python3
"""Portable path defaults shared by the automation scripts."""

from __future__ import annotations

import os
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_HOME = Path(
    os.environ.get(
        "INVESTMENT_REPORTS_DATA_HOME",
        Path.home() / "Library" / "Application Support" / "investment-reports-automation",
    )
).expanduser()
DEFAULT_LIBRARY_ROOT = Path(
    os.environ.get("RESEARCH_LIBRARY_ROOT", DEFAULT_DATA_HOME / "ResearchLibrary")
).expanduser()
DEFAULT_VAULT_ROOT = Path(
    os.environ.get("OBSIDIAN_VAULT_ROOT", DEFAULT_DATA_HOME / "ResearchVault")
).expanduser()
DEFAULT_CONFIG_ROOT = Path(
    os.environ.get("INVESTMENT_REPORTS_CONFIG_ROOT", DEFAULT_LIBRARY_ROOT / "config")
).expanduser()
DEFAULT_RUNTIME_ROOT = Path(
    os.environ.get("INVESTMENT_REPORTS_RUNTIME_DIR", REPO_ROOT / ".runtime")
).expanduser()
