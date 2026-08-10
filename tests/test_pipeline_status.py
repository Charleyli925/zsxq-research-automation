from __future__ import annotations

from datetime import UTC, datetime, timedelta

from zsxq_pipeline.model import ErrorCategory, Stage
from zsxq_pipeline.state import PipelineState
from zsxq_pipeline.status import read_status


NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def test_status_derives_blocked_without_presenting_it_as_waiting(tmp_path):
    database = tmp_path / "pipeline.sqlite3"
    with PipelineState.open(database) as state:
        state.migrate()
        blocked_document = state.upsert_document("zsxq_digest", "blocked", now=NOW)
        retry_document = state.upsert_document("zsxq_digest", "retry", now=NOW)
        state.ensure_stage(blocked_document.id, Stage.PUBLISH, "v1", now=NOW)
        state.ensure_stage(retry_document.id, Stage.SUMMARY, "v1", now=NOW)
        blocked_claim = state.claim_due_stage(Stage.PUBLISH, "v1", now=NOW)
        retry_claim = state.claim_due_stage(Stage.SUMMARY, "v1", now=NOW)
        assert blocked_claim is not None and retry_claim is not None
        state.fail_stage(blocked_claim, category=ErrorCategory.AUTH, now=NOW)
        state.fail_stage(retry_claim, category=ErrorCategory.TRANSIENT, retry_at=NOW + timedelta(minutes=5), now=NOW)

        result = state.derive_health(now=NOW)
        digest = result["sources"]["zsxq_digest"]
        assert result["health"] == "blocked"
        assert digest["totals"]["blocked"] == 1
        assert digest["totals"]["retry_wait"] == 1
        assert digest["stages"]["summary"]["earliest_runnable_at"] == "2026-08-10T12:05:00Z"
        assert "waiting" not in str(result)

    reopened = read_status(database)
    assert reopened["health"] == "blocked"
    assert "_earliest_runnable_epoch" not in str(reopened)
