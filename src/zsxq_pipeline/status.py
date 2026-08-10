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
    """Validate only the durable local state; no network or runtime probes."""

    path = Path(database).expanduser().resolve(strict=False)
    with PipelineState.open(path) as state:
        health = state.derive_health()
        return {
            "ok": health["health"] != "blocked",
            "database": str(path),
            "schema_version": state.schema_version,
            "health": health,
            "counts": {
                "documents": state.table_count("documents"),
                "artifacts": state.table_count("artifacts"),
                "stage_attempts": state.table_count("stage_attempts"),
                "publications": state.table_count("publications"),
                "notification_outbox": state.table_count("notification_outbox"),
            },
        }
