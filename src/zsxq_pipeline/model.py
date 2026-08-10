"""Stable model types shared by the state core and future pipeline workers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
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
    reasoning: str = ""


@dataclass(frozen=True, slots=True)
class SummaryIdentity:
    """The complete cache identity for one deterministic summary invocation.

    ``reasoning`` is deliberately part of this value.  A change in Codex
    reasoning can change the generated artifact even when the model name and
    prompt bytes do not, so it must never share a durable summary cache entry.
    An empty reasoning value is reserved for artifacts imported from the
    version-1 schema; new callers should pass their configured reasoning.
    """

    pdf_sha256: str
    extractor_version: str
    prompt_version: str
    model: str
    reasoning: str

    def __post_init__(self) -> None:
        pdf_sha256 = str(self.pdf_sha256).strip().lower()
        if len(pdf_sha256) != 64 or any(character not in "0123456789abcdef" for character in pdf_sha256):
            raise ValueError("pdf_sha256 must be a lowercase SHA256 digest")
        object.__setattr__(self, "pdf_sha256", pdf_sha256)
        for field_name in ("extractor_version", "prompt_version", "model"):
            value = str(getattr(self, field_name)).strip()
            if not value:
                raise ValueError(f"{field_name} is required")
            object.__setattr__(self, field_name, value)
        object.__setattr__(self, "reasoning", str(self.reasoning).strip())

    @property
    def canonical_json(self) -> str:
        """Stable serialization suitable for a cache-key input or audit log."""

        return json.dumps(
            {
                "extractor_version": self.extractor_version,
                "model": self.model,
                "pdf_sha256": self.pdf_sha256,
                "prompt_version": self.prompt_version,
                "reasoning": self.reasoning,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @property
    def cache_key(self) -> str:
        """A compact, unambiguous key for filesystem cache namespaces."""

        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    id: int
    summary_sha256: str
    target: str
    partition_key: str
    state: PublicationState
    remote_reference: str | None
    target_document: str = ""
    # Publication metadata is diagnostic and supports safe capacity decisions;
    # the immutable identity and state above remain the transaction authority.
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class NotificationRecord:
    id: int
    idempotency_key: str
    event: str
    status: str
    created: bool
    publication_id: int | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    attempt_count: int = 0


@dataclass(frozen=True, slots=True)
class NotificationClaim:
    """An exclusive outbox-delivery lease returned by the notification drainer."""

    id: int
    idempotency_key: str
    event: str
    payload: Mapping[str, Any]
    publication_id: int | None
    lease_token: str
    claimed_at: datetime
    lease_expires_at: datetime
    attempt_count: int


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
