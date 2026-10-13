from __future__ import annotations

import sqlite3
import subprocess
import sys
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.research_evidence_index import reconcile_catalog, refresh_report
from scripts.research_library_index import upsert_report


def test_summary_indexes_before_note_or_publication_and_repairs_replacement(tmp_path):
    db = tmp_path / "catalog.sqlite"
    summary = tmp_path / "20261001-高盛-腾讯.summary.md"
    summary.write_text("# 腾讯\n\n## 广告\n短期内广告尚未加速。", encoding="utf-8")
    result = upsert_report(
        db, {"report_id": "one", "title": summary.stem, "summary_md_path": str(summary)}
    )
    assert result["evidence_index_status"] == "ready"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT broker,report_date FROM reports").fetchone() == (
            "高盛",
            "2026-10-01",
        )
        assert (
            conn.execute(
                "SELECT summary_text FROM report_search WHERE report_search MATCH '广告'"
            ).fetchone()
            is None
        )
        assert (
            "尚未"
            in conn.execute("SELECT summary_text FROM report_search").fetchone()[0]
        )
    summary.write_text("# 腾讯\n\n在需求改善条件下才会上调。", encoding="utf-8")
    repaired = reconcile_catalog(db)
    assert repaired["catalog_updated"] == 1
    assert reconcile_catalog(db)["catalog_updated"] == 0
    with sqlite3.connect(db) as conn:
        assert (
            "需求改善"
            in conn.execute("SELECT summary_text FROM report_search").fetchone()[0]
        )
    summary.unlink()
    missing = reconcile_catalog(db)
    assert missing["catalog_errors"] == 1 and not missing["catalog_complete"]
    assert reconcile_catalog(db)["catalog_errors"] == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM report_search").fetchone()[0] == 0


def test_budget_resumes_catalog_without_vault_and_preserves_legacy_notes(tmp_path):
    db = tmp_path / "catalog.sqlite"
    for index in range(3):
        summary = tmp_path / f"{index}.md"
        summary.write_text(f"# Report\n\nFinding {index}")
        upsert_report(db, {"report_id": str(index), "summary_md_path": str(summary)})
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO report_search(report_id,summary_text,note_path) VALUES('0','legacy original','legacy.md')"
        )
        conn.execute("DELETE FROM report_evidence_state")
        conn.execute("DELETE FROM report_search WHERE note_path='' ")
    first = reconcile_catalog(db, max_updates=1)
    assert first["catalog_pending"] == 2 and first["oldest_pending_at"]
    assert not first["catalog_complete"]
    assert reconcile_catalog(db, max_updates=2)["catalog_complete"]
    with sqlite3.connect(db) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM report_search WHERE report_id='0'"
            ).fetchone()[0]
            == 2
        )
        conn.execute("DELETE FROM reports WHERE report_id='0'")
    assert reconcile_catalog(db)["catalog_deleted"] == 1
    with sqlite3.connect(db) as conn:
        assert (
            conn.execute(
                "SELECT summary_text FROM report_search WHERE report_id='0'"
            ).fetchone()[0]
            == "legacy original"
        )
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "scripts/kb_reconcile_search.py"),
            "--db-path",
            str(db),
            "--vault-root",
            "",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert '"note_directory_available": false' in completed.stdout


def test_catalog_move_and_missing_pdf_are_distinct_from_bad_source(tmp_path):
    db = tmp_path / "catalog.sqlite"
    pdf = tmp_path / "one.pdf"
    pdf.write_bytes(b"fixture")
    assert (
        upsert_report(db, {"report_id": "pdf", "pdf_path": str(pdf)})[
            "evidence_index_status"
        ]
        == "pdf_only"
    )
    md = tmp_path / "one.md"
    md.write_text("ordinary finding")
    upsert_report(db, {"report_id": "one", "summary_md_path": str(md)})
    moved = tmp_path / "moved.md"
    md.rename(moved)
    upsert_report(db, {"report_id": "one", "summary_md_path": str(moved)})
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT source_path FROM report_evidence_state WHERE report_id='one'"
        ).fetchone()[0] == str(moved)
    moved.write_text("password: synthetic-example-secret")
    assert refresh_report(db, "one")["status"] == "error"


def test_missing_owned_fts_row_is_repaired_after_legacy_rebuild(tmp_path):
    db = tmp_path / "catalog.sqlite"
    md = tmp_path / "one.md"
    md.write_text("body-only lighthouse evidence")
    upsert_report(db, {"report_id": "one", "summary_md_path": str(md)})
    with sqlite3.connect(db) as conn:
        conn.execute("DELETE FROM report_search")
    assert reconcile_catalog(db)["catalog_updated"] == 1


def test_broker_inference_uses_institution_prefix_not_a_company_ticker(tmp_path):
    db = tmp_path / "catalog.sqlite"
    md = tmp_path / "20261001-UBS-GS.summary.md"
    md.write_text("# Goldman Sachs\n\nGS results.")
    upsert_report(
        db, {"report_id": "one", "title": md.stem, "summary_md_path": str(md)}
    )
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT broker FROM reports").fetchone()[0] == "瑞银"


def test_worker_records_catalog_failures_without_vault_before_raising(tmp_path):
    from zsxq_pipeline.worker import PipelineWorker, WorkerError

    worker = object.__new__(PipelineWorker)
    worker.config = SimpleNamespace(
        pipeline=SimpleNamespace(
            research_library_root=tmp_path / "library",
            research_library_database=tmp_path / "catalog.sqlite",
            obsidian_vault_root=None,
        ),
        runtime=SimpleNamespace(root=tmp_path),
    )
    worker.clock = lambda: datetime(2026, 10, 13, tzinfo=timezone.utc)
    worker.monotonic = lambda: 0
    payload = {
        "complete": False,
        "catalog_errors": 1,
        "catalog_pending": 7,
        "oldest_pending_at": "2026-10-01",
        "source_status_counts": {"error": 1},
    }
    result = subprocess.CompletedProcess([], 1, stdout=json.dumps(payload), stderr="")
    with patch("zsxq_pipeline.worker.subprocess.run", return_value=result) as run:
        with pytest.raises(WorkerError, match="recorded source errors"):
            worker._maybe_reconcile_search(60)
    command = run.call_args.args[0]
    assert command[command.index("--vault-root") + 1] == ""
    marker = json.loads((tmp_path / "state/kb-reconcile.json").read_text())
    assert marker["catalog"]["catalog_errors"] == 1
    assert marker["catalog"]["oldest_pending_at"] == "2026-10-01"
    assert marker["last_completed_date"] == ""
