#!/usr/bin/env python3
"""Small release-owned entrypoint for the deterministic download CLI."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from zsxq_pipeline.cli import main  # noqa: E402


if __name__ == "__main__":  # pragma: no cover - exercised by shell/runtime tests
    raise SystemExit(main(["download", *sys.argv[1:]]))
