from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zsxq_pipeline.browser import BrowserSessionError
from zsxq_pipeline.download import DOWNLOAD_RATE_LIMIT_REASON, DownloadPipeline, DownloadRequest
from zsxq_pipeline.model import ErrorCategory, Stage, StageState
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
        extraction = state.get_stage_attempt(1, Stage.TEXT_EXTRACT, f"extract:{request.extractor_version}")
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


def test_later_browser_exception_still_finalizes_completed_download(tmp_path):
    plan = _plan()
    plan["download_candidates"].append({"file_id": "file-2", "filename": "second.pdf", "topic_url": "https://example.invalid/2"})
    staging = tmp_path / "staging"
    downloader = FakeDownloader(staging)
    original = downloader.download_candidate_on_page

    def download(candidate, **kwargs):
        if candidate["file_id"] == "file-2":
            raise RuntimeError("CDP disconnected")
        return original(candidate, **kwargs)

    downloader.download_candidate_on_page = download
    archive = tmp_path / "archive.pdf"

    def finalize(request, plan_path, manifest_path, run_id, started_at):
        (staging / "fixture.pdf").replace(archive)
        return {"files": [{"source_file_id": "file-1", "filename": "fixture.pdf", "archive_path": str(archive),
                           "pdf_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}]}

    request = _request(tmp_path)
    outcome = DownloadPipeline(scanner=FakeScanner(plan), downloader=downloader,
                               browser_session_factory=FakeSession, finalizer_runner=finalize).run(request)
    assert not outcome.checkpoint_eligible
    with PipelineState.open(request.database) as state:
        assert state.get_stage_attempt(1, Stage.DOWNLOAD, request.workflow_version)["state"] == "succeeded"
        assert state.get_stage_attempt(2, Stage.DOWNLOAD, request.workflow_version)["state"] == "retry_wait"
        assert state.get_document(1).artifact_id is not None


def test_download_rate_limit_stops_remaining_candidates_and_defers_until_next_day(tmp_path):
    plan = _plan()
    plan["download_candidates"].append(
        {
            "file_id": "file-2",
            "filename": "second.pdf",
            "topic_url": "https://wx.zsxq.com/group/fixture/topic/2",
        }
    )
    plan["download_candidate_count"] = 2
    downloader = FakeDownloader(tmp_path / "staging")
    calls: list[str] = []

    def rate_limited(candidate, **_kwargs):
        calls.append(candidate["file_id"])
        return {
            "status": "blocked",
            "reason_code": DOWNLOAD_RATE_LIMIT_REASON,
            "source_api_code": 13607,
            "file_id": candidate["file_id"],
            "filename": candidate["filename"],
            "message": "下一个自然日自动恢复",
        }

    downloader.download_candidate_on_page = rate_limited
    request = _request(tmp_path)
    outcome = DownloadPipeline(
        browser_session_factory=FakeSession,
        scanner=FakeScanner(plan),
        downloader=downloader,
    ).run(request)

    assert calls == ["file-1"]
    assert outcome.status == "failed"
    assert outcome.reason_code == DOWNLOAD_RATE_LIMIT_REASON
    assert outcome.checkpoint_eligible is False
    with PipelineState.open(request.database) as state:
        first = state.get_stage_attempt(1, Stage.DOWNLOAD, request.workflow_version)
        second = state.get_stage_attempt(2, Stage.DOWNLOAD, request.workflow_version)
        assert first is not None and first["state"] == StageState.RETRY_WAIT.value
        assert first["error_code"] == DOWNLOAD_RATE_LIMIT_REASON
        assert first["available_at_iso"].endswith("16:00:00Z")
        assert second is not None and second["state"] == StageState.QUEUED.value


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


def test_scanner_auth_failure_retries_the_original_window_after_login(tmp_path):
    request = _request(tmp_path)
    scanner = FakeScanner({**_plan(), "blocked_reason": "need_reauth", "download_candidates": []})
    downloader = FakeDownloader(tmp_path / "staging")
    pipeline = DownloadPipeline(browser_session_factory=FakeSession, scanner=scanner, downloader=downloader)

    first = pipeline.run(request)

    assert first.status == "blocked"
    assert first.reason_code == "need_reauth"
    assert first.checkpoint_eligible is False
    assert downloader.pages == []
    with PipelineState.open(request.database) as state:
        window = state.list_source_windows()[0]
        assert window["status"] == "blocked_auth"
        assert state.latest_source_checkpoint(request.source) is None

    scanner.plan = {
        **_plan(), "blocked_reason": None, "window_new_docs_count": 0,
        "download_candidate_count": 0, "download_candidates": [],
    }
    resumed = pipeline.run(replace(request, run_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"))

    assert resumed.status == "success"
    with PipelineState.open(request.database) as state:
        assert state.table_count("source_windows") == 1
        recovered = state.list_source_windows()[0]
        assert recovered["id"] == window["id"]
        assert recovered["status"] == "succeeded"
        assert recovered["window_start_iso"] == window["window_start_iso"]
        assert recovered["window_end_iso"] == window["window_end_iso"]
        assert state.latest_source_checkpoint(request.source) == request.window_end


def test_mid_download_auth_wait_preserves_checkpoint_and_completed_files(tmp_path, monkeypatch):
    requests = [
        replace(_request(tmp_path), run_id=f"{letter * 8}-{letter * 4}-4{letter * 3}-8{letter * 3}-{letter * 12}")
        for letter in "abc"
    ]
    now = [NOW]

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now[0].astimezone(tz) if tz else now[0].replace(tzinfo=None)

    monkeypatch.setattr("zsxq_pipeline.download.datetime", FrozenDatetime)
    monkeypatch.setattr("zsxq_pipeline.state.utc_now", lambda: now[0])
    plan = _plan()
    plan["download_candidates"] = [
        {"file_id": f"file-{index}", "filename": f"fixture-{index}.pdf"}
        for index in range(1, 4)
    ]
    plan["download_candidate_count"] = 3
    downloader = FakeDownloader(tmp_path / "staging")
    original_download = downloader.download_candidate_on_page
    calls = []

    def download(candidate, **kwargs):
        calls.append(candidate["file_id"])
        if candidate["file_id"] == "file-2" and now[0] < NOW + timedelta(minutes=5):
            return {"status": "blocked", "reason_code": "need_reauth", **candidate}
        return original_download(candidate, **kwargs)

    downloader.download_candidate_on_page = download

    def finalize(_request, _plan_path, _manifest_path, _run_id, _started_at):
        entries = []
        for candidate in plan["download_candidates"]:
            source = downloader.staging / candidate["filename"]
            if not source.is_file():
                continue
            destination = tmp_path / "archive" / source.name
            destination.parent.mkdir(exist_ok=True)
            source.replace(destination)
            entries.append({
                "source_file_id": candidate["file_id"], "filename": candidate["filename"],
                "archive_path": str(destination), "pdf_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            })
        return {"files": entries}

    pipeline = DownloadPipeline(
        browser_session_factory=FakeSession, scanner=FakeScanner(plan),
        downloader=downloader, finalizer_runner=finalize,
    )
    first = pipeline.run(requests[0])

    assert calls == ["file-1", "file-2"]
    assert first.status == "blocked"
    assert first.reason_code == "need_reauth"
    assert len(first.downloaded_entries) == 1
    assert first.checkpoint_eligible is False
    with PipelineState.open(requests[0].database) as state:
        assert state.latest_source_checkpoint("foreign") is None
        assert state.list_source_windows()[0]["status"] == "blocked_auth"
        first_stage = state.get_stage_attempt(1, Stage.DOWNLOAD, requests[0].workflow_version)
        auth_stage = state.get_stage_attempt(2, Stage.DOWNLOAD, requests[0].workflow_version)
        assert first_stage["state"] == StageState.SUCCEEDED.value
        assert auth_stage["state"] == StageState.RETRY_WAIT.value
        assert auth_stage["error_category"] == ErrorCategory.AUTH.value
        assert auth_stage["available_at_epoch"] == int((NOW + timedelta(minutes=5)).timestamp())
        assert state.get_stage_attempt(3, Stage.DOWNLOAD, requests[0].workflow_version)["attempt_count"] == 0

    now[0] += timedelta(minutes=4)
    waiting = pipeline.run(requests[1])
    assert calls == ["file-1", "file-2"]
    assert waiting.checkpoint_eligible is False
    with PipelineState.open(requests[0].database) as state:
        assert state.latest_source_checkpoint("foreign") is None

    now[0] += timedelta(minutes=1)
    resumed = pipeline.run(requests[2])
    assert calls == ["file-1", "file-2", "file-2", "file-3"]
    assert resumed.status == "success"
    assert resumed.checkpoint_eligible is True
    assert len(resumed.satisfied_entries) == 1
    with PipelineState.open(requests[0].database) as state:
        assert state.latest_source_checkpoint("foreign") == NOW
        assert all(state.get_stage_attempt(index, Stage.DOWNLOAD, requests[0].workflow_version)["state"] == StageState.SUCCEEDED.value for index in range(1, 4))


@pytest.mark.parametrize(
    ("category", "error_code", "terminal_state", "checkpoint_eligible"),
    [
        (ErrorCategory.AUTH, "permission_denied", StageState.BLOCKED_AUTH, False),
        (ErrorCategory.CONTENT, "source_content_protected", StageState.QUARANTINED, True),
        (ErrorCategory.RELEASE_CONTRACT, "release_invalid", StageState.BLOCKED_RELEASE, False),
    ],
)
def test_rescan_does_not_release_permission_content_or_contract_failures(
    tmp_path, category, error_code, terminal_state, checkpoint_eligible,
):
    request = _request(tmp_path)
    with PipelineState.open(request.database) as state:
        state.migrate()
        document = state.upsert_document(request.source, "file-1", filename="fixture.pdf")
        state.ensure_stage(document.id, Stage.DOWNLOAD, request.workflow_version)
        claim = state.claim_due_stage(Stage.DOWNLOAD, request.workflow_version, document_ids=(document.id,))
        state.fail_stage(claim, category=category, error_code=error_code)
    downloader = FakeDownloader(tmp_path / "staging")

    outcome = DownloadPipeline(
        browser_session_factory=FakeSession, scanner=FakeScanner(_plan()), downloader=downloader,
    ).run(request)

    assert downloader.pages == []
    assert outcome.checkpoint_eligible is checkpoint_eligible
    with PipelineState.open(request.database) as state:
        row = state.get_stage_attempt(document.id, Stage.DOWNLOAD, request.workflow_version)
        assert row["state"] == terminal_state.value
        assert row["attempt_count"] == 1


@pytest.mark.parametrize("rescan", ["empty", "source_replaced", "keywords_changed"])
def test_prior_plan_debt_survives_changed_scan_until_original_file_returns(tmp_path, monkeypatch, rescan):
    now = [NOW]

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now[0].astimezone(tz) if tz else now[0].replace(tzinfo=None)

    monkeypatch.setattr("zsxq_pipeline.download.datetime", FrozenDatetime)
    monkeypatch.setattr("zsxq_pipeline.state.utc_now", lambda: now[0])
    request = _request(tmp_path)
    scanner = FakeScanner(_plan())
    downloader = FakeDownloader(tmp_path / "staging")
    original_download = downloader.download_candidate_on_page
    calls = []

    def download(candidate, **kwargs):
        calls.append(candidate["file_id"])
        if candidate["file_id"] == "file-1" and now[0] == NOW:
            return {"status": "blocked", "reason_code": "need_reauth", **candidate}
        return original_download(candidate, **kwargs)

    downloader.download_candidate_on_page = download

    def finalize(_request, _plan_path, _manifest_path, _run_id, _started_at):
        entries = []
        for source in downloader.staging.glob("*.pdf"):
            destination = tmp_path / "archive" / source.name
            destination.parent.mkdir(exist_ok=True)
            source.replace(destination)
            entries.append({
                "source_file_id": "file-1" if source.name == "fixture.pdf" else "file-2",
                "filename": source.name, "archive_path": str(destination),
                "pdf_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            })
        return {"files": entries}

    pipeline = DownloadPipeline(
        browser_session_factory=FakeSession, scanner=scanner,
        downloader=downloader, finalizer_runner=finalize,
    )
    first = pipeline.run(request)
    assert first.reason_code == "need_reauth"
    assert first.checkpoint_eligible is False

    now[0] += timedelta(minutes=6)
    scanner.plan = {
        **_plan(), "plan_hash": "0" * 64, "window_new_docs_count": 0,
        "keyword_matched_docs_count": 0, "download_candidate_count": 0, "download_candidates": [],
    }
    if rescan == "source_replaced":
        scanner.plan.update({
            "window_new_docs_count": 1, "keyword_matched_docs_count": 1, "download_candidate_count": 1,
            "download_candidates": [{"file_id": "file-2", "filename": "replacement.pdf"}],
        })
    elif rescan == "keywords_changed":
        scanner.plan["window_new_docs_count"] = 1
    second = pipeline.run(replace(request, run_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"))

    assert second.checkpoint_eligible is False
    assert {item["source_file_id"] for item in second.missing_entries} == {"file-1"}
    with PipelineState.open(request.database) as state:
        assert state.latest_source_checkpoint(request.source) is None
        debt = state.list_window_download_debts(state.list_source_windows()[0]["id"], request.workflow_version)
        assert len(debt) == 1 and debt[0]["source_file_id"] == "file-1"
        assert debt[0]["state"] == StageState.RETRY_WAIT.value
        assert state.get_stage_attempt(debt[0]["document_id"], Stage.DOWNLOAD, request.workflow_version)["attempt_count"] == 1

    # The pending plan member is found after login/source repair, so its
    # original claim can finish without silently dropping it from coverage.
    scanner.plan = _plan()
    resumed = pipeline.run(replace(request, run_id="cccccccc-cccc-4ccc-8ccc-cccccccccccc"))
    assert resumed.status == "success"
    assert resumed.checkpoint_eligible is True
    assert calls.count("file-1") == 2
    with PipelineState.open(request.database) as state:
        window_id = state.list_source_windows()[0]["id"]
        assert state.list_window_download_debts(window_id, request.workflow_version) == []
        assert state.latest_source_checkpoint(request.source) == request.window_end


@pytest.mark.parametrize(("category", "error_code", "eligible"), [
    (None, "", True),
    (ErrorCategory.CONTENT, "source_content_protected", True),
    (ErrorCategory.AUTH, "permission_denied", False),
    (ErrorCategory.RELEASE_CONTRACT, "release_invalid", False),
    (ErrorCategory.CONTENT, "other_content_error", False),
])
def test_empty_scan_settles_only_success_or_explicit_protection(tmp_path, category, error_code, eligible):
    request = _request(tmp_path)
    with PipelineState.open(request.database) as state:
        state.migrate()
        window_id = state.register_source_window(
            request.source, request.window_start, request.window_end, status="partial",
        )
        document = state.upsert_document(
            request.source, "file-1", filename="fixture.pdf", source_window_id=window_id,
        )
        state.ensure_stage(document.id, Stage.DOWNLOAD, request.workflow_version)
        claim = state.claim_due_stage(Stage.DOWNLOAD, request.workflow_version, document_ids=(document.id,))
        if category is None:
            state.complete_stage(claim)
        else:
            state.fail_stage(claim, category=category, error_code=error_code)
        before = state.get_stage_attempt(document.id, Stage.DOWNLOAD, request.workflow_version)
    empty = {**_plan(), "window_new_docs_count": 0, "download_candidate_count": 0, "download_candidates": []}
    downloader = FakeDownloader(tmp_path / "staging")
    outcome = DownloadPipeline(
        browser_session_factory=FakeSession, scanner=FakeScanner(empty), downloader=downloader,
    ).run(request)
    assert outcome.checkpoint_eligible is eligible
    assert downloader.pages == []
    with PipelineState.open(request.database) as state:
        after = state.get_stage_attempt(document.id, Stage.DOWNLOAD, request.workflow_version)
        assert after == before
        assert (state.latest_source_checkpoint(request.source) is not None) is eligible
