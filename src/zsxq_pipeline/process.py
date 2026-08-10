"""The direct-Codex digest worker and its compatibility runtime boundary.

This module is deliberately the only orchestration layer that knows about
extraction, summary artifacts, direct Lark publication, and notification
delivery.  It does not invoke OpenClaw, load an agent registry, copy an auth
profile, or inspect a model session.  The durable SQLite state remains the
authority; JSON files in a task directory are compatibility projections for
the existing cron wrapper and human operators.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from .config import PipelineConfig
from .extract import (
    ExtractionBatchResult,
    ExtractionContractError,
    ExtractionError,
    ExtractionItem,
    ExtractorAdapter,
    record_extracted_text_artifact,
    sha256_file,
    validate_extracted_manifest,
)
from .lark import LarkCliConfig, LarkNotifier, LarkPublisher
from .lock import runtime_lock
from .model import ErrorCategory, PublicationState, Stage, StageClaim, StageState, SummaryIdentity
from .notify import (
    NotificationDelivery,
    NotificationDrainer,
    enqueue_document_notification,
    enqueue_terminal_notification,
    export_notification_audit,
    render_document_notice,
)
from .providers.codex import (
    CodexExecutionError,
    CodexProviderConfig,
    CodexSummaryInput,
    CodexSummaryProvider,
    CodexSummaryRequest,
    CodexTimeoutError,
    SummaryOutputValidationError,
    summary_prompt_path,
    summary_system_prompt_path,
)
from .publish import (
    PublicationError,
    SummaryForPublish,
    build_publication_groups,
    publish_group,
    resolve_same_day_capacity_target,
)
from .state import PipelineState
from .sidecars import ArtifactSidecars
from .summary import (
    PersistedSummary,
    SummaryCacheCorruptionError,
    SummaryError,
    SummaryJob,
    SummaryModelFailure,
    SummaryStore,
    build_summary_inputs,
    identities_for_manifest,
    materialize_summary_cache,
    persist_summary_batch,
    prompt_version_hash,
    record_summary_artifact,
    run_summary_jobs,
)


class ProcessError(RuntimeError):
    """A local orchestration request cannot safely be completed."""


class ProcessBusyError(ProcessError):
    """Another digest process still holds the runtime lock."""


# A direct model, extraction, or document-write failure may retry once after a
# bounded cooldown.  Further automatic attempts would repeatedly consume the
# same external capacity without new evidence, so they become an operator-
# visible release block instead of an unbounded cron loop.
_TRANSIENT_RETRY_DELAY = timedelta(minutes=5)
_MAX_TRANSIENT_STAGE_ATTEMPTS = 2


class SummaryProvider(Protocol):
    def summarize(self, request: CodexSummaryRequest) -> Any: ...

    def capability_preflight(self) -> Any: ...


class Publisher(Protocol):
    def capability_preflight(self) -> Any: ...


class Notifier(Protocol):
    def capability_preflight(self) -> Any: ...


def _absolute(value: str | Path) -> Path:
    return Path(value).expanduser().resolve(strict=False)


def _inside(root: Path, value: str | Path, *, field_name: str) -> Path:
    candidate = Path(value).expanduser()
    resolved = (root / candidate).resolve(strict=False) if not candidate.is_absolute() else candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ProcessError(f"{field_name} must remain inside runtime_root") from exc
    return resolved


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ProcessError(f"{name} must be an integer") from exc
    if value < minimum or (maximum is not None and value > maximum):
        range_text = f"{minimum}..{maximum}" if maximum is not None else f">={minimum}"
        raise ProcessError(f"{name} must be in range {range_text}")
    return value


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "true" if default else "false").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ProcessError(f"{name} must be true or false")


def _read_text(path: Path, *, field_name: str) -> str:
    try:
        value = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProcessError(f"unable to read {field_name}: {path}") from exc
    if not value.strip():
        raise ProcessError(f"{field_name} is empty: {path}")
    return value


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(value, encoding="utf-8")
        temporary.replace(path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class ProcessConfig:
    """All direct-worker settings, with runtime-owned paths kept contained."""

    runtime_root: Path
    database: Path
    source: str
    target: str
    target_document: str
    extractor_version: str
    codex_command: str
    codex_model: str
    codex_reasoning: str
    codex_timeout_seconds: int
    codex_work_root: Path
    prompt_path: Path
    system_prompt_path: Path
    text_cache_root: Path
    summary_cache_root: Path
    work_root: Path
    batch_path: Path
    watch_state_path: Path
    result_path: Path
    result_markdown_path: Path
    run_status_path: Path
    usage_path: Path
    quarantine_path: Path
    notification_audit_path: Path
    research_library_root: Path | None
    obsidian_vault_root: Path | None
    lark_command: str
    lark_config_dir: Path | None
    lark_timeout_seconds: int
    lark_parent_position: str
    target_chat_id: str
    notifications_enabled: bool
    summary_max_workers: int = 2
    doc_group_size: int = 10
    doc_group_threshold: int = 15
    watch_root: Path | None = None
    watch_extra_roots: tuple[Path, ...] = ()
    max_files_per_document: int = 20
    quiet_window_minutes: int = 15
    preflight_path: Path | None = None

    def __post_init__(self) -> None:
        root = _absolute(self.runtime_root)
        object.__setattr__(self, "runtime_root", root)
        for name in (
            "database",
            "codex_work_root",
            "text_cache_root",
            "summary_cache_root",
            "work_root",
            "batch_path",
            "watch_state_path",
            "result_path",
            "result_markdown_path",
            "run_status_path",
            "usage_path",
            "quarantine_path",
            "notification_audit_path",
        ):
            object.__setattr__(self, name, _inside(root, getattr(self, name), field_name=name))
        preflight_path = self.preflight_path if self.preflight_path is not None else Path("last_preflight.json")
        object.__setattr__(self, "preflight_path", _inside(root, preflight_path, field_name="preflight_path"))
        # Prompt assets are copied as plain text into each isolated Codex work
        # directory.  The built-in package assets live outside a mutable task
        # runtime, so they cannot be constrained to runtime_root like caches
        # and state files.  Explicit wrapper paths remain runtime-rooted in
        # from_environment below.
        object.__setattr__(self, "prompt_path", _absolute(self.prompt_path))
        object.__setattr__(self, "system_prompt_path", _absolute(self.system_prompt_path))
        if self.watch_root is not None:
            object.__setattr__(self, "watch_root", _absolute(self.watch_root))
        object.__setattr__(self, "watch_extra_roots", tuple(_absolute(path) for path in self.watch_extra_roots))
        if self.lark_config_dir is not None:
            object.__setattr__(self, "lark_config_dir", _absolute(self.lark_config_dir))
        if self.research_library_root is not None:
            object.__setattr__(self, "research_library_root", _absolute(self.research_library_root))
        if self.obsidian_vault_root is not None:
            object.__setattr__(self, "obsidian_vault_root", _absolute(self.obsidian_vault_root))
        for field_name in ("source", "target", "extractor_version", "codex_command", "codex_model", "lark_command"):
            if not str(getattr(self, field_name)).strip():
                raise ProcessError(f"{field_name} is required")
        if not 1 <= int(self.summary_max_workers) <= 2:
            raise ProcessError("summary_max_workers must be between 1 and 2")
        if int(self.doc_group_size) < 1 or int(self.doc_group_threshold) < 0:
            raise ProcessError("document grouping values are invalid")
        if int(self.max_files_per_document) < int(self.doc_group_size):
            raise ProcessError("max_files_per_document must be at least doc_group_size")
        if int(self.quiet_window_minutes) < 0:
            raise ProcessError("quiet_window_minutes must be non-negative")

    @classmethod
    def from_environment(cls, runtime_root: str | Path) -> "ProcessConfig":
        """Build the compatibility configuration sourced by the thin shell wrapper."""

        root = _absolute(runtime_root)

        def runtime_path(name: str, default: str) -> Path:
            return _inside(root, os.environ.get(name, default), field_name=name)

        extras = tuple(
            _absolute(value)
            for value in os.environ.get("WATCH_EXTRA_ROOTS", "").split(":")
            if value.strip()
        )
        watch_root_text = os.environ.get("WATCH_ROOT", "").strip()
        prompt_candidate = os.environ.get("CODEX_PROMPT_PATH", "").strip()
        system_candidate = os.environ.get("CODEX_SYSTEM_PROMPT_PATH", "").strip()
        codex_model = os.environ.get("CODEX_MODEL", "").strip()
        if not codex_model:
            raise ProcessError("CODEX_MODEL is required; set it to the already approved summary model")
        return cls(
            runtime_root=root,
            database=runtime_path("PIPELINE_DATABASE", "state/pipeline.sqlite3"),
            source=os.environ.get("PIPELINE_SOURCE", "zsxq_digest").strip() or "zsxq_digest",
            target=os.environ.get("PUBLISH_TARGET", "lark:zsxq_digest").strip() or "lark:zsxq_digest",
            target_document=os.environ.get("PUBLISH_TARGET_DOCUMENT", "").strip(),
            extractor_version=os.environ.get("EXTRACTOR_VERSION", "ocr-geometry-v2").strip() or "ocr-geometry-v2",
            codex_command=os.environ.get("CODEX_BIN", "codex").strip() or "codex",
            codex_model=codex_model,
            codex_reasoning=(
                os.environ.get("CODEX_REASONING", "").strip()
                or os.environ.get("SUMMARY_AGENT_THINKING", "").strip()
                or "medium"
            ),
            codex_timeout_seconds=_env_int("CODEX_TIMEOUT_SECONDS", 600),
            codex_work_root=runtime_path("CODEX_WORK_ROOT", "work/codex"),
            prompt_path=(
                _inside(root, prompt_candidate, field_name="CODEX_PROMPT_PATH")
                if prompt_candidate
                else summary_prompt_path()
            ),
            system_prompt_path=(
                _inside(root, system_candidate, field_name="CODEX_SYSTEM_PROMPT_PATH")
                if system_candidate
                else summary_system_prompt_path()
            ),
            text_cache_root=runtime_path("TEXT_CACHE_DIR", "text_cache"),
            summary_cache_root=runtime_path("SUMMARY_CACHE_DIR", "summary_cache"),
            work_root=runtime_path("PIPELINE_WORK_ROOT", "work"),
            batch_path=runtime_path("BATCH_JSON", "pending_batch.json"),
            watch_state_path=runtime_path("STATE_FILE", "watch_state.json"),
            result_path=runtime_path("RESULT_JSON", "last_result.json"),
            result_markdown_path=runtime_path("RESULT_MD", "last_result.md"),
            run_status_path=runtime_path("RUN_STATUS_JSON", "run_status.json"),
            usage_path=runtime_path("USAGE_JSON", "last_usage_summary.json"),
            quarantine_path=runtime_path("QUARANTINE_JSON", "quarantine.json"),
            notification_audit_path=runtime_path("NOTIFICATION_JSONL", "notification_messages.jsonl"),
            research_library_root=(
                _absolute(os.environ["RESEARCH_LIBRARY_ROOT"])
                if os.environ.get("RESEARCH_LIBRARY_ROOT", "").strip()
                else None
            ),
            obsidian_vault_root=(
                _absolute(os.environ["OBSIDIAN_VAULT_ROOT"])
                if os.environ.get("OBSIDIAN_VAULT_ROOT", "").strip()
                else None
            ),
            lark_command=os.environ.get("LARK_CLI_BIN", "lark-cli").strip() or "lark-cli",
            lark_config_dir=(
                _absolute(os.environ["LARKSUITE_CLI_CONFIG_DIR"])
                if os.environ.get("LARKSUITE_CLI_CONFIG_DIR", "").strip()
                else None
            ),
            lark_timeout_seconds=_env_int("LARK_CLI_TIMEOUT_SECONDS", 90),
            lark_parent_position=os.environ.get("PUBLISH_LARK_CLI_PARENT_POSITION", "my_library").strip() or "my_library",
            target_chat_id=os.environ.get("TARGET_CHAT_ID", "").strip(),
            notifications_enabled=_env_bool("LARK_CLI_NOTIFICATIONS", True),
            summary_max_workers=_env_int("SUMMARY_WORKER_COUNT", 2, maximum=2),
            doc_group_size=_env_int("DOC_GROUP_SIZE", 10),
            doc_group_threshold=_env_int("DOC_GROUP_THRESHOLD", 15, minimum=0),
            watch_root=_absolute(watch_root_text) if watch_root_text else None,
            watch_extra_roots=extras,
            max_files_per_document=_env_int(
                "MAX_FILES_PER_DOCUMENT",
                _env_int("PUBLISH_MAX_FILES_PER_DOC", 20),
            ),
            quiet_window_minutes=_env_int("QUIET_WINDOW_MINUTES", 15, minimum=0),
            preflight_path=runtime_path("PREFLIGHT_JSON", "last_preflight.json"),
        )

    @classmethod
    def from_pipeline_config(cls, config: PipelineConfig) -> "ProcessConfig":
        """Adapt the typed TOML contract without introducing a second worker path."""

        target = next(iter(config.publish_targets.values()), None)
        if target is None:
            raise ProcessError("a direct process configuration needs one publish target")
        root = config.runtime.root
        prompt = config.codex.prompt_path or summary_prompt_path()
        system = config.codex.system_prompt_path or summary_system_prompt_path()
        return cls(
            runtime_root=root,
            database=config.runtime.database,
            source=next(iter(config.sources), "zsxq_digest"),
            target=target.target,
            target_document=target.target_document,
            extractor_version=config.pipeline.extractor_version or "ocr-geometry-v2",
            codex_command=config.codex.command,
            codex_model=config.codex.model,
            codex_reasoning=config.codex.reasoning,
            codex_timeout_seconds=config.codex.timeout_seconds,
            codex_work_root=config.codex.work_root,
            prompt_path=prompt,
            system_prompt_path=system,
            text_cache_root=_inside(root, "text_cache", field_name="text_cache_root"),
            summary_cache_root=_inside(root, "summary_cache", field_name="summary_cache_root"),
            work_root=_inside(root, "work", field_name="work_root"),
            batch_path=_inside(root, "pending_batch.json", field_name="batch_path"),
            watch_state_path=_inside(root, "watch_state.json", field_name="watch_state_path"),
            result_path=_inside(root, "last_result.json", field_name="result_path"),
            result_markdown_path=_inside(root, "last_result.md", field_name="result_markdown_path"),
            run_status_path=_inside(root, "run_status.json", field_name="run_status_path"),
            usage_path=_inside(root, "last_usage_summary.json", field_name="usage_path"),
            quarantine_path=_inside(root, "quarantine.json", field_name="quarantine_path"),
            notification_audit_path=_inside(root, "notification_messages.jsonl", field_name="notification_audit_path"),
            research_library_root=None,
            obsidian_vault_root=None,
            lark_command=config.lark.command,
            lark_config_dir=config.lark.config_dir,
            lark_timeout_seconds=config.lark.timeout_seconds,
            lark_parent_position=config.lark.parent_position,
            target_chat_id=config.lark.target_chat_id,
            notifications_enabled=config.lark.notifications_enabled,
            summary_max_workers=config.pipeline.summary_max_workers,
            doc_group_size=config.pipeline.doc_group_size,
            doc_group_threshold=config.pipeline.doc_group_threshold,
            max_files_per_document=config.pipeline.max_files_per_document,
        )


@dataclass(frozen=True, slots=True)
class ProcessRequest:
    """One CLI request, including explicitly supplied PDFs or a batch manifest."""

    files: tuple[Path, ...] = ()
    folders: tuple[Path, ...] = ()
    batch_file: Path | None = None
    dry_run: bool = False
    summary_only: bool = False
    no_notify: bool = False
    # The unified worker queues document notices during publication, then
    # drains the shared outbox once under its own bounded quota.  Existing
    # standalone ``process`` callers retain immediate draining by default.
    defer_notification_drain: bool = False
    preflight_only: bool = False
    include_existing: bool = False

    @property
    def local_only(self) -> bool:
        return self.dry_run or self.summary_only


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    status: str
    run_id: str
    files_seen: int = 0
    extracted: int = 0
    summarized: int = 0
    cache_hits: int = 0
    published: int = 0
    quarantined: int = 0
    # This is meaningful only for a scanner-generated batch.  It is derived
    # from durable document stages rather than the user-facing run label, so a
    # content quarantine can be acknowledged without replaying good PDFs.
    ack_eligible: bool = False
    failures: tuple[str, ...] = ()
    notifications: tuple[NotificationDelivery, ...] = ()
    preflight: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "run_id": self.run_id,
            "files_seen": self.files_seen,
            "extracted": self.extracted,
            "summarized": self.summarized,
            "cache_hits": self.cache_hits,
            "published": self.published,
            "quarantined": self.quarantined,
            "ack_eligible": self.ack_eligible,
            "failures": list(self.failures),
            "notifications": [
                {
                    "idempotency_key": item.idempotency_key,
                    "event": item.event,
                    "status": item.status,
                    "deferred": item.deferred,
                    "error": item.error,
                }
                for item in self.notifications
            ],
            "preflight": dict(self.preflight),
        }


def _same_path(left: str | Path, right: str | Path) -> bool:
    return str(Path(left).expanduser().resolve(strict=False)) == str(Path(right).expanduser().resolve(strict=False))


def _failure_category(error: Exception) -> ErrorCategory:
    if isinstance(error, CodexTimeoutError):
        return ErrorCategory.TRANSIENT
    if isinstance(error, SummaryOutputValidationError):
        return ErrorCategory.INVARIANT
    if isinstance(error, (SummaryCacheCorruptionError,)):
        return ErrorCategory.INVARIANT
    if isinstance(error, ExtractionContractError):
        return ErrorCategory.RELEASE_CONTRACT
    text = str(error).lower()
    if any(token in text for token in ("auth", "unauthorized", "forbidden", "token", "keychain", "permission")):
        return ErrorCategory.AUTH
    if isinstance(error, (CodexExecutionError, ExtractionError, SummaryError, PublicationError)):
        return ErrorCategory.TRANSIENT
    return ErrorCategory.INVARIANT


class DigestProcessor:
    """Stateful worker that preserves artifact and remote-write boundaries."""

    def __init__(
        self,
        config: ProcessConfig,
        *,
        extractor: ExtractorAdapter | Any | None = None,
        provider: SummaryProvider | None = None,
        publisher: Publisher | None = None,
        notifier: Notifier | None = None,
        sidecars: ArtifactSidecars | Any | None = None,
        clock: callable = _utc_now,
    ) -> None:
        self.config = config
        self.clock = clock
        self.extractor = extractor or ExtractorAdapter(
            python_executable=os.environ.get("PYTHON_BIN", sys.executable),
            timeout_seconds=_env_int("TEXT_EXTRACT_TIMEOUT_SECONDS", 600),
            environment={"TEXT_EXTRACT_CACHE_DIR": str(config.text_cache_root)},
        )
        self.provider = provider or CodexSummaryProvider(
            CodexProviderConfig(
                command=config.codex_command,
                model=config.codex_model,
                reasoning=config.codex_reasoning,
                work_root=config.codex_work_root,
                timeout_seconds=config.codex_timeout_seconds,
            )
        )
        lark_config = LarkCliConfig(
            command=config.lark_command,
            config_dir=config.lark_config_dir,
            timeout_seconds=config.lark_timeout_seconds,
            parent_position=config.lark_parent_position,
        )
        self.publisher = publisher or LarkPublisher(lark_config)
        self.notifier = notifier or LarkNotifier(lark_config)
        self.sidecars = sidecars or ArtifactSidecars(
            library_root=config.research_library_root,
            vault_root=config.obsidian_vault_root,
            work_root=config.work_root,
            python_executable=os.environ.get("PYTHON_BIN", sys.executable),
        )
        self.summary_store = SummaryStore(config.summary_cache_root)
        self._prompt, self._system_prompt = self._load_prompts()
        self.prompt_version = prompt_version_hash(self._prompt, self._system_prompt)
        self._usage: list[Mapping[str, Any]] = []

    def _load_prompts(self) -> tuple[str, str]:
        prompt_path = self.config.prompt_path if self.config.prompt_path.is_file() else summary_prompt_path()
        system_path = self.config.system_prompt_path if self.config.system_prompt_path.is_file() else summary_system_prompt_path()
        return _read_text(prompt_path, field_name="summary prompt"), _read_text(system_path, field_name="summary system prompt")

    def preflight(self) -> Mapping[str, Any]:
        """Run bounded capability checks only; no summary or Lark write occurs."""

        checks: dict[str, Any] = {}
        checks["extractor"] = self.extractor.preflight()
        checks["codex"] = self.provider.capability_preflight()
        checks["lark_documents"] = self.publisher.capability_preflight()
        checks["lark_notifications"] = self.notifier.capability_preflight()
        return {"ok": True, "checks": {name: _safe_result(value) for name, value in checks.items()}}

    def run(self, request: ProcessRequest, *, acquire_lock: bool = True) -> ProcessOutcome:
        """Run one bounded batch and always refresh compatibility result files."""

        run_id = f"digest-{self.clock().strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:10]}"
        self.config.runtime_root.mkdir(parents=True, exist_ok=True)
        if acquire_lock:
            with runtime_lock(self.config.runtime_root) as acquired:
                if not acquired:
                    outcome = ProcessOutcome("busy", run_id, failures=("another pipeline worker holds the runtime lock",))
                    self._write_outcome(outcome, phase="busy")
                    return outcome
                return self._run_locked(request, run_id=run_id)
        return self._run_locked(request, run_id=run_id)

    def _run_locked(self, request: ProcessRequest, *, run_id: str) -> ProcessOutcome:
        """Perform the work after the shared runtime lock is held by caller."""

        try:
            self._write_status(run_id, "running", "starting")
            if request.preflight_only:
                preflight = self.preflight()
                _write_json(self.config.preflight_path, preflight)
                outcome = ProcessOutcome("success", run_id, preflight=preflight)
                self._write_outcome(outcome, phase="preflight")
                return outcome
            batch_path, scanner_batch = self._materialize_batch(request)
            batch = self._load_batch(batch_path)
            files = batch["files"]
            if not files:
                notifications = self._drain_existing_notifications(request, run_id=run_id)
                outcome = ProcessOutcome("success", run_id, files_seen=0, notifications=notifications)
                self._write_outcome(outcome, phase="idle")
                return outcome
            self._write_status(run_id, "running", "extract")
            outcome = self._run_batch(run_id, batch_path, batch, request)
            if scanner_batch and outcome.ack_eligible and not request.local_only:
                self._ack_scanner_batch()
            self._write_outcome(outcome, phase="complete")
            return outcome
        except Exception as exc:
            outcome = ProcessOutcome("failed", run_id, failures=(str(exc) or type(exc).__name__,))
            self._write_outcome(outcome, phase="failed")
            return outcome

    def _run_batch(
        self,
        run_id: str,
        batch_path: Path,
        batch: dict[str, Any],
        request: ProcessRequest,
    ) -> ProcessOutcome:
        failures: list[str] = []
        quarantined = 0
        notifications: tuple[NotificationDelivery, ...] = ()
        ack_eligible = False
        with PipelineState.open(self.config.database) as state:
            state.migrate()
            documents = self._register_documents(state, batch)
            extracted, extraction_failures, extraction_quarantined = self._extract(state, batch_path, batch, documents)
            failures.extend(extraction_failures)
            quarantined += extraction_quarantined
            if extracted:
                self._write_status(run_id, "running", "summary")
            summaries, cache_hits, summary_failures = self._summarize(state, batch, extracted, documents)
            failures.extend(summary_failures)
            _write_json(batch_path, batch)
            published = 0
            publication_records: tuple[tuple[Any, Any], ...] = ()
            if summaries and not request.local_only:
                self._write_status(run_id, "running", "publish")
                published, publish_failures, publication_records = self._publish(
                    state,
                    batch,
                    summaries,
                    documents,
                    run_id=run_id,
                    queue_notifications=self.config.notifications_enabled and not request.no_notify,
                )
                failures.extend(publish_failures)
                failures.extend(self._archive_published_sidecars(batch, publication_records))
                _write_json(batch_path, batch)
                if publication_records and self.config.notifications_enabled and not request.no_notify:
                    if failures and self.config.target_chat_id:
                        enqueue_terminal_notification(
                            state,
                            event="run_partial",
                            chat_id=self.config.target_chat_id,
                            markdown="本轮知识星球研报总结已部分完成；请查看已发布文档和本地运行结果。",
                            scope_key=run_id,
                        )
            if not request.local_only:
                if self.config.notifications_enabled and not request.no_notify and not request.defer_notification_drain:
                    notifications = NotificationDrainer(state, self.notifier, clock=self.clock).drain()
                export_notification_audit(state, self.config.notification_audit_path)
                ack_eligible = self._scanner_ack_eligible(state, batch, documents)
        _write_json(self.config.usage_path, {"run_id": run_id, "usage": [dict(value) for value in self._usage]})
        status = "success" if not failures else ("partial" if (summaries or quarantined) else "failed")
        return ProcessOutcome(
            status,
            run_id,
            files_seen=len(batch["files"]),
            extracted=len(extracted),
            summarized=len(summaries),
            cache_hits=cache_hits,
            published=published,
            quarantined=quarantined,
            ack_eligible=ack_eligible,
            failures=tuple(failures),
            notifications=notifications,
        )

    def _drain_existing_notifications(
        self,
        request: ProcessRequest,
        *,
        run_id: str,
    ) -> tuple[NotificationDelivery, ...]:
        """Drain the independent outbox even when the scanner has no PDFs."""

        if request.local_only or request.defer_notification_drain:
            return ()
        with PipelineState.open(self.config.database) as state:
            state.migrate()
            deliveries = (
                NotificationDrainer(state, self.notifier, clock=self.clock).drain()
                if self.config.notifications_enabled and not request.no_notify
                else ()
            )
            export_notification_audit(state, self.config.notification_audit_path)
        _write_json(self.config.usage_path, {"run_id": run_id, "usage": []})
        return deliveries

    def _materialize_batch(self, request: ProcessRequest) -> tuple[Path, bool]:
        if request.batch_file is not None:
            payload = self._load_batch(_absolute(request.batch_file))
            _write_json(self.config.batch_path, payload)
            return self.config.batch_path, False
        explicit = self._explicit_files(request.files, request.folders)
        if explicit:
            payload = self._batch_for_files(explicit)
            _write_json(self.config.batch_path, payload)
            return self.config.batch_path, False
        self._scan_batch(include_existing=request.include_existing)
        return self.config.batch_path, True

    def _explicit_files(self, files: Iterable[Path], folders: Iterable[Path]) -> tuple[Path, ...]:
        candidates = [_absolute(path) for path in files]
        for folder in folders:
            root = _absolute(folder)
            if not root.is_dir():
                raise ProcessError(f"folder does not exist: {root}")
            candidates.extend(path.resolve() for path in root.rglob("*.pdf") if path.is_file())
        unique: dict[str, Path] = {}
        for path in candidates:
            if not path.is_file():
                raise ProcessError(f"PDF does not exist: {path}")
            if path.suffix.lower() != ".pdf":
                raise ProcessError(f"not a PDF: {path}")
            unique[str(path)] = path
        return tuple(unique[key] for key in sorted(unique))

    def _batch_for_files(self, files: Sequence[Path]) -> dict[str, Any]:
        generated = self.clock().isoformat()
        return {
            "generated_at": generated,
            "root": "",
            "new_pdf_count": len(files),
            "files": [
                {
                    "path": str(path),
                    "filename": path.name,
                    "scan_root": str(path.parent),
                    "pdf_sha256": sha256_file(path),
                    "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
                    "report_id": "",
                    "title": "",
                    "batch_id": "manual",
                }
                for path in files
            ],
        }

    def _scan_batch(self, *, include_existing: bool) -> None:
        if self.config.watch_root is None:
            raise ProcessError("provide --file, --folder, --batch-file, or configure WATCH_ROOT")
        scanner = Path(__file__).resolve().parents[2] / "scripts" / "scan_new_zsxq_pdfs.py"
        if not scanner.is_file():
            raise ProcessError(f"scanner is unavailable: {scanner}")
        argv = [
            sys.executable,
            str(scanner),
            "--root",
            str(self.config.watch_root),
            "--state-file",
            str(self.config.watch_state_path),
            "--batch-file",
            str(self.config.batch_path),
            "--quiet-window-minutes",
            str(self.config.quiet_window_minutes),
        ]
        for root in self.config.watch_extra_roots:
            argv.extend(("--extra-root", str(root)))
        if include_existing:
            argv.append("--include-existing")
        completed = subprocess.run(argv, capture_output=True, text=True, check=False, shell=False)
        if completed.returncode != 0:
            raise ProcessError(f"scanner failed: {(completed.stderr or completed.stdout).strip()[:800]}")

    def _ack_scanner_batch(self) -> None:
        if self.config.watch_root is None:
            return
        scanner = Path(__file__).resolve().parents[2] / "scripts" / "scan_new_zsxq_pdfs.py"
        argv = [
            sys.executable,
            str(scanner),
            "--root",
            str(self.config.watch_root),
            "--state-file",
            str(self.config.watch_state_path),
            "--batch-file",
            str(self.config.batch_path),
            "--ack-batch",
        ]
        for root in self.config.watch_extra_roots:
            argv.extend(("--extra-root", str(root)))
        completed = subprocess.run(argv, capture_output=True, text=True, check=False, shell=False)
        if completed.returncode != 0:
            raise ProcessError(f"scanner acknowledgement failed: {(completed.stderr or completed.stdout).strip()[:800]}")

    def _load_batch(self, path: Path) -> dict[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProcessError(f"batch manifest is not valid JSON: {path}") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("files"), list):
            raise ProcessError("batch manifest must contain a files list")
        files: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, value in enumerate(raw["files"]):
            if not isinstance(value, Mapping):
                raise ProcessError(f"batch file {index} must be an object")
            item = dict(value)
            source_path = _absolute(str(item.get("path", "")))
            if not source_path.is_file():
                raise ProcessError(f"batch PDF is missing: {source_path}")
            key = str(source_path)
            if key in seen:
                raise ProcessError(f"batch repeats PDF path: {source_path}")
            seen.add(key)
            item["path"] = key
            item["filename"] = str(item.get("filename") or source_path.name).strip() or source_path.name
            digest = str(item.get("pdf_sha256", "")).strip().lower()
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                digest = sha256_file(source_path)
            item["pdf_sha256"] = digest
            item.setdefault("scan_root", str(source_path.parent))
            item.setdefault("modified_at", datetime.fromtimestamp(source_path.stat().st_mtime, tz=timezone.utc).isoformat())
            files.append(item)
        raw["files"] = files
        raw["new_pdf_count"] = len(files)
        raw.setdefault("generated_at", self.clock().isoformat())
        return raw

    def _register_documents(self, state: PipelineState, batch: Mapping[str, Any]) -> dict[str, Any]:
        documents: dict[str, Any] = {}
        for item in batch["files"]:
            assert isinstance(item, Mapping)
            path = str(item["path"])
            source = str(item.get("source") or self.config.source).strip()
            if source != self.config.source:
                raise ProcessError("one processing batch must contain exactly one logical source")
            source_file_id = str(item.get("source_file_id") or f"pdf:{item['pdf_sha256']}").strip()
            source_window_id = item.get("source_window_id")
            if source_window_id is not None:
                try:
                    source_window_id = int(source_window_id)
                except (TypeError, ValueError) as exc:
                    raise ProcessError("source_window_id must be an integer when supplied") from exc
            documents[path] = state.upsert_document(
                source,
                source_file_id,
                filename=str(item["filename"]),
                source_path=path,
                source_window_id=source_window_id,
            )
        return documents

    def _scanner_ack_eligible(
        self,
        state: PipelineState,
        batch: Mapping[str, Any],
        documents: Mapping[str, Any],
    ) -> bool:
        """Return whether every scanned PDF reached a durable terminal boundary.

        A scanner acknowledgement is intentionally all-or-nothing.  It is safe
        only when each input either has a successful publication stage or was
        explicitly quarantined for a content problem.  Notification/sidecar
        retries do not affect this decision because they are downstream
        projections and never revoke publication truth.
        """

        items = tuple(item for item in batch.get("files", ()) if isinstance(item, Mapping))
        if not items:
            return False
        extraction_workflow = f"extract:{self.config.extractor_version}"
        publish_workflow = f"publish:{self.config.target}:{self.config.target_document or 'new'}"
        for item in items:
            path = str(item.get("path", ""))
            document = documents.get(path)
            if document is None:
                return False
            extraction = state.get_stage_attempt(document.id, Stage.TEXT_EXTRACT, extraction_workflow)
            if (
                extraction is not None
                and extraction.get("state") == StageState.QUARANTINED.value
                and extraction.get("error_category") == ErrorCategory.CONTENT.value
            ):
                continue
            publish = state.get_stage_attempt(document.id, Stage.PUBLISH, publish_workflow)
            if publish is None or publish.get("state") != StageState.SUCCEEDED.value:
                return False
        return True

    def _claims(
        self,
        state: PipelineState,
        *,
        documents: Mapping[str, Any],
        stage: Stage,
        workflow_version: str,
    ) -> dict[str, StageClaim]:
        wanted_by_id: dict[int, str] = {}
        for path, document in documents.items():
            existing = state.get_stage_attempt(document.id, stage, workflow_version)
            if existing is not None and existing["state"] == StageState.SUCCEEDED.value:
                continue
            state.ensure_stage(document.id, stage, workflow_version)
            wanted_by_id[document.id] = path
        claims: dict[str, StageClaim] = {}
        while wanted_by_id:
            claim = state.claim_due_stage(
                stage,
                workflow_version,
                document_ids=tuple(wanted_by_id),
                now=self.clock(),
            )
            if claim is None:
                break
            path = wanted_by_id.pop(claim.document_id, None)
            if path is None:
                # The state query is document-scoped.  If an implementation
                # ever violates that contract, leave the foreign row alone
                # rather than turning another batch into blocked_release.
                continue
            claims[path] = claim
        return claims

    def _fail_stage(
        self,
        state: PipelineState,
        claim: StageClaim,
        *,
        category: ErrorCategory,
        error_code: str,
        error_detail: str,
    ) -> StageState:
        """Persist a bounded retry policy for side-effecting pipeline stages."""

        if category is not ErrorCategory.TRANSIENT:
            return state.fail_stage(
                claim,
                category=category,
                error_code=error_code,
                error_detail=error_detail,
                now=self.clock(),
            )
        if claim.attempt_count >= _MAX_TRANSIENT_STAGE_ATTEMPTS:
            exhausted_detail = (
                f"{error_detail.strip() or error_code}; transient retry budget "
                f"({_MAX_TRANSIENT_STAGE_ATTEMPTS} attempts) exhausted"
            )
            return state.fail_stage(
                claim,
                category=ErrorCategory.RELEASE_CONTRACT,
                error_code=f"{error_code}_retry_exhausted",
                error_detail=exhausted_detail,
                now=self.clock(),
            )
        return state.fail_stage(
            claim,
            category=ErrorCategory.TRANSIENT,
            error_code=error_code,
            error_detail=error_detail,
            retry_at=self.clock() + _TRANSIENT_RETRY_DELAY,
            now=self.clock(),
        )

    def _extract(
        self,
        state: PipelineState,
        batch_path: Path,
        batch: dict[str, Any],
        documents: Mapping[str, Any],
    ) -> tuple[dict[str, ExtractionItem], list[str], int]:
        workflow = f"extract:{self.config.extractor_version}"
        previous = {
            path: state.get_stage_attempt(document.id, Stage.TEXT_EXTRACT, workflow)
            for path, document in documents.items()
        }
        claims = self._claims(state, documents=documents, stage=Stage.TEXT_EXTRACT, workflow_version=workflow)
        successful: dict[str, ExtractionItem] = {}
        failures: list[str] = []
        quarantined = 0
        artifact_recovery: dict[str, Any] = {}
        batch_items = {
            str(item["path"]): item
            for item in batch["files"]
            if isinstance(item, dict) and str(item.get("path", ""))
        }

        # A completed extraction is immutable input for the rest of the
        # pipeline.  On a publish/notification retry, rebuild the in-memory
        # item from its durable artifact rather than calling the legacy PDF
        # extractor again.  The batch JSON is only a compatibility projection,
        # so a fresh scanner manifest must not erase this recovery boundary.
        for path, attempt in previous.items():
            raw = batch_items.get(path)
            if raw is None or attempt is None:
                continue
            state_name = str(attempt.get("state", ""))
            if state_name == StageState.SUCCEEDED.value:
                try:
                    item = self._rehydrate_extraction(state, raw, attempt)
                except Exception as exc:
                    if state.requeue_succeeded_stage(
                        documents[path].id,
                        Stage.TEXT_EXTRACT,
                        workflow,
                        reason=f"durable extracted-text artifact unavailable: {exc}",
                        now=self.clock(),
                    ):
                        artifact_recovery[path] = documents[path]
                    else:
                        failures.append(
                            f"extract {raw.get('filename', Path(path).name)}: durable artifact unavailable: {exc}"
                        )
                else:
                    successful[path] = item
                continue
            if (
                state_name == StageState.QUARANTINED.value
                and str(attempt.get("error_category", "")) == ErrorCategory.CONTENT.value
            ):
                quarantined += 1
                detail = str(attempt.get("error_detail", "")).strip() or str(attempt.get("error_code", "")).strip()
                failures.append(f"extract {raw.get('filename', Path(path).name)}: quarantined{f': {detail}' if detail else ''}")
                continue
            if path not in claims and state_name:
                detail = str(attempt.get("error_detail", "")).strip() or state_name
                failures.append(f"extract {raw.get('filename', Path(path).name)}: pending durable state: {detail}")

        if artifact_recovery:
            claims.update(
                self._claims(
                    state,
                    documents=artifact_recovery,
                    stage=Stage.TEXT_EXTRACT,
                    workflow_version=workflow,
                )
            )

        if not claims:
            return successful, failures, quarantined

        # Give the legacy extractor a staging manifest containing only the
        # stages owned by this run.  It is allowed to mutate that staging file;
        # its output is merged into the full compatibility manifest only after
        # validation, preserving successful siblings and quarantines.
        staged_payload = dict(batch)
        staged_payload["files"] = [
            dict(item)
            for item in batch["files"]
            if isinstance(item, Mapping) and str(item.get("path", "")) in claims
        ]
        staged_payload["new_pdf_count"] = len(staged_payload["files"])
        staged_path = self.config.work_root / "extract" / f"claimed-{uuid.uuid4().hex}.json"
        _write_json(staged_path, staged_payload)
        try:
            result: ExtractionBatchResult = self.extractor.extract_batch(staged_path, self.config.work_root / "extract")
        except Exception as exc:
            category = _failure_category(exc)
            for claim in claims.values():
                self._fail_stage(
                    state,
                    claim,
                    category=category,
                    error_code="extractor_failed",
                    error_detail=str(exc),
                )
            return successful, [*failures, f"extraction: {exc}"], quarantined

        returned_files = result.manifest.get("files") if isinstance(result.manifest, Mapping) else None
        returned_by_path = {
            str(item.get("path", "")): item
            for item in returned_files or ()
            if isinstance(item, Mapping) and str(item.get("path", ""))
        }
        for item in result.items:
            claim = claims.get(item.path)
            if claim is None:
                failures.append(f"extract {item.filename}: extractor returned an unclaimed PDF")
                continue
            updated = returned_by_path.get(item.path)
            if updated is None:
                failures.append(f"extract {item.filename}: extractor omitted its manifest entry")
                self._fail_stage(
                    state,
                    claim,
                    category=ErrorCategory.INVARIANT,
                    error_code="extract_manifest_missing_item",
                    error_detail="extractor returned an item absent from its staged manifest",
                )
                continue
            destination = batch_items.get(item.path)
            if destination is not None:
                destination.update(dict(updated))
            if item.succeeded:
                if item.extractor_version != self.config.extractor_version:
                    failures.append(
                        f"extract {item.filename}: extractor profile {item.extractor_version!r} "
                        f"does not match configured {self.config.extractor_version!r}"
                    )
                    self._fail_stage(
                        state,
                        claim,
                        category=ErrorCategory.RELEASE_CONTRACT,
                        error_code="extractor_profile_mismatch",
                        error_detail=(
                            f"validated extractor profile {item.extractor_version!r} does not match "
                            f"configured {self.config.extractor_version!r}"
                        ),
                    )
                    continue
                successful[item.path] = item
                artifact = record_extracted_text_artifact(state, documents[item.path].id, item)
                state.complete_stage(claim, output_artifact_id=artifact.id, now=self.clock())
                continue
            category = item.error_category or ErrorCategory.INVARIANT
            failures.append(f"extract {item.filename}: {item.error_detail or item.error_code}")
            if category is ErrorCategory.CONTENT:
                quarantined += 1
                self._record_quarantine(item)
            self._fail_stage(
                state,
                claim,
                category=category,
                error_code=item.error_code or "extract_failed",
                error_detail=item.error_detail,
            )
        returned_paths = {item.path for item in result.items}
        for path, claim in claims.items():
            if path in returned_paths:
                continue
            filename = str(batch_items.get(path, {}).get("filename", Path(path).name))
            failures.append(f"extract {filename}: extractor did not return an outcome")
            self._fail_stage(
                state,
                claim,
                category=ErrorCategory.INVARIANT,
                error_code="extract_missing_outcome",
                error_detail="extractor did not return an outcome for its claimed PDF",
            )
        _write_json(batch_path, batch)
        return successful, failures, quarantined

    def _rehydrate_extraction(
        self,
        state: PipelineState,
        raw: dict[str, Any],
        attempt: Mapping[str, Any],
    ) -> ExtractionItem:
        """Validate a successful extract stage and rebuild its runtime item.

        Older imported rows may not carry an artifact foreign key.  For those
        rows only, retain the compatibility-manifest fallback; new direct
        worker rows always use the SQLite artifact as the authority.
        """

        artifact_id = attempt.get("output_artifact_id")
        if artifact_id is None:
            items = validate_extracted_manifest({"files": [dict(raw)]})
            if len(items) != 1 or not items[0].succeeded:
                raise ProcessError("successful stage has no reusable extraction manifest")
            item = items[0]
        else:
            artifact = state.get_artifact(int(artifact_id))
            if artifact is None or artifact.kind != "extracted_text":
                raise ProcessError("successful stage references no extracted-text artifact")
            expected_sha = str(raw.get("pdf_sha256", "")).strip().lower()
            if artifact.pdf_sha256 != expected_sha:
                raise ProcessError("extracted-text artifact PDF identity does not match the batch")
            if artifact.extractor_version != self.config.extractor_version:
                raise ProcessError("extracted-text artifact has a different extractor version")
            text_path = Path(artifact.canonical_path).expanduser().resolve(strict=False)
            try:
                text = text_path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError as exc:
                raise ProcessError(f"cannot read extracted-text artifact: {text_path}") from exc
            if not text:
                raise ProcessError("extracted-text artifact is empty")
            item = ExtractionItem(
                path=str(raw["path"]),
                filename=str(raw.get("filename") or Path(str(raw["path"])).name),
                pdf_sha256=expected_sha,
                extractor_version=artifact.extractor_version,
                status="success",
                text_path=text_path,
                text_chars=len(text),
                text_source="durable_state",
                cached=True,
            )
        if item.extractor_version != self.config.extractor_version:
            raise ProcessError("extracted-text manifest has a different extractor version")
        raw.update(_extraction_fields(item))
        return item

    def _record_quarantine(self, item: ExtractionItem) -> None:
        try:
            existing = json.loads(self.config.quarantine_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {"entries": []}
        entries = existing.get("entries") if isinstance(existing, Mapping) else []
        entries = list(entries) if isinstance(entries, list) else []
        key = (item.pdf_sha256, item.path)
        rows = [row for row in entries if isinstance(row, Mapping) and (str(row.get("pdf_sha256", "")), str(row.get("path", ""))) != key]
        rows.append(
            {
                "pdf_sha256": item.pdf_sha256,
                "path": item.path,
                "filename": item.filename,
                "error_code": item.error_code,
                "error_detail": item.error_detail,
                "updated_at": self.clock().isoformat(),
            }
        )
        _write_json(self.config.quarantine_path, {"schema_version": 1, "entries": rows})

    def _summarize(
        self,
        state: PipelineState,
        batch: Mapping[str, Any],
        extracted: Mapping[str, ExtractionItem],
        documents: Mapping[str, Any],
    ) -> tuple[dict[str, PersistedSummary], int, list[str]]:
        current_files = [item for item in batch["files"] if isinstance(item, Mapping) and str(item["path"]) in extracted]
        cache_hits = 0
        completed: dict[str, PersistedSummary] = {}
        failures: list[str] = []
        jobs: list[SummaryJob] = []
        job_context: dict[str, tuple[dict[str, Any], SummaryIdentity, StageClaim]] = {}
        batch_items = {
            str(item["path"]): item
            for item in batch["files"]
            if isinstance(item, dict) and str(item.get("path", ""))
        }
        for raw in current_files:
            item = dict(raw)
            item.update(_extraction_fields(extracted[str(item["path"])]))
            if str(item["path"]) in batch_items:
                batch_items[str(item["path"])].update(_extraction_fields(extracted[str(item["path"])]))
            single = _single_manifest(batch, item)
            identity = identities_for_manifest(
                single,
                prompt_version=self.prompt_version,
                model=self.config.codex_model,
                reasoning=self.config.codex_reasoning,
            )[str(item["path"])]
            workflow = f"summary:{identity.cache_key}"
            claims = self._claims(
                state,
                documents={str(item["path"]): documents[str(item["path"])]},
                stage=Stage.SUMMARY,
                workflow_version=workflow,
            )
            claim = claims.get(str(item["path"]))
            output_json, output_markdown = self._summary_output_paths(identity)
            cached = materialize_summary_cache(
                single,
                identities={str(item["path"]): identity},
                store=self.summary_store,
                output_json=output_json,
                output_markdown=output_markdown,
            )
            if cached is not None:
                persisted = cached.entries[0]
                completed[persisted.entry.path] = persisted
                cache_hits += 1
                if claim is not None:
                    artifact = record_summary_artifact(state, documents[persisted.entry.path].id, persisted)
                    state.complete_stage(claim, output_artifact_id=artifact.id, now=self.clock())
                self._project_summary_sidecar(batch_items[persisted.entry.path], persisted, failures)
                continue
            if claim is None:
                continue
            jobs.append(SummaryJob(job_id=identity.cache_key, expected_paths=(str(item["path"]),), payload={"manifest": single}))
            job_context[identity.cache_key] = (item, identity, claim)

        def run_job(job: SummaryJob) -> Any:
            manifest = job.payload["manifest"]
            assert isinstance(manifest, Mapping)
            inputs = tuple(
                CodexSummaryInput(path=value.path, filename=value.filename, text=value.text)
                for value in build_summary_inputs(manifest)
            )
            result = self.provider.summarize(
                CodexSummaryRequest(
                    job_id=job.job_id,
                    manifest=manifest,
                    inputs=inputs,
                    prompt=self._prompt,
                    system_prompt=self._system_prompt,
                    expected_paths=job.expected_paths,
                )
            )
            usage = getattr(result, "usage", {})
            if isinstance(usage, Mapping) and usage:
                self._usage.append(dict(usage))
            return result

        for outcome in run_summary_jobs(jobs, run_job, max_workers=self.config.summary_max_workers):
            item, identity, claim = job_context[outcome.job.job_id]
            path = str(item["path"])
            if outcome.error is not None:
                category = _failure_category(outcome.error)
                self._fail_stage(
                    state,
                    claim,
                    category=category,
                    error_code="codex_summary_failed",
                    error_detail=str(outcome.error),
                )
                failures.append(f"summary {item['filename']}: {outcome.error}")
                continue
            try:
                artifact = persist_summary_batch(
                    outcome.job.payload["manifest"],
                    outcome.result,
                    identities={path: identity},
                    store=self.summary_store,
                    output_json=self._summary_output_paths(identity)[0],
                    output_markdown=self._summary_output_paths(identity)[1],
                )
                persisted = artifact.entries[0]
                recorded = record_summary_artifact(state, documents[path].id, persisted)
                state.complete_stage(claim, output_artifact_id=recorded.id, now=self.clock())
                completed[path] = persisted
                self._project_summary_sidecar(batch_items[path], persisted, failures)
            except Exception as exc:
                category = _failure_category(exc)
                self._fail_stage(
                    state,
                    claim,
                    category=category,
                    error_code="summary_artifact_failed",
                    error_detail=str(exc),
                )
                failures.append(f"summary {item['filename']}: {exc}")
        return completed, cache_hits, failures

    def _project_summary_sidecar(
        self,
        item: dict[str, Any],
        persisted: PersistedSummary,
        failures: list[str],
    ) -> None:
        """Maintain the readable library projection without changing summary truth."""

        try:
            destination = self.sidecars.persist_summary(item, persisted)
        except Exception as exc:
            failures.append(f"ResearchLibrary {item.get('filename', persisted.entry.filename)}: {exc}")
            destination = persisted.paths.markdown_path
        item["summary_md_path"] = str(destination)
        item["summary_cache_path"] = str(persisted.paths.markdown_path)
        item["summary_cache_key"] = persisted.identity.cache_key

    def _archive_published_sidecars(
        self,
        batch: Mapping[str, Any],
        records: Sequence[tuple[Any, Any]],
    ) -> list[str]:
        """Refresh Obsidian only after a publication is durably successful."""

        failures: list[str] = []
        batch_items = {
            str(item["path"]): item
            for item in batch["files"]
            if isinstance(item, dict) and str(item.get("path", ""))
        }
        for group, publication in records:
            reference = str(publication.remote_reference or "").strip()
            if not reference:
                failures.append(f"Obsidian {group.title}: successful publication had no document URL")
                continue
            try:
                result = self.sidecars.archive_published_group(
                    entries=group.entries,
                    batch_items=batch_items,
                    document_url=reference,
                )
                if result.manifest_path is not None and result.manifest_path.is_file():
                    payload = json.loads(result.manifest_path.read_text(encoding="utf-8"))
                    for item in payload.get("files", []):
                        if isinstance(item, Mapping) and str(item.get("path", "")) in batch_items:
                            batch_items[str(item["path"])].update(dict(item))
            except Exception as exc:
                failures.append(f"Obsidian {group.title}: {exc}")
        return failures

    def _summary_output_paths(self, identity: SummaryIdentity) -> tuple[Path, Path]:
        root = self.config.work_root / "summary-batches"
        return root / f"{identity.cache_key}.json", root / f"{identity.cache_key}.md"

    def _publish(
        self,
        state: PipelineState,
        batch: Mapping[str, Any],
        summaries: Mapping[str, PersistedSummary],
        documents: Mapping[str, Any],
        *,
        run_id: str,
        queue_notifications: bool,
    ) -> tuple[int, list[str], tuple[tuple[Any, Any], ...]]:
        source_items = {str(item["path"]): item for item in batch["files"] if isinstance(item, Mapping)}
        entries = [
            SummaryForPublish(
                summary_sha256=persisted.markdown_sha256,
                markdown=persisted.entry.markdown,
                source=str(source_items[path].get("batch_id", "") or self.config.source),
                path=path,
                filename=persisted.entry.filename,
                source_date=str(source_items[path].get("modified_at", "") or batch.get("generated_at", "")),
            )
            for path, persisted in summaries.items()
        ]
        groups = build_publication_groups(
            entries,
            target=self.config.target,
            target_document=self.config.target_document,
            doc_group_size=self.config.doc_group_size,
            doc_group_threshold=self.config.doc_group_threshold,
        )
        published = 0
        failures: list[str] = []
        records: list[tuple[Any, Any]] = []
        workflow = f"publish:{self.config.target}:{self.config.target_document or 'new'}"
        claims = self._claims(
            state,
            documents={path: documents[path] for path in summaries},
            stage=Stage.PUBLISH,
            workflow_version=workflow,
        )
        same_day_capacity_attempted = False
        for group in groups:
            group_claims = [claims.get(entry.path) for entry in group.entries]
            owned_claims = tuple(claim for claim in group_claims if claim is not None)
            if not owned_claims:
                continue
            # A crash can happen after a group publication becomes successful
            # but before every per-PDF PUBLISH stage is acknowledged.  Resume
            # that durable success to complete only the owned remainder.  Do
            # not bypass a sibling's retry cooldown before any publication
            # exists for the group.
            existing_variants = state.find_publications(group.summary_sha256, group.target, group.partition_key)
            has_success = any(item.state is PublicationState.SUCCESS for item in existing_variants)
            if any(claim is None for claim in group_claims) and not has_success:
                continue
            effective_group = group
            if not group.target_document and not same_day_capacity_attempted:
                # Preserve the old boundary: only the first newly-published
                # group of a run may continue a compatible same-day document.
                # Later groups use their deterministic group partition.
                effective_group = resolve_same_day_capacity_target(
                    state,
                    group,
                    max_files_per_document=self.config.max_files_per_document,
                )
                same_day_capacity_attempted = True
            try:
                result = publish_group(state, self.publisher, effective_group, chat_id=self.config.target_chat_id)
                publication = _published_record(state, effective_group, result.remote_reference)
                # Queue the durable document notification *before* marking a
                # per-document stage succeeded.  If the process dies in this
                # small interval, the still-running/reclaimable stage drives a
                # later idempotent enqueue instead of losing the outbox row.
                if queue_notifications:
                    try:
                        self._queue_document_notifications(state, ((effective_group, publication),), run_id)
                    except Exception as exc:
                        failures.append(f"notify {effective_group.title}: unable to enqueue document notice: {exc}")
                for claim in owned_claims:
                    state.complete_stage(claim, now=self.clock())
                published += 1
                records.append((effective_group, publication))
            except Exception as exc:
                category = _failure_category(exc)
                for claim in owned_claims:
                    self._fail_stage(
                        state,
                        claim,
                        category=category,
                        error_code="lark_publish_failed",
                        error_detail=str(exc),
                    )
                failures.append(f"publish {effective_group.title}: {exc}")
        return published, failures, tuple(records)

    def _queue_document_notifications(self, state: PipelineState, records: Sequence[tuple[Any, Any]], run_id: str) -> None:
        if not self.config.target_chat_id:
            return
        for group, publication in records:
            enqueue_document_notification(
                state,
                publication,
                chat_id=self.config.target_chat_id,
                markdown=render_document_notice(publication, title=group.title, count=len(group.entries)),
                scope_key=run_id,
            )

    def _write_status(self, run_id: str, status: str, phase: str) -> None:
        _write_json(
            self.config.run_status_path,
            {
                "status": status,
                "phase": phase,
                "run_id": run_id,
                "last_heartbeat_at": self.clock().isoformat(),
                "pipeline": "direct-codex-lark",
            },
        )

    def _write_outcome(self, outcome: ProcessOutcome, *, phase: str) -> None:
        payload = outcome.to_dict()
        payload["completed_at"] = self.clock().isoformat()
        _write_json(self.config.result_path, payload)
        lines = [
            "# 知识星球研报总结运行结果",
            "",
            f"- 状态：{outcome.status}",
            f"- 文件：{outcome.files_seen}",
            f"- 提取成功：{outcome.extracted}",
            f"- 摘要：{outcome.summarized}（缓存命中 {outcome.cache_hits}）",
            f"- 飞书文档：{outcome.published}",
            f"- 隔离：{outcome.quarantined}",
        ]
        if outcome.failures:
            lines.extend(("", "## 失败", *[f"- {value}" for value in outcome.failures]))
        _write_text(self.config.result_markdown_path, "\n".join(lines) + "\n")
        self._write_status(outcome.run_id, outcome.status, phase)


def _safe_result(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _safe_result(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_safe_result(item) for item in value]
    if hasattr(value, "argv"):
        return {"argv": list(getattr(value, "argv", ())), "returncode": getattr(value, "returncode", None)}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return type(value).__name__


def _extraction_fields(item: ExtractionItem) -> dict[str, Any]:
    assert item.text_path is not None
    return {
        "pdf_sha256": item.pdf_sha256,
        "text_extract_profile": item.extractor_version,
        "extracted_text_path": str(item.text_path),
        "extracted_text_chars": item.text_chars,
        "text_source": item.text_source,
        "text_extract_status": "success",
    }


def _single_manifest(batch: Mapping[str, Any], item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "generated_at": str(batch.get("generated_at", "")),
        "chunk_index": 1,
        "chunk_total": 1,
        "new_pdf_count": 1,
        "files": [dict(item)],
    }


def _published_record(state: PipelineState, group: Any, reference: str):
    records = state.find_publications(group.summary_sha256, group.target, group.partition_key)
    found = next(
        (
            record
            for record in records
            if record.state is PublicationState.SUCCESS and str(record.remote_reference or "") == str(reference)
        ),
        None,
    )
    if found is None:
        raise ProcessError("successful publication was not found in durable state")
    return found


def processor_from_config(config: ProcessConfig) -> DigestProcessor:
    """Public factory used by the CLI and isolated integration tests."""

    return DigestProcessor(config)


__all__ = [
    "DigestProcessor",
    "ProcessBusyError",
    "ProcessConfig",
    "ProcessError",
    "ProcessOutcome",
    "ProcessRequest",
    "processor_from_config",
]
