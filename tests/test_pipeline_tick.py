from __future__ import annotations

import json

from zsxq_pipeline.cli import main
from zsxq_pipeline.model import ErrorCategory, Stage, StageState
from zsxq_pipeline.state import PipelineState


def _config(tmp_path):
    path = tmp_path / "pipeline.toml"
    path.write_text(
        f'''schema_version = 1

[runtime]
root = "{tmp_path / 'runtime'}"
database = "state/pipeline.sqlite3"

[schedule]
timezone = "Asia/Shanghai"
tick_budget_seconds = 10

[model]
name = "test-model"
provider = "codex"
prompt_version = "test"

[codex]
command = "/usr/bin/true"
model = "test-model"
prompt_version = "test"

[lark]
command = "/usr/bin/true"
notifications_enabled = true

[sources.manual_only]
kind = "zsxq"
state_path = "state/manual.json"

[publish_targets.daily]
kind = "lark"
target = "test"
''',
        encoding="utf-8",
    )
    return path


def test_tick_and_outbox_drain_use_the_same_configured_runtime_lock(tmp_path, capsys):
    config = _config(tmp_path)
    assert main(["tick", "--config", str(config), "--budget-seconds", "1"]) == 0
    tick = json.loads(capsys.readouterr().out)
    assert tick["status"] == "success"
    assert tick["scheduled"] == []

    assert main(["outbox", "drain", "--config", str(config), "--budget-seconds", "1"]) == 0
    outbox = json.loads(capsys.readouterr().out)
    assert outbox["notifications"] == []


def test_retry_plan_requires_an_immutable_review_then_explicit_apply(tmp_path, capsys):
    config = _config(tmp_path)
    database = tmp_path / "runtime" / "state" / "pipeline.sqlite3"
    with PipelineState.open(database) as state:
        state.migrate()
        document = state.upsert_document("manual_only", "fixture", filename="fixture.pdf")
        state.ensure_stage(document.id, Stage.PUBLISH, "publish:v1")
        claim = state.claim_due_stage(Stage.PUBLISH, "publish:v1")
        assert claim is not None
        state.fail_stage(
            claim,
            category=ErrorCategory.AUTH,
            error_code="permission_grant_failed",
            error_detail="fixture",
        )

    plan_path = tmp_path / "retry-plan.json"
    assert (
        main(
            [
                "retry",
                "plan",
                "--config",
                str(config),
                "--stage",
                "publish",
                "--workflow-version",
                "publish:v1",
                "--error-code",
                "permission_grant_failed",
                "--output",
                str(plan_path),
            ]
        )
        == 0
    )
    planned = json.loads(capsys.readouterr().out)
    assert planned["expected_count"] == 1
    assert (
        main(
            [
                "retry",
                "apply",
                "--config",
                str(config),
                "--plan",
                str(plan_path),
                "--expected-count",
                "1",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["applied"] is False
    with PipelineState.open(database) as state:
        attempt = state.get_stage_attempt(document.id, Stage.PUBLISH, "publish:v1")
        assert attempt is not None and attempt["state"] == StageState.BLOCKED_AUTH.value

    assert (
        main(
            [
                "retry",
                "apply",
                "--config",
                str(config),
                "--plan",
                str(plan_path),
                "--expected-count",
                "1",
                "--apply",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["requeued"] == 1
    with PipelineState.open(database) as state:
        attempt = state.get_stage_attempt(document.id, Stage.PUBLISH, "publish:v1")
        assert attempt is not None and attempt["state"] == StageState.QUEUED.value
