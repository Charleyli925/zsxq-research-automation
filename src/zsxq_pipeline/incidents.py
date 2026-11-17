"""Source/phase incidents and completion backed by immutable batch membership."""

from __future__ import annotations

import json
import time
from collections import defaultdict

from .notify import (
    enqueue_pipeline_status_notification,
    render_batch_complete_notice,
    _source_label,
)
from .tasks import TaskQueue, encoded

QUIET_CODES = {
    "",
    "scan_yield",
    "reconcile_yield",
    "resource_busy",
    "stage_deferred",
    "daily_yield",
    "daily_deferred",
}


def window_complete_ready(state, window_id, progress=None):
    progress = progress or state.source_window_progress(window_id)
    if not progress or not progress["total"] or progress["blocked"]:
        return False
    if any(
        progress[name] != progress["total"]
        for name in ("downloaded", "summarized", "published")
    ):
        return False
    row = state._connection.execute(
        """SELECT w.checkpoint_eligible,s.state FROM source_windows w
        LEFT JOIN discovery_scans s ON s.window_id=w.id WHERE w.id=?""",
        (window_id,),
    ).fetchone()
    if row is None or not (
        row["state"] == "complete"
        or row["state"] is None
        and row["checkpoint_eligible"]
    ):
        return False
    projection = state._connection.execute(
        """SELECT 1 FROM tasks t JOIN window_download_members m
        ON m.document_id=json_extract(t.payload_json,'$.document_id')
        WHERE m.window_id=? AND t.kind='projection' AND t.state<>'succeeded'
          AND json_extract(t.payload_json,'$.summary_artifact_id')=(SELECT s.output_artifact_id
            FROM stage_attempts s WHERE s.document_id=m.document_id AND s.stage='summary'
            AND s.state='succeeded' ORDER BY s.id DESC LIMIT 1) LIMIT 1""",
        (window_id,),
    ).fetchone()
    if projection:
        return False
    # Document delivery must finish before the cohort conclusion, even though
    # the document and window scopes differ. Match accepted summary identities.
    pending = state._connection.execute(
        """SELECT 1 FROM notification_outbox n JOIN publications p ON p.id=n.publication_id
        JOIN json_each(p.details_json,'$.entries') e
        WHERE n.event='document_ready' AND n.status IN ('queued','pending','retry_wait','running')
        AND json_extract(e.value,'$.summary_sha256') IN (
          SELECT a.content_sha256 FROM window_download_members m
          JOIN stage_attempts s ON s.document_id=m.document_id AND s.stage='summary' AND s.state='succeeded'
          JOIN artifacts a ON a.id=s.output_artifact_id WHERE m.window_id=?) LIMIT 1""",
        (window_id,),
    ).fetchone()
    return pending is None


def _retire_scope(state, scope, now):
    state._connection.execute(
        """UPDATE notification_outbox SET status='superseded',error_code='superseded',
        updated_at_epoch=? WHERE event IN ('incident_open','incident_reminder')
        AND json_extract(payload_json,'$.scope_key')=? AND status IN ('queued','pending','retry_wait')""",
        (now, scope),
    )


def refresh_notifications(state, config, *, now=None):
    now = int(time.time() if now is None else now)
    queue = TaskQueue(state)
    enabled = config.lark.notifications_enabled and bool(config.lark.target_chat_id)
    # Retire old queued per-window failure messages on upgrade. Sent messages
    # and in-flight delivery receipts stay intact.
    if not queue.metadata("incident_outbox_migrated"):
        state.supersede_pending_notifications(
            events=("download_blocked",), superseded_by="durable-incidents-v5"
        )
        queue.set_metadata("incident_outbox_migrated", True)
    faults = defaultdict(list)
    for row in state._connection.execute(
        "SELECT * FROM tasks WHERE state<>'succeeded'"
    ):
        if row["error_code"] in QUIET_CODES:
            continue
        faults[f"{row['source'] or 'pipeline'}:{row['kind']}"].append(dict(row))
    with state._write_transaction():
        existing = {
            row["scope"]: dict(row)
            for row in state._connection.execute("SELECT * FROM incidents")
        }
        for scope, rows in faults.items():
            prior = existing.get(scope)
            fresh = not prior or prior["state"] == "resolved"
            generation = (
                (prior["generation"] + 1 if prior else 1)
                if fresh
                else prior["generation"]
            )
            code = rows[0]["error_code"]
            notified = 0 if fresh else prior["last_notified_epoch"]
            state._connection.execute(
                """INSERT INTO incidents VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(scope) DO UPDATE
                SET generation=excluded.generation,state='open',reason_code=excluded.reason_code,
                first_seen_epoch=excluded.first_seen_epoch,last_seen_epoch=excluded.last_seen_epoch,
                affected_count=excluded.affected_count,details_json=excluded.details_json""",
                (
                    scope,
                    generation,
                    "open",
                    code,
                    now if fresh else prior["first_seen_epoch"],
                    now,
                    notified,
                    len(rows),
                    encoded(
                        {
                            "codes": sorted({r["error_code"] for r in rows}),
                            "task_ids": [r["id"] for r in rows],
                        }
                    ),
                ),
            )
            if enabled and (fresh or not notified or now - notified >= 14400):
                event = (
                    "incident_open" if fresh or not notified else "incident_reminder"
                )
                action = (
                    "需要核对后恢复"
                    if any(
                        r["state"]
                        in {"needs_action", "quarantined", "needs_reconciliation"}
                        for r in rows
                    )
                    else "已保留进度，按退避时间重试"
                )
                source, kind = scope.rsplit(":", 1)
                _retire_scope(state, scope, now)
                enqueue_pipeline_status_notification(
                    state,
                    event=event,
                    identity=f"{scope}:{generation}:{now // 14400}",
                    chat_id=config.lark.target_chat_id,
                    scope_key=scope,
                    markdown=f"## ⚠️ 知识星球研报｜处理受阻\n\n{_source_label(source)} · {kind}：{len(rows)} 项。{action}。\n\n诊断码：`{code}`",
                )
                state._connection.execute(
                    "UPDATE incidents SET last_notified_epoch=? WHERE scope=?",
                    (now, scope),
                )
        for scope, prior in existing.items():
            prior_ids = json.loads(prior["details_json"]).get("task_ids", [])
            unresolved = any(
                queue.get(task_id)["state"] != "succeeded" for task_id in prior_ids
            )
            if scope in faults or prior["state"] != "open" or unresolved:
                continue
            _retire_scope(state, scope, now)
            state._connection.execute(
                "UPDATE incidents SET state='resolved',last_seen_epoch=?,affected_count=0 WHERE scope=?",
                (now, scope),
            )
            if enabled and prior["last_notified_epoch"]:
                enqueue_pipeline_status_notification(
                    state,
                    event="incident_resolved",
                    identity=f"{scope}:{prior['generation']}",
                    chat_id=config.lark.target_chat_id,
                    scope_key=scope,
                    markdown=f"## 知识星球研报｜阶段故障已解除\n\n{scope} 的故障任务已处理完成；全流程完成情况另按批次统计。",
                )
    if not enabled:
        return
    baseline = queue.metadata("completion_notification_baseline")
    if baseline is None:
        baseline = [
            row["id"]
            for row in state._connection.execute("SELECT id FROM source_windows")
            if window_complete_ready(state, row["id"])
        ]
        queue.set_metadata("completion_notification_baseline", baseline)
    baseline = set(baseline)
    legacy_ceiling = queue.metadata("legacy_notification_window_ceiling", 0)
    for row in state._connection.execute("SELECT id FROM source_windows"):
        if row["id"] in baseline or row["id"] <= legacy_ceiling:
            continue
        scope = f"source-window:{row['id']}"
        progress = state.source_window_progress(row["id"])
        if window_complete_ready(state, row["id"], progress):
            enqueue_pipeline_status_notification(
                state,
                event="batch_complete",
                identity=scope,
                chat_id=config.lark.target_chat_id,
                scope_key=scope,
                terminal=True,
                markdown=render_batch_complete_notice(
                    source=progress["source"],
                    total=progress["total"],
                    summarized=progress["summarized"],
                    published=progress["published"],
                ),
            )
