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

# Keep migration 1 immutable.  Version 2 extends the identity contracts after
# databases created by PR 2 already existed in the field.
MIGRATION_2: tuple[str, ...] = (
    "ALTER TABLE artifacts ADD COLUMN reasoning TEXT NOT NULL DEFAULT ''",
    "DROP INDEX idx_artifacts_summary_identity",
    """
    CREATE UNIQUE INDEX idx_artifacts_summary_identity
    ON artifacts(pdf_sha256, extractor_version, prompt_version, model, reasoning)
    WHERE kind = 'summary' AND pdf_sha256 IS NOT NULL
    """,
    # SQLite cannot alter a table-level UNIQUE constraint.  Preserve primary
    # keys and all existing timestamps while rebuilding the publication key to
    # include its logical target document.  Rebuild the child outbox as well:
    # otherwise SQLite rewrites its FK to the temporary publication table.
    # ``migrate`` temporarily disables foreign-key enforcement around this
    # short, single-connection rebuild and checks it again after commit.
    "ALTER TABLE notification_outbox RENAME TO notification_outbox_v1",
    "ALTER TABLE publications RENAME TO publications_v1",
    """
    CREATE TABLE publications (
      id INTEGER PRIMARY KEY,
      summary_sha256 TEXT NOT NULL,
      target TEXT NOT NULL,
      target_document TEXT NOT NULL DEFAULT '',
      partition_key TEXT NOT NULL,
      state TEXT NOT NULL,
      remote_reference TEXT,
      details_json TEXT NOT NULL DEFAULT '{}',
      created_at_iso TEXT NOT NULL,
      created_at_epoch INTEGER NOT NULL,
      updated_at_iso TEXT NOT NULL,
      updated_at_epoch INTEGER NOT NULL,
      UNIQUE(summary_sha256, target, target_document, partition_key)
    )
    """,
    """
    INSERT INTO publications(
      id, summary_sha256, target, target_document, partition_key, state,
      remote_reference, details_json,
      created_at_iso, created_at_epoch, updated_at_iso, updated_at_epoch
    )
    SELECT
      id, summary_sha256, target, '', partition_key, state,
      remote_reference, details_json,
      created_at_iso, created_at_epoch, updated_at_iso, updated_at_epoch
    FROM publications_v1
    """,
    """
    CREATE TABLE notification_outbox (
      id INTEGER PRIMARY KEY,
      idempotency_key TEXT NOT NULL UNIQUE,
      publication_id INTEGER REFERENCES publications(id),
      event TEXT NOT NULL,
      payload_json TEXT NOT NULL DEFAULT '{}',
      status TEXT NOT NULL DEFAULT 'queued',
      attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
      available_at_iso TEXT,
      available_at_epoch INTEGER,
      lease_token TEXT,
      lease_expires_at_iso TEXT,
      lease_expires_at_epoch INTEGER,
      error_code TEXT NOT NULL DEFAULT '',
      error_detail TEXT NOT NULL DEFAULT '',
      created_at_iso TEXT NOT NULL,
      created_at_epoch INTEGER NOT NULL,
      updated_at_iso TEXT NOT NULL,
      updated_at_epoch INTEGER NOT NULL,
      CHECK ((available_at_iso IS NULL) = (available_at_epoch IS NULL)),
      CHECK ((lease_expires_at_iso IS NULL) = (lease_expires_at_epoch IS NULL))
    )
    """,
    """
    INSERT INTO notification_outbox(
      id, idempotency_key, publication_id, event, payload_json, status,
      attempt_count, available_at_iso, available_at_epoch,
      lease_token, lease_expires_at_iso, lease_expires_at_epoch,
      error_code, error_detail,
      created_at_iso, created_at_epoch, updated_at_iso, updated_at_epoch
    )
    SELECT
      id, idempotency_key, publication_id, event, payload_json, status,
      0, NULL, NULL, NULL, NULL, NULL, '', '',
      created_at_iso, created_at_epoch, updated_at_iso, updated_at_epoch
    FROM notification_outbox_v1
    """,
    "DROP TABLE notification_outbox_v1",
    "DROP TABLE publications_v1",
    "CREATE INDEX idx_publications_state ON publications(state)",
    "CREATE INDEX idx_notification_outbox_status ON notification_outbox(status)",
    "CREATE INDEX idx_notification_outbox_due ON notification_outbox(status, available_at_epoch)",
)

MIGRATIONS: dict[int, tuple[str, ...]] = {1: MIGRATION_1, 2: MIGRATION_2}

# Rebuilding ``publications`` retains its primary keys, but SQLite updates the
# child foreign-key declaration while a table is renamed.  Foreign-key checks
# must therefore be suspended before the transaction, then verified after it.
_FOREIGN_KEY_REBUILD_MIGRATIONS = frozenset({2})


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

    preflight_version = assert_compatible(connection)
    needs_foreign_key_rebuild = any(
        version in _FOREIGN_KEY_REBUILD_MIGRATIONS for version in range(preflight_version + 1, SCHEMA_VERSION + 1)
    )
    foreign_keys_row = connection.execute("PRAGMA foreign_keys").fetchone()
    foreign_keys_enabled = bool(foreign_keys_row[0]) if foreign_keys_row is not None else False
    if needs_foreign_key_rebuild and foreign_keys_enabled:
        # PRAGMA foreign_keys has no effect inside a transaction, so this must
        # happen before BEGIN IMMEDIATE.  The migration remains atomic below.
        connection.execute("PRAGMA foreign_keys = OFF")

    try:
        connection.execute("BEGIN IMMEDIATE")
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
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        if needs_foreign_key_rebuild and foreign_keys_enabled:
            connection.execute("PRAGMA foreign_keys = ON")

    if needs_foreign_key_rebuild and foreign_keys_enabled:
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise SchemaVersionError("migration produced foreign-key violations")
    return SCHEMA_VERSION
