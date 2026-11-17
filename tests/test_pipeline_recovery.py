from datetime import datetime, timezone, timedelta
import json

from test_pipeline_tasks import config_fixture
from zsxq_pipeline.discovery import new_scan
from zsxq_pipeline.model import Stage, ErrorCategory
from zsxq_pipeline.recovery import inventory
from zsxq_pipeline.state import PipelineState
from zsxq_pipeline.download import _script_module


def test_inventory_accounts_for_every_terminal_row_and_only_plans_evidenced_collision(
    tmp_path,
):
    config = config_fixture(tmp_path)
    now = datetime.now(timezone.utc)
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        window = new_scan(
            state, config, config.sources["foreign"], now - timedelta(days=3), now
        )
        candidate = {
            "file_id": "collision",
            "filename": "report.pdf",
            "topic_url": "https://example.invalid/topic/1",
        }
        plan = {
            "window_start": (now - timedelta(days=3)).isoformat(),
            "window_end": now.isoformat(),
            "api_probe_status": "ok",
            "download_candidates": [candidate],
        }
        scanner = _script_module("scan_zsxq_download_candidates")
        plan["plan_hash"] = scanner.canonical_plan_hash(plan)
        directory = config.runtime.root / "plans/foreign"
        directory.mkdir(parents=True)
        (directory / "proof.json").write_text(json.dumps(plan))
        for identity, code, category in [
            ("collision", "staging_collision", ErrorCategory.INVARIANT),
            ("unproved", "staging_collision", ErrorCategory.INVARIANT),
            ("protected", "source_content_protected", ErrorCategory.CONTENT),
        ]:
            doc = state.upsert_document("foreign", identity, source_window_id=window)
            state.ensure_stage(doc.id, Stage.DOWNLOAD, "v")
            owned = state.claim_due_stage(Stage.DOWNLOAD, "v", document_ids=(doc.id,))
            state.fail_stage(owned, category=category, error_code=code)
        before = [
            tuple(r)
            for r in state._connection.execute(
                "SELECT * FROM stage_attempts ORDER BY id"
            )
        ]
    result = inventory(config)
    assert result["terminal_count"] == 3
    assert sum(result["classification_counts"].values()) == 3
    assert result["retry_plans"][0]["expected_count"] == 1
    with PipelineState.open(config.runtime.database) as state:
        assert [
            tuple(r)
            for r in state._connection.execute(
                "SELECT * FROM stage_attempts ORDER BY id"
            )
        ] == before
        retry = result["retry_plans"][0]
        state.apply_stage_retry_plan(
            retry["candidates"], plan_hash=retry["plan_hash"], expected_count=1
        )
        assert (
            state._connection.execute(
                "SELECT count(*) FROM stage_attempts WHERE state='queued'"
            ).fetchone()[0]
            == 1
        )
