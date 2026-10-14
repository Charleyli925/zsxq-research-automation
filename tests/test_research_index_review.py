from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from scripts.archive_to_obsidian import archive_batch
from scripts.kb_common import extract_report_metadata, load_kb_configs, load_note, upsert_metadata
from scripts.research_evidence_index import identity_sha256, reconcile_catalog, refresh_catalog_source
from scripts.research_library_index import upsert_report


@pytest.fixture
def accepted_archive(tmp_path):
    library, vault = tmp_path / "ResearchLibrary", tmp_path / "ResearchVault"
    config = library / "config"
    config.mkdir(parents=True)
    (config / "entities.yml").write_text(
        "companies:\n  - name: Apple\n    tickers: [AAPL]\n", encoding="utf-8"
    )
    summary = library / "20261001-UBS-Apple.summary.md"
    summary.write_text("# Apple\n\nApple outlook improves conditionally.\n", encoding="utf-8")
    pdf = tmp_path / "20261001-UBS-Apple.pdf"
    pdf.write_bytes(b"%PDF-synthetic-test")
    item = {
        "report_id": "apple",
        "title": "20261001-UBS-Apple",
        "path": str(pdf),
        "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "filename": pdf.name,
        "summary_md_path": str(summary),
        "batch_id": "test",
    }
    digest = hashlib.sha256(summary.read_bytes()).hexdigest()
    db = tmp_path / "catalog.sqlite"
    upsert_report(db, {**item, "pdf_path": str(pdf), "expected_source_sha256": digest})
    batch = tmp_path / "batch.json"
    batch.write_text(json.dumps({"files": [item]}), encoding="utf-8")
    return SimpleNamespace(
        db=db, library=library, vault=vault, summary=summary,
        item=item, batch=batch, digest=digest,
    )


def catalog_state(db):
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        row = dict(conn.execute("SELECT * FROM reports WHERE report_id='apple'").fetchone())
        evidence = dict(conn.execute("SELECT * FROM report_evidence_state WHERE report_id='apple'").fetchone())
        owned = conn.execute("SELECT count(*) FROM report_search WHERE note_path=''").fetchone()[0]
    return row, evidence, owned


@pytest.mark.parametrize("empty_tags", ["", "[]"])
def test_archive_enrichment_keeps_accepted_summary_searchable(accepted_archive, empty_tags):
    fixture = accepted_archive
    with sqlite3.connect(fixture.db) as conn:
        conn.execute("UPDATE reports SET companies_json=?,tickers_json=?", (empty_tags, empty_tags))
    upsert_report(fixture.db, {"report_id": "apple", "expected_source_sha256": fixture.digest})
    original = fixture.summary.read_bytes()
    for _ in range(2):
        result = archive_batch(fixture.batch, fixture.library, fixture.vault, "", fixture.db)
        assert result["archived_count"] == 1
        row, evidence, owned = catalog_state(fixture.db)
        assert row["companies_json"] == '["Apple"]'
        assert row["tickers_json"] == '["AAPL"]'
        assert evidence["status"] == "ready"
        assert evidence["content_sha256"] == fixture.digest
        assert evidence["identity_sha256"] == identity_sha256(row)
        assert owned == 1
        assert fixture.summary.read_bytes() == original
        assert reconcile_catalog(fixture.db)["catalog_complete"]
        with sqlite3.connect(fixture.db) as conn:
            assert conn.execute("SELECT count(*) FROM report_search WHERE note_path<>''").fetchone()[0] == 1
            assert "outlook" in conn.execute("SELECT summary_text FROM report_search WHERE note_path=''").fetchone()[0]


@pytest.mark.parametrize("replacement", ["summary_bytes", "summary_path", "title", "pdf_hash", "catalog_identity", "quarantined"])
def test_archive_enrichment_does_not_attest_replacements(accepted_archive, replacement):
    fixture = accepted_archive
    item = dict(fixture.item)
    if replacement == "summary_bytes":
        fixture.summary.write_text("# Microsoft\n\nAzure outlook.\n", encoding="utf-8")
    elif replacement == "summary_path":
        other = fixture.library / "replacement.md"
        other.write_text(fixture.summary.read_text(), encoding="utf-8")
        item["summary_md_path"] = str(other)
    elif replacement == "title":
        item["title"] = "20261001-UBS-Microsoft"
    elif replacement == "pdf_hash":
        item["pdf_sha256"] = "f" * 64
    elif replacement == "catalog_identity":
        with sqlite3.connect(fixture.db) as conn:
            conn.execute("UPDATE reports SET companies_json='[\"Unrelated\"]'")
    else:
        with sqlite3.connect(fixture.db) as conn:
            conn.execute("UPDATE report_evidence_state SET status='source_changed',reason='source_changed_requires_writer_attestation'")
    fixture.batch.write_text(json.dumps({"files": [item]}), encoding="utf-8")
    archive_batch(fixture.batch, fixture.library, fixture.vault, "", fixture.db)
    row, evidence, owned = catalog_state(fixture.db)
    assert evidence["status"] == "source_changed"
    assert evidence["content_sha256"] == fixture.digest
    assert owned == 0
    assert reconcile_catalog(fixture.db)["catalog_errors"] == 1
    with sqlite3.connect(fixture.db) as conn:
        assert refresh_catalog_source(conn, row, force=True)["status"] == "source_changed"


def test_ordinary_metadata_update_cannot_confirm_new_binding(accepted_archive):
    fixture = accepted_archive
    archive_batch(fixture.batch, fixture.library, fixture.vault, "", fixture.db)
    note = load_note(next((fixture.vault / "10_Reports").rglob("*.md")), fixture.vault)
    metadata = extract_report_metadata(note, load_kb_configs(fixture.library / "config"), fixture.vault)
    metadata["companies"] = ["Unrelated"]
    upsert_metadata(fixture.db, note, metadata, "ordinary_update")
    assert reconcile_catalog(fixture.db)["source_status_counts"] == {"source_changed": 1}


@pytest.mark.parametrize("state_dir_exists", [False, True])
def test_reconcile_initializes_missing_catalog_before_query(tmp_path, state_dir_exists):
    db = tmp_path / "new-state" / "catalog.sqlite"
    if state_dir_exists:
        db.parent.mkdir()
    for _ in range(2):
        result = reconcile_catalog(db)
        assert result["catalog_seen"] == 0
        assert result["catalog_errors"] == 0
        assert result["catalog_complete"]
    with sqlite3.connect(db) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"reports", "report_events", "report_search", "report_evidence_state"} <= tables
    summary = tmp_path / "first.md"
    summary.write_text("# First report\n\nNew findings.", encoding="utf-8")
    assert upsert_report(db, {"report_id": "first", "summary_md_path": str(summary)})["evidence_index_status"] == "ready"
    assert reconcile_catalog(db)["catalog_complete"]


@pytest.mark.parametrize("has_empty_vault", [False, True])
@pytest.mark.parametrize("state_dir_exists", [False, True])
def test_first_reconcile_cli_succeeds_without_notes(tmp_path, has_empty_vault, state_dir_exists):
    vault = tmp_path / "ResearchVault"
    if has_empty_vault:
        (vault / "10_Reports").mkdir(parents=True)
    if state_dir_exists:
        (tmp_path / "state").mkdir()
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).parents[1] / "scripts/kb_reconcile_search.py"),
         "--db-path", str(tmp_path / "state/catalog.sqlite"),
         "--vault-root", str(vault) if has_empty_vault else "",
         "--config-root", str(tmp_path / "config")],
        capture_output=True, text=True, check=True,
    )
    result = json.loads(completed.stdout)
    assert result["complete"] and result["catalog_seen"] == 0


def test_first_worker_reconciliation_writes_completion_marker(tmp_path):
    from zsxq_pipeline.worker import PipelineWorker

    worker = object.__new__(PipelineWorker)
    worker.config = SimpleNamespace(
        pipeline=SimpleNamespace(research_library_root=tmp_path / "ResearchLibrary",
            research_library_database=tmp_path / "state/catalog.sqlite", obsidian_vault_root=None),
        runtime=SimpleNamespace(root=tmp_path),
    )
    worker.clock = lambda: datetime(2026, 10, 14, tzinfo=timezone.utc)
    with patch("zsxq_pipeline.worker.subprocess.run", wraps=subprocess.run) as run:
        worker._maybe_reconcile_search()
        worker._maybe_reconcile_search()
        assert run.call_count == 1
    marker = json.loads((tmp_path / "state/kb-reconcile.json").read_text())
    assert marker["last_completed_date"] == "2026-10-14"
    assert marker["catalog"]["catalog_errors"] == 0
