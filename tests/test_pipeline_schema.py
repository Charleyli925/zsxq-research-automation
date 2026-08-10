from __future__ import annotations

import sqlite3

import pytest

from zsxq_pipeline import SCHEMA_VERSION
from zsxq_pipeline.schema import MIGRATION_1, SchemaVersionError
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


def test_v2_migrates_v1_summary_and_publication_foreign_keys_forward_only(tmp_path):
    database = tmp_path / "v1.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA foreign_keys = ON")
    for statement in MIGRATION_1:
        connection.execute(statement)
    connection.execute(
        "INSERT INTO schema_migrations(version, applied_at_iso, applied_at_epoch) VALUES (1, ?, ?)",
        ("2026-08-10T00:00:00Z", 0),
    )
    connection.execute(
        """
        INSERT INTO artifacts(
          kind, pdf_sha256, content_sha256, extractor_version, prompt_version, model, canonical_path,
          created_at_iso, created_at_epoch, updated_at_iso, updated_at_epoch
        ) VALUES ('summary', ?, ?, 'extract-v1', 'prompt-v1', 'model-v1', '/runtime/summary.md', ?, 0, ?, 0)
        """,
        ("a" * 64, "b" * 64, "2026-08-10T00:00:00Z", "2026-08-10T00:00:00Z"),
    )
    connection.execute(
        """
        INSERT INTO publications(
          summary_sha256, target, partition_key, state, remote_reference,
          created_at_iso, created_at_epoch, updated_at_iso, updated_at_epoch
        ) VALUES (?, 'lark:daily', '2026-08-10', 'remote_written', 'https://example.invalid/doc', ?, 0, ?, 0)
        """,
        ("c" * 64, "2026-08-10T00:00:00Z", "2026-08-10T00:00:00Z"),
    )
    connection.execute(
        """
        INSERT INTO notification_outbox(
          idempotency_key, publication_id, event, payload_json, status,
          created_at_iso, created_at_epoch, updated_at_iso, updated_at_epoch
        ) VALUES ('notification:1', 1, 'document_ready', '{}', 'queued', ?, 0, ?, 0)
        """,
        ("2026-08-10T00:00:00Z", "2026-08-10T00:00:00Z"),
    )
    connection.commit()
    connection.close()

    with PipelineState.open(database) as state:
        assert state.schema_version == 1
        assert state.migrate() == SCHEMA_VERSION
        artifact = state.find_summary_artifact("a" * 64, "extract-v1", "prompt-v1", "model-v1", "")
        assert artifact is not None
        assert artifact.reasoning == ""
        publication = state.get_publication("c" * 64, "lark:daily", "2026-08-10")
        assert publication is not None
        assert publication.target_document == ""
        notification = state.get_notification("notification:1")
        assert notification is not None
        assert notification.publication_id == publication.id
        foreign_keys = state._connection.execute("PRAGMA foreign_key_list(notification_outbox)").fetchall()
        assert [row["table"] for row in foreign_keys] == ["publications"]
        assert state._connection.execute("PRAGMA foreign_key_check").fetchall() == []
