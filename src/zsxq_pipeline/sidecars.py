"""Explicit adapters for the ResearchLibrary and Obsidian projections.

The direct-Codex/Lark worker owns durable completion in SQLite.  These local
knowledge-base artifacts are intentionally downstream projections: failures
are visible to the caller but never cause a verified Lark document to be
rewritten or its publication state to be revoked.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .summary import PersistedSummary


class SidecarError(RuntimeError):
    """A local ResearchLibrary or Obsidian projection could not be refreshed."""


@dataclass(frozen=True, slots=True)
class SidecarArchiveResult:
    archived_count: int
    manifest_path: Path | None = None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _batch_id(item: Mapping[str, Any], library_root: Path) -> str:
    configured = str(item.get("batch_id", "")).strip()
    if configured:
        return configured
    source_path = Path(str(item.get("path", ""))).expanduser()
    try:
        relative = source_path.resolve(strict=False).relative_to((library_root / "pdfs").resolve(strict=False))
    except ValueError:
        relative = Path()
    if len(relative.parts) >= 2:
        return relative.parts[0]
    parent = source_path.parent.name
    if "__to__" in parent:
        return parent
    raw = str(item.get("modified_at", "")).strip()
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).strftime("%Y-%m-%d_%H-%M-%S") + "__to__manual"
    except ValueError:
        return "manual"


def _report_id(item: Mapping[str, Any]) -> str:
    explicit = str(item.get("report_id", "")).strip()
    if explicit:
        return explicit
    digest = str(item.get("pdf_sha256", "")).strip().lower()
    if len(digest) >= 16:
        return f"zsxq_{digest[:16]}"
    return "zsxq_" + re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(str(item.get("filename", "report"))).stem)[:64]


def _summary_destination(library_root: Path, item: Mapping[str, Any]) -> Path:
    stem = Path(str(item.get("filename", "report"))).stem or "report"
    safe_stem = re.sub(r"[\\/:\x00]+", "_", stem).strip() or "report"
    return library_root / "summaries" / _batch_id(item, library_root) / f"{safe_stem}.summary.md"


class ArtifactSidecars:
    """Project local summaries without becoming a second workflow authority."""

    def __init__(
        self,
        *,
        library_root: str | Path | None,
        library_database: str | Path | None = None,
        vault_root: str | Path | None,
        work_root: str | Path,
        python_executable: str | Path | None = None,
        repository_root: str | Path | None = None,
    ) -> None:
        self.library_root = Path(library_root).expanduser().resolve(strict=False) if library_root else None
        self.library_database = (
            Path(library_database).expanduser().resolve(strict=False) if library_database else None
        )
        self.vault_root = Path(vault_root).expanduser().resolve(strict=False) if vault_root else None
        self.work_root = Path(work_root).expanduser().resolve(strict=False)
        self.python_executable = str(python_executable or sys.executable)
        self.repository_root = Path(repository_root).expanduser().resolve(strict=False) if repository_root else _repo_root()

    @property
    def enabled(self) -> bool:
        return self.library_root is not None

    def persist_summary(self, item: Mapping[str, Any], persisted: PersistedSummary) -> Path:
        """Copy the committed local Markdown to its readable ResearchLibrary path."""

        if self.library_root is None:
            return persisted.paths.markdown_path
        destination = _summary_destination(self.library_root, item)
        _atomic_write(destination, persisted.entry.markdown.rstrip() + "\n")
        self._upsert_library(item, destination, title=persisted.entry.title)
        return destination

    def archive_published_group(
        self,
        *,
        entries: Sequence[Any],
        batch_items: Mapping[str, Mapping[str, Any]],
        document_url: str,
    ) -> SidecarArchiveResult:
        """Create/update readable Obsidian notes after a verified document publish."""

        if self.library_root is None:
            return SidecarArchiveResult(archived_count=0)
        files: list[dict[str, Any]] = []
        for entry in entries:
            source = batch_items.get(str(entry.path))
            if source is None:
                raise SidecarError(f"missing batch metadata for published path: {entry.path}")
            files.append(dict(source))
        manifest_path = self.work_root / "sidecars" / f"obsidian-{uuid.uuid4().hex}.json"
        _atomic_write(manifest_path, json.dumps({"files": files}, ensure_ascii=False, indent=2) + "\n")
        # Keep ResearchLibrary's human-readable index in sync even on
        # installations that intentionally do not configure an Obsidian vault.
        self._upsert_published_batch(manifest_path, document_url=document_url)
        if self.vault_root is None:
            return SidecarArchiveResult(archived_count=0, manifest_path=manifest_path)
        archive_script = self.repository_root / "scripts" / "archive_to_obsidian.py"
        if not archive_script.is_file():
            raise SidecarError(f"Obsidian archive script is unavailable: {archive_script}")
        payload = self._run(
            [
                self.python_executable,
                str(archive_script),
                "--batch-file",
                str(manifest_path),
                "--library-root",
                str(self.library_root),
                "--vault-root",
                str(self.vault_root),
                "--feishu-doc-url",
                str(document_url),
                *(
                    ("--library-database", str(self.library_database))
                    if self.library_database is not None
                    else ()
                ),
            ],
            operation="archive Obsidian notes",
        )
        count = payload.get("archived_count", 0)
        if isinstance(count, bool) or not isinstance(count, int):
            raise SidecarError("Obsidian archive returned no integer archived_count")
        return SidecarArchiveResult(archived_count=count, manifest_path=manifest_path)

    def _upsert_published_batch(self, manifest_path: Path, *, document_url: str) -> None:
        if self.library_root is None:
            return
        script = self.repository_root / "scripts" / "research_library_index.py"
        if not script.is_file():
            raise SidecarError(f"ResearchLibrary index script is unavailable: {script}")
        self._run(
            [
                self.python_executable,
                str(script),
                "--library-root",
                str(self.library_root),
                *(
                    ("--database", str(self.library_database))
                    if self.library_database is not None
                    else ()
                ),
                "upsert-from-batch",
                "--batch-file",
                str(manifest_path),
                "--feishu-doc-url",
                str(document_url),
                "--index-status",
                "feishu_published",
            ],
            operation="upsert ResearchLibrary publication",
        )

    def _upsert_library(self, item: Mapping[str, Any], summary_path: Path, *, title: str) -> None:
        if self.library_root is None:
            return
        script = self.repository_root / "scripts" / "research_library_index.py"
        if not script.is_file():
            raise SidecarError(f"ResearchLibrary index script is unavailable: {script}")
        self._run(
            [
                self.python_executable,
                str(script),
                "--library-root",
                str(self.library_root),
                *(
                    ("--database", str(self.library_database))
                    if self.library_database is not None
                    else ()
                ),
                "upsert",
                "--report-id",
                _report_id(item),
                "--pdf-sha256",
                str(item.get("pdf_sha256", "")),
                "--title",
                str(title),
                "--pdf-path",
                str(item.get("path", "")),
                "--summary-md-path",
                str(summary_path),
                "--index-status",
                "summary_created",
            ],
            operation="upsert ResearchLibrary summary",
        )

    @staticmethod
    def _run(argv: Sequence[str], *, operation: str) -> Mapping[str, Any]:
        try:
            completed = subprocess.run(
                list(argv), capture_output=True, text=True, check=False, shell=False, timeout=120
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SidecarError(f"unable to {operation}: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise SidecarError(f"unable to {operation}: {detail[:800]}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise SidecarError(f"{operation} did not return JSON") from exc
        if not isinstance(payload, Mapping):
            raise SidecarError(f"{operation} returned an invalid JSON root")
        return payload


__all__ = ["ArtifactSidecars", "SidecarArchiveResult", "SidecarError"]
