from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from zsxq_pipeline.download import DownloadOutcome


def test_download_result_exports_process_exit_code_and_deprecated_compatibility_alias():
    outcome = DownloadOutcome(
        run_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        source="foreign",
        status="success",
        reason_code="download_completed",
        reason_text="ok",
        window_start=datetime(2026, 8, 10, 8, tzinfo=UTC),
        window_end=datetime(2026, 8, 10, 12, tzinfo=UTC),
        plan_path=Path("/runtime/plans/plan.json"),
        plan_hash="a" * 64,
        manifest_path=Path("/runtime/manifests/run.json"),
        plan={"download_candidate_count": 1},
        downloaded_entries=({"filename": "fixture.pdf", "path": "/library/fixture.pdf"},),
        checkpoint_eligible=True,
        state_updated=True,
    )

    payload = outcome.to_dict()
    assert payload["process_exit_code"] == 0
    assert payload["codex_exit_code"] == 0
    assert payload["downloaded_files"] == ["fixture.pdf"]
    assert payload["scan_plan_hash"] == "a" * 64
    assert payload["error_detail"] == ""
