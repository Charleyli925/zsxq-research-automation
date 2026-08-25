from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from zsxq_pipeline.config import load_pipeline_config
from zsxq_pipeline.download import DOWNLOAD_RATE_LIMIT_REASON
from zsxq_pipeline.lock import runtime_lock
from zsxq_pipeline.model import Stage
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
