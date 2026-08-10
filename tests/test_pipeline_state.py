from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from zsxq_pipeline.model import ErrorCategory, PublicationState, Stage, StageState
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
