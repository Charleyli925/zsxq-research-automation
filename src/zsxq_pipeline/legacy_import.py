"""Read-only migration planning for legacy JSON, JSONL, and library SQLite state.

The importer never edits a legacy source.  It first freezes a small, explicit
plan with source-file SHA256 values; ``apply`` verifies every value again
before it creates or changes the new pipeline database.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ._time import from_iso, to_iso_epoch, utc_now
from .model import ErrorCategory, PublicationState, Stage, StageState
from .state import PipelineState


IMPORT_PLAN_VERSION = 1
_KNOWN_JSON_NAMES = {
    "zsxq_foreign_reports_state.json",
    "zsxq_domestic_cicc_reports_state.json",
    "watch_state.json",
    "pending_batch.json",
    "run_status.json",
    "last_result.json",
    "stage_retry_ledger.json",
    "notification_outbox.json",
    "notification_state.json",
    "quarantine.json",
}
_KNOWN_JSONL_NAMES = {"notification_messages.jsonl", "publish_records.jsonl"}


class LegacyImportError(RuntimeError):
    """A legacy source cannot be read safely enough to form an import plan."""


class LegacySourceChangedError(LegacyImportError):
    """A source changed after its preview was frozen, so apply must not write."""


@dataclass(slots=True)
class ImportPlan:
    """Portable, audit-friendly migration data with no raw legacy payloads."""

    legacy_root: str
    generated_at: str
    source_files: list[dict[str, Any]] = field(default_factory=list)
    source_windows: list[dict[str, Any]] = field(default_factory=list)
    documents: list[dict[str, Any]] = field(default_factory=list)
    stage_attempts: list[dict[str, Any]] = field(default_factory=list)
    publications: list[dict[str, Any]] = field(default_factory=list)
    notifications: list[dict[str, Any]] = field(default_factory=list)
    conflicts: list[dict[str, str]] = field(default_factory=list)
    orphan_records: list[dict[str, str]] = field(default_factory=list)
    remote_written_without_success: list[dict[str, str]] = field(default_factory=list)
    unrecognized_paths: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_version": IMPORT_PLAN_VERSION,
            "legacy_root": self.legacy_root,
            "generated_at": self.generated_at,
            "source_files": self.source_files,
            "source_windows": self.source_windows,
            "documents": self.documents,
            "stage_attempts": self.stage_attempts,
            "publications": self.publications,
            "notifications": self.notifications,
            "conflicts": self.conflicts,
            "orphan_records": self.orphan_records,
            "remote_written_without_success": self.remote_written_without_success,
            "unrecognized_paths": self.unrecognized_paths,
            "summary": {
                "source_file_count": len(self.source_files),
                "source_window_count": len(self.source_windows),
                "document_count": len(self.documents),
                "stage_attempt_count": len(self.stage_attempts),
                "publication_count": len(self.publications),
                "notification_count": len(self.notifications),
                "conflict_count": len(self.conflicts),
                "orphan_record_count": len(self.orphan_records),
                "remote_written_without_success_count": len(self.remote_written_without_success),
                "unrecognized_path_count": len(self.unrecognized_paths),
            },
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ImportPlan":
        if int(payload.get("plan_version", 0)) != IMPORT_PLAN_VERSION:
            raise LegacyImportError("unsupported legacy import plan version")
        required_lists = (
            "source_files",
            "source_windows",
            "documents",
            "stage_attempts",
            "publications",
            "notifications",
            "conflicts",
            "orphan_records",
            "remote_written_without_success",
            "unrecognized_paths",
        )
        values: dict[str, list[dict[str, Any]]] = {}
        for name in required_lists:
            candidate = payload.get(name, [])
            if not isinstance(candidate, list) or not all(isinstance(item, dict) for item in candidate):
                raise LegacyImportError(f"plan field {name} must be a list of objects")
            values[name] = [dict(item) for item in candidate]
        root = str(payload.get("legacy_root", "")).strip()
        generated_at = str(payload.get("generated_at", "")).strip()
        if not root or not generated_at:
            raise LegacyImportError("plan has no legacy_root or generated_at")
        return cls(legacy_root=root, generated_at=generated_at, **values)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _valid_sha(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if len(text) == 64 and all(character in "0123456789abcdef" for character in text):
        return text
    return None


def _hash_identity(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _safe_text(value: Any) -> str:
    return str(value or "").strip()


def _safe_diagnostic(value: Any) -> str:
    """Keep useful non-secret diagnostics without echoing legacy credentials."""

    text = _safe_text(value)
    if not text:
        return ""
    lowered = text.lower()
    sensitive_markers = ("authorization", "bearer ", "password", "passwd", "secret", "api_key", "access_token")
    if any(marker in lowered for marker in sensitive_markers):
        return "legacy diagnostic redacted because it may contain sensitive data"
    return text[:512]


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _parse_legacy_time(value: Any) -> datetime | None:
    text = _safe_text(value)
    if not text:
        return None
    try:
        return from_iso(text)
    except (TypeError, ValueError):
        return None


def _source_hint(path: Path, payload: Mapping[str, Any] | None = None) -> str:
    explicit = _safe_text((payload or {}).get("source"))
    if explicit:
        return explicit
    lower = str(path).lower()
    if "domestic" in lower or "cicc" in lower:
        return "zsxq_domestic_cicc"
    if "foreign" in lower or "autodownload" in lower:
        return "zsxq_foreign"
    if "digest" in lower or path.name in {"watch_state.json", "pending_batch.json", "stage_retry_ledger.json"}:
        return "zsxq_digest"
    return "legacy_unknown"


def _document_key(source: str, source_file_id: str) -> str:
    return f"{source}\x1f{source_file_id}"


def _stage_key(record: Mapping[str, Any]) -> str:
    return "\x1f".join(
        [str(record["source"]), str(record["source_file_id"]), str(record["stage"]), str(record["workflow_version"])]
    )


def _publication_key(record: Mapping[str, Any]) -> str:
    return "\x1f".join([str(record["summary_sha256"]), str(record["target"]), str(record["partition_key"])])


class _Collector:
    def __init__(self, root: Path) -> None:
        generated_iso, _ = to_iso_epoch(utc_now())
        self.plan = ImportPlan(legacy_root=str(root), generated_at=generated_iso)
        self._windows: dict[str, dict[str, Any]] = {}
        self._documents: dict[str, dict[str, Any]] = {}
        self._stages: dict[str, dict[str, Any]] = {}
        self._publications: dict[str, dict[str, Any]] = {}
        self._notifications: dict[str, dict[str, Any]] = {}
        self._path_digests: dict[str, str] = {}

    def add_source_file(self, path: Path, sha256: str) -> None:
        self.plan.source_files.append({"path": str(path), "sha256": sha256})

    def unrecognized(self, path: Path, reason: str) -> None:
        self.plan.unrecognized_paths.append({"path": str(path), "reason": reason})

    def orphan(self, path: Path, kind: str, reason: str) -> None:
        self.plan.orphan_records.append({"path": str(path), "kind": kind, "reason": reason})

    def conflict(self, path: Path, kind: str, detail: str) -> None:
        self.plan.conflicts.append({"path": str(path), "kind": kind, "detail": detail})

    def add_window(self, source: str, start: Any, end: Any, *, status: str = "legacy") -> tuple[str, str] | None:
        start_at = _parse_legacy_time(start)
        end_at = _parse_legacy_time(end)
        if start_at is None or end_at is None:
            return None
        start_iso, start_epoch = to_iso_epoch(start_at)
        end_iso, end_epoch = to_iso_epoch(end_at)
        if end_epoch < start_epoch:
            return None
        record = {
            "source": source,
            "window_start": start_iso,
            "window_end": end_iso,
            "status": status,
            "checkpoint_eligible": True,
        }
        key = f"{source}\x1f{start_epoch}\x1f{end_epoch}"
        self._windows.setdefault(key, record)
        return start_iso, end_iso

    def add_document(
        self,
        path: Path,
        *,
        source: str,
        source_file_id: Any,
        filename: Any = "",
        source_path: Any = "",
        pdf_sha256: Any = None,
        window: tuple[Any, Any] | None = None,
    ) -> dict[str, Any] | None:
        source = _safe_text(source)
        file_id = _safe_text(source_file_id)
        if not source or not file_id:
            self.orphan(path, "document", "missing stable source or source_file_id")
            return None
        filename_text = _safe_text(filename)
        path_text = _safe_text(source_path)
        digest = _valid_sha(pdf_sha256)
        imported_window = self.add_window(source, window[0], window[1]) if window is not None else None
        record = {
            "source": source,
            "source_file_id": file_id,
            "filename": filename_text,
            "source_path": path_text,
            "pdf_sha256": digest,
            "window_start": imported_window[0] if imported_window else None,
            "window_end": imported_window[1] if imported_window else None,
        }
        if path_text and digest:
            prior_digest = self._path_digests.get(path_text)
            if prior_digest and prior_digest != digest:
                self.conflict(path, "path_content", f"{path_text} identifies more than one PDF SHA256")
                return None
            self._path_digests[path_text] = digest
        key = _document_key(source, file_id)
        existing = self._documents.get(key)
        if existing is not None:
            old_digest = _valid_sha(existing.get("pdf_sha256"))
            if old_digest and digest and old_digest != digest:
                self.conflict(path, "document_content", f"{source}:{file_id} identifies more than one PDF SHA256")
                return existing
            for name in ("filename", "source_path", "pdf_sha256", "window_start", "window_end"):
                if not existing.get(name) and record.get(name):
                    existing[name] = record[name]
            return existing
        self._documents[key] = record
        return record

    def add_stage(
        self,
        path: Path,
        *,
        source: str,
        source_file_id: str,
        stage: str,
        workflow_version: str,
        state: StageState,
        error_category: ErrorCategory | None = None,
        error_code: str = "",
        error_detail: str = "",
        attempt_count: int = 0,
        available_at: str | None = None,
    ) -> None:
        if stage not in {item.value for item in Stage}:
            self.orphan(path, "stage_attempt", f"unsupported legacy stage: {stage}")
            return
        record = {
            "source": source,
            "source_file_id": source_file_id,
            "stage": stage,
            "workflow_version": workflow_version or "legacy-import",
            "state": state.value,
            "error_category": error_category.value if error_category else None,
            "error_code": _safe_text(error_code),
            "error_detail": _safe_text(error_detail),
            "attempt_count": _nonnegative_int(attempt_count),
            "available_at": available_at,
        }
        key = _stage_key(record)
        existing = self._stages.get(key)
        if existing is None:
            self._stages[key] = record
            return
        # Do not turn durable success into a pending retry during repeated
        # scans of overlapping legacy files.
        if existing["state"] == StageState.SUCCEEDED.value:
            return
        if record["state"] == StageState.SUCCEEDED.value:
            self._stages[key] = record
            return
        state_rank = {
            StageState.QUEUED.value: 1,
            StageState.RETRY_WAIT.value: 2,
            StageState.RUNNING.value: 3,
            StageState.QUARANTINED.value: 4,
            StageState.BLOCKED_AUTH.value: 5,
            StageState.BLOCKED_RELEASE.value: 6,
        }
        if state_rank[record["state"]] >= state_rank[existing["state"]]:
            self._stages[key] = record

    def add_publication(self, path: Path, record: dict[str, Any]) -> None:
        key = _publication_key(record)
        existing = self._publications.get(key)
        if existing is None:
            self._publications[key] = record
            return
        if (
            record.get("remote_reference")
            and existing.get("remote_reference")
            and record["remote_reference"] != existing["remote_reference"]
        ):
            self.conflict(path, "publication_remote_reference", f"publication {key} maps to more than one remote reference")
            return
        rank = {PublicationState.INTENT.value: 1, PublicationState.REMOTE_WRITTEN.value: 2, PublicationState.SUCCESS.value: 3}
        if rank[record["state"]] > rank[existing["state"]]:
            self._publications[key] = record
        elif record.get("remote_reference") and not existing.get("remote_reference"):
            existing["remote_reference"] = record["remote_reference"]

    def add_notification(self, path: Path, record: dict[str, Any]) -> None:
        key = _safe_text(record.get("idempotency_key"))
        if not key:
            self.orphan(path, "notification", "missing idempotency_key")
            return
        self._notifications.setdefault(key, record)

    def finish(self) -> ImportPlan:
        self.plan.source_windows = sorted(self._windows.values(), key=lambda item: (item["source"], item["window_start"]))
        self.plan.documents = sorted(self._documents.values(), key=lambda item: (item["source"], item["source_file_id"]))
        self.plan.stage_attempts = sorted(
            self._stages.values(), key=lambda item: (item["source"], item["source_file_id"], item["stage"], item["workflow_version"])
        )
        self.plan.publications = sorted(
            self._publications.values(), key=lambda item: (item["summary_sha256"], item["target"], item["partition_key"])
        )
        self.plan.notifications = sorted(self._notifications.values(), key=lambda item: item["idempotency_key"])
        successes = {
            _publication_key(record)
            for record in self.plan.publications
            if record["state"] == PublicationState.SUCCESS.value
        }
        self.plan.remote_written_without_success = [
            {
                "summary_sha256": str(record["summary_sha256"]),
                "target": str(record["target"]),
                "partition_key": str(record["partition_key"]),
                "remote_reference": str(record.get("remote_reference") or ""),
            }
            for record in self.plan.publications
            if record["state"] == PublicationState.REMOTE_WRITTEN.value and _publication_key(record) not in successes
        ]
        return self.plan


def _candidate_paths(root: Path) -> list[Path]:
    paths: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.name in _KNOWN_JSON_NAMES or path.name in _KNOWN_JSONL_NAMES or path.name == "processed_files.sqlite":
            paths.append(path)
            continue
        if path.suffix == ".json" and "zsxq_autodownload_runs" in path.parts:
            paths.append(path)
    return sorted(set(paths))


def _extract_file_id(item: Mapping[str, Any]) -> str:
    for field in ("source_file_id", "file_id", "report_id", "id"):
        value = _safe_text(item.get(field))
        if value:
            return value
    return ""


def _extract_pdf_sha(item: Mapping[str, Any]) -> str | None:
    for field in ("pdf_sha256", "file_sha256", "text_extract_cache_key", "sha256"):
        digest = _valid_sha(item.get(field))
        if digest:
            return digest
    return None


def _extract_filename(item: Mapping[str, Any]) -> str:
    return _safe_text(item.get("filename") or item.get("name") or Path(_safe_text(item.get("path"))).name)


def _legacy_document_from_item(
    collector: _Collector,
    path: Path,
    item: Mapping[str, Any],
    *,
    source: str,
    allow_path_identity: bool = False,
    window: tuple[Any, Any] | None = None,
) -> dict[str, Any] | None:
    digest = _extract_pdf_sha(item)
    file_id = _extract_file_id(item)
    source_path = _safe_text(item.get("archive_path") or item.get("path") or item.get("source_path"))
    if not file_id and allow_path_identity and (digest or source_path):
        file_id = f"legacy:{digest or _hash_identity(source_path)}"
    return collector.add_document(
        path,
        source=source,
        source_file_id=file_id,
        filename=_extract_filename(item),
        source_path=source_path,
        pdf_sha256=digest,
        window=window,
    )


def _parse_download_state(collector: _Collector, path: Path, payload: Mapping[str, Any]) -> None:
    source = _source_hint(path, payload)
    collector.add_window(source, payload.get("last_window_start"), payload.get("last_window_end"), status="legacy_checkpoint")


def _parse_run_manifest(collector: _Collector, path: Path, payload: Mapping[str, Any]) -> None:
    source = _source_hint(path, payload)
    window = (payload.get("window_start"), payload.get("window_end"))
    workflow = _safe_text(payload.get("workflow_version")) or "legacy-download"
    for field in ("downloaded_entries", "satisfied_entries"):
        for item in _as_list(payload.get(field)):
            if not isinstance(item, dict):
                collector.orphan(path, "download_manifest_entry", f"{field} contains a non-object")
                continue
            document = _legacy_document_from_item(collector, path, item, source=source, window=window)
            if document is not None:
                collector.add_stage(
                    path,
                    source=document["source"],
                    source_file_id=document["source_file_id"],
                    stage=Stage.DOWNLOAD.value,
                    workflow_version=workflow,
                    state=StageState.SUCCEEDED,
                )
    for item in _as_list(payload.get("missing_candidates")):
        if not isinstance(item, dict):
            continue
        document = _legacy_document_from_item(collector, path, item, source=source, window=window)
        if document is not None:
            collector.add_stage(
                path,
                source=document["source"],
                source_file_id=document["source_file_id"],
                stage=Stage.DOWNLOAD.value,
                workflow_version=workflow,
                state=StageState.BLOCKED_RELEASE,
                error_category=ErrorCategory.INVARIANT,
                error_code="legacy_missing_candidate",
                error_detail="legacy run manifest recorded a missing planned candidate",
            )


def _parse_watch_state(collector: _Collector, path: Path, payload: Mapping[str, Any]) -> None:
    source = _source_hint(path, payload)
    workflow = "legacy-digest"
    for field, stage_state in (("known_files", None), ("pending_files", StageState.QUEUED)):
        raw_entries = payload.get(field)
        entries: Iterable[tuple[str, Any]]
        if isinstance(raw_entries, dict):
            entries = raw_entries.items()
        elif isinstance(raw_entries, list):
            entries = (("", item) for item in raw_entries)
        else:
            continue
        for key, value in entries:
            item = _as_dict(value)
            if key and not item.get("path"):
                item["path"] = key
            document = _legacy_document_from_item(
                collector, path, item, source=source, allow_path_identity=True
            )
            if document is not None and stage_state is not None:
                collector.add_stage(
                    path,
                    source=document["source"],
                    source_file_id=document["source_file_id"],
                    stage=Stage.TEXT_EXTRACT.value,
                    workflow_version=workflow,
                    state=stage_state,
                )


def _parse_pending_batch(collector: _Collector, path: Path, payload: Mapping[str, Any]) -> None:
    source = _source_hint(path, payload)
    workflow = _safe_text(payload.get("workflow_version")) or "legacy-digest"
    for item in _as_list(payload.get("files")):
        if not isinstance(item, dict):
            collector.orphan(path, "pending_batch", "files contains a non-object")
            continue
        document = _legacy_document_from_item(collector, path, item, source=source, allow_path_identity=True)
        if document is not None:
            collector.add_stage(
                path,
                source=document["source"],
                source_file_id=document["source_file_id"],
                stage=Stage.TEXT_EXTRACT.value,
                workflow_version=workflow,
                state=StageState.QUEUED,
            )


def _legacy_stage_outcome(entry: Mapping[str, Any]) -> tuple[StageState, ErrorCategory | None, str | None]:
    status = _safe_text(entry.get("status")).lower()
    error_code = _safe_text(entry.get("error_code"))
    error_type = _safe_text(entry.get("error_type")).lower()
    retryable = entry.get("retryable")
    if status in {"success", "succeeded", "resolved"}:
        return StageState.SUCCEEDED, None, None
    if status in {"recovery_released", "queued", "pending"}:
        return StageState.QUEUED, None, None
    if status in {"retry_exhausted", "blocked_release"} or error_code == "release_contract_mismatch":
        category = ErrorCategory.RELEASE_CONTRACT if error_code == "release_contract_mismatch" else ErrorCategory.INVARIANT
        return StageState.BLOCKED_RELEASE, category, None
    if "auth" in error_type or "auth" in error_code:
        return StageState.BLOCKED_AUTH, ErrorCategory.AUTH, None
    if "content" in error_type or status in {"quarantined", "blocked_content"}:
        return StageState.QUARANTINED, ErrorCategory.CONTENT, None
    next_retry = _safe_text(entry.get("next_retry_at"))
    if status in {"retry_wait", "retry_scheduled"} or retryable is True or str(retryable).lower() == "true":
        parsed = _parse_legacy_time(next_retry)
        if parsed is not None:
            retry_iso, _ = to_iso_epoch(parsed)
            return StageState.RETRY_WAIT, ErrorCategory.TRANSIENT, retry_iso
    # Unknown free text is terminal and explicitly invariant; guessing that it
    # is transient would resurrect a legacy error without evidence.
    return StageState.BLOCKED_RELEASE, ErrorCategory.INVARIANT, None


def _parse_retry_ledger(collector: _Collector, path: Path, payload: Mapping[str, Any]) -> None:
    source = _source_hint(path, payload)
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        collector.orphan(path, "retry_ledger", "entries is not an object")
        return
    for key, raw in entries.items():
        entry = _as_dict(raw)
        digest = _extract_pdf_sha(entry)
        source_path = _safe_text(entry.get("path"))
        file_id = _extract_file_id(entry) or (f"legacy:{digest or _hash_identity(source_path)}" if (digest or source_path) else "")
        document = collector.add_document(
            path,
            source=source,
            source_file_id=file_id,
            filename=_extract_filename(entry),
            source_path=source_path,
            pdf_sha256=digest,
        )
        if document is None:
            collector.orphan(path, "retry_ledger_entry", f"entry {key} has no file identity")
            continue
        stage = _safe_text(entry.get("stage")) or Stage.SUMMARY.value
        workflow = _safe_text(entry.get("workflow_version")) or "legacy-digest"
        state, category, available_at = _legacy_stage_outcome(entry)
        collector.add_stage(
            path,
            source=document["source"],
            source_file_id=document["source_file_id"],
            stage=stage,
            workflow_version=workflow,
            state=state,
            error_category=category,
            error_code=_safe_text(entry.get("error_code")),
            error_detail=_safe_diagnostic(entry.get("message") or entry.get("error")),
            attempt_count=_nonnegative_int(entry.get("attempt_count") or entry.get("attempts")),
            available_at=available_at,
        )


def _parse_quarantine(collector: _Collector, path: Path, payload: Mapping[str, Any]) -> None:
    source = _source_hint(path, payload)
    entries = payload.get("entries") or payload.get("items") or payload.get("files")
    if isinstance(entries, dict):
        iterable: Iterable[Any] = entries.values()
    else:
        iterable = _as_list(entries)
    for raw in iterable:
        entry = _as_dict(raw)
        document = _legacy_document_from_item(collector, path, entry, source=source, allow_path_identity=True)
        if document is None:
            continue
        stage = _safe_text(entry.get("stage")) or Stage.TEXT_EXTRACT.value
        collector.add_stage(
            path,
            source=document["source"],
            source_file_id=document["source_file_id"],
            stage=stage,
            workflow_version=_safe_text(entry.get("workflow_version")) or "legacy-digest",
            state=StageState.QUARANTINED,
            error_category=ErrorCategory.CONTENT,
            error_code=_safe_text(entry.get("reason_code")),
            error_detail=_safe_diagnostic(entry.get("reason") or entry.get("message")),
        )


def _parse_notification_outbox(collector: _Collector, path: Path, payload: Mapping[str, Any]) -> None:
    items = payload.get("items")
    if not isinstance(items, dict):
        collector.orphan(path, "notification_outbox", "items is not an object")
        return
    for key, raw in items.items():
        item = _as_dict(raw)
        collector.add_notification(
            path,
            {
                "idempotency_key": _safe_text(item.get("idempotency_key")) or _safe_text(key),
                "event": _safe_text(item.get("event")) or "legacy_notification",
                "status": _safe_text(item.get("status")) or "queued",
                "payload": {"legacy_source": path.name},
            },
        )


def _publication_from_record(collector: _Collector, path: Path, raw: Mapping[str, Any]) -> None:
    summary = _valid_sha(raw.get("summary_sha256") or raw.get("summary_hash"))
    if summary is None:
        collector.orphan(path, "publication", "missing summary SHA256")
        return
    target = _safe_text(raw.get("target") or raw.get("target_doc_url") or raw.get("doc_url"))
    partition = _safe_text(raw.get("partition_key") or raw.get("publish_key") or raw.get("batch_hash") or raw.get("report_date"))
    if not target or not partition:
        collector.orphan(path, "publication", "missing target or partition_key")
        return
    raw_status = _safe_text(raw.get("status")).lower()
    state = {
        "success": PublicationState.SUCCESS,
        "remote_written": PublicationState.REMOTE_WRITTEN,
        "intent": PublicationState.INTENT,
    }.get(raw_status)
    if state is None:
        if raw_status:
            collector.orphan(path, "publication", f"unsupported publication status: {raw_status}")
        return
    remote_reference = _safe_text(raw.get("doc_url") or raw.get("remote_reference"))
    if state is PublicationState.REMOTE_WRITTEN and not remote_reference:
        collector.orphan(path, "publication", "remote_written record has no remote reference")
        return
    collector.add_publication(
        path,
        {
            "summary_sha256": summary,
            "target": target,
            "partition_key": partition,
            "state": state.value,
            "remote_reference": remote_reference or None,
        },
    )


def _parse_json(collector: _Collector, path: Path, payload: Mapping[str, Any]) -> None:
    if path.name in {"zsxq_foreign_reports_state.json", "zsxq_domestic_cicc_reports_state.json"}:
        _parse_download_state(collector, path, payload)
    elif "zsxq_autodownload_runs" in path.parts:
        _parse_run_manifest(collector, path, payload)
    elif path.name == "watch_state.json":
        _parse_watch_state(collector, path, payload)
    elif path.name == "pending_batch.json":
        _parse_pending_batch(collector, path, payload)
    elif path.name == "stage_retry_ledger.json":
        _parse_retry_ledger(collector, path, payload)
    elif path.name == "notification_outbox.json":
        _parse_notification_outbox(collector, path, payload)
    elif path.name == "quarantine.json":
        _parse_quarantine(collector, path, payload)
    # run_status / last_result / notification_state are intentionally read for
    # the source hash only: they are snapshots, not an authoritative record.


def _parse_jsonl(collector: _Collector, path: Path) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError as exc:
        collector.unrecognized(path, f"cannot decode JSONL: {exc}")
        return
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            collector.orphan(path, "jsonl", f"line {line_number} is not valid JSON")
            continue
        if not isinstance(raw, dict):
            collector.orphan(path, "jsonl", f"line {line_number} is not an object")
            continue
        if path.name == "publish_records.jsonl":
            _publication_from_record(collector, path, raw)
        elif path.name == "notification_messages.jsonl":
            key = _safe_text(raw.get("idempotency_key"))
            if key:
                collector.add_notification(
                    path,
                    {
                        "idempotency_key": key,
                        "event": _safe_text(raw.get("event")) or "legacy_notification",
                        "status": "sent",
                        "payload": {"legacy_source": path.name},
                    },
                )


def _parse_processed_sqlite(collector: _Collector, path: Path) -> None:
    try:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        collector.unrecognized(path, f"cannot open SQLite read-only: {exc}")
        return
    try:
        has_reports = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reports'"
        ).fetchone()
        if has_reports is None:
            collector.unrecognized(path, "processed index has no reports table")
            return
        columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(reports)")}
        source_column = "source" if "source" in columns else None
        id_column = "report_id" if "report_id" in columns else None
        path_column = next((name for name in ("pdf_path", "path", "archive_path") if name in columns), None)
        sha_column = "pdf_sha256" if "pdf_sha256" in columns else None
        title_column = next((name for name in ("title", "filename", "report_title") if name in columns), None)
        if id_column is None:
            collector.unrecognized(path, "processed index reports table has no report_id")
            return
        selected = [id_column]
        for column in (source_column, path_column, sha_column, title_column):
            if column and column not in selected:
                selected.append(column)
        query = "SELECT " + ", ".join(f'"{column}"' for column in selected) + " FROM reports"
        for row in connection.execute(query):
            item = dict(row)
            collector.add_document(
                path,
                source=_safe_text(item.get(source_column)) if source_column else "research_library",
                source_file_id=_safe_text(item.get(id_column)),
                filename=_safe_text(item.get(title_column)) if title_column else "",
                source_path=_safe_text(item.get(path_column)) if path_column else "",
                pdf_sha256=item.get(sha_column) if sha_column else None,
            )
    except sqlite3.Error as exc:
        collector.unrecognized(path, f"cannot inspect processed index: {exc}")
    finally:
        connection.close()


def build_import_plan(legacy_root: str | Path) -> ImportPlan:
    """Create a side-effect-free plan from the supported legacy state files."""

    root = Path(legacy_root).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise LegacyImportError("legacy root must be a directory")
    collector = _Collector(root)
    for path in _candidate_paths(root):
        try:
            digest = _sha256_file(path)
        except OSError as exc:
            collector.unrecognized(path, f"cannot hash source: {exc}")
            continue
        collector.add_source_file(path, digest)
        if path.name == "processed_files.sqlite":
            _parse_processed_sqlite(collector, path)
            continue
        if path.suffix == ".jsonl":
            _parse_jsonl(collector, path)
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            collector.unrecognized(path, f"cannot parse JSON: {exc}")
            continue
        if not isinstance(payload, dict):
            collector.unrecognized(path, "JSON root is not an object")
            continue
        _parse_json(collector, path, payload)
    return collector.finish()


def write_import_plan(plan: ImportPlan, path: str | Path) -> None:
    """Persist a reviewable plan; this never touches legacy source or SQLite state."""

    output = Path(path).expanduser().resolve(strict=False)
    output.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(plan.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output.write_text(rendered, encoding="utf-8")


def load_import_plan(path: str | Path) -> ImportPlan:
    source = Path(path).expanduser().resolve(strict=True)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LegacyImportError(f"cannot read import plan: {exc}") from exc
    if not isinstance(payload, dict):
        raise LegacyImportError("import plan root is not an object")
    return ImportPlan.from_dict(payload)


def verify_plan_sources(plan: ImportPlan) -> None:
    """Fail before opening the destination DB if previewed legacy data changed."""

    for entry in plan.source_files:
        path = Path(str(entry.get("path", ""))).expanduser()
        expected = _valid_sha(entry.get("sha256"))
        if not path.is_file() or expected is None:
            raise LegacySourceChangedError("legacy import plan has a missing or invalid source snapshot")
        actual = _sha256_file(path)
        if actual != expected:
            raise LegacySourceChangedError(f"legacy source changed after plan: {path}")


def _window_lookup(plan: ImportPlan, state: PipelineState) -> dict[tuple[str, str, str], int]:
    result: dict[tuple[str, str, str], int] = {}
    for record in plan.source_windows:
        start = from_iso(str(record["window_start"]))
        end = from_iso(str(record["window_end"]))
        window_id = state.register_source_window(
            str(record["source"]),
            start,
            end,
            status=str(record.get("status") or "legacy"),
            checkpoint_eligible=bool(record.get("checkpoint_eligible")),
        )
        result[(str(record["source"]), str(record["window_start"]), str(record["window_end"]))] = window_id
    return result


def apply_import_plan(plan: ImportPlan, database: str | Path) -> dict[str, int]:
    """Apply an already-reviewed plan idempotently after rechecking all source hashes."""

    verify_plan_sources(plan)
    if plan.conflicts:
        raise LegacyImportError("legacy import plan contains conflicts; resolve them before applying")
    database_path = Path(database).expanduser().resolve(strict=False)
    document_ids: dict[str, int] = {}
    with PipelineState.open(database_path) as state:
        state.migrate()
        window_ids = _window_lookup(plan, state)
        for record in plan.documents:
            window_start = _safe_text(record.get("window_start"))
            window_end = _safe_text(record.get("window_end"))
            source_window_id = window_ids.get((str(record["source"]), window_start, window_end))
            document = state.upsert_document(
                str(record["source"]),
                str(record["source_file_id"]),
                filename=str(record.get("filename") or ""),
                source_path=str(record.get("source_path") or ""),
                source_window_id=source_window_id,
            )
            document_ids[_document_key(document.source, document.source_file_id)] = document.id
            digest = _valid_sha(record.get("pdf_sha256"))
            source_path = _safe_text(record.get("source_path"))
            if digest is not None and source_path:
                state.record_artifact(document.id, kind="pdf", path=source_path, pdf_sha256=digest)
        for record in plan.stage_attempts:
            key = _document_key(str(record["source"]), str(record["source_file_id"]))
            document_id = document_ids.get(key)
            if document_id is None:
                raise LegacyImportError(f"stage references absent document: {key}")
            available = _parse_legacy_time(record.get("available_at"))
            state.import_stage_attempt(
                document_id,
                stage=str(record["stage"]),
                workflow_version=str(record["workflow_version"]),
                state=str(record["state"]),
                error_category=str(record["error_category"]) if record.get("error_category") else None,
                error_code=str(record.get("error_code") or ""),
                error_detail=str(record.get("error_detail") or ""),
                attempt_count=int(record.get("attempt_count") or 0),
                available_at=available,
            )
        publication_ids: dict[str, int] = {}
        for record in plan.publications:
            summary = str(record["summary_sha256"])
            target = str(record["target"])
            partition = str(record["partition_key"])
            publication = state.record_publication_intent(summary, target, partition)
            if str(record["state"]) in {PublicationState.REMOTE_WRITTEN.value, PublicationState.SUCCESS.value}:
                publication = state.record_remote_write(
                    summary,
                    target,
                    partition,
                    remote_reference=str(record.get("remote_reference") or ""),
                )
            if str(record["state"]) == PublicationState.SUCCESS.value:
                publication = state.complete_publication(summary, target, partition)
            publication_ids[_publication_key(record)] = publication.id
        for record in plan.notifications:
            notification = state.enqueue_notification(
                str(record["idempotency_key"]),
                event=str(record["event"]),
                payload=_as_dict(record.get("payload")),
            )
            status = _safe_text(record.get("status"))
            if status and status != "queued":
                state.set_notification_status(notification.idempotency_key, status)
        return {
            "source_windows": state.table_count("source_windows"),
            "documents": state.table_count("documents"),
            "artifacts": state.table_count("artifacts"),
            "stage_attempts": state.table_count("stage_attempts"),
            "publications": state.table_count("publications"),
            "notification_outbox": state.table_count("notification_outbox"),
        }
