from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from zsxq_pipeline.lark import LarkCommandError
from zsxq_pipeline.model import PublicationState
from zsxq_pipeline.notify import (
    BATCH_COMPLETE_EVENT,
    DOWNLOAD_BLOCKED_EVENT,
    DOWNLOAD_COMPLETE_EVENT,
    SUMMARY_PROGRESS_EVENT,
    SUMMARY_STARTED_EVENT,
    NotificationDrainer,
    enqueue_document_notification,
    enqueue_pipeline_status_notification,
    enqueue_terminal_notification,
    is_login_error,
    notification_idempotency_key,
    render_batch_complete_notice,
    render_download_blocked_notice,
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

    def notify_once(self, chat_id: str, markdown: str, *, idempotency_key: str, compact_document: bool = False):
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


@pytest.mark.parametrize("error", [
    RuntimeError("network EOF"),
    RuntimeError('Post "https://accounts.feishu.cn/oauth/v3/token": EOF'),
    LarkCommandError("im notify", 4, json.dumps({
        "ok": False,
        "identity": "bot",
        "error": {
            "type": "network", "subtype": "transport",
            "message": 'API call failed: Post "https://accounts.feishu.cn/oauth/v3/token": EOF',
        },
    })),
])
def test_send_failure_waits_without_reverting_publication(tmp_path, error):
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
            FakeNotifier(error=error),
            clock=lambda: NOW,
        ).drain()
        assert deliveries[0].status == "retry_wait"
        assert state.get_notification(queued.idempotency_key).status == "retry_wait"  # type: ignore[union-attr]
        assert state.get_publication("a" * 64, "daily", "partition-1", target_document=publication.target_document).state is PublicationState.SUCCESS  # type: ignore[union-attr]
        notifier = FakeNotifier()
        recovered = NotificationDrainer(
            state, notifier, clock=lambda: NOW + timedelta(minutes=5),
        ).drain()
        assert [delivery.status for delivery in recovered] == ["sent"]
        assert len(notifier.calls) == 1


@pytest.mark.parametrize("error", [
    RuntimeError("HTTP 403"),
    RuntimeError("permission denied (HTTP 403)"),
    RuntimeError("missing_scope: docs:document:write"),
    LarkCommandError("im notify", 3, json.dumps({
        "ok": False,
        "error": {"type": "permission", "message": "missing required scope"},
    })),
])
def test_permission_failure_stops_automatic_notification_retries(tmp_path, error):
    with PipelineState.open(tmp_path / "pipeline.sqlite3") as state:
        state.migrate()
        publication = _publication(state)
        queued = enqueue_document_notification(
            state, publication, chat_id="oc_test", markdown="document", scope_key="run-1",
        )
        deliveries = NotificationDrainer(
            state, FakeNotifier(error=error), clock=lambda: NOW,
        ).drain()
        assert [delivery.status for delivery in deliveries] == ["failed"]
        assert state.get_notification(queued.idempotency_key).status == "failed"  # type: ignore[union-attr]
        notifier = FakeNotifier()
        assert NotificationDrainer(
            state, notifier, clock=lambda: NOW + timedelta(days=1),
        ).drain() == ()
        assert notifier.calls == []
        assert state.get_publication("a" * 64, "daily", "partition-1", target_document=publication.target_document).state is PublicationState.SUCCESS  # type: ignore[union-attr]


@pytest.mark.parametrize("error", [
    RuntimeError("Unauthorized (HTTP 401)"),
    RuntimeError("HTTP 401"),
    RuntimeError("User identity is missing; refresh token expired"),
    RuntimeError("invalid access token"),
    LarkCommandError("im notify", 3, json.dumps({
        "ok": False,
        "error": {"type": "auth", "message": "User identity is missing; refresh token expired"},
    })),
])
def test_fixed_login_resumes_original_notification_after_sqlite_reopen(tmp_path, error):
    database = tmp_path / "pipeline.sqlite3"
    with PipelineState.open(database) as state:
        state.migrate()
        publication = _publication(state)
        queued = enqueue_document_notification(
            state, publication, chat_id="oc_test", markdown="document", scope_key="run-1",
        )
        deliveries = NotificationDrainer(
            state, FakeNotifier(error=error), clock=lambda: NOW,
        ).drain()
        assert [delivery.status for delivery in deliveries] == ["retry_wait"]
        assert state.get_notification(queued.idempotency_key).status == "retry_wait"  # type: ignore[union-attr]

    # Logging in repairs the external dependency; neither a new message nor
    # an in-memory timer is needed after a process restart.
    with PipelineState.open(database) as reopened:
        notifier = FakeNotifier()
        assert NotificationDrainer(
            reopened, notifier, clock=lambda: NOW + timedelta(minutes=5, seconds=-1),
        ).drain() == ()
        assert notifier.calls == []
        deliveries = NotificationDrainer(
            reopened, notifier, clock=lambda: NOW + timedelta(minutes=5),
        ).drain()
        assert [delivery.status for delivery in deliveries] == ["sent"]
        assert [call[2] for call in notifier.calls] == [queued.idempotency_key]
        assert reopened.get_notification(queued.idempotency_key).id == queued.id  # type: ignore[union-attr]
        assert reopened.get_publication("a" * 64, "daily", "partition-1", target_document=publication.target_document).state is PublicationState.SUCCESS  # type: ignore[union-attr]
        assert NotificationDrainer(
            reopened, notifier, clock=lambda: NOW + timedelta(days=1),
        ).drain() == ()
        assert len(notifier.calls) == 1


def test_login_retry_backoff_is_persistent_and_caps_at_one_hour(tmp_path):
    current = NOW
    with PipelineState.open(tmp_path / "pipeline.sqlite3") as state:
        state.migrate()
        publication = _publication(state)
        queued = enqueue_document_notification(
            state, publication, chat_id="oc_test", markdown="document", scope_key="run-1",
        )
        notifier = FakeNotifier(error=RuntimeError("refresh token expired"))
        for attempt, minutes in enumerate((5, 10, 20, 40, 60, 60), 1):
            deliveries = NotificationDrainer(state, notifier, clock=lambda: current).drain()
            assert [delivery.status for delivery in deliveries] == ["retry_wait"]
            assert state.get_notification(queued.idempotency_key).attempt_count == attempt  # type: ignore[union-attr]
            before_due = FakeNotifier()
            assert NotificationDrainer(
                state, before_due, clock=lambda: current + timedelta(minutes=minutes, seconds=-1),
            ).drain() == ()
            assert before_due.calls == []
            current += timedelta(minutes=minutes)
        assert [call[2] for call in notifier.calls] == [queued.idempotency_key] * 6


def test_login_failure_stops_the_current_drain_before_other_messages(tmp_path):
    with PipelineState.open(tmp_path / "pipeline.sqlite3") as state:
        state.migrate()
        first = enqueue_pipeline_status_notification(
            state, event=DOWNLOAD_COMPLETE_EVENT, identity="first", chat_id="oc_test",
            markdown="first", scope_key="run-1",
        )
        second = enqueue_pipeline_status_notification(
            state, event=SUMMARY_PROGRESS_EVENT, identity="second", chat_id="oc_test",
            markdown="second", scope_key="run-1",
        )
        notifier = FakeNotifier(error=RuntimeError("refresh token expired"))
        deliveries = NotificationDrainer(state, notifier, clock=lambda: NOW).drain(max_items=100)
        assert [delivery.idempotency_key for delivery in deliveries] == [first.idempotency_key]
        assert len(notifier.calls) == 1
        assert state.get_notification(first.idempotency_key).status == "retry_wait"  # type: ignore[union-attr]
        assert state.get_notification(second.idempotency_key).status == "queued"  # type: ignore[union-attr]


@pytest.mark.parametrize(("error", "expected"), [
    ("User identity is missing; refresh token expired", True),
    ("status code: 401", True),
    ("authentication required", True),
    ("auth required", True),
    ("credentials missing", True),
    ("session has expired", True),
    ("invalid access token", True),
    ("HTTP 403", False),
    ("missing_scope", False),
    ("insufficient_scope: login required for this scope", False),
    ("HTTP 403: token expired", False),
    ('Post "https://accounts.feishu.cn/oauth/v3/token": EOF', False),
    ("tokens used 1234", False),
    (LarkCommandError("im notify", 4, json.dumps({
        "error": {"type": "network", "message": "token refresh failed: EOF"},
    })), False),
])
def test_login_error_recognition_distinguishes_permissions_and_transport(error, expected):
    assert is_login_error(error) is expected


@pytest.mark.parametrize("as_string", [False, True])
@pytest.mark.parametrize(("kind", "message", "expected"), [
    ("permission", "user identity is missing", False),
    ("permission", "HTTP 401 Unauthorized", False),
    ("network", "authentication required: EOF", False),
    ("transport", "token refresh failed", False),
    ("timeout", "credentials missing from timed-out response", False),
    ("auth", "auth required", True),
    ("auth", "credentials missing", True),
    ("authentication", "credentials unavailable", True),
    ("login", "sign-in must be renewed", True),
    ("auth", "HTTP 403: token expired", False),
    ("authentication", "missing_scope: login required", False),
])
def test_structured_login_error_keeps_category_precedence_when_persisted(as_string, kind, message, expected):
    error = LarkCommandError("docs create", 4, json.dumps({"error": {"type": kind, "message": message}}))
    assert is_login_error(str(error) if as_string else error) is expected


@pytest.mark.parametrize("as_string", [False, True])
def test_structured_login_error_accepts_trailing_cli_diagnostics(as_string):
    diagnostic = json.dumps({"error": {"type": "permission", "message": "auth required"}})
    error = LarkCommandError("docs create", 4, diagnostic + "\nadditional CLI diagnostics")
    assert is_login_error(str(error) if as_string else error) is False


def test_unrelated_text_does_not_supply_a_structured_cli_error_type():
    diagnostic = json.dumps({"error": {"type": "auth", "message": "credentials unavailable"}})
    assert is_login_error("report text " + diagnostic) is False


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


def test_download_blocked_notice_is_reason_specific_and_deduplicated(tmp_path):
    scope = "source-window:42"
    identity = f"{scope}:blocked_browser_cdp_unresponsive"
    markdown = render_download_blocked_notice(
        source="zsxq_foreign",
        reason_code="blocked_browser_cdp_unresponsive",
    )
    with PipelineState.open(tmp_path / "pipeline.sqlite3") as state:
        state.migrate()
        first = enqueue_pipeline_status_notification(
            state,
            event=DOWNLOAD_BLOCKED_EVENT,
            identity=identity,
            chat_id="oc_test",
            markdown=markdown,
            scope_key=scope,
        )
        duplicate = enqueue_pipeline_status_notification(
            state,
            event=DOWNLOAD_BLOCKED_EVENT,
            identity=identity,
            chat_id="oc_test",
            markdown=markdown,
            scope_key=scope,
        )

        assert first.created is True
        assert duplicate.created is False
        assert "已保留" in first.payload["markdown"]
        assert "已执行有界自愈" not in first.payload["markdown"]
        assert "`blocked_browser_cdp_unresponsive`" in first.payload["markdown"]


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


def test_document_notice_counts_appends_and_lists_only_current_files(tmp_path):
    from zsxq_pipeline.notify import document_entry_total, render_document_notice

    with PipelineState.open(tmp_path / "pipeline.sqlite3") as state:
        state.migrate()
        url = "https://feishu.cn/docx/doxcn12345678"
        records = []
        for index, count in enumerate((10, 6, 2), 1):
            digest = str(index) * 64
            state.record_remote_write(
                digest, "daily", f"part-{index}", remote_reference=url,
                target_document=url,
                details={"created_document": index == 1, "entry_count": count}, now=NOW,
            )
            records.append(state.complete_publication(
                digest, "daily", f"part-{index}", target_document=url, now=NOW,
            ))
        assert document_entry_total(state, records[0]) == 10
        assert document_entry_total(state, records[1]) == 16
        assert document_entry_total(state, records[2]) == 18
        notice = render_document_notice(
            records[1], title="old batch title", count=6,
            filenames=[f"新增研报{i}.pdf" for i in range(6)],
            total=document_entry_total(state, records[1]),
        )
        assert "已追加到原文档｜本次新增 6 篇｜文档累计 16 篇" in notice
        assert "6. 新增研报5" in notice
        assert "old batch title" not in notice
        assert ".pdf" not in notice
        created = render_document_notice(records[0], title="first", count=10, total=10)
        assert "新建文档｜本次新增 10 篇｜文档累计 10 篇" in created


def test_unknown_document_history_does_not_claim_a_total(tmp_path):
    from zsxq_pipeline.notify import document_entry_total, render_document_notice

    with PipelineState.open(tmp_path / "pipeline.sqlite3") as state:
        state.migrate()
        publication = _publication(state)
        assert document_entry_total(state, publication) is None
        assert "累计" not in render_document_notice(publication, title="report", count=1)


def _digest_document(state, index, source, url, count, at, *, chat="group", title=""):
    sha = f"{index:064x}"
    state.record_remote_write(
        sha, "daily", str(index), remote_reference=url, target_document=url, now=at,
        details={"entry_count": count, "entries": [{"source": source}], "title": title},
    )
    publication = state.complete_publication(sha, "daily", str(index), target_document=url, now=at)
    return state.enqueue_notification(
        f"document-{index}", event="document_ready", publication_id=publication.id, now=at,
        payload={"markdown": f"[打开文档]({url})", "chat_id": chat, "scope_key": source},
    )


def test_scheduled_digest_folds_updates_per_link_and_chat_and_keeps_urgent_alerts(tmp_path):
    opening = NOW.replace(hour=1, minute=0, second=0)  # 09:00 in Shanghai
    now = opening - timedelta(seconds=1)
    notifier = FakeNotifier()
    url = "https://feishu.cn/docx/foreign"
    domestic = "https://feishu.cn/docx/domestic"
    with PipelineState.open(tmp_path / "pipeline.sqlite3") as state:
        state.migrate()
        drainer = NotificationDrainer(
            state, notifier, clock=lambda: now, digest_times=("09:00", "18:00"),
        )
        _digest_document(state, 1, "foreign", url, 4, now)
        _digest_document(state, 2, "foreign", url, 2, now)
        _digest_document(state, 3, "unified-tick", domestic, 1, now,
                         title="知识星球研报总结（2026-08-10） 1 篇")
        _digest_document(state, 4, "foreign", url, 1, now, chat="other")
        for scope, manual in [("foreign:scan", False), ("cicc:download", True)]:
            state.enqueue_notification(
                scope, event="incident_open", now=now,
                payload={"scope_key": scope, "requires_action": manual, "markdown": scope, "chat_id": "group"},
            )
        assert [d.event for d in drainer.drain()] == ["incident_open"]
        assert notifier.calls == [("group", "cicc:download", "cicc:download")]
        now = opening
        assert [d.event for d in drainer.drain()] == ["notification_digest"] * 2
        body = next(body for chat, body, _ in notifier.calls if chat == "group" and "汇总" in body)
        assert body.count(url) == 1 and body.count(domestic) == 1
        assert f"[海外研报]({url}) · 新增 6 篇" in body
        assert f"[知识星球研报总结（2026-08-10）]({domestic}) · 新增 1 篇" in body
        assert "unified-tick" not in body
        assert "海外研报 · 列表扫描" in body and "08-10 09:00" in body
        assert len({n.payload["digest_id"] for n in state.list_notifications()
                    if n.event == "document_ready"}) == 2
        # A second writer can commit in the same second, after this slot is frozen.
        _digest_document(state, 5, "foreign", url, 3, now)
        assert not drainer.drain()
        now = opening + timedelta(hours=9)
        assert len(drainer.drain()) == 1 and "新增 3 篇" in notifier.calls[-1][1]
        now += timedelta(days=1)
        assert not drainer.drain()
        assert {n.status for n in state.list_notifications()} == {"sent"}


def test_digest_retries_frozen_members_after_restart_and_catches_up_once(tmp_path):
    path = tmp_path / "pipeline.sqlite3"
    due = NOW.replace(hour=10, minute=0, second=0)  # 18:00 in Shanghai
    now = due
    notifier = FakeNotifier(error=RuntimeError("offline"))
    url = "https://feishu.cn/docx/foreign"
    def drainer(state):
        return NotificationDrainer(state, notifier, clock=lambda: now, digest_times=("18:00",))
    with PipelineState.open(path) as state:
        state.migrate()
        _digest_document(state, 1, "foreign", url, 1, due - timedelta(hours=2))
        assert drainer(state).drain()[0].status == "retry_wait"
        first = notifier.calls[0]
        (member,) = state.list_notifications(statuses=("digest_pending",))
        assert member.attempt_count == 0
        # A process crash after freezing work must not allow the individual post.
        _digest_document(state, 2, "foreign", url, 7, due + timedelta(hours=1))
    notifier.error = None
    now = due + timedelta(days=3, minutes=1)
    with PipelineState.open(path) as state:
        assert [d.event for d in drainer(state).drain()] == ["notification_digest"]
        assert notifier.calls == [first, first]
        assert not drainer(state).drain()
        now += timedelta(days=1)
        assert len(drainer(state).drain()) == 1 and "新增 7 篇" in notifier.calls[-1][1]
        assert {n.status for n in state.list_notifications()} == {"sent"}


def test_digest_keeps_attempted_legacy_key_and_propagates_permission_failure(tmp_path):
    now = NOW
    notifier = FakeNotifier()
    with PipelineState.open(tmp_path / "pipeline.sqlite3") as state:
        state.migrate()
        url = "https://feishu.cn/docx/doc"
        _digest_document(state, 1, "foreign", url, 1, now)
        claim = state.claim_due_notification(now=now)
        state.fail_notification(claim, retry_at=now + timedelta(minutes=1), now=now)
        _digest_document(state, 2, "foreign", url, 3, now)
        drainer = NotificationDrainer(state, notifier, clock=lambda: now, digest_times=("18:00",))
        now += timedelta(minutes=2)
        assert [d.idempotency_key for d in drainer.drain()] == [claim.idempotency_key]
        now += timedelta(days=1)
        notifier.error = RuntimeError("permission denied")
        assert drainer.drain()[0].status == "failed"
        member = next(n for n in state.list_notifications() if n.idempotency_key == "document-2")
        assert member.status == "failed" and member.payload["digest_id"]
        assert not drainer.drain()


def test_digest_link_limit_carries_every_link_into_a_later_slot(tmp_path):
    now = NOW
    notifier = FakeNotifier()
    with PipelineState.open(tmp_path / "pipeline.sqlite3") as state:
        state.migrate()
        urls = [f"https://feishu.cn/docx/doc-{n:03d}" for n in range(24)]
        for n, url in enumerate(urls):
            _digest_document(state, n+1, "foreign", url, 1, now-timedelta(days=3))
        drainer = NotificationDrainer(state, notifier, clock=lambda: now, digest_times=("09:00",))
        assert len(drainer.drain()) == 1 and len(state.list_notifications(statuses=("queued",))) == 4
        now += timedelta(days=1)
        assert len(drainer.drain()) == 1
        combined = "\n".join(body for _, body, _ in notifier.calls)
        assert all(combined.count(url) == 1 for url in urls)


def test_digest_lease_reclaims_same_remote_key_after_send_before_ack(tmp_path):
    from zsxq_pipeline.notification_digest import notification_drain_due

    path = tmp_path / "pipeline.sqlite3"
    now = NOW
    times = ("09:00", "18:00")
    remote = {}
    attempts = []

    class InterruptedSender:
        def notify_once(self, chat, body, *, idempotency_key):
            attempts.append(idempotency_key)
            remote.setdefault(idempotency_key, body)
            if len(attempts) == 1:
                raise SystemExit("process terminated before SQLite acknowledgment")

    with PipelineState.open(path) as state:
        state.migrate()
        _digest_document(state, 1, "foreign", "https://feishu.cn/docx/doc", 1, now-timedelta(hours=1))
        with pytest.raises(SystemExit):
            NotificationDrainer(state, InterruptedSender(), clock=lambda: now, digest_times=times).drain()
        assert not notification_drain_due(state, times, "Asia/Shanghai", now)
    now += timedelta(minutes=3)
    with PipelineState.open(path) as state:
        assert notification_drain_due(state, times, "Asia/Shanghai", now)
        deliveries = NotificationDrainer(state, InterruptedSender(), clock=lambda: now, digest_times=times).drain()
        assert len(deliveries) == 1 and deliveries[0].status == "sent"
        assert set(attempts) == set(remote) and len(attempts) == 2 and len(remote) == 1
        assert not notification_drain_due(state, times, "Asia/Shanghai", now)


def test_scheduler_wakes_parked_updates_when_digest_times_change(tmp_path):
    from zsxq_pipeline.notification_digest import notification_drain_due

    now = NOW
    with PipelineState.open(tmp_path / "pipeline.sqlite3") as state:
        state.migrate()
        _digest_document(state, 1, "foreign", "https://feishu.cn/docx/doc", 1, now)
        def drain(times):
            return NotificationDrainer(state, FakeNotifier(), clock=lambda: now, digest_times=times).drain()
        assert not drain(("18:00",))
        assert not notification_drain_due(state, ("18:00",), "Asia/Shanghai", now)
        assert notification_drain_due(state, ("12:00",), "Asia/Shanghai", now)
        assert not drain(("12:00",))
        assert not notification_drain_due(state, ("12:00",), "Asia/Shanghai", now)
        assert notification_drain_due(state, (), "Asia/Shanghai", now)
        assert [entry.event for entry in drain(())] == ["document_ready"]
