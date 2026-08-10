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
DOWNLOAD_RUNTIME_PATTERNS = {
    "legacy OpenClaw agent invocation": re.compile(r"\bopenclaw\s+agent\b", re.IGNORECASE),
    "legacy Codex download launcher": re.compile(r"run_zsxq(?:_domestic_cicc)?_task_via_codex", re.IGNORECASE),
    "dynamic Playwright MCP": re.compile(r"@playwright/mcp@latest|npx\s+.*playwright", re.IGNORECASE),
    "direct Codex execution in download runtime": re.compile(r"\bcodex\s+exec\b", re.IGNORECASE),
}

UNIFIED_RUNTIME_PATTERNS = {
    "legacy OpenClaw agent invocation": re.compile(r"\bopenclaw\s+agent\b", re.IGNORECASE),
    "legacy task runtime entrypoint": re.compile(
        r"openclaw_tasks/|install_local_runtime\.sh|zsxq-(?:autodownload|domestic-cicc)\.plist",
        re.IGNORECASE,
    ),
    "dynamic Playwright MCP": re.compile(r"@playwright/mcp@latest|npx\s+.*playwright", re.IGNORECASE),
    "development checkout absolute path": re.compile(r"/(?:Users|home)/[^/\s]+/(?:Developer|Documents)/", re.IGNORECASE),
}

RETIRED_RUNTIME_PATHS = {
    Path("openclaw_tasks"),
    Path("deploy/install_local_runtime.sh"),
    Path("deploy/launchd/zsxq-autodownload.plist.template"),
    Path("deploy/launchd/zsxq-domestic-cicc.plist.template"),
    Path("scripts/run_zsxq_download_pipeline.py"),
}


def is_active_download_runtime(relative: Path) -> bool:
    rendered = relative.as_posix()
    return rendered in {
        "scripts/scan_zsxq_download_candidates.py",
        "scripts/download_zsxq_plan_file.py",
        "scripts/finalize_download_batch.py",
        "src/zsxq_pipeline/browser.py",
        "src/zsxq_pipeline/download.py",
    }


def is_active_unified_runtime(relative: Path) -> bool:
    rendered = relative.as_posix()
    return rendered in {
        "src/zsxq_pipeline/cli.py",
        "src/zsxq_pipeline/config.py",
        "src/zsxq_pipeline/lock.py",
        "src/zsxq_pipeline/scheduler.py",
        "src/zsxq_pipeline/worker.py",
        "src/zsxq_pipeline/process.py",
        "src/zsxq_pipeline/extract.py",
        "src/zsxq_pipeline/extractor_worker.py",
        "scripts/run_zsxq_pipeline.py",
        "deploy/install_pipeline_runtime.py",
        "deploy/launchd/zsxq-pipeline.plist.template",
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
    repository_files = iter_repository_files()
    violations = [
        f"{relative}: retired runtime entrypoint must not be restored"
        for relative in sorted(RETIRED_RUNTIME_PATHS)
        if any(
            path.relative_to(ROOT) == relative or relative in path.relative_to(ROOT).parents
            for path in repository_files
        )
    ]
    for path in repository_files:
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
        if is_active_download_runtime(relative):
            for label, pattern in DOWNLOAD_RUNTIME_PATTERNS.items():
                if pattern.search(text):
                    violations.append(f"{relative}: {label}")
        if is_active_unified_runtime(relative):
            for label, pattern in UNIFIED_RUNTIME_PATTERNS.items():
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
