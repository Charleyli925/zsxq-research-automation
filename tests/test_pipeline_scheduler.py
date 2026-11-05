from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from zsxq_pipeline.config import ConfigError, load_pipeline_config
from zsxq_pipeline.scheduler import PipelineScheduler
from zsxq_pipeline.state import PipelineState


def _config(tmp_path, *, timezone: str = "Asia/Shanghai", max_catchup_seconds: int = 86400):
    path = tmp_path / "pipeline.toml"
    path.write_text(
        f'''schema_version = 1

[runtime]
root = "{tmp_path / 'runtime'}"
database = "state/pipeline.sqlite3"

[schedule]
timezone = "{timezone}"
max_catchup_seconds = {max_catchup_seconds}

[model]
name = "test-model"
provider = "codex"
prompt_version = "test"

[sources.foreign]
kind = "zsxq"
state_path = "state/foreign.json"
schedule_times = ["08:00", "12:00", "16:00"]

[publish_targets.daily]
kind = "lark"
target = "test"
''',
        encoding="utf-8",
    )
    return load_pipeline_config(path)


def test_missed_slots_coalesce_from_last_successful_checkpoint(tmp_path):
    config = _config(tmp_path)
    checkpoint = datetime(2026, 8, 9, 23, 50, tzinfo=UTC)
    now = datetime(2026, 8, 10, 9, 5, tzinfo=UTC)  # 17:05 Asia/Shanghai
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        state.register_source_window("foreign", checkpoint - timedelta(hours=1), checkpoint, status="succeeded", checkpoint_eligible=True)
        scheduler = PipelineScheduler(config)
        windows = scheduler.enqueue_due_windows(state, now=now)
        assert len(windows) == 1
        window = windows[0]
        assert window.window_start == checkpoint
        assert window.window_end == now
        assert len(window.due_slots) == 3
        assert window.truncated is False
        assert state.get_schedule_cursor("foreign") == window.due_slots[-1].astimezone(UTC)
        health = state.derive_health(now=now)
        assert health["sources"]["foreign"]["windows"]["latest"]["status"] == "scheduled"
        assert health["sources"]["foreign"]["schedule"]["truncated"] is False
        # Repeating the tick cannot create a duplicate window for any of the
        # three already durable source slots.
        assert scheduler.enqueue_due_windows(state, now=now) == ()



@pytest.mark.parametrize("has_schedule_cursor", [False, True])
def test_week_offline_resumes_the_full_checkpoint_gap_after_restart(tmp_path, has_schedule_cursor):
    config = _config(tmp_path, max_catchup_seconds=3 * 86400)
    checkpoint = datetime(2026, 8, 3, 8, 0, tzinfo=UTC)  # 16:00 Asia/Shanghai
    now = datetime(2026, 8, 10, 9, 5, tzinfo=UTC)
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        if has_schedule_cursor:
            state.schedule_source_window(
                "foreign", checkpoint - timedelta(hours=4), checkpoint,
                due_cursor=checkpoint, truncated=False,
            )
        state.register_source_window(
            "foreign", checkpoint - timedelta(hours=4), checkpoint,
            status="succeeded", checkpoint_eligible=True,
        )

    # Reopen state as the login-time worker does after a machine restart.
    with PipelineState.open(config.runtime.database) as state:
        windows = PipelineScheduler(config).enqueue_due_windows(state, now=now)
        assert len(windows) == 1
        window = windows[0]
        assert window.window_start == checkpoint
        assert window.window_end == now
        assert len(window.due_slots) == 21
        assert window.truncated is False
        assert state.latest_source_checkpoint("foreign") == checkpoint
        assert state.get_schedule_cursor("foreign") == window.due_slots[-1]

    # The persisted window survives a second restart without duplicating work.
    with PipelineState.open(config.runtime.database) as state:
        assert PipelineScheduler(config).enqueue_due_windows(state, now=now) == ()
        assert len(state.list_source_windows()) == 2
        state.register_source_window(
            "foreign", window.window_start, window.window_end,
            status="succeeded", checkpoint_eligible=True,
        )
        assert state.latest_source_checkpoint("foreign") == now


def test_bootstrap_catchup_reports_explicit_truncation(tmp_path):
    config = _config(tmp_path, max_catchup_seconds=3600)
    now = datetime(2026, 8, 10, 8, 10, tzinfo=UTC)  # 16:10 Asia/Shanghai
    with PipelineState.open(config.runtime.database) as state:
        state.migrate()
        windows = PipelineScheduler(config).enqueue_due_windows(state, now=now)
    assert len(windows) == 1
    assert windows[0].truncated is True
    assert windows[0].window_start == now - timedelta(hours=1)


def test_invalid_timezone_is_rejected_before_schedule_fallback(tmp_path):
    with pytest.raises(ConfigError, match="IANA timezone"):
        _config(tmp_path, timezone="not/a-timezone")
