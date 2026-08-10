from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from zsxq_pipeline.model import ErrorCategory, PublicationState, Stage, StageState, SummaryIdentity
from zsxq_pipeline.state import InvariantViolation, LeaseLostError, PipelineState


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def test_documents_keep_source_identity_while_pdf_content_is_deduplicated(tmp_path):
    database = tmp_path / "pipeline.sqlite3"
    sha = "a" * 64
    with PipelineState.open(database) as state:
        state.migrate()
        window = state.register_source_window("zsxq_foreign", NOW - timedelta(hours=1), NOW, now=NOW)
        first = state.upsert_document(
            "zsxq_foreign", "file-1", filename="first.pdf", source_path="/library/a.pdf", source_window_id=window, now=NOW
        )
        second = state.upsert_document(
            "zsxq_domestic_cicc", "file-2", filename="second.pdf", source_path="/library/b.pdf", now=NOW
        )
        first_artifact = state.record_artifact(first.id, kind="pdf", path="/library/a.pdf", pdf_sha256=sha, now=NOW)
        second_artifact = state.record_artifact(second.id, kind="pdf", path="/library/b.pdf", pdf_sha256=sha, now=NOW)

        assert first_artifact.id == second_artifact.id
        assert state.table_count("documents") == 2
        assert state.table_count("artifacts") == 1

        summary = state.record_artifact(
            first.id,
            kind="summary",
            path="/library/summaries/first.md",
            pdf_sha256=sha,
            content_sha256="c" * 64,
            extractor_version="extract-v1",
            prompt_version="prompt-v1",
            model="model-v1",
            now=NOW,
        )
        same_summary = state.record_artifact(
            second.id,
            kind="summary",
            path="/library/summaries/first-copy.md",
            pdf_sha256=sha,
            content_sha256="c" * 64,
            extractor_version="extract-v1",
            prompt_version="prompt-v1",
            model="model-v1",
            now=NOW,
        )
        assert summary.id == same_summary.id
        assert state.get_document(first.id).artifact_id == first_artifact.id
        assert state.table_count("artifacts") == 2
        with pytest.raises(InvariantViolation, match="conflicting content"):
            state.record_artifact(
                second.id,
                kind="summary",
                path="/library/summaries/first-conflict.md",
                pdf_sha256=sha,
                content_sha256="d" * 64,
                extractor_version="extract-v1",
                prompt_version="prompt-v1",
                model="model-v1",
                now=NOW,
            )

        changed = state.upsert_document("zsxq_foreign", "file-3", source_path="/library/a.pdf", now=NOW)
        with pytest.raises(InvariantViolation, match="different content"):
            state.record_artifact(changed.id, kind="pdf", path="/library/a.pdf", pdf_sha256="b" * 64, now=NOW)


def test_claim_is_exclusive_and_expired_leases_are_recoverable_after_a_crash(tmp_path):
    database = tmp_path / "pipeline.sqlite3"
    first = PipelineState.open(database)
    first.migrate()
    document = first.upsert_document("zsxq_foreign", "file-1", filename="report.pdf", now=NOW)
    first.ensure_stage(document.id, Stage.SUMMARY, "workflow-v1", now=NOW)
    second = PipelineState.open(database)
    try:
        initial_claim = first.claim_due_stage(Stage.SUMMARY, "workflow-v1", lease_seconds=30, now=NOW)
        assert initial_claim is not None
        assert second.claim_due_stage(Stage.SUMMARY, "workflow-v1", lease_seconds=30, now=NOW) is None

        recovered_claim = second.claim_due_stage(
            Stage.SUMMARY, "workflow-v1", lease_seconds=30, now=NOW + timedelta(seconds=31)
        )
        assert recovered_claim is not None
        assert recovered_claim.lease_token != initial_claim.lease_token
        assert recovered_claim.attempt_count == 2
        with pytest.raises(LeaseLostError):
            first.complete_stage(initial_claim, now=NOW + timedelta(seconds=31))
        second.complete_stage(recovered_claim, now=NOW + timedelta(seconds=32))
        final = second.get_stage_attempt(document.id, Stage.SUMMARY, "workflow-v1")
        assert final is not None
        assert final["state"] == StageState.SUCCEEDED.value
        assert second.table_count("leases") == 0
    finally:
        first.close()
        second.close()


def test_document_scoped_stage_claim_never_mutates_another_batch(tmp_path):
    database = tmp_path / "pipeline.sqlite3"
    with PipelineState.open(database) as state:
        state.migrate()
        first = state.upsert_document("zsxq_digest", "first", filename="first.pdf", now=NOW)
        second = state.upsert_document("zsxq_digest", "second", filename="second.pdf", now=NOW)
        state.ensure_stage(first.id, Stage.TEXT_EXTRACT, "extract:v1", now=NOW)
        state.ensure_stage(second.id, Stage.TEXT_EXTRACT, "extract:v1", now=NOW)

        claim = state.claim_due_stage(
            Stage.TEXT_EXTRACT,
            "extract:v1",
            document_ids=(first.id,),
            now=NOW,
        )

        assert claim is not None
        assert claim.document_id == first.id
        foreign = state.get_stage_attempt(second.id, Stage.TEXT_EXTRACT, "extract:v1")
        assert foreign is not None
        assert foreign["state"] == StageState.QUEUED.value


def test_missing_artifact_repair_requeues_only_a_successful_stage(tmp_path):
    with PipelineState.open(tmp_path / "pipeline.sqlite3") as state:
        state.migrate()
        document = state.upsert_document("zsxq_digest", "report", filename="report.pdf", now=NOW)
        state.ensure_stage(document.id, Stage.TEXT_EXTRACT, "extract:v1", now=NOW)
        claim = state.claim_due_stage(Stage.TEXT_EXTRACT, "extract:v1", now=NOW)
        assert claim is not None
        state.complete_stage(claim, now=NOW)

        assert state.requeue_succeeded_stage(
            document.id,
            Stage.TEXT_EXTRACT,
            "extract:v1",
            reason="fixture text cache was removed",
            now=NOW,
        )
        repaired = state.get_stage_attempt(document.id, Stage.TEXT_EXTRACT, "extract:v1")
        assert repaired is not None
        assert repaired["state"] == StageState.QUEUED.value
        assert repaired["error_code"] == "artifact_recovery"
        assert not state.requeue_succeeded_stage(
            document.id,
            Stage.TEXT_EXTRACT,
            "extract:v1",
            reason="must not reopen a queued stage twice",
            now=NOW,
        )
def test_failure_categories_control_retry_and_publication_never_rewinds_remote_written(tmp_path):
    database = tmp_path / "pipeline.sqlite3"
    summary_sha = "c" * 64
    with PipelineState.open(database) as state:
        state.migrate()
        document = state.upsert_document("zsxq_digest", "file-1", filename="report.pdf", now=NOW)
        state.ensure_stage(document.id, Stage.PUBLISH, "workflow-v1", now=NOW)
        claim = state.claim_due_stage(Stage.PUBLISH, "workflow-v1", now=NOW)
        assert claim is not None
        outcome = state.fail_stage(
            claim,
            category=ErrorCategory.RELEASE_CONTRACT,
            error_code="release_contract_mismatch",
            error_detail="fixture only",
            now=NOW,
        )
        assert outcome is StageState.BLOCKED_RELEASE
        attempt = state.get_stage_attempt(document.id, Stage.PUBLISH, "workflow-v1")
        assert attempt is not None
        assert attempt["available_at_epoch"] is None
        assert attempt["error_category"] == ErrorCategory.RELEASE_CONTRACT.value

        remote = state.record_remote_write(
            summary_sha,
            "lark:daily",
            "2026-08-10",
            remote_reference="https://www.feishu.cn/docx/fixture",
            now=NOW,
        )
        assert remote.state is PublicationState.REMOTE_WRITTEN

    with PipelineState.open(database) as reopened:
        publication = reopened.get_publication(summary_sha, "lark:daily", "2026-08-10")
        assert publication is not None
        assert publication.state is PublicationState.REMOTE_WRITTEN
        completed = reopened.complete_publication(summary_sha, "lark:daily", "2026-08-10", now=NOW + timedelta(minutes=1))
        assert completed.state is PublicationState.SUCCESS
        with pytest.raises(InvariantViolation, match="cannot be rebound"):
            reopened.record_remote_write(
                summary_sha,
                "lark:daily",
                "2026-08-10",
                remote_reference="https://www.feishu.cn/docx/different",
                now=NOW + timedelta(minutes=2),
            )


def test_notification_idempotency_key_is_unique(tmp_path):
    with PipelineState.open(tmp_path / "pipeline.sqlite3") as state:
        state.migrate()
        first = state.enqueue_notification("run:1:complete", event="completed", payload={"count": 1}, now=NOW)
        second = state.enqueue_notification("run:1:complete", event="completed", payload={"count": 2}, now=NOW)
        assert first.created is True
        assert second.created is False
        assert state.table_count("notification_outbox") == 1


def test_summary_identity_includes_reasoning_and_has_a_stable_cache_key(tmp_path):
    sha = "a" * 64
    identity = SummaryIdentity(sha, "extract-v1", "prompt-v1", "model-v1", "high")
    assert identity.cache_key == SummaryIdentity(sha, "extract-v1", "prompt-v1", "model-v1", "high").cache_key
    assert identity.cache_key != SummaryIdentity(sha, "extract-v1", "prompt-v1", "model-v1", "medium").cache_key

    with PipelineState.open(tmp_path / "pipeline.sqlite3") as state:
        state.migrate()
        document = state.upsert_document("zsxq_digest", "file-1", filename="report.pdf", now=NOW)
        high = state.record_artifact(
            document.id,
            kind="summary",
            path="/runtime/high.md",
            pdf_sha256=sha,
            content_sha256="b" * 64,
            extractor_version=identity.extractor_version,
            prompt_version=identity.prompt_version,
            model=identity.model,
            reasoning=identity.reasoning,
            now=NOW,
        )
        medium = state.record_artifact(
            document.id,
            kind="summary",
            path="/runtime/medium.md",
            pdf_sha256=sha,
            content_sha256="c" * 64,
            extractor_version=identity.extractor_version,
            prompt_version=identity.prompt_version,
            model=identity.model,
            reasoning="medium",
            now=NOW,
        )

        assert high.id != medium.id
        assert state.find_summary_artifact(identity) == high
        assert state.find_summary_artifact(sha, "extract-v1", "prompt-v1", "model-v1", "medium") == medium
        assert state.find_summary_artifact(sha, "extract-v1", "prompt-v1", "model-v1", "low") is None


def test_publication_target_document_binds_pending_intent_without_rewriting_remote(tmp_path):
    summary_sha = "a" * 64
    with PipelineState.open(tmp_path / "pipeline.sqlite3") as state:
        state.migrate()
        intent = state.record_publication_intent(summary_sha, "lark:daily", "2026-08-10", now=NOW)
        remote = state.record_remote_write(
            summary_sha,
            "lark:daily",
            "2026-08-10",
            target_document="daily-doc-1",
            remote_reference="https://example.invalid/daily-doc-1",
            now=NOW,
        )
        assert remote.id == intent.id
        assert remote.target_document == "daily-doc-1"
        assert state.get_publication(
            summary_sha, "lark:daily", "2026-08-10", target_document="daily-doc-1"
        ) == remote
        assert state.get_publication(summary_sha, "lark:daily", "2026-08-10") is None
        second = state.record_publication_intent(
            summary_sha, "lark:daily", "2026-08-10", target_document="daily-doc-2", now=NOW
        )
        assert second.id != remote.id
        assert [record.target_document for record in state.find_publications(summary_sha, "lark:daily", "2026-08-10")] == [
            "daily-doc-1",
            "daily-doc-2",
        ]


def test_notification_delivery_claims_are_exclusive_recoverable_and_queryable(tmp_path):
    database = tmp_path / "pipeline.sqlite3"
    first = PipelineState.open(database)
    first.migrate()
    second = PipelineState.open(database)
    try:
        document_notice = first.enqueue_notification(
            "run:1:document", event="document_ready", payload={"run_id": "run-1"}, now=NOW
        )
        first.enqueue_notification("run:1:terminal", event="run_complete", payload={"run_id": "run-1"}, now=NOW)
        claim = first.claim_due_notification(lease_seconds=30, now=NOW)
        assert claim is not None
        assert claim.idempotency_key == document_notice.idempotency_key
        assert second.claim_due_notification(lease_seconds=30, now=NOW) is not None
        first.fail_notification(claim, retry_at=NOW + timedelta(minutes=1), error_code="network", now=NOW)

        pending = first.list_notifications(statuses={"queued", "retry_wait"})
        assert [item.idempotency_key for item in pending] == ["run:1:document"]
        # The terminal row was already claimed by the second worker.  Its
        # pending state remains observable so an orchestrator can keep the
        # terminal message behind the retrying document message.
        running = first.list_notifications(statuses={"running"}, event_prefix="run_")
        assert [item.idempotency_key for item in running] == ["run:1:terminal"]
        terminal_claim = second.claim_due_notification(lease_seconds=30, now=NOW + timedelta(seconds=31))
        assert terminal_claim is not None
        assert terminal_claim.idempotency_key == "run:1:terminal"
        second.complete_notification(terminal_claim, now=NOW + timedelta(seconds=32))

        retry_claim = first.claim_due_notification(lease_seconds=30, now=NOW + timedelta(minutes=1))
        assert retry_claim is not None
        assert retry_claim.idempotency_key == "run:1:document"
        sent = first.complete_notification(retry_claim, now=NOW + timedelta(minutes=1, seconds=1))
        assert sent.status == "sent"
        assert [item.idempotency_key for item in first.list_notifications(statuses="sent")] == [
            "run:1:document",
            "run:1:terminal",
        ]
    finally:
        second.close()
        first.close()
