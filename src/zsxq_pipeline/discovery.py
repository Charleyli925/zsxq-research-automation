"""Resumable discovery. Durable page commits do not wait for PDF downloads."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
import time
from urllib.parse import quote

from ._time import from_iso
from .download import _script_module
from .model import Stage
from .scheduler import PipelineScheduler
from .state import PipelineState
from .tasks import (
    TaskQueue,
    TaskResult,
    diagnostic,
    encoded,
    fingerprint,
    retry_seconds,
)


def coverage(
    state: PipelineState, source: str, *, discovered: bool = True
) -> datetime | None:
    """Merge proved intervals; a later disjoint success never hides a gap."""
    db = state._connection
    predicate = "w.checkpoint_eligible=1"
    if discovered:
        predicate = "(w.checkpoint_eligible=1 OR s.state='complete')"
    rows = db.execute(
        f"""SELECT w.window_start_epoch lo,w.window_end_epoch hi,w.status
        FROM source_windows w LEFT JOIN discovery_scans s ON s.window_id=w.id
        WHERE w.source=? AND {predicate} ORDER BY lo,hi""",
        (source,),
    ).fetchall()
    # The earliest known window (including a failed bootstrap) fixes the lower
    # boundary. Never infer coverage by discarding an unscanned prefix.
    first = db.execute(
        "SELECT MIN(window_start_epoch) FROM source_windows WHERE source=?", (source,)
    ).fetchone()[0]
    if first is None:
        return None
    end = first
    advanced = False
    for row in rows:
        if row["lo"] > end:
            break
        end = max(end, row["hi"])
        advanced = True
    return datetime.fromtimestamp(end, timezone.utc) if advanced else None


def register_candidate(state, window_id, source, candidate, job, workflow):
    """Membership, identity, pending stage and immutable download input commit together."""
    queue = TaskQueue(state)
    document = state.upsert_document(
        source,
        str(candidate["file_id"]),
        filename=candidate["filename"],
        source_window_id=window_id,
    )
    state.ensure_stage(document.id, Stage.DOWNLOAD, workflow)
    # First observed attachment identity is immutable. Changes to its name or
    # topic need explicit reconciliation, never a second identity by filename.
    row = state._connection.execute(
        "SELECT * FROM attachment_plans WHERE document_id=?", (document.id,)
    ).fetchone()
    if row is None:
        state._connection.execute(
            "INSERT INTO attachment_plans VALUES(?,?,?,?,?,?)",
            (
                document.id,
                window_id,
                encoded(candidate),
                encoded(job),
                workflow,
                int(time.time()),
            ),
        )
    elif any(
        json.loads(row["candidate_json"]).get(key) != candidate.get(key)
        for key in ("topic_url", "filename")
    ):
        raise ValueError("attachment topic identity changed")
    stage = state.get_stage_attempt(document.id, Stage.DOWNLOAD, workflow)
    if stage["state"] != "succeeded":
        queue.enqueue(
            f"download:{document.id}:{workflow}",
            "download",
            {"document_id": document.id, "workflow": workflow},
            source=source,
        )
    return document.id


def new_scan(state: PipelineState, config, source, start, end) -> int:
    scanner = _script_module("scan_zsxq_download_candidates")
    job = scanner.load_json(source.job_config_path)
    keywords = scanner.load_persistent_config(source.keyword_path)
    snapshot = {"job": job, "keywords": keywords, "workflow": source.workflow_version}
    with state._write_transaction():
        window_id = state.register_source_window(
            source.name, start, end, status="scheduled"
        )
        state._connection.execute(
            """INSERT INTO discovery_scans(window_id,state,config_json,config_sha256)
            VALUES(?,'pending',?,?) ON CONFLICT(window_id) DO NOTHING""",
            (window_id, encoded(snapshot), fingerprint(snapshot)),
        )
        TaskQueue(state).enqueue(
            f"scan:{window_id}", "scan", {"window_id": window_id}, source=source.name
        )
    return window_id


def schedule_discovery(state: PipelineState, config, now: datetime) -> list[int]:
    scheduler = PipelineScheduler(config)
    queue = TaskQueue(state)
    created = []
    for source in config.sources.values():
        if not source.schedule_times:
            continue
        target = state._connection.execute(
            "SELECT * FROM discovery_targets WHERE source=?", (source.name,)
        ).fetchone()
        checkpoint = coverage(state, source.name)
        lower = (
            from_iso(target["slot_cursor_iso"])
            if target
            else state.get_schedule_cursor(source.name)
        )
        lower = (
            lower
            or checkpoint
            or now
            - timedelta(
                seconds=source.max_catchup_seconds
                or config.schedule.max_catchup_seconds
            )
        )
        due = scheduler._due_slots(source, lower_exclusive=lower, now=now)
        if due:
            with state._write_transaction():
                state._connection.execute(
                    """INSERT INTO discovery_targets VALUES(?,?,?) ON CONFLICT(source)
                    DO UPDATE SET desired_end_iso=excluded.desired_end_iso,slot_cursor_iso=excluded.slot_cursor_iso""",
                    (source.name, now.isoformat(), due[-1].isoformat()),
                )
            target = state._connection.execute(
                "SELECT * FROM discovery_targets WHERE source=?", (source.name,)
            ).fetchone()
        if target is None:
            continue
        active = state._connection.execute(
            """SELECT 1 FROM discovery_scans d JOIN source_windows w ON w.id=d.window_id
            WHERE w.source=? AND d.state='pending' LIMIT 1""",
            (source.name,),
        ).fetchone()
        if active:
            continue
        end = from_iso(target["desired_end_iso"])
        start = checkpoint
        if start is None:
            earliest = state._connection.execute(
                "SELECT MIN(window_start_iso) FROM source_windows WHERE source=?",
                (source.name,),
            ).fetchone()[0]
            start = (
                from_iso(earliest)
                if earliest
                else end
                - timedelta(
                    seconds=source.max_catchup_seconds
                    or config.schedule.max_catchup_seconds
                )
            )
        if start < end:
            created.append(new_scan(state, config, source, start, end))
        # A bounded overlap once per day discovers recent edits without ever
        # moving the primary coverage backward. Historic edits need explicit
        # audits; a creation-time API cannot prove arbitrary late changes.
        day = now.date().isoformat()
        if (
            checkpoint
            and checkpoint >= end
            and queue.metadata("overlap:" + source.name) != day
        ):
            created.append(
                new_scan(state, config, source, checkpoint - timedelta(days=1), now)
            )
            queue.set_metadata("overlap:" + source.name, day)
    return created


def commit_page(
    state, window_id, topics, *, cursor: str, next_cursor: str, complete: bool
):
    scanner = _script_module("scan_zsxq_download_candidates")
    with state._write_transaction():
        row = state._connection.execute(
            """SELECT d.*,w.source,w.window_start_iso,w.window_end_iso
            FROM discovery_scans d JOIN source_windows w ON w.id=d.window_id WHERE window_id=?""",
            (window_id,),
        ).fetchone()
        if row is None or row["state"] != "pending" or row["cursor"] != cursor:
            raise ValueError("stale discovery page commit")
        snapshot = json.loads(row["config_json"])
        if fingerprint(snapshot) != row["config_sha256"]:
            raise ValueError("scan configuration checksum mismatch")
        filtered = scanner.filter_topics(
            topics,
            window_start=from_iso(row["window_start_iso"]),
            window_end=from_iso(row["window_end_iso"]),
            keyword_payload=snapshot["keywords"],
            archived_names=set(),
            group_url=snapshot["job"]["group_url"],
            completed_file_ids=set(),
        )
        for candidate in filtered["download_candidates"]:
            register_candidate(
                state,
                window_id,
                row["source"],
                candidate,
                snapshot["job"],
                snapshot["workflow"],
            )
        state._connection.execute(
            "INSERT INTO discovery_pages VALUES(?,?,?,?,?)",
            (window_id, row["page_count"], cursor, next_cursor, encoded(topics)),
        )
        state._connection.execute(
            """UPDATE discovery_scans SET cursor=?,page_count=page_count+1,state=?,
            completed_at_epoch=? WHERE window_id=?""",
            (
                next_cursor,
                "complete" if complete else "pending",
                int(time.time()) if complete else None,
                window_id,
            ),
        )
        if complete:
            debts = state.list_window_download_debts(window_id, snapshot["workflow"])
            state.register_source_window(
                row["source"],
                from_iso(row["window_start_iso"]),
                from_iso(row["window_end_iso"]),
                status="partial" if debts else "succeeded",
                checkpoint_eligible=not debts,
            )


def run_scan(config, task, *, session_factory=None, max_pages: int = 4) -> TaskResult:
    from .browser import BrowserSession
    from .worker import PipelineWorker

    scanner = _script_module("scan_zsxq_download_candidates")
    source = config.sources[task["source"]]
    window_id = task["payload"]["window_id"]
    with PipelineState.open(config.runtime.database) as state:
        row = state._connection.execute(
            """SELECT d.*,w.window_start_iso FROM discovery_scans d
            JOIN source_windows w ON w.id=d.window_id WHERE window_id=?""",
            (window_id,),
        ).fetchone()
    if row["state"] == "complete":
        return TaskResult()
    snapshot = json.loads(row["config_json"])
    if fingerprint(snapshot) != row["config_sha256"]:
        return TaskResult("needs_action", "scan_config_corrupt")
    job = snapshot["job"]
    cursor = row["cursor"]
    try:
        with (session_factory or BrowserSession)(
            source.cdp_endpoint,
            connect_timeout_ms=15000,
            cft_launch_options=PipelineWorker._cft_options(source),
        ) as session:
            reason = scanner.navigate_tag_page(
                session.page,
                tag_url=job["tag_url"],
                group_url=job["group_url"],
                group_name=job.get("group_name", "前沿信息收录"),
                tag_name=job.get("tag_name", ""),
                timeout_ms=15000,
                attempts=1,
            )
            if reason:
                return scan_failure(
                    config,
                    task,
                    reason,
                    "Tag navigation did not establish a ready page",
                )
            for _ in range(max_pages):
                tag = scanner.extract_tag_id(job["tag_url"])
                url = f"https://api.zsxq.com/v2/hashtags/{tag}/topics?count=20"
                if cursor:
                    url += "&end_time=" + quote(cursor, safe="")
                payload = scanner.fetch_topics_page(session.page, url)
                error, topics = scanner.parse_topics_api_payload(payload)
                if error:
                    return scan_failure(
                        config, task, error, f"Topics request returned {error}"
                    )
                next_cursor = str(topics[-1].get("create_time") or "") if topics else ""
                if topics and (
                    not next_cursor
                    or (cursor and from_iso(next_cursor) >= from_iso(cursor))
                ):
                    return TaskResult(
                        "needs_action",
                        "api_pagination_stalled",
                        "Source cursor did not move backward; saved pages retained",
                    )
                complete = not topics or from_iso(next_cursor) < from_iso(
                    row["window_start_iso"]
                )
                with PipelineState.open(config.runtime.database) as state:
                    commit_page(
                        state,
                        window_id,
                        topics,
                        cursor=cursor,
                        next_cursor=next_cursor,
                        complete=complete,
                    )
                if complete:
                    with PipelineState.open(config.runtime.database) as state:
                        recover_authenticated_downloads(state, source.name)
                    return TaskResult()
                cursor = next_cursor
        return TaskResult(
            "retry_wait", "scan_yield", "Page budget reached; resume saved cursor", 5
        )
    except Exception as exc:
        code = str(getattr(exc, "code", "") or "api_unavailable")
        if isinstance(exc, (ValueError, KeyError, TypeError)):
            return TaskResult("needs_action", "scan_contract_invalid", diagnostic(exc))
        return scan_failure(config, task, code, diagnostic(exc))


def scan_failure(config, task, code, detail):
    delay = retry_seconds(int(task.get("failure_count", 0)) + 1)
    if code in {"need_reauth", "api_status_401", "api_error_401"}:
        delay = 3600
        scope = "source:" + task["source"]
    elif code.startswith("blocked_browser_"):
        if code in {"blocked_browser_missing", "blocked_browser_configuration_invalid"}:
            return TaskResult("needs_action", code, detail)
        scope = "browser"
    elif code in {
        "api_status_403",
        "api_status_404",
        "api_invalid_payload",
        "api_invalid_topics",
    }:
        return TaskResult("needs_action", code, detail)
    else:
        scope = "source:" + task["source"]
    with PipelineState.open(config.runtime.database) as state:
        TaskQueue(state).cool_down(scope, delay, code)
    return TaskResult("retry_wait", code, detail, delay)


def import_saved_discovery(state, config) -> dict:
    """One offline migration from successful, hash-verified saved scan evidence.

    No terminal stage is requeued. Missing/corrupt evidence remains unproved
    and will be rediscovered through normal bounded scans.
    """
    queue = TaskQueue(state)
    if queue.metadata("discovery_import_v5"):
        return queue.metadata("discovery_import_v5")
    if queue.metadata("legacy_notification_window_ceiling") is None:
        queue.set_metadata(
            "legacy_notification_window_ceiling",
            state._connection.execute(
                "SELECT COALESCE(MAX(id),0) FROM source_windows"
            ).fetchone()[0],
        )
    scanner = _script_module("scan_zsxq_download_candidates")
    imported = 0
    invalid = 0
    windows = {
        (w["source"], int(w["window_start_epoch"]), int(w["window_end_epoch"])): w
        for w in state.list_source_windows()
    }
    for source in config.sources.values():
        if not source.job_config_path:
            continue
        job = scanner.load_json(source.job_config_path)
        keywords = scanner.load_persistent_config(source.keyword_path)
        root = config.runtime.root / "plans" / source.name
        for path in sorted(root.glob("*.json")):
            try:
                plan = json.loads(path.read_text())
                if plan.get("blocked_reason") or plan.get("api_probe_status") != "ok":
                    continue
                if plan.get("plan_hash") != scanner.canonical_plan_hash(plan):
                    invalid += 1
                    continue
                key = (
                    source.name,
                    int(from_iso(plan["window_start"]).timestamp()),
                    int(from_iso(plan["window_end"]).timestamp()),
                )
                window = windows.get(key)
                if window is None:
                    invalid += 1
                    continue
                snapshot = {
                    "job": job,
                    "keywords": keywords,
                    "workflow": source.workflow_version,
                    "legacy_plan_hash": plan["plan_hash"],
                }
                with state._write_transaction():
                    for candidate in plan.get("download_candidates", []):
                        register_candidate(
                            state,
                            window["id"],
                            source.name,
                            candidate,
                            job,
                            source.workflow_version,
                        )
                    # Old filename-based skips do not prove attachment identity.
                    # They must be rediscovered; never bootstrap coverage from
                    # such a partial window solely on a filename match.
                    if (
                        plan.get("skipped_duplicates")
                        and not window["checkpoint_eligible"]
                    ):
                        continue
                    state._connection.execute(
                        """INSERT INTO discovery_scans(window_id,state,config_json,config_sha256,completed_at_epoch)
                        VALUES(?,'complete',?,?,?) ON CONFLICT(window_id) DO NOTHING""",
                        (
                            window["id"],
                            encoded(snapshot),
                            fingerprint(snapshot),
                            int(time.time()),
                        ),
                    )
                    imported += 1
            except (OSError, ValueError, KeyError, TypeError):
                invalid += 1
    result = {"imported_plans": imported, "invalid_or_unmatched_plans": invalid}
    queue.set_metadata("discovery_import_v5", result)
    return result


def recover_authenticated_downloads(state, source):
    """Only a successful authenticated scan can release same-source login debt."""
    db = state._connection
    workflows = db.execute(
        """SELECT DISTINCT s.workflow_version FROM stage_attempts s JOIN documents d ON d.id=s.document_id
        WHERE d.source=? AND s.stage='download' AND s.state='blocked_auth' AND s.error_code='need_reauth'""",
        (source,),
    ).fetchall()
    for row in workflows:
        candidates = state.plan_stage_retry(
            stage="download", workflow_version=row[0], error_code="need_reauth"
        )
        candidates = [
            c
            for c in candidates
            if state.get_document(c["document_id"]).source == source
        ]
        state.apply_stage_retry_plan(
            candidates,
            plan_hash=fingerprint({"verified_login": source, "candidates": candidates}),
            expected_count=len(candidates),
        )
    with state._write_transaction():
        db.execute(
            "DELETE FROM dependency_cooldowns WHERE scope=? AND reason_code IN ('need_reauth','api_status_401','api_error_401')",
            ("source:" + source,),
        )
