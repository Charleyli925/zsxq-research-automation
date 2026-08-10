from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zsxq_pipeline.extract import (
    ExtractionValidationError,
    ExtractorAdapter,
    classify_extraction_failure,
    record_extracted_text_artifact,
)
from zsxq_pipeline.model import ErrorCategory
from zsxq_pipeline.state import PipelineState


def _write_fake_extractor(path: Path, *, invalid_success: bool = False) -> None:
    source = f'''\
import json
import sys
from pathlib import Path

if "--preflight-only" in sys.argv:
    print(json.dumps({{"ok": True, "extractor_profile": "fixture-v1"}}))
    raise SystemExit(0)

batch_path = Path(sys.argv[sys.argv.index("--batch-file") + 1])
output_dir = Path(sys.argv[sys.argv.index("--output-dir") + 1])
payload = json.loads(batch_path.read_text(encoding="utf-8"))
for index, item in enumerate(payload["files"]):
    if index == 0:
        text_path = output_dir / "fixture-text.txt"
        if not {invalid_success!r}:
            text_path.write_text("可读正文", encoding="utf-8")
        item.update({{
            "text_extract_status": "success",
            "extracted_text_path": str(text_path),
            "extracted_text_chars": 4,
            "text_source": "fixture_extract",
            "text_extract_profile": "fixture-v1",
            "text_extract_cached": False,
            "text_extract_diagnostics": {{"strategy": "fixture"}},
        }})
    else:
        item.update({{
            "text_extract_status": "failed",
            "text_extract_error": "fixture PDF content is unreadable",
            "text_extract_error_type": "content_failure",
            "text_extract_error_code": "no_usable_text",
            "text_extract_retryable": False,
            "text_extract_profile": "fixture-v1",
        }})
batch_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
print(json.dumps({{"ok": True}}))
'''
    path.write_text(source, encoding="utf-8")


def _manifest(tmp_path: Path) -> dict[str, object]:
    first_pdf = tmp_path / "first.pdf"
    second_pdf = tmp_path / "second.pdf"
    first_pdf.write_bytes(b"first pdf")
    second_pdf.write_bytes(b"second pdf")
    return {
        "files": [
            {
                "path": str(first_pdf),
                "filename": first_pdf.name,
                "pdf_sha256": hashlib.sha256(first_pdf.read_bytes()).hexdigest(),
            },
            {
                "path": str(second_pdf),
                "filename": second_pdf.name,
                "pdf_sha256": hashlib.sha256(second_pdf.read_bytes()).hexdigest(),
            },
        ]
    }


def test_extractor_adapter_stages_manifest_and_keeps_content_failure_isolated(tmp_path):
    script = tmp_path / "fixture_extractor.py"
    _write_fake_extractor(script)
    batch_path = tmp_path / "batch.json"
    original = _manifest(tmp_path)
    batch_path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")

    adapter = ExtractorAdapter(script_path=script, python_executable=sys.executable, timeout_seconds=10)
    assert adapter.preflight()["ok"] is True
    result = adapter.extract_batch(batch_path, tmp_path / "out")

    assert len(result.successful) == 1
    assert len(result.failed) == 1
    assert result.successful[0].text_path is not None
    assert result.successful[0].text_path.read_text(encoding="utf-8") == "可读正文"
    assert result.failed[0].error_category is ErrorCategory.CONTENT
    committed = json.loads(batch_path.read_text(encoding="utf-8"))
    assert committed["files"][0]["text_extract_status"] == "success"
    assert committed["files"][1]["text_extract_status"] == "failed"
    assert not list((tmp_path / "out").glob(".extract-manifest-*.json"))


def test_invalid_success_never_replaces_original_manifest(tmp_path):
    script = tmp_path / "fixture_extractor.py"
    _write_fake_extractor(script, invalid_success=True)
    batch_path = tmp_path / "batch.json"
    original = _manifest(tmp_path)
    original_text = json.dumps(original, ensure_ascii=False, sort_keys=True)
    batch_path.write_text(original_text, encoding="utf-8")

    adapter = ExtractorAdapter(script_path=script, python_executable=sys.executable, timeout_seconds=10)
    with pytest.raises(ExtractionValidationError, match="extracted text is missing"):
        adapter.extract_batch(batch_path, tmp_path / "out")

    assert batch_path.read_text(encoding="utf-8") == original_text


@pytest.mark.parametrize(
    ("legacy_type", "expected"),
    [
        ("content_failure", ErrorCategory.CONTENT),
        ("env_failure", ErrorCategory.RELEASE_CONTRACT),
        ("transient_failure", ErrorCategory.TRANSIENT),
        ("auth_failure", ErrorCategory.AUTH),
        ("unknown", ErrorCategory.INVARIANT),
    ],
)
def test_legacy_failure_types_map_to_state_categories(legacy_type, expected):
    assert classify_extraction_failure(legacy_type) is expected


def test_successful_text_artifact_can_be_recorded_with_extractor_provenance(tmp_path):
    script = tmp_path / "fixture_extractor.py"
    _write_fake_extractor(script)
    batch_path = tmp_path / "batch.json"
    batch_path.write_text(json.dumps(_manifest(tmp_path), ensure_ascii=False), encoding="utf-8")
    result = ExtractorAdapter(script_path=script, python_executable=sys.executable).extract_batch(batch_path, tmp_path / "out")

    with PipelineState.open(tmp_path / "pipeline.sqlite3") as state:
        state.migrate()
        document = state.upsert_document("fixture", "one", filename="first.pdf", now=datetime(2026, 8, 10, tzinfo=UTC))
        artifact = record_extracted_text_artifact(state, document.id, result.successful[0])

    assert artifact.kind == "extracted_text"
    assert artifact.extractor_version == "fixture-v1"
