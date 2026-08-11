"""Transactional API for the pipeline's single SQLite state authority.

Workers use this module instead of assembling SQL or writing parallel JSON
checkpoints.  The API is intentionally small and every mutating operation
uses a short ``BEGIN IMMEDIATE`` transaction.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
import uuid
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Self

from . import SCHEMA_VERSION
from ._time import from_iso, require_aware, to_iso_epoch, utc_now
from .model import (
    ArtifactRecord,
    DocumentRecord,
    ErrorCategory,
    NotificationClaim,
    NotificationRecord,
    PipelineHealth,
    PublicationRecord,
    PublicationState,
    Stage,
    StageClaim,
    StageState,
    SummaryIdentity,
    as_error_category,
    as_stage,
    as_stage_state,
    canonical_json_value,
)
from .schema import assert_compatible, connect, installed_schema_version, migrate


class StateError(RuntimeError):
    """Base class for a state transition rejected by the durable contract."""


class StateNotMigratedError(StateError):
    """The caller attempted business work before ``db migrate`` completed."""


class InvariantViolation(StateError):
    """A unique identity or monotonic transition would be violated."""


class LeaseLostError(StateError):
    """A worker attempted to finish work after its exclusive lease was lost."""


class UnknownDocumentError(StateError):
    """A stage or artifact references a document that does not exist."""


_SHA256_LENGTH = 64


def _json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(canonical_json_value(dict(value or {})), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _normalize_filename(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", Path(value).stem)
    compact = "".join(character.casefold() for character in normalized if character.isalnum())
    return compact or normalized.casefold()


def _canonical_path(value: str | Path) -> str:
    raw = str(value).strip()
    if not raw:
        return ""
    return str(Path(raw).expanduser().resolve(strict=False))


def _validate_sha256(value: str | None, *, field: str, required: bool = False) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        if required:
            raise ValueError(f"{field} is required")
        return None
    if len(text) != _SHA256_LENGTH or any(character not in "0123456789abcdef" for character in text):
        raise ValueError(f"{field} must be a lowercase SHA256 digest")
    return text


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _timestamp(value: datetime | None) -> tuple[datetime, str, int]:
    instant = require_aware(value) if value is not None else utc_now()
    iso, epoch = to_iso_epoch(instant)
    return instant, iso, epoch


def _stage_retry_fingerprint(row: Mapping[str, Any]) -> str:
    """Bind a human-reviewed retry plan to one exact durable row state."""

    fields = {
        "attempt_count": int(row["attempt_count"]),
        "document_id": int(row["document_id"]),
        "error_category": str(row["error_category"] or ""),
        "error_code": str(row["error_code"] or ""),
        "id": int(row["id"]),
        "state": str(row["state"]),
        "updated_at_epoch": int(row["updated_at_epoch"]),
        "workflow_version": str(row["workflow_version"]),
    }
    encoded = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _row_document(row: sqlite3.Row) -> DocumentRecord:
    return DocumentRecord(
        id=int(row["id"]),
        source=str(row["source"]),
        source_file_id=str(row["source_file_id"]),
        filename=str(row["filename"]),
        normalized_filename=str(row["normalized_filename"]),
        source_path=str(row["source_path"]),
        artifact_id=int(row["artifact_id"]) if row["artifact_id"] is not None else None,
    )


def _row_artifact(row: sqlite3.Row) -> ArtifactRecord:
    return ArtifactRecord(
        id=int(row["id"]),
        kind=str(row["kind"]),
        pdf_sha256=str(row["pdf_sha256"]) if row["pdf_sha256"] else None,
        content_sha256=str(row["content_sha256"]) if row["content_sha256"] else None,
        canonical_path=str(row["canonical_path"]),
        extractor_version=str(row["extractor_version"]),
        prompt_version=str(row["prompt_version"]),
        model=str(row["model"]),
        reasoning=str(row["reasoning"]),
    )


def _row_publication(row: sqlite3.Row) -> PublicationRecord:
    try:
        decoded_details = json.loads(str(row["details_json"]))
    except (TypeError, ValueError):
        decoded_details = {}
    details = decoded_details if isinstance(decoded_details, dict) else {}
    return PublicationRecord(
        id=int(row["id"]),
        summary_sha256=str(row["summary_sha256"]),
        target=str(row["target"]),
        partition_key=str(row["partition_key"]),
        state=PublicationState(str(row["state"])),
        remote_reference=str(row["remote_reference"]) if row["remote_reference"] else None,
        target_document=str(row["target_document"]),
        details=details,
    )


def _row_notification(row: sqlite3.Row, *, created: bool) -> NotificationRecord:
    try:
        decoded = json.loads(str(row["payload_json"]))
        payload = decoded if isinstance(decoded, dict) else {}
    except (TypeError, ValueError):
        payload = {}
    return NotificationRecord(
        id=int(row["id"]),
        idempotency_key=str(row["idempotency_key"]),
        event=str(row["event"]),
        status=str(row["status"]),
        created=created,
        publication_id=int(row["publication_id"]) if row["publication_id"] is not None else None,
        payload=payload,
        attempt_count=int(row["attempt_count"]),
    )


def _summary_identity(
    pdf_sha256: str | SummaryIdentity,
    extractor_version: str | None = None,
    prompt_version: str | None = None,
    model: str | None = None,
    reasoning: str | None = None,
) -> SummaryIdentity:
    if isinstance(pdf_sha256, SummaryIdentity):
        if any(value is not None for value in (extractor_version, prompt_version, model, reasoning)):
            raise ValueError("do not combine SummaryIdentity with individual summary identity fields")
        return pdf_sha256
    return SummaryIdentity(
        pdf_sha256=_validate_sha256(pdf_sha256, field="pdf_sha256", required=True) or "",
        extractor_version=str(extractor_version or "").strip(),
        prompt_version=str(prompt_version or "").strip(),
        model=str(model or "").strip(),
        reasoning=str(reasoning or "").strip(),
    )


class PipelineState:
    """A local SQLite state store with explicit, monotonic transition methods."""

    def __init__(self, path: Path, connection: sqlite3.Connection) -> None:
        self.path = Path(path).expanduser().resolve(strict=False)
        self._connection = connection

    @classmethod
    def open(cls, path: str | Path) -> Self:
        """Open a database and reject a schema newer than this package supports.

        Opening an empty database is harmless; callers must explicitly call
        :meth:`migrate` before reading or writing pipeline entities.
        """

        resolved = Path(path).expanduser().resolve(strict=False)
        connection = connect(resolved)
        try:
            assert_compatible(connection)
        except Exception:
            connection.close()
            raise
        return cls(resolved, connection)

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    @property
    def schema_version(self) -> int:
        return installed_schema_version(self._connection)

    def migrate(self) -> int:
        return migrate(self._connection)

    def _require_migrated(self) -> None:
        version = assert_compatible(self._connection)
        if version != SCHEMA_VERSION:
            raise StateNotMigratedError(
                f"pipeline database schema is {version}; run `zsxq-pipeline db migrate` before state operations"
            )

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        self._require_migrated()
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self._connection.execute("COMMIT")
        except Exception:
            self._connection.execute("ROLLBACK")
            raise

    def register_source_window(
        self,
        source: str,
        window_start: datetime,
        window_end: datetime,
        *,
        status: str = "planned",
        checkpoint_eligible: bool = False,
        now: datetime | None = None,
    ) -> int:
        source = str(source).strip()
        if not source:
            raise ValueError("source is required")
        _, start_iso, start_epoch = _timestamp(window_start)
        _, end_iso, end_epoch = _timestamp(window_end)
        if end_epoch < start_epoch:
            raise ValueError("window_end must not be before window_start")
        _, now_iso, now_epoch = _timestamp(now)
        with self._write_transaction():
            self._connection.execute(
                """
                INSERT INTO source_windows(
                  source, window_start_iso, window_start_epoch, window_end_iso, window_end_epoch,
                  status, checkpoint_eligible, created_at_iso, created_at_epoch, updated_at_iso, updated_at_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, window_start_epoch, window_end_epoch) DO UPDATE SET
                  status=excluded.status,
                  checkpoint_eligible=excluded.checkpoint_eligible,
                  updated_at_iso=excluded.updated_at_iso,
                  updated_at_epoch=excluded.updated_at_epoch
                """,
                (
                    source,
                    start_iso,
                    start_epoch,
                    end_iso,
                    end_epoch,
                    str(status).strip() or "planned",
                    int(bool(checkpoint_eligible)),
                    now_iso,
                    now_epoch,
                    now_iso,
                    now_epoch,
                ),
            )
            row = self._connection.execute(
                "SELECT id FROM source_windows WHERE source = ? AND window_start_epoch = ? AND window_end_epoch = ?",
                (source, start_epoch, end_epoch),
            ).fetchone()
        assert row is not None
        return int(row["id"])

    def latest_source_checkpoint(self, source: str) -> datetime | None:
        """Return the last successfully committed endpoint for one source.

        This is the only scheduler-facing checkpoint lookup.  Task-local JSON
        mirrors may seed the first migration, but must not supersede this
        durable value once a source has completed a pipeline window.
        """

        self._require_migrated()
        normalized = str(source).strip()
        if not normalized:
            raise ValueError("source is required")
        row = self._connection.execute(
            """
            SELECT window_end_iso
            FROM source_windows
            WHERE source = ? AND checkpoint_eligible = 1
            ORDER BY window_end_epoch DESC, id DESC
            LIMIT 1
            """,
            (normalized,),
        ).fetchone()
        return from_iso(str(row["window_end_iso"])) if row is not None else None

    def get_schedule_cursor(self, source: str) -> datetime | None:
        """Return the latest configured slot durably enqueued for ``source``.

        This intentionally differs from :meth:`latest_source_checkpoint`.
        A source window may be scheduled but still be waiting for browser work;
        advancing this cursor prevents a second tick from creating another
        window for the same missed slots, while the business checkpoint still
        protects the actual source range until download reconciliation succeeds.
        """

        self._require_migrated()
        normalized = str(source).strip()
        if not normalized:
            raise ValueError("source is required")
        row = self._connection.execute(
            "SELECT cursor_iso FROM schedule_cursors WHERE source = ?", (normalized,)
        ).fetchone()
        return from_iso(str(row["cursor_iso"])) if row is not None else None

    def schedule_source_window(
        self,
        source: str,
        window_start: datetime,
        window_end: datetime,
        *,
        due_cursor: datetime,
        truncated: bool,
        now: datetime | None = None,
    ) -> int:
        """Atomically persist one scheduled window and advance its slot cursor.

        The cursor update happens in the same short transaction *after* the
        source-window upsert.  A process death before commit therefore leaves
        neither partial state nor a silently skipped clock slot.
        """

        normalized = str(source).strip()
        if not normalized:
            raise ValueError("source is required")
        _, start_iso, start_epoch = _timestamp(window_start)
        _, end_iso, end_epoch = _timestamp(window_end)
        _, cursor_iso, cursor_epoch = _timestamp(due_cursor)
        if end_epoch < start_epoch:
            raise ValueError("window_end must not be before window_start")
        if cursor_epoch > end_epoch:
            raise ValueError("due_cursor must not be after window_end")
        _, now_iso, now_epoch = _timestamp(now)
        with self._write_transaction():
            prior = self._connection.execute(
                "SELECT cursor_epoch FROM schedule_cursors WHERE source = ?", (normalized,)
            ).fetchone()
            if prior is not None and cursor_epoch < int(prior["cursor_epoch"]):
                raise InvariantViolation(
                    f"schedule cursor for {normalized!r} would move backward; refusing duplicate catch-up"
                )
            self._connection.execute(
                """
                INSERT INTO source_windows(
                  source, window_start_iso, window_start_epoch, window_end_iso, window_end_epoch,
                  status, checkpoint_eligible, created_at_iso, created_at_epoch, updated_at_iso, updated_at_epoch
                ) VALUES (?, ?, ?, ?, ?, 'scheduled', 0, ?, ?, ?, ?)
                ON CONFLICT(source, window_start_epoch, window_end_epoch) DO UPDATE SET
                  status=CASE
                    WHEN source_windows.status IN ('succeeded', 'partial', 'blocked', 'running') THEN source_windows.status
                    ELSE 'scheduled'
                  END,
                  updated_at_iso=excluded.updated_at_iso,
                  updated_at_epoch=excluded.updated_at_epoch
                """,
                (normalized, start_iso, start_epoch, end_iso, end_epoch, now_iso, now_epoch, now_iso, now_epoch),
            )
            row = self._connection.execute(
                "SELECT id FROM source_windows WHERE source=? AND window_start_epoch=? AND window_end_epoch=?",
                (normalized, start_epoch, end_epoch),
            ).fetchone()
            self._connection.execute(
                """
                INSERT INTO schedule_cursors(
                  source, cursor_iso, cursor_epoch,
                  last_window_start_iso, last_window_start_epoch,
                  last_window_end_iso, last_window_end_epoch, truncated,
                  created_at_iso, created_at_epoch, updated_at_iso, updated_at_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source) DO UPDATE SET
                  cursor_iso=excluded.cursor_iso,
                  cursor_epoch=excluded.cursor_epoch,
                  last_window_start_iso=excluded.last_window_start_iso,
                  last_window_start_epoch=excluded.last_window_start_epoch,
                  last_window_end_iso=excluded.last_window_end_iso,
                  last_window_end_epoch=excluded.last_window_end_epoch,
                  truncated=excluded.truncated,
                  updated_at_iso=excluded.updated_at_iso,
                  updated_at_epoch=excluded.updated_at_epoch
                """,
                (
                    normalized,
                    cursor_iso,
                    cursor_epoch,
                    start_iso,
                    start_epoch,
                    end_iso,
                    end_epoch,
                    int(bool(truncated)),
                    now_iso,
                    now_epoch,
                    now_iso,
                    now_epoch,
                ),
            )
        assert row is not None
        return int(row["id"])

    def list_source_windows(
        self,
        *,
        statuses: Iterable[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Read scheduled work without consulting task directories or PID files."""

        self._require_migrated()
        requested = tuple(sorted({str(value).strip() for value in (statuses or ()) if str(value).strip()}))
        clauses: list[str] = []
        parameters: list[Any] = []
        if requested:
            clauses.append("status IN (" + ", ".join("?" for _ in requested) + ")")
            parameters.extend(requested)
        query = "SELECT * FROM source_windows"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY window_end_epoch, id"
        if limit is not None:
            if int(limit) < 1:
                return []
            query += " LIMIT ?"
            parameters.append(int(limit))
        rows = self._connection.execute(query, tuple(parameters)).fetchall()
        return [dict(row) for row in rows]

    def list_documents_for_processing(
        self,
        source: str,
        *,
        extractor_workflow: str,
        limit: int,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Return a bounded, priority-ordered set of due PDF-backed documents.

        The processor owns the detailed workflow identities for summary and
        publication.  Only a missing next stage or the latest due runnable
        attempt is returned.  Downstream work is closed before new extraction
        so explicit recoveries cannot starve behind a source backlog. Terminal
        attempts stay visible for an explicit retry plan instead of being
        reported as fresh failures on every tick.
        """

        self._require_migrated()
        normalized = str(source).strip()
        workflow = str(extractor_workflow).strip()
        if not normalized or not workflow:
            raise ValueError("source and extractor_workflow are required")
        if int(limit) < 1:
            return []
        _, _, now_epoch = _timestamp(now)
        rows = self._connection.execute(
            """
            WITH runtime(now_epoch) AS (VALUES (?))
            SELECT d.id, d.source, d.source_file_id, d.source_window_id, d.filename, d.source_path,
                   a.pdf_sha256, a.canonical_path,
                   extract.state AS extract_state,
                   summary.state AS summary_state,
                   publish.state AS publish_state
            FROM documents d
            CROSS JOIN runtime
            JOIN artifacts a ON a.id=d.artifact_id AND a.kind='pdf'
            LEFT JOIN stage_attempts extract
              ON extract.document_id=d.id AND extract.stage='text_extract' AND extract.workflow_version=?
            LEFT JOIN stage_attempts summary
              ON summary.id=(
                SELECT s.id FROM stage_attempts s
                WHERE s.document_id=d.id AND s.stage='summary'
                ORDER BY s.id DESC LIMIT 1
              )
            LEFT JOIN stage_attempts publish
              ON publish.id=(
                SELECT p.id FROM stage_attempts p
                WHERE p.document_id=d.id AND p.stage='publish'
                ORDER BY p.id DESC LIMIT 1
              )
            WHERE d.source=?
              AND (
                extract.id IS NULL
                OR (
                  (extract.state='queued' AND (extract.available_at_epoch IS NULL OR extract.available_at_epoch <= runtime.now_epoch))
                  OR (extract.state='retry_wait' AND extract.available_at_epoch <= runtime.now_epoch)
                  OR (extract.state='running' AND (extract.lease_expires_at_epoch IS NULL OR extract.lease_expires_at_epoch <= runtime.now_epoch))
                )
                OR (
                  extract.state='succeeded'
                  AND (
                    summary.id IS NULL
                    OR (
                      (summary.state='queued' AND (summary.available_at_epoch IS NULL OR summary.available_at_epoch <= runtime.now_epoch))
                      OR (summary.state='retry_wait' AND summary.available_at_epoch <= runtime.now_epoch)
                      OR (summary.state='running' AND (summary.lease_expires_at_epoch IS NULL OR summary.lease_expires_at_epoch <= runtime.now_epoch))
                    )
                    OR (
                      summary.state='succeeded'
                      AND (
                        publish.id IS NULL
                        OR (publish.state='queued' AND (publish.available_at_epoch IS NULL OR publish.available_at_epoch <= runtime.now_epoch))
                        OR (publish.state='retry_wait' AND publish.available_at_epoch <= runtime.now_epoch)
                        OR (publish.state='running' AND (publish.lease_expires_at_epoch IS NULL OR publish.lease_expires_at_epoch <= runtime.now_epoch))
                      )
                    )
                  )
                )
              )
            ORDER BY
              CASE
                WHEN extract.state='succeeded' AND summary.state='succeeded' THEN 0
                WHEN extract.state='succeeded' THEN 1
                ELSE 2
              END,
              d.id
            LIMIT ?
            """,
            (now_epoch, workflow, normalized, int(limit)),
        ).fetchall()
        return [dict(row) for row in rows]

    def source_window_progress(self, source_window_id: int) -> dict[str, Any] | None:
        """Return exact PDF, summary, and publication counts for one source window.

        A scheduled source window is the durable user-facing cohort.  Counting
        its latest per-document stage rows avoids inventing progress from a
        worker invocation, which may process only a quota-bounded slice or be
        resumed after a crash.
        """

        self._require_migrated()
        window_id = int(source_window_id)
        window = self._connection.execute(
            "SELECT id, source FROM source_windows WHERE id=?", (window_id,)
        ).fetchone()
        if window is None:
            return None
        row = self._connection.execute(
            """
            WITH window_documents AS (
              SELECT documents.id
              FROM documents
              JOIN artifacts ON artifacts.id=documents.artifact_id AND artifacts.kind='pdf'
              WHERE documents.source_window_id=?
            ),
            latest AS (
              SELECT
                stage_attempts.document_id,
                stage_attempts.stage,
                stage_attempts.state,
                ROW_NUMBER() OVER (
                  PARTITION BY stage_attempts.document_id, stage_attempts.stage
                  ORDER BY stage_attempts.id DESC
                ) AS row_number
              FROM stage_attempts
              JOIN window_documents ON window_documents.id=stage_attempts.document_id
            )
            SELECT
              COUNT(window_documents.id) AS total,
              COALESCE(SUM(CASE WHEN summary_stage.state='succeeded' THEN 1 ELSE 0 END), 0) AS summarized,
              COALESCE(SUM(CASE WHEN publish_stage.state='succeeded' THEN 1 ELSE 0 END), 0) AS published,
              COALESCE(SUM(
                CASE WHEN
                  extract_stage.state IN ('blocked_auth', 'blocked_release', 'quarantined')
                  OR summary_stage.state IN ('blocked_auth', 'blocked_release', 'quarantined')
                  OR publish_stage.state IN ('blocked_auth', 'blocked_release', 'quarantined')
                THEN 1 ELSE 0 END
              ), 0) AS blocked
            FROM window_documents
            LEFT JOIN latest extract_stage
              ON extract_stage.document_id=window_documents.id
             AND extract_stage.stage='text_extract'
             AND extract_stage.row_number=1
            LEFT JOIN latest summary_stage
              ON summary_stage.document_id=window_documents.id
             AND summary_stage.stage='summary'
             AND summary_stage.row_number=1
            LEFT JOIN latest publish_stage
              ON publish_stage.document_id=window_documents.id
             AND publish_stage.stage='publish'
             AND publish_stage.row_number=1
            """,
            (window_id,),
        ).fetchone()
        assert row is not None
        return {
            "source_window_id": window_id,
            "source": str(window["source"]),
            "total": int(row["total"]),
            "summarized": int(row["summarized"]),
            "published": int(row["published"]),
            "blocked": int(row["blocked"]),
        }

    def upsert_document(
        self,
        source: str,
        source_file_id: str,
        *,
        filename: str = "",
        normalized_filename: str | None = None,
        source_path: str | Path = "",
        source_window_id: int | None = None,
        now: datetime | None = None,
    ) -> DocumentRecord:
        source = str(source).strip()
        source_file_id = str(source_file_id).strip()
        if not source or not source_file_id:
            raise ValueError("source and source_file_id are required")
        filename = str(filename).strip()
        normalized = str(normalized_filename).strip() if normalized_filename is not None else _normalize_filename(filename)
        canonical_source_path = _canonical_path(source_path)
        _, now_iso, now_epoch = _timestamp(now)
        with self._write_transaction():
            if source_window_id is not None:
                found_window = self._connection.execute(
                    "SELECT 1 FROM source_windows WHERE id = ?", (int(source_window_id),)
                ).fetchone()
                if found_window is None:
                    raise InvariantViolation(f"source window {source_window_id} does not exist")
            self._connection.execute(
                """
                INSERT INTO documents(
                  source, source_file_id, source_window_id, filename, normalized_filename, source_path,
                  created_at_iso, created_at_epoch, updated_at_iso, updated_at_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source, source_file_id) DO UPDATE SET
                  source_window_id=COALESCE(excluded.source_window_id, documents.source_window_id),
                  filename=CASE WHEN excluded.filename <> '' THEN excluded.filename ELSE documents.filename END,
                  normalized_filename=CASE
                    WHEN excluded.normalized_filename <> '' THEN excluded.normalized_filename
                    ELSE documents.normalized_filename
                  END,
                  source_path=CASE WHEN excluded.source_path <> '' THEN excluded.source_path ELSE documents.source_path END,
                  updated_at_iso=excluded.updated_at_iso,
                  updated_at_epoch=excluded.updated_at_epoch
                """,
                (
                    source,
                    source_file_id,
                    source_window_id,
                    filename,
                    normalized,
                    canonical_source_path,
                    now_iso,
                    now_epoch,
                    now_iso,
                    now_epoch,
                ),
            )
            row = self._connection.execute(
                "SELECT * FROM documents WHERE source = ? AND source_file_id = ?", (source, source_file_id)
            ).fetchone()
        assert row is not None
        return _row_document(row)

    def get_document(self, document_id: int) -> DocumentRecord:
        self._require_migrated()
        row = self._connection.execute("SELECT * FROM documents WHERE id = ?", (int(document_id),)).fetchone()
        if row is None:
            raise UnknownDocumentError(f"document {document_id} does not exist")
        return _row_document(row)

    def record_artifact(
        self,
        document_id: int,
        *,
        kind: str,
        path: str | Path,
        pdf_sha256: str | None = None,
        content_sha256: str | None = None,
        extractor_version: str = "",
        prompt_version: str = "",
        model: str = "",
        reasoning: str = "",
        size_bytes: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> ArtifactRecord:
        """Attach one artifact to a document without collapsing source identities.

        A PDF digest represents content identity and is unique.  Two source
        documents may reference it, but a single canonical path may never be
        silently reassigned to different content.
        """

        kind = str(kind).strip().lower()
        if not kind:
            raise ValueError("artifact kind is required")
        canonical_path = _canonical_path(path)
        if not canonical_path:
            raise ValueError("artifact path is required")
        materialized_path = Path(canonical_path)
        pdf_digest = _validate_sha256(pdf_sha256, field="pdf_sha256")
        content_digest = _validate_sha256(content_sha256, field="content_sha256")
        if kind == "pdf" and pdf_digest is None:
            if materialized_path.exists() and materialized_path.is_file():
                pdf_digest = _hash_file(materialized_path)
            else:
                raise ValueError("pdf artifacts require pdf_sha256 when the file is not available")
        if content_digest is None:
            content_digest = pdf_digest
        extractor_version = str(extractor_version).strip()
        prompt_version = str(prompt_version).strip()
        model = str(model).strip()
        reasoning = str(reasoning).strip()
        if kind == "summary" and (pdf_digest is None or not extractor_version or not prompt_version or not model):
            raise ValueError("summary artifacts require pdf_sha256, extractor_version, prompt_version, and model")
        summary_identity = (
            SummaryIdentity(pdf_digest or "", extractor_version, prompt_version, model, reasoning)
            if kind == "summary"
            else None
        )
        if size_bytes is not None and int(size_bytes) < 0:
            raise ValueError("size_bytes must not be negative")
        _, now_iso, now_epoch = _timestamp(now)
        metadata_value = dict(metadata or {})
        metadata_value["observed_paths"] = sorted(
            {str(item) for item in metadata_value.get("observed_paths", []) if str(item).strip()} | {canonical_path}
        )
        with self._write_transaction():
            document = self._connection.execute("SELECT * FROM documents WHERE id = ?", (int(document_id),)).fetchone()
            if document is None:
                raise UnknownDocumentError(f"document {document_id} does not exist")
            if kind == "pdf" and pdf_digest is not None:
                artifact = self._connection.execute(
                    "SELECT * FROM artifacts WHERE kind = 'pdf' AND pdf_sha256 = ?", (pdf_digest,)
                ).fetchone()
            elif kind == "summary" and pdf_digest is not None:
                artifact = self._connection.execute(
                    """
                    SELECT * FROM artifacts
                    WHERE kind = 'summary' AND pdf_sha256 = ? AND extractor_version = ?
                      AND prompt_version = ? AND model = ? AND reasoning = ?
                    """,
                    (
                        summary_identity.pdf_sha256 if summary_identity is not None else pdf_digest,
                        summary_identity.extractor_version if summary_identity is not None else extractor_version,
                        summary_identity.prompt_version if summary_identity is not None else prompt_version,
                        summary_identity.model if summary_identity is not None else model,
                        summary_identity.reasoning if summary_identity is not None else reasoning,
                    ),
                ).fetchone()
            else:
                artifact = self._connection.execute(
                    """
                    SELECT * FROM artifacts
                    WHERE kind = ? AND content_sha256 IS ? AND canonical_path = ?
                    ORDER BY id LIMIT 1
                    """,
                    (kind, content_digest, canonical_path),
                ).fetchone()
            if artifact is None:
                cursor = self._connection.execute(
                    """
                    INSERT INTO artifacts(
                      kind, pdf_sha256, content_sha256, extractor_version, prompt_version, model, reasoning,
                      canonical_path, size_bytes, metadata_json,
                      created_at_iso, created_at_epoch, updated_at_iso, updated_at_epoch
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        kind,
                        pdf_digest,
                        content_digest,
                        extractor_version,
                        prompt_version,
                        model,
                        reasoning,
                        canonical_path,
                        int(size_bytes) if size_bytes is not None else None,
                        _json(metadata_value),
                        now_iso,
                        now_epoch,
                        now_iso,
                        now_epoch,
                    ),
                )
                artifact_id = int(cursor.lastrowid)
                artifact = self._connection.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
            else:
                artifact_id = int(artifact["id"])
                if (
                    kind == "summary"
                    and content_digest is not None
                    and artifact["content_sha256"] is not None
                    and str(artifact["content_sha256"]) != content_digest
                ):
                    raise InvariantViolation(
                        "one summary identity produced conflicting content; create a new workflow version instead"
                    )
                existing_metadata: dict[str, Any]
                try:
                    decoded = json.loads(str(artifact["metadata_json"]))
                    existing_metadata = decoded if isinstance(decoded, dict) else {}
                except (TypeError, ValueError):
                    existing_metadata = {}
                observed = {str(item) for item in existing_metadata.get("observed_paths", []) if str(item).strip()}
                observed.update(metadata_value["observed_paths"])
                existing_metadata.update(metadata_value)
                existing_metadata["observed_paths"] = sorted(observed)
                self._connection.execute(
                    """
                    UPDATE artifacts SET content_sha256=COALESCE(content_sha256, ?),
                      size_bytes=COALESCE(size_bytes, ?), metadata_json=?, updated_at_iso=?, updated_at_epoch=?
                    WHERE id=?
                    """,
                    (content_digest, int(size_bytes) if size_bytes is not None else None, _json(existing_metadata), now_iso, now_epoch, artifact_id),
                )
                artifact = self._connection.execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()

            conflicts = self._connection.execute(
                """
                SELECT id FROM documents
                WHERE source_path = ? AND artifact_id IS NOT NULL AND artifact_id <> ?
                """,
                (canonical_path, artifact_id),
            ).fetchall()
            if conflicts:
                raise InvariantViolation(
                    f"path {canonical_path} already identifies different content; refusing silent artifact replacement"
                )
            current_artifact_id = int(document["artifact_id"]) if document["artifact_id"] is not None else None
            current_path = str(document["source_path"] or "")
            if current_artifact_id is not None and current_artifact_id != artifact_id and current_path == canonical_path:
                raise InvariantViolation(
                    f"document {document_id} would replace content at the same path without an explicit revision"
                )
            if kind == "pdf":
                self._connection.execute(
                    """
                    UPDATE documents SET artifact_id=?, source_path=?, updated_at_iso=?, updated_at_epoch=? WHERE id=?
                    """,
                    (artifact_id, canonical_path, now_iso, now_epoch, int(document_id)),
                )
        assert artifact is not None
        return _row_artifact(artifact)

    def find_summary_artifact(
        self,
        pdf_sha256: str | SummaryIdentity | None = None,
        extractor_version: str | None = None,
        prompt_version: str | None = None,
        model: str | None = None,
        reasoning: str | None = None,
        *,
        identity: SummaryIdentity | None = None,
    ) -> ArtifactRecord | None:
        """Find a reusable summary only when its complete invocation identity matches.

        Passing a :class:`~zsxq_pipeline.model.SummaryIdentity` is preferred;
        individual fields are retained for small adapters that do not need to
        construct a separate value first.  A different reasoning level is a
        deliberate cache miss.
        """

        if identity is not None:
            if pdf_sha256 is not None or any(value is not None for value in (extractor_version, prompt_version, model, reasoning)):
                raise ValueError("do not combine identity with individual summary identity fields")
            summary_identity = identity
        else:
            if pdf_sha256 is None:
                raise ValueError("summary identity is required")
            summary_identity = _summary_identity(pdf_sha256, extractor_version, prompt_version, model, reasoning)
        self._require_migrated()
        row = self._connection.execute(
            """
            SELECT * FROM artifacts
            WHERE kind = 'summary' AND pdf_sha256 = ? AND extractor_version = ?
              AND prompt_version = ? AND model = ? AND reasoning = ?
            ORDER BY id
            LIMIT 1
            """,
            (
                summary_identity.pdf_sha256,
                summary_identity.extractor_version,
                summary_identity.prompt_version,
                summary_identity.model,
                summary_identity.reasoning,
            ),
        ).fetchone()
        return _row_artifact(row) if row is not None else None

    def get_artifact(self, artifact_id: int) -> ArtifactRecord | None:
        """Return one durable artifact by primary key.

        Worker recovery uses this narrow lookup to rehydrate an already
        completed extraction without rerunning a non-idempotent external
        tool.  The caller still validates the artifact kind, identity, and
        local payload before treating it as usable.
        """

        self._require_migrated()
        row = self._connection.execute("SELECT * FROM artifacts WHERE id = ?", (int(artifact_id),)).fetchone()
        return _row_artifact(row) if row is not None else None

    def ensure_stage(
        self,
        document_id: int,
        stage: Stage | str,
        workflow_version: str,
        *,
        available_at: datetime | None = None,
        now: datetime | None = None,
    ) -> int:
        """Create one future stage output identity without overwriting an existing one."""

        normalized_stage = as_stage(stage)
        workflow_version = str(workflow_version).strip()
        if not workflow_version:
            raise ValueError("workflow_version is required")
        _, now_iso, now_epoch = _timestamp(now)
        available_iso: str | None = None
        available_epoch: int | None = None
        if available_at is not None:
            _, available_iso, available_epoch = _timestamp(available_at)
        with self._write_transaction():
            if self._connection.execute("SELECT 1 FROM documents WHERE id = ?", (int(document_id),)).fetchone() is None:
                raise UnknownDocumentError(f"document {document_id} does not exist")
            self._connection.execute(
                """
                INSERT INTO stage_attempts(
                  document_id, stage, workflow_version, state, available_at_iso, available_at_epoch,
                  created_at_iso, created_at_epoch, updated_at_iso, updated_at_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id, stage, workflow_version) DO NOTHING
                """,
                (
                    int(document_id),
                    normalized_stage.value,
                    workflow_version,
                    StageState.QUEUED.value,
                    available_iso,
                    available_epoch,
                    now_iso,
                    now_epoch,
                    now_iso,
                    now_epoch,
                ),
            )
            row = self._connection.execute(
                "SELECT id FROM stage_attempts WHERE document_id=? AND stage=? AND workflow_version=?",
                (int(document_id), normalized_stage.value, workflow_version),
            ).fetchone()
        assert row is not None
        return int(row["id"])

    def claim_due_stage(
        self,
        stage: Stage | str,
        workflow_version: str,
        *,
        document_ids: Iterable[int] | None = None,
        lease_seconds: int = 300,
        now: datetime | None = None,
    ) -> StageClaim | None:
        """Atomically claim one runnable stage, including expired crash leases.

        ``document_ids`` scopes a worker to the documents it actually owns.
        A batch worker must never claim, much less block, another batch's
        matching workflow row merely because both use the same stage version.
        """

        normalized_stage = as_stage(stage)
        workflow_version = str(workflow_version).strip()
        if not workflow_version:
            raise ValueError("workflow_version is required")
        if int(lease_seconds) <= 0:
            raise ValueError("lease_seconds must be positive")
        scoped_document_ids: tuple[int, ...] | None = None
        if document_ids is not None:
            scoped_document_ids = tuple(sorted({int(document_id) for document_id in document_ids}))
            if not scoped_document_ids:
                return None
        current, now_iso, now_epoch = _timestamp(now)
        expiry = current + timedelta(seconds=int(lease_seconds))
        _, expiry_iso, expiry_epoch = _timestamp(expiry)
        with self._write_transaction():
            self._connection.execute("DELETE FROM leases WHERE expires_at_epoch <= ?", (now_epoch,))
            document_scope = ""
            scope_parameters: tuple[int, ...] = ()
            if scoped_document_ids is not None:
                document_scope = " AND stage_attempts.document_id IN (" + ", ".join("?" for _ in scoped_document_ids) + ")"
                scope_parameters = scoped_document_ids
            row = self._connection.execute(
                """
                SELECT stage_attempts.*, documents.source, documents.source_file_id
                FROM stage_attempts
                JOIN documents ON documents.id = stage_attempts.document_id
                WHERE stage_attempts.stage = ? AND stage_attempts.workflow_version = ?
                """
                + document_scope
                + """
                  AND (
                    (stage_attempts.state = ? AND (stage_attempts.available_at_epoch IS NULL OR stage_attempts.available_at_epoch <= ?))
                    OR (stage_attempts.state = ? AND stage_attempts.available_at_epoch <= ?)
                    OR (stage_attempts.state = ? AND (
                      stage_attempts.lease_expires_at_epoch IS NULL OR stage_attempts.lease_expires_at_epoch <= ?
                    ))
                  )
                ORDER BY
                  CASE stage_attempts.state
                    WHEN 'queued' THEN 0
                    WHEN 'retry_wait' THEN 1
                    ELSE 2
                  END,
                  COALESCE(stage_attempts.available_at_epoch, 0),
                  stage_attempts.id
                LIMIT 1
                """,
                (
                    normalized_stage.value,
                    workflow_version,
                    *scope_parameters,
                    StageState.QUEUED.value,
                    now_epoch,
                    StageState.RETRY_WAIT.value,
                    now_epoch,
                    StageState.RUNNING.value,
                    now_epoch,
                ),
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            attempt_id = int(row["id"])
            self._connection.execute(
                """
                UPDATE stage_attempts
                SET state=?, attempt_count=attempt_count + 1, lease_token=?, lease_expires_at_iso=?,
                    lease_expires_at_epoch=?, updated_at_iso=?, updated_at_epoch=?
                WHERE id=?
                """,
                (
                    StageState.RUNNING.value,
                    token,
                    expiry_iso,
                    expiry_epoch,
                    now_iso,
                    now_epoch,
                    attempt_id,
                ),
            )
            self._connection.execute(
                """
                INSERT INTO leases(
                  lease_key, stage_attempt_id, owner_token, acquired_at_iso, acquired_at_epoch, expires_at_iso, expires_at_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(lease_key) DO UPDATE SET
                  stage_attempt_id=excluded.stage_attempt_id,
                  owner_token=excluded.owner_token,
                  acquired_at_iso=excluded.acquired_at_iso,
                  acquired_at_epoch=excluded.acquired_at_epoch,
                  expires_at_iso=excluded.expires_at_iso,
                  expires_at_epoch=excluded.expires_at_epoch
                """,
                (f"stage:{attempt_id}", attempt_id, token, now_iso, now_epoch, expiry_iso, expiry_epoch),
            )
            claimed = self._connection.execute("SELECT attempt_count FROM stage_attempts WHERE id = ?", (attempt_id,)).fetchone()
        assert claimed is not None
        return StageClaim(
            attempt_id=attempt_id,
            document_id=int(row["document_id"]),
            source=str(row["source"]),
            source_file_id=str(row["source_file_id"]),
            stage=normalized_stage,
            workflow_version=workflow_version,
            lease_token=token,
            claimed_at=current,
            lease_expires_at=expiry,
            attempt_count=int(claimed["attempt_count"]),
        )

    def _assert_claim(self, claim: StageClaim) -> sqlite3.Row:
        row = self._connection.execute("SELECT * FROM stage_attempts WHERE id = ?", (int(claim.attempt_id),)).fetchone()
        if row is None:
            raise LeaseLostError(f"stage attempt {claim.attempt_id} no longer exists")
        if str(row["state"]) != StageState.RUNNING.value or str(row["lease_token"] or "") != claim.lease_token:
            raise LeaseLostError(f"lease for stage attempt {claim.attempt_id} is no longer held by this worker")
        return row

    def complete_stage(
        self,
        claim: StageClaim,
        *,
        output_artifact_id: int | None = None,
        now: datetime | None = None,
    ) -> None:
        """Complete a claimed stage and release its lease in the same transaction."""

        _, now_iso, now_epoch = _timestamp(now)
        with self._write_transaction():
            self._assert_claim(claim)
            if output_artifact_id is not None:
                artifact = self._connection.execute("SELECT 1 FROM artifacts WHERE id = ?", (int(output_artifact_id),)).fetchone()
                if artifact is None:
                    raise InvariantViolation(f"output artifact {output_artifact_id} does not exist")
            self._connection.execute(
                """
                UPDATE stage_attempts SET state=?, error_category=NULL, error_code='', error_detail='',
                  available_at_iso=NULL, available_at_epoch=NULL, lease_token=NULL,
                  lease_expires_at_iso=NULL, lease_expires_at_epoch=NULL,
                  output_artifact_id=COALESCE(?, output_artifact_id), updated_at_iso=?, updated_at_epoch=?
                WHERE id=?
                """,
                (StageState.SUCCEEDED.value, output_artifact_id, now_iso, now_epoch, claim.attempt_id),
            )
            self._connection.execute("DELETE FROM leases WHERE stage_attempt_id = ?", (claim.attempt_id,))

    def fail_stage(
        self,
        claim: StageClaim,
        *,
        category: ErrorCategory | str,
        error_code: str = "",
        error_detail: str = "",
        retry_at: datetime | None = None,
        now: datetime | None = None,
    ) -> StageState:
        """Persist a category-driven failure outcome and release the worker lease."""

        normalized_category = as_error_category(category)
        if normalized_category is ErrorCategory.TRANSIENT:
            next_time = retry_at if retry_at is not None else now or utc_now()
            _, available_iso, available_epoch = _timestamp(next_time)
            next_state = StageState.RETRY_WAIT
        else:
            if retry_at is not None:
                raise ValueError("only transient failures may have retry_at")
            available_iso = None
            available_epoch = None
            next_state = {
                ErrorCategory.AUTH: StageState.BLOCKED_AUTH,
                ErrorCategory.RELEASE_CONTRACT: StageState.BLOCKED_RELEASE,
                ErrorCategory.CONTENT: StageState.QUARANTINED,
                ErrorCategory.INVARIANT: StageState.BLOCKED_RELEASE,
            }[normalized_category]
        _, now_iso, now_epoch = _timestamp(now)
        with self._write_transaction():
            self._assert_claim(claim)
            self._connection.execute(
                """
                UPDATE stage_attempts SET state=?, error_category=?, error_code=?, error_detail=?,
                  available_at_iso=?, available_at_epoch=?, lease_token=NULL,
                  lease_expires_at_iso=NULL, lease_expires_at_epoch=NULL,
                  updated_at_iso=?, updated_at_epoch=? WHERE id=?
                """,
                (
                    next_state.value,
                    normalized_category.value,
                    str(error_code).strip(),
                    str(error_detail).strip(),
                    available_iso,
                    available_epoch,
                    now_iso,
                    now_epoch,
                    claim.attempt_id,
                ),
            )
            self._connection.execute("DELETE FROM leases WHERE stage_attempt_id = ?", (claim.attempt_id,))
        return next_state

    def import_stage_attempt(
        self,
        document_id: int,
        *,
        stage: Stage | str,
        workflow_version: str,
        state: StageState | str,
        error_category: ErrorCategory | str | None = None,
        error_code: str = "",
        error_detail: str = "",
        attempt_count: int = 0,
        available_at: datetime | None = None,
        now: datetime | None = None,
    ) -> int:
        """Compatibility-only import path; normal workers use claim/complete/fail."""

        normalized_stage = as_stage(stage)
        normalized_state = as_stage_state(state)
        workflow_version = str(workflow_version).strip()
        if not workflow_version:
            raise ValueError("workflow_version is required")
        category = as_error_category(error_category).value if error_category is not None else None
        if normalized_state is StageState.RETRY_WAIT and available_at is None:
            raise ValueError("retry_wait imports require available_at")
        available_iso = available_epoch = None
        if available_at is not None:
            _, available_iso, available_epoch = _timestamp(available_at)
        _, now_iso, now_epoch = _timestamp(now)
        with self._write_transaction():
            if self._connection.execute("SELECT 1 FROM documents WHERE id = ?", (int(document_id),)).fetchone() is None:
                raise UnknownDocumentError(f"document {document_id} does not exist")
            self._connection.execute(
                """
                INSERT INTO stage_attempts(
                  document_id, stage, workflow_version, state, error_category, error_code, error_detail,
                  attempt_count, available_at_iso, available_at_epoch,
                  created_at_iso, created_at_epoch, updated_at_iso, updated_at_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id, stage, workflow_version) DO UPDATE SET
                  state=CASE
                    WHEN stage_attempts.state = 'succeeded' THEN stage_attempts.state
                    ELSE excluded.state
                  END,
                  error_category=CASE
                    WHEN stage_attempts.state = 'succeeded' THEN stage_attempts.error_category
                    ELSE excluded.error_category
                  END,
                  error_code=CASE WHEN stage_attempts.state = 'succeeded' THEN stage_attempts.error_code ELSE excluded.error_code END,
                  error_detail=CASE WHEN stage_attempts.state = 'succeeded' THEN stage_attempts.error_detail ELSE excluded.error_detail END,
                  attempt_count=MAX(stage_attempts.attempt_count, excluded.attempt_count),
                  available_at_iso=CASE WHEN stage_attempts.state = 'succeeded' THEN stage_attempts.available_at_iso ELSE excluded.available_at_iso END,
                  available_at_epoch=CASE WHEN stage_attempts.state = 'succeeded' THEN stage_attempts.available_at_epoch ELSE excluded.available_at_epoch END,
                  updated_at_iso=excluded.updated_at_iso,
                  updated_at_epoch=excluded.updated_at_epoch
                """,
                (
                    int(document_id),
                    normalized_stage.value,
                    workflow_version,
                    normalized_state.value,
                    category,
                    str(error_code).strip(),
                    str(error_detail).strip(),
                    max(0, int(attempt_count)),
                    available_iso,
                    available_epoch,
                    now_iso,
                    now_epoch,
                    now_iso,
                    now_epoch,
                ),
            )
            row = self._connection.execute(
                "SELECT id FROM stage_attempts WHERE document_id=? AND stage=? AND workflow_version=?",
                (int(document_id), normalized_stage.value, workflow_version),
            ).fetchone()
        assert row is not None
        return int(row["id"])

    def get_stage_attempt(self, document_id: int, stage: Stage | str, workflow_version: str) -> dict[str, Any] | None:
        self._require_migrated()
        row = self._connection.execute(
            "SELECT * FROM stage_attempts WHERE document_id=? AND stage=? AND workflow_version=?",
            (int(document_id), as_stage(stage).value, str(workflow_version)),
        ).fetchone()
        return dict(row) if row is not None else None

    def plan_stage_retry(
        self,
        *,
        stage: Stage | str,
        workflow_version: str,
        error_code: str,
    ) -> list[dict[str, Any]]:
        """Return exact terminal failure rows eligible for explicit recovery.

        This is intentionally read-only.  An operator must preserve the
        returned fingerprints in a retry plan and pass that plan to the
        separate apply operation; no broad "retry all" mutation exists.
        """

        self._require_migrated()
        normalized_stage = as_stage(stage).value
        workflow = str(workflow_version).strip()
        code = str(error_code).strip()
        if not workflow or not code:
            raise ValueError("workflow_version and error_code are required")
        rows = self._connection.execute(
            """
            SELECT id, document_id, stage, workflow_version, state, error_category,
                   error_code, error_detail, attempt_count, updated_at_epoch
            FROM stage_attempts
            WHERE stage=? AND workflow_version=? AND error_code=?
              AND state IN ('blocked_auth', 'blocked_release', 'quarantined')
            ORDER BY id
            """,
            (normalized_stage, workflow, code),
        ).fetchall()
        candidates: list[dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            candidates.append(
                {
                    "attempt_id": int(record["id"]),
                    "document_id": int(record["document_id"]),
                    "stage": str(record["stage"]),
                    "workflow_version": str(record["workflow_version"]),
                    "state": str(record["state"]),
                    "error_category": str(record["error_category"] or ""),
                    "error_code": str(record["error_code"]),
                    "attempt_count": int(record["attempt_count"]),
                    "updated_at_epoch": int(record["updated_at_epoch"]),
                    "fingerprint": _stage_retry_fingerprint(record),
                }
            )
        return candidates

    def apply_stage_retry_plan(
        self,
        candidates: Iterable[Mapping[str, Any]],
        *,
        plan_hash: str,
        expected_count: int,
        now: datetime | None = None,
    ) -> int:
        """Requeue exactly the reviewed candidates if none changed since plan.

        The operation preserves succeeded rows by construction and rejects any
        missing, added, or altered candidate before it writes the first row.
        This makes a field fix recoverable without abusing the legacy import
        API or converting unrelated terminal failures into automatic work.
        """

        normalized = tuple(dict(candidate) for candidate in candidates)
        if int(expected_count) != len(normalized):
            raise InvariantViolation("retry plan candidate count does not match --expected-count")
        if not normalized:
            return 0
        digest = str(plan_hash).strip().lower()
        if len(digest) != _SHA256_LENGTH or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("plan_hash must be a SHA256 digest")
        ids = [int(candidate.get("attempt_id", 0)) for candidate in normalized]
        if len(set(ids)) != len(ids) or any(identifier <= 0 for identifier in ids):
            raise InvariantViolation("retry plan contains invalid or duplicate stage attempt ids")
        _, now_iso, now_epoch = _timestamp(now)
        with self._write_transaction():
            placeholders = ", ".join("?" for _ in ids)
            rows = self._connection.execute(
                "SELECT * FROM stage_attempts WHERE id IN (" + placeholders + ") ORDER BY id", tuple(sorted(ids))
            ).fetchall()
            if len(rows) != len(normalized):
                raise InvariantViolation("a retry plan row no longer exists")
            expected_by_id = {int(candidate["attempt_id"]): candidate for candidate in normalized}
            for row in rows:
                record = dict(row)
                expected = expected_by_id[int(record["id"])]
                if (
                    str(record["state"]) not in {StageState.BLOCKED_AUTH.value, StageState.BLOCKED_RELEASE.value, StageState.QUARANTINED.value}
                    or _stage_retry_fingerprint(record) != str(expected.get("fingerprint", ""))
                    or int(record["document_id"]) != int(expected.get("document_id", -1))
                    or str(record["stage"]) != str(expected.get("stage", ""))
                    or str(record["workflow_version"]) != str(expected.get("workflow_version", ""))
                ):
                    raise InvariantViolation("a retry plan row changed after planning; create a new retry plan")
            self._connection.execute(
                """
                UPDATE stage_attempts
                SET state=?, error_category=NULL, error_code='manual_retry',
                    error_detail=?, available_at_iso=NULL, available_at_epoch=NULL,
                    lease_token=NULL, lease_expires_at_iso=NULL, lease_expires_at_epoch=NULL,
                    updated_at_iso=?, updated_at_epoch=?
                WHERE id IN ("""
                + placeholders
                + ")",
                (
                    StageState.QUEUED.value,
                    f"requeued by verified retry plan {digest}",
                    now_iso,
                    now_epoch,
                    *ids,
                ),
            )
        return len(normalized)

    def requeue_succeeded_stage(
        self,
        document_id: int,
        stage: Stage | str,
        workflow_version: str,
        *,
        reason: str,
        now: datetime | None = None,
    ) -> bool:
        """Reopen a success only when its required local artifact is missing.

        This narrow repair path is deliberately not a general retry API.  It
        prevents a stale ``succeeded`` stage from becoming permanently
        unclaimable after an artifact cache was lost, while retaining the
        diagnostic reason in the durable row.
        """

        self._require_migrated()
        _, now_iso, now_epoch = _timestamp(now)
        with self._write_transaction():
            cursor = self._connection.execute(
                """
                UPDATE stage_attempts
                SET state=?, error_category=NULL, error_code='artifact_recovery', error_detail=?,
                    available_at_iso=NULL, available_at_epoch=NULL,
                    lease_token=NULL, lease_expires_at_iso=NULL, lease_expires_at_epoch=NULL,
                    output_artifact_id=NULL, updated_at_iso=?, updated_at_epoch=?
                WHERE document_id=? AND stage=? AND workflow_version=? AND state=?
                """,
                (
                    StageState.QUEUED.value,
                    str(reason).strip() or "required local artifact is unavailable",
                    now_iso,
                    now_epoch,
                    int(document_id),
                    as_stage(stage).value,
                    str(workflow_version),
                    StageState.SUCCEEDED.value,
                ),
            )
        return cursor.rowcount == 1

    def record_publication_intent(
        self,
        summary_sha256: str,
        target: str,
        partition_key: str,
        *,
        target_document: str = "",
        details: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> PublicationRecord:
        summary_digest = _validate_sha256(summary_sha256, field="summary_sha256", required=True)
        target = str(target).strip()
        partition_key = str(partition_key).strip()
        target_document = str(target_document).strip()
        if not target or not partition_key:
            raise ValueError("target and partition_key are required")
        _, now_iso, now_epoch = _timestamp(now)
        with self._write_transaction():
            self._connection.execute(
                """
                INSERT INTO publications(
                  summary_sha256, target, target_document, partition_key, state, details_json,
                  created_at_iso, created_at_epoch, updated_at_iso, updated_at_epoch
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(summary_sha256, target, target_document, partition_key) DO NOTHING
                """,
                (
                    summary_digest,
                    target,
                    target_document,
                    partition_key,
                    PublicationState.INTENT.value,
                    _json(details),
                    now_iso,
                    now_epoch,
                    now_iso,
                    now_epoch,
                ),
            )
            row = self._connection.execute(
                "SELECT * FROM publications WHERE summary_sha256=? AND target=? AND target_document=? AND partition_key=?",
                (summary_digest, target, target_document, partition_key),
            ).fetchone()
        assert row is not None
        return _row_publication(row)

    def record_remote_write(
        self,
        summary_sha256: str,
        target: str,
        partition_key: str,
        *,
        remote_reference: str,
        target_document: str = "",
        details: Mapping[str, Any] | None = None,
        now: datetime | None = None,
    ) -> PublicationRecord:
        """Durably record a remote write before any local success acknowledgement."""

        remote_reference = str(remote_reference).strip()
        if not remote_reference:
            raise ValueError("remote_reference is required for remote_written")
        summary_digest = _validate_sha256(summary_sha256, field="summary_sha256", required=True)
        target = str(target).strip()
        partition_key = str(partition_key).strip()
        target_document = str(target_document).strip()
        if not target or not partition_key:
            raise ValueError("target and partition_key are required")
        _, now_iso, now_epoch = _timestamp(now)
        with self._write_transaction():
            row = self._connection.execute(
                """
                SELECT * FROM publications
                WHERE summary_sha256=? AND target=? AND target_document=? AND partition_key=?
                """,
                (summary_digest, target, target_document, partition_key),
            ).fetchone()
            if row is None and target_document:
                # A create intent cannot know its eventual document URL.  Bind
                # that pending v2 intent exactly once when the remote write
                # discovers the document, instead of making a second intent.
                pending = self._connection.execute(
                    """
                    SELECT * FROM publications
                    WHERE summary_sha256=? AND target=? AND target_document='' AND partition_key=?
                      AND state=?
                    """,
                    (summary_digest, target, partition_key, PublicationState.INTENT.value),
                ).fetchone()
                if pending is not None:
                    self._connection.execute(
                        """
                        UPDATE publications SET target_document=?, updated_at_iso=?, updated_at_epoch=?
                        WHERE id=?
                        """,
                        (target_document, now_iso, now_epoch, int(pending["id"])),
                    )
                    row = self._connection.execute("SELECT * FROM publications WHERE id=?", (int(pending["id"]),)).fetchone()
            if row is None:
                self._connection.execute(
                    """
                    INSERT INTO publications(
                      summary_sha256, target, target_document, partition_key, state, details_json,
                      created_at_iso, created_at_epoch, updated_at_iso, updated_at_epoch
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        summary_digest,
                        target,
                        target_document,
                        partition_key,
                        PublicationState.INTENT.value,
                        _json(details),
                        now_iso,
                        now_epoch,
                        now_iso,
                        now_epoch,
                    ),
                )
                row = self._connection.execute(
                    """
                    SELECT * FROM publications
                    WHERE summary_sha256=? AND target=? AND target_document=? AND partition_key=?
                    """,
                    (summary_digest, target, target_document, partition_key),
                ).fetchone()
            assert row is not None
            current = PublicationState(str(row["state"]))
            existing_reference = str(row["remote_reference"] or "")
            if current is PublicationState.SUCCESS:
                if existing_reference and existing_reference != remote_reference:
                    raise InvariantViolation("a completed publication cannot be rebound to another remote reference")
            elif current is PublicationState.REMOTE_WRITTEN:
                if existing_reference and existing_reference != remote_reference:
                    raise InvariantViolation("a remote-written publication cannot be rebound to another remote reference")
            else:
                self._connection.execute(
                    """
                    UPDATE publications SET state=?, remote_reference=?, details_json=?, updated_at_iso=?, updated_at_epoch=?
                    WHERE id=?
                    """,
                    (PublicationState.REMOTE_WRITTEN.value, remote_reference, _json(details), now_iso, now_epoch, int(row["id"])),
                )
            row = self._connection.execute("SELECT * FROM publications WHERE id = ?", (int(row["id"]),)).fetchone()
        assert row is not None
        return _row_publication(row)

    def complete_publication(
        self,
        summary_sha256: str,
        target: str,
        partition_key: str,
        *,
        target_document: str = "",
        now: datetime | None = None,
    ) -> PublicationRecord:
        """Commit local publication success only after a durable remote-written row exists."""

        summary_digest = _validate_sha256(summary_sha256, field="summary_sha256", required=True)
        target = str(target).strip()
        partition_key = str(partition_key).strip()
        target_document = str(target_document).strip()
        _, now_iso, now_epoch = _timestamp(now)
        with self._write_transaction():
            row = self._connection.execute(
                """
                SELECT * FROM publications
                WHERE summary_sha256=? AND target=? AND target_document=? AND partition_key=?
                """,
                (summary_digest, target, target_document, partition_key),
            ).fetchone()
            if row is None:
                raise InvariantViolation("cannot complete a publication without a remote_written record")
            current = PublicationState(str(row["state"]))
            if current is PublicationState.INTENT or not str(row["remote_reference"] or ""):
                raise InvariantViolation("cannot complete a publication before remote_written is durable")
            if current is not PublicationState.SUCCESS:
                self._connection.execute(
                    "UPDATE publications SET state=?, updated_at_iso=?, updated_at_epoch=? WHERE id=?",
                    (PublicationState.SUCCESS.value, now_iso, now_epoch, int(row["id"])),
                )
                row = self._connection.execute("SELECT * FROM publications WHERE id=?", (int(row["id"]),)).fetchone()
        assert row is not None
        return _row_publication(row)

    def get_publication(
        self,
        summary_sha256: str,
        target: str,
        partition_key: str,
        *,
        target_document: str = "",
    ) -> PublicationRecord | None:
        self._require_migrated()
        digest = _validate_sha256(summary_sha256, field="summary_sha256", required=True)
        target = str(target).strip()
        partition_key = str(partition_key).strip()
        target_document = str(target_document).strip()
        row = self._connection.execute(
            """
            SELECT * FROM publications
            WHERE summary_sha256=? AND target=? AND target_document=? AND partition_key=?
            """,
            (digest, target, target_document, partition_key),
        ).fetchone()
        return _row_publication(row) if row is not None else None

    def find_publications(
        self,
        summary_sha256: str,
        target: str,
        partition_key: str,
    ) -> tuple[PublicationRecord, ...]:
        """Return every publication variant for a recoverable logical partition.

        A caller that did not know the target document before create can use
        this recovery query, then resume only the matching `intent` or
        `remote_written` transaction without blindly writing another document.
        """

        self._require_migrated()
        digest = _validate_sha256(summary_sha256, field="summary_sha256", required=True)
        rows = self._connection.execute(
            """
            SELECT * FROM publications
            WHERE summary_sha256=? AND target=? AND partition_key=?
            ORDER BY id
            """,
            (digest, str(target).strip(), str(partition_key).strip()),
        ).fetchall()
        return tuple(_row_publication(row) for row in rows)

    def list_publications(
        self,
        *,
        target: str,
        states: Iterable[PublicationState | str] | None = None,
        partition_prefix: str = "",
    ) -> tuple[PublicationRecord, ...]:
        """List durable publications for bounded recovery/capacity decisions.

        Callers deliberately receive no mutable database rows.  ``partition``
        is a logical namespace (for example ``2026-08-10:``), rather than a
        filesystem location, so same-day capacity lookup remains deterministic
        after a runtime directory moves.
        """

        self._require_migrated()
        normalized_target = str(target).strip()
        if not normalized_target:
            raise ValueError("target is required")
        clauses = ["target=?"]
        parameters: list[Any] = [normalized_target]
        if partition_prefix:
            clauses.append("partition_key LIKE ?")
            parameters.append(f"{str(partition_prefix)}%")
        normalized_states = tuple(PublicationState(str(value)).value for value in (states or ()))
        if normalized_states:
            placeholders = ", ".join("?" for _ in normalized_states)
            clauses.append(f"state IN ({placeholders})")
            parameters.extend(normalized_states)
        rows = self._connection.execute(
            f"SELECT * FROM publications WHERE {' AND '.join(clauses)} "
            "ORDER BY updated_at_epoch DESC, id DESC",
            parameters,
        ).fetchall()
        return tuple(_row_publication(row) for row in rows)

    def enqueue_notification(
        self,
        idempotency_key: str,
        *,
        event: str,
        payload: Mapping[str, Any] | None = None,
        publication_id: int | None = None,
        now: datetime | None = None,
    ) -> NotificationRecord:
        key = str(idempotency_key).strip()
        event = str(event).strip()
        if not key or not event:
            raise ValueError("idempotency_key and event are required")
        _, now_iso, now_epoch = _timestamp(now)
        with self._write_transaction():
            if publication_id is not None and self._connection.execute(
                "SELECT 1 FROM publications WHERE id = ?", (int(publication_id),)
            ).fetchone() is None:
                raise InvariantViolation(f"publication {publication_id} does not exist")
            cursor = self._connection.execute(
                """
                INSERT INTO notification_outbox(
                  idempotency_key, publication_id, event, payload_json, status,
                  created_at_iso, created_at_epoch, updated_at_iso, updated_at_epoch
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (key, publication_id, event, _json(payload), now_iso, now_epoch, now_iso, now_epoch),
            )
            row = self._connection.execute(
                "SELECT * FROM notification_outbox WHERE idempotency_key = ?", (key,)
            ).fetchone()
        assert row is not None
        return _row_notification(row, created=cursor.rowcount == 1)

    def get_notification(self, idempotency_key: str) -> NotificationRecord | None:
        """Read an outbox item without changing its delivery state."""

        key = str(idempotency_key).strip()
        if not key:
            raise ValueError("idempotency_key is required")
        self._require_migrated()
        row = self._connection.execute(
            "SELECT * FROM notification_outbox WHERE idempotency_key=?", (key,)
        ).fetchone()
        return _row_notification(row, created=False) if row is not None else None

    def list_notifications(
        self,
        *,
        statuses: Iterable[str] | None = None,
        event_prefix: str | None = None,
        publication_id: int | None = None,
    ) -> tuple[NotificationRecord, ...]:
        """List durable outbox rows without claiming or changing any lease.

        Notification orchestration can use this to keep a terminal event behind
        an earlier document event for the same run.  The API deliberately
        exposes payloads but does not prescribe product-specific scope keys.
        """

        self._require_migrated()
        clauses: list[str] = []
        values: list[Any] = []
        if statuses is not None:
            raw_statuses = (statuses,) if isinstance(statuses, str) else statuses
            normalized_statuses = tuple(sorted({str(value).strip() for value in raw_statuses if str(value).strip()}))
            if not normalized_statuses:
                return ()
            clauses.append(f"status IN ({','.join('?' for _ in normalized_statuses)})")
            values.extend(normalized_statuses)
        if event_prefix is not None:
            prefix = str(event_prefix).strip()
            if not prefix:
                return ()
            clauses.append("event LIKE ? ESCAPE '\\'")
            values.append(prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if publication_id is not None:
            clauses.append("publication_id=?")
            values.append(int(publication_id))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM notification_outbox{where} ORDER BY created_at_epoch, id",
            tuple(values),
        ).fetchall()
        return tuple(_row_notification(row, created=False) for row in rows)

    def supersede_pending_notifications(
        self,
        *,
        events: Iterable[str],
        superseded_by: str,
        now: datetime | None = None,
    ) -> int:
        """Retire queued/retrying stale conclusions before a newer one lands."""

        names = tuple(sorted({str(event).strip() for event in events if str(event).strip()}))
        replacement = str(superseded_by).strip()
        if not names or not replacement:
            return 0
        _, now_iso, now_epoch = _timestamp(now)
        placeholders = ", ".join("?" for _ in names)
        with self._write_transaction():
            cursor = self._connection.execute(
                f"""
                UPDATE notification_outbox
                SET status='superseded', error_code='superseded', error_detail=?,
                    available_at_iso=NULL, available_at_epoch=NULL,
                    lease_token=NULL, lease_expires_at_iso=NULL, lease_expires_at_epoch=NULL,
                    updated_at_iso=?, updated_at_epoch=?
                WHERE event IN ({placeholders})
                  AND idempotency_key <> ?
                  AND status IN ('queued', 'pending', 'retry_wait')
                """,
                (f"superseded by {replacement}", now_iso, now_epoch, *names, replacement),
            )
        return int(cursor.rowcount)

    def set_notification_status(self, idempotency_key: str, status: str, *, now: datetime | None = None) -> None:
        """Compatibility importer transition; delivery workers will gain a dedicated API later."""

        key = str(idempotency_key).strip()
        if not key:
            raise ValueError("idempotency_key is required")
        _, now_iso, now_epoch = _timestamp(now)
        with self._write_transaction():
            cursor = self._connection.execute(
                "UPDATE notification_outbox SET status=?, updated_at_iso=?, updated_at_epoch=? WHERE idempotency_key=?",
                (str(status).strip() or "queued", now_iso, now_epoch, key),
            )
            if cursor.rowcount != 1:
                raise InvariantViolation(f"notification {key} does not exist")

    def claim_due_notification(
        self,
        *,
        lease_seconds: int = 300,
        now: datetime | None = None,
    ) -> NotificationClaim | None:
        """Atomically lease one due notification for a side-effecting sender.

        Delivery is intentionally independent from publication completion.  A
        crashed sender is reclaimable after its lease expires; a failed send is
        retried only when :meth:`fail_notification` supplies a retry time.
        Rows are FIFO within the same due class, so callers that enqueue a
        document notification before a terminal notification retain that order.
        """

        if int(lease_seconds) <= 0:
            raise ValueError("lease_seconds must be positive")
        current, now_iso, now_epoch = _timestamp(now)
        expiry = current + timedelta(seconds=int(lease_seconds))
        _, expiry_iso, expiry_epoch = _timestamp(expiry)
        with self._write_transaction():
            row = self._connection.execute(
                """
                SELECT * FROM notification_outbox
                WHERE (
                  status IN ('queued', 'pending')
                  AND (available_at_epoch IS NULL OR available_at_epoch <= ?)
                ) OR (
                  status = 'retry_wait'
                  AND (available_at_epoch IS NULL OR available_at_epoch <= ?)
                ) OR (
                  status = 'running'
                  AND (lease_expires_at_epoch IS NULL OR lease_expires_at_epoch <= ?)
                )
                ORDER BY
                  CASE status
                    WHEN 'queued' THEN 0
                    WHEN 'pending' THEN 0
                    WHEN 'retry_wait' THEN 1
                    ELSE 2
                  END,
                  COALESCE(available_at_epoch, created_at_epoch),
                  id
                LIMIT 1
                """,
                (now_epoch, now_epoch, now_epoch),
            ).fetchone()
            if row is None:
                return None
            token = uuid.uuid4().hex
            notification_id = int(row["id"])
            self._connection.execute(
                """
                UPDATE notification_outbox
                SET status='running', attempt_count=attempt_count + 1,
                    lease_token=?, lease_expires_at_iso=?, lease_expires_at_epoch=?,
                    updated_at_iso=?, updated_at_epoch=?
                WHERE id=?
                """,
                (token, expiry_iso, expiry_epoch, now_iso, now_epoch, notification_id),
            )
            row = self._connection.execute("SELECT * FROM notification_outbox WHERE id=?", (notification_id,)).fetchone()
        assert row is not None
        notification = _row_notification(row, created=False)
        return NotificationClaim(
            id=notification.id,
            idempotency_key=notification.idempotency_key,
            event=notification.event,
            payload=notification.payload,
            publication_id=notification.publication_id,
            lease_token=token,
            claimed_at=current,
            lease_expires_at=expiry,
            attempt_count=notification.attempt_count,
        )

    def _assert_notification_claim(self, claim: NotificationClaim) -> sqlite3.Row:
        row = self._connection.execute("SELECT * FROM notification_outbox WHERE id=?", (int(claim.id),)).fetchone()
        if row is None:
            raise LeaseLostError(f"notification {claim.idempotency_key} no longer exists")
        if str(row["status"]) != "running" or str(row["lease_token"] or "") != claim.lease_token:
            raise LeaseLostError(f"notification lease for {claim.idempotency_key} is no longer held by this worker")
        return row

    def complete_notification(self, claim: NotificationClaim, *, now: datetime | None = None) -> NotificationRecord:
        """Acknowledge a sent notification while holding its delivery lease."""

        _, now_iso, now_epoch = _timestamp(now)
        with self._write_transaction():
            self._assert_notification_claim(claim)
            self._connection.execute(
                """
                UPDATE notification_outbox
                SET status='sent', available_at_iso=NULL, available_at_epoch=NULL,
                    lease_token=NULL, lease_expires_at_iso=NULL, lease_expires_at_epoch=NULL,
                    error_code='', error_detail='', updated_at_iso=?, updated_at_epoch=?
                WHERE id=?
                """,
                (now_iso, now_epoch, int(claim.id)),
            )
            row = self._connection.execute("SELECT * FROM notification_outbox WHERE id=?", (int(claim.id),)).fetchone()
        assert row is not None
        return _row_notification(row, created=False)

    def fail_notification(
        self,
        claim: NotificationClaim,
        *,
        retry_at: datetime | None = None,
        error_code: str = "",
        error_detail: str = "",
        now: datetime | None = None,
    ) -> NotificationRecord:
        """Release a failed delivery lease, optionally making it retryable later."""

        if retry_at is not None:
            _, available_iso, available_epoch = _timestamp(retry_at)
            next_status = "retry_wait"
        else:
            available_iso = available_epoch = None
            next_status = "failed"
        _, now_iso, now_epoch = _timestamp(now)
        with self._write_transaction():
            self._assert_notification_claim(claim)
            self._connection.execute(
                """
                UPDATE notification_outbox
                SET status=?, available_at_iso=?, available_at_epoch=?,
                    lease_token=NULL, lease_expires_at_iso=NULL, lease_expires_at_epoch=NULL,
                    error_code=?, error_detail=?, updated_at_iso=?, updated_at_epoch=?
                WHERE id=?
                """,
                (
                    next_status,
                    available_iso,
                    available_epoch,
                    str(error_code).strip(),
                    str(error_detail).strip(),
                    now_iso,
                    now_epoch,
                    int(claim.id),
                ),
            )
            row = self._connection.execute("SELECT * FROM notification_outbox WHERE id=?", (int(claim.id),)).fetchone()
        assert row is not None
        return _row_notification(row, created=False)

    def derive_health(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Derive operational health from all durable stage rows, never a last exit code."""

        self._require_migrated()
        _, now_iso, now_epoch = _timestamp(now)
        source_rows = self._connection.execute(
            "SELECT source FROM documents UNION SELECT source FROM source_windows ORDER BY source"
        ).fetchall()
        sources: dict[str, dict[str, Any]] = {
            str(row["source"]): {"stages": {}, "totals": _empty_counts()} for row in source_rows
        }
        window_rows = self._connection.execute(
            """
            SELECT source, status, checkpoint_eligible, window_end_iso, window_end_epoch
            FROM source_windows
            ORDER BY source, window_end_epoch DESC, id DESC
            """
        ).fetchall()
        for row in window_rows:
            source = str(row["source"])
            payload = sources.setdefault(source, {"stages": {}, "totals": _empty_counts()})
            windows = payload.setdefault("windows", {"counts": {}, "latest": None, "latest_checkpoint": None})
            status = str(row["status"])
            counts = windows["counts"]
            counts[status] = int(counts.get(status, 0)) + 1
            if windows["latest"] is None:
                windows["latest"] = {"status": status, "window_end": str(row["window_end_iso"])}
            if bool(row["checkpoint_eligible"]) and windows["latest_checkpoint"] is None:
                windows["latest_checkpoint"] = str(row["window_end_iso"])
        cursor_rows = self._connection.execute(
            """
            SELECT source, cursor_iso, last_window_end_iso, truncated
            FROM schedule_cursors ORDER BY source
            """
        ).fetchall()
        for row in cursor_rows:
            source = str(row["source"])
            payload = sources.setdefault(source, {"stages": {}, "totals": _empty_counts()})
            payload["schedule"] = {
                "cursor": str(row["cursor_iso"]),
                "last_window_end": str(row["last_window_end_iso"]),
                "truncated": bool(row["truncated"]),
            }
        rows = self._connection.execute(
            """
            SELECT documents.source, stage_attempts.stage, stage_attempts.state,
              stage_attempts.available_at_iso, stage_attempts.available_at_epoch,
              stage_attempts.lease_expires_at_epoch
            FROM stage_attempts JOIN documents ON documents.id = stage_attempts.document_id
            ORDER BY documents.source, stage_attempts.stage
            """
        ).fetchall()
        blocked_count = 0
        degraded_count = 0
        for row in rows:
            source = str(row["source"])
            stage = str(row["stage"])
            state = StageState(str(row["state"]))
            bucket = sources.setdefault(source, {"stages": {}, "totals": _empty_counts()})["stages"].setdefault(
                stage, _empty_counts()
            )
            _increment_count(bucket, state)
            _increment_count(sources[source]["totals"], state)
            if state in {StageState.BLOCKED_AUTH, StageState.BLOCKED_RELEASE}:
                blocked_count += 1
            elif state in {StageState.RETRY_WAIT, StageState.QUARANTINED}:
                degraded_count += 1
            elif state is StageState.RUNNING and row["lease_expires_at_epoch"] is not None and int(row["lease_expires_at_epoch"]) <= now_epoch:
                degraded_count += 1
            if state in {StageState.QUEUED, StageState.RETRY_WAIT}:
                _consider_runnable(bucket, row["available_at_iso"], row["available_at_epoch"])
                _consider_runnable(sources[source]["totals"], row["available_at_iso"], row["available_at_epoch"])
        health = (
            PipelineHealth.BLOCKED
            if blocked_count
            else PipelineHealth.DEGRADED
            if degraded_count
            else PipelineHealth.HEALTHY
        )
        for source_payload in sources.values():
            _finalize_counts(source_payload["totals"])
            for stage_payload in source_payload["stages"].values():
                _finalize_counts(stage_payload)
        return {
            "schema_version": SCHEMA_VERSION,
            "health": health.value,
            "as_of": now_iso,
            "sources": sources,
        }

    def table_count(self, table: str) -> int:
        """Small diagnostic helper used by CLI doctor and tests; table names are allow-listed."""

        self._require_migrated()
        allowed = {
            "runs",
            "source_windows",
            "schedule_cursors",
            "documents",
            "artifacts",
            "stage_attempts",
            "publications",
            "notification_outbox",
            "leases",
        }
        if table not in allowed:
            raise ValueError(f"unsupported state table: {table}")
        row = self._connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
        assert row is not None
        return int(row["count"])


def _empty_counts() -> dict[str, Any]:
    return {
        "queued": 0,
        "running": 0,
        "succeeded": 0,
        "retry_wait": 0,
        "blocked": 0,
        "blocked_auth": 0,
        "blocked_release": 0,
        "quarantined": 0,
        "runnable_now_count": 0,
        "earliest_runnable_at": None,
        "_earliest_runnable_epoch": None,
    }


def _increment_count(bucket: dict[str, Any], state: StageState) -> None:
    bucket[state.value] = int(bucket.get(state.value, 0)) + 1
    if state in {StageState.BLOCKED_AUTH, StageState.BLOCKED_RELEASE}:
        bucket["blocked"] = int(bucket.get("blocked", 0)) + 1


def _consider_runnable(bucket: dict[str, Any], iso_value: str | None, epoch_value: int | None) -> None:
    if epoch_value is None:
        bucket["runnable_now_count"] = int(bucket.get("runnable_now_count", 0)) + 1
        bucket["earliest_runnable_at"] = None
        bucket["_earliest_runnable_epoch"] = None
        return
    if int(bucket.get("runnable_now_count", 0)):
        return
    earliest = bucket.get("_earliest_runnable_epoch")
    if earliest is None or int(epoch_value) < int(earliest):
        bucket["_earliest_runnable_epoch"] = int(epoch_value)
        bucket["earliest_runnable_at"] = str(iso_value) if iso_value else None


def _finalize_counts(bucket: dict[str, Any]) -> None:
    bucket.pop("_earliest_runnable_epoch", None)
