from contextlib import nullcontext
from datetime import datetime, timezone, timedelta
from dataclasses import replace
import json
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import pytest

from zsxq_pipeline.discovery import (
    commit_page,
    coverage,
    import_saved_discovery,
    new_scan,
    run_scan,
    scan_failure,
    schedule_discovery,
    recover_authenticated_downloads,
)
from zsxq_pipeline.download import _script_module
from zsxq_pipeline.incidents import refresh_notifications, window_complete_ready
from zsxq_pipeline.lock import held_lock, runtime_lock
from zsxq_pipeline.model import Stage, ErrorCategory
from zsxq_pipeline.state import PipelineState, LeaseLostError
from zsxq_pipeline.tasks import TaskQueue, TaskResult, diagnostic
from zsxq_pipeline import service
from test_pipeline_worker import _config

NOW = datetime(2026, 11, 17, 9, tzinfo=timezone.utc)


def config_fixture(tmp_path):
    config = _config(tmp_path)
    for source in config.sources.values():
        source.job_config_path.write_text(
            json.dumps(
                {
                    "group_url": "https://wx.zsxq.com/group/12345678901234",
                    "tag_url": "https://wx.zsxq.com/tags/test/12345678901234",
                    "download_dir": str(tmp_path / "downloads"),
                }
            )
        )
        source.keyword_path.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "standalone_keywords": ["report"],
                    "region_keywords": [],
                    "region_required_keywords": [],
                    "exclude_keywords": [],
                }
            )
        )
    return config


def topic(file_id="one", date=None):
    return {
        "topic_id": file_id,
        "create_time": (date or NOW - timedelta(hours=1)).isoformat(),
        "talk": {"files": [{"file_id": file_id, "name": "report.pdf"}]},
    }


@pytest.fixture
def state(tmp_path):
    with PipelineState.open(tmp_path / "state.sqlite3") as state:
        state.migrate()
        yield state


def test_stale_token_cannot_commit_or_heartbeat_and_live_writer_is_not_reclaimed(state):
    now = [100]
    q = TaskQueue(state, clock=lambda: now[0])
    tid = q.enqueue("scan:a", "scan", {})
    first = q.claim(tid, lease_seconds=10)
    now[0] += 11
    assert not q.recover_dead(tid, writer_is_dead=False)
    assert q.get(tid)["state"] == "running"
    assert q.recover_dead(tid, writer_is_dead=True)
    now[0] += 5
    second = q.claim(tid)
    assert first["owner_token"] != second["owner_token"]
    assert not q.heartbeat(tid, first["owner_token"])
    with pytest.raises(LeaseLostError):
        q.finish(tid, first["owner_token"], TaskResult())
    q.finish(tid, second["owner_token"], TaskResult())
    assert q.get(tid)["attempt_count"] == 2


def test_recovery_only_releases_owned_stage(state):
    doc = state.upsert_document("foreign", "f")
    for stage in (Stage.SUMMARY, Stage.PUBLISH):
        state.ensure_stage(doc.id, stage, "test")
        state.claim_due_stage(stage, "test")
    q = TaskQueue(state)
    q.release_interrupted_stages(
        {"kind": "prepare", "payload": {"document_id": doc.id}}
    )
    assert (
        state.get_stage_attempt(doc.id, Stage.SUMMARY, "test")["state"] == "retry_wait"
    )
    assert state.get_stage_attempt(doc.id, Stage.PUBLISH, "test")["state"] == "running"


def test_task_inputs_are_immutable_and_retry_is_not_an_immediate_loop(state):
    q = TaskQueue(state, clock=lambda: 100)
    tid = q.enqueue("one", "scan", {"window_id": 1}, source="foreign")
    with pytest.raises(ValueError):
        q.enqueue("one", "scan", {"window_id": 2}, source="foreign")
    task = q.claim(tid)
    q.finish(
        tid,
        task["owner_token"],
        TaskResult("retry_wait", "api_unavailable", "network", 300),
    )
    assert q.due() == []
    assert q.get(tid)["available_at_epoch"] == 400
    assert q.get(tid)["failure_count"] == 1


def test_fairness_crosses_sources_and_resources_even_with_large_old_backlog(state):
    q = TaskQueue(state, clock=lambda: 100)
    for i in range(120):
        q.enqueue(str(i), "download", {}, source="foreign")
    domestic = q.enqueue("domestic", "scan", {}, source="domestic")
    publish = q.enqueue("publish", "publish", {}, source="foreign")
    due = q.due(limit=4)
    assert {domestic, publish} <= {t["id"] for t in due}
    assert q.due({"publish"}, limit=1)[0]["id"] == publish
    first = q.claim(due[0]["id"])
    browser = next(
        t for t in due if t["resource"] == "browser" and t["id"] != first["id"]
    )
    assert q.claim(browser["id"]) is None
    assert q.claim(publish) is not None
    q.cool_down("source:foreign", 300, "api_unavailable")
    assert all(t["source"] != "foreign" or t["resource"] != "browser" for t in q.due())


def test_discovery_page_is_atomic_and_overlap_membership_never_shrinks(tmp_path):
    config = config_fixture(tmp_path)
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        window = new_scan(
            state, config, config.sources["foreign"], NOW - timedelta(days=1), NOW
        )
        with mock.patch.object(
            TaskQueue, "enqueue", side_effect=RuntimeError("crash before page commit")
        ):
            with pytest.raises(RuntimeError):
                commit_page(
                    state, window, [topic()], cursor="", next_cursor="c", complete=False
                )
        assert state.table_count("documents") == 0
        assert (
            state._connection.execute(
                "SELECT count(*) FROM discovery_pages"
            ).fetchone()[0]
            == 0
        )
        commit_page(
            state,
            window,
            [topic("one"), topic("two")],
            cursor="",
            next_cursor="c",
            complete=True,
        )
        assert state.source_window_progress(window)["total"] == 2
        assert state.source_window_progress(window)["downloaded"] == 0
        assert not window_complete_ready(state, window)
        other = new_scan(
            state, config, config.sources["foreign"], NOW - timedelta(days=2), NOW
        )
        commit_page(
            state, other, [topic("one")], cursor="", next_cursor="c", complete=True
        )
        assert state.source_window_progress(window)["total"] == 2
        assert state.source_window_progress(other)["total"] == 1
        assert (
            state._connection.execute(
                "SELECT count(*) FROM tasks WHERE kind='download'"
            ).fetchone()[0]
            == 2
        )
        assert coverage(state, "foreign") == NOW
        assert all(w["checkpoint_eligible"] == 0 for w in state.list_source_windows())


def test_coverage_cannot_jump_over_unscanned_gap(state):
    state.register_source_window(
        "s", NOW - timedelta(days=3), NOW - timedelta(days=2), checkpoint_eligible=True
    )
    state.register_source_window(
        "s", NOW - timedelta(days=1), NOW, checkpoint_eligible=True
    )
    assert coverage(state, "s") == NOW - timedelta(days=2)


def test_discovery_preserves_exact_endpoints_and_does_not_reopen_a_finished_window(
    tmp_path,
):
    config = config_fixture(tmp_path)
    end = NOW.replace(microsecond=648677)
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        initial = schedule_discovery(state, config, end)
        for window in initial:
            commit_page(state, window, [], cursor="", next_cursor="", complete=True)
        assert coverage(state, "foreign") == end
        # The daily overlap exactly matches this source's one-day bootstrap.
        assert schedule_discovery(state, config, end) == []
        assert schedule_discovery(state, config, end + timedelta(seconds=30)) == []
        assert len(state.list_source_windows()) == len(initial) == 2
        assert all(w["checkpoint_eligible"] for w in state.list_source_windows())
        assert coverage(state, "foreign", discovered=False) == end

        # Two intervals meeting only in their integer seconds still have a gap.
        state.register_source_window(
            "gap", NOW - timedelta(days=1), NOW, checkpoint_eligible=True
        )
        state.register_source_window(
            "gap",
            NOW.replace(microsecond=1),
            end + timedelta(days=1),
            checkpoint_eligible=True,
        )
        assert coverage(state, "gap") == NOW


def test_subsecond_legacy_window_cannot_certify_a_longer_interval(tmp_path):
    config = config_fixture(tmp_path)
    start = NOW - timedelta(days=1)
    end = NOW.replace(microsecond=600000)
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        old = state.register_source_window(
            "foreign", start, NOW, checkpoint_eligible=True
        )
        scanner = _script_module("scan_zsxq_download_candidates")
        plan = {
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "api_probe_status": "ok",
            "download_candidates": [],
        }
        plan["plan_hash"] = scanner.canonical_plan_hash(plan)
        root = config.runtime.root / "plans/foreign"
        root.mkdir(parents=True)
        (root / "different-window.json").write_text(json.dumps(plan))
        assert import_saved_discovery(state, config)["imported_plans"] == 0
        assert coverage(state, "foreign") == NOW
        new = new_scan(state, config, config.sources["foreign"], start, end)
        assert new != old
        commit_page(
            state, new, [topic("at-end", end)], cursor="", next_cursor="", complete=True
        )
        assert coverage(state, "foreign") == end
        assert state.source_window_progress(new)["total"] == 1


def test_schedule_keeps_one_pending_scan_and_advances_target_despite_download_debt(
    tmp_path,
):
    config = config_fixture(tmp_path)
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        first = schedule_discovery(state, config, NOW)
        assert len(first) == 2
        assert schedule_discovery(state, config, NOW + timedelta(days=1)) == []
        for window in first:
            commit_page(
                state, window, [topic()], cursor="", next_cursor="c", complete=True
            )
        second = schedule_discovery(state, config, NOW + timedelta(days=1))
        assert len(second) == 2
        assert (
            state._connection.execute(
                "SELECT count(*) FROM stage_attempts WHERE stage='download' AND state='queued'"
            ).fetchone()[0]
            == 2
        )


def test_scan_resumes_committed_cursor_and_repeated_cursor_is_terminal(
    tmp_path, monkeypatch
):
    config = config_fixture(tmp_path)
    scanner = __import__(
        "zsxq_pipeline.discovery", fromlist=["_script_module"]
    )._script_module("scan_zsxq_download_candidates")

    class Session:
        page = object()

        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr(scanner, "navigate_tag_page", lambda *a, **kw: None)
    calls = []

    def fetch(page, url):
        calls.append(url)
        if len(calls) == 2:
            raise RuntimeError(
                "network cut token=hidden https://example.test/a?secret=hidden"
            )
        return {
            "ok": True,
            "status": 200,
            "text": json.dumps({"succeeded": True, "resp_data": {"topics": [topic()]}}),
        }

    monkeypatch.setattr(scanner, "fetch_topics_page", fetch)
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        window = new_scan(
            state, config, config.sources["foreign"], NOW - timedelta(days=1), NOW
        )
        task = TaskQueue(state).get(1)
    result = run_scan(config, task, session_factory=Session)
    assert result.state == "retry_wait"
    assert "hidden" not in result.detail
    assert f"window={window} page=2 saved_pages=1" in result.detail
    with PipelineState.open(config.runtime.database) as state:
        assert (
            state._connection.execute(
                "SELECT page_count FROM discovery_scans"
            ).fetchone()[0]
            == 1
        )
    result = run_scan(config, task, session_factory=Session)
    assert result.code == "api_pagination_stalled"
    assert "end_time=" in calls[-1]
    with PipelineState.open(config.runtime.database) as state:
        assert state.source_window_progress(window)["total"] == 1


def test_historical_scan_retries_the_exact_page_after_one_1059(tmp_path, monkeypatch):
    config = config_fixture(tmp_path)
    config = replace(
        config,
        lark=replace(config.lark, notifications_enabled=True, target_chat_id="test"),
    )
    scanner = _script_module("scan_zsxq_download_candidates")
    end = NOW.replace(microsecond=648677)
    start = end - timedelta(days=1)
    page_one = [topic("too-new", end + timedelta(microseconds=1)), topic("at-end", end)]

    def body_ok(topics):
        return {"succeeded": True, "resp_data": {"topics": topics}}

    responses = iter(
        [
            body_ok(page_one),
            {"succeeded": False, "code": 1059},
            body_ok(
                [
                    topic("at-start", start),
                    topic("too-old", start - timedelta(seconds=1)),
                ]
            ),
        ]
    )
    bounds = []

    def fetch(page, url):
        bounds.append(parse_qs(urlsplit(url).query).get("end_time", [""])[0])
        return {"ok": True, "status": 200, "text": json.dumps(next(responses))}

    def session(*args, **kwargs):
        return nullcontext(SimpleNamespace(page=object()))

    monkeypatch.setattr(scanner, "navigate_tag_page", lambda *a, **kw: None)
    monkeypatch.setattr(scanner, "fetch_topics_page", fetch)
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        window = new_scan(state, config, config.sources["foreign"], start, end)
        now = int(time.time()) + 2
        q = TaskQueue(state, clock=lambda: now)
        scan = q.due({"scan"})[0]["id"]
        first = q.claim(scan)
        result = run_scan(config, first, session_factory=session)
        q.finish(scan, first["owner_token"], result)
        assert result.code == "api_error_1059" and result.retry_after == 300
        assert f"window={window} page=2 saved_pages=1 http=200" in result.detail
        assert datetime.fromisoformat(bounds[0]) == NOW + timedelta(seconds=1)
        assert bounds[1] == page_one[-1]["create_time"]
        sibling = q.enqueue("another-scan", "scan", {}, source="foreign")
        download = q.due({"download"})[0]["id"]
        assert not q.due({"scan"}) and q.claim(sibling) is None
        # A list failure does not suspend attachments already found on page one.
        claim = q.claim(download)
        assert claim
        q.finish(download, claim["owner_token"], TaskResult())
        refresh_notifications(state, config, now=now)
        assert not state.list_notifications()
        assert q.get(scan)["error_code"] == "api_error_1059"
        now += 300
        second = q.claim(scan)
        result = run_scan(config, second, session_factory=session)
        q.finish(scan, second["owner_token"], result)
        assert result.state == "succeeded" and bounds[2] == bounds[1]
        refresh_notifications(state, config, now=now)
        assert not state.list_notifications()
        assert state.source_window_progress(window)["total"] == 2
        assert {
            r[0]
            for r in state._connection.execute("SELECT source_file_id FROM documents")
        } == {"at-start", "at-end"}
        assert coverage(state, "foreign") == end


@pytest.mark.parametrize("second_failure", [True, False], ids=["repeated", "overdue"])
def test_persistent_or_overdue_1059_alerts_once_and_resets_after_recovery(
    tmp_path, second_failure
):
    config = config_fixture(tmp_path)
    config = replace(
        config,
        lark=replace(config.lark, notifications_enabled=True, target_chat_id="test"),
    )
    now = 100
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        window = new_scan(
            state, config, config.sources["foreign"], NOW - timedelta(days=1), NOW
        )
        q = TaskQueue(state, clock=lambda: now)
        scan = q.due({"scan"})[0]["id"]

        def fail(task_id, delay):
            owned = q.claim(task_id)
            q.finish(
                task_id,
                owned["owner_token"],
                TaskResult("retry_wait", "api_error_1059", "", delay),
            )

        fail(scan, 300)
        refresh_notifications(state, config, now=now)
        assert not state.list_notifications()
        now = 400
        if second_failure:
            fail(scan, 600)
        else:
            refresh_notifications(state, config, now=699)
            assert not state.list_notifications()
            now = 700
        refresh_notifications(state, config, now=now)
        refresh_notifications(state, config, now=now + 1)
        (notice,) = state.list_notifications()
        assert notice.event == "incident_open"
        assert (
            "主题列表" in notice.payload["markdown"]
            and f"#{window}" in notice.payload["markdown"]
        )
        assert "第 1 页" in notice.payload["markdown"]
        delivery = state.claim_due_notification() if second_failure else None
        now = 1100
        owned = q.claim(scan)
        q.finish(scan, owned["owner_token"], TaskResult())
        refresh_notifications(state, config, now=now)
        if delivery:
            assert [(n.event, n.status) for n in state.list_notifications()] == [
                ("incident_open", "running")
            ]
            state.complete_notification(delivery)
            refresh_notifications(state, config, now=now)
        earlier = [(n.event, n.status) for n in state.list_notifications()]
        assert earlier == (
            [("incident_open", "sent"), ("incident_resolved", "queued")]
            if second_failure
            else [("incident_open", "superseded")]
        )
        # A new one-off failure in this scope must not inherit the old notification.
        next_task = q.enqueue("new-window", "scan", {}, source="foreign")
        fail(next_task, 300)
        refresh_notifications(state, config, now=now)
        now += 300
        owned = q.claim(next_task)
        q.finish(next_task, owned["owner_token"], TaskResult())
        refresh_notifications(state, config, now=now)
        assert [(n.event, n.status) for n in state.list_notifications()] == earlier


@pytest.mark.parametrize("code", ["api_status_401", "api_status_429"])
def test_source_auth_and_throttle_still_block_downloads_and_alert_on_first_failure(
    tmp_path, code
):
    config = config_fixture(tmp_path)
    config = replace(
        config,
        lark=replace(config.lark, notifications_enabled=True, target_chat_id="test"),
    )
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        q = TaskQueue(state)
        scan = q.enqueue("scan", "scan", {}, source="foreign")
        download = q.enqueue("download", "download", {}, source="foreign")
        other = q.enqueue("other", "scan", {}, source="domestic")
        owned = q.claim(scan)
        q.finish(scan, owned["owner_token"], scan_failure(config, owned, code, code))
        assert {t["id"] for t in q.due()} == {other} and q.claim(download) is None
        refresh_notifications(state, config)
        assert [n.event for n in state.list_notifications()] == ["incident_open"]


def test_positive_scan_releases_only_same_source_download_login_failures(state):
    for source, stage, code in [
        ("foreign", Stage.DOWNLOAD, "need_reauth"),
        ("domestic", Stage.DOWNLOAD, "need_reauth"),
        ("foreign", Stage.PUBLISH, "need_reauth"),
        ("foreign", Stage.DOWNLOAD, "permission_denied"),
    ]:
        doc = state.upsert_document(source, str(stage) + code)
        state.ensure_stage(doc.id, stage, "v")
        claim = state.claim_due_stage(stage, "v", document_ids=(doc.id,))
        state.fail_stage(claim, category=ErrorCategory.AUTH, error_code=code)
    recover_authenticated_downloads(state, "foreign")
    assert (
        state._connection.execute(
            "SELECT count(*) FROM stage_attempts WHERE state='queued'"
        ).fetchone()[0]
        == 1
    )
    assert (
        state._connection.execute(
            "SELECT count(*) FROM stage_attempts WHERE state='blocked_auth'"
        ).fetchone()[0]
        == 3
    )


def test_incidents_dedupe_by_source_phase_and_only_resolve_after_success(tmp_path):
    config = config_fixture(tmp_path)
    config = replace(
        config,
        lark=replace(config.lark, notifications_enabled=True, target_chat_id="test"),
    )
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        q = TaskQueue(state, clock=lambda: 100)
        for i in range(3):
            tid = q.enqueue(f"scan:{i}", "scan", {}, source="foreign")
            task = q.claim(tid)
            q.finish(
                tid,
                task["owner_token"],
                TaskResult("retry_wait", "api_unavailable", "offline", 1),
            )
        refresh_notifications(state, config, now=100)
        refresh_notifications(state, config, now=200)
        (notice,) = state.list_notifications()
        claim = state.claim_due_notification()
        assert claim.id == notice.id
        state.complete_notification(claim)
        q.clock = lambda: 1000
        # One task starts a quiet wait while the others still report errors.
        waiting = q.claim(q.due()[0]["id"])
        q.finish(
            waiting["id"],
            waiting["owner_token"],
            TaskResult("retry_wait", "resource_busy", "", 3000),
        )
        refresh_notifications(state, config, now=1000)
        assert len(state.list_notifications()) == 1
        q.clock = lambda: 2000
        for task in q.due():
            owned = q.claim(task["id"])
            q.finish(task["id"], owned["owner_token"], TaskResult())
        refresh_notifications(state, config, now=2000)
        assert len(state.list_notifications()) == 1
        q.clock = lambda: 4000
        owned = q.claim(waiting["id"])
        q.finish(owned["id"], owned["owner_token"], TaskResult())
        refresh_notifications(state, config, now=4000)
        notices = state.list_notifications()
        assert [(n.event, n.status) for n in notices] == [
            ("incident_open", "sent"),
            ("incident_resolved", "queued"),
        ]


def test_shared_worker_gate_blocks_maintenance_but_not_another_worker(tmp_path):
    with held_lock(tmp_path / ".maintenance.lock", shared=True) as first:
        assert first is not None
        with held_lock(tmp_path / ".maintenance.lock", shared=True) as second:
            assert second is not None
        with runtime_lock(tmp_path) as acquired:
            assert not acquired


def test_inherited_writer_lock_survives_parent_handle_close(tmp_path):
    path = tmp_path / "writer.lock"
    with held_lock(path) as handle:
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            pass_fds=(handle.fileno(),),
        )
    try:
        with held_lock(path) as competing:
            assert competing is None
    finally:
        child.terminate()
        child.wait(timeout=5)
    with held_lock(path) as free:
        assert free is not None


def test_monitor_hard_deadline_kills_actual_child_and_frees_owned_stage(
    tmp_path, monkeypatch
):
    config = config_fixture(tmp_path)
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        tid = TaskQueue(state).enqueue("scan:test", "scan", {}, source="foreign")
        task = TaskQueue(state).claim(tid)
    popen = subprocess.Popen
    children = []

    def hung_child(argv, **kwargs):
        if argv[0] == "ps":
            return popen(argv, **kwargs)
        child = popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(service.subprocess, "Popen", hung_child)
    monkeypatch.setattr(service, "task_deadline", lambda *a: -4.9)
    service.monitor_task(tmp_path / "pipeline.toml", tid, task["owner_token"])
    assert children[0].returncode == -signal.SIGTERM
    with PipelineState.open(config.runtime.database) as state:
        assert TaskQueue(state).get(tid)["error_code"] == "worker_interrupted"
    with runtime_lock(config.runtime.root) as acquired:
        assert acquired


def test_diagnostic_redacts_tokens_and_signed_queries():
    value = diagnostic(
        "Authorization=secret https://example.test/file?signature=secret cookie=secret"
    )
    assert "secret" not in value
    assert "[redacted]" in value


def test_supervisor_claims_and_hands_work_off_without_holding_worker_lifetime(tmp_path):
    config = config_fixture(tmp_path)
    spawned = []

    def spawn(path, task, cfg):
        assert cfg.runtime.database == config.runtime.database
        spawned.append(task)

    result = service.supervise(
        tmp_path / "pipeline.toml", budget_seconds=1, spawn=spawn
    )
    assert result["execution_only"] is True
    assert len(spawned) == 1  # The single browser slot, across both sources.
    with runtime_lock(config.runtime.root) as acquired:
        assert acquired  # Supervisor returned; the durable claim is still visible.
    with PipelineState.open(config.runtime.database) as state:
        assert TaskQueue(state).get(spawned[0]["id"])["state"] == "running"


def test_completion_counts_missing_pdf_and_requires_finished_discovery(state):
    window = state.register_source_window("s", NOW - timedelta(days=1), NOW)
    one = state.upsert_document("s", "one", source_window_id=window)
    state.upsert_document("s", "missing", source_window_id=window)
    state.record_artifact(one.id, kind="pdf", path="/fixture.pdf", pdf_sha256="a" * 64)
    for stage in (Stage.SUMMARY, Stage.PUBLISH):
        state.ensure_stage(one.id, stage, "v")
        state.complete_stage(state.claim_due_stage(stage, "v", document_ids=(one.id,)))
    assert state.source_window_progress(window)["total"] == 2
    assert not window_complete_ready(state, window)
    state._connection.execute(
        "DELETE FROM window_download_members WHERE document_id<>?", (one.id,)
    )
    assert not window_complete_ready(state, window)
    state.register_source_window(
        "s", NOW - timedelta(days=1), NOW, checkpoint_eligible=True
    )
    assert window_complete_ready(state, window)


def test_production_entrypoints_use_supervisor(tmp_path, monkeypatch, capsys):
    from zsxq_pipeline import cli

    calls = []

    def run(path, **kwargs):
        calls.append(kwargs)
        return {"status": "success", "execution_only": True}

    monkeypatch.setattr(cli, "supervise", run)
    for command in (["tick"], ["run-stage", "--stage", "process"], ["outbox", "drain"]):
        assert (
            cli.main(
                [
                    *command,
                    "--config",
                    str(tmp_path / "unused.toml"),
                    "--budget-seconds",
                    "3",
                ]
            )
            == 0
        )
    assert calls == [
        {"budget_seconds": 3},
        {"stage": "process", "budget_seconds": 3},
        {"stage": "outbox", "budget_seconds": 3},
    ]
    assert capsys.readouterr().out.count("execution_only") == 3


def test_task_retry_requires_exact_unchanged_terminal_cohort(state):
    q = TaskQueue(state, clock=lambda: 100)
    tid = q.enqueue("scan:bad", "scan", {})
    owned = q.claim(tid)
    q.finish(
        tid, owned["owner_token"], TaskResult("needs_action", "scan_config_corrupt")
    )
    plan = q.retry_plan("scan", "scan_config_corrupt")
    assert plan["expected_count"] == 1
    changed = {**plan, "expected_count": 2}
    with pytest.raises(ValueError, match="hash or count"):
        q.apply_retry_plan(changed, expected_count=2)
    assert q.apply_retry_plan(plan, expected_count=1) == 1
    with pytest.raises(ValueError, match="changed after planning"):
        q.apply_retry_plan(plan, expected_count=1)
    assert q.get(tid)["attempt_count"] == 1


def test_contract_failure_is_not_automatically_requeued_by_unchanged_stage(tmp_path):
    config = config_fixture(tmp_path)
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        doc = state.upsert_document("foreign", "contract")
        state.record_artifact(
            doc.id, kind="pdf", path="/fixture.pdf", pdf_sha256="a" * 64
        )
        service._enqueue_due_documents(state, config)
        q = TaskQueue(state)
        task = q.claim(q.due()[0]["id"])
        q.finish(
            task["id"],
            task["owner_token"],
            TaskResult("needs_action", "processing_contract_failed"),
        )
        service._enqueue_due_documents(state, config)
        assert q.get(task["id"])["state"] == "needs_action"


def test_deadline_cleanup_includes_descendant_with_independent_session(tmp_path):
    pid_file = tmp_path / "grandchild.pid"
    script = (
        "import subprocess,sys,time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True); "
        f"Path({str(pid_file)!r}).write_text(str(child.pid)); time.sleep(30)"
    )
    child = subprocess.Popen([sys.executable, "-c", script], start_new_session=True)
    try:
        import time

        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_file.exists()
        grandchild = int(pid_file.read_text())
        service.terminate_descendants(child.pid)
        # The owning process may not have reaped its child yet; a zombie has
        # exited and cannot hold a resource or write an output.
        state = subprocess.run(
            ["ps", "-p", str(grandchild), "-o", "stat="], capture_output=True, text=True
        ).stdout.strip()
        assert not state or state.startswith("Z")
    finally:
        child.terminate()
        child.wait(timeout=5)


def test_separate_prepare_publish_workers_and_completion_notification_order(
    tmp_path, monkeypatch
):
    from test_pipeline_process import (
        FakeExtractor,
        FakeProvider,
        FakePublisher,
        FakeNotifier,
    )
    from zsxq_pipeline.process import DigestProcessor
    from zsxq_pipeline.extract import sha256_file

    config = config_fixture(tmp_path)
    config = replace(
        config,
        pipeline=replace(config.pipeline, extractor_version="extract-fixture-v1"),
        lark=replace(config.lark, notifications_enabled=True, target_chat_id="test"),
    )
    provider, publisher = FakeProvider(), FakePublisher()

    def processor(settings):
        return DigestProcessor(
            settings,
            extractor=FakeExtractor(),
            provider=provider,
            publisher=publisher,
            notifier=FakeNotifier(),
        )

    monkeypatch.setattr(service, "DigestProcessor", processor)
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"fixture PDF bytes")
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        window = state.register_source_window(
            "foreign", NOW - timedelta(days=1), NOW, checkpoint_eligible=True
        )
        doc = state.upsert_document(
            "foreign", "attachment", filename=pdf.name, source_window_id=window
        )
        state.record_artifact(doc.id, kind="pdf", path=pdf, pdf_sha256=sha256_file(pdf))
        service._enqueue_due_documents(state, config)
        q = TaskQueue(state)
        prepare = q.claim(q.due()[0]["id"])
    result = service.execute_task(config, prepare)
    assert result.state == "succeeded", result
    with PipelineState.open(config.runtime.database) as state:
        q = TaskQueue(state)
        q.finish(prepare["id"], prepare["owner_token"], result)
        service._enqueue_due_documents(state, config)
        publish = q.claim(q.due({"publish"})[0]["id"])
    provider.fail_if_called = True
    result = service.execute_task(config, publish)
    assert result.state == "succeeded", result
    assert len(provider.requests) == 1
    assert len(publisher.created) == 1
    with PipelineState.open(config.runtime.database) as state:
        TaskQueue(state).finish(publish["id"], publish["owner_token"], result)
        assert state.source_window_progress(window)["published"] == 1
        assert not window_complete_ready(state, window)
        for notice in state.list_notifications(event_prefix="document_ready"):
            state.set_notification_status(notice.idempotency_key, "sent")
        assert not window_complete_ready(
            state, window
        )  # Local projections are still owed.
        queue = TaskQueue(state)
        for task in queue.due({"projection"}):
            owned = queue.claim(task["id"])
            queue.finish(
                task["id"], owned["owner_token"], service.execute_task(config, owned)
            )
        assert window_complete_ready(state, window)
