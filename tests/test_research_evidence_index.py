from __future__ import annotations

import sqlite3
import subprocess
import sys
import json
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.research_evidence_index import (
    reconcile_catalog,
    refresh_report,
    refresh_catalog_source,
)
from scripts.research_library_index import upsert_report


def test_summary_indexes_before_publication_and_requires_attested_replacement(tmp_path):
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
    assert repaired["source_status_counts"]["source_changed"] == 1
    assert reconcile_catalog(db)["catalog_updated"] == 0
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM report_search").fetchone()[0] == 0
    assert (
        upsert_report(
            db,
            {
                "report_id": "one",
                "expected_source_sha256": hashlib.sha256(
                    summary.read_bytes()
                ).hexdigest(),
            },
        )["evidence_index_status"]
        == "ready"
    )
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


def test_unattested_identity_change_and_force_cannot_bless_replaced_content(tmp_path):
    db, md = tmp_path / "catalog.sqlite", tmp_path / "source.md"
    md.write_text("# Apple\n\nApple AI outlook.")
    upsert_report(
        db,
        {
            "report_id": "one",
            "title": "Apple",
            "broker": "Broker A",
            "companies_json": '["Apple"]',
            "summary_md_path": str(md),
        },
    )
    original_hash = hashlib.sha256(md.read_bytes()).hexdigest()
    md.write_text("# Microsoft\n\nAzure AI outlook.")
    assert refresh_report(db, "one")["status"] == "source_changed"
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute("SELECT * FROM reports").fetchone())
        assert (
            refresh_catalog_source(conn, row, force=True)["status"] == "source_changed"
        )
        assert (
            conn.execute("SELECT content_sha256 FROM report_evidence_state").fetchone()[
                0
            ]
            == original_hash
        )
        assert conn.execute("SELECT count(*) FROM report_search").fetchone()[0] == 0
    # Restoring accepted bytes restores the existing binding automatically.
    md.write_text("# Apple\n\nApple AI outlook.")
    assert reconcile_catalog(db)["source_status_counts"] == {"ready": 1}
    assert (
        upsert_report(db, {"report_id": "one", "title": "Wrong Company"})[
            "evidence_index_status"
        ]
        == "source_changed"
    )


def test_transient_io_retries_without_edit_but_quarantined_content_does_not(tmp_path):
    db, md = tmp_path / "catalog.sqlite", tmp_path / "source.md"
    md.write_text("# Apple\n\nRevenue improves conditionally.")
    with patch.object(Path, "read_bytes", side_effect=OSError("temporary offline")):
        assert (
            upsert_report(db, {"report_id": "one", "summary_md_path": str(md)})[
                "evidence_index_status"
            ]
            == "error"
        )
    signature = md.stat()
    with sqlite3.connect(db) as conn:
        retry_at = datetime.fromisoformat(
            conn.execute("SELECT next_retry_at FROM report_evidence_state").fetchone()[
                0
            ]
        )
    assert (
        reconcile_catalog(db, now=retry_at - timedelta(seconds=1))["catalog_updated"]
        == 0
    )
    assert reconcile_catalog(db, now=retry_at)["source_status_counts"] == {"ready": 1}
    assert md.stat() == signature
    md.write_text("password: synthetic-example-secret")
    assert refresh_report(db, "one")["status"] == "error"
    with patch.object(
        Path,
        "read_bytes",
        side_effect=AssertionError("must not retry quarantined bytes"),
    ):
        assert (
            reconcile_catalog(db, now=retry_at + timedelta(days=1))["catalog_updated"]
            == 0
        )


def test_attestation_must_match_bytes_and_migration_preserves_known_version(tmp_path):
    db, md = tmp_path / "catalog.sqlite", tmp_path / "source.md"
    md.write_text("original evidence")
    upsert_report(db, {"report_id": "one", "summary_md_path": str(md)})
    with sqlite3.connect(db) as conn:
        for column in ("identity_sha256", "error_kind", "retry_count", "next_retry_at"):
            conn.execute(f"ALTER TABLE report_evidence_state DROP COLUMN {column}")
        conn.execute("UPDATE report_evidence_state SET schema_version=1")
    assert reconcile_catalog(db)["source_status_counts"] == {"ready": 1}
    md.write_text("other evidence")
    assert (
        upsert_report(db, {"report_id": "one", "expected_source_sha256": "0" * 64})[
            "evidence_index_status"
        ]
        == "source_changed"
    )
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM report_search").fetchone()[0] == 0


def test_ticker_changes_are_identity_changes_and_retry_backoff_is_bounded(tmp_path):
    db, md = tmp_path / "catalog.sqlite", tmp_path / "source.md"
    md.write_text("original evidence")
    upsert_report(db, {"report_id": "one", "summary_md_path": str(md)})
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE reports SET tickers_json='[\"MSFT\"]'")
    assert reconcile_catalog(db)["source_status_counts"] == {"source_changed": 1}
    # A separate previously unread source cannot be stuck after transient I/O.
    with patch.object(Path, "read_bytes", side_effect=OSError("temporarily offline")):
        upsert_report(db, {"report_id": "retry", "summary_md_path": str(md)})
        for attempt in range(8):
            with sqlite3.connect(db) as conn:
                retry_at = datetime.fromisoformat(
                    conn.execute(
                        "SELECT next_retry_at FROM report_evidence_state WHERE report_id='retry'"
                    ).fetchone()[0]
                )
            reconcile_catalog(db, now=retry_at)
            with sqlite3.connect(db) as conn:
                count, next_at = conn.execute(
                    "SELECT retry_count,next_retry_at FROM report_evidence_state WHERE report_id='retry'"
                ).fetchone()
            assert count == attempt + 2
            assert (
                0 < (datetime.fromisoformat(next_at) - retry_at).total_seconds() <= 3600
            )


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


def test_sk_hynix_label_is_not_a_key_but_lowercase_key_is_excluded(tmp_path):
    db = tmp_path / "catalog.sqlite"
    md = tmp_path / "issuer.md"
    md.write_text(
        "# SK-Hynix-000660-KS-Project-2026E-OP-of-W251tr-Raise-TP-to-W3100k\n\nFindings."
    )
    assert (
        upsert_report(db, {"report_id": "issuer", "summary_md_path": str(md)})[
            "evidence_index_status"
        ]
        == "ready"
    )
    md.write_text("sk-" + "a" * 48)
    assert refresh_report(db, "issuer")["status"] == "error"
