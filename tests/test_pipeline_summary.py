from __future__ import annotations

import hashlib
import json
import threading
import time
import unicodedata
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zsxq_pipeline.model import SummaryIdentity
from zsxq_pipeline.state import PipelineState
from zsxq_pipeline.summary import (
    SummaryEntry,
    SummaryInvariantError,
    SummaryJob,
    SummaryStore,
    SummaryValidationError,
    build_summary_inputs,
    identities_for_manifest,
    materialize_summary_cache,
    persist_summary_batch,
    prompt_version_hash,
    record_summary_artifact,
    run_summary_jobs,
    validate_summary_payload,
)


def _sha(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _entry(path: str, *, markdown: str = "# 摘要\n\n- 结论") -> dict[str, str]:
    return {
        "path": path,
        "filename": Path(path).name,
        "title": "测试研报",
        "quality_hint": "",
        "markdown": markdown,
    }


def _manifest(tmp_path: Path) -> dict[str, object]:
    files: list[dict[str, object]] = []
    for name in ("alpha", "beta"):
        pdf_path = tmp_path / f"{name}.pdf"
        text_path = tmp_path / f"{name}.txt"
        pdf_path.write_bytes(name.encode("utf-8"))
        text_path.write_text(f"{name} extracted text", encoding="utf-8")
        files.append(
            {
                "path": str(pdf_path),
                "filename": pdf_path.name,
                "pdf_sha256": _sha(name),
                "extracted_text_path": str(text_path),
                "extracted_text_chars": len(f"{name} extracted text"),
                "text_extract_profile": "extract-fixture-v1",
                "text_source": "fixture",
            }
        )
    return {"chunk_index": 1, "chunk_total": 1, "files": files}


def test_summary_identity_and_prompt_hash_include_reasoning():
    first = SummaryIdentity(_sha("pdf"), "extract-v1", "prompt-v1", "gpt", "low")
    second = SummaryIdentity(_sha("pdf"), "extract-v1", "prompt-v1", "gpt", "high")

    assert first.cache_key != second.cache_key
    assert first.canonical_json != second.canonical_json
    assert prompt_version_hash("prompt", "system") != prompt_version_hash("prompt changed", "system")


def test_summary_schema_requires_exact_ordered_paths_and_no_extra_fields(tmp_path):
    manifest = _manifest(tmp_path)
    paths = [item["path"] for item in manifest["files"]]  # type: ignore[index]
    payload = {
        "status": "success",
        "handled_count": 2,
        "handled_paths": paths,
        "error": None,
        "summaries": [_entry(paths[0]), _entry(paths[1])],
    }

    result = validate_summary_payload(payload, expected_paths=paths)
    assert result.succeeded
    assert [entry.path for entry in result.summaries] == paths

    with pytest.raises(SummaryValidationError, match="unsupported field"):
        validate_summary_payload({**payload, "debug": True}, expected_paths=paths)
    with pytest.raises(SummaryValidationError, match="exactly"):
        validate_summary_payload({**payload, "handled_paths": list(reversed(paths))}, expected_paths=paths)
    with pytest.raises(SummaryValidationError, match="exactly"):
        validate_summary_payload(
            {**payload, "summaries": [_entry(paths[1]), _entry(paths[0])]}, expected_paths=paths
        )


def test_summary_preserves_nfd_manifest_key_while_accepting_equivalent_model_echo(tmp_path):
    exact_path = str(tmp_path / unicodedata.normalize("NFD", "BÉIS.pdf"))
    model_path = unicodedata.normalize("NFC", exact_path)
    manifest = {
        "files": [
            {
                "path": exact_path,
                "filename": Path(exact_path).name,
                "pdf_sha256": _sha("unicode-pdf"),
                "text_extract_profile": "extract-fixture-v1",
            }
        ]
    }

    identities = identities_for_manifest(manifest, prompt_version="prompt-v1", model="model-v1", reasoning="medium")
    result = validate_summary_payload(
        {
            "status": "success",
            "handled_count": 1,
            "handled_paths": [model_path],
            "error": None,
            "summaries": [_entry(model_path)],
        },
        expected_paths=(exact_path,),
    )

    assert list(identities) == [exact_path]
    assert result.handled_paths == (exact_path,)
    assert result.summaries[0].path == exact_path


def test_summary_store_reuses_only_exact_identity_and_refuses_conflicting_content(tmp_path):
    identity = SummaryIdentity(_sha("pdf"), "extract-v1", "prompt-v1", "model-v1", "low")
    different_reasoning = SummaryIdentity(_sha("pdf"), "extract-v1", "prompt-v1", "model-v1", "high")
    store = SummaryStore(tmp_path / "cache")
    entry = SummaryEntry(**_entry("/reports/alpha.pdf"))

    persisted = store.persist(identity, entry, source_metadata={"text_source": "fixture"})
    assert persisted.paths.json_path.is_file()
    assert persisted.paths.markdown_path.is_file()
    assert store.load(identity, expected_path=entry.path) == persisted
    assert store.load(different_reasoning, expected_path=entry.path) is None
    same_pdf_from_another_source = store.persist(
        identity,
        SummaryEntry(**_entry("/another-source/alpha-copy.pdf")),
    )
    assert same_pdf_from_another_source.entry.path == "/another-source/alpha-copy.pdf"
    assert same_pdf_from_another_source.paths == persisted.paths
    with pytest.raises(SummaryInvariantError, match="conflicting durable content"):
        store.persist(identity, SummaryEntry(**_entry(entry.path, markdown="# different")))


def test_summary_batch_artifacts_cache_and_state_all_preserve_reasoning(tmp_path):
    manifest = _manifest(tmp_path)
    identities = identities_for_manifest(manifest, prompt_version="prompt-v1", model="model-v1", reasoning="medium")
    paths = [item["path"] for item in manifest["files"]]  # type: ignore[index]
    provider_payload = {
        "status": "success",
        "handled_count": 2,
        "handled_paths": paths,
        "error": None,
        "summaries": [_entry(paths[0], markdown="# Alpha\n\n- A"), _entry(paths[1], markdown="# Beta\n\n- B")],
    }
    store = SummaryStore(tmp_path / "cache")
    output_json = tmp_path / "batch.summary.json"
    output_markdown = tmp_path / "batch.summary.md"

    artifact = persist_summary_batch(
        manifest,
        provider_payload,
        identities=identities,
        store=store,
        output_json=output_json,
        output_markdown=output_markdown,
    )

    assert artifact.summary_sha256 == _sha(output_markdown.read_text(encoding="utf-8").strip())
    emitted = json.loads(output_json.read_text(encoding="utf-8"))
    assert emitted["entries"][0]["summary_identity"]["reasoning"] == "medium"
    materialized = materialize_summary_cache(
        manifest,
        identities=identities,
        store=store,
        output_json=tmp_path / "cached.summary.json",
        output_markdown=tmp_path / "cached.summary.md",
    )
    assert materialized is not None
    assert materialized.output_markdown.read_text(encoding="utf-8") == output_markdown.read_text(encoding="utf-8")

    with PipelineState.open(tmp_path / "pipeline.sqlite3") as state:
        state.migrate()
        document = state.upsert_document("fixture", "alpha", filename="alpha.pdf", now=datetime(2026, 8, 10, tzinfo=UTC))
        recorded = record_summary_artifact(state, document.id, artifact.entries[0])
        assert recorded.reasoning == "medium"
        assert state.find_summary_artifact(identity=artifact.entries[0].identity) is not None


def test_build_inputs_only_reads_extracted_text(tmp_path):
    manifest = _manifest(tmp_path)
    inputs = build_summary_inputs(manifest)

    assert [item.path for item in inputs] == [item["path"] for item in manifest["files"]]  # type: ignore[index]
    assert inputs[0].text == "alpha extracted text"


def test_parallel_summary_primitive_caps_workers_and_returns_manifest_order():
    jobs = tuple(SummaryJob(job_id=name, expected_paths=(f"/{name}.pdf",)) for name in ("slow", "fast", "middle"))
    active = 0
    peak = 0
    lock = threading.Lock()

    def run(job: SummaryJob) -> str:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        time.sleep({"slow": 0.05, "fast": 0.01, "middle": 0.02}[job.job_id])
        with lock:
            active -= 1
        return job.job_id

    outcomes = run_summary_jobs(jobs, run, max_workers=2)

    assert peak <= 2
    assert [outcome.result for outcome in outcomes] == ["slow", "fast", "middle"]
    with pytest.raises(ValueError, match="between 1 and 2"):
        run_summary_jobs(jobs, run, max_workers=3)
