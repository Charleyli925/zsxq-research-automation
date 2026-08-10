#!/usr/bin/env python3
"""Release-local entrypoint used by the unified launchd template.

The installer invokes this file from ``runtime/current``.  Resolving imports
relative to the entrypoint avoids any dependency on a developer checkout or a
globally editable Python package installation.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parents[1]
    source = root / "src"
    if not source.is_dir():
        print("zsxq-pipeline entrypoint: release src directory is missing", file=sys.stderr)
        return 2
    sys.path.insert(0, str(source))
    from zsxq_pipeline.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
