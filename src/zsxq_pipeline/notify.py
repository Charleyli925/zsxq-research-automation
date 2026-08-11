"""Durable, ordered delivery of direct Lark notifications.

The SQLite outbox is the authority.  This module intentionally does not keep
another mutable queue or infer business completion from a legacy JSONL file:
publication can be successful while the bot delivery is waiting to retry.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from .model import NotificationClaim, NotificationRecord, PublicationRecord
from .state import PipelineState


DOCUMENT_EVENT = "document_ready"
TERMINAL_EVENTS = frozenset({"run_complete", "run_partial", "run_failed"})
DOWNLOAD_COMPLETE_EVENT = "download_complete"
SUMMARY_STARTED_EVENT = "summary_started"
SUMMARY_PROGRESS_EVENT = "summary_progress"
BATCH_COMPLETE_EVENT = "batch_complete"
PIPELINE_STATUS_EVENTS = frozenset(
    {
        DOWNLOAD_COMPLETE_EVENT,
        SUMMARY_STARTED_EVENT,
        SUMMARY_PROGRESS_EVENT,
        BATCH_COMPLETE_EVENT,
    }
)
_ACTIVE_NOTIFICATION_STATES = frozenset({"queued", "pending", "retry_wait", "running"})


class NotificationError(RuntimeError):
    """A payload cannot safely be delivered through the notification outbox."""


class LarkNotificationClient(Protocol):
    """The small bot-only surface consumed by the durable drainer."""

    def notify_once(self, chat_id: str, markdown: str, *, idempotency_key: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class NotificationDelivery:
    """One local outcome; remote message IDs deliberately stay diagnostic-only."""

    idempotency_key: str
    event: str
    status: str
    deferred: bool = False
    error: str = ""


def notification_idempotency_key(event: str, identity: str) -> str:
    """Return a deterministic Lark-safe idempotency key no longer than 50 bytes.

    The stable digest avoids exposing an arbitrary source filename or document
    URL as a remote idempotency key while making distinct publication/run
    identities non-colliding for practical purposes.
    """

    normalized_event = "".join(character for character in str(event).lower() if character.isalnum() or character in "_-" )
    normalized_event = normalized_event[:12] or "event"
    digest = hashlib.sha256(f"{event}\0{identity}".encode("utf-8")).hexdigest()[:32]
    return f"zsxq-{normalized_event}-{digest}"


def _payload(*, chat_id: str, markdown: str, scope_key: str, terminal: bool) -> dict[str, str | bool]:
    chat = str(chat_id).strip()
    body = str(markdown).strip()
    scope = str(scope_key).strip()
    if not chat:
        raise NotificationError("notification chat_id is required")
    if not body:
        raise NotificationError("notification Markdown is required")
    if not scope:
        raise NotificationError("notification scope_key is required")
    return {"chat_id": chat, "markdown": body, "scope_key": scope, "terminal": bool(terminal)}


def enqueue_document_notification(
    state: PipelineState,
    publication: PublicationRecord,
    *,
    chat_id: str,
    markdown: str,
    scope_key: str,
) -> NotificationRecord:
    """Queue one document notice after (and only after) publication success."""

    if publication.state.value != "success":
        raise NotificationError("document notifications require a successful publication")
    identity = f"publication:{publication.id}:{publication.remote_reference or ''}"
    return state.enqueue_notification(
        notification_idempotency_key(DOCUMENT_EVENT, identity),
        event=DOCUMENT_EVENT,
        payload=_payload(chat_id=chat_id, markdown=markdown, scope_key=scope_key, terminal=False),
        publication_id=publication.id,
    )


def enqueue_pipeline_status_notification(
    state: PipelineState,
    *,
    event: str,
    identity: str,
    chat_id: str,
    markdown: str,
    scope_key: str,
    terminal: bool = False,
) -> NotificationRecord:
    """Queue one low-frequency pipeline status with a durable cohort identity.

    Status notifications are deliberately independent from a process run ID.
    A source window can span multiple bounded worker invocations, so its start,
    milestones, and completion must deduplicate across every invocation while
    remaining recoverable through the shared outbox.
    """

    event_name = str(event).strip()
    if event_name not in PIPELINE_STATUS_EVENTS:
        raise NotificationError(f"unsupported pipeline status event: {event_name}")
    normalized_identity = str(identity).strip()
    if not normalized_identity:
        raise NotificationError("pipeline status identity is required")
    return state.enqueue_notification(
        notification_idempotency_key(event_name, normalized_identity),
        event=event_name,
        payload=_payload(chat_id=chat_id, markdown=markdown, scope_key=scope_key, terminal=terminal),
    )


def enqueue_terminal_notification(
    state: PipelineState,
    *,
    event: str,
    chat_id: str,
    markdown: str,
    scope_key: str,
) -> NotificationRecord:
    """Queue a run-level terminal message behind all document notices in scope."""

    event_name = str(event).strip()
    if event_name not in TERMINAL_EVENTS:
        raise NotificationError(f"unsupported terminal notification event: {event_name}")
    key = notification_idempotency_key(event_name, scope_key)
    # A newer run conclusion replaces any undelivered older terminal state.
    # Document notices are deliberately excluded: each successfully published
    # document remains useful even when a later batch conclusion supersedes.
    state.supersede_pending_notifications(events=TERMINAL_EVENTS, superseded_by=key)
    return state.enqueue_notification(
        key,
        event=event_name,
        payload=_payload(chat_id=chat_id, markdown=markdown, scope_key=scope_key, terminal=True),
    )


def render_document_notice(publication: PublicationRecord, *, title: str, count: int) -> str:
    """Render the concise, user-facing document notice from durable data."""

    reference = str(publication.remote_reference or "").strip()
    if not reference:
        raise NotificationError("successful publication has no remote reference")
    noun = "篇" if int(count) == 1 else "篇研报"
    return f"## 知识星球研报总结\n\n已发布《{str(title).strip() or '研报总结'}》：{int(count)} {noun}\n\n[打开文档]({reference})"


def _source_label(source: str) -> str:
    normalized = str(source).strip()
    lowered = normalized.casefold()
    if "cicc" in lowered or "domestic" in lowered or "中金" in normalized:
        return "中金研报"
    if "foreign" in lowered or "海外" in normalized or "外资" in normalized:
        return "海外研报"
    return normalized or "知识星球研报"


def render_download_complete_notice(*, source: str, count: int) -> str:
    """Render one exact, non-empty download result."""

    total = int(count)
    if total < 1:
        raise NotificationError("download completion requires at least one PDF")
    return (
        "## 知识星球研报｜下载完成\n\n"
        f"{_source_label(source)}：本轮新增 **{total}** 份 PDF，已进入待总结队列。"
    )


def render_summary_started_notice(*, source: str, total: int) -> str:
    """Render the single start notice for one durable source window."""

    count = int(total)
    if count < 1:
        raise NotificationError("summary start requires at least one PDF")
    return (
        "## 知识星球研报｜开始总结\n\n"
        f"{_source_label(source)}：本批共 **{count}** 份 PDF。后续仅在关键进度和完成时更新。"
    )


def render_summary_progress_notice(
    *,
    source: str,
    total: int,
    summarized: int,
    published: int,
    milestone: int,
) -> str:
    """Render one of the bounded 25/50/75 percent progress milestones."""

    count = int(total)
    summary_count = int(summarized)
    publish_count = int(published)
    percentage = int(milestone)
    if count < 1 or not (0 <= summary_count <= count) or not (0 <= publish_count <= count):
        raise NotificationError("pipeline progress counts are inconsistent")
    if percentage not in {25, 50, 75}:
        raise NotificationError("pipeline progress milestone must be 25, 50, or 75")
    return (
        f"## 知识星球研报｜进度 {percentage}%\n\n"
        f"{_source_label(source)}｜总结 **{summary_count}/{count}**｜发布 **{publish_count}/{count}**"
    )


def render_batch_complete_notice(*, source: str, total: int, summarized: int, published: int) -> str:
    """Render the one terminal success notice for a durable source window."""

    count = int(total)
    summary_count = int(summarized)
    publish_count = int(published)
    if count < 1 or summary_count != count or publish_count != count:
        raise NotificationError("batch completion requires every PDF to be summarized and published")
    return (
        "## ✅ 知识星球研报｜本批完成\n\n"
        f"{_source_label(source)}｜总结 **{summary_count}/{count}**｜发布 **{publish_count}/{count}**"
    )


def export_notification_audit(state: PipelineState, path: str | Path) -> None:
    """Write a compatibility-only JSONL projection; it is never read for truth."""

    destination = Path(path).expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    rows = state.list_notifications()
    rendered = "".join(
        json.dumps(
            {
                "idempotency_key": row.idempotency_key,
                "event": row.event,
                "status": row.status,
                "publication_id": row.publication_id,
                "attempt_count": row.attempt_count,
                "payload": dict(row.payload),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        + "\n"
        for row in rows
    )
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(destination)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _payload_text(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise NotificationError(f"notification payload.{name} is required")
    return value.strip()


def _is_terminal(claim: NotificationClaim) -> bool:
    return claim.event in TERMINAL_EVENTS or bool(claim.payload.get("terminal", False))


def _has_pending_document(state: PipelineState, claim: NotificationClaim) -> bool:
    """Keep every terminal status behind any still-actionable document link."""

    for record in state.list_notifications(statuses=_ACTIVE_NOTIFICATION_STATES):
        if record.id == claim.id or record.event != DOCUMENT_EVENT:
            continue
        return True
    return False


def _is_auth_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(token in text for token in ("auth", "token", "permission", "keychain", "unauthorized", "forbidden"))


class NotificationDrainer:
    """Claim and send the durable outbox without coupling it to publication."""

    def __init__(
        self,
        state: PipelineState,
        notifier: LarkNotificationClient,
        *,
        retry_delay: timedelta = timedelta(minutes=5),
        lease_seconds: int = 120,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        if retry_delay.total_seconds() <= 0:
            raise ValueError("retry_delay must be positive")
        if int(lease_seconds) <= 0:
            raise ValueError("lease_seconds must be positive")
        self.state = state
        self.notifier = notifier
        self.retry_delay = retry_delay
        self.lease_seconds = int(lease_seconds)
        self.clock = clock

    def drain(self, *, max_items: int = 100) -> tuple[NotificationDelivery, ...]:
        """Deliver due rows in order; a failure never rolls back publication."""

        if int(max_items) < 1:
            raise ValueError("max_items must be positive")
        delivered: list[NotificationDelivery] = []
        for _ in range(int(max_items)):
            now = self.clock()
            claim = self.state.claim_due_notification(lease_seconds=self.lease_seconds, now=now)
            if claim is None:
                break
            if _is_terminal(claim) and _has_pending_document(self.state, claim):
                self.state.fail_notification(
                    claim,
                    retry_at=now + self.retry_delay,
                    error_code="document_before_terminal",
                    error_detail="a document notification in this run is not delivered yet",
                    now=now,
                )
                delivered.append(
                    NotificationDelivery(claim.idempotency_key, claim.event, "retry_wait", deferred=True)
                )
                # A second immediate claim can only make ordering worse.
                break
            try:
                self.notifier.notify_once(
                    _payload_text(claim.payload, "chat_id"),
                    _payload_text(claim.payload, "markdown"),
                    idempotency_key=claim.idempotency_key,
                )
            except Exception as exc:
                error = str(exc).strip() or type(exc).__name__
                if _is_auth_error(exc):
                    record = self.state.fail_notification(
                        claim,
                        error_code="notification_auth",
                        error_detail=error,
                        now=now,
                    )
                else:
                    record = self.state.fail_notification(
                        claim,
                        retry_at=now + self.retry_delay,
                        error_code="notification_send_failed",
                        error_detail=error,
                        now=now,
                    )
                delivered.append(NotificationDelivery(claim.idempotency_key, claim.event, record.status, error=error))
                # Preserve FIFO/document-before-terminal across a transient outage.
                break
            record = self.state.complete_notification(claim, now=now)
            delivered.append(NotificationDelivery(claim.idempotency_key, claim.event, record.status))
        return tuple(delivered)


__all__ = [
    "BATCH_COMPLETE_EVENT",
    "DOCUMENT_EVENT",
    "DOWNLOAD_COMPLETE_EVENT",
    "PIPELINE_STATUS_EVENTS",
    "SUMMARY_PROGRESS_EVENT",
    "SUMMARY_STARTED_EVENT",
    "TERMINAL_EVENTS",
    "LarkNotificationClient",
    "NotificationDelivery",
    "NotificationDrainer",
    "NotificationError",
    "enqueue_document_notification",
    "enqueue_pipeline_status_notification",
    "enqueue_terminal_notification",
    "export_notification_audit",
    "notification_idempotency_key",
    "render_batch_complete_notice",
    "render_download_complete_notice",
    "render_document_notice",
    "render_summary_progress_notice",
    "render_summary_started_notice",
]
