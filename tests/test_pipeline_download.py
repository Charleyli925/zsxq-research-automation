from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zsxq_pipeline.browser import BrowserSessionError
from zsxq_pipeline.download import DownloadPipeline, DownloadRequest
from zsxq_pipeline.model import Stage, StageState
from zsxq_pipeline.state import PipelineState


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


class FakeScanner:
    def __init__(self, plan: dict) -> None:
        self.plan = plan
        self.page = None

    def load_json(self, _path: Path) -> dict:
        return {"download_settings": {"staging_dir": "/fixture/staging"}}

    def load_persistent_config(self, _path: Path) -> dict:
        return {"schema_version": 2}

    def scan_window(self, page, **_kwargs) -> dict:
        self.page = page
        return dict(self.plan)


class FakeDownloader:
    def __init__(self, staging: Path, *, protected: bool = False) -> None:
        self.staging = staging
        self.protected = protected
        self.pages = []

    def choose_staging_dir(self, _config: dict) -> Path:
        return self.staging

    def download_candidate_on_page(self, candidate: dict, *, page, staging_dir: Path, **_kwargs) -> dict:
        self.pages.append(page)
        if self.protected:
            return {
                "status": "blocked",
                "reason_code": "source_content_protected",
                "file_id": candidate["file_id"],
                "filename": candidate["filename"],
            }
        staging_dir.mkdir(parents=True, exist_ok=True)
        payload = b"%PDF-1.7\n" + b"x" * 2048
        destination = staging_dir / candidate["filename"]
        destination.write_bytes(payload)
        return {
            "status": "downloaded",
            "reason_code": "download_completed",
            "file_id": candidate["file_id"],
            "filename": candidate["filename"],
            "path": str(destination),
            "size_bytes": destination.stat().st_size,
        }


class FakeSession:
    def __init__(self, *_args, **_kwargs) -> None:
        self.page = object()

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


class FailingSession(FakeSession):
    def __enter__(self):
        raise BrowserSessionError(
            "blocked_browser_cdp_unresponsive",
            "Target.createTarget timed out; connect_attempts=2; retry-1[observed=97,owned=97,closed=96,failed=0]",
        )


def _request(tmp_path: Path) -> DownloadRequest:
    return DownloadRequest(
        source="foreign",
        runtime_root=tmp_path / "runtime",
        database=tmp_path / "runtime" / "state" / "pipeline.sqlite3",
        job_config_path=tmp_path / "job.json",
        keyword_path=tmp_path / "keywords.json",
        legacy_state_path=tmp_path / "legacy-state.json",
        cdp_endpoint="http://127.0.0.1:9223",
        window_start=NOW - timedelta(hours=1),
        window_end=NOW,
        run_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )


def _plan() -> dict:
    return {
        "schema_version": 3,
        "plan_hash": "f" * 64,
        "window_new_docs_count": 1,
        "keyword_matched_docs_count": 1,
        "download_candidate_count": 1,
        "scan_mode": "api_first",
        "api_probe_status": "ok",
        "blocked_reason": None,
        "download_candidates": [
            {
                "file_id": "file-1",
                "filename": "fixture.pdf",
                "topic_url": "https://wx.zsxq.com/group/fixture/topic/1",
            }
        ],
    }


def test_download_pipeline_uses_one_page_and_commits_only_after_manifest_reconciliation(tmp_path):
    staging = tmp_path / "staging"
    archive = tmp_path / "archive" / "fixture.pdf"
    scanner = FakeScanner(_plan())
    downloader = FakeDownloader(staging)

    def finalizer(request, _plan_path, manifest_path, _run_id, _started_at):
        archive.parent.mkdir(parents=True, exist_ok=True)
        source = staging / "fixture.pdf"
        source.replace(archive)
        entry = {
            "source_file_id": "file-1",
            "filename": "fixture.pdf",
            "path": str(archive),
            "archive_path": str(archive),
            "pdf_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        }
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps({"downloaded_entries": [entry]}), encoding="utf-8")
        return {"files": [], "satisfied_candidates": []}

    request = _request(tmp_path)
    outcome = DownloadPipeline(
        browser_session_factory=FakeSession,
        scanner=scanner,
        downloader=downloader,
        finalizer_runner=finalizer,
    ).run(request)

    assert outcome.status == "success"
    assert outcome.checkpoint_eligible is True
    assert [entry["filename"] for entry in outcome.downloaded_entries] == ["fixture.pdf"]
    assert scanner.page is downloader.pages[0]
    with PipelineState.open(request.database) as state:
        assert state.latest_source_checkpoint("foreign") == NOW
        attempt = state.get_stage_attempt(1, Stage.DOWNLOAD, request.workflow_version)
        assert attempt is not None
        assert attempt["state"] == StageState.SUCCEEDED.value
        extraction = state.get_stage_attempt(1, Stage.TEXT_EXTRACT, request.extractor_version)
        assert extraction is not None
        assert extraction["state"] == StageState.QUEUED.value


def test_content_protection_is_terminal_but_allows_the_window_to_checkpoint(tmp_path):
    scanner = FakeScanner(_plan())
    downloader = FakeDownloader(tmp_path / "staging", protected=True)
    request = _request(tmp_path)
    outcome = DownloadPipeline(
        browser_session_factory=FakeSession,
        scanner=scanner,
        downloader=downloader,
    ).run(request)

    assert outcome.status == "success"
    assert outcome.reason_code == "source_content_protected"
    assert outcome.checkpoint_eligible is True
    with PipelineState.open(request.database) as state:
        assert state.latest_source_checkpoint("foreign") == NOW
        attempt = state.get_stage_attempt(1, Stage.DOWNLOAD, request.workflow_version)
        assert attempt is not None
        assert attempt["state"] == StageState.QUARANTINED.value


def test_browser_failure_persists_exact_diagnostics_before_scanning(tmp_path):
    scanner = FakeScanner(_plan())
    result_path = tmp_path / "result.json"
    request = replace(_request(tmp_path), result_path=result_path)

    outcome = DownloadPipeline(
        browser_session_factory=FailingSession,
        scanner=scanner,
        downloader=FakeDownloader(tmp_path / "staging"),
    ).run(request)

    assert outcome.status == "blocked"
    assert outcome.reason_code == "blocked_browser_cdp_unresponsive"
    assert "Target.createTarget timed out" in outcome.error_detail
    assert scanner.page is None
    plan = json.loads(outcome.plan_path.read_text(encoding="utf-8"))
    assert plan["blocked_reason"] == "blocked_browser_cdp_unresponsive"
    assert plan["blocked_detail"] == outcome.error_detail
    assert plan["scan_mode"] == "not_started"
    assert plan["api_probe_status"] == "not_started"
    assert plan["download_candidates"] == []
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["error_detail"] == outcome.error_detail
