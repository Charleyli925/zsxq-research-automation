"""Short scheduler claims and isolated resource-bounded workers for production."""

from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace, asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from .config import load_pipeline_config
from .discovery import import_saved_discovery, schedule_discovery, run_scan
from .download import (
    DownloadPipeline,
    DownloadRequest,
    _script_module,
    DOWNLOAD_RATE_LIMIT_REASON,
)
from .lock import held_lock
from .model import Stage
from .notification_digest import notification_drain_due
from .process import DigestProcessor, ProcessConfig, ProcessRequest
from .projections import run_projection
from .state import PipelineState
from .tasks import (
    TaskQueue,
    TaskResult,
    RESOURCE_LIMITS,
    diagnostic,
    retry_seconds,
    stage_result,
)
from .worker import PipelineWorker, _atomic_json
from ._time import from_iso

STAGE_KINDS = {
    "download": {"scan", "download"},
    "process": {"prepare", "publish", "projection"},
    "outbox": {"outbox"},
    "daily-digest": {"daily"},
    "all": None,
}


def task_deadline(config, kind):
    return {
        "scan": 120,
        "download": 180,
        "prepare": config.codex.timeout_seconds + 660,
        "publish": max(180, config.lark.timeout_seconds * 5),
        "projection": 420,
        "reconcile": 180,
        "daily": max(300, config.daily_digest.timeout_seconds + 600),
        "outbox": 180,
    }[kind]


def _task_lock(config, task_id):
    return config.runtime.root / "tasks" / f"{task_id}.lock"


def _enqueue_due_documents(state, config):
    queue = TaskQueue(state)
    for source in config.sources.values():
        rows = state.list_documents_for_processing(
            source.name,
            extractor_workflow="extract:"
            + (config.pipeline.extractor_version or "ocr-geometry-v2"),
            limit=200,
        )
        for row in rows:
            kind = "publish" if row["summary_state"] == "succeeded" else "prepare"
            identity = f"{kind}:{row['id']}:{config.pipeline.extractor_version}"
            task_id = queue.enqueue(
                identity, kind, {"document_id": row["id"]}, source=source.name
            )
            task = queue.get(task_id)
            # A terminal stage can become due only after an explicit retry or
            # a newly valid dependency. Preserve lifetime attempts and inputs.
            if task["state"] in {
                "needs_action",
                "quarantined",
                "needs_reconciliation",
                "succeeded",
            }:
                reviewed = state._connection.execute(
                    "SELECT 1 FROM stage_attempts WHERE document_id=? AND state='queued' AND error_code='manual_retry' LIMIT 1",
                    (row["id"],),
                ).fetchone()
                if reviewed:
                    with state._write_transaction():
                        state._connection.execute(
                            "UPDATE tasks SET state='queued',available_at_epoch=0,error_code='',error_detail='' WHERE id=?",
                            (task_id,),
                        )
    # Exact stage retry plans also re-enable attachment tasks without changing
    # their immutable input or re-scanning the source.
    with state._write_transaction():
        state._connection.execute("""UPDATE tasks SET state='queued',available_at_epoch=0 WHERE kind='download'
            AND state IN ('needs_action','quarantined','needs_reconciliation') AND EXISTS (
            SELECT 1 FROM stage_attempts s WHERE s.document_id=json_extract(tasks.payload_json,'$.document_id')
            AND s.stage='download' AND s.workflow_version=json_extract(tasks.payload_json,'$.workflow')
            AND s.state='queued' AND s.error_code='manual_retry')""")


def _periodic_tasks(state, config, now):
    queue = TaskQueue(state)
    stamp = int(now.timestamp())
    db = state._connection
    if config.lark.notifications_enabled and notification_drain_due(
        state, config.lark.notification_digest_times, config.schedule.timezone, now
    ):
        queue.enqueue(f"outbox:{stamp // 60}", "outbox", {})
    if config.pipeline.research_library_database:
        # One audit job per day. Its own durable cooldown governs failures;
        # future days cannot pile up duplicate audits behind an open failure.
        if not db.execute(
            "SELECT 1 FROM tasks WHERE kind='reconcile' AND state IN ('queued','running','retry_wait') LIMIT 1"
        ).fetchone():
            queue.enqueue("reconcile:" + now.date().isoformat(), "reconcile", {})
    if config.daily_digest.enabled:
        if not db.execute(
            "SELECT 1 FROM tasks WHERE kind='daily' AND state IN ('queued','running','retry_wait') LIMIT 1"
        ).fetchone():
            queue.enqueue(f"daily:{stamp // 300}", "daily", {})


def _spawn(config_path, task, config):
    command = [
        sys.executable,
        str(Path(__file__).resolve().parents[2] / "scripts/run_zsxq_pipeline.py"),
        "task-worker",
        "--config",
        str(config_path),
        "--task-id",
        str(task["id"]),
        "--token",
        task["owner_token"],
    ]
    logs = config.runtime.root / "logs/tasks"
    logs.mkdir(parents=True, exist_ok=True)
    with (logs / f"{task['id']}.log").open("ab") as log:
        return subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            close_fds=True,
        )


def supervise(
    config_path,
    *,
    stage="all",
    budget_seconds=None,
    spawn=None,
    monotonic=time.monotonic,
    sleep=time.sleep,
):
    config = load_pipeline_config(config_path)
    budget = int(
        budget_seconds
        if budget_seconds is not None
        else config.schedule.tick_budget_seconds
    )
    if budget < 1:
        raise ValueError("budget must be positive")
    kinds = STAGE_KINDS[stage]
    spawned = []
    quotas = {
        "scan": max(1, sum(bool(s.schedule_times) for s in config.sources.values())),
        "download": config.schedule.download_quota,
        "prepare": config.schedule.process_quota,
        "publish": config.schedule.process_quota,
        "projection": 2 * config.schedule.process_quota,
        "reconcile": 1,
        "daily": 1,
        "outbox": 1,
    }
    dispatched = {kind: 0 for kind in quotas}
    scheduled = []
    errors = []
    deadline = monotonic() + budget
    children = []
    notified_at = 0
    with ExitStack() as stack:
        if (
            stack.enter_context(
                held_lock(config.runtime.root / ".maintenance.lock", shared=True)
            )
            is None
        ):
            return {"status": "busy", "scheduled": [], "notifications": []}
        if (
            stack.enter_context(held_lock(config.runtime.root / ".pipeline.lock"))
            is None
        ):
            return {"status": "busy", "scheduled": [], "notifications": []}
        with PipelineState.open(config.runtime.database) as state:
            state.migrate()
            import_saved_discovery(state, config)
            if kinds is None or "scan" in kinds:
                scheduled = schedule_discovery(
                    state, config, datetime.now(timezone.utc)
                )
        while monotonic() < deadline:
            with PipelineState.open(config.runtime.database) as state:
                queue = TaskQueue(state)
                active = state._connection.execute(
                    "SELECT id FROM tasks WHERE state='running' AND lease_expires_at_epoch<=?",
                    (int(time.time()),),
                ).fetchall()
                for row in active:
                    with ExitStack() as recovery:
                        free = recovery.enter_context(
                            held_lock(_task_lock(config, row["id"]))
                        )
                        document_id = queue.get(row["id"])["payload"].get("document_id")
                        document_free = (
                            recovery.enter_context(
                                held_lock(
                                    config.runtime.root
                                    / "tasks"
                                    / f"document-{document_id}.lock"
                                )
                            )
                            if document_id
                            else True
                        )
                        queue.recover_dead(
                            row["id"],
                            writer_is_dead=free is not None
                            and document_free is not None,
                        )
                _enqueue_due_documents(state, config)
                _periodic_tasks(state, config, datetime.now(timezone.utc))
                from .incidents import refresh_notifications

                if monotonic() - notified_at >= 10:
                    refresh_notifications(state, config)
                    notified_at = monotonic()
                due = queue.due(kinds)
                for candidate in due:
                    if monotonic() >= deadline:
                        break
                    if dispatched[candidate["kind"]] >= quotas[candidate["kind"]]:
                        continue
                    claimed = queue.claim(candidate["id"])
                    if claimed is None:
                        continue
                    dispatched[candidate["kind"]] += 1
                    try:
                        child = (spawn or _spawn)(config_path, claimed, config)
                        if child is not None:
                            children.append(child)
                        spawned.append(claimed["id"])
                    except Exception as exc:
                        queue.finish(
                            claimed["id"],
                            claimed["owner_token"],
                            TaskResult(
                                "retry_wait", "worker_spawn_failed", diagnostic(exc), 60
                            ),
                        )
                        errors.append("worker_spawn_failed")
                running = state._connection.execute(
                    "SELECT COUNT(*) FROM tasks WHERE state='running'"
                ).fetchone()[0]
            children = [child for child in children if child.poll() is None]
            if not running or spawn is not None:
                break
            sleep(min(1, max(0, deadline - monotonic())))
    return {
        "status": "partial" if errors else "success",
        "scheduled": scheduled,
        "spawned": spawned,
        "notifications": [],
        "failures": errors,
        "execution_only": True,
    }


def _document_input(state, document_id):
    row = state._connection.execute(
        """SELECT d.*,a.pdf_sha256,a.canonical_path FROM documents d
        JOIN artifacts a ON a.id=d.artifact_id AND a.kind='pdf' WHERE d.id=?""",
        (document_id,),
    ).fetchone()
    if row is None:
        raise ValueError("document has no verified PDF artifact")
    return {
        "path": row["canonical_path"],
        "pdf_sha256": row["pdf_sha256"],
        "filename": row["filename"],
        "source": row["source"],
        "source_file_id": row["source_file_id"],
        "source_window_id": row["source_window_id"],
        "batch_id": "unified-tick",
    }


def _run_document(config, task):
    document_id = task["payload"]["document_id"]
    with PipelineState.open(config.runtime.database) as state:
        item = _document_input(state, document_id)
    work = config.runtime.root / "work/tasks" / str(task["id"])
    work.mkdir(parents=True, exist_ok=True)
    batch = work / "batch.json"
    _atomic_json(batch, {"files": [item]})
    base = ProcessConfig.from_pipeline_config(config)
    overrides = {
        name: work / Path(getattr(base, name)).name
        for name in (
            "batch_path",
            "result_path",
            "result_markdown_path",
            "run_status_path",
            "usage_path",
            "quarantine_path",
            "notification_audit_path",
        )
    }
    overrides.update(
        batch_path=batch, source=task["source"], work_root=work, summary_max_workers=1
    )
    processor = DigestProcessor(replace(base, **overrides))
    result = processor.run(
        ProcessRequest(
            batch_file=batch,
            summary_only=task["kind"] == "prepare",
            publish_only=task["kind"] == "publish",
            defer_projections=True,
            defer_notification_drain=True,
        ),
        acquire_lock=False,
    )
    with PipelineState.open(config.runtime.database) as state:
        stage = "summary" if task["kind"] == "prepare" else "publish"
        row = state._connection.execute(
            "SELECT * FROM stage_attempts WHERE document_id=? AND stage=? ORDER BY id DESC LIMIT 1",
            (document_id, stage),
        ).fetchone()
        if row is None and task["kind"] == "prepare":
            row = state._connection.execute(
                "SELECT * FROM stage_attempts WHERE document_id=? AND stage='text_extract' ORDER BY id DESC LIMIT 1",
                (document_id,),
            ).fetchone()
            if row is not None and row["state"] == "succeeded":
                return TaskResult(
                    "retry_wait",
                    "summary_not_committed",
                    diagnostic("; ".join(result.failures)),
                    300,
                )
        if row is None:
            return TaskResult(
                "needs_action",
                "processing_contract_failed",
                diagnostic("; ".join(result.failures)),
            )
        return stage_result(dict(row))


def _run_download(config, task):
    with PipelineState.open(config.runtime.database) as state:
        document_id = task["payload"]["document_id"]
        workflow = task["payload"]["workflow"]
        prior = state.get_stage_attempt(document_id, Stage.DOWNLOAD, workflow)
        check = stage_result(prior)
        if check.state in {
            "succeeded",
            "needs_action",
            "quarantined",
            "needs_reconciliation",
        }:
            return check
        if prior and int(prior.get("available_at_epoch") or 0) > time.time():
            return check
        row = state._connection.execute(
            """SELECT p.*,w.source,w.window_start_iso,w.window_end_iso FROM attachment_plans p
            JOIN source_windows w ON w.id=p.window_id WHERE p.document_id=?""",
            (document_id,),
        ).fetchone()
        if row is None:
            return TaskResult("needs_action", "attachment_plan_missing")
    source = config.sources[task["source"]]
    work = config.runtime.root / "work/tasks" / str(task["id"])
    work.mkdir(parents=True, exist_ok=True)
    job = work / "job.json"
    _atomic_json(job, json.loads(row["job_config_json"]))
    scanner = _script_module("scan_zsxq_download_candidates")
    plan = {
        "schema_version": 3,
        "window_start": row["window_start_iso"],
        "window_end": row["window_end_iso"],
        "download_candidates": [json.loads(row["candidate_json"])],
        "download_candidate_count": 1,
        "api_probe_status": "ok",
        "scan_mode": "durable_attachment",
        "blocked_reason": None,
    }
    plan["plan_hash"] = scanner.canonical_plan_hash(plan)
    request = DownloadRequest(
        source=source.name,
        runtime_root=config.runtime.root,
        database=config.runtime.database,
        job_config_path=job,
        keyword_path=source.keyword_path,
        legacy_state_path=source.state_path,
        cdp_endpoint=source.cdp_endpoint,
        window_start=from_iso(row["window_start_iso"]),
        window_end=from_iso(row["window_end_iso"]),
        cft_launch_options=PipelineWorker._cft_options(source),
        workflow_version=workflow,
        extractor_version=config.pipeline.extractor_version,
        prepared_plan=plan,
        defer_library_projection=True,
        navigation_attempts=1,
    )
    outcome = DownloadPipeline().run(request)
    with PipelineState.open(config.runtime.database) as state:
        result = stage_result(
            state.get_stage_attempt(document_id, Stage.DOWNLOAD, workflow)
        )
        if result.code == DOWNLOAD_RATE_LIMIT_REASON:
            TaskQueue(state).cool_down("download", result.retry_after, result.code)
        elif result.code == "need_reauth":
            TaskQueue(state).cool_down("source:" + task["source"], 3600, result.code)
        if outcome.reason_code.startswith("blocked_browser_"):
            TaskQueue(state).cool_down("browser", 300, outcome.reason_code)
            return TaskResult(
                "retry_wait", outcome.reason_code, outcome.error_detail, 300
            )
        return result


def _run_reconcile(config, task):
    library = config.pipeline.research_library_root
    if library is None:
        return TaskResult()
    script = Path(__file__).resolve().parents[2] / "scripts/kb_reconcile_search.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--db-path",
            str(config.pipeline.research_library_database),
            "--vault-root",
            str(config.pipeline.obsidian_vault_root or ""),
            "--config-root",
            str(library / "config"),
            "--max-updates",
            "100",
        ],
        capture_output=True,
        text=True,
        timeout=150,
        check=False,
    )
    try:
        result = json.loads(completed.stdout)
    except ValueError:
        return TaskResult(
            "retry_wait", "reconcile_failed", diagnostic(completed.stderr), 3600
        )
    with PipelineState.open(config.runtime.database) as state:
        TaskQueue(state).set_metadata("last_reconcile", result)
    if result.get("catalog_errors") or result.get("errors"):
        return TaskResult(
            "retry_wait",
            "reconcile_sources_changed",
            "Source evidence requires verification; accepted bytes retained",
            21600,
        )
    if not result.get("complete"):
        return TaskResult(
            "retry_wait", "reconcile_yield", "Bounded audit batch complete", 300
        )
    return TaskResult()


def execute_task(config, task):
    kind = task["kind"]
    if kind == "scan":
        return run_scan(config, task)
    if kind == "download":
        return _run_download(config, task)
    if kind in {"prepare", "publish"}:
        return _run_document(config, task)
    if kind == "projection":
        return run_projection(config, task)
    if kind == "reconcile":
        return _run_reconcile(config, task)
    if kind == "outbox":
        PipelineWorker(config)._drain_outbox(config.schedule.outbox_quota)
        return TaskResult()
    if kind == "daily":
        from .daily_digest import DailyDigest

        outcome = DailyDigest(config).step(datetime.now(timezone.utc))
        if outcome.get("status") == "needs_reconciliation":
            return TaskResult("needs_reconciliation", "daily_publication_uncertain")
        if outcome.get("status") == "retry_wait":
            return TaskResult(
                "retry_wait",
                "daily_deferred",
                "Daily step retains its saved retry deadline",
                900,
            )
        if outcome.get("status") not in {"complete", "idle", "empty", "disabled"}:
            return TaskResult("retry_wait", "daily_yield", "Daily checkpoint saved", 5)
        return TaskResult()
    raise ValueError("unsupported task kind")


def terminate_descendants(parent_pid):
    """Stop descendants even when a CLI opened a separate process session.

    Only the current child ancestry is eligible; pre-existing shared browsers
    and unrelated user processes are never selected by command name.
    """
    completed = subprocess.run(
        ["ps", "-axo", "pid=,ppid="],
        capture_output=True,
        text=True,
        timeout=2,
        check=True,
    )
    parents = {}
    for line in completed.stdout.splitlines():
        pid, parent = map(int, line.split())
        parents[pid] = parent
    selected = []
    frontier = {parent_pid}
    while frontier:
        children = {
            pid
            for pid, parent in parents.items()
            if parent in frontier and pid != os.getpid()
        }
        selected.extend(children)
        frontier = children
    for pid in reversed(selected):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def execute_child(config_path, task_id, token, result_path):
    config = load_pipeline_config(config_path)
    with PipelineState.open(config.runtime.database) as state:
        task = TaskQueue(state).get(task_id)
    if task["state"] != "running" or task["owner_token"] != token:
        raise ValueError("obsolete task claim")

    # The child has its own session and inherited writer locks. Even if its
    # monitor dies, the alarm terminates this group at the absolute deadline.
    def expired(*_args):
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        try:
            terminate_descendants(os.getpid())
        finally:
            os.killpg(os.getpgrp(), signal.SIGTERM)

    signal.signal(signal.SIGALRM, expired)
    signal.signal(signal.SIGTERM, expired)
    signal.setitimer(signal.ITIMER_REAL, task_deadline(config, task["kind"]))
    try:
        result = execute_task(config, task)
    except (ValueError, KeyError, TypeError) as exc:
        result = TaskResult("needs_action", "task_contract_invalid", diagnostic(exc))
    except Exception as exc:
        result = TaskResult(
            "retry_wait",
            "task_execution_failed",
            diagnostic(exc),
            retry_seconds(task.get("failure_count", 0) + 1),
        )
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        terminate_descendants(os.getpid())
    _atomic_json(Path(result_path), asdict(result))


def monitor_task(config_path, task_id, token):
    config = load_pipeline_config(config_path)
    with PipelineState.open(config.runtime.database) as state:
        task = TaskQueue(state).get(task_id)
    if task["state"] != "running" or task["owner_token"] != token:
        return
    with ExitStack() as stack:
        handles = [
            stack.enter_context(
                held_lock(config.runtime.root / ".maintenance.lock", shared=True)
            ),
            stack.enter_context(held_lock(_task_lock(config, task_id))),
        ]
        slot = None
        for i in range(RESOURCE_LIMITS[task["resource"]]):
            candidate = stack.enter_context(
                held_lock(
                    config.runtime.root
                    / "tasks"
                    / f"resource-{task['resource']}-{i}.lock"
                )
            )
            if candidate is not None:
                slot = candidate
                break
        handles.append(slot)
        document_id = task["payload"].get("document_id")
        if document_id:
            handles.append(
                stack.enter_context(
                    held_lock(
                        config.runtime.root / "tasks" / f"document-{document_id}.lock"
                    )
                )
            )
            if task["kind"] == "prepare":
                with PipelineState.open(config.runtime.database) as state:
                    item = state._connection.execute(
                        "SELECT a.pdf_sha256 FROM documents d JOIN artifacts a ON a.id=d.artifact_id WHERE d.id=?",
                        (document_id,),
                    ).fetchone()
                if item and item["pdf_sha256"]:
                    handles.append(
                        stack.enter_context(
                            held_lock(
                                config.runtime.root
                                / "tasks"
                                / f"content-{item['pdf_sha256']}.lock"
                            )
                        )
                    )
        if any(handle is None for handle in handles):
            with PipelineState.open(config.runtime.database) as state:
                TaskQueue(state).finish(
                    task_id,
                    token,
                    TaskResult(
                        "retry_wait", "resource_busy", "Writer resource is held", 5
                    ),
                )
            return
        output = config.runtime.root / "tasks" / f"{task_id}-{token}.json"
        command = [
            sys.executable,
            str(Path(__file__).resolve().parents[2] / "scripts/run_zsxq_pipeline.py"),
            "task-execute",
            "--config",
            str(config_path),
            "--task-id",
            str(task_id),
            "--token",
            token,
            "--result",
            str(output),
        ]
        child = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            pass_fds=tuple(handle.fileno() for handle in handles),
        )
        deadline = time.monotonic() + task_deadline(config, task["kind"]) + 5
        try:
            while child.poll() is None and time.monotonic() < deadline:
                with PipelineState.open(config.runtime.database) as state:
                    if not TaskQueue(state).heartbeat(task_id, token):
                        break
                time.sleep(2)
        finally:
            if child.poll() is None:
                try:
                    terminate_descendants(child.pid)
                finally:
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=3)
        if output.is_file():
            result = TaskResult(**json.loads(output.read_text()))
        else:
            result = TaskResult(
                "retry_wait",
                "worker_interrupted",
                "Worker ended without a durable completion; reconcile saved artifacts",
                60,
            )
        with PipelineState.open(config.runtime.database) as state:
            queue = TaskQueue(state)
            if queue.get(task_id)["owner_token"] == token:
                queue.finish(task_id, token, result)
                if result.code == "worker_interrupted" and document_id:
                    # Lease reclamation is safe after the actual child group
                    # has exited; publication intent guards remain untouched.
                    queue.release_interrupted_stages(task)
