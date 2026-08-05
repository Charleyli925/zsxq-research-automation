#!/usr/bin/env python3
"""Reject machine-specific paths, likely credentials, and runtime artifacts."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".runtime",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "logs",
    "output",
    "state",
    "summary_cache",
    "text_cache",
    "tmp",
}
FORBIDDEN_FILENAMES = {
    ".env",
    "config.env",
}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".jpeg",
    ".jpg",
    ".key",
    ".pdf",
    ".pem",
    ".png",
    ".sqlite",
    ".sqlite3",
}
TEXT_PATTERNS = {
    "macOS user path": re.compile(r"/" + r"Users/[^/\s]+/"),
    "GitHub token": re.compile(r"\b(?:gh[opsu]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "OpenAI-style secret": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    "Feishu chat identifier": re.compile(r"\boc_[0-9a-f]{16,}\b"),
    "Feishu user identifier": re.compile(r"\bou_[0-9A-Za-z]{16,}\b"),
    "AWS access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "non-example email address": re.compile(
        r"\b[A-Za-z0-9._%+-]+@(?!example\.com\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"
    ),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
}


def iter_repository_files(root: Path = ROOT) -> list[Path]:
    """Scan Git-visible files, including intended untracked changes but not local excludes."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            check=True,
            capture_output=True,
        )
        relative_paths = [
            Path(raw.decode("utf-8", errors="surrogateescape"))
            for raw in completed.stdout.split(b"\0")
            if raw
        ]
    except (OSError, subprocess.CalledProcessError):
        relative_paths = [path.relative_to(root) for path in root.rglob("*") if path.is_file()]

    return sorted(
        path
        for relative in relative_paths
        for path in [root / relative]
        if path.is_file() and not EXCLUDED_DIRS.intersection(relative.parts)
    )


def main() -> int:
    violations: list[str] = []
    for path in iter_repository_files():
        relative = path.relative_to(ROOT)
        if path.name in FORBIDDEN_FILENAMES and not path.name.endswith(".example"):
            violations.append(f"{relative}: local environment file")
        if path.suffix.casefold() in FORBIDDEN_SUFFIXES:
            violations.append(f"{relative}: runtime or report artifact")

        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, pattern in TEXT_PATTERNS.items():
            if pattern.search(text):
                violations.append(f"{relative}: {label}")

    if violations:
        print("Repository hygiene check failed:", file=sys.stderr)
        for violation in violations:
            print(f"- {violation}", file=sys.stderr)
        return 1

    print("Repository hygiene check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
