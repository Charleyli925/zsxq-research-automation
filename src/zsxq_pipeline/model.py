"""Stable model types shared by the state core and future pipeline workers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class Stage(StrEnum):
    """A durable unit of work for one source document."""

    DOWNLOAD = "download"
    TEXT_EXTRACT = "text_extract"
    SUMMARY = "summary"
    PUBLISH = "publish"
    NOTIFY = "notify"


class StageState(StrEnum):
    """The only state values a stage attempt may persist."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    RETRY_WAIT = "retry_wait"
    BLOCKED_AUTH = "blocked_auth"
    BLOCKED_RELEASE = "blocked_release"
    QUARANTINED = "quarantined"


class ErrorCategory(StrEnum):
    """Machine-readable retry policy inputs.

    Human-readable errors are diagnostics only.  They never decide whether a
    task will retry; the category below does.
    """

    TRANSIENT = "transient"
    AUTH = "auth"
    RELEASE_CONTRACT = "release_contract"
    CONTENT = "content"
    INVARIANT = "invariant"


class PublicationState(StrEnum):
    """A two-step remote write transaction, followed by local completion."""

    INTENT = "intent"
    REMOTE_WRITTEN = "remote_written"
    SUCCESS = "success"


class PipelineHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    BLOCKED = "blocked"


@dataclass(frozen=True, slots=True)
class StageClaim:
    """The exclusive lease returned to a worker after an atomic claim."""

    attempt_id: int
    document_id: int
    source: str
    source_file_id: str
    stage: Stage
    workflow_version: str
    lease_token: str
    claimed_at: datetime
    lease_expires_at: datetime
    attempt_count: int


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    id: int
    source: str
    source_file_id: str
    filename: str
    normalized_filename: str
    source_path: str
    artifact_id: int | None


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    id: int
    kind: str
    pdf_sha256: str | None
    content_sha256: str | None
    canonical_path: str
    extractor_version: str = ""
    prompt_version: str = ""
    model: str = ""


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    id: int
    summary_sha256: str
    target: str
    partition_key: str
    state: PublicationState
    remote_reference: str | None


@dataclass(frozen=True, slots=True)
class NotificationRecord:
    id: int
    idempotency_key: str
    event: str
    status: str
    created: bool


def as_stage(value: Stage | str) -> Stage:
    return value if isinstance(value, Stage) else Stage(str(value))


def as_stage_state(value: StageState | str) -> StageState:
    return value if isinstance(value, StageState) else StageState(str(value))


def as_error_category(value: ErrorCategory | str) -> ErrorCategory:
    return value if isinstance(value, ErrorCategory) else ErrorCategory(str(value))


def as_publication_state(value: PublicationState | str) -> PublicationState:
    return value if isinstance(value, PublicationState) else PublicationState(str(value))


def canonical_json_value(value: Any) -> Any:
    """Reject values that would make a persisted JSON diagnostic ambiguous."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [canonical_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): canonical_json_value(item) for key, item in value.items()}
    raise TypeError(f"unsupported diagnostic JSON value: {type(value)!r}")
