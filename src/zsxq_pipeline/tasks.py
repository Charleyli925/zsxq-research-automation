"""Fenced durable work. An expired lease alone never proves a writer dead."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
import time
import uuid
from typing import Any, Mapping

from .state import PipelineState, LeaseLostError

TERMINAL = frozenset(
    {"succeeded", "needs_action", "quarantined", "needs_reconciliation"}
)
KINDS = frozenset(
    {
        "scan",
        "download",
        "prepare",
        "publish",
        "projection",
        "reconcile",
        "daily",
        "outbox",
    }
)
RESOURCE_LIMITS = {"browser": 1, "compute": 2, "publish": 1, "library": 1, "outbox": 1}
RESOURCES = {
    "scan": "browser",
    "download": "browser",
    "prepare": "compute",
    "publish": "publish",
    "projection": "library",
    "reconcile": "library",
    "daily": "compute",
    "outbox": "outbox",
}
OWNED_STAGES = {
    "download": ("download",),
    "prepare": ("text_extract", "summary"),
    "publish": ("publish",),
}


def encoded(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def diagnostic(value: object) -> str:
    text = " ".join(str(value).split())
    text = re.sub(
        r"(?i)(token|authorization|cookie|secret|password|api[_-]?key)([=: ]+)[^\s,;]+",
        r"\1\2[redacted]",
        text,
    )
    text = re.sub(r"(https?://[^?\s]+)\?[^\s]+", r"\1?[redacted]", text)
    return text[:1200]


@dataclass(frozen=True)
class TaskResult:
    state: str = "succeeded"
    code: str = ""
    detail: str = ""
    retry_after: int = 0

    def __post_init__(self):
        if self.state not in TERMINAL | {"retry_wait"}:
            raise ValueError("invalid task outcome")
        if self.state == "retry_wait" and self.retry_after < 1:
            raise ValueError("retry requires a future deadline")


class TaskQueue:
    def __init__(self, state: PipelineState, *, clock=time.time):
        self.state = state
        self.db = state._connection
        self.clock = clock

    def enqueue(
        self,
        identity: str,
        kind: str,
        payload: Mapping[str, Any],
        *,
        source: str = "",
        available_at: int = 0,
    ) -> int:
        if kind not in KINDS or not identity:
            raise ValueError("unknown task kind or empty identity")
        now = int(self.clock())
        with self.state._write_transaction():
            self.db.execute(
                """INSERT INTO tasks(identity,kind,source,resource,payload_json,available_at_epoch,
                created_at_epoch,updated_at_epoch) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(identity) DO NOTHING""",
                (
                    identity,
                    kind,
                    source,
                    RESOURCES[kind],
                    encoded(payload),
                    available_at,
                    now,
                    now,
                ),
            )
            row = self.db.execute(
                "SELECT * FROM tasks WHERE identity=?", (identity,)
            ).fetchone()
            if (
                row["kind"] != kind
                or row["source"] != source
                or row["payload_json"] != encoded(payload)
            ):
                raise ValueError("task identity reused with different inputs")
        return int(row["id"])

    def get(self, task_id: int) -> dict:
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError("task not found")
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result

    def claim(self, task_id: int, *, lease_seconds: int = 120) -> dict | None:
        now = int(self.clock())
        with self.state._write_transaction():
            row = self.get(task_id)
            if (
                row["state"] not in {"queued", "retry_wait"}
                or row["available_at_epoch"] > now
            ):
                return None
            scopes = ["source:" + row["source"]] if row["resource"] == "browser" else []
            if row["resource"] == "browser":
                scopes.append("browser")
            if row["kind"] == "download":
                scopes.append("download")
            if row["kind"] == "scan":
                scopes.append("topics:" + row["source"])
            if (
                scopes
                and self.db.execute(
                    "SELECT 1 FROM dependency_cooldowns WHERE available_at_epoch>? AND scope IN ("
                    + ",".join("?" for _ in scopes)
                    + ") LIMIT 1",
                    (now, *scopes),
                ).fetchone()
            ):
                return None
            running = self.db.execute(
                "SELECT count(*) FROM tasks WHERE resource=? AND state='running'",
                (row["resource"],),
            ).fetchone()[0]
            if running >= RESOURCE_LIMITS[row["resource"]]:
                return None
            token = uuid.uuid4().hex
            self.db.execute(
                """UPDATE tasks SET state='running',owner_token=?,lease_expires_at_epoch=?,
                heartbeat_at_epoch=?,started_at_epoch=?,updated_at_epoch=?,attempt_count=attempt_count+1
                WHERE id=?""",
                (token, now + lease_seconds, now, now, now, task_id),
            )
            return self.get(task_id)

    def heartbeat(self, task_id: int, token: str, *, lease_seconds: int = 120) -> bool:
        now = int(self.clock())
        with self.state._write_transaction():
            return bool(
                self.db.execute(
                    """UPDATE tasks SET heartbeat_at_epoch=?,lease_expires_at_epoch=?
                WHERE id=? AND state='running' AND owner_token=?""",
                    (now, now + lease_seconds, task_id, token),
                ).rowcount
            )

    def finish(self, task_id: int, token: str, result: TaskResult) -> None:
        now = int(self.clock())
        with self.state._write_transaction():
            changed = self.db.execute(
                """UPDATE tasks SET state=?,available_at_epoch=?,error_code=?,error_detail=?,
                finished_at_epoch=?,updated_at_epoch=?,owner_token=NULL,lease_expires_at_epoch=NULL
                WHERE id=? AND state='running' AND owner_token=?""",
                (
                    result.state,
                    now + result.retry_after if result.state == "retry_wait" else 0,
                    result.code,
                    diagnostic(result.detail),
                    now,
                    now,
                    task_id,
                    token,
                ),
            ).rowcount
            if not changed:
                raise LeaseLostError("task completion rejected: obsolete claim token")
            if result.state == "succeeded" or result.code in {
                "scan_yield",
                "daily_yield",
                "reconcile_yield",
            }:
                self.db.execute(
                    "UPDATE tasks SET failure_count=0 WHERE id=?", (task_id,)
                )
            elif result.state == "retry_wait" and result.code != "resource_busy":
                self.db.execute(
                    "UPDATE tasks SET failure_count=failure_count+1 WHERE id=?",
                    (task_id,),
                )

    def recover_dead(self, task_id: int, *, writer_is_dead: bool) -> bool:
        """Caller must prove the task lock free; expiry alone is insufficient."""
        row = self.get(task_id)
        if (
            not writer_is_dead
            or row["state"] != "running"
            or row["lease_expires_at_epoch"] > self.clock()
        ):
            return False
        with self.state._write_transaction():
            self.finish(
                task_id,
                row["owner_token"],
                TaskResult(
                    "retry_wait",
                    "worker_interrupted",
                    "Writer lock is free after lease expiry; resume from durable receipts.",
                    5,
                ),
            )
            self.release_interrupted_stages(row)
        return True

    def release_interrupted_stages(self, task: Mapping[str, Any]) -> None:
        """Only after task AND document writer exclusion is proved by caller."""
        document_id = task["payload"].get("document_id")
        stages = OWNED_STAGES.get(task["kind"], ())
        if not document_id or not stages:
            return
        marks = ",".join("?" for _ in stages)
        where = f"document_id=? AND state='running' AND stage IN ({marks})"
        now = int(self.clock())
        iso = (
            datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")
        )
        due = (
            datetime.fromtimestamp(now + 5, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        with self.state._write_transaction():
            self.db.execute(
                f"DELETE FROM leases WHERE stage_attempt_id IN (SELECT id FROM stage_attempts WHERE {where})",
                (document_id, *stages),
            )
            self.db.execute(
                f"""UPDATE stage_attempts SET state='retry_wait',available_at_epoch=?,available_at_iso=?,
                lease_token=NULL,lease_expires_at_epoch=NULL,lease_expires_at_iso=NULL,updated_at_iso=?,updated_at_epoch=?
                WHERE {where}""",
                (now + 5, due, iso, now, document_id, *stages),
            )

    def due(self, kinds: set[str] | None = None, *, limit: int = 100) -> list[dict]:
        now = int(self.clock())
        clause = (
            ""
            if kinds is None
            else " AND t.kind IN (" + ",".join("?" for _ in kinds) + ")"
        )
        rows = self.db.execute(
            """WITH candidates AS (SELECT t.*, ROW_NUMBER() OVER (PARTITION BY t.resource,t.source,t.kind ORDER BY COALESCE(t.started_at_epoch,0),t.created_at_epoch,t.id) AS ordinal, COALESCE((SELECT MAX(h.started_at_epoch) FROM tasks h
             WHERE h.resource=t.resource AND h.source=t.source AND h.kind=t.kind),0) served
             FROM tasks t WHERE t.state IN ('queued','retry_wait') AND t.available_at_epoch<=?
             AND NOT EXISTS (SELECT 1 FROM dependency_cooldowns c WHERE c.available_at_epoch>?
               AND (c.scope='browser' AND t.resource='browser' OR c.scope='download' AND t.kind='download'
                    OR c.scope='topics:'||t.source AND t.kind='scan'
                    OR c.scope='source:'||t.source AND t.resource='browser'))
             """
            + clause
            + """) SELECT * FROM candidates ORDER BY ordinal,served,COALESCE(started_at_epoch,0),created_at_epoch,id LIMIT ?""",
            (now, now, *(kinds or ()), limit),
        ).fetchall()
        return [self.get(row["id"]) for row in rows]

    def retry_plan(self, kind: str, code: str) -> dict:
        if kind not in KINDS or not code:
            raise ValueError("retry plan requires an exact kind and error code")
        rows = self.db.execute(
            "SELECT id FROM tasks WHERE kind=? AND error_code=? AND state IN ('needs_action','quarantined','needs_reconciliation') ORDER BY id",
            (kind, code),
        ).fetchall()
        items = [
            {"id": r["id"], "fingerprint": fingerprint(self.get(r["id"]))} for r in rows
        ]
        plan = {
            "schema_version": 1,
            "database": str(self.state.path),
            "kind": kind,
            "error_code": code,
            "candidates": items,
            "expected_count": len(items),
        }
        plan["plan_hash"] = fingerprint(plan)
        return plan

    def apply_retry_plan(self, plan: Mapping[str, Any], *, expected_count: int) -> int:
        unsigned = {k: v for k, v in plan.items() if k != "plan_hash"}
        if plan.get("plan_hash") != fingerprint(unsigned) or expected_count != plan.get(
            "expected_count"
        ):
            raise ValueError("task retry plan hash or count mismatch")
        if str(self.state.path) != plan.get("database"):
            raise ValueError("task retry plan belongs to another database")
        items = plan["candidates"]
        if len(items) != expected_count or len({item["id"] for item in items}) != len(
            items
        ):
            raise ValueError("task retry plan candidate mismatch")
        with self.state._write_transaction():
            for item in items:
                task = self.get(item["id"])
                if (
                    task["state"] not in TERMINAL - {"succeeded"}
                    or fingerprint(task) != item["fingerprint"]
                ):
                    raise ValueError(
                        "task changed after planning; inspect and create a fresh plan"
                    )
            for item in items:
                self.db.execute(
                    "UPDATE tasks SET state='queued',available_at_epoch=0,updated_at_epoch=? WHERE id=?",
                    (int(self.clock()), item["id"]),
                )
        return len(items)

    def cool_down(self, scope: str, seconds: int, reason: str) -> None:
        now = int(self.clock())
        with self.state._write_transaction():
            self.db.execute(
                """INSERT INTO dependency_cooldowns VALUES(?,?,?,?) ON CONFLICT(scope) DO UPDATE
                SET available_at_epoch=MAX(available_at_epoch,excluded.available_at_epoch),reason_code=excluded.reason_code,
                    updated_at_epoch=excluded.updated_at_epoch""",
                (scope, now + seconds, reason, now),
            )

    def metadata(self, key: str, default=None):
        row = self.db.execute(
            "SELECT value_json FROM runtime_metadata WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row else default

    def set_metadata(self, key: str, value: Any) -> None:
        with self.state._write_transaction():
            self.db.execute(
                "INSERT INTO runtime_metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json",
                (key, encoded(value)),
            )


def retry_seconds(attempt: int) -> int:
    return min(3600, 300 * 2 ** min(max(0, attempt - 1), 4))


def stage_result(
    attempt: Mapping[str, Any] | None, *, now: int | None = None
) -> TaskResult:
    if attempt is None:
        return TaskResult(
            "needs_action", "stage_missing", "Expected durable stage was not created"
        )
    state = str(attempt["state"])
    code = str(attempt.get("error_code") or "")
    detail = str(attempt.get("error_detail") or "")
    if state == "succeeded":
        return TaskResult()
    if state == "quarantined":
        return TaskResult("quarantined", code, detail)
    if state in {"blocked_auth", "blocked_release"}:
        uncertain = (
            "reconcil" in detail.lower() or "unfinished body write" in detail.lower()
        )
        return TaskResult(
            "needs_reconciliation" if uncertain else "needs_action", code, detail
        )
    return TaskResult(
        "retry_wait",
        code or "stage_deferred",
        detail,
        max(5, int(attempt.get("available_at_epoch") or 0) - int(now or time.time())),
    )
