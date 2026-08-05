#!/usr/bin/env python3
"""
This file handles the local file cleanup after browser downloading is done.

Relation to other files:
- Codex/OpenClaw first use Chrome to download PDFs into `~/Downloads`.
- This script then finds the files that belong to the current run,
  moves them into the final batch folder, and updates the state file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from runtime_paths import DEFAULT_RUNTIME_ROOT, REPO_ROOT
    from zsxq_focus_config import load_persistent_config
    from zsxq_keyword_matcher import match_title
    from research_library_index import db_path_for_library, record_event, upsert_report
except ModuleNotFoundError:  # pragma: no cover - package import fallback
    from scripts.runtime_paths import DEFAULT_RUNTIME_ROOT, REPO_ROOT
    from scripts.zsxq_focus_config import load_persistent_config
    from scripts.zsxq_keyword_matcher import match_title
    from scripts.research_library_index import db_path_for_library, record_event, upsert_report


JOB_CONFIG_PATH = REPO_ROOT / "config/local/zsxq_foreign_reports_job.json"
KEYWORDS_PATH = REPO_ROOT / "config/local/interest_keywords.json"
STATE_PATH = DEFAULT_RUNTIME_ROOT / "state/zsxq_foreign_reports_state.json"


@dataclass
class CandidateFile:
    """A local file that passed the checks for this run."""

    path: Path
    archive_name: str
    modified_at: datetime
    matched_keywords: list[str]
    match_rule: str | None
    size_bytes: int
    source_priority: int


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def parse_args() -> argparse.Namespace:
    """Read the time window and optional custom file paths from the command line."""

    parser = argparse.ArgumentParser(
        description="Archive newly downloaded ZSXQ foreign-report PDFs into a time-window folder."
    )
    parser.add_argument("--config", default=str(JOB_CONFIG_PATH))
    parser.add_argument("--keywords", default=str(KEYWORDS_PATH))
    parser.add_argument("--state", default=str(STATE_PATH))
    parser.add_argument("--window-start", help="ISO8601 timestamp. Defaults to last_successful_check_at from state.")
    parser.add_argument("--window-end", help="ISO8601 timestamp. Defaults to current local time.")
    parser.add_argument(
        "--downloaded-after",
        help=(
            "ISO8601 timestamp. If provided, archive filtering uses local file mtime > downloaded_after "
            "instead of window_start. Useful for explicit-history runs."
        ),
    )
    parser.add_argument(
        "--downloaded-before",
        help="ISO8601 timestamp. Used with --downloaded-after. Defaults to current local time.",
    )
    parser.add_argument(
        "--skip-state-update",
        action="store_true",
        help="Archive files but keep the state checkpoint unchanged.",
    )
    parser.add_argument("--run-id", default="", help="Stable UUID for one launcher run.")
    parser.add_argument(
        "--scan-plan",
        default="",
        help="Candidate plan for this run. When set, only listed files may be archived.",
    )
    parser.add_argument(
        "--run-manifest",
        default="",
        help="Persistent aggregate manifest shared by every finalize call in this run.",
    )
    parser.add_argument(
        "--commit-state",
        action="store_true",
        help="Commit the frozen scan window only after all planned candidates are reconciled.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=0.0,
        help="Wait briefly for a later download micro-batch before returning.",
    )
    parser.add_argument("--wait-interval-seconds", type=float, default=2.0)
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without moving files.")
    return parser.parse_args()


def parse_iso8601(value: str) -> datetime:
    return datetime.fromisoformat(value)


def build_filename_key(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", Path(name).stem)
    collapsed = [char.casefold() for char in normalized if char.isalnum()]
    return "".join(collapsed) or normalized.casefold()


def validate_scan_plan(
    plan: dict[str, Any],
    *,
    window_start: datetime,
    window_end: datetime,
) -> dict[str, Any]:
    plan_start = str(plan.get("window_start") or "").strip()
    plan_end = str(plan.get("window_end") or "").strip()
    if not plan_start or not plan_end:
        raise SystemExit("scan plan is missing its frozen window")
    if parse_iso8601(plan_start) != window_start or parse_iso8601(plan_end) != window_end:
        raise SystemExit("scan plan window does not match finalize window")
    candidates = plan.get("download_candidates")
    if not isinstance(candidates, list):
        raise SystemExit("scan plan download_candidates must be a list")
    declared_count = plan.get("download_candidate_count")
    try:
        declared_count = int(declared_count)
    except Exception as exc:
        raise SystemExit("scan plan download_candidate_count is invalid") from exc
    if declared_count != len(candidates):
        raise SystemExit(
            f"scan plan candidate invariant failed: declared={declared_count}, actual={len(candidates)}"
        )
    return plan


def load_scan_plan(
    path: Path,
    *,
    window_start: datetime,
    window_end: datetime,
    run_manifest_path: Path | None = None,
) -> dict[str, Any]:
    if path.exists():
        plan = load_json(path)
    elif run_manifest_path is not None and run_manifest_path.exists():
        manifest = load_json(run_manifest_path)
        snapshot = manifest.get("scan_snapshot")
        if not isinstance(snapshot, dict):
            raise SystemExit(f"scan plan is missing and run manifest has no scan snapshot: {path}")
        plan = snapshot
        recorded_hash = str(manifest.get("scan_plan_sha256") or "").strip()
        if recorded_hash and canonical_json_sha256(plan) != recorded_hash:
            raise SystemExit("run manifest scan snapshot hash mismatch")
    else:
        raise SystemExit(f"scan plan is missing: {path}")
    return validate_scan_plan(plan, window_start=window_start, window_end=window_end)


def planned_candidates_by_filename(plan: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    by_name: dict[str, list[dict[str, Any]]] = {}
    for raw in plan.get("download_candidates") or []:
        if not isinstance(raw, dict):
            continue
        filename = str(raw.get("filename") or raw.get("name") or "").strip()
        if not filename:
            continue
        row = dict(raw)
        row["filename"] = filename
        by_name.setdefault(build_filename_key(filename), []).append(row)
    return by_name


def candidate_identity(row: dict[str, Any]) -> str:
    file_id = str(row.get("file_id") or "").strip()
    if file_id:
        return f"file_id:{file_id}"
    return f"filename:{build_filename_key(str(row.get('filename') or row.get('name') or ''))}"


def canonical_json_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def filter_candidates_to_scan_plan(
    candidates: list[CandidateFile],
    plan: dict[str, Any],
) -> tuple[list[CandidateFile], list[dict[str, Any]], dict[str, dict[str, Any]]]:
    planned_by_name = planned_candidates_by_filename(plan)
    accepted: list[CandidateFile] = []
    rejected: list[dict[str, Any]] = []
    selected_plan_rows: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        key = build_filename_key(candidate.archive_name)
        rows = planned_by_name.get(key) or []
        if not rows:
            rejected.append(
                {
                    "filename": candidate.archive_name,
                    "reason": "not_in_scan_plan",
                    "source_paths": [str(candidate.path)],
                }
            )
            continue
        accepted.append(candidate)
        exact_rows = [row for row in rows if str(row.get("filename") or "") == candidate.archive_name]
        selected_plan_rows[candidate.archive_name] = exact_rows[0] if exact_rows else rows[0]
    return accepted, rejected, selected_plan_rows


def merge_manifest_entries(existing: list[dict[str, Any]], additions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in [*existing, *additions]:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("candidate_identity") or entry.get("pdf_sha256") or entry.get("archive_path") or "")
        if not key:
            key = f"filename:{build_filename_key(str(entry.get('filename') or ''))}"
        if key in seen:
            continue
        seen.add(key)
        merged.append(entry)
    return merged


def reconcile_same_filename_plan_duplicates(
    expected_rows: list[dict[str, Any]],
    downloaded_entries: list[dict[str, Any]],
    satisfied_entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Credit duplicate plan rows that map to one physical archive filename.

    Older immutable plans could contain the same normalized filename from two
    topics with different file IDs. The filesystem can only retain one file at
    that path, so once one planned identity is archived the remaining exact
    filename rows are satisfied duplicates rather than missing downloads.
    """

    rows_by_name: dict[str, list[dict[str, Any]]] = {}
    for row in expected_rows:
        filename = str(row.get("filename") or row.get("name") or "").strip()
        if filename:
            rows_by_name.setdefault(build_filename_key(filename), []).append(row)

    completed_entries = [*downloaded_entries, *satisfied_entries]
    completed_identities = {
        str(entry.get("candidate_identity") or "")
        for entry in completed_entries
        if str(entry.get("candidate_identity") or "").strip()
    }
    completed_by_name: dict[str, dict[str, Any]] = {}
    for entry in completed_entries:
        filename = str(entry.get("filename") or "").strip()
        if filename:
            completed_by_name.setdefault(build_filename_key(filename), entry)

    additions: list[dict[str, Any]] = []
    for filename_key, rows in rows_by_name.items():
        if len(rows) < 2:
            continue
        source_entry = completed_by_name.get(filename_key)
        if not source_entry:
            continue
        for row in rows:
            identity = candidate_identity(row)
            if identity in completed_identities:
                continue
            filename = str(row.get("filename") or row.get("name") or "").strip()
            addition = {
                "candidate_identity": identity,
                "source_file_id": row.get("file_id"),
                "source_topic_id": row.get("topic_id"),
                "filename": filename,
                "pdf_sha256": source_entry.get("pdf_sha256"),
                "disposition": "same_window_filename_duplicate",
                "duplicate_of_candidate_identity": source_entry.get(
                    "candidate_identity"
                ),
                "existing_archive_path": source_entry.get("archive_path")
                or source_entry.get("existing_archive_path")
                or source_entry.get("path")
                or "",
                "matched_keywords": row.get("matched_keywords") or [],
                "match_rule": row.get("match_rule"),
            }
            additions.append(addition)
            completed_identities.add(identity)
    return additions


def build_run_manifest(
    *,
    path: Path,
    run_id: str,
    scan_plan_path: Path,
    scan_plan: dict[str, Any],
    attempt_entries: list[dict[str, Any]],
    attempt_satisfied_entries: list[dict[str, Any]],
    batch_dir: Path | None,
    rejected_files: list[dict[str, Any]],
    now: datetime,
    dry_run: bool,
) -> dict[str, Any]:
    scan_plan_sha256 = canonical_json_sha256(scan_plan)
    existing: dict[str, Any] = {}
    if path.exists():
        existing = load_json(path)
        if str(existing.get("run_id") or "") != run_id:
            raise SystemExit("run manifest belongs to a different run_id")
        if str(existing.get("scan_window_start") or "") != str(scan_plan.get("window_start") or ""):
            raise SystemExit("run manifest window_start changed during the run")
        if str(existing.get("scan_window_end") or "") != str(scan_plan.get("window_end") or ""):
            raise SystemExit("run manifest window_end changed during the run")
        existing_plan_sha256 = str(existing.get("scan_plan_sha256") or "").strip()
        if existing_plan_sha256 and existing_plan_sha256 != scan_plan_sha256:
            raise SystemExit("scan plan changed during the run")

    expected_rows = [
        dict(row)
        for row in scan_plan.get("download_candidates") or []
        if isinstance(row, dict)
    ]
    downloaded_entries = merge_manifest_entries(
        existing.get("downloaded_entries") if isinstance(existing.get("downloaded_entries"), list) else [],
        attempt_entries,
    )
    satisfied_entries = merge_manifest_entries(
        existing.get("satisfied_entries") if isinstance(existing.get("satisfied_entries"), list) else [],
        attempt_satisfied_entries,
    )
    satisfied_entries = merge_manifest_entries(
        satisfied_entries,
        reconcile_same_filename_plan_duplicates(
            expected_rows,
            downloaded_entries,
            satisfied_entries,
        ),
    )
    archive_dirs = [str(value) for value in (existing.get("archive_dirs") or []) if str(value).strip()]
    if batch_dir is not None and str(batch_dir) not in archive_dirs and attempt_entries:
        archive_dirs.append(str(batch_dir))

    completed_identities = {
        str(entry.get("candidate_identity") or "")
        for entry in [*downloaded_entries, *satisfied_entries]
        if str(entry.get("candidate_identity") or "").strip()
    }
    # Old/legacy intermediate manifests may not yet carry candidate_identity.
    completed_name_keys = {
        build_filename_key(str(entry.get("filename") or ""))
        for entry in [*downloaded_entries, *satisfied_entries]
        if str(entry.get("filename") or "").strip()
        and not str(entry.get("candidate_identity") or "").strip()
    }
    missing_candidates: list[dict[str, Any]] = []
    for row in expected_rows:
        filename = str(row.get("filename") or row.get("name") or "").strip()
        if candidate_identity(row) in completed_identities or build_filename_key(filename) in completed_name_keys:
            continue
        missing_candidates.append(row)

    attempts = existing.get("finalize_attempts") if isinstance(existing.get("finalize_attempts"), list) else []
    attempts = [*attempts]
    attempts.append(
        {
            "attempt_number": len(attempts) + 1,
            "finalized_at": now.isoformat(),
            "batch_dir": str(batch_dir) if batch_dir else None,
            "new_file_count": len(attempt_entries),
            "new_files": [entry.get("filename") for entry in attempt_entries],
            "new_satisfied_count": len(attempt_satisfied_entries),
            "new_satisfied_files": [entry.get("filename") for entry in attempt_satisfied_entries],
            "rejected_files": rejected_files,
            "dry_run": dry_run,
        }
    )
    blocked_reason = str(scan_plan.get("blocked_reason") or "").strip()
    invariant_errors: list[str] = []
    if int(scan_plan.get("download_candidate_count") or 0) != len(expected_rows):
        invariant_errors.append("scan_plan_candidate_count_mismatch")
    if len(downloaded_entries) > len(expected_rows):
        invariant_errors.append("downloaded_count_exceeds_candidate_count")
    if len(downloaded_entries) + len(satisfied_entries) > len(expected_rows):
        invariant_errors.append("reconciled_count_exceeds_candidate_count")
    if len(downloaded_entries) + len(satisfied_entries) + len(missing_candidates) != len(expected_rows):
        invariant_errors.append("candidate_reconciliation_count_mismatch")
    if blocked_reason:
        invariant_errors.append(f"scan_blocked:{blocked_reason}")

    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": existing.get("created_at") or now.isoformat(),
        "updated_at": now.isoformat(),
        "scan_plan_path": str(scan_plan_path),
        "scan_plan_sha256": scan_plan_sha256,
        "scan_window_start": scan_plan.get("window_start"),
        "scan_window_end": scan_plan.get("window_end"),
        "scan_snapshot": scan_plan,
        "expected_candidates": expected_rows,
        "candidate_count": len(expected_rows),
        "downloaded_entries": downloaded_entries,
        "downloaded_files": [str(entry.get("filename") or "") for entry in downloaded_entries],
        "downloaded_count": len(downloaded_entries),
        "satisfied_entries": satisfied_entries,
        "satisfied_files": [str(entry.get("filename") or "") for entry in satisfied_entries],
        "satisfied_count": len(satisfied_entries),
        "archive_dirs": archive_dirs,
        "missing_candidates": missing_candidates,
        "missing_candidate_count": len(missing_candidates),
        "finalize_attempts": attempts,
        "invariant_errors": invariant_errors,
        "state_commit_eligible": not invariant_errors and not missing_candidates,
        "state_committed_at": existing.get("state_committed_at"),
    }
    if not dry_run:
        save_json(path, payload)
    return payload


def validate_pdf_file(path: Path) -> str | None:
    try:
        size_bytes = path.stat().st_size
    except OSError:
        return "stat_failed"

    if size_bytes <= 0:
        return "empty_file"

    try:
        with path.open("rb") as handle:
            header = handle.read(5)
    except OSError:
        return "read_failed"

    if not header.startswith(b"%PDF-"):
        return "missing_pdf_header"

    return None


def compute_pdf_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_report_id(pdf_sha256: str) -> str:
    return f"zsxq_{pdf_sha256[:16]}"


def is_library_pdf_root(archive_root: Path, library_root: Path | None) -> bool:
    if library_root is None:
        return False
    try:
        return archive_root.expanduser().resolve(strict=False) == (library_root / "pdfs").expanduser().resolve(strict=False)
    except OSError:
        return False


def library_pdf_path(pdf_root: Path, batch_dir_name: str, archive_name: str) -> Path:
    return pdf_root / batch_dir_name / archive_name


def existing_pdf_sha256_paths(pdf_root: Path) -> dict[str, Path]:
    if not pdf_root.exists():
        return {}
    paths_by_sha256: dict[str, Path] = {}
    for path in pdf_root.rglob("*.pdf"):
        if not path.is_file():
            continue
        try:
            paths_by_sha256.setdefault(compute_pdf_sha256(path), path)
        except OSError:
            continue
    return paths_by_sha256


def existing_pdf_sha256s(pdf_root: Path) -> set[str]:
    return set(existing_pdf_sha256_paths(pdf_root))


def try_record_event(db_path: Path, payload: dict[str, Any]) -> None:
    try:
        record_event(db_path, payload)
    except Exception:
        # Trace events are metadata only; they must not break archiving.
        pass


def discover_candidates(
    staging_dir: Path,
    extra_staging_dirs: list[Path] | None,
    allowed_extensions: set[str],
    keywords_payload: dict[str, Any],
    window_start: datetime,
    window_end: datetime,
    archive_root: Path,
    downloaded_after: datetime | None,
    downloaded_before: datetime | None,
    planned_filenames: set[str] | None = None,
) -> tuple[list[CandidateFile], list[dict[str, Any]]]:
    """Pick the files that belong to this run.

    A file is accepted only if it:
    - is in the download folder
    - has the right extension
    - is not a half-downloaded temp file
    - is not already archived before
    - falls inside the current time window
    """

    grouped_candidates: dict[str, list[CandidateFile]] = {}
    preferred_archive_names: dict[str, str] = {}
    rejected_files: list[dict[str, Any]] = []
    archived_names = {path.name for path in archive_root.rglob("*") if path.is_file()}
    archived_name_keys = {build_filename_key(name) for name in archived_names}
    source_roots = [staging_dir, *(extra_staging_dirs or [])]

    for source_priority, source_root in enumerate(source_roots):
        if not source_root.exists() or not source_root.is_dir():
            continue

        try:
            source_paths = list(source_root.iterdir())
        except PermissionError:
            rejected_files.append(
                {
                    "filename": source_root.name,
                    "reason": "permission_denied",
                    "source_paths": [str(source_root)],
                    "source_priority": source_priority,
                }
            )
            continue

        for path in source_paths:
            if not path.is_file():
                continue
            if path.suffix.lower() not in allowed_extensions:
                continue
            if path.name.startswith(".") or path.name.endswith(".crdownload"):
                continue
            # We use local mtime for final local file selection.
            # For explicit-history runs, downloaded_after binds archiving to "this run actually downloaded files".
            path_stat = path.stat()
            modified_at = datetime.fromtimestamp(path_stat.st_mtime).astimezone()
            lower_bound = downloaded_after if downloaded_after is not None else window_start
            upper_bound = downloaded_before if downloaded_before is not None else window_end
            if modified_at <= lower_bound or modified_at > upper_bound:
                continue

            match_result = match_title(path.stem, keywords_payload)
            filename_key = build_filename_key(path.name)
            if path.name in archived_names:
                continue
            if filename_key in archived_name_keys and (
                planned_filenames is None or path.name not in planned_filenames
            ):
                continue
            grouped_candidates.setdefault(filename_key, []).append(
                CandidateFile(
                    path=path,
                    archive_name=path.name,
                    modified_at=modified_at,
                    matched_keywords=match_result.matched_keywords,
                    match_rule=match_result.match_rule,
                    size_bytes=int(path_stat.st_size),
                    source_priority=source_priority,
                )
            )
            preferred_archive_names.setdefault(filename_key, path.name)

    accepted_candidates: list[CandidateFile] = []
    for filename_key, items in grouped_candidates.items():
        preferred_archive_name = preferred_archive_names.get(filename_key) or items[0].path.name
        validations = {item.path: validate_pdf_file(item.path) for item in items}
        best_candidate = max(
            items,
            key=lambda item: (
                validations[item.path] is None,
                item.size_bytes,
                item.modified_at.timestamp(),
                -item.source_priority,
            ),
        )
        validation_error = validations[best_candidate.path]
        if validation_error is not None:
            rejected_files.append(
                {
                    "filename": preferred_archive_name,
                    "reason": validation_error,
                    "source_paths": [str(item.path) for item in items],
                }
            )
            continue

        best_candidate.archive_name = preferred_archive_name
        accepted_candidates.append(best_candidate)

    accepted_candidates.sort(key=lambda item: item.modified_at)
    rejected_files.sort(key=lambda item: item["filename"])
    return accepted_candidates, rejected_files


def build_batch_dir_name(window_start: datetime, window_end: datetime) -> str:
    return (
        f"{window_start.strftime('%Y-%m-%d_%H-%M-%S')}"
        f"__to__"
        f"{window_end.strftime('%Y-%m-%d_%H-%M-%S')}"
    )


def archive_candidates(
    candidates: list[CandidateFile],
    archive_root: Path,
    batch_dir_name: str,
    dry_run: bool,
    library_root: Path | None = None,
    planned_rows: dict[str, dict[str, Any]] | None = None,
    satisfied_duplicates: list[dict[str, Any]] | None = None,
) -> tuple[Path, list[dict[str, Any]]]:
    """Move accepted files into one batch folder and build the manifest data."""

    use_library_layout = is_library_pdf_root(archive_root, library_root)
    batch_dir = archive_root / batch_dir_name
    manifest_entries: list[dict[str, Any]] = []
    archived_pdf_paths = existing_pdf_sha256_paths(archive_root) if use_library_layout else {}
    archived_pdf_sha256s = set(archived_pdf_paths)

    if not dry_run:
        batch_dir.mkdir(parents=True, exist_ok=True)

    for candidate in candidates:
        pdf_sha256 = compute_pdf_sha256(candidate.path)
        report_id = build_report_id(pdf_sha256)
        planned_row = (
            (planned_rows or {}).get(candidate.archive_name)
            or (planned_rows or {}).get(build_filename_key(candidate.archive_name))
            or {}
        )
        if use_library_layout:
            if pdf_sha256 in archived_pdf_sha256s:
                if satisfied_duplicates is not None and planned_row:
                    existing_path = archived_pdf_paths.get(pdf_sha256)
                    satisfied_duplicates.append(
                        {
                            "candidate_identity": candidate_identity(planned_row),
                            "source_file_id": planned_row.get("file_id"),
                            "source_topic_id": planned_row.get("topic_id"),
                            "filename": candidate.archive_name,
                            "source_path": str(candidate.path),
                            "pdf_sha256": pdf_sha256,
                            "disposition": "already_archived_content_duplicate",
                            "existing_archive_path": str(existing_path) if existing_path else "",
                            "matched_keywords": candidate.matched_keywords,
                            "match_rule": candidate.match_rule,
                        }
                    )
                continue
            destination = library_pdf_path(archive_root, batch_dir_name, candidate.archive_name)
        else:
            destination = batch_dir / candidate.archive_name
        manifest_entries.append(
            {
                "report_id": report_id,
                "pdf_sha256": pdf_sha256,
                "candidate_identity": candidate_identity(planned_row) if planned_row else f"filename:{build_filename_key(candidate.archive_name)}",
                "source_file_id": planned_row.get("file_id"),
                "source_topic_id": planned_row.get("topic_id"),
                "batch_id": batch_dir_name,
                "filename": candidate.archive_name,
                "title": Path(candidate.archive_name).stem,
                "source_path": str(candidate.path),
                "archive_path": str(destination),
                "library_pdf_path": str(destination) if use_library_layout else "",
                "path": str(destination),
                "modified_at": candidate.modified_at.isoformat(),
                "downloaded_at": candidate.modified_at.isoformat(),
                "matched_keywords": candidate.matched_keywords,
                "match_rule": candidate.match_rule,
            }
        )
        if not dry_run:
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(candidate.path), str(destination))
            archived_pdf_sha256s.add(pdf_sha256)
            archived_pdf_paths.setdefault(pdf_sha256, destination)
            if library_root is not None:
                try:
                    db_path = db_path_for_library(library_root)
                    upsert_report(
                        db_path,
                        {
                            "report_id": report_id,
                            "pdf_sha256": pdf_sha256,
                            "title": Path(candidate.archive_name).stem,
                            "batch_id": batch_dir_name,
                            "pdf_path": str(destination),
                            "downloaded_at": candidate.modified_at.isoformat(),
                            "index_status": "pdf_archived",
                        },
                    )
                    try_record_event(
                        db_path,
                        {
                            "report_id": report_id,
                            "pdf_sha256": pdf_sha256,
                            "batch_id": batch_dir_name,
                            "status": "downloaded",
                            "artifact_path": str(candidate.path),
                        },
                    )
                    try_record_event(
                        db_path,
                        {
                            "report_id": report_id,
                            "pdf_sha256": pdf_sha256,
                            "batch_id": batch_dir_name,
                            "status": "pdf_archived",
                            "artifact_path": str(destination),
                        },
                    )
                except Exception:
                    # The SQLite index is metadata only; it must not break archiving.
                    pass

    return batch_dir, manifest_entries


def update_state(
    state_path: Path,
    state: dict[str, Any],
    window_start: datetime,
    window_end: datetime,
    batch_dir: Path | None,
    manifest_entries: list[dict[str, Any]],
    dry_run: bool,
    *,
    archive_dirs: list[str] | None = None,
    run_id: str = "",
) -> None:
    """Write the state file used by the next run.

    The most important field is `last_successful_check_at`.
    The next default run starts from that time.
    """

    updated_state = dict(state)
    updated_state["last_window_start"] = window_start.isoformat()
    updated_state["last_window_end"] = window_end.isoformat()

    if dry_run:
        updated_state["last_run_summary"] = (
            f"Dry run: {len(manifest_entries)} file(s) would be archived "
            f"for window {window_start.isoformat()} -> {window_end.isoformat()}."
        )
    else:
        updated_state["last_successful_check_at"] = window_end.isoformat()
        updated_state["last_batch_dir"] = str(batch_dir) if batch_dir else None
        updated_state["last_batch_dirs"] = archive_dirs if archive_dirs is not None else ([str(batch_dir)] if batch_dir else [])
        updated_state["last_batch_file_count"] = len(manifest_entries)
        updated_state["last_batch_files"] = [entry["filename"] for entry in manifest_entries]
        if run_id:
            updated_state["last_run_id"] = run_id
        updated_state["last_run_summary"] = (
            f"Archived {len(manifest_entries)} file(s) for window "
            f"{window_start.isoformat()} -> {window_end.isoformat()}."
        )

    save_json(state_path, updated_state)


def main() -> int:
    # Step 1: read config, keywords, and previous state.
    args = parse_args()
    config = load_json(Path(args.config))
    keywords = load_persistent_config(Path(args.keywords))
    state = load_json(Path(args.state))

    # Step 2: decide the time window for this run.
    window_start = (
        parse_iso8601(args.window_start)
        if args.window_start
        else parse_iso8601(state["last_successful_check_at"])
    )
    window_end = parse_iso8601(args.window_end) if args.window_end else datetime.now().astimezone()
    downloaded_after = parse_iso8601(args.downloaded_after) if args.downloaded_after else None
    downloaded_before = parse_iso8601(args.downloaded_before) if args.downloaded_before else None

    if window_end < window_start:
        raise SystemExit("window_end must be later than or equal to window_start")
    if downloaded_after and downloaded_before and downloaded_before < downloaded_after:
        raise SystemExit("downloaded_before must be later than or equal to downloaded_after")

    run_values = [bool(args.run_id), bool(args.scan_plan), bool(args.run_manifest)]
    if any(run_values) and not all(run_values):
        raise SystemExit("--run-id, --scan-plan and --run-manifest must be provided together")
    run_mode = all(run_values)
    if run_mode:
        try:
            uuid.UUID(args.run_id)
        except (ValueError, AttributeError) as exc:
            raise SystemExit("--run-id must be a UUID") from exc
    if args.commit_state and not run_mode:
        raise SystemExit("--commit-state requires run-aware finalize arguments")
    if args.wait_seconds < 0 or args.wait_interval_seconds <= 0:
        raise SystemExit("wait durations must be positive")

    scan_plan_path = Path(args.scan_plan) if run_mode else None
    run_manifest_path = Path(args.run_manifest) if run_mode else None
    scan_plan = (
        load_scan_plan(
            scan_plan_path,
            window_start=window_start,
            window_end=window_end,
            run_manifest_path=run_manifest_path,
        )
        if scan_plan_path is not None
        else None
    )
    planned_filenames = (
        {
            str(row.get("filename") or row.get("name") or "").strip()
            for row in scan_plan.get("download_candidates") or []
            if isinstance(row, dict) and str(row.get("filename") or row.get("name") or "").strip()
        }
        if scan_plan is not None
        else None
    )

    download_settings = config["download_settings"]
    staging_dir = Path(download_settings["staging_dir"]).expanduser()
    extra_staging_dirs = [Path(value).expanduser() for value in download_settings.get("extra_staging_dirs", [])]
    archive_root = Path(download_settings["archive_root"]).expanduser()
    archive_root.mkdir(parents=True, exist_ok=True)
    research_library = config.get("research_library") or {}
    library_root_raw = str(research_library.get("root", "")).strip() if isinstance(research_library, dict) else ""
    library_root = Path(library_root_raw).expanduser() if library_root_raw else None

    # Step 3: collect one or more browser download micro-batches. In run mode,
    # the immutable scan plan is an allow-list; unrelated PDFs stay untouched.
    batch_dir: Path | None = None
    attempt_entries: list[dict[str, Any]] = []
    attempt_satisfied_entries: list[dict[str, Any]] = []
    all_rejected_files: list[dict[str, Any]] = []
    run_manifest: dict[str, Any] | None = None
    deadline = time.monotonic() + args.wait_seconds
    first_pass = True
    last_discovery_upper_bound = downloaded_before

    while True:
        discovery_upper_bound = downloaded_before
        if discovery_upper_bound is None and downloaded_after is not None:
            discovery_upper_bound = datetime.now().astimezone()
        last_discovery_upper_bound = discovery_upper_bound
        candidates, rejected_files = discover_candidates(
            staging_dir=staging_dir,
            extra_staging_dirs=extra_staging_dirs,
            allowed_extensions={ext.lower() for ext in download_settings["allowed_extensions"]},
            keywords_payload=keywords,
            window_start=window_start,
            window_end=window_end,
            archive_root=archive_root,
            downloaded_after=downloaded_after,
            downloaded_before=discovery_upper_bound,
            planned_filenames=planned_filenames,
        )
        planned_rows: dict[str, dict[str, Any]] = {}
        if scan_plan is not None:
            candidates, plan_rejections, planned_rows = filter_candidates_to_scan_plan(candidates, scan_plan)
            rejected_files.extend(plan_rejections)

        new_entries: list[dict[str, Any]] = []
        new_satisfied_entries: list[dict[str, Any]] = []
        if candidates:
            batch_dir_name = build_batch_dir_name(window_start, window_end)
            batch_dir, new_entries = archive_candidates(
                candidates=candidates,
                archive_root=archive_root,
                batch_dir_name=batch_dir_name,
                dry_run=args.dry_run,
                library_root=library_root,
                planned_rows=planned_rows,
                satisfied_duplicates=new_satisfied_entries,
            )
            if not args.dry_run:
                batch_manifest_path = batch_dir / "batch_manifest.json"
                old_batch = load_json(batch_manifest_path) if batch_manifest_path.exists() else {}
                merged_batch_entries = merge_manifest_entries(
                    old_batch.get("files") if isinstance(old_batch.get("files"), list) else [],
                    new_entries,
                )
                save_json(batch_manifest_path, {"files": merged_batch_entries})
                if library_root is not None:
                    save_json(
                        library_root / "batches" / batch_dir_name / "batch_manifest.json",
                        {
                            "batch_id": batch_dir_name,
                            "source_batch_dir": str(batch_dir),
                            "files": merged_batch_entries,
                        },
                    )

        attempt_entries.extend(new_entries)
        attempt_satisfied_entries.extend(new_satisfied_entries)
        if first_pass or new_entries or new_satisfied_entries:
            all_rejected_files.extend(rejected_files)
            if run_mode and scan_plan_path is not None and run_manifest_path is not None and scan_plan is not None:
                run_manifest = build_run_manifest(
                    path=run_manifest_path,
                    run_id=args.run_id,
                    scan_plan_path=scan_plan_path,
                    scan_plan=scan_plan,
                    attempt_entries=new_entries,
                    attempt_satisfied_entries=new_satisfied_entries,
                    batch_dir=batch_dir,
                    rejected_files=rejected_files,
                    now=datetime.now().astimezone(),
                    dry_run=args.dry_run,
                )
        first_pass = False

        if not run_mode or args.dry_run:
            break
        if run_manifest and bool(run_manifest.get("state_commit_eligible")):
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(min(args.wait_interval_seconds, max(deadline - time.monotonic(), 0.0)))

    if run_mode and run_manifest is None and run_manifest_path is not None:
        run_manifest = load_json(run_manifest_path) if run_manifest_path.exists() else None

    # Step 5: store the result so the next run knows where to continue.
    state_updated = False
    if run_mode and run_manifest is not None:
        aggregate_entries = run_manifest.get("downloaded_entries") or []
        aggregate_archive_dirs = run_manifest.get("archive_dirs") or []
        aggregate_batch_dir = Path(aggregate_archive_dirs[-1]) if aggregate_archive_dirs else None
        if args.commit_state and not args.skip_state_update and bool(run_manifest.get("state_commit_eligible")):
            update_state(
                state_path=Path(args.state),
                state=load_json(Path(args.state)),
                window_start=window_start,
                window_end=window_end,
                batch_dir=aggregate_batch_dir,
                manifest_entries=aggregate_entries,
                dry_run=args.dry_run,
                archive_dirs=[str(value) for value in aggregate_archive_dirs],
                run_id=args.run_id,
            )
            state_updated = not args.dry_run
            if state_updated and run_manifest_path is not None:
                run_manifest["state_committed_at"] = datetime.now().astimezone().isoformat()
                run_manifest["updated_at"] = run_manifest["state_committed_at"]
                save_json(run_manifest_path, run_manifest)
    elif not args.skip_state_update:
        update_state(
            state_path=Path(args.state),
            state=state,
            window_start=window_start,
            window_end=window_end,
            batch_dir=batch_dir,
            manifest_entries=attempt_entries,
            dry_run=args.dry_run,
        )
        state_updated = not args.dry_run

    # Step 6: print a short machine-readable summary for logs and callers.
    aggregate_entries = (
        run_manifest.get("downloaded_entries")
        if run_manifest is not None and isinstance(run_manifest.get("downloaded_entries"), list)
        else attempt_entries
    )
    archive_dirs = (
        run_manifest.get("archive_dirs")
        if run_manifest is not None and isinstance(run_manifest.get("archive_dirs"), list)
        else ([str(batch_dir)] if batch_dir else [])
    )
    summary = {
        "schema_version": 2 if run_mode else 1,
        "run_id": args.run_id or None,
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "downloaded_after": downloaded_after.isoformat() if downloaded_after else None,
        "downloaded_before": last_discovery_upper_bound.isoformat() if last_discovery_upper_bound else None,
        "batch_dir": archive_dirs[-1] if archive_dirs else None,
        "archive_dirs": archive_dirs,
        "attempt_file_count": len(attempt_entries),
        "attempt_files": [entry["filename"] for entry in attempt_entries],
        "attempt_satisfied_count": len(attempt_satisfied_entries),
        "attempt_satisfied_files": [entry["filename"] for entry in attempt_satisfied_entries],
        "file_count": len(aggregate_entries),
        "files": [entry["filename"] for entry in aggregate_entries],
        "rejected_file_count": len(all_rejected_files),
        "rejected_files": all_rejected_files,
        "missing_candidate_count": int((run_manifest or {}).get("missing_candidate_count") or 0),
        "missing_candidates": (run_manifest or {}).get("missing_candidates") or [],
        "satisfied_candidate_count": int((run_manifest or {}).get("satisfied_count") or 0),
        "satisfied_candidates": (run_manifest or {}).get("satisfied_entries") or [],
        "state_commit_eligible": bool((run_manifest or {}).get("state_commit_eligible")) if run_mode else True,
        "state_updated": state_updated,
        "run_manifest_path": str(run_manifest_path) if run_manifest_path else None,
        "dry_run": args.dry_run,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.commit_state and run_mode and not state_updated:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
