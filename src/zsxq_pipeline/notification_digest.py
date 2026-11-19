"""Schedule group updates from the existing notification outbox."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
import json
import re
from zoneinfo import ZoneInfo

from ._time import to_iso_epoch


DIGEST_EVENT = "notification_digest"
STATUS_LABELS = {
    "download_complete": "下载完成",
    "summary_started": "开始总结",
    "summary_progress": "总结进度",
    "batch_complete": "批次完成",
    "run_complete": "运行完成",
}
# Keep the first alert requiring intervention on the direct delivery path.
# A row that may already have reached Lark must retain its original remote key.
ROUTINE = """n.status IN ('queued','pending') AND n.attempt_count=0 AND (
    n.event IN ('document_ready','download_complete','summary_started','summary_progress',
                'batch_complete','run_complete','incident_reminder','incident_resolved')
    OR (n.event='incident_open' AND json_extract(n.payload_json,'$.requires_action')=0))"""


@dataclass
class _DocumentUpdates:
    count: int = 0
    known_count: bool = True
    sources: set[str] = field(default_factory=set)
    title: str = ""


def _slots(now, times, timezone):
    zone = ZoneInfo(timezone)
    day = now.astimezone(zone).date()
    instants = sorted(
        {
            datetime.combine(
                day + timedelta(days=offset), time.fromisoformat(value), zone
            ).timestamp()
            for offset in (-1, 0, 1)
            for value in times
        }
    )
    stamp = now.timestamp()
    return (
        datetime.fromtimestamp(max(s for s in instants if s <= stamp), zone),
        datetime.fromtimestamp(min(s for s in instants if s > stamp), zone),
    )


def notification_drain_due(state, times, timezone, now):
    """Also wake for an expired lease or for rows parked under an old schedule."""
    epoch = int(now.timestamp())
    next_epoch = int(_slots(now, times, timezone)[1].timestamp()) if times else 0
    return (
        state._connection.execute(
            """SELECT 1 FROM notification_outbox WHERE
           (status IN ('queued','pending','retry_wait') AND COALESCE(available_at_epoch,0)<=?)
           OR (status='running' AND COALESCE(lease_expires_at_epoch,0)<=?)
           OR (status IN ('queued','pending') AND error_code='digest_scheduled'
               AND available_at_epoch<>?) LIMIT 1""",
            (epoch, epoch, next_epoch),
        ).fetchone()
        is not None
    )


def _body(rows, cutoff):
    from .notify import _source_label

    docs = defaultdict(_DocumentUpdates)
    statuses = Counter()
    incidents = {}
    for row in rows:
        event = row["event"]
        payload = json.loads(row["payload_json"])
        if event == "document_ready":
            publication = json.loads(row["details_json"] or "{}")
            url = row["remote_reference"]
            updates = docs[url]
            entries = publication.get("entries") or []
            if not updates.title and publication.get("title"):
                title = " ".join(publication["title"].split())
                # The publication title's count describes one append, whereas
                # the digest counts all appends to this URL within the slot.
                updates.title = re.sub(r"(?<=）) \d+ 篇(?= |$)", "", title)
            count = publication.get("entry_count", len(entries))
            if type(count) is int and count > 0:
                updates.count += count
            else:
                updates.known_count = False
            updates.sources.update(
                _source_label(entry["source"])
                for entry in entries
                if entry.get("source")
                and entry["source"] not in {"unified-tick", "zsxq_digest"}
            )
        elif event in STATUS_LABELS:
            statuses[STATUS_LABELS[event]] += 1
        else:
            # Only the last known state of a particular source/stage belongs in the digest.
            scope = payload["scope_key"]
            status = "已恢复"
            if event != "incident_resolved":
                status = {True: "需人工处理", False: "自动重试中"}.get(
                    payload.get("requires_action"), "仍受阻"
                )
                if payload.get("reason_code"):
                    status += f"（{payload['reason_code']}）"
            source, _, phase = scope.rpartition(":")
            phases = {
                "scan": "列表扫描",
                "download": "附件下载",
                "prepare": "摘要准备",
                "publish": "发布",
                "reconcile": "数据核对",
                "daily": "日报",
            }
            label = "研报系统" if source == "pipeline" else _source_label(source)
            incidents[scope] = f"{label} · {phases.get(phase, phase)} {status}"

    parts = [f"## 知识星球研报汇总｜{cutoff:%m-%d %H:%M}"]
    for index, (url, updates) in enumerate(docs.items(), 1):
        label = (
            updates.title or "、".join(sorted(updates.sources)) or f"研报文档 {index}"
        )
        label = re.sub(r"([\\\[\]])", r"\\\1", label)
        increment = f"新增 {updates.count} 篇" if updates.known_count else "文档有更新"
        parts.append(f"[{label}]({url}) · {increment}")
    if statuses:
        parts.append("、".join(f"{name} {n} 次" for name, n in statuses.items()))
    parts.extend(incidents.values())
    return "\n\n".join(parts)


def prepare_notification_digests(state, times, timezone, now):
    """Park new rows or freeze one digest per chat/slot before claiming a send.

    The caller holds the same SQLite write transaction as the subsequent claim.
    A retry after a crash always finds the original digest body and remote key.
    """
    from .notify import notification_idempotency_key

    db = state._connection
    if not times:
        db.execute("""UPDATE notification_outbox SET available_at_epoch=NULL,available_at_iso=NULL,
            error_code='' WHERE status IN ('queued','pending') AND error_code='digest_scheduled'""")
        return
    cutoff, following = _slots(now, times, timezone)
    next_iso, next_epoch = to_iso_epoch(following)
    # Re-evaluate parked rows when a local schedule is edited.
    rows = db.execute(
        f"""SELECT n.*,p.details_json,p.remote_reference FROM notification_outbox n
        LEFT JOIN publications p ON p.id=n.publication_id WHERE {ROUTINE}
        AND (COALESCE(n.available_at_epoch,0)<=? OR
             (n.error_code='digest_scheduled' AND n.available_at_epoch<>?))
        ORDER BY n.created_at_epoch,n.id""",
        (int(now.timestamp()), next_epoch),
    ).fetchall()
    groups = defaultdict(list)
    park = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        if not payload.get("chat_id") or (
            row["event"] == "document_ready" and not row["remote_reference"]
        ):
            # An incomplete legacy row stays on its existing delivery/error path.
            continue
        if row["created_at_epoch"] <= cutoff.timestamp():
            groups[payload["chat_id"]].append(row)
        else:
            park.append(row["id"])

    for chat, members in groups.items():
        key = notification_idempotency_key(
            DIGEST_EVENT, f"{chat}:{int(cutoff.timestamp())}"
        )
        previous = db.execute(
            """SELECT 1 FROM notification_outbox WHERE idempotency_key=?
            OR (event=? AND json_extract(payload_json,'$.chat_id')=?
                AND status IN ('queued','pending','running','retry_wait')) LIMIT 1""",
            (key, DIGEST_EVENT, chat),
        ).fetchone()
        if previous:
            park.extend(row["id"] for row in members)
            continue
        # Keep large imports within one message per slot. Carry other links forward.
        urls = set()
        included = []
        for row in members:
            url = row["remote_reference"] if row["event"] == "document_ready" else None
            if url and url not in urls and len(urls) >= 20:
                park.append(row["id"])
                continue
            if url:
                urls.add(url)
            included.append(row)
        parent = state.enqueue_notification(
            key,
            event=DIGEST_EVENT,
            now=now,
            payload={
                "chat_id": chat,
                "scope_key": key,
                "markdown": _body(included, cutoff),
                "terminal": False,
                "cutoff": cutoff.isoformat(),
            },
        )
        db.executemany(
            """UPDATE notification_outbox SET status='digest_pending',error_code='',
            available_at_iso=NULL,available_at_epoch=NULL,
            payload_json=json_set(payload_json,'$.digest_id',?) WHERE id=?""",
            ((parent.id, row["id"]) for row in included),
        )
    db.executemany(
        """UPDATE notification_outbox SET error_code='digest_scheduled',
        available_at_iso=?,available_at_epoch=? WHERE id=?""",
        ((next_iso, next_epoch, row_id) for row_id in park),
    )
