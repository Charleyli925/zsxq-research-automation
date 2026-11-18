from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from unittest.mock import patch

from scripts.repair_summary_projections import apply_plan, build_plan
from scripts.research_evidence_index import reconcile_catalog
from zsxq_pipeline.model import SummaryIdentity
from zsxq_pipeline.publish import SummaryForPublish
from zsxq_pipeline.sidecars import ArtifactSidecars
from zsxq_pipeline.state import PipelineState
from zsxq_pipeline.summary import SummaryEntry, SummaryStore, record_summary_artifact
from test_pipeline_worker import _config


def fixture(tmp_path):
    config = _config(tmp_path)
    config = replace(config, pipeline=replace(config.pipeline,
        research_library_root=config.runtime.root / "library",
        research_library_database=config.runtime.root / "state/catalog.sqlite",
        obsidian_vault_root=config.runtime.root / "vault"))
    root = config.runtime.root
    root.mkdir(parents=True)
    pdf = root / "original-filename.pdf"
    pdf.write_bytes(b"%PDF-synthetic")
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    cached = SummaryStore(root / "summary_cache").persist(
        SummaryIdentity(digest, "extract", "prompt", "model", "high"),
        SummaryEntry(str(pdf), pdf.name, "Accepted summary title", "", "# Findings\n\nConditional outlook."),
    )
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        document = state.upsert_document("foreign", "one", filename=pdf.name)
        record_summary_artifact(state, document.id, cached)
    sidecars = ArtifactSidecars(library_root=config.pipeline.research_library_root,
        library_database=config.pipeline.research_library_database,
        vault_root=config.pipeline.obsidian_vault_root, work_root=root / "work")
    item = {"path": str(pdf), "filename": pdf.name, "pdf_sha256": digest, "batch_id": "test"}
    item["summary_md_path"] = str(sidecars.persist_summary(item, cached))
    entry = SummaryForPublish(cached.markdown_sha256, cached.entry.markdown, "test", str(pdf), pdf.name,
                              title=cached.entry.title)
    sidecars.archive_published_group(entries=(entry,), batch_items={str(pdf): item}, document_url="https://example.invalid/doc")
    with sqlite3.connect(config.pipeline.research_library_database) as conn:
        conn.execute("UPDATE reports SET title='original-filename'")
    assert reconcile_catalog(config.pipeline.research_library_database)["catalog_errors"] == 1
    return config, item


def test_exact_projection_repair_restores_index_and_preserves_note_link(tmp_path):
    config, item = fixture(tmp_path)
    notes = list(config.pipeline.obsidian_vault_root.rglob("*.md"))
    plan = build_plan(config)
    assert plan["expected_count"] == 1 and not plan["excluded"]
    outcome = apply_plan(config, plan, 1)
    assert outcome["repaired"] == 1
    assert (Path(outcome["backup"]) / "catalog.sqlite").is_file()
    assert list(config.pipeline.obsidian_vault_root.rglob("*.md")) == notes
    assert reconcile_catalog(config.pipeline.research_library_database)["catalog_complete"]
    with sqlite3.connect(config.pipeline.research_library_database) as conn:
        assert conn.execute("SELECT title FROM reports").fetchone()[0] == "Accepted summary title"
    with sqlite3.connect(config.runtime.database) as conn:
        assert conn.execute("SELECT count(*) FROM publications").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM notification_outbox").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["summary", "pdf", "catalog", "count"])
def test_projection_repair_rejects_changed_evidence_before_writing(tmp_path, change):
    config, item = fixture(tmp_path)
    plan = build_plan(config)
    count = 1
    if change in ("summary", "pdf"):
        Path(item["summary_md_path"] if change == "summary" else item["path"]).write_text("changed bytes")
    elif change == "catalog":
        with sqlite3.connect(config.pipeline.research_library_database) as conn:
            conn.execute("UPDATE reports SET title='new title'")
    else:
        count = 2
    with pytest.raises(ValueError, match="changed|mismatch"):
        apply_plan(config, plan, count)
    assert not (config.runtime.root / "backups").exists()


def test_projection_repair_resumes_after_catalog_commit_before_note_write(tmp_path):
    config, _ = fixture(tmp_path)
    with patch.object(ArtifactSidecars, "archive_published_group", side_effect=RuntimeError("power loss")):
        with pytest.raises(RuntimeError, match="power loss"):
            apply_plan(config, build_plan(config), 1)
    # Persisting the summary already made the catalog ready. The independent
    # repair journal must still surface the unfinished note for a fresh plan.
    with sqlite3.connect(config.pipeline.research_library_database) as conn:
        assert conn.execute("SELECT status FROM report_evidence_state").fetchone()[0] == "ready"
    fresh = build_plan(config)
    assert fresh["expected_count"] == 1
    assert apply_plan(config, fresh, 1)["repaired"] == 1
    assert build_plan(config)["expected_count"] == 0
