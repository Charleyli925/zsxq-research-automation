from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from zsxq_pipeline.config import load_pipeline_config
from zsxq_pipeline.lock import runtime_lock
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
