from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from types import SimpleNamespace
from unittest import mock

import pytest

from zsxq_pipeline.config import load_pipeline_config
from zsxq_pipeline.download import DOWNLOAD_RATE_LIMIT_REASON, DownloadPipeline
from zsxq_pipeline.lock import runtime_lock
from zsxq_pipeline.model import ErrorCategory, Stage
from zsxq_pipeline.notify import (
    BATCH_COMPLETE_EVENT,
    DOWNLOAD_BLOCKED_EVENT,
    DOWNLOAD_COMPLETE_EVENT,
    SUMMARY_PROGRESS_EVENT,
    SUMMARY_STARTED_EVENT,
)
from zsxq_pipeline.state import PipelineState
from zsxq_pipeline.worker import PipelineWorker


def _config(tmp_path):
    path = tmp_path / "pipeline.toml"
    path.write_text(
        f'''schema_version = 1

[runtime]
root = "{tmp_path / 'runtime'}"
database = "state/pipeline.sqlite3"

[schedule]
timezone = "Asia/Shanghai"
tick_budget_seconds = 30
download_quota = 4
process_quota = 4
outbox_quota = 4
max_catchup_seconds = 86400

[model]
name = "test-model"
provider = "codex"
prompt_version = "test"

[sources.foreign]
kind = "zsxq"
state_path = "state/foreign.json"
job_config = "{tmp_path / 'foreign-job.json'}"
keyword_file = "{tmp_path / 'foreign-keywords.json'}"
cdp_endpoint = "http://127.0.0.1:9223"
schedule_times = ["16:00"]

[sources.domestic]
kind = "zsxq"
state_path = "state/domestic.json"
job_config = "{tmp_path / 'domestic-job.json'}"
keyword_file = "{tmp_path / 'domestic-keywords.json'}"
cdp_endpoint = "http://127.0.0.1:9224"
schedule_times = ["16:00"]

[publish_targets.daily]
kind = "lark"
target = "test"
''',
        encoding="utf-8",
    )
    return load_pipeline_config(path)


def _notification_config(tmp_path):
    config = _config(tmp_path)
    return replace(
        config,
        lark=replace(config.lark, notifications_enabled=True, target_chat_id="oc_test"),
    )


@pytest.mark.parametrize("quota", [1, 2])
def test_failed_old_windows_cannot_starve_other_sources_or_new_windows(tmp_path, quota):
    config = _config(tmp_path)
    config = replace(config, schedule=replace(config.schedule, download_quota=quota))
    now = [datetime(2026, 11, 11, 8, tzinfo=UTC)]
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        for index in range(3):
            state.register_source_window("foreign", now[0] - timedelta(days=3),
                                         now[0] - timedelta(days=2, minutes=-index), status="partial")
        state.register_source_window("domestic", now[0] - timedelta(days=1), now[0], status="scheduled")
    attempts = []

    def download(request):
        attempts.append((request.source, request.window_end))
        raise RuntimeError("synthetic transport failure before registration")

    # Recreate the worker on every invocation to verify restart persistence.
    for _ in range(6):
        PipelineWorker(config, clock=lambda: now[0], download_runner=download).run_stage("download")
        now[0] += timedelta(minutes=5)
    assert {source for source, _ in attempts[:2]} == {"foreign", "domestic"}
    assert len({end for source, end in attempts if source == "foreign"}) == 3


def test_upgrade_restores_owed_attachment_from_old_plan_before_checkpoint_skip(tmp_path):
    config = _config(tmp_path)
    now = datetime(2026, 11, 11, 8, tzinfo=UTC)
    start, end = now - timedelta(days=2), now - timedelta(days=1)
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        window = state.register_source_window("foreign", start, end, status="partial")
        state.register_source_window("foreign", start, now, status="succeeded", checkpoint_eligible=True)
    plan_root = config.runtime.root / "plans/foreign"
    plan_root.mkdir(parents=True)
    (plan_root / "old.json").write_text(json.dumps({
        "window_start": start.isoformat(), "window_end": end.isoformat(),
        "download_candidates": [{"file_id": "owed", "filename": "owed.pdf"}],
    }))
    requests = []
    worker = PipelineWorker(config, download_runner=lambda request: requests.append(request) or
                            SimpleNamespace(status="failed", source="foreign", reason_code="download_incomplete"))
    worker.run_stage("download")
    assert len(requests) == 1 and requests[0].window_start == start
    with PipelineState.open(config.runtime.database) as state:
        assert len(state.list_window_download_debts(window, "download:v1")) == 1


def test_daily_search_reconciliation_runs_once_per_day_and_records_progress(tmp_path):
    config = _config(tmp_path)
    library = config.runtime.root / "projections" / "ResearchLibrary"
    vault = config.runtime.root / "projections" / "ResearchVault"
    database = config.runtime.root / "state" / "research_library.sqlite"
    config = replace(
        config,
        pipeline=replace(
            config.pipeline,
            research_library_root=library,
            research_library_database=database,
            obsidian_vault_root=vault,
        ),
    )
    now = [datetime(2026, 9, 24, 3, 0, tzinfo=UTC)]
    worker = PipelineWorker(config, clock=lambda: now[0])
    response = SimpleNamespace(returncode=0, stdout=json.dumps({"complete": True, "seen": 2, "updated": 1, "pending": 0, "deleted": 0, "errors": 0}))
    with mock.patch("zsxq_pipeline.worker.subprocess.run", return_value=response) as run:
        worker._maybe_reconcile_search()
        worker._maybe_reconcile_search()
        assert run.call_count == 1
        assert "--max-updates" not in run.call_args.args[0]
        assert "timeout" not in run.call_args.kwargs
        marker = config.runtime.root / "state" / "kb-reconcile.json"
        assert json.loads(marker.read_text(encoding="utf-8"))["last_completed_date"] == "2026-09-24"
        now[0] += timedelta(days=1)
        worker._maybe_reconcile_search()
        assert run.call_count == 2


def test_tick_isolates_source_failures_and_still_drains_outbox(tmp_path):
    config = _config(tmp_path)
    attempted: list[str] = []

    def download(request):
        attempted.append(request.source)
        if request.source == "foreign":
            raise RuntimeError("synthetic browser outage")
        return SimpleNamespace(status="success", source=request.source)

    delivered = (SimpleNamespace(idempotency_key="k", event="document_ready", status="sent", deferred=False, error=""),)
    now = datetime(2026, 8, 10, 8, 10, tzinfo=UTC)
    worker = PipelineWorker(
        config,
        clock=lambda: now,
        download_runner=download,
        process_runner=lambda source, rows: SimpleNamespace(status="success"),
        outbox_runner=lambda max_items: delivered,
    )
    outcome = worker.tick()
    assert attempted == ["foreign", "domestic"]
    assert outcome.status == "partial"
    assert outcome.downloaded == 1
    assert outcome.notifications == delivered
    assert any(item.startswith("download:foreign") for item in outcome.failures)


def test_process_stage_uses_existing_source_identity_and_respects_quota(tmp_path):
    config = _config(tmp_path)
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"synthetic-pdf")
    seen = []
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        document = state.upsert_document("foreign", "source-file-1", filename=pdf.name, source_path=pdf)
        state.record_artifact(document.id, kind="pdf", path=pdf, pdf_sha256="a" * 64, size_bytes=pdf.stat().st_size)

    def process(source, rows):
        seen.append((source, rows))
        return SimpleNamespace(status="success")

    worker = PipelineWorker(
        config,
        process_runner=process,
        download_runner=lambda request: SimpleNamespace(status="success", source=request.source),
        outbox_runner=lambda max_items: (),
    )
    outcome = worker.run_stage("process")
    assert outcome.status == "success"
    assert outcome.processed == 1
    assert seen[0][0] == "foreign"
    assert seen[0][1][0]["source_file_id"] == "source-file-1"


def test_process_partial_result_is_visible_in_the_top_level_tick(tmp_path):
    config = _config(tmp_path)
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"synthetic-pdf")
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        document = state.upsert_document("foreign", "source-file-1", filename=pdf.name, source_path=pdf)
        state.record_artifact(document.id, kind="pdf", path=pdf, pdf_sha256="a" * 64, size_bytes=pdf.stat().st_size)

    worker = PipelineWorker(
        config,
        process_runner=lambda source, rows: SimpleNamespace(
            status="partial",
            failures=("local projection failed",),
        ),
        download_runner=lambda request: SimpleNamespace(status="success", source=request.source),
        outbox_runner=lambda max_items: (),
    )

    outcome = worker.run_stage("process")

    assert outcome.status == "partial"
    assert outcome.failures == ("process:foreign:partial:1",)


def test_all_stage_drains_prior_outbox_before_process_overruns_budget(tmp_path):
    config = _config(tmp_path)
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"synthetic-pdf")
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        document = state.upsert_document("foreign", "source-file-1", filename=pdf.name, source_path=pdf)
        state.record_artifact(document.id, kind="pdf", path=pdf, pdf_sha256="a" * 64, size_bytes=pdf.stat().st_size)

    now = [0.0]
    events: list[str] = []
    delivered = (
        SimpleNamespace(
            idempotency_key="prior",
            event="document_ready",
            status="sent",
            deferred=False,
            error="",
        ),
    )

    def process(source, rows):
        events.append("process")
        now[0] = 2.0
        return SimpleNamespace(status="success")

    def outbox(max_items):
        events.append("outbox")
        return delivered

    worker = PipelineWorker(
        config,
        monotonic=lambda: now[0],
        process_runner=process,
        download_runner=lambda request: SimpleNamespace(status="success", source=request.source),
        outbox_runner=outbox,
    )
    outcome = worker.run_stage("all", budget_seconds=1)

    assert events == ["outbox", "process"]
    assert outcome.processed == 1
    assert outcome.notifications == delivered
    assert outcome.budget_exhausted is True


def test_tick_returns_busy_without_mutating_schedule_state(tmp_path):
    config = _config(tmp_path)
    now = datetime(2026, 8, 10, 8, 10, tzinfo=UTC)
    worker = PipelineWorker(config, clock=lambda: now)
    with runtime_lock(config.runtime.root) as acquired:
        assert acquired
        outcome = worker.tick()
    assert outcome.status == "busy"
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        assert state.table_count("source_windows") == 0


def test_time_budget_exits_before_claiming_download_work(tmp_path):
    config = _config(tmp_path)
    calls: list[str] = []
    values = iter((0.0, 2.0))
    worker = PipelineWorker(
        config,
        clock=lambda: datetime(2026, 8, 10, 8, 10, tzinfo=UTC),
        monotonic=lambda: next(values, 2.0),
        download_runner=lambda request: calls.append(request.source) or SimpleNamespace(status="success", source=request.source),
        outbox_runner=lambda max_items: (),
    )
    outcome = worker.tick(budget_seconds=1)
    assert outcome.budget_exhausted is True
    assert calls == []


def test_covered_later_window_is_settled_without_a_second_browser_run(tmp_path):
    config = _config(tmp_path)
    start = datetime(2026, 8, 10, 7, 0, tzinfo=UTC)
    end = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    calls: list[str] = []
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        state.register_source_window("foreign", start, end, status="succeeded", checkpoint_eligible=True)
        state.schedule_source_window(
            "foreign",
            start,
            end - timedelta(minutes=15),
            due_cursor=end - timedelta(minutes=30),
            truncated=False,
        )
        row = state.list_source_windows(statuses=("scheduled",))[0]
    worker = PipelineWorker(
        config,
        download_runner=lambda request: calls.append(request.source) or SimpleNamespace(status="success", source=request.source),
        outbox_runner=lambda max_items: (),
    )
    assert worker._run_download_window(row) is None
    assert calls == []


def test_successful_nonempty_download_queues_one_exact_count_status(tmp_path):
    config = _notification_config(tmp_path)
    start = datetime(2026, 8, 10, 7, 0, tzinfo=UTC)
    end = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        state.schedule_source_window(
            "foreign",
            start,
            end,
            due_cursor=end,
            truncated=False,
        )

    def download(request):
        return SimpleNamespace(
            status="success",
            source=request.source,
            checkpoint_eligible=True,
            downloaded_entries=tuple({"filename": f"report-{index}.pdf"} for index in range(1, 4)),
        )

    outcome = PipelineWorker(
        config,
        download_runner=download,
        outbox_runner=lambda max_items: (),
    ).run_stage("download")

    assert outcome.status == "success"
    with PipelineState.open(config.runtime.database) as state:
        notifications = state.list_notifications()
        assert [item.event for item in notifications] == [DOWNLOAD_COMPLETE_EVENT]
        assert "本轮新增 **3** 份 PDF" in notifications[0].payload["markdown"]


def test_blocked_download_exposes_reason_and_queues_one_retry_alert(tmp_path):
    config = _notification_config(tmp_path)
    start = datetime(2026, 8, 10, 7, 0, tzinfo=UTC)
    end = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        state.schedule_source_window(
            "foreign",
            start,
            end,
            due_cursor=end,
            truncated=False,
        )

    def download(request):
        return SimpleNamespace(
            status="blocked",
            source=request.source,
            reason_code="blocked_browser_cdp_unresponsive",
            checkpoint_eligible=False,
            downloaded_entries=(),
        )

    worker = PipelineWorker(config, download_runner=download, outbox_runner=lambda max_items: ())
    first = worker.run_stage("download")
    second = worker.run_stage("download")

    assert first.failures == (
        "download:foreign:blocked:blocked_browser_cdp_unresponsive",
    )
    assert second.failures == first.failures
    with PipelineState.open(config.runtime.database) as state:
        notifications = state.list_notifications()
        assert [item.event for item in notifications] == [DOWNLOAD_BLOCKED_EVENT]
        assert "`blocked_browser_cdp_unresponsive`" in notifications[0].payload["markdown"]


def test_account_wide_download_rate_limit_stops_the_remaining_tick_quota(tmp_path):
    config = _config(tmp_path)
    start = datetime(2026, 8, 10, 7, 0, tzinfo=UTC)
    end = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        state.schedule_source_window("foreign", start, end, due_cursor=end, truncated=False)
        state.schedule_source_window("domestic", start, end, due_cursor=end, truncated=False)

    calls: list[str] = []

    def download(request):
        calls.append(request.source)
        return SimpleNamespace(
            status="failed",
            source=request.source,
            reason_code=DOWNLOAD_RATE_LIMIT_REASON,
            checkpoint_eligible=False,
            downloaded_entries=(),
        )

    outcome = PipelineWorker(
        config,
        download_runner=download,
        outbox_runner=lambda max_items: (),
    ).run_stage("download")

    assert calls == ["foreign"]
    assert outcome.failures == (
        f"download:foreign:failed:{DOWNLOAD_RATE_LIMIT_REASON}",
    )


def test_process_statuses_are_one_start_one_milestone_then_one_completion(tmp_path):
    config = _notification_config(tmp_path)
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        window = state.register_source_window(
            "foreign",
            datetime(2026, 8, 10, 7, 0, tzinfo=UTC),
            datetime(2026, 8, 10, 8, 0, tzinfo=UTC),
        )
        for index, seed in enumerate("abcdef12", start=1):
            document = state.upsert_document(
                "foreign",
                f"source-file-{index}",
                filename=f"report-{index}.pdf",
                source_path=f"/library/report-{index}.pdf",
                source_window_id=window,
            )
            state.record_artifact(
                document.id,
                kind="pdf",
                path=f"/library/report-{index}.pdf",
                pdf_sha256=seed * 64,
            )
            extractor_version = config.pipeline.extractor_version or "ocr-geometry-v2"
            workflow = f"extract:{extractor_version}"
            state.ensure_stage(document.id, Stage.TEXT_EXTRACT, workflow)
            claim = state.claim_due_stage(Stage.TEXT_EXTRACT, workflow, document_ids=(document.id,))
            assert claim is not None
            state.complete_stage(claim)

    def process(source, rows):
        with PipelineState.open(config.runtime.database) as state:
            state.migrate()
            for row in rows:
                for stage, workflow in (
                    (Stage.SUMMARY, "summary:test"),
                    (Stage.PUBLISH, "publish:test"),
                ):
                    state.ensure_stage(int(row["id"]), stage, workflow)
                    claim = state.claim_due_stage(stage, workflow, document_ids=(int(row["id"]),))
                    assert claim is not None
                    state.complete_stage(claim)
        return SimpleNamespace(status="success", failures=())

    worker = PipelineWorker(
        config,
        process_runner=process,
        outbox_runner=lambda max_items: (),
    )
    assert worker.run_stage("process").processed == 4
    with PipelineState.open(config.runtime.database) as state:
        first_events = [item.event for item in state.list_notifications()]
        assert first_events == [SUMMARY_STARTED_EVENT, SUMMARY_PROGRESS_EVENT]
        progress = state.list_notifications()[-1]
        assert "进度 50%" in progress.payload["markdown"]
        assert "总结 **4/8**｜发布 **4/8**" in progress.payload["markdown"]

    assert worker.run_stage("process").processed == 4
    assert worker.run_stage("process").processed == 0
    with PipelineState.open(config.runtime.database) as state:
        notifications = state.list_notifications()
        assert [item.event for item in notifications] == [
            SUMMARY_STARTED_EVENT,
            SUMMARY_PROGRESS_EVENT,
            BATCH_COMPLETE_EVENT,
        ]
        assert "总结 **8/8**｜发布 **8/8**" in notifications[-1].payload["markdown"]
        assert notifications[-1].payload["terminal"] is True


@pytest.mark.parametrize("pending_status", ["blocked_auth", "partial", "running"])
def test_download_stage_resumes_original_pending_window_without_a_new_schedule(tmp_path, pending_status):
    config = _config(tmp_path)
    start = datetime(2026, 8, 10, 7, 0, tzinfo=UTC)
    end = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        original_id = state.register_source_window("foreign", start, end, status=pending_status)
        generic_id = state.register_source_window(
            "domestic", start - timedelta(days=1), end - timedelta(days=1), status="blocked",
        )
    calls = []

    def download(request):
        calls.append((request.source, request.window_start, request.window_end))
        return SimpleNamespace(status="success", source=request.source, checkpoint_eligible=True)

    outcome = PipelineWorker(config, download_runner=download, outbox_runner=lambda max_items: ()).run_stage("download")

    assert outcome.status == "success"
    assert calls == [("foreign", start, end)]
    with PipelineState.open(config.runtime.database) as state:
        windows = {row["id"]: row for row in state.list_source_windows()}
        assert windows[original_id]["status"] == "succeeded"
        assert windows[original_id]["checkpoint_eligible"] == 1
        assert windows[generic_id]["status"] == "blocked"
        assert windows[generic_id]["checkpoint_eligible"] == 0
        assert state.table_count("source_windows") == 2


def test_auth_failure_skips_sibling_windows_but_keeps_other_source_and_local_work(tmp_path):
    config = _notification_config(tmp_path)
    start = datetime(2026, 8, 10, 7, 0, tzinfo=UTC)
    end = datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        # More siblings than the quota must not hide another source behind
        # windows skipped after the first login failure.
        for index in range(config.schedule.download_quota + 1):
            state.register_source_window("foreign", start, end + timedelta(minutes=index), status="blocked_auth")
        state.register_source_window("domestic", start, end + timedelta(minutes=10), status="scheduled")
        pdf = tmp_path / "already-downloaded.pdf"
        pdf.write_bytes(b"synthetic-existing-pdf")
        document = state.upsert_document("foreign", "existing", filename=pdf.name, source_path=pdf)
        state.record_artifact(document.id, kind="pdf", path=pdf, pdf_sha256="a" * 64)
    calls = []
    processes = []
    drains = []

    def download(request):
        calls.append(request.source)
        blocked = request.source == "foreign"
        with PipelineState.open(config.runtime.database) as state:
            state.register_source_window(
                request.source, request.window_start, request.window_end,
                status="blocked_auth" if blocked else "succeeded", checkpoint_eligible=not blocked,
            )
        return SimpleNamespace(
            status="blocked" if blocked else "success", source=request.source,
            reason_code="need_reauth" if blocked else "download_completed", checkpoint_eligible=not blocked,
            downloaded_entries=(),
        )

    def process(source, rows):
        processes.append(source)
        return SimpleNamespace(status="success")

    def outbox(max_items):
        drains.append(max_items)
        return ()

    worker = PipelineWorker(config, download_runner=download, process_runner=process, outbox_runner=outbox)
    first = worker.run_stage("all")
    second = worker.run_stage("download")

    assert first.status == "partial"
    assert first.downloaded == 1
    assert first.processed == 1
    assert processes == ["foreign"]
    assert drains
    assert calls == ["foreign", "domestic", "foreign"]
    assert second.failures == ("download:foreign:blocked:need_reauth",)
    with PipelineState.open(config.runtime.database) as state:
        alerts = [item for item in state.list_notifications() if item.event == DOWNLOAD_BLOCKED_EVENT]
        assert len(alerts) == 1
        assert state.latest_source_checkpoint("foreign") is None
        assert state.latest_source_checkpoint("domestic") == end + timedelta(minutes=10)


@pytest.mark.parametrize("checkpoint_minutes", [30, 90])
def test_download_debt_keeps_original_window_despite_an_overlapping_checkpoint(tmp_path, checkpoint_minutes):
    config = _config(tmp_path)
    start = datetime(2026, 8, 10, 7, 0, tzinfo=UTC)
    end = start + timedelta(hours=1)
    checkpoint = start + timedelta(minutes=checkpoint_minutes)
    workflow = config.sources["foreign"].workflow_version
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        original_id = state.register_source_window("foreign", start, end, status="blocked_auth")
        document = state.upsert_document("foreign", "owed-file", filename="owed.pdf", source_window_id=original_id)
        state.ensure_stage(document.id, Stage.DOWNLOAD, workflow)
        claim = state.claim_due_stage(Stage.DOWNLOAD, workflow, document_ids=(document.id,))
        state.fail_stage(
            claim, category=ErrorCategory.AUTH, error_code="need_reauth",
            retry_at=end + timedelta(minutes=5),
        )
        state.register_source_window(
            "foreign", start - timedelta(hours=1), checkpoint, status="succeeded", checkpoint_eligible=True,
        )
        row = next(item for item in state.list_source_windows() if item["id"] == original_id)
    calls = []
    empty_scanner = SimpleNamespace(
        load_json=lambda path: {}, load_persistent_config=lambda path: {},
        scan_window=lambda page, **kwargs: {
            "download_candidates": [], "window_new_docs_count": 0, "download_candidate_count": 0,
        },
    )
    pipeline = DownloadPipeline(
        browser_session_factory=lambda *args, **kwargs: nullcontext(SimpleNamespace(page=object())),
        scanner=empty_scanner,
    )

    def download(request):
        calls.append((request.window_start, request.window_end))
        return pipeline.run(request)

    worker = PipelineWorker(config, download_runner=download, outbox_runner=lambda max_items: ())
    outcome = worker._run_download_window(row)

    assert calls == [(start, end)]
    assert outcome is not None and outcome.checkpoint_eligible is False
    assert {item["source_file_id"] for item in outcome.missing_entries} == {"owed-file"}
    with PipelineState.open(config.runtime.database) as state:
        original = next(item for item in state.list_source_windows() if item["id"] == original_id)
        assert original["checkpoint_eligible"] == 0
        assert original["status"] == "blocked_auth"
        assert state.latest_source_checkpoint("foreign") == checkpoint
        assert len(state.list_window_download_debts(original_id, workflow)) == 1


def test_download_without_debt_keeps_the_uncovered_suffix_optimization(tmp_path):
    config = _config(tmp_path)
    start = datetime(2026, 8, 10, 7, 0, tzinfo=UTC)
    middle = start + timedelta(minutes=30)
    end = start + timedelta(hours=1)
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        original_id = state.register_source_window("foreign", start, end, status="partial")
        state.register_source_window("foreign", start, middle, status="succeeded", checkpoint_eligible=True)
        row = next(item for item in state.list_source_windows() if item["id"] == original_id)
    calls = []

    def download(request):
        calls.append((request.window_start, request.window_end))
        return SimpleNamespace(status="success", source="foreign", checkpoint_eligible=True)

    worker = PipelineWorker(config, download_runner=download, outbox_runner=lambda max_items: ())
    assert worker._run_download_window(row).checkpoint_eligible is True
    assert calls == [(middle, end)]
    with PipelineState.open(config.runtime.database) as state:
        original = next(item for item in state.list_source_windows() if item["id"] == original_id)
        assert original["status"] == "succeeded"
        assert original["checkpoint_eligible"] == 1
