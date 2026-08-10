"""Forward-only SQLite schema migrations for the pipeline state core."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from pathlib import Path

from . import SCHEMA_VERSION
from ._time import to_iso_epoch, utc_now


class SchemaVersionError(RuntimeError):
    """The database needs code newer than the process that opened it."""


MIGRATION_1: tuple[str, ...] = (
    """
    CREATE TABLE schema_migrations (
      version INTEGER PRIMARY KEY,
      applied_at_iso TEXT NOT NULL,
      applied_at_epoch INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE runs (
      id TEXT PRIMARY KEY,
      workflow TEXT NOT NULL,
      source TEXT NOT NULL DEFAULT '',
      status TEXT NOT NULL,
      started_at_iso TEXT NOT NULL,
      started_at_epoch INTEGER NOT NULL,
      ended_at_iso TEXT,
      ended_at_epoch INTEGER,
      details_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    """
    CREATE TABLE source_windows (
      id INTEGER PRIMARY KEY,
      source TEXT NOT NULL,
      window_start_iso TEXT NOT NULL,
      window_start_epoch INTEGER NOT NULL,
      window_end_iso TEXT NOT NULL,
      window_end_epoch INTEGER NOT NULL,
      status TEXT NOT NULL DEFAULT 'planned',
      checkpoint_eligible INTEGER NOT NULL DEFAULT 0 CHECK (checkpoint_eligible IN (0, 1)),
      created_at_iso TEXT NOT NULL,
      created_at_epoch INTEGER NOT NULL,
      updated_at_iso TEXT NOT NULL,
      updated_at_epoch INTEGER NOT NULL,
      CHECK (window_end_epoch >= window_start_epoch),
      UNIQUE(source, window_start_epoch, window_end_epoch)
    )
    """,
    """
    CREATE TABLE artifacts (
      id INTEGER PRIMARY KEY,
      kind TEXT NOT NULL,
      pdf_sha256 TEXT,
      content_sha256 TEXT,
      extractor_version TEXT NOT NULL DEFAULT '',
      prompt_version TEXT NOT NULL DEFAULT '',
      model TEXT NOT NULL DEFAULT '',
      canonical_path TEXT NOT NULL DEFAULT '',
      size_bytes INTEGER,
      metadata_json TEXT NOT NULL DEFAULT '{}',
      created_at_iso TEXT NOT NULL,
      created_at_epoch INTEGER NOT NULL,
      updated_at_iso TEXT NOT NULL,
      updated_at_epoch INTEGER NOT NULL,
      CHECK (size_bytes IS NULL OR size_bytes >= 0)
    )
    """,
    """
    CREATE TABLE documents (
      id INTEGER PRIMARY KEY,
      source TEXT NOT NULL,
      source_file_id TEXT NOT NULL,
      source_window_id INTEGER REFERENCES source_windows(id),
      artifact_id INTEGER REFERENCES artifacts(id),
      filename TEXT NOT NULL DEFAULT '',
      normalized_filename TEXT NOT NULL DEFAULT '',
      source_path TEXT NOT NULL DEFAULT '',
      created_at_iso TEXT NOT NULL,
      created_at_epoch INTEGER NOT NULL,
      updated_at_iso TEXT NOT NULL,
      updated_at_epoch INTEGER NOT NULL,
      UNIQUE(source, source_file_id)
    )
    """,
    """
    CREATE TABLE stage_attempts (
      id INTEGER PRIMARY KEY,
      document_id INTEGER NOT NULL REFERENCES documents(id),
      stage TEXT NOT NULL,
      workflow_version TEXT NOT NULL,
      state TEXT NOT NULL,
      error_category TEXT,
      error_code TEXT NOT NULL DEFAULT '',
      error_detail TEXT NOT NULL DEFAULT '',
      attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
      available_at_iso TEXT,
      available_at_epoch INTEGER,
      lease_token TEXT,
      lease_expires_at_iso TEXT,
      lease_expires_at_epoch INTEGER,
      output_artifact_id INTEGER REFERENCES artifacts(id),
      created_at_iso TEXT NOT NULL,
      created_at_epoch INTEGER NOT NULL,
      updated_at_iso TEXT NOT NULL,
      updated_at_epoch INTEGER NOT NULL,
      CHECK ((available_at_iso IS NULL) = (available_at_epoch IS NULL)),
      CHECK ((lease_expires_at_iso IS NULL) = (lease_expires_at_epoch IS NULL)),
      UNIQUE(document_id, stage, workflow_version)
    )
    """,
    """
    CREATE TABLE publications (
      id INTEGER PRIMARY KEY,
      summary_sha256 TEXT NOT NULL,
      target TEXT NOT NULL,
      partition_key TEXT NOT NULL,
      state TEXT NOT NULL,
      remote_reference TEXT,
      details_json TEXT NOT NULL DEFAULT '{}',
      created_at_iso TEXT NOT NULL,
      created_at_epoch INTEGER NOT NULL,
      updated_at_iso TEXT NOT NULL,
      updated_at_epoch INTEGER NOT NULL,
      UNIQUE(summary_sha256, target, partition_key)
    )
    """,
    """
    CREATE TABLE notification_outbox (
      id INTEGER PRIMARY KEY,
      idempotency_key TEXT NOT NULL UNIQUE,
      publication_id INTEGER REFERENCES publications(id),
      event TEXT NOT NULL,
      payload_json TEXT NOT NULL DEFAULT '{}',
      status TEXT NOT NULL DEFAULT 'queued',
      created_at_iso TEXT NOT NULL,
      created_at_epoch INTEGER NOT NULL,
      updated_at_iso TEXT NOT NULL,
      updated_at_epoch INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE leases (
      lease_key TEXT PRIMARY KEY,
      stage_attempt_id INTEGER NOT NULL UNIQUE REFERENCES stage_attempts(id),
      owner_token TEXT NOT NULL,
      acquired_at_iso TEXT NOT NULL,
      acquired_at_epoch INTEGER NOT NULL,
      expires_at_iso TEXT NOT NULL,
      expires_at_epoch INTEGER NOT NULL
    )
    """,
    """
    CREATE UNIQUE INDEX idx_artifacts_pdf_content_identity
    ON artifacts(pdf_sha256)
    WHERE kind = 'pdf' AND pdf_sha256 IS NOT NULL
    """,
    """
    CREATE UNIQUE INDEX idx_artifacts_summary_identity
    ON artifacts(pdf_sha256, extractor_version, prompt_version, model)
    WHERE kind = 'summary' AND pdf_sha256 IS NOT NULL
    """,
    "CREATE INDEX idx_documents_source_window ON documents(source_window_id)",
    "CREATE INDEX idx_stage_attempts_due ON stage_attempts(stage, workflow_version, state, available_at_epoch)",
    "CREATE INDEX idx_stage_attempts_document ON stage_attempts(document_id)",
    "CREATE INDEX idx_publications_state ON publications(state)",
    "CREATE INDEX idx_notification_outbox_status ON notification_outbox(status)",
    "CREATE INDEX idx_leases_expiry ON leases(expires_at_epoch)",
)

MIGRATIONS: dict[int, tuple[str, ...]] = {1: MIGRATION_1}


def connect(path: Path) -> sqlite3.Connection:
    """Open one local database connection without running a migration."""

    path = Path(path).expanduser().resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def installed_schema_version(connection: sqlite3.Connection) -> int:
    found = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()
    if found is None:
        return 0
    row = connection.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
    return int(row["version"] or 0)


def assert_compatible(connection: sqlite3.Connection) -> int:
    version = installed_schema_version(connection)
    if version > SCHEMA_VERSION:
        raise SchemaVersionError(
            f"pipeline database schema {version} is newer than supported schema {SCHEMA_VERSION}; refusing downgrade"
        )
    return version


def _apply_statements(connection: sqlite3.Connection, statements: Iterable[str]) -> None:
    for statement in statements:
        connection.execute(statement)


def migrate(connection: sqlite3.Connection) -> int:
    """Apply every missing migration exactly once, never downgrade a database."""

    connection.execute("BEGIN IMMEDIATE")
    try:
        current = assert_compatible(connection)
        for version in range(current + 1, SCHEMA_VERSION + 1):
            statements = MIGRATIONS.get(version)
            if statements is None:  # pragma: no cover - a developer error, kept fail-closed
                raise SchemaVersionError(f"missing migration implementation for schema version {version}")
            _apply_statements(connection, statements)
            applied_iso, applied_epoch = to_iso_epoch(utc_now())
            connection.execute(
                "INSERT INTO schema_migrations(version, applied_at_iso, applied_at_epoch) VALUES (?, ?, ?)",
                (version, applied_iso, applied_epoch),
            )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return SCHEMA_VERSION
