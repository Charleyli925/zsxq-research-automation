from __future__ import annotations

from datetime import datetime, timedelta, timezone

from zsxq_pipeline.model import PublicationState
from zsxq_pipeline.notify import (
    BATCH_COMPLETE_EVENT,
    DOWNLOAD_COMPLETE_EVENT,
    SUMMARY_PROGRESS_EVENT,
    SUMMARY_STARTED_EVENT,
    NotificationDrainer,
    enqueue_document_notification,
    enqueue_pipeline_status_notification,
    enqueue_terminal_notification,
    notification_idempotency_key,
    render_batch_complete_notice,
    render_download_complete_notice,
    render_summary_progress_notice,
    render_summary_started_notice,
)
from zsxq_pipeline.state import PipelineState


NOW = datetime(2026, 8, 10, 1, 2, 3, tzinfo=timezone.utc)


class FakeNotifier:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, str, str]] = []

    def notify_once(self, chat_id: str, markdown: str, *, idempotency_key: str):
        self.calls.append((chat_id, markdown, idempotency_key))
        if self.error is not None:
            raise self.error
        return object()


def _publication(state: PipelineState):
    remote = state.record_remote_write(
        "a" * 64,
        "daily",
        "partition-1",
        remote_reference="https://feishu.cn/docx/doxcn12345678",
        target_document="https://feishu.cn/docx/doxcn12345678",
        now=NOW,
    )
    published = state.complete_publication(
        "a" * 64,
        "daily",
        "partition-1",
        target_document="https://feishu.cn/docx/doxcn12345678",
        now=NOW,
    )
    assert published.id == remote.id
    assert published.state is PublicationState.SUCCESS
    return published


def test_document_delivery_is_idempotent_and_independent_from_publication(tmp_path):
    with PipelineState.open(tmp_path / "pipeline.sqlite3") as state:
        state.migrate()
        publication = _publication(state)
        first = enqueue_document_notification(
            state,
            publication,
            chat_id="oc_test",
            markdown="[打开文档](https://feishu.cn/docx/doxcn12345678)",
            scope_key="run-1",
        )
        duplicate = enqueue_document_notification(
            state,
            publication,
            chat_id="oc_test",
            markdown="[打开文档](https://feishu.cn/docx/doxcn12345678)",
            scope_key="run-1",
        )
        assert first.created is True
        assert duplicate.created is False

        notifier = FakeNotifier()
        deliveries = NotificationDrainer(state, notifier, clock=lambda: NOW).drain()
        assert [delivery.status for delivery in deliveries] == ["sent"]
        assert len(notifier.calls) == 1
        assert state.get_publication("a" * 64, "daily", "partition-1", target_document=publication.target_document)
        assert state.get_notification(first.idempotency_key).status == "sent"  # type: ignore[union-attr]


def test_send_failure_waits_without_reverting_publication(tmp_path):
    with PipelineState.open(tmp_path / "pipeline.sqlite3") as state:
        state.migrate()
        publication = _publication(state)
        queued = enqueue_document_notification(
            state,
            publication,
            chat_id="oc_test",
            markdown="document",
            scope_key="run-1",
        )
        deliveries = NotificationDrainer(
            state,
            FakeNotifier(error=RuntimeError("network EOF")),
            clock=lambda: NOW,
        ).drain()
        assert deliveries[0].status == "retry_wait"
        assert state.get_notification(queued.idempotency_key).status == "retry_wait"  # type: ignore[union-attr]
        assert state.get_publication("a" * 64, "daily", "partition-1", target_document=publication.target_document).state is PublicationState.SUCCESS  # type: ignore[union-attr]


def test_terminal_is_deferred_until_document_delivery_has_finished(tmp_path):
    with PipelineState.open(tmp_path / "pipeline.sqlite3") as state:
        state.migrate()
        publication = _publication(state)
        document = enqueue_document_notification(
            state,
            publication,
            chat_id="oc_test",
            markdown="document",
            scope_key="run-1",
        )
        terminal = enqueue_terminal_notification(
            state,
            event="run_complete",
            chat_id="oc_test",
            markdown="complete",
            scope_key="run-1",
        )
        # Make the document wait so a terminal row would otherwise leapfrog it.
        claim = state.claim_due_notification(now=NOW)
        assert claim is not None and claim.idempotency_key == document.idempotency_key
        state.fail_notification(claim, retry_at=NOW + timedelta(minutes=1), now=NOW)

        notifier = FakeNotifier()
        deliveries = NotificationDrainer(state, notifier, clock=lambda: NOW).drain()
        assert len(deliveries) == 1
        assert deliveries[0].deferred is True
        assert notifier.calls == []
        assert state.get_notification(terminal.idempotency_key).status == "retry_wait"  # type: ignore[union-attr]


def test_new_terminal_supersedes_old_terminal_and_waits_for_another_run_document(tmp_path):
    with PipelineState.open(tmp_path / "pipeline.sqlite3") as state:
        state.migrate()
        publication = _publication(state)
        document = enqueue_document_notification(
            state,
            publication,
            chat_id="oc_test",
            markdown="document",
            scope_key="run-old",
        )
        old_terminal = enqueue_terminal_notification(
            state,
            event="run_partial",
            chat_id="oc_test",
            markdown="old partial",
            scope_key="run-old",
        )
        claim = state.claim_due_notification(now=NOW)
        assert claim is not None and claim.idempotency_key == document.idempotency_key
        state.fail_notification(claim, retry_at=NOW + timedelta(minutes=1), now=NOW)

        new_terminal = enqueue_terminal_notification(
            state,
            event="run_complete",
            chat_id="oc_test",
            markdown="new complete",
            scope_key="run-new",
        )
        assert state.get_notification(old_terminal.idempotency_key).status == "superseded"  # type: ignore[union-attr]

        deliveries = NotificationDrainer(state, FakeNotifier(), clock=lambda: NOW).drain()
        assert len(deliveries) == 1
        assert deliveries[0].idempotency_key == new_terminal.idempotency_key
        assert deliveries[0].deferred is True


def test_notification_keys_are_stable_and_fit_lark_limit():
    first = notification_idempotency_key("document_ready", "x" * 1000)
    assert first == notification_idempotency_key("document_ready", "x" * 1000)
    assert len(first) <= 50


def test_pipeline_statuses_are_concise_window_scoped_and_idempotent(tmp_path):
    scope = "source-window:42"
    with PipelineState.open(tmp_path / "pipeline.sqlite3") as state:
        state.migrate()
        download = enqueue_pipeline_status_notification(
            state,
            event=DOWNLOAD_COMPLETE_EVENT,
            identity=scope,
            chat_id="oc_test",
            markdown=render_download_complete_notice(source="zsxq_foreign", count=19),
            scope_key=scope,
        )
        duplicate = enqueue_pipeline_status_notification(
            state,
            event=DOWNLOAD_COMPLETE_EVENT,
            identity=scope,
            chat_id="oc_test",
            markdown=render_download_complete_notice(source="zsxq_foreign", count=19),
            scope_key=scope,
        )
        started = enqueue_pipeline_status_notification(
            state,
            event=SUMMARY_STARTED_EVENT,
            identity=scope,
            chat_id="oc_test",
            markdown=render_summary_started_notice(source="zsxq_foreign", total=19),
            scope_key=scope,
        )
        progress = enqueue_pipeline_status_notification(
            state,
            event=SUMMARY_PROGRESS_EVENT,
            identity=f"{scope}:milestone:50",
            chat_id="oc_test",
            markdown=render_summary_progress_notice(
                source="zsxq_foreign", total=19, summarized=12, published=7, milestone=50
            ),
            scope_key=scope,
        )
        complete = enqueue_pipeline_status_notification(
            state,
            event=BATCH_COMPLETE_EVENT,
            identity=scope,
            chat_id="oc_test",
            markdown=render_batch_complete_notice(source="zsxq_foreign", total=19, summarized=19, published=19),
            scope_key=scope,
            terminal=True,
        )

        assert download.created is True
        assert duplicate.created is False
        assert [item.event for item in state.list_notifications()] == [
            DOWNLOAD_COMPLETE_EVENT,
            SUMMARY_STARTED_EVENT,
            SUMMARY_PROGRESS_EVENT,
            BATCH_COMPLETE_EVENT,
        ]
        assert "本轮新增 **19** 份 PDF" in download.payload["markdown"]
        assert "总结 **12/19**｜发布 **7/19**" in progress.payload["markdown"]
        assert started.payload["terminal"] is False
        assert complete.payload["terminal"] is True


def test_batch_completion_waits_for_its_document_link(tmp_path):
    scope = "source-window:42"
    with PipelineState.open(tmp_path / "pipeline.sqlite3") as state:
        state.migrate()
        publication = _publication(state)
        document = enqueue_document_notification(
            state,
            publication,
            chat_id="oc_test",
            markdown="document",
            scope_key=scope,
        )
        complete = enqueue_pipeline_status_notification(
            state,
            event=BATCH_COMPLETE_EVENT,
            identity=scope,
            chat_id="oc_test",
            markdown=render_batch_complete_notice(source="zsxq_foreign", total=1, summarized=1, published=1),
            scope_key=scope,
            terminal=True,
        )
        claim = state.claim_due_notification(now=NOW)
        assert claim is not None and claim.idempotency_key == document.idempotency_key
        state.fail_notification(claim, retry_at=NOW + timedelta(minutes=1), now=NOW)

        notifier = FakeNotifier()
        deliveries = NotificationDrainer(state, notifier, clock=lambda: NOW).drain()
        assert len(deliveries) == 1 and deliveries[0].deferred is True
        assert notifier.calls == []
        assert state.get_notification(complete.idempotency_key).status == "retry_wait"  # type: ignore[union-attr]
