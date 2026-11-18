"""Resumable discovery. Durable page commits do not wait for PDF downloads."""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import json
import time
from urllib.parse import quote

from ._time import from_iso
from .download import _script_module
from .model import Stage
from .scheduler import PipelineScheduler
from .state import PipelineState
from .tasks import (
    TOPICS_PAGE_INTERVAL_SECONDS,
    TaskQueue,
    TaskResult,
    diagnostic,
    encoded,
    fingerprint,
    next_failure_count,
    retry_seconds,
)


def _proved_intervals(
    state: PipelineState, source: str, *, discovered: bool = True
) -> tuple[datetime | None, list[tuple[datetime, datetime]]]:
    """Return the earliest known boundary and the union of proved windows."""
    db = state._connection
    predicate = "w.checkpoint_eligible=1"
    if discovered:
        predicate = "(w.checkpoint_eligible=1 OR s.state='complete')"
    rows = db.execute(
        f"""SELECT w.window_start_iso,w.window_end_iso,{predicate} AS proved
        FROM source_windows w LEFT JOIN discovery_scans s ON s.window_id=w.id
        WHERE w.source=?""",
        (source,),
    )
    # The earliest known window (including a failed bootstrap) fixes the lower
    # boundary. Never infer coverage by discarding an unscanned prefix.
    intervals = sorted(
        (
            from_iso(r["window_start_iso"]),
            from_iso(r["window_end_iso"]),
            bool(r["proved"]),
        )
        for r in rows
    )
    merged = []
    for start, stop, proved in intervals:
        if not proved:
            continue
        if merged and start <= merged[-1][1]:
            left, right = merged[-1]
            merged[-1] = (left, max(stop, right))
        else:
            merged.append((start, stop))
    earliest = intervals[0][0] if intervals else None
    return earliest, merged


def _continuous_end(earliest, proved):
    return proved[0][1] if proved and proved[0][0] == earliest else None


def coverage(
    state: PipelineState, source: str, *, discovered: bool = True
) -> datetime | None:
    return _continuous_end(*_proved_intervals(state, source, discovered=discovered))


def _missing_intervals(start, end, proved):
    gaps = []
    for left, right in proved:
        if left >= end or start >= end:
            break
        if start < left:
            gaps.append((start, left))
        start = max(start, right)
    if start < end:
        gaps.append((start, end))
    return gaps


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


def _discovery_window(state, source, start, end):
    # Legacy identity keys are whole seconds. Reuse a containing interval or
    # widen the new scan to a free key without changing an existing receipt.
    while True:
        row = state._connection.execute(
            """SELECT id,window_start_iso,window_end_iso FROM source_windows WHERE source=?
            AND window_start_epoch=? AND window_end_epoch=?""",
            (source, int(start.timestamp()), int(end.timestamp())),
        ).fetchone()
        if row is None:
            return state.register_source_window(source, start, end, status="scheduled")
        if (
            from_iso(row["window_start_iso"]) <= start
            and from_iso(row["window_end_iso"]) >= end
        ):
            return row["id"]
        start -= timedelta(seconds=1)


def new_scan(state: PipelineState, config, source, start, end) -> int:
    scanner = _script_module("scan_zsxq_download_candidates")
    job = scanner.load_json(source.job_config_path)
    keywords = scanner.load_persistent_config(source.keyword_path)
    snapshot = {"job": job, "keywords": keywords, "workflow": source.workflow_version}
    with state._write_transaction():
        window_id = _discovery_window(state, source.name, start, end)
        inserted = state._connection.execute(
            """INSERT INTO discovery_scans(window_id,state,config_json,config_sha256)
            VALUES(?,'pending',?,?) ON CONFLICT(window_id) DO NOTHING""",
            (window_id, encoded(snapshot), fingerprint(snapshot)),
        )
        if inserted.rowcount:
            TaskQueue(state).enqueue(
                f"scan:{window_id}",
                "scan",
                {"window_id": window_id},
                source=source.name,
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
        earliest, proved = _proved_intervals(state, source.name)
        checkpoint = _continuous_end(earliest, proved)
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
        start = earliest or end - timedelta(
            seconds=source.max_catchup_seconds or config.schedule.max_catchup_seconds
        )
        gaps = _missing_intervals(start, end, proved)
        if gaps:
            with state._write_transaction():
                gap = gaps[-1]
                if len(gaps) > 1:
                    key = "last_gap_choice:" + source.name
                    oldest = queue.metadata(key) == "newest"
                    gap = gaps[0] if oldest else gaps[-1]
                    queue.set_metadata(key, "oldest" if oldest else "newest")
                created.append(new_scan(state, config, source, *gap))
        # A bounded overlap once per day discovers recent edits without ever
        # moving the primary coverage backward. Historic edits need explicit
        # audits; a creation-time API cannot prove arbitrary late changes.
        day = now.date().isoformat()
        if (
            checkpoint
            and checkpoint >= end
            and queue.metadata("overlap:" + source.name) != day
        ):
            window = new_scan(
                state, config, source, checkpoint - timedelta(days=1), now
            )
            pending = state._connection.execute(
                "SELECT 1 FROM discovery_scans WHERE window_id=? AND state='pending'",
                (window,),
            ).fetchone()
            if pending:
                created.append(window)
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
        # A worker may die after this commit and before finishing its task.
        # The next page must not inherit failure evidence for the old cursor.
        state._connection.execute(
            """UPDATE tasks SET failure_count=0,error_code='scan_yield',error_detail=''
            WHERE identity=? AND kind='scan' AND state='running'
            AND error_code IN ('api_error_1059','api_unavailable')""",
            (f"scan:{window_id}",),
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


def run_scan(config, task, *, session_factory=None, max_pages: int = 1) -> TaskResult:
    """Honor the caller's page budget and the shared one-page dispatch limit."""
    from .browser import BrowserSession
    from .worker import PipelineWorker

    scanner = _script_module("scan_zsxq_download_candidates")
    source = config.sources[task["source"]]
    window_id = task["payload"]["window_id"]
    with PipelineState.open(config.runtime.database) as state:
        row = state._connection.execute(
            """SELECT d.*,w.window_start_iso,w.window_end_iso FROM discovery_scans d
            JOIN source_windows w ON w.id=d.window_id WHERE window_id=?""",
            (window_id,),
        ).fetchone()
    if row["state"] == "complete":
        return TaskResult()
    if max_pages < 1:
        return TaskResult(
            "retry_wait",
            "stage_deferred",
            "No listing page requested",
            TOPICS_PAGE_INTERVAL_SECONDS,
        )
    snapshot = json.loads(row["config_json"])
    if fingerprint(snapshot) != row["config_sha256"]:
        return TaskResult("needs_action", "scan_config_corrupt")
    job = snapshot["job"]
    cursor = row["cursor"]
    context = f"window={window_id} phase=tag_navigation"
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
                    f"{context} Tag navigation did not establish a ready page",
                )
            tag = scanner.extract_tag_id(job["tag_url"])
            # The source accepts an end_time cursor. Start just above the
            # inclusive window end, then preserve each source cursor verbatim.
            upper = from_iso(row["window_end_iso"]).replace(microsecond=0) + timedelta(
                seconds=1
            )
            first_bound = upper.astimezone(timezone(timedelta(hours=8))).strftime(
                "%Y-%m-%dT%H:%M:%S.000%z"
            )
            saved_pages = row["page_count"]
            context = (
                f"window={window_id} page={saved_pages + 1} saved_pages={saved_pages}"
            )
            bound = cursor or first_bound
            url = f"https://api.zsxq.com/v2/hashtags/{tag}/topics?count=20&end_time={quote(bound, safe='')}"
            payload = scanner.fetch_topics_page(session.page, url)
            error, topics = scanner.parse_topics_api_payload(payload)
            if error:
                status = payload.get("status")
                http = status if type(status) is int else "unknown"
                return scan_failure(
                    config,
                    task,
                    error,
                    f"{context} http={http}",
                    retry_after=_retry_after_seconds(payload.get("retry_after")),
                )
            next_cursor = str(topics[-1].get("create_time") or "") if topics else ""
            if topics and (not next_cursor or from_iso(next_cursor) >= from_iso(bound)):
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
                    recover_authenticated_downloads(state, source.name)
                    return TaskResult()
        return TaskResult(
            "retry_wait",
            "scan_yield",
            "Page committed; resume saved cursor",
            TOPICS_PAGE_INTERVAL_SECONDS,
        )
    except Exception as exc:
        code = str(getattr(exc, "code", "") or "api_unavailable")
        detail = diagnostic(f"{context} {exc}")
        if isinstance(exc, (ValueError, KeyError, TypeError)):
            return TaskResult("needs_action", "scan_contract_invalid", detail)
        return scan_failure(config, task, code, detail)


def _retry_after_seconds(value):
    if not isinstance(value, str) or not value.strip():
        return 0
    value = value.strip()
    now = int(time.time())
    # Keep even an oversized upstream pause representable by SQLite and the
    # timezone-aware notification formatter.
    limit = int(datetime(9999, 1, 1, tzinfo=timezone.utc).timestamp()) - now
    if value.isascii() and value.isdigit():
        digits = value.lstrip("0") or "0"
        return limit if len(digits) > len(str(limit)) else min(limit, int(digits))
    try:
        date = parsedate_to_datetime(value)
        return max(0, min(limit, int(date.timestamp()) - now)) if date.tzinfo else 0
    except (ValueError, TypeError, OverflowError, OSError):
        return 0


def scan_failure(config, task, code, detail, *, retry_after=0):
    delay = retry_seconds(
        next_failure_count(
            "scan", task.get("error_code", ""), int(task.get("failure_count", 0)), code
        )
    )
    if code == "api_error_1059":
        scope = "topics:" + task["source"]
    elif code in {"need_reauth", "api_status_401", "api_error_401"}:
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
        queue = TaskQueue(state)
        if retry_after:
            queue.cool_down("topics", retry_after, code)
        delay = max(delay, retry_after)
        queue.cool_down(scope, delay, code)
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
        (w["source"], from_iso(w["window_start_iso"]), from_iso(w["window_end_iso"])): w
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
                    from_iso(plan["window_start"]),
                    from_iso(plan["window_end"]),
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
