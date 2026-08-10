from __future__ import annotations

import sqlite3

import pytest

from zsxq_pipeline import SCHEMA_VERSION
from zsxq_pipeline.schema import SchemaVersionError
from zsxq_pipeline.state import PipelineState


def test_empty_database_migration_is_forward_only_and_idempotent(tmp_path):
    database = tmp_path / "state" / "pipeline.sqlite3"
    with PipelineState.open(database) as state:
        assert state.schema_version == 0
        assert state.migrate() == SCHEMA_VERSION
        assert state.schema_version == SCHEMA_VERSION
        assert state.migrate() == SCHEMA_VERSION
        assert state.table_count("documents") == 0
        tables = {
            row["name"]
            for row in state._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {
        "schema_migrations",
        "runs",
        "source_windows",
        "documents",
        "artifacts",
        "stage_attempts",
        "publications",
        "notification_outbox",
        "leases",
    }.issubset(tables)


def test_database_from_a_newer_schema_is_rejected_without_downgrade(tmp_path):
    database = tmp_path / "newer.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at_iso TEXT NOT NULL, applied_at_epoch INTEGER NOT NULL)"
    )
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at_iso, applied_at_epoch) VALUES (?, ?, ?)",
        (SCHEMA_VERSION + 1, "2026-08-10T00:00:00Z", 0),
    )
    connection.commit()
    connection.close()

    with pytest.raises(SchemaVersionError, match="newer"):
        PipelineState.open(database)
