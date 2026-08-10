from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

import pytest

from zsxq_pipeline.legacy_import import (
    LegacySourceChangedError,
    apply_import_plan,
    build_import_plan,
    load_import_plan,
    write_import_plan,
)
from zsxq_pipeline.state import PipelineState


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _write_json(path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _build_legacy_fixture(root) -> None:
    first_sha = "a" * 64
    second_sha = "b" * 64
    _write_json(
        root / "state" / "zsxq_foreign_reports_state.json",
        {
            "last_window_start": "2026-08-10T10:00:00+08:00",
            "last_window_end": "2026-08-10T12:00:00+08:00",
            "last_successful_check_at": "2026-08-10T12:00:00+08:00",
        },
    )
    _write_json(
        root / "state" / "zsxq_autodownload_runs" / "run.json",
        {
            "window_start": "2026-08-10T10:00:00+08:00",
            "window_end": "2026-08-10T12:00:00+08:00",
            "workflow_version": "legacy-download-v1",
            "downloaded_entries": [
                {
                    "source_file_id": "foreign-file-1",
                    "filename": "foreign.pdf",
                    "archive_path": "/library/foreign.pdf",
                    "pdf_sha256": first_sha,
                }
            ],
        },
    )
    _write_json(
        root / "task" / "zsxq_pdf_digest" / "pending_batch.json",
        {
            "workflow_version": "legacy-digest-v1",
            "files": [{"filename": "pending.pdf", "path": "/library/pending.pdf", "pdf_sha256": second_sha}],
        },
    )
    entries = {
        f"entry-{index}": {
            "file_sha256": f"{index:064x}",
            "filename": f"blocked-{index}.pdf",
            "stage": "publish",
            "workflow_version": "legacy-release-v1",
            "status": "retry_exhausted",
            "error_code": "publish_failed",
            "error_type": "transient_failure",
            "message": "Authorization: Bearer fixture-secret legacy retry budget exhausted",
            "attempt_count": 4,
        }
        for index in range(1, 527)
    }
    _write_json(root / "task" / "zsxq_pdf_digest" / "stage_retry_ledger.json", {"entries": entries})
    records = [
        {
            "summary_hash": "c" * 64,
            "target_doc_url": "https://www.feishu.cn/docx/fixture",
            "batch_hash": "batch-success",
            "doc_url": "https://www.feishu.cn/docx/fixture",
            "status": "success",
        },
        {
            "summary_hash": "d" * 64,
            "target_doc_url": "https://www.feishu.cn/docx/pending",
            "batch_hash": "batch-remote-written",
            "doc_url": "https://www.feishu.cn/docx/pending",
            "status": "remote_written",
        },
    ]
    records_path = root / "task" / "zsxq_pdf_digest" / "publish_records.jsonl"
    records_path.parent.mkdir(parents=True, exist_ok=True)
    records_path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    _write_json(
        root / "task" / "zsxq_pdf_digest" / "notification_outbox.json",
        {"items": {"run:fixture": {"event": "completed", "status": "sent"}}},
    )

    index_path = root / "library" / "state" / "processed_files.sqlite"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(index_path)
    connection.execute(
        "CREATE TABLE reports(report_id TEXT PRIMARY KEY, source TEXT, pdf_path TEXT, pdf_sha256 TEXT, title TEXT)"
    )
    connection.execute(
        "INSERT INTO reports VALUES (?, ?, ?, ?, ?)",
        ("library-report", "zsxq_foreign", "/library/foreign-copy.pdf", first_sha, "foreign-copy.pdf"),
    )
    connection.commit()
    connection.close()


def test_import_plan_is_read_only_then_imports_idempotently_with_terminal_retry_states(tmp_path):
    legacy_root = tmp_path / "legacy"
    _build_legacy_fixture(legacy_root)
    destination = tmp_path / "new" / "pipeline.sqlite3"

    plan = build_import_plan(legacy_root)
    assert not destination.exists()
    assert len(plan.source_files) >= 7
    assert len(plan.documents) >= 528
    assert len(plan.stage_attempts) >= 528
    assert len(plan.remote_written_without_success) == 1
    assert not plan.conflicts
    assert "fixture-secret" not in json.dumps(plan.to_dict())

    plan_path = tmp_path / "review" / "legacy-plan.json"
    write_import_plan(plan, plan_path)
    reloaded = load_import_plan(plan_path)
    first_counts = apply_import_plan(reloaded, destination)
    second_counts = apply_import_plan(reloaded, destination)
    assert first_counts == second_counts
    assert first_counts["artifacts"] == 2
    assert first_counts["publications"] == 2
    assert first_counts["notification_outbox"] == 1

    with PipelineState.open(destination) as state:
        health = state.derive_health(now=NOW)
        release = health["sources"]["zsxq_digest"]["stages"]["publish"]
        assert release["blocked"] == 526
        assert release["retry_wait"] == 0
        assert health["health"] == "blocked"
        foreign = state._connection.execute(
            "SELECT source_window_id FROM documents WHERE source = ? AND source_file_id = ?",
            ("zsxq_foreign", "foreign-file-1"),
        ).fetchone()
        assert foreign is not None
        assert foreign["source_window_id"] is not None


def test_apply_rejects_legacy_source_changes_before_creating_destination_database(tmp_path):
    legacy_root = tmp_path / "legacy"
    _build_legacy_fixture(legacy_root)
    plan = build_import_plan(legacy_root)
    destination = tmp_path / "new" / "pipeline.sqlite3"
    watch_state = legacy_root / "task" / "zsxq_pdf_digest" / "pending_batch.json"
    watch_state.write_text('{"files": []}\n', encoding="utf-8")

    with pytest.raises(LegacySourceChangedError, match="changed after plan"):
        apply_import_plan(plan, destination)
    assert not destination.exists()
