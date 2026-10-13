from __future__ import annotations

import hashlib
import sqlite3
from types import SimpleNamespace
from unittest import mock

import pytest

from zsxq_pipeline.model import SummaryIdentity
from zsxq_pipeline.sidecars import ArtifactSidecars, SidecarError
from zsxq_pipeline.summary import PersistedSummary, SummaryArtifactPaths, SummaryEntry


def test_sidecar_helpers_share_one_runtime_owned_library_database(tmp_path):
    library = tmp_path / "ResearchLibrary"
    vault = tmp_path / "ResearchVault"
    work = tmp_path / "runtime" / "work"
    database = tmp_path / "runtime" / "state" / "research-library.sqlite"
    pdf = library / "pdfs" / "batch-1" / "alpha.pdf"
    markdown_path = tmp_path / "runtime" / "summary_cache" / "markdown" / "summary.md"
    json_path = tmp_path / "runtime" / "summary_cache" / "json" / "summary.json"
    pdf.parent.mkdir(parents=True)
    markdown_path.parent.mkdir(parents=True)
    markdown_path.write_text("# Alpha\n\n- finding", encoding="utf-8")
    persisted = PersistedSummary(
        identity=SummaryIdentity(
            "a" * 64, "extract-v1", "prompt-v1", "model-v1", "high"
        ),
        entry=SummaryEntry(
            str(pdf), "alpha.pdf", "Alpha", "ok", "# Alpha\n\n- finding"
        ),
        paths=SummaryArtifactPaths(json_path=json_path, markdown_path=markdown_path),
        markdown_sha256=hashlib.sha256(b"# Alpha\n\n- finding").hexdigest(),
    )
    item = {
        "path": str(pdf),
        "filename": "alpha.pdf",
        "pdf_sha256": "a" * 64,
        "batch_id": "batch-1",
    }
    sidecars = ArtifactSidecars(
        library_root=library,
        library_database=database,
        vault_root=vault,
        work_root=work,
    )

    with mock.patch.object(
        ArtifactSidecars, "_run", return_value={"archived_count": 1}
    ) as run:
        destination = sidecars.persist_summary(item, persisted)
        item["summary_md_path"] = str(destination)
        result = sidecars.archive_published_group(
            entries=(SimpleNamespace(path=str(pdf)),),
            batch_items={str(pdf): item},
            document_url="https://example.com/doc",
        )

    commands = [call.args[0] for call in run.call_args_list]
    assert result.archived_count == 1
    assert destination.is_file()
    assert commands[0][commands[0].index("--database") + 1] == str(database)
    assert (
        commands[0][commands[0].index("--expected-source-sha256") + 1]
        == hashlib.sha256(destination.read_bytes()).hexdigest()
    )
    assert commands[1][commands[1].index("--database") + 1] == str(database)
    assert commands[2][commands[2].index("--library-database") + 1] == str(database)


def test_generated_summary_attests_report_identity_and_keeps_binding_after_publication(
    tmp_path,
):
    from scripts.research_evidence_index import reconcile_catalog

    database = tmp_path / "catalog.sqlite"
    pdf = tmp_path / "import-filename.pdf"
    pdf.write_bytes(b"fixture")
    item = {"path": str(pdf), "filename": pdf.name, "pdf_sha256": "a" * 64}
    identity = SummaryIdentity("a" * 64, "extract-v1", "prompt-v1", "model-v1", "high")
    sidecars = ArtifactSidecars(
        library_root=tmp_path / "Library",
        library_database=database,
        work_root=tmp_path / "work",
        vault_root=None,
    )

    def persisted(markdown):
        return PersistedSummary(
            identity=identity,
            entry=SummaryEntry(
                str(pdf), pdf.name, "Committed report title", "ok", markdown
            ),
            paths=SummaryArtifactPaths(
                json_path=tmp_path / "summary.json",
                markdown_path=tmp_path / "summary.md",
            ),
            markdown_sha256=hashlib.sha256(markdown.encode()).hexdigest(),
        )

    first = persisted("# Findings\n\nRevenue improves conditionally.")
    destination = sidecars.persist_summary(item, first)
    destination.write_text("Unrelated replacement")
    assert reconcile_catalog(database)["source_status_counts"] == {"source_changed": 1}
    second = persisted("# Findings\n\nRevenue improves only after demand recovers.")
    destination = sidecars.persist_summary(item, second)
    item["summary_md_path"] = str(destination)
    sidecars.archive_published_group(
        entries=(second.entry,),
        batch_items={str(pdf): item},
        document_url="https://example.com/doc",
    )
    with sqlite3.connect(database) as conn:
        assert conn.execute(
            "SELECT status,content_sha256 FROM report_evidence_state"
        ).fetchone() == ("ready", hashlib.sha256(destination.read_bytes()).hexdigest())
        assert (
            conn.execute("SELECT title FROM reports").fetchone()[0]
            == second.entry.title
        )
    wrong_item = {**item, "pdf_sha256": "b" * 64}
    with pytest.raises(SidecarError, match="identity does not match"):
        sidecars.persist_summary(wrong_item, second)


def test_archive_rejects_a_partial_helper_result(tmp_path):
    sidecars = ArtifactSidecars(
        library_root=tmp_path / "ResearchLibrary",
        library_database=tmp_path / "runtime" / "state" / "research-library.sqlite",
        vault_root=tmp_path / "ResearchVault",
        work_root=tmp_path / "runtime" / "work",
    )
    source = tmp_path / "report.pdf"
    item = {"path": str(source), "filename": source.name}

    with mock.patch.object(
        ArtifactSidecars, "_run", return_value={"archived_count": 0}
    ):
        with pytest.raises(SidecarError, match="completed 0 of 1"):
            sidecars.archive_published_group(
                entries=(SimpleNamespace(path=str(source)),),
                batch_items={str(source): item},
                document_url="https://example.com/doc",
            )
