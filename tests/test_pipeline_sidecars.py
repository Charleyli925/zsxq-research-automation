from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest import mock

from zsxq_pipeline.model import SummaryIdentity
from zsxq_pipeline.sidecars import ArtifactSidecars
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
        identity=SummaryIdentity("a" * 64, "extract-v1", "prompt-v1", "model-v1", "high"),
        entry=SummaryEntry(str(pdf), "alpha.pdf", "Alpha", "ok", "# Alpha\n\n- finding"),
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

    with mock.patch.object(ArtifactSidecars, "_run", return_value={"archived_count": 1}) as run:
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
    assert commands[1][commands[1].index("--database") + 1] == str(database)
    assert commands[2][commands[2].index("--library-database") + 1] == str(database)
