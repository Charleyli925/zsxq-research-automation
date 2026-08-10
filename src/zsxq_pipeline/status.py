"""Read-only health projection for the pipeline state core."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .state import PipelineState


def read_status(database: str | Path) -> dict[str, Any]:
    """Return the stable JSON-ready state projection for one pipeline database."""

    with PipelineState.open(database) as state:
        return state.derive_health()


def doctor_state_only(database: str | Path) -> dict[str, Any]:
    """Validate that durable local state is structurally readable.

    Business-stage failures remain visible in ``health`` but do not make the
    database or release contract invalid.  In particular, a reviewed legacy
    import may intentionally preserve terminal blocked rows before the new
    runtime is installed.  Treating that business state as a failed doctor
    would make the documented import-before-install cutover impossible.
    """

    path = Path(database).expanduser().resolve(strict=False)
    with PipelineState.open(path) as state:
        health = state.derive_health()
        return {
            "ok": True,
            "database": str(path),
            "schema_version": state.schema_version,
            "health": health,
            "counts": {
                "documents": state.table_count("documents"),
                "schedule_cursors": state.table_count("schedule_cursors"),
                "artifacts": state.table_count("artifacts"),
                "stage_attempts": state.table_count("stage_attempts"),
                "publications": state.table_count("publications"),
                "notification_outbox": state.table_count("notification_outbox"),
            },
        }
